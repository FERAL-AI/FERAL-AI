"""Complete local request planning against actual transports; no model/account IO."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.context_manager import OllamaContextRefusal, fit_request_history
from agents.local_tool_budget import DISCOVERY, LocalRequestRefusal, fit_local_request, local_input_bytes, retrieve_local_tools
from agents.token_estimate import estimate_tokens
from tests.test_local_tool_budget import tool
from tests.test_ollama_context_contract import Server, invoke, provider
from tests.test_stream_nonstream_parity import _make_default_gate_orchestrator


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def paired_turn(label, text):
    return [{"role": "user", "content": text},
            {"role": "assistant", "tool_calls": [{"id": label, "type": "function", "function": {"name": "files__read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": label, "content": "Exact file result " + text},
            {"role": "assistant", "content": "Exact grounded response"}]


@pytest.mark.parametrize("path", ["chat", "stream", "routed", "fallback"])
async def test_short_history_complete_request_trims_whole_old_turn_with_exact_recent_and_policy(path, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=16384)
    llm = provider(server, name="openai" if path == "fallback" else "ollama")
    messages = [{"role": "system", "content": "Exact authorization policy " * 200},
                *paired_turn("old", "旧🦦" * 2200),
                {"role": "system", "content": "Exact later policy: ask before writing"},
                *paired_turn("recent", "recent exact data"),
                {"role": "user", "content": "Latest question exact"}]
    before = copy.deepcopy(messages)
    assert len(messages) <= 15 and local_input_bytes(messages, []) > 32000
    try:
        await invoke(llm, path, messages)
        assert len(server.inference) == 1
        rows = server.inference[0]["messages"]
        assert rows == [messages[0], messages[5], *messages[6:]]
        assert local_input_bytes(rows, []) <= 32000
        assert estimate_tokens(wire({"messages": rows, "tools": []})) + 256 + 256 <= 16384
        assert messages == before
    finally:
        await llm.close()


@pytest.mark.parametrize("path", ["chat", "stream", "routed", "fallback"])
async def test_checked_token_capacity_trims_old_turn_even_when_bytes_fit(path, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=4096)
    llm = provider(server, name="openai" if path == "fallback" else "ollama")
    messages = [{"role": "system", "content": "Exact policy"},
                {"role": "user", "content": "旧" * 4000}, {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "preceding question"}, {"role": "assistant", "content": "preceding answer"},
                {"role": "user", "content": "Latest exact"}]
    before = copy.deepcopy(messages)
    assert local_input_bytes(messages, []) < 32000
    try:
        await invoke(llm, path, messages)
        assert server.inference[0]["messages"] == [messages[0], *messages[3:]]
        assert messages == before
        assert [r.url.path for r in server.requests] == ["/api/ps", "/v1/chat/completions"]
    finally:
        await llm.close()


def prose(size):
    return ("Exact synthetic context " * (size // 24 + 1))[:size]


def calibrated_native_request():
    """Synthetic shape calibrated to observed aggregate bytes, not a wire capture."""
    tools = [tool(name) for name in DISCOVERY] + [tool("files__write", "write file")]
    tools += [tool(f"optional_{i}__write", "write " + prose(850)) for i in range(9)]
    tools[-1]["function"]["description"] += prose(11692 - len(wire(tools).encode()))
    messages = [{"role": "system", "content": prose(18513)},
                {"role": "user", "content": "What is 13 plus 29? Reply only with the number."},
                {"role": "assistant", "content": "42"},
                {"role": "user", "content": prose(290)},
                {"role": "assistant", "tool_calls": [{"id": "old-write", "type": "function", "function": {"name": "files__write", "arguments": '{"path":"synthetic","content":"DENY"}'}}]},
                {"role": "tool", "tool_call_id": "old-write", "content": '{"status":"pending_approval","request_id":"synthetic-review"}'},
                {"role": "assistant", "content": "That action was sent for your approval."},
                {"role": "user", "content": "files__write write " + prose(311 - len("files__write write "))}]
    history_size = len(wire(messages[1:]).encode())
    messages[5]["content"] += prose(1888 - history_size)
    return messages, tools


@pytest.mark.parametrize("path", ["chat", "stream", "routed", "fallback"])
async def test_observed_native_byte_totals_fit_by_optional_whole_schema_retrieval(path, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    messages, tools = calibrated_native_request()
    assert len(wire(messages[:1]).encode()) == 18545
    assert len(wire(messages[1:]).encode()) == 1888
    assert len(wire(tools).encode()) == 11692
    assert local_input_bytes(messages, tools) == 32146
    body = {"messages": messages, "tools": tools}
    with pytest.raises(LocalRequestRefusal):
        fit_local_request(body, "ollama")
    assert local_input_bytes(body["messages"], tools) > 32000  # Protected minimum still too large.
    before = copy.deepcopy((messages, tools))
    server = Server(capacity=16384)
    llm = provider(server, name="openai" if path == "fallback" else "ollama")
    try:
        await invoke(llm, path, messages, tools)
        assert len(server.inference) == 1
        request = server.inference[0]
        assert local_input_bytes(request["messages"], request["tools"]) <= 32000
        assert request["messages"][1:] == messages[3:]
        assert request["messages"][0]["content"].startswith(messages[0]["content"] + "\n\nLocal tool discovery:")
        names = {t["function"]["name"] for t in request["tools"]}
        assert set(DISCOVERY) | {"files__write"} <= names
        assert len(request["tools"]) < len(tools)
        assert all(t in tools for t in request["tools"])  # Whole exact schemas, no field abbreviation.
        assert (messages, tools) == before
        # Retrieval is per request; a short new request gets the full fitting
        # catalogue again, without changing the registry or the old transcript.
        fresh = [{"role": "system", "content": "Exact fresh policy"}, {"role": "user", "content": "files__write write exact"}]
        await invoke(llm, path, fresh, tools)
        assert server.inference[-1]["tools"] == tools
        assert server.inference[-1]["messages"] == fresh
        assert (messages, tools) == before
    finally:
        await llm.close()


async def test_optional_schemas_also_fit_checked_token_budget(monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    tools = [tool(name) for name in DISCOVERY] + [tool("files__write", "write")]
    tools += [tool("optional__write", "write " + "旧" * 2400)]
    messages = [{"role": "system", "content": "Exact policy"}, {"role": "user", "content": "files__write write exact"}]
    server = Server(capacity=4096)
    llm = provider(server)
    before = copy.deepcopy((messages, tools))
    try:
        await invoke(llm, "chat", messages, tools)
        request = server.inference[0]
        assert len(request["tools"]) == 3
        assert request["tools"] == tools[:3]
        assert estimate_tokens(wire({"messages": request["messages"], "tools": request["tools"]})) + 512 <= 4096
        assert (messages, tools) == before
        assert [r.url.path for r in server.requests] == ["/api/ps", "/api/ps", "/v1/chat/completions"]
    finally:
        await llm.close()


async def test_changed_allocation_between_optional_selection_and_final_check_refuses(monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=4096)
    llm = provider(server)
    calls = 0
    def respond(request):
        nonlocal calls
        if request.url.path.endswith("/api/ps"):
            calls += 1
            if calls == 2:
                server.capacity = 512
        return server.respond(request)
    await llm.client.aclose()
    llm.client = httpx.AsyncClient(base_url=llm.base_url, transport=httpx.MockTransport(respond))
    tools = [tool(name) for name in DISCOVERY] + [tool("files__write", "write"), tool("optional__write", "write " + "旧" * 2400)]
    try:
        result = await llm.chat([{"role": "user", "content": "files__write write exact"}], tools, max_tokens=256)
        assert result["error_code"] == "local_context_overflow"
        assert calls == 2 and server.inference == []
    finally:
        await llm.close()


@pytest.mark.parametrize("kind", ["policy", "latest", "schema", "token"])
async def test_protected_minimum_refusal_never_cloud_failover(kind, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=4096 if kind == "token" else 16384)
    llm = provider(server)
    llm._config = {"fallback_providers": ["openai"]}
    llm._build_candidate_list = MagicMock(return_value=[("ollama", {"model": "fixture-model", "supported": True}),
                                                     ("openai", {"model": "fixture-cloud", "supported": True, "api_key": "fixture", "base_url": "http://configured-cloud/v1"})])
    messages = [{"role": "system", "content": "Exact policy"}, {"role": "user", "content": "files__write write exact"}]
    tools = [tool(name) for name in DISCOVERY] + [tool("files__write", "write")]
    if kind == "policy":
        messages[0]["content"] = "Exact policy " * 3000
    elif kind == "latest":
        messages[-1]["content"] += "🦦" * 9000
    elif kind == "schema":
        tools[-1]["function"]["description"] = "required full schema " * 1000
    else:
        messages[-1]["content"] += "旧" * 5000
    before = copy.deepcopy((messages, tools))
    real_client = httpx.AsyncClient
    def isolated(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.respond)
        return real_client(*args, **kwargs)
    try:
        with patch("agents.llm_provider.httpx.AsyncClient", isolated):
            result = await llm.chat_with_failover(messages, tools, max_tokens=256)
        assert result["error_code"] in {"local_request_byte_overflow", "local_tool_schema_budget", "local_context_overflow"}
        assert server.inference == []
        assert all(r.url.host == "127.0.0.1" for r in server.requests)
        assert (messages, tools) == before
    finally:
        await llm.close()


def test_current_tool_schema_mandatory_and_pairing_is_not_split():
    messages = [{"role": "system", "content": "Exact policy"}, {"role": "user", "content": "first"},
                {"role": "assistant", "tool_calls": [{"id": "call", "function": {"name": "files__read", "arguments": "{}"}}]},
                {"role": "user", "content": "current"}, {"role": "tool", "tool_call_id": "call", "content": "result"}]
    assert fit_request_history(messages, lambda rows: len(rows) < 5) == messages
    current = [{"role": "user", "content": "continue"}, *messages[2:3], messages[4]]
    _, selected = retrieve_local_tools(current, [tool(name) for name in DISCOVERY] + [tool("files__read")], request_fits=lambda rows, schemas: True)
    assert any(t["function"]["name"] == "files__read" for t in selected)
    with pytest.raises(LocalRequestRefusal):
        retrieve_local_tools(current, [tool(name) for name in DISCOVERY])


@pytest.mark.parametrize("handler", ["handle_command", "handle_command_stream"])
async def test_actual_orchestration_context_refusal_is_error_not_direct_mode(handler, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server()
    llm = provider(server)
    orch = _make_default_gate_orchestrator()
    llm.available = True
    orch.llm = llm
    orch._build_system_prompt = AsyncMock(return_value="Exact policy " * 3000)
    orch._direct_execute = AsyncMock()
    try:
        await getattr(orch, handler)("synthetic-thread", "hello")
        frames = [call.args[1] for call in orch.send.await_args_list]
        errors = [f.payload for f in frames if f.type == "error"]
        assert errors and errors[-1]["code"] == "local_request_byte_overflow"
        assert "input byte budget" in errors[-1]["message"]
        orch._direct_execute.assert_not_awaited()
        assert server.inference == []
        assert not any(f.type == "text_response" for f in frames)
        assert not any(row.get("role") == "assistant" for row in orch.conversation_history["synthetic-thread"])
    finally:
        tasks = list(orch._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await llm.close()


@pytest.mark.parametrize("handler", ["handle_command", "handle_command_stream"])
async def test_actual_daily_conversation_keeps_full_transcript_but_bounded_provider_view(handler, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server()
    llm = provider(server)
    llm.available = True
    orch = _make_default_gate_orchestrator()
    orch.llm = llm
    orch._build_system_prompt = AsyncMock(return_value=prose(28000))
    orch._direct_execute = AsyncMock()
    prompts = [f"Synthetic question {i}: " + prose(500) for i in range(16)]
    try:
        for prompt in prompts:
            await getattr(orch, handler)("daily-synthetic", prompt)
        saved = orch.conversation_history["daily-synthetic"]
        assert [r["content"] for r in saved if r.get("role") == "user"] == prompts
        assert len(server.inference) == len(prompts)
        assert all(local_input_bytes(r["messages"], r.get("tools", [])) <= 32000 for r in server.inference)
        assert server.inference[-1]["messages"][-1]["content"] == prompts[-1]
        assert any(r.get("content") == prompts[-2] for r in server.inference[-1]["messages"])
        assert len(server.inference[-1]["messages"]) < len(saved)
        assert not any(c.args[1].type == "error" for c in orch.send.await_args_list)
        orch._direct_execute.assert_not_awaited()
    finally:
        tasks = list(orch._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await llm.close()
