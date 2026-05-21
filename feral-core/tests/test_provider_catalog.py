"""Unit tests for ProviderCatalog — the unified LLM provider + model registry."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from providers.base import BaseProvider
from providers.catalog import (
    BUILT_IN_DESCRIPTORS,
    CachedModelList,
    ProviderCatalog,
    ProviderDescriptor,
    get_shared_catalog,
    reset_shared_catalog,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def empty_cache(tmp_path) -> Path:
    return tmp_path / "model_catalog.json"


@pytest.fixture
def catalog(empty_cache) -> ProviderCatalog:
    # Clear env so the built-in adapter factory doesn't see production keys.
    with patch.dict("os.environ", {}, clear=False):
        # Belt-and-suspenders: also drop the common API keys individually.
        for key in (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
            "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
            "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "AWS_ACCESS_KEY_ID",
        ):
            if key in __import__("os").environ:
                del __import__("os").environ[key]
        yield ProviderCatalog(cache_path=empty_cache)


# ----------------------------------------------------------------------
# Descriptor + list shape
# ----------------------------------------------------------------------


class TestDescriptors:
    def test_built_in_descriptors_cover_core_providers(self, catalog):
        ids = {d.provider_id for d in catalog.list_providers()}
        for pid in ("openai", "anthropic", "gemini", "ollama", "groq"):
            assert pid in ids, f"descriptor missing for {pid}"

    def test_list_providers_is_sorted(self, catalog):
        ids = [d.provider_id for d in catalog.list_providers()]
        assert ids == sorted(ids)

    def test_get_descriptor_known(self, catalog):
        d = catalog.get_descriptor("openai")
        assert d is not None
        assert d.display_name == "OpenAI"
        assert d.requires_api_key is True
        assert d.supports_local is False

    def test_get_descriptor_unknown_returns_none(self, catalog):
        assert catalog.get_descriptor("not-real") is None

    def test_ollama_marked_local(self, catalog):
        d = catalog.get_descriptor("ollama")
        assert d is not None
        assert d.supports_local is True
        assert d.requires_api_key is False
        assert "11434" in d.default_base_url


# ----------------------------------------------------------------------
# Alias resolution
# ----------------------------------------------------------------------


class TestAliases:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("openai", "openai"),
            ("OpenAI", "openai"),
            ("open ai", "openai"),
            ("chatgpt", "openai"),
            ("claude", "anthropic"),
            ("anthropic", "anthropic"),
            ("google gemini", "gemini"),
            ("groq", "groq"),
            ("  openrouter  ", "openrouter"),
            ("open router", "openrouter"),
        ],
    )
    def test_resolves_canonical_and_alias(self, catalog, text, expected):
        assert catalog.resolve_alias(text) == expected

    def test_substring_unambiguous_wins(self, catalog):
        # "llama" is a substring of "ollama" and matches no other id.
        assert catalog.resolve_alias("llama") == "ollama"
        # "deepseek" is unambiguous even as substring.
        assert catalog.resolve_alias("seek") == "deepseek"

    def test_empty_input_returns_none(self, catalog):
        assert catalog.resolve_alias("") is None
        assert catalog.resolve_alias("   ") is None

    def test_ambiguous_substring_returns_none(self, catalog):
        # "o" appears in multiple provider ids; must not silently pick one.
        assert catalog.resolve_alias("o") is None


# ----------------------------------------------------------------------
# Caching + live refresh
# ----------------------------------------------------------------------


class FakeAdapter(BaseProvider):
    """Adapter double that returns a configurable model list."""

    provider_id = "openai"
    display_name = "OpenAI"
    _models = ["fallback-model"]

    def __init__(self, models: list[str], *, raises: bool = False) -> None:
        self._live_models = list(models)
        self._raises = raises

    async def chat(self, *a, **kw):  # pragma: no cover - unused here
        raise NotImplementedError

    async def refresh_models(self):
        if self._raises:
            raise RuntimeError("simulated network failure")
        return list(self._live_models)


class TestModelLists:
    @pytest.mark.asyncio
    async def test_first_call_goes_live_and_caches(self, catalog):
        catalog.register_adapter(FakeAdapter(models=["gpt-9", "gpt-9-mini"]))
        result = await catalog.list_models("openai", live=True, force=True)
        assert result.models == ["gpt-9", "gpt-9-mini"]
        assert result.source == "live"
        assert result.last_refresh > 0

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_hits_cache(self, catalog):
        catalog.register_adapter(FakeAdapter(models=["m1"]))
        first = await catalog.list_models("openai", live=True, force=True)
        # Swap the adapter so a live call would return different data.
        catalog.register_adapter(FakeAdapter(models=["m2"]))
        second = await catalog.list_models("openai", live=True, force=False)
        assert second.models == first.models == ["m1"]

    @pytest.mark.asyncio
    async def test_force_true_ignores_cache(self, catalog):
        catalog.register_adapter(FakeAdapter(models=["m1"]))
        await catalog.list_models("openai", force=True)
        catalog.register_adapter(FakeAdapter(models=["m2"]))
        out = await catalog.list_models("openai", force=True)
        assert out.models == ["m2"]

    @pytest.mark.asyncio
    async def test_live_false_returns_cache_without_refresh(self, catalog):
        catalog.register_adapter(FakeAdapter(models=["cached"]))
        await catalog.list_models("openai", live=True, force=True)
        catalog.register_adapter(FakeAdapter(models=["fresh"], raises=True))
        out = await catalog.list_models("openai", live=False)
        assert out.models == ["cached"]

    @pytest.mark.asyncio
    async def test_refresh_failure_falls_back_to_list_models(self, catalog):
        # No prior cache; live call raises. Adapter's _models (fallback
        # list) should be surfaced with source="fallback".
        catalog.register_adapter(FakeAdapter(models=[], raises=True))
        out = await catalog.list_models("openai", force=True)
        assert out.source == "fallback"

    @pytest.mark.asyncio
    async def test_list_models_unknown_provider_raises(self, catalog):
        with pytest.raises(KeyError):
            await catalog.list_models("not-a-provider")


# ----------------------------------------------------------------------
# Probe
# ----------------------------------------------------------------------


class TestProbe:
    @pytest.mark.asyncio
    async def test_probe_reachable_provider(self, catalog):
        catalog.register_adapter(FakeAdapter(models=["ready"]))
        status = await catalog.probe("openai")
        assert status.reachable is True
        assert status.error == ""

    @pytest.mark.asyncio
    async def test_probe_unreachable_reports_error(self, catalog):
        catalog.register_adapter(FakeAdapter(models=[], raises=True))
        status = await catalog.probe("openai")
        assert status.reachable is False

    @pytest.mark.asyncio
    async def test_probe_unknown_provider_honest(self, catalog):
        status = await catalog.probe("ghost")
        assert status.reachable is False
        assert "unknown" in status.error


# ----------------------------------------------------------------------
# Disk cache
# ----------------------------------------------------------------------


class TestDiskCache:
    @pytest.mark.asyncio
    async def test_cache_persists_to_disk(self, catalog, empty_cache):
        catalog.register_adapter(FakeAdapter(models=["persisted"]))
        await catalog.list_models("openai", force=True)
        assert empty_cache.is_file()
        raw = json.loads(empty_cache.read_text())
        assert raw["providers"]["openai"]["models"] == ["persisted"]

    def test_load_from_disk_rehydrates(self, tmp_path):
        cache = tmp_path / "cache.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({
            "schema_version": 1,
            "providers": {
                "openai": {"models": ["from-disk"], "last_refresh": 99.0, "source": "cache"},
            },
        }))
        catalog = ProviderCatalog(cache_path=cache)
        # Without a live call the cached value is what list_models returns.
        import asyncio as _aio
        out = _aio.run(catalog.list_models("openai", live=False))
        assert out.models == ["from-disk"]

    def test_corrupted_cache_is_ignored(self, tmp_path):
        cache = tmp_path / "bad.json"
        cache.write_text("not json")
        # Must not raise.
        ProviderCatalog(cache_path=cache)


# ----------------------------------------------------------------------
# Configure
# ----------------------------------------------------------------------


class TestConfigure:
    def test_configure_rebinds_api_key(self, catalog):
        catalog.configure("openai", api_key="sk-test")
        adapter = catalog.get_adapter("openai")
        assert adapter is not None
        # The adapter exposes _api_key on the concrete class.
        assert getattr(adapter, "_api_key", None) == "sk-test"

    def test_configure_unknown_provider_raises(self, catalog):
        with pytest.raises(KeyError):
            catalog.configure("not-real", api_key="k")


# ----------------------------------------------------------------------
# Shared singleton
# ----------------------------------------------------------------------


class TestSharedSingleton:
    def test_get_shared_returns_same_instance(self, tmp_path, monkeypatch):
        reset_shared_catalog()
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        first = get_shared_catalog()
        second = get_shared_catalog()
        assert first is second
        reset_shared_catalog()


# ----------------------------------------------------------------------
# Bundled model_catalog.json freshness — Roadmap §3.5 P0 (W1)
# ----------------------------------------------------------------------
#
# These assertions pin the verified-current frontier model IDs as of
# 2026-04-24 (the day GPT-5.5 shipped) to the bundled
# ``feral-core/providers/model_catalog.json`` file. They fail loudly
# the moment a known-deprecated literal sneaks back in via a botched
# refresh / merge — the exact failure mode that produced the
# "Settings → Providers shows GPT-4o-mini" bug from Appendix A.1.


# Verified current as of 2026-04-24. Source: openai.com,
# platform.claude.com, ai.google.dev (cited in
# docs/AGENT_PROMPTS.md §A and §D.W1).
_VERIFIED_OPENAI_IDS = {
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
}
_VERIFIED_ANTHROPIC_IDS = {
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
}
_VERIFIED_GEMINI_IDS = {
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
}

# Anything older than the 2026-04 frontier rollover. If one of these
# names appears in the bundled catalog, the picker will show stale
# defaults to the user — refuse the build.
_DEPRECATED_OPENAI_IDS = {"gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4"}
_DEPRECATED_ANTHROPIC_IDS = {
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
}
# Only 2.0 and older count as "deprecated" for the recommended
# shortlist. 2.5 remains the stable cost-effective tier alongside 3.x
# / 3.1 flagship — the W24a recommended list keeps 2.5-pro,
# 2.5-flash, 2.5-flash-lite for users who want the cheaper models.
_DEPRECATED_GEMINI_IDS = {
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash-lite-001",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "gemini-1.0-pro",
}


def _bundled_catalog() -> dict:
    catalog_path = (
        Path(__file__).resolve().parent.parent
        / "providers"
        / "model_catalog.json"
    )
    return json.loads(catalog_path.read_text())


class TestBundledCatalogFreshness:
    def test_last_fetched_marker_present(self):
        raw = _bundled_catalog()
        assert raw.get("last_fetched"), (
            "model_catalog.json must carry a non-empty top-level "
            "'last_fetched' so the v2 picker can render an age badge"
        )

    def test_anthropic_has_models_endpoint(self):
        """As of 2026-04-26 Anthropic publishes /v1/models (the live
        refresh script fetches it), so the bundled entry's endpoint is
        no longer null. Previous 2026-04-24 assumption was that
        Anthropic had no public /v1/models — the W24a live refresh on
        2026-04-26 confirmed 9 models returned and the endpoint is real.
        """
        raw = _bundled_catalog()
        anthropic = raw["providers"]["anthropic"]
        endpoint = anthropic.get("endpoint")
        assert endpoint == "https://api.anthropic.com/v1/models", (
            f"Anthropic now publishes /v1/models as of 2026-04-26; "
            f"bundled entry's endpoint must match. Got: {endpoint!r}"
        )
        # last_fetched tracks when the live refresh last ran. The old
        # curated_at marker stays too (as a "human last reviewed" date)
        # but is no longer required — the machine has an endpoint.
        assert anthropic.get("last_fetched") or anthropic.get("curated_at"), (
            "Anthropic entry must carry last_fetched (from live refresh) "
            "OR curated_at (from human review) so the UI can display "
            "'list age = N days'"
        )

    def test_openai_contains_every_verified_frontier_id(self):
        models = set(_bundled_catalog()["providers"]["openai"]["models"])
        missing = _VERIFIED_OPENAI_IDS - models
        assert not missing, (
            f"openai.models is missing verified 2026-04 frontier IDs: "
            f"{sorted(missing)}"
        )

    def test_anthropic_contains_every_verified_frontier_id(self):
        """Live /v1/models returns dated snapshots for some IDs (e.g.
        ``claude-haiku-4-5-20251001`` instead of the ``claude-haiku-4-5``
        alias). Anthropic treats them as identical weights. Accept a
        model as "present" iff the verified id is a prefix of any
        listed id — captures both the alias and its dated twin.
        """
        models = _bundled_catalog()["providers"]["anthropic"]["models"]
        missing = {
            v for v in _VERIFIED_ANTHROPIC_IDS
            if not any(m == v or m.startswith(v + "-") for m in models)
        }
        assert not missing, (
            f"anthropic.models is missing verified 2026-04 frontier IDs "
            f"(neither alias nor dated snapshot): {sorted(missing)}"
        )

    def test_gemini_contains_every_verified_frontier_id(self):
        models = set(_bundled_catalog()["providers"]["gemini"]["models"])
        missing = _VERIFIED_GEMINI_IDS - models
        assert not missing, (
            f"gemini.models is missing verified 2026-04 frontier IDs: "
            f"{sorted(missing)}"
        )

    def test_openai_recommended_filter_drops_known_deprecated_ids(self):
        """Post-W24a the bundled catalog is the RAW live /v1/models
        response (so legacy ids like ``gpt-4o-mini`` still appear in
        the catalog). The UX guarantee lives at the filter layer:
        ``recommended_for()`` must drop the deprecated set that drove
        the Settings → Providers stale-dropdown bug.
        """
        from providers.recommended import recommended_for
        models = _bundled_catalog()["providers"]["openai"]["models"]
        recommended = set(recommended_for("openai", models))
        leaked = _DEPRECATED_OPENAI_IDS & recommended
        assert not leaked, (
            f"openai recommended shortlist still contains deprecated IDs "
            f"that drove the stale-dropdown bug: {sorted(leaked)}"
        )

    def test_anthropic_recommended_filter_drops_known_deprecated_ids(self):
        from providers.recommended import recommended_for
        models = _bundled_catalog()["providers"]["anthropic"]["models"]
        recommended = set(recommended_for("anthropic", models))
        leaked = _DEPRECATED_ANTHROPIC_IDS & recommended
        assert not leaked, (
            f"anthropic recommended shortlist still contains deprecated "
            f"3.5/3 IDs: {sorted(leaked)}"
        )

    def test_gemini_recommended_filter_drops_legacy_20_ids(self):
        """2.0 / 1.x Gemini ids are still in the raw live catalog
        (Google hasn't removed them), but the conductor-curated
        recommended shortlist drops them — new users get 3.1 / 3 / 2.5
        picks only.
        """
        from providers.recommended import recommended_for
        models = _bundled_catalog()["providers"]["gemini"]["models"]
        recommended = set(recommended_for("gemini", models))
        leaked = _DEPRECATED_GEMINI_IDS & recommended
        assert not leaked, (
            f"gemini recommended shortlist still contains 2.x IDs: "
            f"{sorted(leaked)}"
        )

    def test_openai_recommended_head_is_a_frontier_id(self):
        """After chat-only + recommended filtering, the first entry
        must be a current GPT-5 frontier id so fresh installs land on
        a sensible default via ``default_model_for()``. The raw catalog
        head might be ``babbage-002`` (alphabetical live /v1/models
        ordering) — that's expected and handled by the filter layer.
        """
        from providers.recommended import recommended_for
        from providers.model_classes import filter_models
        models = _bundled_catalog()["providers"]["openai"]["models"]
        chat_only = filter_models("openai", models, model_class="chat")
        recommended = recommended_for("openai", chat_only)
        assert recommended, "openai recommended shortlist must not be empty"
        assert recommended[0].startswith("gpt-5"), (
            f"openai recommended[0]={recommended[0]!r} should be a "
            f"current GPT-5 frontier id (full list: {recommended[:5]}...)"
        )


# ----------------------------------------------------------------------
# Lazy default_model_for — Roadmap §3.5 P0 (W1)
# ----------------------------------------------------------------------


class TestDefaultModelLazyResolve:
    def test_descriptor_no_longer_carries_hardcoded_default(self, catalog):
        # The literal ``gpt-4o-mini`` etc. lived on the descriptor for
        # a long time and went stale every quarter. The current
        # contract: descriptors expose an empty default_model and
        # callers ask the catalog to resolve it lazily.
        for pid in ("openai", "anthropic", "gemini", "groq", "deepseek"):
            desc = catalog.get_descriptor(pid)
            assert desc is not None
            assert desc.default_model == "", (
                f"descriptor for {pid} still carries hardcoded "
                f"default_model={desc.default_model!r} — that's the "
                "exact pattern §3.5 P0 bans"
            )

    def test_default_model_for_unknown_returns_empty(self, catalog):
        assert catalog.default_model_for("not-a-provider") == ""

    def test_default_model_for_returns_first_cached_model(self, catalog):
        catalog.register_adapter(FakeAdapter(models=["pinned-default", "second"]))
        # Seed the cache.
        import asyncio as _aio
        _aio.run(catalog.list_models("openai", force=True))
        assert catalog.default_model_for("openai") == "pinned-default"

    def test_default_model_for_falls_back_to_adapter_list_models(self, catalog):
        # No cache yet — default_model_for should reach into the
        # adapter's bundled list (BaseProvider.list_models() returns
        # the class-level _models attribute) rather than returning
        # empty. FakeAdapter._models = ["fallback-model"].
        catalog.register_adapter(FakeAdapter(models=[]))
        assert catalog.default_model_for("openai") == "fallback-model"


# ----------------------------------------------------------------------
# Chat-readiness signal — Wave 1 A3 (catalog truthfulness)
# ----------------------------------------------------------------------
#
# The Wave 1 / A3 contract: the catalog must distinguish
# configured-but-not-chat-ready adapters (bedrock today) from the
# production-wired majority so the Settings / Setup UI doesn't
# advertise a stubbed provider as equivalently ready to OpenAI /
# Anthropic. Discovery + probe semantics are unchanged — only the
# ``chat_ready`` + ``stub_reason`` signal is new.


class TestChatReadinessSignal:
    def test_production_providers_default_chat_ready(self, catalog):
        # Every production-wired built-in must advertise chat_ready=True
        # with an empty stub_reason so the UI doesn't render a warning
        # chip for providers that genuinely carry chat turns today.
        for pid in ("openai", "anthropic", "gemini", "groq", "deepseek",
                    "openrouter", "together", "fireworks", "ollama", "lmstudio"):
            status = catalog.status_for(pid)
            assert status.chat_ready is True, (
                f"{pid} descriptor must default to chat_ready=True "
                f"(got {status.chat_ready!r})"
            )
            assert status.stub_reason == "", (
                f"{pid} must not carry a stub_reason "
                f"(got {status.stub_reason!r})"
            )

    def test_bedrock_flagged_as_chat_ready(self, catalog):
        # W2 Lane 09: the Bedrock adapter (BedrockProvider.chat /
        # stream_chat) is live — speaks bedrock-runtime.converse via
        # boto3 and returns the canonical ChatResponse shape. The
        # descriptor was previously pinned to chat_ready=False which
        # made the UI show a stale "preview" chip; aligning the
        # descriptor with the live adapter is fix #2 in
        # findings/13-llm-core.md.
        status = catalog.status_for("bedrock")
        assert status.chat_ready is True
        assert status.stub_reason == ""
        # Discovery must still work — the picker relies on the descriptor
        # being configured / reachable independent of chat readiness.
        desc = catalog.get_descriptor("bedrock")
        assert desc is not None
        assert desc.provider_id == "bedrock"

    def test_status_to_dict_exposes_readiness_fields(self, catalog):
        # REST consumers (``/api/llm/providers``) call ``to_dict`` to
        # serialise the status. Both new fields must ride along so the
        # v2 picker can render the chip without a second round-trip.
        payload = catalog.status_for("bedrock").to_dict()
        assert payload["chat_ready"] is True  # W2 Lane 09 alignment
        assert payload["stub_reason"] == ""
        ready_payload = catalog.status_for("openai").to_dict()
        assert ready_payload["chat_ready"] is True
        assert ready_payload["stub_reason"] == ""

    def test_adapter_override_downgrades_descriptor(self, catalog):
        # A community adapter that declares itself not chat-ready must
        # override a chat_ready=True descriptor — the adapter is what
        # actually runs ``chat()`` so its opinion wins when it opts
        # out of the default. Confirms the precedence rule documented
        # on :meth:`ProviderCatalog._resolve_chat_readiness`.
        class StubbedAdapter(FakeAdapter):
            chat_ready = False
            stub_reason = "override: chat not wired in this environment"

        catalog.register_adapter(StubbedAdapter(models=["m1"]))
        status = catalog.status_for("openai")
        assert status.chat_ready is False
        assert status.stub_reason == (
            "override: chat not wired in this environment"
        )

    def test_adapter_without_attr_keeps_descriptor_default(self, catalog):
        # Adapters predating this signal don't expose ``chat_ready``.
        # The catalog must default them to the descriptor's verdict
        # (True for every production provider) rather than blow up on
        # the missing attribute. This keeps legacy / community
        # adapters working unchanged.
        class LegacyAdapter(FakeAdapter):
            pass

        assert not hasattr(LegacyAdapter, "chat_ready") or \
            LegacyAdapter.chat_ready is True  # belt-and-suspenders
        catalog.register_adapter(LegacyAdapter(models=["m1"]))
        status = catalog.status_for("openai")
        assert status.chat_ready is True
        assert status.stub_reason == ""

    def test_adapter_cannot_upgrade_descriptor_stub(self, catalog):
        # Inverse of test_adapter_override_downgrades_descriptor: a
        # descriptor that declares the adapter is stubbed must NOT be
        # silently upgraded by an adapter that happens to expose
        # ``chat_ready=True``. The descriptor carries the
        # package-author's verdict and a downstream adapter reassigning
        # the class attr shouldn't paper over a known stub. (Only
        # adapter-level ``False`` wins.)
        #
        # Pre-W2 this test used Bedrock as the canonical "stubbed"
        # descriptor. After W2 Lane 09 aligned Bedrock with its live
        # adapter, we validate the contract using a synthetic
        # descriptor — the same precedence rule still applies and
        # any future stubbed-by-default provider will rely on it.
        # ProviderDescriptor is already imported at the top of this module.

        STUB_ID = "synthetic-stub"
        catalog._descriptors[STUB_ID] = ProviderDescriptor(
            provider_id=STUB_ID,
            display_name="Synthetic Stub",
            supports_local=False,
            requires_api_key=False,
            default_base_url="",
            default_model="",
            chat_ready=False,
            stub_reason="synthetic stub for precedence test",
        )
        status = catalog.status_for(STUB_ID)
        assert status.chat_ready is False

        class StubAdapterButReady(FakeAdapter):
            chat_ready = True

        ready_adapter = StubAdapterButReady(models=["m"])
        ready_adapter.provider_id = STUB_ID
        catalog.register_adapter(ready_adapter)
        status2 = catalog.status_for(STUB_ID)
        assert status2.chat_ready is False, (
            "adapter-level chat_ready=True must not upgrade a "
            "descriptor-level stub verdict"
        )
