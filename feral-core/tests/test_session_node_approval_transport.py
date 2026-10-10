"""Actual BrainState outbound boundary with inert sockets; no phone/network."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from api.state import BrainState
from agents.tool_runner import ToolRunner
from agents.orchestrator import Orchestrator
from models.protocol import (
    ApprovalRequestPayload,
    ApprovalResolvedPayload,
    FeralMessage,
    HUP_VERSION,
)


def state_with_nodes():
    state = BrainState.__new__(BrainState)
    state.daemons = {
        "phone": SimpleNamespace(send_json=AsyncMock()),
        "glasses": SimpleNamespace(send_json=AsyncMock()),
        "foreign": SimpleNamespace(send_json=AsyncMock()),
    }
    state._daemon_session_bindings = {
        "phone": {"origin-chat"},
        "glasses": {"origin-chat"},
        "foreign": {"other-chat"},
        "disconnected": {"origin-chat"},
    }
    return state


@pytest.mark.parametrize("kind", ["approval_request", "approval_resolved"])
async def test_actual_push_queues_valid_hup_approval_only_for_matching_live_nodes(kind):
    state = state_with_nodes()
    payload = {"request_id": "inert-review", "session_id": "origin-chat"}
    if kind == "approval_request":
        payload.update(
            tool_name="notes_memory__save_note",
            speak="Approve saving the note?",
            created_at=1,
            expires_at=301,
        )
    else:
        payload.update(outcome="approved", resolved_by="inert")
    frame = {"type": kind, "payload": payload}
    sent = await state.push_to_session_nodes("origin-chat", frame)
    assert sent == 2 and frame["payload"] == payload
    for node in ("phone", "glasses"):
        outgoing = state.daemons[node].send_json.await_args.args[0]
        assert outgoing["hup_version"] == HUP_VERSION and outgoing["ts"] >= 0
        parsed = FeralMessage.model_validate(outgoing)
        UUID(parsed.msg_id)
        model = (
            ApprovalRequestPayload
            if kind == "approval_request"
            else ApprovalResolvedPayload
        )
        assert model.model_validate(parsed.payload).request_id == "inert-review"
    state.daemons["foreign"].send_json.assert_not_awaited()


async def test_actual_push_failure_counts_only_accepted_socket_send():
    state = state_with_nodes()
    state.daemons["phone"].send_json.side_effect = RuntimeError("inert socket failure")
    assert (
        await state.push_to_session_nodes(
            "origin-chat",
            {"type": "approval_resolved", "payload": {"request_id": "inert"}},
        )
        == 1
    )
    state.daemons["glasses"].send_json.assert_awaited_once()
    state.daemons["foreign"].send_json.assert_not_awaited()


async def test_socket_removed_after_inventory_is_not_counted():
    state = state_with_nodes()
    actual_inventory = state.nodes_for_session

    def inventory(session):
        nodes = actual_inventory(session)
        state.daemons.pop("phone")
        return nodes

    state.nodes_for_session = inventory
    assert (
        await state.push_to_session_nodes(
            "origin-chat",
            {"type": "approval_resolved", "payload": {"request_id": "inert"}},
        )
        == 1
    )


async def test_replaced_socket_receives_frame_without_sending_to_old_peer():
    state = state_with_nodes()
    old = state.daemons["phone"]
    replacement = SimpleNamespace(send_json=AsyncMock())
    actual_inventory = state.nodes_for_session

    def inventory(session):
        nodes = actual_inventory(session)
        state.daemons["phone"] = replacement
        return nodes

    state.nodes_for_session = inventory
    assert (
        await state.push_to_session_nodes(
            "origin-chat",
            {"type": "approval_resolved", "payload": {"request_id": "inert"}},
        )
        == 2
    )
    old.send_json.assert_not_awaited()
    replacement.send_json.assert_awaited_once()


async def test_real_approval_publishers_reach_actual_state_dict_sender(monkeypatch):
    import api.state as state_module

    state = state_with_nodes()
    monkeypatch.setattr(state_module, "state", state)
    runner = ToolRunner.__new__(ToolRunner)
    await runner._push_approval_request(
        "origin-chat",
        {
            "request_id": "inert-review",
            "tool_name": "notes_memory__save_note",
            "args": {},
            "created_at": 1,
            "expires_at": 301,
        },
    )
    orch = Orchestrator.__new__(Orchestrator)
    await orch._push_approval_resolved(
        "origin-chat", "inert-review", "approved", "notes_memory__save_note", "inert"
    )
    for node in ("phone", "glasses"):
        sent = state.daemons[node].send_json.await_args_list
        assert [call.args[0]["type"] for call in sent] == [
            "approval_request",
            "approval_resolved",
        ]
        ApprovalRequestPayload.model_validate(sent[0].args[0]["payload"])
        ApprovalResolvedPayload.model_validate(sent[1].args[0]["payload"])
    state.daemons["foreign"].send_json.assert_not_awaited()


async def test_empty_or_foreign_session_has_no_acceptance_claim():
    state = state_with_nodes()
    frame = {"type": "approval_resolved", "payload": {"request_id": "inert"}}
    assert await state.push_to_session_nodes("", frame) == 0
    assert await state.push_to_session_nodes("missing-chat", frame) == 0
    for peer in state.daemons.values():
        peer.send_json.assert_not_awaited()


async def test_voice_sender_preserves_none_callback_contract():
    state = state_with_nodes()
    assert (
        await state._send_dict_to_node(
            "phone", {"type": "approval_resolved", "payload": {"request_id": "inert"}}
        )
        is None
    )
    assert (
        await state._send_dict_to_node(
            "missing", {"type": "approval_resolved", "payload": {"request_id": "inert"}}
        )
        is None
    )
    state.daemons["phone"].send_json.assert_awaited_once()
