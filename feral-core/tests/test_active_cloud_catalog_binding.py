"""Active cloud catalog connection matches resolved runtime; all HTTP is inert."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from agents.llm_provider import LLMProvider, is_supported_catalog_provider, runtime_provider_id
from api.routes import llm as routes
from config.loader import ConfigLoader
from providers.catalog import BUILT_IN_DESCRIPTORS, CachedModelList, ProviderCatalog


@pytest.fixture
def observed(monkeypatch, tmp_path):
    calls = []
    original = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "fixture-result"}}],
                                            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        raise AssertionError("Unexpected fixture HTTP")

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("FERAL_LLM_PROVIDER", "openai")
    monkeypatch.setenv("FERAL_LLM_MODEL", "fixture-model")
    monkeypatch.setenv("FERAL_LLM_BASE_URL", "https://fixture.invalid/active/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-legacy-key")
    monkeypatch.setattr("agents.llm_provider._resolve_api_key", lambda provider: "fixture-active-key")
    return calls, original


def test_binding_is_network_free_idempotent_and_preserves_inactive_connections(observed, tmp_path):
    calls, _ = observed
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    catalog.configure("anthropic", base_url="https://fixture.invalid/inactive/v1", api_key="fixture-inactive")
    inactive = catalog.get_adapter("anthropic")
    assert catalog.bind_active_cloud("openai", "https://fixture.invalid/active/v1/", "fixture-active-key")
    active = catalog.get_adapter("openai")
    assert active._base_url == "https://fixture.invalid/active/v1"
    assert active._api_key == "fixture-active-key"
    cached = CachedModelList(models=["fixture-cached"], last_refresh=1, source="cache")
    catalog._models["openai"] = cached
    catalog._warnings["openai"] = "fixture warning"
    assert not catalog.bind_active_cloud("openai", active._base_url, active._api_key)
    assert catalog.get_adapter("openai") is active and catalog._models["openai"] is cached
    assert catalog._warnings["openai"] == "fixture warning"
    assert catalog.get_adapter("anthropic") is inactive
    assert calls == []


def test_runtime_binding_refuses_absent_runtime_even_when_owner_identities_match(observed, tmp_path):
    from providers.catalog import bind_active_cloud_runtime
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    orchestrator = SimpleNamespace(llm=None)
    owner = SimpleNamespace(orchestrator=orchestrator, provider_catalog=catalog)
    previous = dict(catalog._adapters)
    with pytest.raises(RuntimeError, match="^Active connection owner changed before catalog binding$"):
        bind_active_cloud_runtime(catalog, None, owner, orchestrator, owner)
    assert catalog._adapters == previous and observed[0] == []


@pytest.mark.parametrize("provider", ["ollama", "lmstudio", "codex", "local", "hybrid", "bedrock", "together", "open a"])
def test_binding_does_not_promote_local_cli_catalog_only_or_fuzzy_runtime_identity(observed, tmp_path, provider):
    calls, _ = observed
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    previous = dict(catalog._adapters)
    assert not catalog.bind_active_cloud(provider, "https://fixture.invalid/v1", "fixture-key")
    assert catalog._adapters == previous and calls == []
    assert not catalog.bind_active_local("openai", "https://fixture.invalid/v1")


@pytest.mark.parametrize("catalog_id", [desc.provider_id for desc in BUILT_IN_DESCRIPTORS
    if not desc.supports_local and desc.requires_api_key and is_supported_catalog_provider(desc.provider_id)])
def test_every_supported_cloud_adapter_accepts_actual_runtime_connection(observed, tmp_path, catalog_id):
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    raw_runtime_id = runtime_provider_id(catalog_id)
    endpoint = "https://fixture.invalid/" + catalog_id + "/v1/"
    assert catalog.bind_active_cloud(raw_runtime_id, endpoint, "fixture-active-key")
    adapter = catalog.get_adapter(catalog_id)
    assert adapter._base_url == endpoint.rstrip("/") and adapter._api_key == "fixture-active-key"
    assert not catalog.bind_active_cloud(raw_runtime_id, endpoint, "fixture-active-key")
    assert observed[0] == []


def test_construction_exception_never_logs_active_endpoint_or_secret(observed, monkeypatch, tmp_path, caplog):
    from providers.openai_provider import OpenAIProvider
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    old = catalog.get_adapter("openai")

    def broken(self, *args, **kwargs):
        raise RuntimeError("private-constructor-secret https://private-connection.invalid/v1")

    monkeypatch.setattr(OpenAIProvider, "__init__", broken)
    with pytest.raises(RuntimeError, match="^Active cloud catalog connection could not be bound$"):
        catalog.bind_active_cloud("openai", "https://private-connection.invalid/v1", "private-constructor-secret")
    assert "private-constructor-secret" not in caplog.text and "private-connection.invalid" not in caplog.text
    assert catalog.get_adapter("openai") is old and observed[0] == []


def test_key_rotation_replaces_only_active_adapter_and_invalidates_its_cache(observed, tmp_path):
    calls, _ = observed
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    catalog.bind_active_cloud("openai", "https://fixture.invalid/v1", "fixture-first")
    old = catalog.get_adapter("openai")
    catalog._models["openai"] = CachedModelList(models=["old"], last_refresh=1, source="cache")
    assert catalog.bind_active_cloud("openai", "https://fixture.invalid/v1", "fixture-second")
    assert catalog.get_adapter("openai") is not old
    assert catalog.get_adapter("openai")._api_key == "fixture-second"
    assert "openai" not in catalog._models and calls == []


def test_catalog_runtime_alias_uses_exact_resolved_connection(observed, tmp_path):
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    assert catalog.bind_active_cloud("kimi", "https://fixture.invalid/moonshot/v1", "fixture-moonshot")
    assert catalog.get_adapter("moonshot")._base_url == "https://fixture.invalid/moonshot/v1"
    assert catalog.get_adapter("moonshot")._api_key == "fixture-moonshot"
    assert observed[0] == []


def test_failed_construction_preserves_old_adapter_cache_and_redacts_connection(observed, monkeypatch, tmp_path):
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    old = catalog.get_adapter("openai")
    cached = CachedModelList(models=["old"], last_refresh=1, source="cache")
    catalog._models["openai"] = cached
    monkeypatch.setattr(catalog, "_build_adapter", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="^Active cloud catalog connection could not be bound$"):
        catalog.bind_active_cloud("openai", "https://private.invalid/v1", "private-fixture-secret")
    assert catalog.get_adapter("openai") is old and catalog._models["openai"] is cached
    assert observed[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("request_base,expected", [
    (None, "https://fixture.invalid/active/v1"),
    ("https://fixture.invalid/new/v1/", "https://fixture.invalid/new/v1"),
    ("", "https://api.openai.com/v1"),
])
async def test_keyless_reviewed_config_binds_actual_runtime_and_explicit_probe(observed, monkeypatch, tmp_path, request_base, expected):
    calls, client = observed
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    config.update_settings("llm", "provider", "openai")
    config.update_settings("llm", "base_url", "https://fixture.invalid/active/v1")
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    catalog.configure("anthropic", base_url="https://fixture.invalid/inactive/v1", api_key="fixture-inactive")
    inactive = catalog.get_adapter("anthropic")
    runtime = LLMProvider()
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=config, provider_catalog=catalog,
                                                        orchestrator=SimpleNamespace(llm=runtime)))
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            body = {"provider": "openai", "model": "fixture-model", "fallback_providers": []}
            if request_base is not None:
                body["base_url"] = request_base
            result = await api.post("/api/llm/config", json=body)
            assert result.status_code == 200 and result.json()["reconfigured"]["ok"] is True
            assert runtime.base_url.rstrip("/") == expected
            assert catalog.get_adapter("openai")._base_url == expected
            assert catalog.get_adapter("openai")._api_key == runtime.api_key == "fixture-active-key"
            assert catalog.get_adapter("anthropic") is inactive
            calls.clear()
            assert (await api.get("/api/llm/providers")).status_code == 200
            assert (await api.get("/api/llm/providers/openai/models?live=false")).status_code == 200
            assert calls == []
            probe = await api.post("/api/llm/providers/openai/probe")
            assert probe.json()["reachable"] is True
            assert [str(r.url) for r in calls] == [expected + "/models"]
            assert calls[0].headers["Authorization"] == "Bearer fixture-active-key"
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,raised", [(path, raised)
    for path in ("switch", "config-update", "select-active", "add-active", "config-save")
    for raised in (False, True) if raised or path not in ("switch", "config-update")])
async def test_failed_activation_never_publishes_draft_connection_or_private_failure(observed, monkeypatch, tmp_path, caplog, path, raised):
    from security import vault_keys
    calls, client = observed
    owner, runtime, app, vault = live_routes(observed, monkeypatch, tmp_path)
    vault_keys.add_provider_key("openai", "next", "fixture-next-key", vault=vault)
    adapter = owner.provider_catalog.get_adapter("openai")
    method = "switch_provider" if path in ("switch", "config-update") else "reconfigure"

    async def fail(*args, **kwargs):
        if raised:
            raise RuntimeError("private-secret https://private.invalid")
        return {"ok": False, "reason": "private-secret https://private.invalid"}

    monkeypatch.setattr(runtime, method, fail)
    try:
        async with client(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as api:
            if path == "switch":
                result = await api.post("/api/llm/switch", json={"provider": "openai", "model": "fixture-model",
                    "base_url": "https://fixture.invalid/new/v1", "api_key": "fixture-next-key"})
            elif path == "config-update":
                result = await api.post("/api/config/update", json={"section": "llm", "key": "base_url",
                    "value": "https://fixture.invalid/new/v1"})
            elif path == "config-save":
                result = await api.post("/api/llm/config", json={"provider": "openai", "model": "fixture-model",
                    "base_url": "https://fixture.invalid/new/v1", "api_key": "fixture-next-key"})
            else:
                url = "/api/llm/providers/openai/keys" + ("/active" if path == "select-active" else "")
                result = await api.post(url, json={"label": "next", "api_key": "fixture-next-key", "set_active": True})
            if path == "config-update":
                assert result.status_code == 500
                from api.routes import config as config_routes
                with pytest.raises(RuntimeError, match="^Runtime activation was not verified; settings may already be saved$"):
                    await config_routes.update_config({"section": "llm", "key": "base_url",
                        "value": "https://fixture.invalid/new/v1"})
            elif path == "switch":
                assert result.status_code == 503
            else:
                assert result.status_code == 200 and result.json()["reconfigured"]["ok"] is False
            assert owner.provider_catalog.get_adapter("openai") is adapter
            assert adapter._api_key == runtime.api_key and adapter._base_url == runtime.base_url
            assert "private-secret" not in result.text + caplog.text
            assert "private.invalid" not in result.text + caplog.text
            assert calls == []
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
async def test_keyless_activation_tracks_changed_resolved_credential(observed, monkeypatch, tmp_path):
    calls, client = observed
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    runtime = LLMProvider()
    active = {"key": "fixture-first"}
    monkeypatch.setattr("agents.llm_provider._resolve_api_key", lambda provider: active["key"])
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=config, provider_catalog=catalog,
                                                        orchestrator=SimpleNamespace(llm=runtime)))
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            for key in ("fixture-first", "fixture-second"):
                active["key"] = key
                result = await api.post("/api/llm/config", json={"provider": "openai", "model": "fixture-model",
                    "base_url": "https://fixture.invalid/active/v1", "fallback_providers": []})
                assert result.status_code == 200
                assert catalog.get_adapter("openai")._api_key == runtime.api_key == key
                calls.clear()
                probe = await api.post("/api/llm/providers/openai/probe")
                assert probe.json()["reachable"] is True
                assert [r.headers["Authorization"] for r in calls] == ["Bearer " + key]
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["runtime", "catalog"])
async def test_owner_replacement_during_activation_cannot_bind_retired_connection(observed, monkeypatch, tmp_path, replacement):
    _, client = observed
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    previous = catalog.get_adapter("openai")
    runtime = LLMProvider()
    orchestrator = SimpleNamespace(llm=runtime)
    shared = SimpleNamespace(config=config, provider_catalog=catalog, orchestrator=orchestrator)
    original = runtime.reconfigure

    async def replaced(**kwargs):
        result = await original(**kwargs)
        if replacement == "runtime":
            shared.orchestrator = SimpleNamespace(llm=object())
        else:
            shared.provider_catalog = ProviderCatalog(cache_path=tmp_path / "replacement.json")
        return result

    monkeypatch.setattr(runtime, "reconfigure", replaced)
    monkeypatch.setattr(routes, "state", shared)
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            result = await api.post("/api/llm/config", json={"provider": "openai", "model": "fixture-model",
                "base_url": "https://fixture.invalid/new/v1", "fallback_providers": []})
            assert result.status_code == 503
            assert result.json()["detail"]["code"] == "active_cloud_catalog_binding_unavailable"
            assert catalog.get_adapter("openai") is previous
            assert "fixture-active-key" not in result.text and "fixture.invalid" not in result.text
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
async def test_config_binding_failure_reports_saved_state_without_private_success(observed, monkeypatch, tmp_path):
    _, client = observed
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    runtime = LLMProvider()
    monkeypatch.setattr(catalog, "_build_adapter", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=config, provider_catalog=catalog,
                                                        orchestrator=SimpleNamespace(llm=runtime)))
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post("/api/llm/config", json={"provider": "openai", "model": "fixture-model",
                "base_url": "https://fixture.invalid/new/v1", "fallback_providers": []})
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "active_cloud_catalog_binding_unavailable"
            assert "success" not in response.json()
            assert "fixture.invalid" not in response.text and "fixture-active-key" not in response.text
            saved = await api.get("/api/llm/config")
            assert saved.json()["base_url"] == runtime.base_url == "https://fixture.invalid/new/v1"
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["models", "error"])
async def test_replaced_active_connection_refuses_late_probe_without_retry_or_cache(observed, monkeypatch, tmp_path, result):
    _, client = observed
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    catalog.bind_active_cloud("openai", "https://fixture.invalid/old/v1", "fixture-first")
    old = catalog.get_adapter("openai")
    entered, release = asyncio.Event(), asyncio.Event()

    async def held():
        entered.set()
        await release.wait()
        if result == "error":
            raise RuntimeError("private fixture detail")
        return ["stale-model"]

    monkeypatch.setattr(old, "refresh_models", held)
    monkeypatch.setattr(routes, "state", SimpleNamespace(provider_catalog=catalog))
    app = FastAPI()
    app.include_router(routes.router)
    async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        pending = asyncio.create_task(api.post("/api/llm/providers/openai/probe"))
        await asyncio.wait_for(entered.wait(), 1)
        catalog.bind_active_cloud("openai", "https://fixture.invalid/new/v1", "fixture-second")
        release.set()
        response = await asyncio.wait_for(pending, 1)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "provider_configuration_changed"
        assert "private" not in response.text and "reachable" not in response.json()
        assert "openai" not in catalog._models and observed[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("construction_failure", [False, True])
async def test_actual_startup_binds_after_labeled_key_hydration_without_http(observed, monkeypatch, tmp_path, caplog, construction_failure):
    from api import state as state_module
    from api.boot_report import BootReport
    from agents import persona_loader
    from memory import consciousness
    from security import vault_keys

    class StopAfterBinding(BaseException):
        pass

    captured, tasks, connection = [], [], []

    original_init = LLMProvider.__init__

    def capture_init(self):
        original_init(self)
        captured.append(self)

    class CapturedCatalog(ProviderCatalog):
        def bind_active_cloud(self, provider_id, base_url, api_key):
            connection.append((provider_id, base_url, api_key))
            if construction_failure:
                self._build_adapter = lambda *args, **kwargs: None
            super().bind_active_cloud(provider_id, base_url, api_key)
            raise StopAfterBinding()

    monkeypatch.setattr(LLMProvider, "__init__", capture_init)
    monkeypatch.setattr("agents.llm_provider._resolve_api_key", lambda provider: "fixture-legacy-key")
    monkeypatch.setattr(state_module, "ProviderCatalog", CapturedCatalog)
    monkeypatch.setattr(vault_keys, "get_active_key", lambda provider: "fixture-hydrated-key")
    monkeypatch.setattr(persona_loader, "load_personas", lambda *args: [])
    monkeypatch.setattr(persona_loader, "load_workflow_packs", lambda *args: [])
    monkeypatch.setattr(consciousness, "ConsciousnessStore", lambda *args: SimpleNamespace(set_on_change=lambda callback: None))
    monkeypatch.setattr(consciousness, "default_snapshot_path", lambda: tmp_path / "absent-snapshot")
    monkeypatch.setattr(state_module, "NodeSubdeviceStore", lambda *args, **kwargs: SimpleNamespace(sweep_stale=lambda: None))
    monkeypatch.setattr(state_module, "_default_catalog_cache", lambda: tmp_path / "boot-catalog.json")
    profile = tmp_path / "restart-profile"
    profile.mkdir()
    (profile / "settings.json").write_text(json.dumps({"llm": {"provider": "openai", "model": "fixture-model",
        "base_url": "https://fixture.invalid/restart/v1", "fallback_providers": []}}))
    monkeypatch.setenv("FERAL_HOME", str(profile))
    for name in ("FERAL_LLM_PROVIDER", "FERAL_LLM_MODEL", "FERAL_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    for name, value in config.export_as_env().items():
        monkeypatch.setenv(name, value)
    brain = SimpleNamespace(_native_vault_deferred=False, memory=SimpleNamespace(db_path=tmp_path / "unused.sqlite"),
        _boot_report=BootReport(), skill_registry=MagicMock(), config=config,
        ideas_engine=None, register_background_task=tasks.append)
    try:
        expected_error = RuntimeError if construction_failure else StopAfterBinding
        with pytest.raises(expected_error):
            await state_module.BrainState.init(brain)
        assert connection == [("openai", "https://fixture.invalid/restart/v1", "fixture-hydrated-key")]
        adapter = brain.provider_catalog.get_adapter("openai")
        if construction_failure:
            assert adapter._base_url != captured[0].base_url
            report = brain._boot_report.to_dict()
            assert report["summary"]["failed"] >= 1
            assert "fixture-hydrated-key" not in caplog.text
        else:
            assert adapter._base_url == captured[0].base_url == "https://fixture.invalid/restart/v1"
            assert adapter._api_key == captured[0].api_key == "fixture-hydrated-key"
        assert observed[0] == []
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runtime in captured:
            await runtime.client.aclose()


class InertVault:
    def __init__(self):
        self.values = {}

    def put(self, namespace, key, value, **kwargs):
        self.values[(namespace, key)] = value

    def get(self, namespace, key):
        return self.values.get((namespace, key))

    def store(self, key, value, **kwargs):
        self.values[("default", key)] = value

    def get_credential(self, key):
        return self.values.get(("default", key))


def live_routes(observed, monkeypatch, tmp_path, provider="openai"):
    from api.routes import config as config_routes
    from security import vault_keys
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    config.update_settings("llm", "provider", provider)
    config.update_settings("llm", "model", "fixture-model")
    config.update_settings("llm", "base_url", "https://fixture.invalid/active/v1")
    monkeypatch.setattr(config, "save_credentials", lambda values: None)
    catalog = ProviderCatalog(cache_path=tmp_path / "catalog.json")
    # Startup exports saved settings before constructing the runtime. These
    # isolated route fixtures have no running brain to publish settings for us.
    monkeypatch.setenv("FERAL_LLM_PROVIDER", provider)
    runtime = LLMProvider()
    assert runtime.provider == runtime_provider_id(provider)
    catalog.bind_active_cloud(runtime.provider, runtime.base_url, runtime.api_key)
    vault = InertVault()
    original = vault_keys.get_active_key
    monkeypatch.setattr(vault_keys, "get_active_key", lambda pid: original(pid, vault=vault))
    owner = SimpleNamespace(config=config, provider_catalog=catalog, orchestrator=SimpleNamespace(llm=runtime), vault=vault)
    monkeypatch.setattr(routes, "state", owner)
    monkeypatch.setattr(config_routes, "state", owner)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(config_routes.router)
    return owner, runtime, app, vault


async def assert_current_probe(api, calls, provider, runtime):
    calls.clear()
    await api.get("/api/llm/providers")
    await api.get("/api/llm/providers/" + provider + "/models?live=false")
    assert calls == []
    result = await api.post("/api/llm/providers/" + provider + "/probe")
    assert result.json()["reachable"] is True
    assert [str(r.url) for r in calls] == [runtime.base_url.rstrip("/") + "/models"]
    assert [r.headers["Authorization"] for r in calls] == ["Bearer " + runtime.api_key]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "moonshot"])
@pytest.mark.parametrize("operation", ["add-active", "select-active"])
async def test_labeled_key_routes_preserve_active_custom_endpoint_and_runtime_alias(observed, monkeypatch, tmp_path, provider, operation):
    from security import vault_keys
    calls, client = observed
    owner, runtime, app, vault = live_routes(observed, monkeypatch, tmp_path, provider)
    if operation == "select-active":
        vault_keys.add_provider_key(provider, "next", "fixture-next-key", vault=vault)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            path = "/api/llm/providers/" + provider + "/keys"
            body = {"label": "next", "api_key": "fixture-next-key", "set_active": True}
            if operation == "select-active":
                path += "/active"
                body = {"label": "next"}
            result = await api.post(path, json=body)
            assert result.status_code == 200 and result.json()["reconfigured"]["ok"] is True
            assert runtime.api_key == "fixture-next-key"
            assert runtime.base_url == "https://fixture.invalid/active/v1"
            adapter = owner.provider_catalog.get_adapter(provider)
            assert adapter._base_url == runtime.base_url and adapter._api_key == runtime.api_key
            await assert_current_probe(api, calls, provider, runtime)
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add-active", "select-active"])
async def test_inactive_key_only_change_preserves_scoped_endpoint_and_active_runtime(observed, monkeypatch, tmp_path, operation):
    from security import vault_keys
    calls, client = observed
    owner, runtime, app, vault = live_routes(observed, monkeypatch, tmp_path)
    owner.provider_catalog.configure("anthropic", base_url="https://fixture.invalid/inactive/v1", api_key="fixture-old")
    active = owner.provider_catalog.get_adapter("openai")
    if operation == "select-active":
        vault_keys.add_provider_key("anthropic", "next", "fixture-next-key", vault=vault)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            path = "/api/llm/providers/anthropic/keys"
            body = {"label": "next", "api_key": "fixture-next-key", "set_active": True}
            if operation == "select-active":
                path += "/active"
                body = {"label": "next"}
            calls.clear()
            result = await api.post(path, json=body)
            assert result.status_code == 200 and "reconfigured" not in result.json()
            assert owner.provider_catalog.get_adapter("anthropic")._base_url == "https://fixture.invalid/inactive/v1"
            assert owner.provider_catalog.get_adapter("anthropic")._api_key == "fixture-next-key"
            assert owner.provider_catalog.get_adapter("openai") is active
            assert runtime.provider == "openai" and calls == []
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["switch", "config-update"])
@pytest.mark.parametrize("provider", ["openai", "moonshot"])
async def test_other_activation_routes_bind_actual_connection_not_draft_defaults(observed, monkeypatch, tmp_path, path, provider):
    calls, client = observed
    owner, runtime, app, _ = live_routes(observed, monkeypatch, tmp_path, provider)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            if path == "switch":
                result = await api.post("/api/llm/switch", json={"provider": provider, "model": "fixture-model",
                    "base_url": "https://fixture.invalid/switched/v1", "api_key": "fixture-switch-key"})
            else:
                result = await api.post("/api/config/update", json={"section": "llm", "key": "base_url",
                    "value": "https://fixture.invalid/switched/v1"})
            assert result.status_code == 200
            adapter = owner.provider_catalog.get_adapter(provider)
            assert adapter._base_url == runtime.base_url == "https://fixture.invalid/switched/v1"
            assert adapter._api_key == runtime.api_key
            await assert_current_probe(api, calls, provider, runtime)
    finally:
        await runtime.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["switch", "config-update", "select-active", "add-active"])
@pytest.mark.parametrize("replacement", ["owner", "orchestrator", "runtime", "catalog"])
async def test_each_activation_refuses_replacement_after_await(observed, monkeypatch, tmp_path, path, replacement):
    from api.routes import config as config_routes
    from security import vault_keys
    _, client = observed
    owner, runtime, app, vault = live_routes(observed, monkeypatch, tmp_path)
    vault_keys.add_provider_key("openai", "next", "fixture-next-key", vault=vault)
    method = "switch_provider" if path in ("switch", "config-update") else "reconfigure"
    original = getattr(runtime, method)

    async def replaced(*args, **kwargs):
        result = await original(*args, **kwargs)
        if replacement == "owner":
            changed = SimpleNamespace(config=owner.config, provider_catalog=owner.provider_catalog,
                orchestrator=owner.orchestrator, vault=vault)
            monkeypatch.setattr(routes, "state", changed)
            monkeypatch.setattr(config_routes, "state", changed)
        elif replacement == "orchestrator":
            owner.orchestrator = SimpleNamespace(llm=runtime)
        elif replacement == "runtime":
            owner.orchestrator.llm = object()
        else:
            owner.provider_catalog = ProviderCatalog(cache_path=tmp_path / "replacement.json")
        return result

    monkeypatch.setattr(runtime, method, replaced)
    try:
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            if path == "switch":
                result = await api.post("/api/llm/switch", json={"provider": "openai", "model": "fixture-model",
                    "base_url": "https://fixture.invalid/new/v1", "api_key": "fixture-next-key"})
            elif path == "config-update":
                result = await api.post("/api/config/update", json={"section": "llm", "key": "base_url",
                    "value": "https://fixture.invalid/new/v1"})
            else:
                url = "/api/llm/providers/openai/keys" + ("/active" if path == "select-active" else "")
                result = await api.post(url, json={"label": "next", "api_key": "fixture-next-key", "set_active": True})
            assert result.status_code == 503
            assert result.json()["detail"]["code"] == "active_cloud_catalog_binding_unavailable"
            assert "fixture-next-key" not in result.text and "fixture.invalid" not in result.text
    finally:
        await runtime.client.aclose()
