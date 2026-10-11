"""Registered phone ingress uses real durable receipts; execution is inert."""
import asyncio
import json
import sqlite3
import threading
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memory.store import MemoryStore
from models.protocol import parse_message
from tests.test_phone_chat_responsive_intake import _state
from tests.test_hup_protocol import _TEST_NODE_KEY, _node_client, _register_node

pytestmark = [pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]
DEVICE = "ab570199-5a75-4477-b2f7-07b1d3475a2c"
OTHER = "261e24db-0f51-438e-8196-30443c015f17"


def frame(request=None, **changes):
    data = {"type": "chat_request", "msg_id": request or str(uuid4()), "payload": {
        "turn_contract_version": 1, "session_id": "shared", "text": "read fixture",
        "reply_mode": "final", "channel": "chat", "device_target": "brain",
        "reply_to": "delivery-hint",
    }}
    data["payload"].update(changes)
    return data


def ready(brain, *, device=DEVICE):
    brain.device_pairing_store.verify_device.return_value = device
    brain.device_pairing_store.verify_phone_bearer.return_value = None


def receipt(ws):
    frames = []
    for _ in range(5):
        item = ws.receive_json()
        frames.append(item)
        if item["type"] == "chat_turn_terminal":
            return frames
    raise AssertionError("No terminal receipt")


def test_actual_route_exact_request_replay_preserves_primary_owner_and_private_identity(tmp_path):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    native_owner = object()
    brain.sessions["shared"] = native_owner
    brain.chat_turns = None
    brain.orchestrator.handle_command = AsyncMock(return_value={"text": "fixture final"})
    request = frame()
    try:
        with _node_client(brain) as client:
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "alias-one", "phone")
                ws.send_json(request)
                first = receipt(ws)
                assert [item["type"] for item in first] == ["chat_turn_accepted", "chat_response", "chat_turn_terminal"]
                final = first[-1]["payload"]
                assert final["processing_outcome"] == "completed"
                assert final["action_outcome"] == "not_asserted"
                assert final["final_text"] == "fixture final"
                assert brain.sessions["shared"] is native_owner
                assert "paired_device_id" not in brain.orchestrator.handle_command.call_args.kwargs["context"]
                assert DEVICE not in json.dumps(first)
                assert sum(item.get("role") == "user" and item.get("text") == "read fixture" for item in store.working_get("shared")) == 1
                duplicate = frame(request["msg_id"], reply_to="new-delivery-hint", reply_mode="stream")
                ws.send_json(duplicate)
                replay = receipt(ws)
                assert [item["type"] for item in replay] == ["chat_turn_accepted", "chat_turn_terminal"]
                assert replay[-1]["payload"]["replayed"] is True
                assert replay[-1]["payload"]["turn_id"] == final["turn_id"]
                assert brain.orchestrator.handle_command.await_count == 1
                ws.send_json(frame(request["msg_id"], text="changed goal"))
                refused = ws.receive_json()
                assert refused["payload"]["name"] == "chat_turn_request_conflict"
        private = sqlite3.connect(store.db_path).execute("SELECT source_principal_json FROM chat_turn_receipts").fetchone()[0]
        assert json.loads(private) == {"version": 1, "kind": "paired_device", "device_id": DEVICE}
    finally:
        asyncio.run(store.aclose())


@pytest.mark.parametrize("invalid_id", [None, "", "not-a-uuid", str(uuid4()).upper()])
def test_explicit_raw_canonical_request_id_required_before_runner(tmp_path, invalid_id):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain.chat_turns = None
    try:
        with _node_client(brain) as client:
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "phone", "phone")
                message = frame()
                if invalid_id is None:
                    message.pop("msg_id")
                else:
                    message["msg_id"] = invalid_id
                ws.send_json(message)
                assert ws.receive_json()["payload"]["name"] == "chat_turn_invalid_request"
                brain.orchestrator.handle_command.assert_not_awaited()
                assert sqlite3.connect(store.db_path).execute("SELECT COUNT(*) FROM chat_turn_receipts").fetchone()[0] == 0
    finally:
        asyncio.run(store.aclose())


@pytest.mark.parametrize("version", [True, "1", 0, 2])
def test_phone_opt_in_version_is_exact(version):
    with pytest.raises(ValueError):
        parse_message(frame(turn_contract_version=version))


def test_foreign_verified_device_cannot_replay_same_session_request(tmp_path):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain.chat_turns = None
    brain.orchestrator.handle_command = AsyncMock(return_value={"text": "private fixture final"})
    message = frame()
    try:
        with _node_client(brain) as client:
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "one", "phone")
                ws.send_json(message)
                receipt(ws)
            ready(brain, device=OTHER)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "two", "phone")
                ws.send_json(message)
                refused = ws.receive_json()
                assert refused["payload"]["name"] == "chat_turn_request_conflict"
                assert "private fixture final" not in json.dumps(refused)
                assert brain.orchestrator.handle_command.await_count == 1
    finally:
        asyncio.run(store.aclose())


def test_tracked_error_empty_and_legacy_key_do_not_claim_completed(tmp_path):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain.chat_turns = None
    try:
        with _node_client(brain) as client:
            ready(brain, device=None)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "legacy", "phone")
                ws.send_json(frame())
                assert ws.receive_json()["payload"]["name"] == "chat_turn_device_authority_unavailable"
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "phone", "phone")
                brain.orchestrator.handle_command = AsyncMock(side_effect=RuntimeError("inert error"))
                ws.send_json(frame())
                assert receipt(ws)[-1]["payload"]["processing_outcome"] == "failed"
                store.working_push("shared", {"role": "assistant", "text": "prior unrelated answer"})
                brain.orchestrator.handle_command = AsyncMock(return_value=None)
                ws.send_json(frame())
                final = receipt(ws)[-1]["payload"]
                assert final["processing_outcome"] == "unavailable" and final["final_text"] == ""
    finally:
        asyncio.run(store.aclose())


def test_receive_stays_responsive_and_full_tracked_runner_keeps_session_lane(tmp_path):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain.chat_turns = None
    started = threading.Event()
    release = asyncio.Event()
    calls = []

    async def inert(**kwargs):
        calls.append(kwargs["text"])
        if kwargs["text"] == "first":
            started.set()
            await release.wait()
        return {"text": kwargs["text"]}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert)
    try:
        with _node_client(brain) as client:
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "phone", "phone")
                ws.send_json(frame(text="first"))
                assert ws.receive_json()["type"] == "chat_turn_accepted"
                assert started.wait(2)
                ws.send_json(frame(text="second"))
                assert ws.receive_json()["type"] == "chat_turn_accepted"
                ws.send_json({"type": "totally_bogus", "payload": {}})
                assert ws.receive_json()["type"] == "error"
                assert calls == ["first"]
                ws.portal.call(release.set)
                first = receipt(ws)
                second = receipt(ws)
                assert {first[-1]["payload"]["final_text"], second[-1]["payload"]["final_text"]} == {"first", "second"}
                assert calls == ["first", "second"]
        assert brain._phone_intake_locks == {}
    finally:
        asyncio.run(store.aclose())


@pytest.mark.parametrize("mode", ["disconnect", "revocation", "replacement"])
def test_stale_peer_cannot_publish_or_dispatch_after_owned_work_waits(tmp_path, mode):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain.chat_turns = None
    entered, cancelled = threading.Event(), threading.Event()
    release = asyncio.Event()

    async def inert(**kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"text": "must not reach stale peer"}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert)
    message = frame()
    try:
        with _node_client(brain) as client:
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "phone", "phone")
                ws.send_json(message)
                accepted = ws.receive_json()["payload"]
                assert entered.wait(2)
                if mode == "revocation":
                    ready(brain, device=None)
                    ws.portal.call(release.set)
                    ws.send_json({"type": "totally_bogus", "payload": {}})
                    # Ordered route response; no stale final was emitted.
                    assert ws.receive_json()["type"] == "error"
                elif mode == "replacement":
                    old_intake = brain.daemons["phone"]._feral_phone_chat_intake
                    client.portal = ws.portal
                    try:
                        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as new:
                            _register_node(new, "phone", "phone")
                            assert cancelled.wait(2)
                            assert old_intake.closed
                    finally:
                        client.portal = None
            if mode == "disconnect":
                assert cancelled.wait(2)
            rows = sqlite3.connect(store.db_path).execute(
                "SELECT receipt_json FROM chat_turn_receipts WHERE turn_id=?", (accepted["turn_id"],)
            ).fetchall()
            assert rows and json.loads(rows[0][0])["processing_outcome"] == "cancelled"
        assert brain.orchestrator._text_response_suppressed.get("shared") is None
        assert brain._phone_intake_locks == {}
    finally:
        asyncio.run(store.aclose())


def test_stream_request_and_authenticated_reconnect_replay_same_result(tmp_path):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain.chat_turns = None
    brain.orchestrator.handle_command_stream = AsyncMock(return_value="stream fixture")
    request = frame(reply_mode="stream")
    try:
        with _node_client(brain) as client:
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "old-node-alias", "phone")
                ws.send_json(request)
                final = receipt(ws)[-1]["payload"]
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "new-node-alias", "phone")
                ws.send_json(frame(request["msg_id"], reply_mode="final", reply_to="new-hint"))
                replay = receipt(ws)
                assert replay[-1]["payload"]["turn_id"] == final["turn_id"]
                assert replay[-1]["payload"]["final_text"] == "stream fixture"
                assert replay[-1]["payload"]["replayed"] is True
                brain.orchestrator.handle_command_stream.assert_awaited_once()
                brain.orchestrator.handle_command.assert_not_awaited()
    finally:
        asyncio.run(store.aclose())


def test_opt_in_text_command_uses_same_tracked_prelude_once_and_primary_sid(tmp_path):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain.chat_turns = None
    brain.primary_session_id = "shared"
    owner = brain.sessions["shared"] = object()
    brain.orchestrator.handle_command_stream = AsyncMock(return_value="text fixture")
    request = {"type": "text_command", "msg_id": str(uuid4()), "payload": {
        "turn_contract_version": 1, "text": "text fixture", "context": {"paired_device_id": "forged"},
    }}
    try:
        with _node_client(brain) as client:
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "phone", "phone")
                ws.send_json(request)
                result = receipt(ws)
                assert [item["type"] for item in result] == ["chat_turn_accepted", "chat_turn_terminal"]
                assert result[-1]["payload"]["processing_outcome"] == "completed"
                assert result[-1]["payload"]["session_id"] == "shared"
                ctx = brain.orchestrator.handle_command_stream.call_args.kwargs["context"]
                assert "paired_device_id" not in ctx and ctx["source_node"] == "phone"
                assert brain.sessions["shared"] is owner
                ws.send_json(request)
                assert receipt(ws)[-1]["payload"]["replayed"] is True
                brain.orchestrator.handle_command_stream.assert_awaited_once()
                assert len(store.working_get("shared")) == 1
    finally:
        asyncio.run(store.aclose())


def test_owner_capacity_bounds_queued_turns_but_allows_exact_duplicate(tmp_path):
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain.chat_turns = None
    release = asyncio.Event()

    async def inert(**kwargs):
        await release.wait()
        return {"text": "bounded fixture"}

    brain.orchestrator.handle_command = AsyncMock(side_effect=inert)
    # Existing per-session bound is eight; the same owner has a total of
    # sixteen across independent session lanes.
    requests = [frame(session_id="shared" if index < 8 else "second") for index in range(16)]
    try:
        with _node_client(brain) as client:
            ready(brain)
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, "phone", "phone")
                for request in requests:
                    ws.send_json(request)
                    assert ws.receive_json()["type"] == "chat_turn_accepted"
                ws.send_json(frame())
                assert ws.receive_json()["payload"]["name"] == "chat_turn_quota"
                ws.send_json(requests[0])
                duplicate = ws.receive_json()
                assert duplicate["type"] == "chat_turn_accepted" and duplicate["payload"]["replayed"] is True
                assert brain.orchestrator.handle_command.await_count == 2
                assert sqlite3.connect(store.db_path).execute("SELECT COUNT(*) FROM chat_turn_receipts").fetchone()[0] == 16
                ws.portal.call(release.set)
                terminal_ids = set()
                # Independent lanes may publish their legacy replies before
                # another lane's durable terminal commit finishes.
                for _ in range(32):
                    item = ws.receive_json()
                    if item["type"] == "chat_turn_terminal":
                        assert item["payload"]["processing_outcome"] == "completed"
                        terminal_ids.add(item["payload"]["request_id"])
                assert terminal_ids == {request["msg_id"] for request in requests}
        assert brain.orchestrator.handle_command.await_count == 16
        assert brain._phone_intake_locks == {}
    finally:
        asyncio.run(store.aclose())


@pytest.mark.parametrize("credential_kind", ["token", "phone_bearer"])
def test_real_pairing_credentials_bind_receipt_and_reconnect_without_execution(tmp_path, credential_kind):
    from security.device_pairing import DevicePairingStore
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    pairing = DevicePairingStore(db_path=str(tmp_path / "pairing.db"))
    issued = pairing.pair_device("inert phone", kind="browser_node_v2")
    pairing.verify_device = MagicMock(wraps=pairing.verify_device)
    pairing.verify_phone_bearer = MagicMock(wraps=pairing.verify_phone_bearer)
    brain.chat_turns = None
    brain.orchestrator.handle_command = AsyncMock(return_value={"text": "real pairing fixture"})
    message = frame()
    try:
        with _node_client(brain) as client:
            brain.device_pairing_store = pairing
            headers = {"authorization": "Bearer " + issued[credential_kind]}
            with client.websocket_connect("/v1/node", headers=headers) as ws:
                _register_node(ws, "first-alias", "phone")
                ws.send_json(message)
                final = receipt(ws)[-1]["payload"]
                assert final["processing_outcome"] == "completed"
            with client.websocket_connect("/v1/node", headers=headers) as ws:
                _register_node(ws, "replacement-alias", "phone")
                ws.send_json(message)
                replay = receipt(ws)[-1]["payload"]
                assert replay["turn_id"] == final["turn_id"] and replay["replayed"] is True
                assert issued["device_id"] not in json.dumps(replay)
                brain.orchestrator.handle_command.assert_awaited_once()
        # Full secret verification occurs at the two connection admissions;
        # retained dispatch/publication guards do not hash or renew the token.
        assert pairing.verify_device.call_count == 2
        assert pairing.verify_phone_bearer.call_count == (2 if credential_kind == "phone_bearer" else 0)
        binding = json.loads(sqlite3.connect(store.db_path).execute("SELECT source_principal_json FROM chat_turn_receipts").fetchone()[0])
        assert binding["device_id"] == issued["device_id"]
    finally:
        asyncio.run(store.aclose())


@pytest.mark.parametrize("invalidation", ["revocation", "rotation", "expiry"])
def test_real_revocation_blocks_actual_tool_runner_after_delayed_collaborator(tmp_path, invalidation):
    from agents.tool_runner import ToolRunner
    from security.agent_turn_lease import AgentTurnRevoked
    from security.device_pairing import DevicePairingStore
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    pairing = DevicePairingStore(db_path=str(tmp_path / "pairing.db"))
    issued = pairing.pair_device("inert phone", kind="browser_node_v2")
    brain.chat_turns = None
    runner = ToolRunner.__new__(ToolRunner)
    runner._orch = brain.orchestrator
    runner._native_agent_dispatch_lease = None
    runner._resolve_surface_for_session = lambda _: "brain_host"
    runner._record_tool_invocation = MagicMock()
    runner._turn_id_for = lambda _: ""
    runner._execute_tool_call_inner = AsyncMock(return_value={"success": True})
    entered, refused = threading.Event(), threading.Event()
    release = asyncio.Event()

    async def delayed_collaborator(**kwargs):
        entered.set()
        await release.wait()
        with pytest.raises(AgentTurnRevoked):
            await runner.execute_tool_call("shared", {"id": "inert-one", "name": "fixture__inert", "args": {}}, [])
        refused.set()
        return {"text": "stale result"}

    brain.orchestrator.handle_command = AsyncMock(side_effect=delayed_collaborator)
    try:
        with _node_client(brain) as client:
            brain.device_pairing_store = pairing
            with client.websocket_connect("/v1/node", headers={"authorization": "Bearer " + issued["phone_bearer"]}) as ws:
                _register_node(ws, "phone", "phone")
                ws.send_json(frame())
                accepted = ws.receive_json()["payload"]
                assert entered.wait(2)
                if invalidation == "revocation":
                    assert pairing.revoke_device(issued["device_id"])
                elif invalidation == "rotation":
                    assert pairing.rotate_phone_bearer(issued["device_id"])
                else:
                    conn = pairing._conn()
                    try:
                        conn.execute("UPDATE device_credentials SET expires_at=1 WHERE device_id=?", (issued["device_id"],))
                        conn.commit()
                    finally:
                        conn.close()
                ws.portal.call(release.set)
                assert refused.wait(2)
                ws.send_json({"type": "totally_bogus", "payload": {}})
                assert ws.receive_json()["type"] == "error"
            runner._execute_tool_call_inner.assert_not_awaited()
            row = json.loads(sqlite3.connect(store.db_path).execute("SELECT receipt_json FROM chat_turn_receipts WHERE turn_id=?", (accepted["turn_id"],)).fetchone()[0])
            assert row["processing_outcome"] == "cancelled" and row["action_outcome"] != "success"
    finally:
        asyncio.run(store.aclose())


def test_new_request_after_real_revocation_has_typed_refusal_then_close(tmp_path):
    from security.device_pairing import DevicePairingStore
    from starlette.websockets import WebSocketDisconnect
    brain = _state()
    store = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    pairing = DevicePairingStore(db_path=str(tmp_path / "pairing.db"))
    issued = pairing.pair_device("inert phone", kind="browser_node_v2")
    brain.chat_turns = None
    try:
        with _node_client(brain) as client:
            brain.device_pairing_store = pairing
            with client.websocket_connect("/v1/node", headers={"authorization": "Bearer " + issued["phone_bearer"]}) as ws:
                _register_node(ws, "phone", "phone")
                assert pairing.revoke_device(issued["device_id"])
                ws.send_json(frame())
                error = ws.receive_json()
                assert error["type"] == "error" and error["payload"]["name"] == "chat_turn_device_authority_unavailable"
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_json()
                assert closed.value.code == 4003
                brain.orchestrator.handle_command.assert_not_awaited()
                assert sqlite3.connect(store.db_path).execute("SELECT COUNT(*) FROM chat_turn_receipts").fetchone()[0] == 0
    finally:
        asyncio.run(store.aclose())
