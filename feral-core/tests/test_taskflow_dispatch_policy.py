"""Persistent workflow dispatch through actual ToolRunner/approval methods.

Only the executor and UI notifications are deterministic doubles. No account,
network effect or external engine is used; SQLite and policy dispatch are real.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agents.orchestrator import Orchestrator
from agents.taskflow import TaskFlowRuntime
from agents.tool_runner import ToolRunner
from models.skill_manifest import BrandProfile, EndpointParam, SkillEndpoint, SkillManifest
from security.exec_approvals import ApprovalManager
from skills.call_context import current_context
from skills.registry import SkillRegistry


@pytest_asyncio.fixture
async def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_AUTONOMY", "hybrid")
    monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "on")
    registry = SkillRegistry()
    registry.register(SkillManifest(
        skill_id="notes_memory", brand=BrandProfile(name="Workflow probe"),
        description="test-only controlled dispatch",
        endpoints=[SkillEndpoint(id=name, method="PYTHON", url="", description=name,
                                 safety_tier=tier, read_only_hint=read_only,
                                 params=[EndpointParam(name="value", type="string", required=True)])
                   for name, tier, read_only in (
                       ("search_notes", "safe", True), ("save_note", "confirm", False),
                       ("list_recent", "deny", False),
                   )],
    ))
    orch = Orchestrator.__new__(Orchestrator)
    orch.skills = registry
    orch._mcp_client = None
    orch._session_surfaces = {}
    orch._active_turns = {}
    orch.memory = None
    seen = []

    async def execute(**kwargs):
        seen.append((current_context(), kwargs))
        return {"success": True, "data": {"message": "fixture operation finished"},
                "tool_outcome_verified": True}

    orch.executor = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    orch.tool_runner = ToolRunner(orch, approval_manager=ApprovalManager())
    orch._emit_tool_start = AsyncMock()
    orch._emit_tool_result = AsyncMock()
    orch._try_genui_for_result = AsyncMock()
    orch._send_text = AsyncMock()
    orch._push_approval_resolved = AsyncMock()
    orch._summarize_action_result = lambda *_: "fixture dispatch result"
    orch.tool_runner._notify_user_of_pending_approval = AsyncMock()
    runtime = TaskFlowRuntime(db_path=str(tmp_path / "flows.db"),
                              skill_registry=registry, orchestrator=orch)
    runtime._supervisor = SimpleNamespace(paused=False)
    orch.taskflows = runtime
    yield runtime, orch, seen
    await runtime.stop()
    runtime._conn.close()


def create(runtime, endpoint="save_note", *, session="owner", args=None, following=True):
    steps = [{"type": "skill.invoke", "skill_id": "notes_memory",
              "endpoint": endpoint, "args": {"value": "exact terms"} if args is None else args}]
    if following:
        steps.append({"type": "noop"})
    return runtime.create_flow(session_id=session, title="isolated policy fixture", steps=steps)


async def review(wired):
    runtime, orch, seen = wired
    flow = create(runtime)
    await runtime._run_flow(flow["id"])
    row = runtime.get_flow(flow["id"])
    assert row["status"] == "waiting" and row["wait_until"] is None
    assert row["steps"][0]["status"] == "waiting"
    assert seen == []
    pending = orch.tool_runner.list_pending()[0]
    return flow, pending


async def test_safe_invocation_uses_full_dispatcher_identity_and_validation(wired):
    runtime, orch, seen = wired
    flow = create(runtime, "search_notes")
    await runtime._run_flow(flow["id"])
    assert runtime.get_flow(flow["id"])["status"] == "completed"
    assert len(seen) == 1
    context, call = seen[0]
    assert context.session_id == "owner" and context.surface == "taskflow"
    assert context.call_id == f"taskflow:{flow['id']}:{flow['steps'][0]['id']}"
    assert call["args"] == {"value": "exact terms"}
    bad = create(runtime, "search_notes", args={})
    await runtime._run_flow(bad["id"])
    assert runtime.get_flow(bad["id"])["status"] == "failed"
    assert len(seen) == 1


async def test_confirm_executes_once_via_real_approval_and_continues(wired):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    await runtime._run_flow(flow["id"])
    assert seen == []
    assert runtime.resume_flow(flow["id"])["status"] == "waiting"
    result = await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    assert result["status"] == "approved"
    assert len(seen) == 1
    assert seen[0][0].surface == "taskflow"
    row = runtime.get_flow(flow["id"])
    assert row["steps"][0]["status"] == "completed"
    assert row["current_step"] == 1
    assert row["steps"][0]["result"]["tool_outcome_verified"] is True
    await runtime._run_flow(flow["id"])
    assert runtime.get_flow(flow["id"])["status"] == "completed"
    again = await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    assert again["status"] == "not_found" and len(seen) == 1
    assert runtime.resume_flow(flow["id"])["status"] == "completed"


async def test_reject_and_wrong_session_never_execute(wired):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    wrong = await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="other")
    assert wrong["status"] == "session_mismatch"
    assert runtime.get_flow(flow["id"])["status"] == "waiting"
    result = await orch.resolve_tool_approval_request(pending["request_id"], approved=False, session_id="owner")
    assert result["status"] == "rejected" and seen == []
    assert runtime.get_flow(flow["id"])["status"] == "failed"


@pytest.mark.parametrize("change", ["cancel", "pause", "plan", "terms", "expiry"])
async def test_stale_or_revoked_approval_does_not_dispatch(wired, change):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    if change == "cancel":
        runtime.cancel_flow(flow["id"])
    elif change == "pause":
        runtime._supervisor.paused = True
    elif change == "plan":
        orch.tool_runner.plan_mode.enter("owner")
    elif change == "terms":
        orch.tool_runner._pending_approvals[pending["request_id"]]["args"] = {"value": "changed terms"}
    else:
        orch.tool_runner._pending_approvals[pending["request_id"]]["expires_at"] = time.time() - 1
    result = await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    assert result["status"] in {"rejected", "not_found"}
    assert seen == []
    assert runtime.get_flow(flow["id"])["current_step"] == 0


@pytest.mark.parametrize("endpoint, gate", [("list_recent", "deny"), ("save_note", "pause"), ("save_note", "plan")])
async def test_initial_gate_denies_without_execution(wired, endpoint, gate):
    runtime, orch, seen = wired
    if gate == "pause":
        runtime._supervisor.paused = True
    elif gate == "plan":
        orch.tool_runner.plan_mode.enter("owner")
    flow = create(runtime, endpoint)
    await runtime._run_flow(flow["id"])
    assert runtime.get_flow(flow["id"])["status"] == "failed"
    assert seen == [] and orch.tool_runner.list_pending() == []


async def test_dispatcher_unavailable_fails_closed(wired):
    runtime, orch, seen = wired
    runtime._orchestrator = None
    flow = create(runtime, "search_notes")
    await runtime._run_flow(flow["id"])
    assert runtime.get_flow(flow["id"])["status"] == "failed"
    assert "dispatcher unavailable" in runtime.get_flow(flow["id"])["error"]
    assert seen == []


async def test_policy_exception_is_not_a_direct_skill_fallback(wired):
    runtime, orch, seen = wired
    orch.tool_runner.enforce_safety = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("policy unavailable"))
    flow = create(runtime)
    await runtime._run_flow(flow["id"])
    row = runtime.get_flow(flow["id"])
    assert row["status"] == "waiting" and row["steps"][0]["status"] == "outcome_unknown"
    assert seen == []


async def test_cancel_running_step_never_advances_or_dispatches_next(wired):
    runtime, orch, seen = wired
    entered = asyncio.Event()

    async def blocked(**kwargs):
        seen.append((current_context(), kwargs))
        entered.set()
        await asyncio.Event().wait()

    orch.executor.execute.side_effect = blocked
    flow = create(runtime, "search_notes")
    task = asyncio.create_task(runtime._run_flow(flow["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime.cancel_flow(flow["id"])
        await asyncio.wait_for(task, 5)
        row = runtime.get_flow(flow["id"])
        assert row["status"] == "cancelled"
        assert row["current_step"] == 0 and row["steps"][1]["status"] == "pending"
        assert runtime.resume_flow(flow["id"])["status"] == "cancelled"
        assert len(seen) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_approval_execution_exception_requires_reconciliation(wired):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    orch.executor.execute.side_effect = RuntimeError("possible remote commit before error")
    with pytest.raises(RuntimeError):
        await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    row = runtime.get_flow(flow["id"])
    assert row["status"] == "waiting" and row["steps"][0]["status"] == "outcome_unknown"
    assert runtime.resume_flow(flow["id"])["status"] == "waiting"
    await runtime._run_flow(flow["id"])
    assert orch.executor.execute.await_count == 1


@pytest.mark.parametrize("step_type, endpoint, safe", [
    ("skill.invoke", "search_notes", True), ("skill.invoke", "save_note", False),
    ("llm.chat", "", False), ("note.save", "", False), ("http.get", "", False), ("noop", "", True),
])
async def test_restart_replays_only_declared_safe_work(wired, step_type, endpoint, safe):
    runtime, orch, seen = wired
    flow = create(runtime, endpoint or "save_note", following=False)
    step_id = flow["steps"][0]["id"]
    runtime._conn.execute("UPDATE taskflows SET status = 'running' WHERE id = ?", (flow["id"],))
    runtime._conn.execute("UPDATE taskflow_steps SET step_type = ?, status = 'running' WHERE id = ?", (step_type, step_id))
    runtime._conn.commit()
    runtime._recover_after_restart()
    row = runtime.get_flow(flow["id"])
    assert row["status"] == ("queued" if safe else "waiting")
    assert row["steps"][0]["status"] == ("pending" if safe else "outcome_unknown")
    if not safe:
        runtime.resume_flow(flow["id"])
        await runtime._run_flow(flow["id"])
        assert seen == []


async def test_notification_failure_after_execution_does_not_replay(wired):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    orch._emit_tool_result.side_effect = RuntimeError("UI disconnected")
    with pytest.raises(RuntimeError):
        await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    assert runtime.get_flow(flow["id"])["steps"][0]["status"] == "completed"
    await runtime._run_flow(flow["id"])
    assert len(seen) == 1 and runtime.get_flow(flow["id"])["status"] == "completed"


async def test_immediate_approval_during_notification_has_durable_binding(wired):
    runtime, orch, seen = wired
    flow = create(runtime)

    async def immediate(session_id, _tool_name, pending):
        row = runtime.get_flow(flow["id"])
        assert row["steps"][0]["result"]["approval"]["request_id"] == pending["request_id"]
        assert pending["taskflow"]["flow_id"] == flow["id"]
        answer = await orch.resolve_tool_approval_request(pending["request_id"],
                                                        approved=True, session_id=session_id)
        assert answer["status"] == "approved"

    orch.tool_runner._notify_user_of_pending_approval.side_effect = immediate
    await runtime._run_flow(flow["id"])
    assert runtime.get_flow(flow["id"])["steps"][0]["status"] == "completed"
    assert len(seen) == 1
    await runtime._run_flow(flow["id"])
    assert len(seen) == 1 and runtime.get_flow(flow["id"])["status"] == "completed"


@pytest.mark.parametrize("change", ["pause", "cancel"])
async def test_pause_or_cancel_after_claim_before_dispatch_never_executes(wired, change):
    runtime, orch, seen = wired
    flow, pending = await review(wired)

    async def before_dispatch(*_args):
        if change == "pause":
            runtime._supervisor.paused = True
        else:
            runtime.cancel_flow(flow["id"])

    orch._emit_tool_start.side_effect = before_dispatch
    await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    assert seen == []
    assert runtime.get_flow(flow["id"])["current_step"] == 0


async def test_cancel_active_approval_dispatch_retains_unknown_result(wired):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    entered = asyncio.Event()

    async def blocked(**kwargs):
        seen.append((current_context(), kwargs))
        entered.set()
        await asyncio.Event().wait()

    orch.executor.execute.side_effect = blocked
    task = asyncio.create_task(orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id="owner"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime.cancel_flow(flow["id"])
        with pytest.raises(asyncio.CancelledError):
            await task
        row = runtime.get_flow(flow["id"])
        assert row["status"] == "cancelled"
        assert row["steps"][0]["status"] == "outcome_unknown"
        assert row["steps"][1]["status"] == "pending"
        assert runtime._approved_tasks == {} and len(seen) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_late_notification_error_preserves_immediately_completed_action(wired):
    runtime, orch, seen = wired
    flow = create(runtime)

    async def immediate(session_id, _tool_name, pending):
        await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id=session_id)
        raise RuntimeError("notification failed after completed approval")

    orch.tool_runner._notify_user_of_pending_approval.side_effect = immediate
    await runtime._run_flow(flow["id"])
    assert runtime.get_flow(flow["id"])["steps"][0]["status"] == "completed"
    await runtime._run_flow(flow["id"])
    assert runtime.get_flow(flow["id"])["status"] == "completed" and len(seen) == 1


async def test_cancelled_resolution_notification_clears_claim_without_replay(wired):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    orch._push_approval_resolved.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="owner")
    assert seen == [] and runtime._approved_tasks == {}
    assert runtime.get_flow(flow["id"])["steps"][0]["status"] == "outcome_unknown"
    runtime.resume_flow(flow["id"])
    await runtime._run_flow(flow["id"])
    assert seen == []


@pytest.mark.parametrize("claimed", [False, True])
async def test_reopened_database_never_replays_lost_review_or_claim(wired, claimed):
    runtime, orch, seen = wired
    flow, pending = await review(wired)
    if claimed:
        assert runtime.prepare_approved_dispatch(pending)
    # A replacement process loses the existing in-memory pending manager.
    orch.tool_runner._pending_approvals.clear()
    reopened = TaskFlowRuntime(db_path=runtime._db_path, skill_registry=orch.skills, orchestrator=orch)
    reopened._supervisor = runtime._supervisor
    orch.taskflows = reopened
    try:
        reopened._recover_after_restart()
        row = reopened.get_flow(flow["id"])
        assert row["status"] == "waiting" and row["wait_until"] is None
        assert row["steps"][0]["status"] == ("outcome_unknown" if claimed else "waiting")
        reopened.resume_flow(flow["id"])
        await reopened._run_flow(flow["id"])
        assert seen == [] and orch.tool_runner.list_pending() == []
    finally:
        runtime._approved_tasks.clear()
        await reopened.stop()
        reopened._conn.close()
        orch.taskflows = runtime


async def test_cancel_after_successful_read_preserves_known_step_result(wired, monkeypatch):
    runtime, orch, seen = wired
    flow = create(runtime, "search_notes")
    original = runtime._execute_step

    async def returned_before_cancel(*args):
        result = await original(*args)
        runtime._conn.execute("UPDATE taskflows SET status = 'cancelled' WHERE id = ?", (flow["id"],))
        runtime._conn.commit()
        return result

    monkeypatch.setattr(runtime, "_execute_step", returned_before_cancel)
    await runtime._run_flow(flow["id"])
    row = runtime.get_flow(flow["id"])
    assert row["status"] == "cancelled" and row["current_step"] == 0
    assert row["steps"][0]["status"] == "completed"
    assert row["steps"][0]["result"]["result"]["success"] is True
    assert row["steps"][1]["status"] == "pending" and len(seen) == 1
