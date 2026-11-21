"""Configured chat output is forwarded at actual HTTP provider boundaries."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agents.llm_provider import LLMProvider, ProviderCooldownTracker
from agents.orchestrator import Orchestrator


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.setenv("FERAL_NATIVE_DEFER_VAULT_INIT", "1")
    monkeypatch.setenv("FERAL_OFFLINE", "1")
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    monkeypatch.setenv("FERAL_MULTI_AGENT", "false")
    # Make an accidental secondary HTTP client fail in-process, never touch
    # a daemon or external service. Every expected transport is explicit.
    real_client = httpx.AsyncClient
    def guarded_client(*args, **kwargs):
        if "transport" not in kwargs:
            def refuse(request):
                raise AssertionError("Unmocked HTTP transport in isolated output-budget fixture")
            kwargs["transport"] = httpx.MockTransport(refuse)
        return real_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", guarded_client)
    from config.loader import clear_settings_cache
    clear_settings_cache()
    yield
    clear_settings_cache()


class FixtureServer:
    def __init__(self, *, empty_first=False, capacity=16384):
        self.bodies = []
        self.empty_first = empty_first
        self.capacity = capacity

    def respond(self, request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "fixture-model", "context_length": self.capacity}]})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        self.bodies.append(body)
        answer = "" if self.empty_first and len(self.bodies) == 1 else "ACK"
        if body.get("stream"):
            return httpx.Response(200, text='data: '+json.dumps({"choices":[{"delta":{"content":answer}}]})+'\n\ndata: [DONE]\n\n', headers={"Content-Type":"text/event-stream"})
        return httpx.Response(200, json={"choices":[{"message":{"role":"assistant","content":answer},"finish_reason":"stop"}]})


def make_provider(server, config=None):
    provider = LLMProvider.__new__(LLMProvider)
    provider.available = True
    provider.provider = "ollama"
    provider.model = "fixture-model"
    provider.base_url = "http://127.0.0.1:11436/v1"
    provider.api_key = "fixture"
    provider._local_engine = None
    provider._config = config if config is not None else {"fallback_providers": []}
    provider._budget_check = AsyncMock(return_value=None)
    provider._budget_record = AsyncMock()
    provider._cooldown = ProviderCooldownTracker()
    provider._last_budget_routing = {}
    provider.client = httpx.AsyncClient(base_url=provider.base_url, transport=httpx.MockTransport(server.respond))
    return provider


async def run_real_chat(provider, streaming):
    frames = []
    async def send(sid, message):
        frames.append(message.model_dump())
    registry = SimpleNamespace(skills={}, get_tools_for_skills=lambda skills: [])
    orch = Orchestrator(skill_registry=registry, send_to_client=send, daemons={})
    orch.llm = provider
    orch._multi_agent_enabled = False
    orch._streaming_enabled = streaming
    orch._route_prompt = AsyncMock(return_value=[])
    orch._ensure_core_skills = lambda skills: skills
    orch._build_system_prompt = AsyncMock(return_value="Synthetic fixture; no external actions.")
    orch._force_tool_for_query = lambda *args: None
    handler = orch.handle_command_stream if streaming else orch.handle_command
    try:
        await handler("output-budget-fixture", "Reply ACK.")
        return frames
    finally:
        if orch._background_tasks:
            import asyncio
            await asyncio.gather(*list(orch._background_tasks), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("empty_first", [False, True])
async def test_real_orchestrator_and_existing_retry_forward_saved_limit(streaming, empty_first):
    server = FixtureServer(empty_first=empty_first)
    provider = make_provider(server, {"max_tokens": 512, "fallback_providers": []})
    try:
        frames = await run_real_chat(provider, streaming)
        assert len(server.bodies) == (2 if empty_first else 1)
        assert [b["max_tokens"] for b in server.bodies] == [512] * len(server.bodies)
        assert not any(frame["type"] == "error" for frame in frames)
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "stream", "failover"])
@pytest.mark.parametrize("configured, explicit, call_site, expected", [
    ({}, None, "chat", 1024),
    ({"max_tokens": 512}, None, "chat", 512),
    ({"max_tokens": 512}, 4096, "chat", 4096),
    ({"max_tokens": 512}, 256, "learner", 256),
    ({"max_tokens": 512}, 120, "screen_loop", 120),
    ({"max_tokens": 512}, None, "learner", 1024),
    ({"max_tokens": "broken"}, 256, "learner", 256),
])
async def test_default_explicit_background_and_budget_reservation(path, configured, explicit, call_site, expected):
    server = FixtureServer()
    provider = make_provider(server, {"fallback_providers": [], **configured})
    kwargs = {"call_site": call_site}
    if explicit is not None:
        kwargs["max_tokens"] = explicit
    try:
        if path == "stream":
            result = [event async for event in provider.chat_stream([{"role":"user","content":"ACK"}], **kwargs)]
            assert not any(event["type"] == "error" for event in result)
        else:
            method = provider.chat if path == "chat" else provider.chat_with_failover
            result = await method([{"role":"user","content":"ACK"}], **kwargs)
            assert result["choices"]
        assert len(server.bodies) == 1
        assert server.bodies[0]["max_tokens"] == expected
        assert provider._budget_check.await_args.args[-1] == expected
    finally:
        await provider.close()


INVALID = [0, -1, True, False, 512.0, "512", None, {"private": "not logged"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "stream", "failover"])
@pytest.mark.parametrize("invalid", INVALID)
async def test_manual_invalid_config_refuses_before_budget_or_http(path, invalid):
    server = FixtureServer()
    provider = make_provider(server, {"max_tokens": invalid, "fallback_providers": []})
    try:
        if path == "stream":
            events = [event async for event in provider.chat_stream([{"role":"user","content":"ACK"}])]
            assert len(events) == 1
            assert events[0]["type"] == "error"
            result = events[0]
        else:
            method = provider.chat if path == "chat" else provider.chat_with_failover
            result = await method([{"role":"user","content":"ACK"}])
            assert result["choices"] == []
        assert result["error_code"] == "llm_configuration_error"
        assert "not logged" not in str(result)
        assert server.bodies == []
        provider._budget_check.assert_not_awaited()
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_orchestrator_configuration_refusal_never_executes_task(streaming):
    server = FixtureServer()
    provider = make_provider(server, {"max_tokens": "private malformed value", "fallback_providers": []})
    try:
        frames = await run_real_chat(provider, streaming)
        errors = [frame["payload"] for frame in frames if frame["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "llm_configuration_error"
        assert not any(frame["type"] in {"tool_result", "text_response"} for frame in frames)
        assert "private malformed value" not in str(frames)
        assert not server.bodies
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_cross_provider_failover_keeps_selected_allowance(streaming):
    server = FixtureServer()
    original_respond = server.respond
    def respond(request):
        if request.url.path == "/v1/chat/completions" and request.url.host == "127.0.0.1":
            server.bodies.append(json.loads(request.content))
            return httpx.Response(401, json={"error":{"message":"synthetic unauthorized"}})
        return original_respond(request)
    server.respond = respond
    provider = make_provider(server, {"max_tokens": 512, "fallback_providers": ["openai"]})
    provider._get_provider_config = lambda name: {"model":"fixture-fallback","base_url":"https://fallback.invalid/v1","api_key":"fixture","supported":True}
    real_client = httpx.AsyncClient
    def fixture_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.respond)
        return real_client(*args, **kwargs)
    try:
        with patch("agents.llm_provider.httpx.AsyncClient", fixture_client):
            if streaming:
                result = [event async for event in provider.chat_stream([{"role":"user","content":"ACK"}])]
                assert any(event.get("content") == "ACK" for event in result)
            else:
                result = await provider.chat_with_failover([{"role":"user","content":"ACK"}])
                assert result["choices"]
        assert len(server.bodies) >= 2
        assert server.bodies[-1]["model"] == "fixture-fallback"
        assert {body["max_tokens"] for body in server.bodies} == {512}
    finally:
        await provider.close()


def config_loader(tmp_path):
    from config.loader import ConfigLoader
    loader = ConfigLoader(project_dir=str(tmp_path / "project"))
    loader.discover(load_credentials=False)
    return loader


@pytest.mark.parametrize("invalid", [value for value in INVALID if value is not None])
def test_config_write_rejects_before_any_mutation(tmp_path, invalid):
    from config.loader import ChatOutputBudgetError
    import copy
    loader = config_loader(tmp_path)
    before = copy.deepcopy(loader._merged)
    target = loader.user_home / "settings.json"
    assert not target.exists()
    with pytest.raises(ChatOutputBudgetError):
        loader.update_settings("llm", "max_tokens", invalid)
    assert loader._merged == before
    assert not target.exists()


def test_merge_patch_null_deletes_allowance_and_reverts_default(tmp_path):
    from config.loader import validate_chat_output_settings_patch
    loader = config_loader(tmp_path)
    loader.update_settings("llm", "max_tokens", 512)
    assert loader._merged["llm"]["max_tokens"] == 512
    loader.update_settings("llm", "max_tokens", None)
    assert loader._merged["llm"]["max_tokens"] == 1024
    assert "max_tokens" not in json.loads((loader.user_home / "settings.json").read_text())["llm"]
    loader.discover(load_credentials=False)
    assert loader._merged["llm"]["max_tokens"] == 1024
    validate_chat_output_settings_patch({"llm":{"max_tokens":None}})
    loader.save_user_settings({"llm":{"max_tokens":512, "provider":"ollama"}, "meta":{"fixture":"keep"}})
    loader.save_user_settings({"llm":{"max_tokens":None}})
    stored = json.loads((loader.user_home / "settings.json").read_text())
    assert stored["llm"] == {"provider":"ollama"}
    assert stored["meta"] == {"fixture":"keep"}


@pytest.mark.parametrize("project_limit, local_limit, expected", [
    (512, None, 512), (512, 768, 768), (None, 768, 768),
])
def test_null_delete_restores_layered_limit_without_resetting_other_live_state(tmp_path, project_limit, local_limit, expected):
    from config.loader import ConfigLoader
    loader = config_loader(tmp_path)
    directory = loader.project_dir / ".feral"
    directory.mkdir(parents=True)
    if project_limit is not None:
        (directory / "settings.json").write_text(json.dumps({"llm": {"max_tokens": project_limit}}))
    if local_limit is not None:
        (directory / "settings.local.json").write_text(json.dumps({"llm": {"max_tokens": local_limit}}))
    loader.update_settings("llm", "max_tokens", 2048)
    # Active state can include values derived from unlocked credentials which
    # settings-only resolution must not replace or re-derive.
    fallback = [{"provider": "synthetic", "model": "fixture"}]
    loader._merged["llm"]["fallback_providers"] = fallback
    loader._setup_complete = True
    with patch.object(ConfigLoader, "_load_credentials", side_effect=AssertionError("Vault must not be opened")) as credentials:
        loader.update_settings("llm", "max_tokens", None)
        assert loader._merged["llm"]["max_tokens"] == expected
        assert loader._merged["llm"]["fallback_providers"] is fallback
        assert loader._setup_complete is True
        stored = json.loads((loader.user_home / "settings.json").read_text())
        assert "max_tokens" not in stored["llm"]
        reloaded = ConfigLoader(project_dir=str(loader.project_dir))
        assert reloaded.discover(load_credentials=False)["llm"]["max_tokens"] == expected
        credentials.assert_not_called()


@pytest.mark.parametrize("invalid_layer", [True, "not-a-token-count"])
def test_null_delete_preserves_invalid_layer_refusal_instead_of_masking_with_default(tmp_path, invalid_layer):
    from config.loader import ChatOutputBudgetError, ConfigLoader, resolve_chat_output_budget
    loader = config_loader(tmp_path)
    directory = loader.project_dir / ".feral"
    directory.mkdir(parents=True)
    (directory / "settings.json").write_text(json.dumps({"llm": {"max_tokens": invalid_layer}}))
    loader.update_settings("llm", "max_tokens", 512)
    loader.update_settings("llm", "max_tokens", None)
    reloaded = ConfigLoader(project_dir=str(loader.project_dir)).discover(load_credentials=False)
    assert loader._merged["llm"]["max_tokens"] == reloaded["llm"]["max_tokens"] == invalid_layer
    for settings in (loader._merged["llm"], reloaded["llm"]):
        with pytest.raises(ChatOutputBudgetError):
            resolve_chat_output_budget(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_http_null_delete_uses_layered_limit_on_next_actual_request_and_reload(tmp_path, monkeypatch, streaming):
    from fastapi import FastAPI
    from api.routes import config as routes
    from config.loader import ConfigLoader
    loader = config_loader(tmp_path)
    directory = loader.project_dir / ".feral"
    directory.mkdir(parents=True)
    (directory / "settings.json").write_text(json.dumps({"llm": {"max_tokens": 512}}))
    (directory / "settings.local.json").write_text(json.dumps({"llm": {"max_tokens": 768}}))
    loader.update_settings("llm", "max_tokens", 2048)
    server = FixtureServer()
    provider = make_provider(server, {"max_tokens": 2048})
    provider.switch_provider = AsyncMock()
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=loader, orchestrator=SimpleNamespace(llm=provider)))
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            response = await client.post("/api/config/update", json={"section": "llm", "key": "max_tokens", "value": None})
        assert response.status_code == 200
        frames = await run_real_chat(provider, streaming)
        assert not any(frame["type"] == "error" for frame in frames)
        assert [body["max_tokens"] for body in server.bodies] == [768]
        reloaded = ConfigLoader(project_dir=str(loader.project_dir)).discover(load_credentials=False)
        assert loader._merged["llm"]["max_tokens"] == reloaded["llm"]["max_tokens"] == 768
        assert "max_tokens" not in json.loads((loader.user_home / "settings.json").read_text())["llm"]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_config_http_write_updates_actual_next_request(tmp_path, monkeypatch, streaming):
    from fastapi import FastAPI
    from api.routes import config as routes
    loader = config_loader(tmp_path)
    server = FixtureServer()
    provider = make_provider(server)
    provider.switch_provider = AsyncMock()
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=loader, orchestrator=SimpleNamespace(llm=provider)))
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            response = await client.post("/api/config/update", json={"section":"llm","key":"max_tokens","value":512})
        assert response.status_code == 200
        provider.switch_provider.assert_awaited_once()
        assert loader._merged["llm"]["max_tokens"] == 512
        assert json.loads((loader.user_home / "settings.json").read_text())["llm"]["max_tokens"] == 512
        frames = await run_real_chat(provider, streaming)
        assert not any(frame["type"] == "error" for frame in frames)
        assert [body["max_tokens"] for body in server.bodies] == [512]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [value for value in INVALID if value is not None])
async def test_config_http_invalid_write_is_400_and_leaves_profile_unchanged(tmp_path, monkeypatch, invalid):
    from fastapi import FastAPI
    from api.routes import config as routes
    import copy
    loader = config_loader(tmp_path)
    loader.update_settings("llm", "max_tokens", 512)
    before = copy.deepcopy(loader._merged)
    target = loader.user_home / "settings.json"
    original = target.read_bytes()
    llm = SimpleNamespace(switch_provider=AsyncMock())
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=loader, orchestrator=SimpleNamespace(llm=llm)))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/config/update", json={"section":"llm","key":"max_tokens","value":invalid})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "llm_configuration_error"
    assert "not logged" not in response.text
    assert loader._merged == before
    assert target.read_bytes() == original
    llm.switch_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_rejects_invalid_settings_before_credentials_identity_or_completion(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from api.routes import config as routes
    from unittest.mock import Mock
    loader = config_loader(tmp_path)
    credentials = Mock()
    loader.save_credentials = credentials
    loader.mark_setup_complete = Mock()
    monkeypatch.setattr(routes, "state", SimpleNamespace(config=loader))
    identity = Mock()
    monkeypatch.setattr(routes, "_write_identity_files", identity)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/setup/complete", json={"settings":{"llm":{"max_tokens":True}},"credentials":{"OPENAI_API_KEY":"synthetic secret"},"identity":{"name":"synthetic"}})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "llm_configuration_error"
    credentials.assert_not_called()
    loader.mark_setup_complete.assert_not_called()
    identity.assert_not_called()
    assert not (loader.user_home / "settings.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_thinking_headroom_remains_authoritative(streaming, monkeypatch):
    monkeypatch.setenv("FERAL_REASONING_EFFORT", "high")
    server = FixtureServer()
    def respond(request):
        assert request.url.path == "/v1/messages"
        body = json.loads(request.content)
        server.bodies.append(body)
        if body.get("stream"):
            return httpx.Response(200, text='event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ACK"}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n', headers={"Content-Type":"text/event-stream"})
        return httpx.Response(200, json={"content":[{"type":"text","text":"ACK"}],"usage":{"input_tokens":1,"output_tokens":1}})
    server.respond = respond
    provider = make_provider(server, {"max_tokens":512,"fallback_providers":[]})
    provider.provider = "anthropic"
    provider.model = "claude-sonnet-4-6"
    real_client = httpx.AsyncClient
    def fixture_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.respond)
        return real_client(*args, **kwargs)
    try:
        with patch("agents.llm_provider.httpx.AsyncClient", fixture_client):
            if streaming:
                events = [event async for event in provider.chat_stream([{"role":"user","content":"ACK"}])]
                assert not any(event["type"] == "error" for event in events)
            else:
                result = await provider.chat([{"role":"user","content":"ACK"}])
                assert result["choices"]
        assert len(server.bodies) == 1
        body = server.bodies[0]
        assert body["thinking"]["type"] == "enabled"
        assert body["max_tokens"] == body["thinking"]["budget_tokens"] + 1024
        assert body["max_tokens"] > 512
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "stream", "failover"])
@pytest.mark.parametrize("allowance, fits", [(512, True), (900, False)])
async def test_actual_local_capacity_preflight_uses_selected_output_allowance(path, allowance, fits):
    server = FixtureServer(capacity=1024)
    provider = make_provider(server, {"max_tokens":allowance,"fallback_providers":[]})
    try:
        messages = [{"role":"user","content":"Reply ACK."}]
        if path == "stream":
            result = [event async for event in provider.chat_stream(messages)]
            if fits:
                assert any(event.get("content") == "ACK" for event in result)
            else:
                assert result[-1]["error_code"] == "local_context_overflow"
        else:
            method = provider.chat if path == "chat" else provider.chat_with_failover
            result = await method(messages)
            if fits:
                assert result["choices"]
            else:
                assert result["error_code"] == "local_context_overflow"
        assert provider._budget_check.await_args.args[-1] == allowance
        if fits:
            assert [body["max_tokens"] for body in server.bodies] == [allowance]
        else:
            assert server.bodies == [], "No inference may be dispatched with unreserved output room."
    finally:
        await provider.close()
