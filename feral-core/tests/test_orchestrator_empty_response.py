"""Empty successful HTTP completions must not become successful agent replies."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import httpx

from agents.chat_turns import ChatTurnManager
from agents.llm_provider import LLMProvider, ProviderCooldownTracker
from agents.orchestrator import Orchestrator
from memory.store import MemoryStore


CODEWORD = "SILVER-MAPLE-934"


class ScriptedProvider:
    available = True
    model_name = "synthetic-provider"
    extract_response = LLMProvider.extract_response

    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.rounds[min(len(self.calls) - 1, len(self.rounds) - 1)]
        if answer.get("error"):
            return {"error": answer["error"], "error_code": "synthetic_provider_error", "choices": []}
        message = {"role": "assistant", "content": answer.get("text", "")}
        if answer.get("tool"):
            message["tool_calls"] = [{
                "id": "synthetic-tool-1", "type": "function",
                "function": {"name": "notes_memory__default", "arguments": "{}"},
            }]
        return {"choices": [{"message": message, "finish_reason": "stop"}]}

    async def chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.rounds[min(len(self.calls) - 1, len(self.rounds) - 1)]
        if answer.get("text"):
            yield {"type": "text_delta", "content": answer["text"]}
        if answer.get("tool"):
            yield {"type": "tool_call_delta", "tool_call": {
                "id": "synthetic-tool-1", "name": "notes_memory__default", "args": {},
            }}
        if answer.get("error"):
            yield {"type": "error", "content": answer["error"], "error_code": "synthetic_provider_error"}
            return
        yield {"type": "done"}


async def run_tracked(tmp_path, *, streaming, rounds):
    frames = []

    async def send(session_id, message):
        frames.append(message.model_dump())

    registry = SimpleNamespace(skills={}, get_tools_for_skills=lambda skills: [])
    orch = Orchestrator(skill_registry=registry, send_to_client=send, daemons={})
    orch._multi_agent_enabled = False
    orch._streaming_enabled = streaming
    orch._route_prompt = AsyncMock(return_value=[])
    orch._ensure_core_skills = lambda skills: skills
    orch._build_system_prompt = AsyncMock(return_value="Synthetic test, no external actions.")
    orch._force_tool_for_query = lambda *args: None
    provider = ScriptedProvider(rounds)
    orch.llm = provider
    orch._call_llm_chat = provider.chat
    # Only the tool execution boundary is synthetic. The real loop still emits
    # its tool result, owns retries/history and settles the real SQLite receipt.
    orch._execute_tool_call_for_llm = AsyncMock(return_value={"success": True, "data": {"value": "local fixture"}})
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    manager = ChatTurnManager(SimpleNamespace(memory=store))
    owner = object()
    emitted = []

    async def emit(kind, payload):
        emitted.append((kind, payload))

    async def run():
        handler = orch.handle_command_stream if streaming else orch.handle_command
        return await handler("empty-response-fixture", "Reply with the test codeword only.")

    request_id = str(uuid4())
    try:
        accepted = await manager.submit(owner=owner, session_id="empty-response-fixture", request_id=request_id,
                                        terms={"text": "Reply with the test codeword only."}, run=run, emit=emit)
        tasks = [live.task for live in manager._live.values() if live.task is not None]
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        terminal = await manager.status(session_id="empty-response-fixture", request_id=request_id)
        assert terminal["turn_id"] == accepted["turn_id"]
        assert any(kind == "chat_turn_terminal" for kind, _payload in emitted)
        return orch, provider, frames, terminal
    finally:
        await store.aclose()
        if orch._background_tasks:
            await asyncio.gather(*list(orch._background_tasks), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_exhausted_empty_completion_is_failed_and_not_assistant_history(tmp_path, streaming):
    orch, provider, frames, terminal = await run_tracked(tmp_path, streaming=streaming, rounds=[{}])
    assert len(provider.calls) == 2, "Keep the existing one prompt-addition retry, never add a third request."
    errors = [frame["payload"] for frame in frames if frame["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "provider_empty_response"
    assert errors[0]["recoverable"] is True
    assert "retry" in errors[0]["message"].lower()
    assert "model" in errors[0]["message"].lower()
    assert terminal["processing_outcome"] == "failed"
    assert terminal["final_text"] == ""
    assert "context_checkpoint" not in terminal
    assert not any(frame["type"] == "text_response" for frame in frames)
    history = orch.conversation_history["empty-response-fixture"]
    assert not any(row.get("role") == "assistant" for row in history)
    assert "processed your request" not in str(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("rounds, calls", [([{"text": CODEWORD}], 1), ([{}, {"text": CODEWORD}], 2)])
async def test_short_unpunctuated_answer_and_retry_recovery_remain_completed(tmp_path, streaming, rounds, calls):
    orch, provider, frames, terminal = await run_tracked(tmp_path, streaming=streaming, rounds=rounds)
    assert len(provider.calls) == calls
    assert not any(frame["type"] == "error" for frame in frames)
    assert terminal["processing_outcome"] == "completed"
    assert terminal["final_text"] == CODEWORD
    assert any(row.get("content") == CODEWORD for row in orch.conversation_history["empty-response-fixture"])
    if streaming:
        assert "".join(frame["payload"]["delta"] for frame in frames if frame["type"] == "stream_delta") == CODEWORD
        assert sum(frame["payload"]["is_final"] for frame in frames if frame["type"] == "stream_delta") == 1
    else:
        assert [frame["payload"]["text"] for frame in frames if frame["type"] == "text_response"] == [CODEWORD]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_only_outcome_is_not_reclassified_as_empty_provider_failure(tmp_path, streaming):
    orch, provider, frames, terminal = await run_tracked(tmp_path, streaming=streaming, rounds=[{"tool": True}, {}])
    assert len(provider.calls) == 3
    orch._execute_tool_call_for_llm.assert_awaited_once()
    assert sum(frame["type"] == "tool_result" for frame in frames) == 1
    assert not any(frame["type"] == "error" for frame in frames)
    assert terminal["processing_outcome"] != "failed"
    assert "processed your request" not in str(frames)
    assert any(row.get("role") == "tool" for row in orch.conversation_history["empty-response-fixture"])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_provider_error_is_not_retried_or_replaced_by_empty_error(tmp_path, streaming):
    _orch, provider, frames, terminal = await run_tracked(tmp_path, streaming=streaming,
                                                        rounds=[{"error": "Synthetic failure"}])
    assert len(provider.calls) == 1
    assert [frame["payload"]["code"] for frame in frames if frame["type"] == "error"] == ["synthetic_provider_error"]
    assert terminal["processing_outcome"] == "failed"
    assert "context_checkpoint" not in terminal


@pytest.mark.asyncio
async def test_partial_stream_failure_keeps_delivered_prose_without_replay(tmp_path):
    _orch, provider, frames, terminal = await run_tracked(tmp_path, streaming=True,
                                                        rounds=[{"text": CODEWORD, "error": "Synthetic interruption"}])
    assert len(provider.calls) == 1
    assert "".join(frame["payload"]["delta"] for frame in frames if frame["type"] == "stream_delta") == CODEWORD
    assert [frame["payload"]["code"] for frame in frames if frame["type"] == "error"] == ["synthetic_provider_error"]
    assert terminal["processing_outcome"] == "failed"
    assert terminal["final_text"] == ""
    assert "context_checkpoint" not in terminal


class FragmentedSSE(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        # Exercise actual HTTP incremental line decoding, including a short
        # unpunctuated final answer and arbitrarily split DONE/usage frames.
        for offset in range(0, len(self.body), 7):
            yield self.body[offset:offset + 7]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["", CODEWORD])
@pytest.mark.parametrize("done_sentinel", [False, True])
async def test_actual_provider_parser_preserves_empty_and_short_final_sse(answer, done_sentinel):
    # A synthetic fixture of the observed HTTP-200/stop/empty-content shape;
    # this is not a claim to possess the original provider's raw SSE bytes.
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": answer}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 6649, "completion_tokens": 25, "total_tokens": 6674}},
    ]
    sse = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    if done_sentinel:
        sse += "data: [DONE]\n\n"
    inference = []

    def respond(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "fixture-model", "context_length": 16384}]})
        assert request.url.path == "/v1/chat/completions"
        inference.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FragmentedSSE(sse.encode()))

    provider = LLMProvider.__new__(LLMProvider)
    provider.provider = "ollama"
    provider.model = "fixture-model"
    provider.base_url = "http://127.0.0.1:11436/v1"
    provider.api_key = "fixture"
    provider._local_engine = None
    provider._config = {"fallback_providers": []}
    provider._budget_check = AsyncMock(return_value=None)
    provider._budget_record = AsyncMock()
    provider._cooldown = ProviderCooldownTracker()
    provider._last_budget_routing = {}
    provider.client = httpx.AsyncClient(base_url=provider.base_url, transport=httpx.MockTransport(respond))
    try:
        events = [event async for event in provider.chat_stream(messages=[{"role": "user", "content": "Synthetic codeword"}])]
        assert len(inference) == 1
        assert "".join(event["content"] for event in events if event["type"] == "text_delta") == answer
        assert not any(event["type"] in {"error", "tool_call_delta"} for event in events)
        if done_sentinel:
            assert events[-1]["type"] == "done"
            assert events[-1]["usage"]["output_tokens"] == 25
        else:
            # The existing parser bills an EOF usage block but does not
            # invent a DONE event. Prose must still survive that EOF.
            assert not any(event["type"] == "done" for event in events)
        provider._budget_record.assert_awaited_once()
    finally:
        await provider.close()
