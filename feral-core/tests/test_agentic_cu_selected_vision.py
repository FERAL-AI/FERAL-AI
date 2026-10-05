"""Selected vision uses shared wire/budget code and inert effect boundaries."""
import asyncio
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio

from agents.llm_provider import LLMProvider
from config.loader import ConfigLoader
from cost.budget import CostBudget
from providers.catalog import BUILT_IN_DESCRIPTORS, CachedModelList, ProviderCatalog
from providers.ollama_provider import OllamaProvider
from skills.impl.agentic_computer_use import AgenticComputerUseSkill
from tests.test_agentic_computer_use_authority import authority as _authority_fixture

authority = _authority_fixture


IMAGE = [{"role": "user", "content": [{"type": "text", "text": "inert task"},
         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aW5lcnQ="}}]}]


@pytest_asyncio.fixture
async def wired(monkeypatch, tmp_path):
    for key in ("FERAL_VLM_PROVIDER", "FERAL_VLM_MODEL", "FERAL_VLM_BASE_URL", "FERAL_VLM_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "FERAL_CONTEXT_WINDOW_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("agents.llm_provider._resolve_api_key", lambda _: "")
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider, llm.model, llm.available = "openai", "gpt-4o", True
    llm.base_url, llm.api_key = "https://primary.invalid/v1", "inert-primary-key"
    llm._local_engine, llm._cost_budget = None, None
    llm._config = {"fallback_providers": ["anthropic", "openai"]}
    llm._ollama_context_observation = None
    primary_calls, calls, clients = [], [], []

    def no_primary(request):
        primary_calls.append(request)
        raise AssertionError("selected request used primary client")

    original_client = httpx.AsyncClient
    llm.client = original_client(base_url=llm.base_url, transport=httpx.MockTransport(no_primary))
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    state = SimpleNamespace(orchestrator=SimpleNamespace(llm=llm), config=config, provider_catalog=None)
    monkeypatch.setitem(sys.modules, "api.state", SimpleNamespace(state=state))
    controls = {"response": '{"action":"done","summary":"inert result"}', "failure": False,
                "entered": None, "release": None}

    async def respond(request):
        calls.append(request)
        if controls["failure"]:
            return httpx.Response(controls.get("failure_status", 400), json={"error": "private-wire-canary"})
        if request.url.path.endswith("/api/ps"):
            return httpx.Response(200, json={"models": [{"name": "llava:7b", "context_length": controls.get("capacity", 8192)}]})
        if controls["entered"] is not None:
            controls["entered"].set()
            await controls["release"].wait()
        body = json.loads(request.content)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"content": [{"type": "text", "text": controls["response"]}],
                                           "usage": {"input_tokens": 10, "output_tokens": 10}})
        assert body["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": controls["response"]}}],
                                       "usage": {"prompt_tokens": 10, "completion_tokens": 10}})

    def inert_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        client = original_client(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", inert_client)
    # A selection must never construct, switch, detect or probe a provider.
    constructor = Mock(side_effect=AssertionError("provider constructor forbidden"))
    monkeypatch.setattr(LLMProvider, "__init__", constructor)
    switch = AsyncMock(side_effect=AssertionError("provider switch forbidden"))
    monkeypatch.setattr(llm, "switch_provider", switch)
    probe = AsyncMock(side_effect=AssertionError("provider probe forbidden"))
    monkeypatch.setattr(llm, "_probe_chat_availability", probe)
    value = SimpleNamespace(llm=llm, state=state, config=config, calls=calls, clients=clients,
                            primary_calls=primary_calls, controls=controls, skill=AgenticComputerUseSkill(),
                            constructor=constructor, switch=switch, probe=probe)
    try:
        yield value
    finally:
        assert primary_calls == []
        constructor.assert_not_called()
        switch.assert_not_awaited()
        probe.assert_not_awaited()
        await llm.client.aclose()
        for client in clients:
            await client.aclose()


def select(monkeypatch, provider="ollama", model="llava:7b", url="http://local.invalid:12345"):
    monkeypatch.setenv("FERAL_VLM_PROVIDER", provider)
    monkeypatch.setenv("FERAL_VLM_MODEL", model)
    monkeypatch.setenv("FERAL_VLM_BASE_URL", url)


@pytest.mark.parametrize("url", ["http://local.invalid:12345", "http://local.invalid:12345/v1/",
                                  "http://local.invalid:12345/proxy/v1"])
async def test_keyless_local_binding_is_passive_exact_and_owned(wired, monkeypatch, url):
    select(monkeypatch, url=url)
    wired.llm.available = False  # Unrelated cloud primary readiness is not local readiness.
    selected = await wired.skill._get_vlm({})
    assert selected.provider == "ollama" and selected.model == "llava:7b"
    assert selected.base_url.endswith("/v1") and "/v1/v1" not in selected.base_url
    assert wired.calls == wired.clients == []
    assert "inert-primary-key" not in repr(selected) and "local.invalid" not in repr(selected)
    with pytest.raises(FrozenInstanceError):
        selected.model = "different"
    before = (wired.llm.provider, wired.llm.model, wired.llm.base_url, wired.llm.api_key,
              wired.llm.available, wired.llm.client, wired.llm._config)
    response = await selected.chat(messages=IMAGE)
    assert selected.extract_response(response)[0].startswith('{"action":"done"')
    assert len(wired.calls) == 2  # Existing read-only capacity check plus one inference.
    assert wired.calls[0].url.path.endswith("/api/ps")
    request = wired.calls[1]
    assert str(request.url) == selected.base_url + "/chat/completions"
    assert json.loads(request.content)["messages"] == IMAGE
    assert request.headers["authorization"] == "Bearer ollama"
    assert all(client.is_closed for client in wired.clients)
    assert not wired.llm.client.is_closed
    assert before == (wired.llm.provider, wired.llm.model, wired.llm.base_url, wired.llm.api_key,
                      wired.llm.available, wired.llm.client, wired.llm._config)


async def test_env_wins_saved_selection_and_saved_wins_primary(wired, monkeypatch):
    for key, value in {"provider": "ollama", "model": "llava:7b", "base_url": "http://saved.invalid:1111"}.items():
        wired.config.update_settings("vision", key, value)
    before = (wired.config.user_home / "settings.json").read_bytes()
    saved = await wired.skill._get_vlm({})
    assert (saved.provider, saved.model, saved.base_url) == ("ollama", "llava:7b", "http://saved.invalid:1111/v1")
    select(monkeypatch, url="http://explicit.invalid:2222/v1")
    explicit = await wired.skill._get_vlm({})
    assert explicit.base_url == "http://explicit.invalid:2222/v1"
    assert (wired.config.user_home / "settings.json").read_bytes() == before
    assert wired.calls == []


@pytest.mark.parametrize("provider,model,key_name,header", [
    ("anthropic", "claude-sonnet-4-6", "ANTHROPIC_API_KEY", "x-api-key"),
    ("gemini", "gemini-2.0-flash", "GEMINI_API_KEY", "authorization"),
    ("openai", "gpt-4o", "OPENAI_API_KEY", "authorization"),
])
async def test_cloud_credential_is_selected_provider_specific(wired, monkeypatch, provider, model, key_name, header):
    select(monkeypatch, provider=provider, model=model, url="https://selected.invalid/v1")
    binding = await wired.skill._get_vlm({"OPENAI_API_KEY": "inert-openai-key", key_name: "inert-selected-key"})
    assert binding.api_key == "inert-selected-key" and wired.calls == []
    response = await binding.chat(messages=IMAGE)
    assert response.get("error") is None and len(wired.calls) == 1
    request = wired.calls[0]
    assert request.url.host == "selected.invalid"
    assert request.headers[header].endswith("inert-selected-key")
    payload = json.loads(request.content)
    assert payload["model"] == model
    if provider == "anthropic":
        assert payload["messages"][0]["content"][1]["type"] == "image"
        assert payload["messages"][0]["content"][1]["source"]["data"] == "aW5lcnQ="
    else:
        assert payload["messages"] == IMAGE
    assert "inert-primary-key" not in request.headers.values()


async def test_active_vault_and_dedicated_key_without_unrelated_keys(wired, monkeypatch):
    select(monkeypatch, provider="anthropic", model="claude-sonnet-4-6", url="https://selected.invalid/v1")
    resolver = Mock(side_effect=lambda provider: "inert-vault-key" if provider == "anthropic" else "")
    monkeypatch.setattr("agents.llm_provider._resolve_api_key", resolver)
    assert (await wired.skill._get_vlm({})).api_key == "inert-vault-key"
    resolver.assert_called_once_with("anthropic")
    resolver.reset_mock()
    monkeypatch.setenv("FERAL_VLM_API_KEY", "inert-dedicated-key")
    assert (await wired.skill._get_vlm({"ANTHROPIC_API_KEY": "inert-other-key"})).api_key == "inert-dedicated-key"
    resolver.assert_not_called()


async def test_no_inference_of_provider_from_an_unrelated_key(wired):
    wired.llm.api_key = ""
    result = await wired.skill._get_vlm({"ANTHROPIC_API_KEY": "inert-anthropic-key"})
    assert result["error_code"] == "vision_credentials_unavailable"
    assert wired.calls == []


@pytest.mark.parametrize("provider,model", [("ollama", "mistral:7b"), ("deepseek", "deepseek-chat"),
                                            ("lmstudio", "llava:7b"), ("codex", "gpt-5-codex"),
                                            ("unknown-provider", "unknown-model"), ("openai", "whisper-1")])
async def test_unsupported_selection_refuses_before_capture(wired, monkeypatch, provider, model):
    select(monkeypatch, provider, model)
    monkeypatch.setattr(wired.skill, "_registered_authority", lambda *args: object())
    capture = AsyncMock()
    monkeypatch.setattr(wired.skill, "_capture_screen", capture)
    result = await wired.skill.execute("execute_task", {"task": "inert task"}, {})
    assert result["success"] is False
    assert result["error_code"] in {"vision_model_unsupported", "vision_binding_unavailable"}
    capture.assert_not_awaited()
    assert wired.calls == []


@pytest.mark.parametrize("url", ["file:///private/fixture", "https://user:secret@fixture.invalid/v1",
                                  "https://fixture.invalid/v1?secret=private", "https://fixture.invalid/v1#private"])
async def test_invalid_endpoint_is_redacted_before_capture(wired, monkeypatch, url):
    select(monkeypatch, url=url)
    result = await wired.skill._get_vlm({})
    assert result["error_code"] == "vision_binding_invalid"
    assert "private" not in repr(result) and "secret" not in repr(result)
    assert wired.calls == []


async def test_authoritative_empty_or_missing_local_tag_never_revives_default(wired, monkeypatch, tmp_path):
    select(monkeypatch)
    descriptor = next(item for item in BUILT_IN_DESCRIPTORS if item.provider_id == "ollama")
    monkeypatch.setattr(ProviderCatalog, "_bind_builtin_adapters", lambda self: None)
    catalog = ProviderCatalog(cache_path=tmp_path / "models.json", descriptors=[descriptor])
    catalog.register_adapter(OllamaProvider(base_url="http://local.invalid:12345/v1"))
    wired.state.provider_catalog = catalog
    for ids in ([], ["llava:13b"]):
        catalog._models["ollama"] = CachedModelList(models=ids, last_refresh=0, source="live")
        result = await wired.skill._get_vlm({})
        assert result["error_code"] == "vision_model_not_installed"
    assert wired.calls == []


async def test_inventory_at_primary_endpoint_does_not_refuse_other_selected_endpoint(wired, monkeypatch, tmp_path):
    select(monkeypatch, url="http://other.invalid:2222/v1")
    descriptor = next(item for item in BUILT_IN_DESCRIPTORS if item.provider_id == "ollama")
    monkeypatch.setattr(ProviderCatalog, "_bind_builtin_adapters", lambda self: None)
    catalog = ProviderCatalog(cache_path=tmp_path / "models.json", descriptors=[descriptor])
    catalog.register_adapter(OllamaProvider(base_url="http://primary.invalid:1111/v1"))
    catalog._models["ollama"] = CachedModelList(models=[], last_refresh=0, source="live")
    wired.state.provider_catalog = catalog
    selected = await wired.skill._get_vlm({})
    assert selected.model == "llava:7b" and selected.base_url == "http://other.invalid:2222/v1"
    assert wired.calls == []  # Presence at the other endpoint remains unverified.


async def test_different_provider_requires_selected_model_without_speculative_default(wired, monkeypatch):
    monkeypatch.setenv("FERAL_VLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FERAL_VLM_MODEL", "")
    result = await wired.skill._get_vlm({"ANTHROPIC_API_KEY": "inert-key"})
    assert result["error_code"] == "vision_binding_unavailable" and wired.calls == []


@pytest.mark.parametrize("capacity", [8192, 32])
async def test_selected_local_capacity_verification_preserves_primary_observation(wired, monkeypatch, capacity):
    select(monkeypatch, url="http://vision.invalid:2222/v1")
    wired.llm.provider, wired.llm.model = "ollama", "llama3.2:3b"
    wired.llm.base_url = "http://primary.invalid:1111/v1"
    observed = (wired.llm.base_url, wired.llm.model, 16384, time.monotonic())
    wired.llm._ollama_context_observation = observed
    before = wired.llm.context_window_tokens
    assert before == 16384
    wired.controls["capacity"] = capacity
    selected = await wired.skill._get_vlm({})
    result = await selected.chat(messages=IMAGE)
    assert wired.llm._ollama_context_observation is observed
    assert wired.llm.context_window_tokens == before
    assert wired.calls[0].method == "GET" and wired.calls[0].url.host == "vision.invalid"
    if capacity == 32:
        assert result["error_code"] == "vision_request_unavailable" and len(wired.calls) == 1
    else:
        assert not result.get("error") and len(wired.calls) == 2


async def test_public_selected_failure_stops_without_further_dispatch(wired, monkeypatch):
    select(monkeypatch, provider="openai", model="gpt-4o", url="https://selected.invalid/v1")
    monkeypatch.setenv("FERAL_VLM_API_KEY", "inert-selected-key")
    wired.controls["failure"] = True
    monkeypatch.setattr(wired.skill, "_registered_authority", lambda *args: object())
    capture, action = AsyncMock(return_value="aW5lcnQ="), AsyncMock()
    monkeypatch.setattr(wired.skill, "_capture_screen", capture)
    monkeypatch.setattr(wired.skill, "_execute_action", action)
    result = await wired.skill.execute("execute_task", {"task": "inert task", "max_steps": 10}, {})
    assert result["success"] is False and result["error_code"] == "vision_request_unavailable"
    assert result["data"]["completed"] is False and len(wired.calls) == 1
    capture.assert_awaited_once()
    action.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 429, 503])
async def test_selected_provider_failure_has_no_fallback_or_private_diagnostic(wired, monkeypatch, caplog, status):
    select(monkeypatch, provider="openai", model="gpt-4o", url="https://selected.invalid/v1")
    monkeypatch.setenv("FERAL_VLM_API_KEY", "inert-selected-key")
    wired.controls["failure"] = True
    wired.controls["failure_status"] = status
    selected = await wired.skill._get_vlm({})
    response = await selected.chat(messages=IMAGE)
    assert response["error_code"] == "vision_request_unavailable"
    assert len(wired.calls) == 1 and wired.calls[0].url.host == "selected.invalid"
    assert "private-wire-canary" not in repr(response) + caplog.text
    assert "selected.invalid" not in repr(response) + caplog.text
    assert all(client.is_closed for client in wired.clients) and not wired.llm.client.is_closed


async def test_capped_cloud_multimodal_refuses_while_local_records_vision_site(wired, monkeypatch, tmp_path):
    budget = CostBudget(db_path=tmp_path / "cost.db", settings={"cost": {"global_per_hour_usd": 1}})
    wired.llm.set_cost_budget(budget)
    try:
        select(monkeypatch, provider="openai", model="gpt-4o", url="https://selected.invalid/v1")
        monkeypatch.setenv("FERAL_VLM_API_KEY", "inert-selected-key")
        response = await (await wired.skill._get_vlm({})).chat(messages=IMAGE)
        assert response["error_code"] == "pricing_unavailable" and wired.calls == []
        select(monkeypatch)
        response = await (await wired.skill._get_vlm({})).chat(messages=IMAGE)
        assert response.get("error") is None
        rows = await budget.get_reservations()
        assert len(rows) == 1 and rows[0]["call_site"] == "vision"
        assert rows[0]["model"] == "llava:7b" and rows[0]["pricing_basis"] == "local_compute_only"
        assert rows[0]["status"] == "settled" and rows[0]["actual_dollars"] == 0
    finally:
        await budget.close()


async def test_cancellation_closes_owned_client_not_primary(wired, monkeypatch):
    select(monkeypatch, provider="openai", model="gpt-4o", url="https://selected.invalid/v1")
    monkeypatch.setenv("FERAL_VLM_API_KEY", "inert-selected-key")
    wired.controls["entered"], wired.controls["release"] = asyncio.Event(), asyncio.Event()
    selected = await wired.skill._get_vlm({})
    task = asyncio.create_task(selected.chat(messages=IMAGE))
    await asyncio.wait_for(wired.controls["entered"].wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(wired.calls) == 1 and all(client.is_closed for client in wired.clients)
    assert not wired.llm.client.is_closed and wired.llm.provider == "openai"


async def test_actual_outer_review_selected_vision_preserves_inner_click_review(authority, wired, monkeypatch, tmp_path):
    from agents.tool_runner import ToolRunner
    from models.skill_manifest import SkillManifest
    from security.exec_approvals import ApprovalManager
    from security.trust_ledger import TrustLedger
    from skills.call_context import current_context

    select(monkeypatch)
    wired.controls["response"] = '{"action":"click","x":4,"y":7}'
    registry = authority.state.skill_registry
    registry.register(SkillManifest.model_validate_json((Path(__file__).parents[1] / "skills/manifests/agentic_computer_use.json").read_text()))
    for endpoint in registry.skills["gui_computer_use"].endpoints:
        if endpoint.id == "screenshot":
            endpoint.read_only_hint, endpoint.safety_tier = True, "auto"
        elif endpoint.id == "mouse_click":
            endpoint.requires_user_approval, endpoint.safety_tier = True, "confirm"
    orch = SimpleNamespace(llm=wired.llm, skills=registry, executor=authority.state.skill_executor,
        _active_turns={"exact-owner": [{"_feral_turn_id": "outer-turn"}]}, _session_surfaces={},
        _mcp_client=None, daemons={}, _send_text=AsyncMock())
    runner = ToolRunner(orch, autonomy_mode="strict", trust_ledger=TrustLedger(persist=False),
        approval_manager=ApprovalManager(db_path=str(tmp_path / "outer-approval.db")))
    orch.tool_runner = runner
    authority.state.orchestrator, authority.state.config, authority.state.tool_runner = orch, wired.config, runner
    monkeypatch.setitem(sys.modules, "api.state", SimpleNamespace(state=authority.state))
    captures, clicks = [], []

    async def inert_effect(tool_name, args, manifest, endpoint):
        if tool_name == "agentic_computer_use__execute_task":
            return await authority.skill.execute("execute_task", args, {})
        if tool_name == "gui_computer_use__screenshot":
            captures.append(current_context())
            return {"success": True, "status_code": 200, "data": {"image_base64": "aW5lcnQ="}, "error": None}
        clicks.append(tool_name)
        return {"success": True, "data": None, "error": None}

    authority.state.skill_executor._execute_inner.side_effect = inert_effect
    call = {"id": "outer-call", "name": "agentic_computer_use__execute_task", "args": {"task": "inert task", "max_steps": 2}}
    pending = await runner.execute_tool_call_for_llm("exact-owner", call, registry.get_all_tools())
    assert pending["status"] == "pending_approval" and captures == wired.calls == []
    grant = runner.approve_pending(pending["request_id"], session_id="exact-owner", exact_once=True)
    result = await runner.execute_tool_call_for_llm("exact-owner", call, registry.get_all_tools(), approval=grant["approval"])
    assert result["status"] == "pending_approval" and clicks == [] and len(captures) == 1
    assert len(wired.calls) == 2 and captures[0].session_id == "exact-owner" and captures[0].turn_id == "outer-turn"
    assert runner.list_pending()[0]["tool_name"] == "gui_computer_use__mouse_click"
