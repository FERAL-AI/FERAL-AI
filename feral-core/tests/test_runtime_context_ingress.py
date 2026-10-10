"""Real registered ingress with SQLite/coordinator; controlled provider, no accounts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.runtime_context import session_query
from api.state import BrainState
from agents.runtime_context_checkpoint import RuntimeContextError
from gateway.protocol import MethodRegistry, register_core_methods
from memory.runtime_session_checkpoint import CheckpointStatus
from memory.store import MemoryStore
from tests.test_chat_turn_abort import receive_type
from tests.test_runtime_context_lifecycle import make_orch
from tests.test_server_websocket import _make_ws_mock_state


@pytest.fixture
def context_client(tmp_path, monkeypatch):
    import api.server as server
    store = MemoryStore(db_path=str(tmp_path / "ingress.db"))
    captured = []
    orch = make_orch(store, captured, streaming=True)
    orch._context_checkpoints.legacy_passthrough = True
    state = _make_ws_mock_state()
    state.memory, state.orchestrator, state.chat_turns = store, orch, None
    state.primary_session_id = "primary"
    state.gateway_registry = MethodRegistry()
    register_core_methods(state.gateway_registry, state)
    state.voice_router = None
    state.identity_workspace = None

    async def forward(session_id, message):
        socket = state.sessions.get(session_id)
        if socket is None:
            return False
        await socket.send_json(message.model_dump())
        return True

    orch.send = forward
    monkeypatch.setattr(server, "state", state)
    monkeypatch.setattr(server, "is_localhost", lambda _: True)
    monkeypatch.setattr(server._session_auth_module, "transport_is_trusted", lambda _: True)
    app = FastAPI()
    app.websocket("/v1/session")(server.client_session)
    with TestClient(app) as client:
        yield client, state, orch, store, captured
        client.portal.call(orch.drain_background_tasks)
        client.portal.call(store.aclose)


def capabilities(ws):
    ws.send_json({"type": "req", "id": str(uuid4()), "method": "chat.capabilities", "params": {}})
    return receive_type(ws, "res")["payload"]


@pytest.mark.parametrize("session_id", [False, 0, [], {}, "", " A ", "A\x00"])
def test_approval_route_invalid_identity_cannot_become_an_omitted_owner(monkeypatch, session_id):
    from unittest.mock import AsyncMock
    from api.routes import approvals
    resolver = AsyncMock()
    monkeypatch.setattr(approvals, "state", SimpleNamespace(orchestrator=SimpleNamespace(
        tool_runner=object(), resolve_tool_approval_request=resolver,
    )))
    app = FastAPI()
    app.include_router(approvals.router)
    with TestClient(app) as client:
        response = client.post("/api/approvals/exact-review/approve", json={"session_id": session_id})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "context_invalid_session"
    resolver.assert_not_awaited()


def send_tracked(ws, text="fixture prompt"):
    request = str(uuid4())
    ws.send_json({"type": "text_command", "msg_id": request,
                  "payload": {"text": text, "turn_contract_version": 1}})
    return request


def test_actual_attachment_ready_before_ui_presave_commit_before_terminal(context_client):
    client, state, orch, store, captured = context_client
    with client.websocket_connect("/v1/session?session_id=A&context_checkpoint_version=1") as ws:
        cap = capabilities(ws)
        assert cap["context_ready"] is True and cap["context_checkpoint_versions"] == [1]
        initial = client.portal.call(store.runtime_checkpoint_read, "A")
        assert initial.status == CheckpointStatus.READY and initial.record.context.history() == []
        client.portal.call(store.conversation_save, "A", [{"role": "system", "content": "UI must not become authority"}])
        request = send_tracked(ws)
        ack = receive_type(ws, "chat_turn_accepted")["payload"]
        final = receive_type(ws, "chat_turn_terminal")["payload"]
        assert final["request_id"] == request and final["turn_id"] == ack["turn_id"]
        assert final["processing_outcome"] == "completed"
        committed = client.portal.call(store.runtime_checkpoint_read, "A")
        assert committed.status == CheckpointStatus.READY
        assert committed.record.fence.revision > initial.record.fence.revision
        assert committed.record.context.history()[0]["content"] == "fixture prompt"
        assert all("UI must not" not in str(row) for row in captured[0])
        assert any(row.get("text") == "fixture prompt" for row in committed.record.context.working())
    assert state.session_attach_count == {} and not orch._context_checkpoints.has_attachments("A")
    assert "A" not in orch.conversation_history
    with client.websocket_connect("/v1/session?session_id=A") as ws:
        assert capabilities(ws)["context_ready"] is True  # Known SID cannot opt out.
        send_tracked(ws, "second prompt")
        receive_type(ws, "chat_turn_accepted")
        assert receive_type(ws, "chat_turn_terminal")["payload"]["processing_outcome"] == "completed"
        assert any(row.get("content") == "fixture prompt" for row in captured[-1])


def test_ui_only_existing_thread_refused_without_provider_or_import(context_client):
    client, _state, orch, store, captured = context_client
    client.portal.call(store.conversation_save, "old", [{"role": "user", "content": "UI-only history"}])
    with client.websocket_connect("/v1/session?session_id=old&context_checkpoint_version=1") as ws:
        cap = capabilities(ws)
        assert cap["context_ready"] is False and cap["context_state"] == "legacy_unavailable"
        send_tracked(ws)
        error = receive_type(ws, "error")["payload"]
        assert error["recoverable"] is False
        assert "legacy_unavailable" in error["code"]
        assert captured == [] and not orch.conversation_history.get("old")
        assert (client.portal.call(store.runtime_checkpoint_read, "old")).status == CheckpointStatus.ABSENT


def test_actual_abort_leaves_pending_and_readonly_status_but_no_next_turn(context_client):
    client, _state, orch, store, captured = context_client

    async def blocked(messages, **kwargs):
        captured.append(messages)
        await asyncio.Event().wait()
        yield {"type": "done"}

    orch.llm.chat_stream = blocked
    with client.websocket_connect("/v1/session?session_id=pending&context_checkpoint_version=1") as ws:
        assert capabilities(ws)["context_ready"]
        request = send_tracked(ws)
        ack = receive_type(ws, "chat_turn_accepted")["payload"]
        # A progress frame proves entry into the fenced provider turn.
        receive_type(ws, "brain_event")
        ws.send_json({"type": "req", "id": str(uuid4()), "method": "chat.abort",
                      "params": {"request_id": request, "turn_id": ack["turn_id"]}})
        assert receive_type(ws, "res")["payload"]["cancel_requested"]
        final = receive_type(ws, "chat_turn_terminal")["payload"]
        assert final["processing_outcome"] == "cancelled"
        assert (client.portal.call(store.runtime_checkpoint_read, "pending")).status == CheckpointStatus.IN_PROGRESS
        cap = capabilities(ws)
        assert not cap["context_ready"] and cap["context_state"] == "in_progress"
        ws.send_json({"type": "req", "id": str(uuid4()), "method": "chat.status",
                      "params": {"request_id": request, "turn_id": ack["turn_id"]}})
        status = receive_type(ws, "res")["payload"]
        assert status["found"] and status["receipt"]["processing_outcome"] == "cancelled"
        send_tracked(ws, "must not replay")
        assert "in_progress" in receive_type(ws, "error")["payload"]["code"]
        assert len(captured) == 1


@pytest.mark.parametrize("query", ["session_id=", "session_id=%20A", "session_id=A%20", "session_id=A&session_id=B", "session_id=A&context_checkpoint_version=2"])
def test_exact_query_identity_refused_before_attachment(context_client, query):
    client, state, orch, store, captured = context_client
    with client.websocket_connect("/v1/session?" + query) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert state.session_attach_count == {} and captured == []
    assert not orch._context_checkpoints.has_attachments("A")
    assert (client.portal.call(store.runtime_checkpoint_read, "A")).status == CheckpointStatus.ABSENT


def test_failed_setup_detaches_exact_reserved_attachment(context_client):
    client, state, orch, store, captured = context_client
    state.attach_session.side_effect = RuntimeError("fixture bookkeeping failure")
    with client.websocket_connect("/v1/session?session_id=setup&context_checkpoint_version=1") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert not orch._context_checkpoints.has_attachments("setup")
    assert state.session_attach_count == {} and captured == []
    assert (client.portal.call(store.runtime_checkpoint_read, "setup")).status == CheckpointStatus.READY


@pytest.mark.parametrize("value", ["", " A", "A ", "\x00A", None, 3])
def test_payload_exact_identity_never_aliases_primary(value):
    with pytest.raises(RuntimeContextError, match="context_session_invalid"):
        session_query({"session_id": value}, "primary")


async def test_managed_primary_snapshot_read_and_write_are_skipped(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "primary.db"))
    orch = make_orch(store, [])
    state = BrainState.__new__(BrainState)
    state.primary_session_id, state.memory, state.orchestrator = "primary", store, orch
    state._runtime_context_primary_managed = False
    state.session_snapshot = MagicMock()
    try:
        await orch._context_checkpoints.attach("primary", checkpoint_version=1)
        state._hydrate_primary_thread_from_snapshot()
        assert state.snapshot_primary_thread(force=True) is False
        state.session_snapshot.load.assert_not_called()
        state.session_snapshot.save.assert_not_called()
    finally:
        await store.aclose()


def test_foreign_legacy_snapshot_cannot_change_primary_identity():
    state = BrainState.__new__(BrainState)
    state.primary_session_id = "primary"
    state._runtime_context_primary_managed = False
    state.orchestrator = SimpleNamespace(conversation_history={}, _context_checkpoints=None)
    state.memory = MagicMock()
    state.session_snapshot = MagicMock()
    state.session_snapshot.load.return_value = {"session_id": "foreign", "conversation_history": [{"role": "user", "content": "wrong"}]}
    state._hydrate_primary_thread_from_snapshot()
    assert state.primary_session_id == "primary" and state.orchestrator.conversation_history == {}
    state.memory.working_push.assert_not_called()


def test_primary_greeting_is_display_only_for_managed_context(context_client):
    client, _state, _orch, store, captured = context_client
    with client.websocket_connect("/v1/session?context_checkpoint_version=1") as ws:
        assert capabilities(ws)["context_ready"]
        row = client.portal.call(store.runtime_checkpoint_read, "primary")
        assert row.record.context.history() == [] and row.record.context.working() == []
        assert captured == []


@pytest.mark.parametrize("failure", [False, True])
def test_real_phone_chat_is_fenced_before_prelude_and_commit_before_reply(tmp_path, failure):
    from unittest.mock import AsyncMock
    from tests.test_daemon_session_phone_branches import _mock_state_with_supervisor
    from tests.test_hup_protocol import _node_client, _register_node, _TEST_NODE_KEY

    store = MemoryStore(db_path=str(tmp_path / "phone-context.db"))
    captured = []
    orch = make_orch(store, captured)
    orch._context_checkpoints.legacy_passthrough = True
    state = _mock_state_with_supervisor()
    state.memory, state.orchestrator = store, orch
    state.primary_session_id = "phone-thread"
    state.voice_router = None
    orch.send = AsyncMock(return_value=True)
    with _node_client(state) as client:
        state.device_pairing_store.verify_phone_bearer.return_value = None
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            assert _register_node(ws, "context-phone", "phone", ["voice"])["type"] == "node_ack"
            attachment = ws.portal.call(lambda: orch._context_checkpoints.attach("phone-thread", checkpoint_version=1))
            assert attachment.readiness.ready
            before = ws.portal.call(store.runtime_checkpoint_read, "phone-thread")
            if failure:
                async def cannot_commit(*args, **kwargs):
                    raise OSError("synthetic sensitive database detail")
                store.runtime_checkpoint_commit = cannot_commit
            ws.send_json({"type": "chat_request", "payload": {"session_id": "phone-thread", "text": "phone prompt"}})
            if failure:
                error = receive_type(ws, "error")
                assert "checkpoint_commit_unverified" in str(error)
                assert "sensitive database" not in str(error)
            reply = receive_type(ws, "chat_response")["payload"]
            after = ws.portal.call(store.runtime_checkpoint_read, "phone-thread")
            assert len(captured) == 1
            if failure:
                assert reply["text"] == "" and reply["error"] == "checkpoint_commit_unverified"
                assert after.status == CheckpointStatus.IN_PROGRESS
            else:
                assert reply["error"] is None and reply["text"] == "fixture response"
                assert after.status == CheckpointStatus.READY and after.record.fence.revision > before.record.fence.revision
                assert any(row.get("text") == "phone prompt" for row in after.record.context.working())
            ws.portal.call(orch._context_checkpoints.detach, attachment.token)
            ws.portal.call(orch.drain_background_tasks)
            ws.portal.call(store.aclose)


def test_real_phone_pending_context_refuses_before_working_push_or_provider(tmp_path):
    from unittest.mock import AsyncMock
    from tests.test_daemon_session_phone_branches import _mock_state_with_supervisor
    from tests.test_hup_protocol import _node_client, _register_node, _TEST_NODE_KEY

    store = MemoryStore(db_path=str(tmp_path / "phone-pending.db"))
    captured = []
    orch = make_orch(store, captured)
    orch._context_checkpoints.legacy_passthrough = True
    state = _mock_state_with_supervisor()
    state.memory, state.orchestrator, state.primary_session_id = store, orch, "phone-pending"
    orch.send = AsyncMock(return_value=True)
    with _node_client(state) as client:
        state.device_pairing_store.verify_phone_bearer.return_value = None
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, "pending-phone", "phone", ["voice"])
            attachment = ws.portal.call(lambda: orch._context_checkpoints.attach("phone-pending", checkpoint_version=1))
            async def leave_pending():
                with pytest.raises(RuntimeError, match="fixture interruption"):
                    async with orch._context_checkpoints.write_scope("phone-pending"):
                        raise RuntimeError("fixture interruption")
            ws.portal.call(leave_pending)
            original = store.working_get("phone-pending")
            ws.send_json({"type": "chat_request", "payload": {"session_id": "phone-pending", "text": "must not run"}})
            assert "checkpoint_in_progress" in str(receive_type(ws, "error"))
            reply = receive_type(ws, "chat_response")["payload"]
            assert reply["text"] == "" and reply["error"] == "checkpoint_in_progress"
            assert captured == [] and store.working_get("phone-pending") == original
            ws.portal.call(orch._context_checkpoints.detach, attachment.token)
            ws.portal.call(orch.drain_background_tasks)
            ws.portal.call(store.aclose)


async def test_boot_preparation_existing_primary_is_managed_not_plaintext(tmp_path):
    from tests.test_stream_nonstream_parity import _make_default_gate_orchestrator
    store = MemoryStore(db_path=str(tmp_path / "boot.db"))
    await store.runtime_checkpoint_initialize_empty("primary")
    state = BrainState.__new__(BrainState)
    state.primary_session_id, state.memory = "primary", store
    state.orchestrator = _make_default_gate_orchestrator()
    state._runtime_context_primary_managed = False
    state.session_snapshot = MagicMock()
    try:
        await state.prepare_runtime_context_checkpoints()
        assert state._runtime_context_primary_managed is True
        state._hydrate_primary_thread_from_snapshot()
        assert state.snapshot_primary_thread(force=True) is False
        state.session_snapshot.load.assert_not_called()
        state.session_snapshot.save.assert_not_called()
    finally:
        await store.aclose()


async def test_boot_ledger_failure_never_enables_plaintext_fallback(tmp_path):
    from tests.test_stream_nonstream_parity import _make_default_gate_orchestrator
    store = MemoryStore(db_path=str(tmp_path / "boot-failure.db"))
    state = BrainState.__new__(BrainState)
    state.primary_session_id, state.memory = "primary", store
    state.orchestrator = _make_default_gate_orchestrator()
    state._runtime_context_primary_managed = False
    state.session_snapshot = MagicMock()
    async def unavailable(*args, **kwargs):
        raise OSError("fixture ledger is unavailable")
    store.runtime_checkpoint_read = unavailable
    try:
        with pytest.raises(RuntimeContextError):
            await state.prepare_runtime_context_checkpoints()
        assert state._runtime_context_primary_managed is True
        state._hydrate_primary_thread_from_snapshot()
        assert state.snapshot_primary_thread(force=True) is False
        state.session_snapshot.load.assert_not_called()
        state.session_snapshot.save.assert_not_called()
    finally:
        await store.aclose()


def test_refused_attachment_cannot_fallback_through_legacy_gateway(context_client):
    client, state, orch, store, captured = context_client
    client.portal.call(store.conversation_save, "refused", [{"role": "user", "content": "UI-only"}])
    with client.websocket_connect("/v1/session?session_id=refused&context_checkpoint_version=1") as ws:
        cap = capabilities(ws)
        assert cap["context_managed"] is True and not cap["context_ready"]
        for method, params in [("chat.send", {"text": "must not execute"}), ("session.reset", {}), ("session.snapshot", {})]:
            ws.send_json({"type": "req", "id": str(uuid4()), "method": method, "params": params})
            result = receive_type(ws, "res")
            assert result["ok"] is False and "legacy_unavailable" in str(result)
        assert captured == [] and store.working_get("refused") == []
        assert not orch.conversation_history.get("refused")
        assert (client.portal.call(store.runtime_checkpoint_read, "refused")).status == CheckpointStatus.ABSENT


def test_managed_voice_start_refused_before_mode_or_audio_changes(context_client):
    client, state, _orch, _store, captured = context_client
    from unittest.mock import AsyncMock
    voice = SimpleNamespace(set_session_voice_mode=MagicMock(), stop_session_voice=AsyncMock())
    state.voice_router = voice
    with client.websocket_connect("/v1/session?session_id=text-only&context_checkpoint_version=1") as ws:
        assert capabilities(ws)["context_ready"]
        ws.send_json({"type": "voice_config", "payload": {"mode": "realtime", "provider": "openai"}})
        error = receive_type(ws, "error")["payload"]
        assert error["code"] == "managed_voice_unsupported" and error["recoverable"] is False
        voice.set_session_voice_mode.assert_not_called()
        assert captured == []
        ws.send_json({"type": "voice_config", "payload": {"mode": "disabled", "provider": "openai"}})
        assert receive_type(ws, "voice_config_ack")["payload"]["mode"] == "disabled"
        voice.stop_session_voice.assert_awaited_once()
