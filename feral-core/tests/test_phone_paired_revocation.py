"""Legacy paired ingress retains credential revocation at real tool dispatch."""
import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.tool_runner import ToolRunner
from security.agent_turn_lease import AgentTurnRevoked
from security.device_pairing import DevicePairingStore
from tests.test_hup_protocol import _node_client, _register_node
from tests.test_phone_chat_responsive_intake import _state

pytestmark = [pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]


@pytest.mark.parametrize("entry", ["chat_request", "text_command"])
@pytest.mark.parametrize("invalidation", ["revoke", "rotate", "expire", "store", "cancel"])
def test_real_legacy_pair_revocation_fences_delayed_tool_and_keeps_operator_work(tmp_path, entry, invalidation):
    brain = _state()
    pairing = DevicePairingStore(db_path=str(tmp_path / "pair.db"))
    issued = pairing.pair_device("inert phone", kind="browser_node_v2")
    brain.primary_session_id = "shared"
    runner = ToolRunner.__new__(ToolRunner)
    runner._orch = brain.orchestrator
    runner._native_agent_dispatch_lease = None
    runner._resolve_surface_for_session = lambda _: "brain_host"
    runner._record_tool_invocation = MagicMock()
    runner._turn_id_for = lambda _: ""
    runner._execute_tool_call_inner = AsyncMock(return_value={"success": True, "data": {"inert": True}})
    entered, denied = threading.Event(), threading.Event()
    release = asyncio.Event()

    async def collaborator(**kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # A collaborator swallowing cancellation still cannot clear its
            # inherited, server-owned connection/credential dispatch fence.
            pass
        with pytest.raises(AgentTurnRevoked):
            await runner.execute_tool_call(kwargs["session_id"],
                {"id": "legacy-inert", "name": "fixture__inert", "args": {}}, [])
        denied.set()
        return "stale result"

    brain.orchestrator.handle_command = AsyncMock(side_effect=collaborator)
    brain.orchestrator.handle_command_stream = AsyncMock(side_effect=collaborator)
    with _node_client(brain) as client:
        brain.device_pairing_store = pairing
        with client.websocket_connect("/v1/node", headers={"authorization": "Bearer " + issued["phone_bearer"]}) as ws:
            _register_node(ws, "phone", "phone")
            payload = {"text": "inert legacy turn"}
            if entry == "chat_request":
                payload.update(session_id="shared", device_target="brain", reply_mode="final")
            ws.send_json({"type": entry, "payload": payload})
            assert entered.wait(2)
            intake = brain.daemons["phone"]._feral_phone_chat_intake
            if invalidation == "revoke":
                assert pairing.revoke_device(issued["device_id"])
            elif invalidation == "rotate":
                assert pairing.rotate_phone_bearer(issued["device_id"])
            elif invalidation == "expire":
                conn = pairing._conn()
                try:
                    conn.execute("UPDATE device_credentials SET expires_at=1 WHERE device_id=?", (issued["device_id"],))
                    conn.commit()
                finally:
                    conn.close()
            elif invalidation == "store":
                brain.device_pairing_store = DevicePairingStore(db_path=str(tmp_path / "replacement.db"))
            else:
                ws.portal.call(intake.stop)
            ws.portal.call(release.set)
            assert denied.wait(2)
            runner._execute_tool_call_inner.assert_not_awaited()
            # An unrelated operator task does not inherit the phone fence.
            async def operator_read():
                return await runner.execute_tool_call("operator-session",
                    {"id": "operator-inert", "name": "fixture__inert", "args": {}}, [])
            result = ws.portal.call(operator_read)
            assert result["success"] is True
            runner._execute_tool_call_inner.assert_awaited_once()
    assert brain._phone_intake_locks == {}


def test_optional_ingress_predicate_refuses_before_legacy_prelude_and_preserves_default():
    from types import SimpleNamespace
    from api.phone_chat_intake import PhoneChatIntake

    async def exercise():
        socket = object()
        brain = SimpleNamespace(orchestrator=object(), memory=object(), daemons={"phone": socket},
                                register_background_task=lambda task: task)
        intake = PhoneChatIntake(brain, socket, lambda: brain)
        intake.node_id = "phone"
        operation = AsyncMock()
        assert intake.submit("shared", operation, admission_current=lambda: False)
        await asyncio.gather(*tuple(intake.tasks), return_exceptions=True)
        operation.assert_not_awaited()
        assert intake.submit("shared", operation)
        await asyncio.gather(*tuple(intake.tasks), return_exceptions=True)
        operation.assert_awaited_once()
        await intake.drain()
        assert brain._phone_intake_locks == {}

    asyncio.run(exercise())
