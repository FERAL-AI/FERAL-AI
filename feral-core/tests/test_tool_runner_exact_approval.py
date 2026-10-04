"""One reviewed workflow action must not grant future changed actions."""
import pytest

from security.agent_turn_lease import AgentTurnLease, AgentTurnRevoked
from tests.test_taskflow_dispatch_policy import wired as _wired, create, review

wired = _wired


def ticket(orch):
    pending = orch.tool_runner.enforce_safety("notes_memory__save_note", {"value": "reviewed"},
                                             session_id="owner", surface="taskflow")
    accepted = orch.tool_runner.approve_pending(pending["request_id"], session_id="owner", exact_once=True)
    return accepted["approval"]


async def test_exact_receipt_is_one_use_and_never_standing(wired):
    _runtime, orch, seen = wired
    approval = ticket(orch)
    call = {"id": "fixture-call", "name": "notes_memory__save_note", "args": {"value": "reviewed"}}
    first = await orch.tool_runner.execute_tool_call_for_llm("owner", call, [], approval=approval)
    assert first["success"] is True and len(seen) == 1
    repeated = await orch.tool_runner.execute_tool_call_for_llm("owner", call, [], approval=approval)
    assert repeated["error_code"] == "invalid_approval" and len(seen) == 1
    new = await orch.tool_runner.execute_tool_call_for_llm("owner", call, [])
    assert new["status"] == "pending_approval" and len(seen) == 1
    assert orch.tool_runner._approval_mgr.check_approval(call["name"], "owner")[0] is False


@pytest.mark.parametrize("change", ["session", "args", "tool", "json"])
async def test_model_arguments_and_changed_terms_cannot_use_exact_receipt(wired, change):
    _runtime, orch, seen = wired
    approval = ticket(orch)
    call = {"name": "notes_memory__save_note", "args": {"value": "reviewed"}}
    session = "owner"
    if change == "session":
        session = "other"
    elif change == "args":
        call["args"] = {"value": "changed"}
    elif change == "tool":
        call["name"] = "notes_memory__search_notes"
    else:
        approval = {"session_id": "owner", "tool_name": call["name"], "args": call["args"]}
    result = await orch.tool_runner.execute_tool_call_for_llm(session, call, [], approval=approval)
    assert result["error_code"] == "invalid_approval" and seen == []


@pytest.mark.parametrize("gate", ["plan", "deny", "lease"])
async def test_one_call_receipt_never_bypasses_current_policy(wired, gate):
    _runtime, orch, seen = wired
    approval = ticket(orch)
    call = {"name": "notes_memory__save_note", "args": {"value": "reviewed"}}
    if gate == "plan":
        orch.tool_runner.plan_mode.enter("owner")
    elif gate == "deny":
        orch.skills.skills["notes_memory"].endpoints[1].safety_tier = "deny"
    else:
        from types import SimpleNamespace
        state = SimpleNamespace(_native_agent_turn_generation=1, _native_bootstrap_required=True,
                                orchestrator=orch, memory=object())
        orch.tool_runner._native_agent_dispatch_lease = AgentTurnLease(state, 0)
    if gate == "lease":
        with pytest.raises(AgentTurnRevoked):
            await orch.tool_runner.execute_tool_call_for_llm("owner", call, [], approval=approval)
    else:
        result = await orch.tool_runner.execute_tool_call_for_llm("owner", call, [], approval=approval)
        assert result.get("status") == "PermissionOutcome::Deny" or result.get("error_code") == "plan_mode_blocked"
    assert seen == []


async def test_second_workflow_with_changed_terms_requires_its_own_review(wired):
    runtime, orch, seen = wired
    _flow, pending = await review(wired)
    await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    next_flow = create(runtime, args={"value": "different material terms"})
    await runtime._run_flow(next_flow["id"])
    assert runtime.get_flow(next_flow["id"])["status"] == "waiting"
    assert len(seen) == 1
    assert orch.tool_runner.list_pending()[0]["args"] == {"value": "different material terms"}


async def test_central_dispatch_checkpoint_rechecks_pause_after_validation(wired, monkeypatch):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    validator = orch.tool_runner._get_dispatch_validator()
    original = validator.validate

    def pause_during_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        runtime._supervisor.paused = True
        return result

    monkeypatch.setattr(validator, "validate", pause_during_validation)
    with pytest.raises(RuntimeError, match="no longer active"):
        await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    assert seen == []
    assert runtime.get_flow(flow["id"])["steps"][0]["status"] == "outcome_unknown"
