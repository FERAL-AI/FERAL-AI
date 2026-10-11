"""Exact cancellation and real ASGI WebSocket tracked-turn integration."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from agents.chat_turns import turn_audit
from agents.orchestrator import Orchestrator
from gateway.protocol import MethodRegistry, register_core_methods
from memory.store import MemoryStore
from tests.test_chat_turn_receipts import tracked as _tracked, submitted, terminal
from tests.test_server_websocket import _make_ws_mock_state
from tests.test_stream_nonstream_parity import _make_default_gate_orchestrator

tracked = _tracked


async def test_immediate_abort_before_runner_instruction_settles_receipt(tracked):
    manager, _state = tracked
    receipt, owner, emit, run = await submitted(manager)
    result = await manager.abort(owner=owner, session_id="thread-A", turn_id=receipt["turn_id"], request_id=receipt["request_id"])
    assert result["cancel_requested"] is True
    final = await terminal(manager, receipt)
    assert final["processing_outcome"] == "cancelled"
    run.assert_not_awaited()
    assert final["action_outcome"] == "not_asserted"
    for _ in range(100):
        if not manager._live:
            break
        await asyncio.sleep(0)
    assert manager._live == {}


@pytest.mark.parametrize("handler", ["handle_command", "handle_command_stream"])
async def test_cancelling_queued_b_does_not_teardown_active_a_children(handler):
    orch = Orchestrator.__new__(Orchestrator)
    orch._session_locks = {}
    entered, release = asyncio.Event(), asyncio.Event()

    async def body(*args):
        entered.set()
        await release.wait()

    orch._handle_command_impl = body
    orch._handle_command_stream_impl = body
    teardown = []
    orch._w17_cancel_subsessions_nowait = teardown.append
    active = asyncio.create_task(getattr(orch, handler)("thread-A", "A"))
    await entered.wait()
    queued = asyncio.create_task(getattr(orch, handler)("thread-A", "B"))
    await asyncio.sleep(0)
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    assert teardown == [] and not active.done()
    release.set()
    await active
    assert teardown == ["thread-A"]


async def test_abort_wrong_owner_session_or_request_cannot_cancel_active_turn(tracked):
    manager, _state = tracked
    entered, release = asyncio.Event(), asyncio.Event()

    async def run():
        audit = turn_audit("thread-A")
        audit.began = True
        entered.set()
        await release.wait()
        return "finished"

    receipt, owner, _emit, _run = await submitted(manager, run=run)
    await entered.wait()
    for wrong_owner, session, request in [(object(), "thread-A", receipt["request_id"]),
                                          (owner, "thread-C", receipt["request_id"]),
                                          (owner, "thread-A", str(uuid4()))]:
        result = await manager.abort(owner=wrong_owner, session_id=session, request_id=request, turn_id=receipt["turn_id"])
        assert result["cancel_requested"] is False
    result = await manager.abort(owner=owner, session_id="thread-A", request_id=receipt["request_id"], turn_id=receipt["turn_id"])
    assert result["cancel_requested"] is True
    final = await terminal(manager, receipt)
    assert final["processing_outcome"] == "cancelled" and final["action_outcome"] == "unknown"


async def test_caught_cancellation_cannot_start_another_tool(tracked):
    from agents.tool_runner import ToolRunner
    manager, _state = tracked
    entered = asyncio.Event()
    attempted = []
    runner = ToolRunner.__new__(ToolRunner)

    async def run():
        turn_audit("thread-A").began = True
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                runner._guard_agent_lease()
            attempted.append("guard blocked")
            return "cancelled despite caught coroutine cancellation"

    receipt, owner, _emit, _run = await submitted(manager, run=run)
    await entered.wait()
    await manager.abort(owner=owner, session_id="thread-A", request_id=receipt["request_id"], turn_id=receipt["turn_id"])
    final = await terminal(manager, receipt)
    assert attempted == ["guard blocked"] and final["processing_outcome"] == "cancelled"


@pytest.fixture
def tracked_client(tmp_path, monkeypatch):
    import api.server as server
    store = MemoryStore(db_path=str(tmp_path / "real-memory.db"))
    state = _make_ws_mock_state()
    state.memory = store
    state.skill_gen = None
    state.chat_turns = None
    state.gateway_registry = MethodRegistry()
    register_core_methods(state.gateway_registry, state)
    orch = _make_default_gate_orchestrator()
    orch._streaming_enabled = True
    state.orchestrator = orch

    async def forward(session_id, message):
        ws = state.sessions.get(session_id)
        if ws is None:
            return False
        await ws.send_json(message.model_dump())
        return True

    orch.send = forward
    monkeypatch.setattr(server, "state", state)
    monkeypatch.setattr(server, "is_localhost", lambda _: True)
    monkeypatch.setattr(server._session_auth_module, "transport_is_trusted", lambda _: True)
    prepare = AsyncMock(side_effect=lambda **kwargs: (kwargs["text"], {"source": "websocket"}, kwargs["text"]))
    monkeypatch.setattr(server, "_prepare_chat_turn_context", prepare)
    app = FastAPI()
    app.websocket("/v1/session")(server.client_session)
    with TestClient(app) as client:
        yield client, state, orch, prepare
        client.portal.call(store.aclose)


def receive_type(ws, kind):
    for _ in range(40):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"No {kind} frame")


def test_actual_ws_capability_and_multiround_terminal_correlate_after_tools(tracked_client):
    client, state, orch, prepare = tracked_client
    rounds = []

    async def stream(*args, **kwargs):
        rounds.append(kwargs)
        if len(rounds) == 1:
            yield {"type": "text_delta", "content": "I will inspect it."}
            yield {"type": "tool_call_delta", "tool_call": {"id": "fixture-tool", "name": "notes_memory__default", "args": {}}}
        else:
            yield {"type": "text_delta", "content": "Actual final result"}
        yield {"type": "done"}

    orch.llm.chat_stream = stream
    orch._execute_tool_call_for_llm = AsyncMock(return_value={"success": True, "data": {"message": "fixture"}})
    orch._try_genui_for_result = AsyncMock()
    request = str(uuid4())
    with client.websocket_connect("/v1/session?session_id=thread-A") as ws:
        ws.send_json({"type": "req", "id": str(uuid4()), "method": "chat.capabilities", "params": {}})
        capability = receive_type(ws, "res")
        assert capability["payload"]["turn_contract_versions"] == [1]
        assert rounds == [] and state.chat_turns is None
        ws.send_json({"type": "text_command", "msg_id": request, "session_id": "forged", "payload": {"text": "inspect fixture", "turn_contract_version": 1}})
        ack = receive_type(ws, "chat_turn_accepted")
        assert ack["session_id"] == "thread-A" and ack["payload"]["request_id"] == request
        observed = []
        while True:
            frame = ws.receive_json()
            observed.append(frame)
            if frame["type"] == "chat_turn_terminal":
                break
        final = observed[-1]["payload"]
        assert final["turn_id"] == ack["payload"]["turn_id"] and final["request_id"] == request
        assert final["processing_outcome"] == "completed" and final["final_text"] == "Actual final result"
        assert final["action_outcome"] == "not_asserted" and len(rounds) == 2
        assert any(frame["type"] == "tool_result" for frame in observed)
        assert any(frame["type"] == "stream_delta" and frame["payload"].get("is_final") for frame in observed[:-1])
        prepare.assert_awaited_once()


def test_actual_gateway_abort_is_responsive_while_provider_waits(tracked_client):
    client, _state, orch, _prepare = tracked_client

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()
        yield {"type": "done"}

    orch.llm.chat_stream = blocked
    request = str(uuid4())
    with client.websocket_connect("/v1/session?session_id=thread-A") as ws:
        ws.send_json({"type": "req", "id": request, "method": "chat.send", "params": {"text": "wait fixture", "turn_contract_version": 1, "session_id": "foreign"}})
        ack = receive_type(ws, "res")["payload"]
        ws.send_json({"type": "req", "id": str(uuid4()), "method": "chat.abort", "params": {"turn_id": ack["turn_id"], "request_id": request, "session_id": "foreign"}})
        abort = receive_type(ws, "res")
        assert abort["payload"]["status"] == "cancel_requested"
        terminal_frame = receive_type(ws, "event")
        while terminal_frame["event"] != "chat.turn_terminal":
            terminal_frame = receive_type(ws, "event")
        assert terminal_frame["payload"]["processing_outcome"] == "cancelled"
        assert terminal_frame["payload"]["session_id"] == "thread-A"


@pytest.mark.parametrize("outcome", ["pending", "error", "budget", "refused", "missing_runtime"])
def test_actual_turn_hooks_classify_pending_failure_budget_and_refusal(tracked_client, outcome):
    client, state, orch, _prepare = tracked_client
    rounds = []
    if outcome == "pending":
        orch.tool_runner.set_autonomy_mode("strict")
        orch.skills.skills["notes_memory"].endpoints[0].read_only_hint = False
        orch.executor.execute = AsyncMock()
    elif outcome == "refused":
        orch._execute_tool_call_for_llm = AsyncMock(return_value={"success": False, "status": "PermissionOutcome::Deny", "safety_level": "deny"})
    elif outcome == "missing_runtime":
        state.orchestrator = None

    async def stream(*args, **kwargs):
        rounds.append(kwargs)
        if outcome == "error":
            yield {"type": "error", "content": "fixture provider failure"}
        elif outcome == "budget":
            yield {"type": "budget_exceeded", "payload": {"cap_dollars": 1, "current_dollars": 1}}
        elif len(rounds) == 1:
            yield {"type": "tool_call_delta", "tool_call": {"id": "fixture-policy", "name": "notes_memory__default", "args": {}}}
            yield {"type": "done"}
        else:
            yield {"type": "text_delta", "content": "fixture final status"}
            yield {"type": "done"}

    orch.llm.chat_stream = stream
    with client.websocket_connect("/v1/session?session_id=policy-fixture") as ws:
        ws.send_json({"type": "text_command", "msg_id": str(uuid4()), "payload": {"text": "fixture policy task", "turn_contract_version": 1}})
        receive_type(ws, "chat_turn_accepted")
        final = receive_type(ws, "chat_turn_terminal")["payload"]
        assert final["processing_outcome"] == {"pending": "awaiting_approval", "error": "failed", "budget": "budget_exceeded", "refused": "refused", "missing_runtime": "unavailable"}[outcome]
        assert final["action_outcome"] == "not_asserted"
        if outcome == "pending":
            assert final["approval_request_ids"]
            orch.executor.execute.assert_not_awaited()


def test_actual_ws_auth_first_and_replayed_terminal_do_not_execute_again(tracked_client, monkeypatch):
    import api.server as server
    client, _state, orch, _prepare = tracked_client
    monkeypatch.setattr(server, "is_localhost", lambda _: False)
    monkeypatch.setattr(server, "verify_session", lambda token: token == "fixture-session-token")
    request = str(uuid4())
    command = {"type": "text_command", "msg_id": request, "payload": {"text": "fixture authenticated task", "turn_contract_version": 1}}
    with client.websocket_connect("/v1/session?session_id=auth-thread") as ws:
        ws.send_json({"type": "auth", "token": "fixture-session-token"})
        ws.send_json(command)
        ack = receive_type(ws, "chat_turn_accepted")
        original = receive_type(ws, "chat_turn_terminal")
    with client.websocket_connect("/v1/session?session_id=auth-thread") as ws:
        ws.send_json({"type": "auth", "token": "fixture-session-token"})
        ws.send_json(command)
        replay_ack = receive_type(ws, "chat_turn_accepted")
        replay = receive_type(ws, "chat_turn_terminal")
        assert replay_ack["payload"]["turn_id"] == ack["payload"]["turn_id"]
        assert replay["payload"]["final_text"] == original["payload"]["final_text"]
        assert replay["payload"]["replayed"] is True
    assert orch._route_prompt.await_count == 1


async def test_detach_waits_for_owned_terminal_receipt_before_drained(tracked):
    manager, _state = tracked
    entered = asyncio.Event()

    async def run():
        turn_audit("thread-A").began = True
        entered.set()
        await asyncio.Event().wait()

    receipt, owner, _emit, _run = await submitted(manager, run=run)
    await entered.wait()
    assert await manager.detach(owner) is True
    final = await manager.status(session_id="thread-A", request_id=receipt["request_id"], turn_id=receipt["turn_id"])
    assert final["processing_outcome"] == "cancelled"
    assert final["action_outcome"] == "unknown"


async def test_detach_cannot_report_drained_when_owner_ignores_cancellation(tracked):
    manager, _state = tracked
    entered, release = asyncio.Event(), asyncio.Event()

    async def run():
        turn_audit("thread-A").began = True
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            return "late fixture result"

    receipt, owner, _emit, _run = await submitted(manager, run=run)
    await entered.wait()
    try:
        assert await manager.detach(owner) is False
        unresolved = await manager.status(session_id="thread-A", request_id=receipt["request_id"], turn_id=receipt["turn_id"])
        assert unresolved["status"] == "running" and "processing_outcome" not in unresolved
    finally:
        release.set()
    final = await terminal(manager, receipt)
    assert final["processing_outcome"] == "cancelled" and final["action_outcome"] == "unknown"
