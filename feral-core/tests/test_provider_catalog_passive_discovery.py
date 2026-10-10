"""Passive discovery must not turn a fresh installation into a live request."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from providers.base import BaseProvider
from providers.catalog import CachedModelList, ProviderCatalog, ProviderDescriptor


class DiscoverySentinel(BaseProvider):
    """Only the explicit live branch may cross the adapter I/O boundary."""

    def __init__(self, provider_id: str, models: list[str]) -> None:
        self.provider_id = provider_id
        self._models = list(models)
        self.refresh_calls = 0

    async def refresh_models(self):
        self.refresh_calls += 1
        return ["explicitly-discovered-model"]


@pytest.fixture(params=["ollama", "openai"])
def inventory(request, tmp_path):
    provider_id = request.param
    descriptor = ProviderDescriptor(
        provider_id=provider_id,
        display_name="Inert provider",
        supports_local=provider_id == "ollama",
        requires_api_key=provider_id != "ollama",
        default_base_url="http://fixture.invalid",
        default_model="",
    )
    # Do not construct real adapters or read credentials in this fixture.
    with patch.object(ProviderCatalog, "_bind_builtin_adapters"):
        catalog = ProviderCatalog(
            cache_path=tmp_path / "catalog.json", descriptors=[descriptor],
        )
    adapter = DiscoverySentinel(provider_id, ["adapter-known-model"])
    catalog.register_adapter(adapter)
    return catalog, adapter, provider_id


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_cold_passive_read_never_refreshes_even_with_force(inventory, force):
    catalog, adapter, provider_id = inventory
    result = await catalog.list_models(provider_id, live=False, force=force)
    assert adapter.refresh_calls == 0
    assert result.models == ["adapter-known-model"]
    assert result.source == "fallback"
    assert result.last_refresh == 0
    assert not catalog._cache_path.exists()


@pytest.mark.asyncio
async def test_empty_passive_local_inventory_does_not_claim_installation(inventory):
    catalog, adapter, provider_id = inventory
    adapter._models = []
    result = await catalog.list_models(provider_id, live=False)
    assert adapter.refresh_calls == 0
    assert result.models == []
    assert result.source == "fallback"
    assert result.last_refresh == 0


@pytest.mark.asyncio
async def test_stale_passive_cache_preserves_warning_without_refresh(inventory):
    catalog, adapter, provider_id = inventory
    cached = CachedModelList(models=["previous-model"], last_refresh=1, source="cache")
    catalog._models[provider_id] = cached
    catalog._warnings[provider_id] = "Previous explicit refresh failed"
    result = await catalog.list_models(provider_id, live=False, force=True)
    assert result is cached
    assert result.warning == "Previous explicit refresh failed"
    assert adapter.refresh_calls == 0


@pytest.mark.asyncio
async def test_explicit_live_refresh_and_subsequent_passive_read(inventory):
    catalog, adapter, provider_id = inventory
    result = await catalog.list_models(provider_id, live=True, force=True)
    assert adapter.refresh_calls == 1
    assert result.models == ["explicitly-discovered-model"]
    assert result.source == "live"
    assert result.last_refresh <= time.time()
    assert catalog._cache_path.exists()
    assert await catalog.list_models(provider_id, live=False) is result
    assert adapter.refresh_calls == 1


def test_native_and_web_cached_route_use_passive_cold_catalog(inventory, monkeypatch):
    from api.routes import llm

    catalog, adapter, provider_id = inventory
    monkeypatch.setattr(llm, "state", SimpleNamespace(provider_catalog=catalog))
    app = FastAPI()
    app.include_router(llm.router)
    client = TestClient(app)
    # This is the native onboarding request and the shared API contract
    # available to web consumers. Web's explicit live refresh remains separate.
    response = client.get(f"/api/llm/providers/{provider_id}/models?live=false")
    assert response.status_code == 200
    assert response.json()["models"] == ["adapter-known-model"]
    assert response.json()["source"] == "fallback"
    assert adapter.refresh_calls == 0
    live = client.get(f"/api/llm/providers/{provider_id}/models?live=true&force=true")
    assert live.status_code == 200
    assert live.json()["models"] == ["explicitly-discovered-model"]
    assert adapter.refresh_calls == 1


def test_cli_default_list_is_passive_and_live_flag_is_explicit(inventory, monkeypatch, capsys):
    from cli import model_commands

    catalog, adapter, provider_id = inventory
    monkeypatch.setattr(model_commands, "_build_catalog", lambda: catalog)
    assert model_commands.cmd_models_list(provider=provider_id) == 0
    assert adapter.refresh_calls == 0
    assert "adapter-known-model" in capsys.readouterr().out
    assert model_commands.cmd_models_list(provider=provider_id, live=True) == 0
    assert adapter.refresh_calls == 1
    assert "explicitly-discovered-model" in capsys.readouterr().out
