"""ProviderCatalog.refresh_async — Roadmap §3.5 P0 ().

The runtime refresher keeps an already-running brain's model catalog
current without waiting for the next ``provider-research.yml`` cron PR.
This file pins three contracts:

1. ``refresh_async()`` actually issues live ``refresh_models`` calls
   for providers that have a configured key.
2. Providers without a credential are SKIPPED (no wasted HTTP).
3. The catalog's last-refresh timestamp moves forward and an info
   line lands in the logger so operators can see the run.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from providers.base import BaseProvider
from providers.catalog import (
    CachedModelList,
    ProviderCatalog,
    ProviderDescriptor,
)


class CountingAdapter(BaseProvider):
    """Adapter double that counts how many times refresh_models is hit."""

    provider_id = "counting"
    display_name = "Counting"
    _models = ["bundled-fallback"]

    def __init__(self, models: list[str], *, raises: bool = False) -> None:
        self._live_models = list(models)
        self._raises = raises
        self.refresh_calls = 0

    async def chat(self, *a, **kw):  # pragma: no cover - unused here
        raise NotImplementedError

    async def refresh_models(self):
        self.refresh_calls += 1
        if self._raises:
            raise RuntimeError("simulated network failure")
        return list(self._live_models)


def _make_descriptor(
    pid: str,
    *,
    requires_api_key: bool = True,
    env_var: str = "DUMMY_KEY",
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=pid,
        display_name=pid.title(),
        supports_local=False,
        requires_api_key=requires_api_key,
        default_base_url=f"https://api.{pid}.test",
        default_model="",
        credential_env_var=env_var,
    )


@pytest.fixture
def in_memory_cache(tmp_path) -> Path:
    """In-memory-ish cache path under tmp_path (per the spec)."""
    return tmp_path / "model_catalog.json"


@pytest.fixture
def configured_catalog(in_memory_cache, monkeypatch):
    """Catalog with two providers: ``openai`` configured + ``anthropic`` not."""
    monkeypatch.setenv("DUMMY_OPENAI_KEY", "sk-test-key")
    monkeypatch.delenv("DUMMY_ANTHROPIC_KEY", raising=False)
    descriptors = (
        _make_descriptor("openai", env_var="DUMMY_OPENAI_KEY"),
        _make_descriptor("anthropic", env_var="DUMMY_ANTHROPIC_KEY"),
    )
    cat = ProviderCatalog(cache_path=in_memory_cache, descriptors=descriptors)
    return cat


class TestRefreshAsyncRunsAtStartup:
    @pytest.mark.asyncio
    async def test_refreshes_only_configured_providers(self, configured_catalog):
        # Replace the freshly-bound (None) adapters with counting ones.
        openai_adapter = CountingAdapter(models=["gpt-fresh-1", "gpt-fresh-2"])
        anthropic_adapter = CountingAdapter(models=["claude-fresh-1"])
        # Bind by replacing provider_id on the test doubles so
        # register_adapter() finds the right descriptor.
        openai_adapter.provider_id = "openai"
        anthropic_adapter.provider_id = "anthropic"
        configured_catalog.register_adapter(openai_adapter)
        configured_catalog.register_adapter(anthropic_adapter)

        result = await configured_catalog.refresh_async()

        # Openai had a key — refreshed.
        assert "openai" in result
        assert openai_adapter.refresh_calls == 1
        # Anthropic had no key — skipped.
        assert "anthropic" not in result
        assert anthropic_adapter.refresh_calls == 0

    @pytest.mark.asyncio
    async def test_writes_new_last_refresh_timestamp(self, configured_catalog):
        openai_adapter = CountingAdapter(models=["pinned"])
        openai_adapter.provider_id = "openai"
        configured_catalog.register_adapter(openai_adapter)

        before = time.time()
        await configured_catalog.refresh_async()
        after = time.time()

        cached = await configured_catalog.list_models("openai", live=False)
        assert cached.models == ["pinned"]
        assert before <= cached.last_refresh <= after, (
            "refresh_async should land a fresh last_refresh timestamp "
            "between the call's start and end"
        )

    @pytest.mark.asyncio
    async def test_writes_an_info_line(self, configured_catalog, caplog):
        openai_adapter = CountingAdapter(models=["pinned"])
        openai_adapter.provider_id = "openai"
        configured_catalog.register_adapter(openai_adapter)

        with caplog.at_level(logging.INFO, logger="feral.providers.catalog"):
            await configured_catalog.refresh_async()

        info_lines = [
            r.getMessage()
            for r in caplog.records
            if r.name == "feral.providers.catalog" and r.levelno == logging.INFO
        ]
        assert any(
            "refresh_async" in line.lower() for line in info_lines
        ), (
            "refresh_async must emit an info-level summary so "
            "operators can see the run in the brain log: "
            f"got {info_lines!r}"
        )

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_provider_has_a_key(
        self, in_memory_cache, monkeypatch
    ):
        # Two providers, both lack their env keys.
        monkeypatch.delenv("DUMMY_OPENAI_KEY", raising=False)
        monkeypatch.delenv("DUMMY_ANTHROPIC_KEY", raising=False)
        descriptors = (
            _make_descriptor("openai", env_var="DUMMY_OPENAI_KEY"),
            _make_descriptor("anthropic", env_var="DUMMY_ANTHROPIC_KEY"),
        )
        cat = ProviderCatalog(cache_path=in_memory_cache, descriptors=descriptors)

        # Sanity: even if adapters exist, they should not be polled.
        a = CountingAdapter(models=["never-called"])
        a.provider_id = "openai"
        cat.register_adapter(a)

        result = await cat.refresh_async()
        assert result == {}
        assert a.refresh_calls == 0

    @pytest.mark.asyncio
    async def test_provider_failure_does_not_crash_the_loop(self, configured_catalog):
        # Configure two providers; one raises, one succeeds.
        # We swap in two counting doubles via register_adapter.
        good = CountingAdapter(models=["good-model"])
        good.provider_id = "openai"
        bad = CountingAdapter(models=[], raises=True)
        bad.provider_id = "anthropic"
        configured_catalog.register_adapter(good)
        configured_catalog.register_adapter(bad)

        # Give anthropic a key so it gets included in the candidate list.
        import os as _os
        _os.environ["DUMMY_ANTHROPIC_KEY"] = "sk-x"
        try:
            result = await configured_catalog.refresh_async()
        finally:
            _os.environ.pop("DUMMY_ANTHROPIC_KEY", None)

        # ``good`` succeeds → present in result.
        assert "openai" in result
        assert result["openai"].models == ["good-model"]


class TestRefreshAsyncRespectsConcurrency:
    @pytest.mark.asyncio
    async def test_max_concurrency_caps_parallel_calls(
        self, in_memory_cache, monkeypatch
    ):
        # Build 5 providers, all configured, with a slow adapter that
        # records the maximum number of in-flight refreshes seen.
        for i in range(5):
            monkeypatch.setenv(f"DUMMY_KEY_{i}", "sk-x")

        descriptors = tuple(
            _make_descriptor(f"prov{i}", env_var=f"DUMMY_KEY_{i}")
            for i in range(5)
        )
        cat = ProviderCatalog(cache_path=in_memory_cache, descriptors=descriptors)

        in_flight = 0
        max_in_flight = 0

        class SlowAdapter(BaseProvider):
            display_name = "Slow"
            _models = ["fallback"]

            def __init__(self, pid: str):
                self.provider_id = pid

            async def chat(self, *a, **kw):  # pragma: no cover
                raise NotImplementedError

            async def refresh_models(self):
                import asyncio as _aio
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                try:
                    await _aio.sleep(0.05)
                    return [f"{self.provider_id}-model"]
                finally:
                    in_flight -= 1

        for i in range(5):
            cat.register_adapter(SlowAdapter(f"prov{i}"))

        await cat.refresh_async(max_concurrency=2)

        assert max_in_flight <= 2, (
            f"refresh_async ignored max_concurrency=2 — saw "
            f"{max_in_flight} simultaneous adapter calls"
        )
