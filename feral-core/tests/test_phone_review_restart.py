"""Stored private review renewal with real pairing/SQLite and inert model effects."""

import asyncio
import copy
import json
from uuid import uuid4

import pytest

from agents.chat_turns import TrackedTaskOriginGuard
from agents.taskflow import TaskFlowRuntime
from agents.tool_runner import ToolRunner
from api.routes.taskflows import _start_task
from security.approval_ingress import PairedDevicePrincipal
from security.exec_approvals import ApprovalManager
from tests import test_phone_taskflow_origin, test_taskflow_model_steps

wired = test_taskflow_model_steps.wired
paired = test_phone_taskflow_origin.paired
ask = test_taskflow_model_steps.ask
action = test_taskflow_model_steps.action


def phone_goal(rt, source):
    origin = {"contract_version": 1, "source": "tracked_chat_turn", "owner_verified": True,
              "session_id": "origin-A", "request_id": str(uuid4()), "turn_id": str(uuid4()),
              "tool_call_id": "phone-launch", "surface": "http_api"}
    guard = TrackedTaskOriginGuard(source.require_current, source)
    created = _start_task({"goal": "inert durable goal"}, origin, guard, rt, source)
    assert created["ok"] is True
    return created["flow_id"]


async def test_owned_review_checkpoint_private_and_source_filtered(wired, tmp_path):
    rt, orch, effects = wired
    source, _ = paired(tmp_path)
    foreign, _ = paired(tmp_path, "foreign")
    flow_id = phone_goal(rt, source)
    _, pending = await ask(rt, orch, flow_id)
    descriptor = orch.tool_runner.phone_review_descriptor(pending, source_principal=source)
    assert descriptor["source_binding"] == source.storage_binding()
    rows = rt.list_device_review_checkpoints("origin-A", source_principal=source)
    assert len(rows) == 1 and rows[0]["task_review"] == descriptor["task_review"]
    assert rows[0]["session_id"] == "origin-A" and rows[0]["approval_available"] is False
    assert rows[0]["review_renewal_required"] is True
    assert rt.list_device_review_checkpoints("origin-A", source_principal=foreign) == []
    assert rt.list_device_review_checkpoints("other", source_principal=source) == []
    assert source.device_id not in json.dumps(rows)
    assert source.device_id not in json.dumps(rt.get_flow(flow_id))
    assert effects == []


@pytest.mark.parametrize("change", ["version_bool", "wrong_digest", "wrong_step", "extra_field", "origin", "foreign"])
async def test_explicit_renewal_rejects_changed_card_before_effect(wired, tmp_path, change):
    rt, orch, effects = wired
    source, _ = paired(tmp_path)
    flow_id = phone_goal(rt, source)
    _, pending = await ask(rt, orch, flow_id)
    card = copy.deepcopy(rt.get_flow(flow_id)["steps"][0]["result"]["task_review"])
    original = copy.deepcopy(card)
    origin = "origin-A"
    if change == "version_bool":
        card["version"] = True
    elif change == "wrong_digest":
        card["terms_digest"] = "0" * 64
    elif change == "wrong_step":
        card["step_id"] += 1
    elif change == "extra_field":
        card["owner"] = source.device_id
    elif change == "origin":
        origin = "other"
    else:
        source, _ = paired(tmp_path, "foreign")
    result = await rt.renew_device_model_review(flow_id, origin_session_id=origin,
                                               task_review=card, source_principal=source)
    assert result["review_renewal"]["status"] == "refused" and effects == []
    assert rt.get_flow(flow_id)["steps"][0]["result"]["task_review"] == original
    assert orch.tool_runner.get_pending(pending["request_id"]) is not None


async def test_restart_fresh_same_device_renews_once_and_old_card_cannot_dispatch(wired, tmp_path):
    rt, orch, effects = wired
    source, pair_store = paired(tmp_path)
    flow_id = phone_goal(rt, source)
    _, old_pending = await ask(rt, orch, flow_id)
    old_card = copy.deepcopy(rt.get_flow(flow_id)["steps"][0]["result"]["task_review"])
    await rt.stop()
    reopened = TaskFlowRuntime(db_path=str(tmp_path / "flows.db"), skill_registry=orch.skills, orchestrator=orch)
    reopened._supervisor = rt._supervisor
    orch.taskflows = reopened
    orch.tool_runner = ToolRunner(orch, approval_manager=ApprovalManager())
    issued = pair_store.rotate_phone_bearer(source.device_id)
    assert pair_store.verify_phone_bearer(issued["phone_bearer"]) == source.device_id
    fresh = PairedDevicePrincipal(source.device_id, lambda: pair_store.admitted_credential_current(
        device_id=source.device_id, credential=issued["phone_bearer"], bearer_kind="phone_bearer"))
    with pytest.raises(asyncio.CancelledError):
        source.require_current()
    try:
        reopened._recover_after_restart()
        assert orch.tool_runner.list_pending() == []
        rows = reopened.list_device_review_checkpoints("origin-A", source_principal=fresh)
        assert rows[0]["task_review"] == old_card
        renewed = await reopened.renew_device_model_review(flow_id, origin_session_id="origin-A",
                                                          task_review=old_card, source_principal=fresh)
        assert renewed["review_renewal"]["status"] == "waiting" and effects == []
        new_card = renewed["review_renewal"]["task_review"]
        assert new_card["request_id"] != old_card["request_id"]
        assert new_card["terms_digest"] != old_card["terms_digest"]
        if reopened._review_publications:
            await asyncio.gather(*reopened._review_publications)
        assert len(orch.tool_runner.list_pending()) == 1
        old = await orch.resolve_tool_approval_request(old_card["request_id"], approved=True,
            session_id="origin-A", source_principal=fresh, task_review=old_card)
        assert old["status"] != "approved" and effects == []
        accepted = await orch.resolve_tool_approval_request(new_card["request_id"], approved=True,
            session_id="origin-A", source_principal=fresh, task_review=new_card)
        assert accepted["status"] == "approved" and len(effects) == 1
        duplicate = await orch.resolve_tool_approval_request(new_card["request_id"], approved=True,
            session_id="origin-A", source_principal=fresh, task_review=new_card)
        assert duplicate["status"] != "approved" and len(effects) == 1

        async def continuation(session, text, context=None):
            result = await action(orch, session)
            assert result["data"]["nonce"] == "verified-fixture"
            return "Grounded result"

        orch._handle_command_impl = continuation
        await reopened._run_flow(flow_id)
        assert reopened.read_origin_receipt("origin-A", flow_id)["processing_outcome"] == "completed"
        assert len(effects) == 1
    finally:
        await reopened.stop()
        reopened._conn.close()


@pytest.mark.parametrize("status", ["cancelled", "outcome_unknown"])
async def test_cancelled_or_uncertain_checkpoint_cannot_renew(wired, tmp_path, status):
    rt, orch, effects = wired
    source, _ = paired(tmp_path)
    flow_id = phone_goal(rt, source)
    await ask(rt, orch, flow_id)
    card = rt.get_flow(flow_id)["steps"][0]["result"]["task_review"]
    if status == "cancelled":
        rt.cancel_flow(flow_id)
    else:
        rt._conn.execute("UPDATE taskflow_steps SET status='outcome_unknown' WHERE flow_id=?", (flow_id,))
        rt._conn.commit()
    assert rt.list_device_review_checkpoints("origin-A", source_principal=source) == []
    result = await rt.renew_device_model_review(flow_id, origin_session_id="origin-A",
                                               task_review=card, source_principal=source)
    assert result["review_renewal"]["status"] == "refused" and effects == []


async def test_revocation_during_renewal_commit_rolls_back_and_discards_new_pending(wired, tmp_path):
    rt, orch, effects = wired
    source, _ = paired(tmp_path)
    flow_id = phone_goal(rt, source)
    _, pending = await ask(rt, orch, flow_id)
    original = rt.get_flow(flow_id)
    orch.tool_runner.deny_pending(pending["request_id"], session_id=pending["session_id"])
    fenced = PairedDevicePrincipal(source.device_id,
        lambda: source.is_current() is True and not rt._conn.in_transaction)
    with pytest.raises(asyncio.CancelledError):
        await rt.renew_device_model_review(flow_id, origin_session_id="origin-A",
            task_review=original["steps"][0]["result"]["task_review"], source_principal=fenced)
    assert rt.get_flow(flow_id)["steps"][0]["result"] == original["steps"][0]["result"]
    assert orch.tool_runner.list_pending() == [] and effects == []


async def test_legacy_flow_does_not_acquire_phone_review_from_session_membership(wired, tmp_path):
    rt, orch, effects = wired
    source, _ = paired(tmp_path)
    flow_id = test_taskflow_model_steps.goal(rt)
    await ask(rt, orch, flow_id)
    assert rt.list_device_review_checkpoints("origin-A", source_principal=source) == []
    assert "task_review" not in rt.get_flow(flow_id)["steps"][0]["result"]
    assert effects == []
