"""Consumer REST drives real ACP pipes; setup and local-owner boundaries fail closed."""
from types import SimpleNamespace
import json

import httpx
import pytest
from fastapi import FastAPI

from api.routes import coding
from bridges import coding_setup
pytest_plugins = ["tests.test_external_agent_skill"]


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(coding_setup, "setup_path", lambda: tmp_path / "setup.json")
    monkeypatch.setattr(coding.state, "supervisor", SimpleNamespace(paused=False))
    async def prepared(provider):
        return {**provider, "prepared": True, "context_tokens": 16384}
    monkeypatch.setattr(coding, "prepare_provider", prepared)
    app = FastAPI()
    app.include_router(coding.router)
    return app


def client(app, host="127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 1234)), base_url="http://localhost")


async def test_real_pipe_rest_task_permission_poll_cancel(app, tmp_path, fake_agent, isolated_registry, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-must-not-reach-coding")
    monkeypatch.setenv("FERAL_ENV_JAIL_ALLOW", "OPENAI_API_KEY")
    async with client(app) as c:
        assert (await c.post("/api/coding/tasks", json={"prompt": "edit"})).status_code == 424
        assert (await c.post("/api/coding/provider", json={"model": "test", "prepare": True})).status_code == 200
        assert (await c.post("/api/coding/tasks", json={"prompt": "edit", "workspace_dir": str(tmp_path)})).status_code == 403
        assert (await c.post("/api/coding/workspaces", json={"path": str(tmp_path)})).status_code == 200
        started = await c.post("/api/coding/tasks", json={"prompt": "make change", "workspace_dir": str(tmp_path)})
        assert started.status_code == 200, started.text
        turn = started.json()
        assert turn["status"] == "awaiting_permission"
        handle = turn["session_handle"]
        jail = isolated_registry.get(handle).process._jail
        assert jail is not None
        assert "OPENAI_API_KEY" not in jail.env
        permission = turn["pending_permissions"][0]
        assert "details" in permission
        assert (await c.post(f'/api/coding/permissions/{permission["request_id"]}', json={"decision": "allow_always"})).status_code == 400
        approved = await c.post(f'/api/coding/permissions/{permission["request_id"]}', json={"decision": "allow_once"})
        assert approved.status_code == 200, approved.text
        completed = (await c.get(f"/api/coding/sessions/{handle}")).json()
        assert completed["status"] == "completed"
        assert completed["tool_calls"]
        assert (await c.post(f"/api/coding/sessions/{handle}/cancel")).json()["closed"] is True
        assert (await c.get(f"/api/coding/sessions/{handle}")).status_code == 404
    await isolated_registry.close_all()


async def test_pause_denies_start_and_allow_but_allows_reject(app, tmp_path, fake_agent, isolated_registry):
    async with client(app) as c:
        await c.post("/api/coding/provider", json={"model": "test", "prepare": True})
        await c.post("/api/coding/workspaces", json={"path": str(tmp_path)})
        turn = (await c.post("/api/coding/tasks", json={"prompt": "edit", "workspace_dir": str(tmp_path)})).json()
        request_id = turn["pending_permissions"][0]["request_id"]
        coding.state.supervisor.paused = True
        assert (await c.post("/api/coding/tasks", json={"prompt": "edit", "workspace_dir": str(tmp_path)})).status_code == 423
        assert (await c.post(f"/api/coding/permissions/{request_id}", json={"decision": "allow_once"})).status_code == 423
        assert (await c.post(f"/api/coding/permissions/{request_id}", json={"decision": "reject_once"})).status_code == 200
    await isolated_registry.close_all()


async def test_remote_operator_and_paired_identity_cannot_use_coding(app):
    async with client(app, "192.168.1.20") as c:
        assert (await c.get("/api/coding")).status_code == 403
        assert (await c.post("/api/coding/provider", json={"model": "test"})).status_code == 403
    paired_app = FastAPI()
    paired_app.include_router(coding.router)
    @paired_app.middleware("http")
    async def paired(request, call_next):
        request.state.phone_device_id = "paired-phone"
        return await call_next(request)
    async with client(paired_app) as c:
        assert (await c.get("/api/coding")).status_code == 403


@pytest.mark.parametrize("body", [{"model": "a", "base_url": "https://cloud.example/v1"}, {"model": "a", "base_url": "http://user:key@localhost/v1"}, {"model": ""}, {"model": "bad name"}])
async def test_bad_provider_is_not_saved(app, body):
    async with client(app) as c:
        assert (await c.post("/api/coding/provider", json=body)).status_code == 400
    assert coding_setup.read_setup()["provider"] is None


def test_explicit_local_config_asks_and_disables_project_plugins(app):
    coding_setup.save_setup({"workspaces": [], "provider": {"model": "qwen3:4b", "base_url": "http://localhost:11434/v1"}})
    env = coding_setup.opencode_environment()
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"] == "ask"
    assert config["enabled_providers"] == ["theora-local"]
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert not any("KEY" in key for key in env)


def test_corrupt_setup_is_not_treated_as_empty_grants(app):
    coding_setup.setup_path().write_text("invalid JSON")
    with pytest.raises(ValueError):
        coding_setup.read_setup()


@pytest.mark.parametrize("value", [{"workspaces": "/private/tmp/project"}, {"workspaces": ["relative"]}, {"provider": {"model": "test", "prepared": "true"}}])
def test_malformed_setup_cannot_authorize_a_workspace(app, value):
    coding_setup.setup_path().write_text(json.dumps(value))
    with pytest.raises(ValueError):
        coding_setup.read_setup()


async def test_preparation_reuses_weights_and_configures_real_context(monkeypatch):
    calls = []
    def respond(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"], "model_info": {"qwen3.context_length": 32768}})
        return httpx.Response(200, json={"status": "success"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    prepared = await coding_setup.prepare_provider({"model": "qwen3:4b", "base_url": "http://localhost:11434/v1"})
    assert prepared["prepared"] is True
    assert prepared["source_model"] == "qwen3:4b"
    assert prepared["model"].startswith("theora-coding-")
    assert calls[1] == ("/api/create", {"from": "qwen3:4b", "model": prepared["model"], "parameters": {"num_ctx": 16384}, "stream": False})
    assert all(path != "/api/pull" for path, _ in calls)


@pytest.mark.parametrize("details", [{"capabilities": ["completion"]}, {"capabilities": ["tools"], "remote_host": "cloud"}, {"capabilities": ["tools"], "model_info": {"x.context_length": 4096}}])
async def test_unsuitable_model_does_not_create_alias(monkeypatch, details):
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=details)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    with pytest.raises(ValueError):
        await coding_setup.prepare_provider({"model": "model", "base_url": "http://localhost:11434/v1"})
    assert calls == ["/api/show"]


async def test_saved_but_unprepared_model_cannot_start(app, tmp_path):
    async with client(app) as c:
        await c.post("/api/coding/provider", json={"model": "test"})
        await c.post("/api/coding/workspaces", json={"path": str(tmp_path)})
        response = await c.post("/api/coding/tasks", json={"prompt": "edit", "workspace_dir": str(tmp_path)})
        assert response.status_code == 424
        assert "Prepare" in response.text


async def test_project_plugins_are_blocked_before_launch_even_after_grant(app, tmp_path, fake_agent, isolated_registry):
    workspace = tmp_path / "project"
    workspace.mkdir()
    async with client(app) as c:
        await c.post("/api/coding/provider", json={"model": "test", "prepare": True})
        assert (await c.post("/api/coding/workspaces", json={"path": str(workspace)})).status_code == 200
        config_dir = tmp_path / ".opencode"
        config_dir.mkdir()
        plugin = config_dir / "plugin.ts"
        marker = tmp_path / "plugin-executed"
        source = "import fs from 'node:fs'; fs.writeFileSync(" + json.dumps(str(marker)) + ", 'executed');"
        plugin.write_text(source)
        refused = await c.post("/api/coding/workspaces", json={"path": str(workspace)})
        assert refused.status_code == 400
        assert ".opencode" in refused.text
        refused = await c.post("/api/coding/tasks", json={"prompt": "edit", "workspace_dir": str(workspace)})
        assert refused.status_code == 403
        assert not isolated_registry.list(), "an unreviewed plugin project spawned an agent"
        assert plugin.read_text() == source, "preflight modified the user's project"
        assert not marker.exists(), "a rejected project executed its plugin"


def test_protocol_exposes_semantic_action_kind_for_review():
    from bridges.acp import parse_session_update
    event = parse_session_update("session", {"sessionUpdate": "tool_call", "toolCallId": "run", "kind": "execute", "status": "pending"})
    assert event.to_dict()["action_kind"] == "execute"
