"""Selected local probe endpoint matches saved/live configuration; no real HTTP."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from agents.llm_provider import LLMProvider
from api.routes import llm as routes
from config.loader import ConfigLoader
from providers.catalog import ProviderCatalog
from providers.ollama_provider import OllamaProvider


@pytest.fixture
def observed(monkeypatch, tmp_path):
    calls = []
    behavior = {"models": [{"name": "fixture-model"}], "error": None}
    original = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        if behavior["error"] is not None:
            raise httpx.ConnectError("synthetic private diagnostic", request=request)
        if request.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": behavior["models"]})
        if request.url.path.endswith("/api/ps"):
            return httpx.Response(200, json={"models": [{"name": "fixture-model", "context_length": 16384}]})
        if request.url.path.endswith("/api/chat"):
            return httpx.Response(200, json={"message": {"content": "42"}, "model": "fixture-model"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        raise AssertionError("Unexpected HTTP: " + str(request.url))

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(httpx, "AsyncClient", client)
        scoped.setenv("HOME", str(tmp_path / "user-home"))
        scoped.setenv("FERAL_LLM_PROVIDER", "ollama")
        scoped.setenv("FERAL_LLM_MODEL", "fixture-model")
        scoped.setenv("FERAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
        scoped.setenv("FERAL_OLLAMA_BASE_URL", "http://127.0.0.1:11435")
        yield calls, behavior, original



@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["http://127.0.0.1:11436/v1", "http://127.0.0.1:11436/v1/", "http://127.0.0.1:11436"])
async def test_native_ollama_probe_observes_normalized_endpoint(observed, base):
    calls, _, _ = observed
    adapter = OllamaProvider(base_url=base)
    assert await adapter.refresh_models() == ["fixture-model"]
    assert [str(request.url) for request in calls] == ["http://127.0.0.1:11436/api/tags"]
    assert all(request.method == "GET" for request in calls)


@pytest.mark.asyncio
async def test_keyless_activation_rebinds_actual_catalog_and_runtime(observed, monkeypatch, tmp_path):
    calls, _, client = observed
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    runtime = LLMProvider()
    runtime.set_catalog(catalog)
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=config, provider_catalog=catalog,
                                                        orchestrator=SimpleNamespace(llm=runtime)))
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post("/api/llm/config", json={"provider": "ollama", "model": "fixture-model",
                                                             "base_url": "http://127.0.0.1:11436/v1", "fallback_providers": []})
            assert response.status_code == 200 and response.json()["success"] is True
            assert response.json()["reconfigured"]["ok"] is True
            assert runtime.base_url == "http://127.0.0.1:11436/v1"
            saved = await api.get("/api/llm/config")
            assert saved.json()["base_url"] == runtime.base_url
            calls.clear()
            probe = await api.post("/api/llm/providers/ollama/probe")
            assert probe.status_code == 200
            assert [str(request.url) for request in calls] == ["http://127.0.0.1:11436/api/tags"]
            assert probe.json()["reachable"] is True and probe.json()["error"] == ""
            assert all(request.method == "GET" for request in calls)
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("base,expected", [
    ("http://127.0.0.1:11436/proxy/v1/", "http://127.0.0.1:11436/proxy/api/tags"),
    ("http://127.0.0.1:11436/v1/proxy", "http://127.0.0.1:11436/v1/proxy/api/tags"),
])
async def test_native_ollama_retains_explicit_proxy_prefix(observed, base, expected):
    calls, _, _ = observed
    await OllamaProvider(base_url=base).refresh_models()
    assert [str(request.url) for request in calls] == [expected]


def test_local_binding_is_idempotent_preserves_cache_and_inactive_overrides(observed, tmp_path):
    from providers.catalog import CachedModelList
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    catalog.configure("ollama", base_url="http://127.0.0.1:11499/v1")
    inactive = catalog.get_adapter("ollama")
    clouds = {pid: catalog.get_adapter(pid) for pid in ("openai", "anthropic")}
    assert catalog.bind_active_local("lmstudio", "http://127.0.0.1:11437/v1")
    assert catalog.get_adapter("ollama") is inactive
    assert {pid: catalog.get_adapter(pid) for pid in clouds} == clouds
    assert not catalog.bind_active_local("openai", "https://synthetic.example/v1")
    assert catalog.get_adapter("ollama") is inactive
    assert catalog.bind_active_local("ollama", "http://127.0.0.1:11436/v1")
    current = catalog.get_adapter("ollama")
    cached = CachedModelList(models=["cached-fixture"], last_refresh=1, source="live")
    catalog._models["ollama"] = cached
    catalog._warnings["ollama"] = ["fixture warning"]
    assert not catalog.bind_active_local("local-ollama", "http://127.0.0.1:11436/")
    assert catalog.get_adapter("ollama") is current and catalog._models["ollama"] is cached
    assert catalog._warnings["ollama"] == ["fixture warning"]
    assert catalog.bind_active_local("ollama", "")
    assert catalog.get_adapter("ollama")._base_url == "http://127.0.0.1:11435"
    assert "ollama" not in catalog._models and "ollama" not in catalog._warnings
    catalog._models["ollama"] = cached
    catalog._warnings["ollama"] = ["fixture warning"]
    catalog.invalidate_models("ollama")
    assert "ollama" not in catalog._models and "ollama" not in catalog._warnings


@pytest.mark.asyncio
async def test_unchanged_and_empty_keyless_config_follow_actual_runtime_defaults(observed, monkeypatch, tmp_path):
    calls, _, client = observed
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    runtime = LLMProvider()
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=config, provider_catalog=catalog, orchestrator=SimpleNamespace(llm=runtime)))
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            for body, expected in [
                ({"base_url": "http://127.0.0.1:11436/v1"}, "http://127.0.0.1:11436"),
                ({}, "http://127.0.0.1:11436"),
                ({"base_url": ""}, "http://127.0.0.1:11435"),
            ]:
                payload = {"provider": "ollama", "model": "fixture-model", "fallback_providers": [], **body}
                result = await api.post("/api/llm/config", json=payload)
                assert result.status_code == 200 and result.json()["reconfigured"]["ok"] is True
                assert runtime.base_url == expected + "/v1"
                calls.clear()
                status = await api.post("/api/llm/providers/ollama/probe")
                assert status.json()["reachable"] is True
                assert [str(request.url) for request in calls] == [expected + "/api/tags"]
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("negative", ["empty", "unreachable"])
async def test_saved_probe_reports_real_negative_outcome(observed, tmp_path, negative):
    calls, behavior, _ = observed
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    catalog.bind_active_local("ollama", "http://127.0.0.1:11436/v1")
    behavior.update(models=[], error="unreachable" if negative == "unreachable" else None)
    status = await catalog.probe("ollama")
    assert status.reachable is False and status.error
    assert "ollama" not in catalog._models
    assert [str(request.url) for request in calls] == ["http://127.0.0.1:11436/api/tags"]


@pytest.mark.asyncio
@pytest.mark.parametrize("old_result", ["models", "error"])
async def test_replaced_inflight_probe_refuses_without_cache_or_auto_retry(observed, monkeypatch, tmp_path, old_result):
    calls, _, client = observed
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    catalog.bind_active_local("ollama", "http://127.0.0.1:11436/v1")
    old = catalog.get_adapter("ollama")
    entered, release = asyncio.Event(), asyncio.Event()
    async def held():
        entered.set()
        await release.wait()
        if old_result == "error":
            raise RuntimeError("private previous endpoint")
        return ["previous-endpoint-model"]
    monkeypatch.setattr(old, "refresh_models", held)
    monkeypatch.setattr(routes, "state", SimpleNamespace(provider_catalog=catalog))
    app = FastAPI()
    app.include_router(routes.router)
    async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        pending = asyncio.create_task(api.post("/api/llm/providers/ollama/probe"))
        await asyncio.wait_for(entered.wait(), 1)
        assert catalog.bind_active_local("ollama", "http://127.0.0.1:11437/v1")
        release.set()
        response = await asyncio.wait_for(pending, 1)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "provider_configuration_changed"
        assert "private" not in response.text and "reachable" not in response.json()
        assert "ollama" not in catalog._models and calls == []


def test_local_constructor_failure_is_not_silently_reported_bound(observed, monkeypatch, tmp_path):
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    old = catalog.get_adapter("ollama")
    monkeypatch.setattr(catalog, "_build_adapter", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="could not bind"):
        catalog.bind_active_local("ollama", "http://127.0.0.1:11436/v1")
    assert catalog.get_adapter("ollama") is old


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", ["http://127.0.0.1:11436/v1", ""])
async def test_actual_brain_init_binds_resolved_runtime_after_saved_restart(observed, monkeypatch, tmp_path, saved):
    from api import state as state_module
    from api.boot_report import BootReport
    from agents import persona_loader
    from memory import consciousness
    from security import vault_keys

    class StopAfterLocalBinding(BaseException):
        pass
    captured, tasks = [], []
    class CapturedLLM(LLMProvider):
        def __init__(self):
            super().__init__()
            captured.append(self)
    def stop(*args, **kwargs):
        raise StopAfterLocalBinding()
    monkeypatch.setattr(vault_keys, "get_active_key", stop)
    monkeypatch.setattr(persona_loader, "load_personas", lambda *args: [])
    monkeypatch.setattr(persona_loader, "load_workflow_packs", lambda *args: [])
    monkeypatch.setattr(consciousness, "ConsciousnessStore", lambda *args: SimpleNamespace(set_on_change=lambda callback: None))
    monkeypatch.setattr(consciousness, "default_snapshot_path", lambda: tmp_path / "absent-snapshot")
    monkeypatch.setattr(state_module, "NodeSubdeviceStore", lambda *args, **kwargs: SimpleNamespace(sweep_stale=lambda: None))
    monkeypatch.setattr(state_module, "_default_catalog_cache", lambda: tmp_path / "boot-catalog.json")
    with monkeypatch.context() as environment:
        environment.setattr("agents.llm_provider.LLMProvider", CapturedLLM)
        profile = tmp_path / "saved-profile"
        profile.mkdir()
        (profile / "settings.json").write_text(json.dumps({"llm": {
            "provider": "ollama", "model": "fixture-model", "base_url": saved,
            "fallback_providers": [],
        }}))
        environment.setenv("FERAL_HOME", str(profile))
        for name in ("FERAL_LLM_PROVIDER", "FERAL_LLM_MODEL", "FERAL_LLM_BASE_URL"):
            environment.delenv(name, raising=False)
        config = ConfigLoader(project_dir=str(tmp_path))
        config.discover(load_credentials=False)
        assert config.get("llm", "base_url") == saved
        # Match the real BrainState constructor's settings export without
        # retaining environment changes beyond this disposable fixture.
        for name, value in config.export_as_env().items():
            environment.setenv(name, value)
        # This is the real init method; heavyweight memory/hardware before the
        # catalogue is substituted, and a sentinel stops after local LLM binding.
        brain = SimpleNamespace(_native_vault_deferred=False, memory=SimpleNamespace(db_path=tmp_path / "unused.sqlite"),
                                _boot_report=BootReport(), skill_registry=MagicMock(), config=config,
                                ideas_engine=None, register_background_task=tasks.append)
        try:
            with pytest.raises(StopAfterLocalBinding):
                await state_module.BrainState.init(brain)
            assert len(captured) == 1
            expected = "http://127.0.0.1:11436" if saved else "http://127.0.0.1:11435"
            assert captured[0].base_url == expected + "/v1"
            calls = observed[0]
            calls.clear()
            status = await brain.provider_catalog.probe("ollama")
            assert status.reachable is True
            assert [str(request.url) for request in calls] == [expected + "/api/tags"]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for runtime in captured:
                await runtime.client.aclose()


@pytest.mark.asyncio
async def test_native_chat_and_capacity_use_same_normalized_proxy_root(observed):
    from providers.base import ChatMessage
    calls, _, _ = observed
    result = await OllamaProvider(base_url="http://127.0.0.1:11436/proxy/v1/").chat(
        [ChatMessage(role="user", content="Synthetic arithmetic fixture")],
        model="fixture-model", max_tokens=256,
    )
    assert result.text == "42"
    assert [str(request.url) for request in calls] == [
        "http://127.0.0.1:11436/proxy/api/ps",
        "http://127.0.0.1:11436/proxy/api/chat",
    ]
    assert [request.method for request in calls] == ["GET", "POST"]
    assert json.loads(calls[-1].content)["options"]["num_predict"] == 256


@pytest.mark.asyncio
async def test_keyless_binding_failure_does_not_claim_live_success(observed, monkeypatch, tmp_path):
    _, _, client = observed
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    old = catalog.get_adapter("ollama")
    monkeypatch.setattr(catalog, "_build_adapter", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=config, provider_catalog=catalog, orchestrator=None))
    app = FastAPI()
    app.include_router(routes.router)
    async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        response = await api.post("/api/llm/config", json={"provider": "ollama", "model": "fixture-model",
            "base_url": "http://127.0.0.1:11436/v1", "fallback_providers": []})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "local_provider_binding_unavailable"
        assert "success" not in response.json()
        assert catalog.get_adapter("ollama") is old
        saved = await api.get("/api/llm/config")
        assert saved.json()["base_url"] == "http://127.0.0.1:11436/v1"
