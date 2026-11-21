"""Private exact reviews at real CTM/TaskFlow/central dispatcher boundaries."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agents.chat_turns import observe_tool_result
from agents.orchestrator import Orchestrator
from api import state as state_module
from security.approval_ingress import begin_node_approval_ingress, end_node_approval_ingress
from tests import test_phone_taskflow_origin as phone, test_taskflow_model_steps as model

wired_origin = phone.wired_origin
rig = phone.rig
wired = model.wired
pytestmark = pytest.mark.no_auto_feral_home


async def initial_review(wired_origin, source):
    brain, manager, orch = wired_origin
    async def propose():
        pending = await orch.tool_runner.execute_tool_call_for_llm("thread-A",
            {"id": "initial-action", "name": "background_task__start", "args": {"goal": "inert reviewed work"}},
            [], surface="http_api")
        observe_tool_result("thread-A", pending)
        return pending
    results, receipt = await phone.phone_turn(brain, manager, source, propose, sid="thread-A")
    pending = results[0]
    assert pending["status"] == "pending_approval"
    descriptor = orch.tool_runner.phone_review_descriptor(pending, source_principal=source)
    assert descriptor is not None
    return pending, descriptor, receipt


async def answer(orch, pending, descriptor, source, *, approved=True, session_id=None, card=None):
    return await orch.resolve_tool_approval_request(pending["request_id"], approved=approved,
        session_id=session_id if session_id is not None else descriptor["origin_session_id"],
        source_principal=source, task_review=card if card is not None else descriptor["task_review"])


async def test_initial_tracked_review_private_binding_correct_caller_single_flow(wired_origin, tmp_path):
    brain, _, orch = wired_origin
    source, _ = phone.paired(tmp_path)
    pending, descriptor, receipt = await initial_review(wired_origin, source)
    card = descriptor["task_review"]
    assert card["kind"] == "task_start" and card["turn_id"] == receipt["turn_id"]
    assert source.device_id not in json.dumps(card)
    token = begin_node_approval_ingress(source)
    try:
        free_text = await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="thread-A")
        assert free_text["status"] == "approval_device_authority_unavailable"
        result = await answer(orch, pending, descriptor, source)
    finally:
        end_node_approval_ingress(token)
    assert result["status"] == "approved" and result["session_id"] == "thread-A"
    assert result["task_review"] == card and len(brain.taskflows.list_flows()) == 1
    assert brain.taskflows.flow_matches_source(result["result"]["data"]["flow_id"], source)
    assert pending["request_id"] not in orch.tool_runner._pending_phone_reviews
    assert (await answer(orch, pending, descriptor, source))["status"] == "approval_device_authority_unavailable"
    assert len(brain.taskflows.list_flows()) == 1


@pytest.mark.parametrize("change", ["foreign", "raw_principal", "origin", "extra", "digest", "bool_version"])
async def test_initial_foreign_spoof_or_changed_card_cannot_consume_review(wired_origin, tmp_path, change):
    brain, _, orch = wired_origin
    source, _ = phone.paired(tmp_path)
    foreign, _ = phone.paired(tmp_path, "foreign")
    pending, descriptor, _ = await initial_review(wired_origin, source)
    caller = foreign if change == "foreign" else {"device_id": source.device_id} if change == "raw_principal" else source
    card = copy.deepcopy(descriptor["task_review"])
    sid = "forged" if change == "origin" else None
    if change == "extra":
        card["owner_verified"] = True
    if change == "digest":
        card["terms_digest"] = "0" * 64
    if change == "bool_version":
        card["version"] = True
    outcome = await answer(orch, pending, descriptor, caller, session_id=sid, card=card)
    assert outcome["status"] == "approval_device_authority_unavailable"
    assert brain.taskflows.list_flows() == [] and orch.tool_runner.get_pending(pending["request_id"])


async def test_origin_private_request_and_resolution_never_fall_back_to_broadcast(wired_origin, tmp_path, monkeypatch):
    brain, _, orch = wired_origin
    source, _ = phone.paired(tmp_path)
    pending, descriptor, _ = await initial_review(wired_origin, source)
    private, broadcast = AsyncMock(return_value=True), AsyncMock(return_value=9)
    monkeypatch.setattr(brain, "push_to_review_device", private, raising=False)
    monkeypatch.setattr(brain, "push_to_session_nodes", broadcast, raising=False)
    monkeypatch.setattr(state_module, "state", brain)
    orch._push_approval_resolved = Orchestrator._push_approval_resolved.__get__(orch)
    await orch.tool_runner._push_approval_request(pending["session_id"], pending)
    assert private.await_count == 1 and broadcast.await_count == 0
    target, message = private.await_args.args
    assert target == source.storage_binding() and message["payload"]["task_review"] == descriptor["task_review"]
    assert source.device_id not in json.dumps(message)
    assert (await answer(orch, pending, descriptor, source, approved=False))["status"] == "rejected"
    assert private.await_count == 2 and broadcast.await_count == 0
    assert private.await_args.args[1]["type"] == "approval_resolved"
    await orch.tool_runner._push_approval_request(pending["session_id"], pending)
    assert private.await_count == 2 and broadcast.await_count == 0  # Consumed private review has no fallback.


@pytest.mark.parametrize("change", ["args", "expiry", "revoked"])
async def test_changed_pending_or_revoked_source_invalidates_public_descriptor(wired_origin, tmp_path, change):
    brain, _, orch = wired_origin
    source, pairing = phone.paired(tmp_path)
    pending, descriptor, _ = await initial_review(wired_origin, source)
    stored = orch.tool_runner._pending_approvals[pending["request_id"]]
    if change == "args":
        stored["args"]["goal"] = "changed"
    if change == "expiry":
        stored["expires_at"] += 100
    if change == "revoked":
        pairing.revoke_device(source.device_id)
    assert orch.tool_runner.phone_review_descriptor(stored, source_principal=source) is None
    assert (await answer(orch, pending, descriptor, source))["status"] == "approval_device_authority_unavailable"
    assert brain.taskflows.list_flows() == []


def owned_goal(rt, source):
    from api.routes.taskflows import _start_task
    origin = {"contract_version": 1, "source": "tracked_chat_turn", "owner_verified": True,
              "session_id": "origin-A", "request_id": str(uuid4()), "turn_id": str(uuid4()),
              "tool_call_id": "launch-call", "surface": "http_api"}
    result = _start_task({"goal": "inert durable work"}, origin, lambda: None, rt, source)
    assert result["ok"]
    return result["flow_id"]


async def test_durable_model_review_exact_owner_bound_before_notify_and_continues(wired, tmp_path):
    rt, orch, calls = wired
    source, _ = phone.paired(tmp_path)
    flow_id = owned_goal(rt, source)
    published = []
    async def notify(session, tool, pending):
        descriptor = orch.tool_runner.phone_review_descriptor(pending, source_principal=source)
        assert descriptor and descriptor["origin_session_id"] == "origin-A"
        assert rt.get_flow(flow_id)["steps"][0]["result"]["task_review"] == descriptor["task_review"]
        published.append(descriptor)
    orch.tool_runner._notify_user_of_pending_approval = notify
    _, pending = await model.ask(rt, orch, flow_id)
    assert len(published) == 1 and calls == []
    descriptor = published[0]
    assert descriptor["task_review"]["kind"] == "taskflow_action"
    assert descriptor["task_review"]["action_id"] == "model-action-1"
    assert pending["session_id"] != "origin-A"
    rejected = await answer(orch, pending, descriptor, source, session_id=pending["session_id"])
    assert rejected["status"] == "approval_device_authority_unavailable" and calls == []
    result = await answer(orch, pending, descriptor, source)
    assert result["status"] == "approved" and result["session_id"] == "origin-A" and len(calls) == 1
    assert calls[0]["args"] == {"value": "exact terms"}
    await rt._run_flow(flow_id, single_step=True)
    assert len(calls) == 1 and rt.get_flow(flow_id)["status"] == "completed"


async def test_revocation_during_publication_prevents_central_model_dispatch(wired, tmp_path):
    rt, orch, calls = wired
    source, pairing = phone.paired(tmp_path)
    flow_id = owned_goal(rt, source)
    _, pending = await model.ask(rt, orch, flow_id)
    descriptor = orch.tool_runner.phone_review_descriptor(pending, source_principal=source)
    async def revoke_at_publication(*args, **kwargs):
        pairing.revoke_device(source.device_id)
    orch._push_approval_resolved = revoke_at_publication
    with pytest.raises(asyncio.CancelledError):
        await answer(orch, pending, descriptor, source)
    assert calls == [] and rt.get_flow(flow_id)["status"] != "completed"


@pytest.mark.parametrize("mutation", ["owner", "args", "cancel"])
async def test_postconsumption_change_prevents_central_dispatch(wired, tmp_path, mutation):
    rt, orch, calls = wired
    source, _ = phone.paired(tmp_path)
    foreign, _ = phone.paired(tmp_path, "foreign")
    flow_id = owned_goal(rt, source)
    _, pending = await model.ask(rt, orch, flow_id)
    descriptor = orch.tool_runner.phone_review_descriptor(pending, source_principal=source)
    async def changed_after_review(*args, **kwargs):
        if mutation == "owner":
            rt._conn.execute("UPDATE taskflows SET origin_principal_json=? WHERE id=?",
                (json.dumps(foreign.storage_binding(), sort_keys=True, separators=(",", ":")), flow_id))
            rt._conn.commit()
        elif mutation == "args":
            pending["args"]["value"] = "changed after review"
        else:
            rt.cancel_flow(flow_id)
    orch._push_approval_resolved = changed_after_review
    try:
        result = await answer(orch, pending, descriptor, source)
        assert result["result"].get("success") is not True
    except asyncio.CancelledError:
        pass
    assert calls == [] and rt.get_flow(flow_id)["status"] != "completed"


@pytest.mark.parametrize("receipt", [
    {"success": False, "error": "inert verified failure"},
    {"success": False, "outcome": "unknown", "error": "inert uncertain result"},
])
async def test_phone_approved_failure_or_uncertainty_does_not_advance(wired, tmp_path, receipt):
    rt, orch, _ = wired
    source, _ = phone.paired(tmp_path)
    flow_id = owned_goal(rt, source)
    _, pending = await model.ask(rt, orch, flow_id)
    descriptor = orch.tool_runner.phone_review_descriptor(pending, source_principal=source)
    orch.executor.execute = AsyncMock(return_value=receipt)
    result = await answer(orch, pending, descriptor, source)
    assert result["status"] == "approved"  # Approval receipt is distinct from execution outcome.
    assert result["result"]["success"] is False and orch.executor.execute.await_count == 1
    assert rt.get_flow(flow_id)["status"] != "completed"
    assert orch.tool_runner.get_pending(pending["request_id"]) is None


async def test_revocation_after_verified_effect_keeps_receipt_and_never_replays(wired, tmp_path):
    rt, orch, _ = wired
    source, pairing = phone.paired(tmp_path)
    flow_id = owned_goal(rt, source)
    _, pending = await model.ask(rt, orch, flow_id)
    descriptor = orch.tool_runner.phone_review_descriptor(pending, source_principal=source)
    effects = []
    async def execute_then_revoke(**kwargs):
        effects.append(kwargs)
        pairing.revoke_device(source.device_id)
        return {"success": True, "data": {"nonce": "verified-fixture"}, "tool_outcome_verified": True}
    orch.executor.execute = AsyncMock(side_effect=execute_then_revoke)
    with pytest.raises(asyncio.CancelledError):
        await answer(orch, pending, descriptor, source)
    step = rt.get_flow(flow_id)["steps"][0]
    assert step["result"]["reason"] == "model_continuation"
    assert step["result"]["model_actions"]["model-action-1"]["result"]["data"]["nonce"] == "verified-fixture"
    assert len(effects) == 1
    assert (await answer(orch, pending, descriptor, source))["status"] == "approval_device_authority_unavailable"
    await rt._run_flow(flow_id, single_step=True)
    assert rt.get_flow(flow_id)["status"] == "completed" and len(effects) == 1


async def test_foreground_or_legacy_review_never_becomes_device_owned(wired, tmp_path):
    rt, orch, calls = wired
    source, _ = phone.paired(tmp_path)
    pending = await model.action(orch, "ordinary-foreground")
    orch.tool_runner.bind_phone_review(pending)
    assert orch.tool_runner.phone_review_descriptor(pending, source_principal=source) is None
    spoof = {"version": 1, "request_id": pending["request_id"], "origin_session_id": "ordinary-foreground"}
    result = await orch.resolve_tool_approval_request(pending["request_id"], approved=True,
        session_id="ordinary-foreground", source_principal=source, task_review=spoof)
    assert result["status"] == "approval_device_authority_unavailable" and calls == []
    result = await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="ordinary-foreground")
    assert result["status"] == "approved" and len(calls) == 1
