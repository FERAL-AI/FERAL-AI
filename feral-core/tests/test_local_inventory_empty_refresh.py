"""Local discovery distinguishes verified empty inventories from failed reads."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from providers.catalog import BUILT_IN_DESCRIPTORS, ProviderCatalog
from providers.lmstudio_provider import LMStudioProvider
from providers.ollama_provider import OllamaProvider


@pytest.fixture(params=["lmstudio", "ollama"])
def local_inventory(request, tmp_path, monkeypatch):
    provider_id = request.param
    key, id_key = ("data", "id") if provider_id == "lmstudio" else ("models", "name")
    wire = {"body": {key: [{id_key: "installed-before"}]}, "error": None, "calls": []}

    def respond(req):
        wire["calls"].append(req)
        if wire["error"] is not None:
            raise wire["error"]
        return httpx.Response(200, content=json.dumps(wire["body"]), request=req)

    original_client = httpx.AsyncClient

    def inert_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", inert_client)
    adapter = (LMStudioProvider if provider_id == "lmstudio" else OllamaProvider)(
        base_url="http://fixture.invalid/v1",
    )
    descriptor = next(d for d in BUILT_IN_DESCRIPTORS if d.provider_id == provider_id)
    with patch.object(ProviderCatalog, "_bind_builtin_adapters"):
        catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json", descriptors=[descriptor])
    catalog.register_adapter(adapter)
    return SimpleNamespace(
        catalog=catalog, adapter=adapter, provider_id=provider_id,
        key=key, id_key=id_key, wire=wire, descriptor=descriptor,
    )


@pytest.mark.asyncio
async def test_successful_empty_refresh_clears_adapter_cache_and_disk(local_inventory):
    f = local_inventory
    assert (await f.catalog.list_models(f.provider_id, live=True, force=True)).models == ["installed-before"]
    f.wire["body"] = {f.key: []}
    empty = await f.catalog.list_models(f.provider_id, live=True, force=True)
    assert f.adapter.list_models() == []
    assert empty.models == []
    assert empty.source == "live" and empty.last_refresh > 0 and empty.warning == ""
    assert f.catalog.default_model_for(f.provider_id) == ""
    count = len(f.wire["calls"])
    assert await f.catalog.list_models(f.provider_id, live=False, force=True) is empty
    assert len(f.wire["calls"]) == count
    with patch.object(ProviderCatalog, "_bind_builtin_adapters"):
        reopened = ProviderCatalog(cache_path=f.catalog._cache_path, descriptors=[f.descriptor])
    reopened.register_adapter(f.adapter)
    assert (await reopened.list_models(f.provider_id, live=False)).models == []
    assert len(f.wire["calls"]) == count


@pytest.mark.asyncio
async def test_authoritative_empty_cache_does_not_revive_adapter_default(local_inventory):
    f = local_inventory
    f.wire["body"] = {f.key: []}
    await f.catalog.list_models(f.provider_id, live=True, force=True)
    f.adapter._models = ["stale-adapter-default"]
    assert f.catalog.default_model_for(f.provider_id) == ""
    assert (await f.catalog.list_models(f.provider_id, live=False)).models == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "protocol"])
async def test_failed_refresh_keeps_previous_inventory_with_visible_warning(local_inventory, failure):
    f = local_inventory
    prior = await f.catalog.list_models(f.provider_id, live=True, force=True)
    if failure == "transport":
        f.wire["error"] = httpx.ConnectError("inert connection failure")
    else:
        f.wire["body"] = {"error": "inert malformed inventory"}
    failed = await f.catalog.list_models(f.provider_id, live=True, force=True)
    assert failed is prior
    assert failed.models == f.adapter.list_models() == ["installed-before"]
    assert failed.last_refresh == prior.last_refresh
    assert "refresh failed" in failed.warning
    assert (await f.catalog.list_models(f.provider_id, live=False)).warning


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {}, "string", [None], [{}], [{"identifier": "x"}], [{"id": 7, "name": 7}], [{"id": "", "name": ""}]])
async def test_malformed_wire_inventory_never_becomes_empty_success(local_inventory, bad):
    f = local_inventory
    assert await f.adapter.refresh_models() == ["installed-before"]
    f.wire["body"] = {f.key: bad}
    with pytest.raises(ValueError, match="inventory"):
        await f.adapter.refresh_models()
    assert f.adapter.list_models() == ["installed-before"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, [], "not-json-object", "missing", "error"])
async def test_invalid_response_envelope_keeps_previous_inventory(local_inventory, bad):
    f = local_inventory
    assert await f.adapter.refresh_models() == ["installed-before"]
    if bad == "missing":
        f.wire["body"] = {}
    elif bad == "error":
        f.wire["body"] = {f.key: [], "error": {"message": "inert provider failure"}}
    else:
        f.wire["body"] = bad
    with pytest.raises(ValueError, match="inventory"):
        await f.adapter.refresh_models()
    assert f.adapter.list_models() == ["installed-before"]


@pytest.mark.asyncio
async def test_empty_success_after_failure_clears_warning_without_old_ids(local_inventory):
    f = local_inventory
    await f.catalog.list_models(f.provider_id, live=True, force=True)
    f.wire["error"] = httpx.ConnectError("inert connection failure")
    assert (await f.catalog.list_models(f.provider_id, live=True, force=True)).warning
    f.wire["error"] = None
    f.wire["body"] = {f.key: []}
    empty = await f.catalog.list_models(f.provider_id, live=True, force=True)
    assert empty.models == [] and empty.warning == ""
    assert f.provider_id not in f.catalog._warnings


@pytest.mark.asyncio
async def test_empty_probe_clears_stale_catalog_and_prior_warning(local_inventory):
    f = local_inventory
    await f.catalog.list_models(f.provider_id, live=True, force=True)
    f.catalog._warnings[f.provider_id] = "Previous explicit request failed"
    f.wire["body"] = {f.key: []}
    status = await f.catalog.probe(f.provider_id)
    # Retain the current readiness contract: an empty service is not usable.
    assert status.reachable is False and status.error == "provider returned no models"
    assert f.catalog._models[f.provider_id].models == []
    assert f.catalog._models[f.provider_id].warning == ""
    assert f.provider_id not in f.catalog._warnings
    assert f.catalog.default_model_for(f.provider_id) == ""


@pytest.mark.asyncio
async def test_failed_probe_preserves_visible_stale_inventory(local_inventory):
    f = local_inventory
    prior = await f.catalog.list_models(f.provider_id, live=True, force=True)
    f.wire["body"] = {f.key: None}
    status = await f.catalog.probe(f.provider_id)
    assert status.reachable is False and status.error
    assert f.catalog._models[f.provider_id] is prior
    passive = await f.catalog.list_models(f.provider_id, live=False)
    assert passive.models == ["installed-before"] and passive.warning


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "not-an-inventory", [None], [3]])
async def test_local_adapter_protocol_failure_is_not_empty_success(local_inventory, monkeypatch, bad):
    f = local_inventory
    prior = await f.catalog.list_models(f.provider_id, live=True, force=True)
    monkeypatch.setattr(f.adapter, "refresh_models", AsyncMock(return_value=bad))
    failed = await f.catalog.list_models(f.provider_id, live=True, force=True)
    assert failed is prior and failed.models == ["installed-before"]
    assert failed.warning


def test_public_model_route_clears_local_inventory_on_success_empty(local_inventory, monkeypatch):
    from api.routes import llm

    f = local_inventory
    monkeypatch.setattr(llm, "state", SimpleNamespace(provider_catalog=f.catalog))
    app = FastAPI()
    app.include_router(llm.router)
    client = TestClient(app)
    path = f"/api/llm/providers/{f.provider_id}/models"
    assert client.get(path + "?live=true&force=true").json()["models"] == ["installed-before"]
    f.wire["body"] = {f.key: []}
    empty = client.get(path + "?live=true&force=true")
    assert empty.status_code == 200
    assert empty.json()["models"] == [] and empty.json()["source"] == "live"
    count = len(f.wire["calls"])
    assert client.get(path + "?live=false").json()["models"] == []
    assert len(f.wire["calls"]) == count
