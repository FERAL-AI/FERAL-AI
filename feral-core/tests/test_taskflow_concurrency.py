"""Actual persisted TaskFlow concurrency with inert memory/executor boundaries."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
import pytest

from agents.orchestrator import Orchestrator
from agents.taskflow import TaskFlowRuntime
from agents.tool_runner import ToolRunner
from models.skill_manifest import BrandProfile, EndpointParam, SkillEndpoint, SkillManifest
from security.exec_approvals import ApprovalManager
from skills.registry import SkillRegistry


class MemoryBoundary:
    def __init__(self):
        self.entered: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.calls: list[tuple[str, str]] = []
        self.effects = 0
        self.peak_effects = 0

    def delay(self, name):
        self.entered[name] = asyncio.Event()
        self.release[name] = asyncio.Event()

    async def search_all(self, query, **kwargs):
        self.calls.append(("read", query))
        if query in self.entered:
            self.entered[query].set()
            await self.release[query].wait()
        return [{"content": query}]

    async def save(self, content, **kwargs):
        self.calls.append(("effect", content))
        self.effects += 1
        self.peak_effects = max(self.peak_effects, self.effects)
        try:
            if content in self.entered:
                self.entered[content].set()
                await self.release[content].wait()
            return {"content": content}
        finally:
            self.effects -= 1


@pytest_asyncio.fixture
async def runtime(tmp_path):
    memory = MemoryBoundary()
    rt = TaskFlowRuntime(db_path=str(tmp_path / "flows.db"), memory_store=memory)
    yield rt, memory
    await rt.stop()
    rt._conn.close()


def flow(rt, name, *, read=False, steps=None):
    return rt.create_flow(session_id="owner", title=name, steps=steps or [
        {"type": "memory.search", "query": name} if read else
        {"type": "note.save", "content": name},
    ])


async def status(rt, item, wanted):
    async def poll():
        while rt.get_flow(item["id"])["status"] != wanted:
            await asyncio.sleep(0.01)
        return rt.get_flow(item["id"])
    return await asyncio.wait_for(poll(), 5)


async def test_slow_effect_does_not_block_independent_read(runtime):
    rt, memory = runtime
    memory.delay("shared-resource")
    effect = flow(rt, "shared-resource")
    read = flow(rt, "independent-read", read=True)
    await rt.start()
    await asyncio.wait_for(memory.entered["shared-resource"].wait(), 5)
    await status(rt, read, "completed")
    assert rt.get_flow(effect["id"])["status"] == "running"
    memory.release["shared-resource"].set()
    await status(rt, effect, "completed")
    assert memory.calls.count(("effect", "shared-resource")) == 1


async def test_effects_share_one_conservative_lane_and_do_not_overlap(runtime):
    rt, memory = runtime
    memory.delay("first")
    first, second = flow(rt, "first"), flow(rt, "second")
    read = flow(rt, "read", read=True)
    await rt.start()
    await asyncio.wait_for(memory.entered["first"].wait(), 5)
    await status(rt, read, "completed")
    assert ("effect", "second") not in memory.calls
    assert rt.get_flow(second["id"])["steps"][0]["status"] == "pending"
    memory.release["first"].set()
    await status(rt, first, "completed")
    await status(rt, second, "completed")
    assert memory.peak_effects == 1


async def test_retained_reads_are_bounded_and_waiting_effects_do_not_fill_slots(runtime):
    rt, memory = runtime
    for index in range(5):
        memory.delay(str(index))
        flow(rt, str(index), read=True)
    await rt.start()
    for index in range(4):
        await asyncio.wait_for(memory.entered[str(index)].wait(), 5)
    assert not memory.entered["4"].is_set()
    assert len(rt._flow_tasks) == len(rt._step_tasks) == rt._max_active_flows
    memory.release["0"].set()
    await asyncio.wait_for(memory.entered["4"].wait(), 5)
    assert len(rt._flow_tasks) <= rt._max_active_flows
    for event in memory.release.values():
        event.set()


async def test_many_queued_effects_do_not_starve_a_ready_local_read(runtime):
    rt, memory = runtime
    memory.delay("held")
    flow(rt, "held")
    for index in range(250):
        flow(rt, f"waiting-effect-{index}")
    read = flow(rt, "late-read", read=True)
    await rt.start()
    await asyncio.wait_for(memory.entered["held"].wait(), 5)
    await status(rt, read, "completed")
    assert memory.calls == [("effect", "held"), ("read", "late-read")]


async def test_cancel_isolated_effect_remains_unknown_other_read_finishes(runtime):
    rt, memory = runtime
    memory.delay("effect")
    memory.delay("read")
    effect, read = flow(rt, "effect"), flow(rt, "read", read=True)
    await rt.start()
    await asyncio.wait_for(memory.entered["effect"].wait(), 5)
    await asyncio.wait_for(memory.entered["read"].wait(), 5)
    rt.cancel_flow(effect["id"])
    async def settled():
        while rt.get_flow(effect["id"])["steps"][0]["status"] != "outcome_unknown":
            await asyncio.sleep(0.01)
    await asyncio.wait_for(settled(), 5)
    assert rt.get_flow(effect["id"])["status"] == "cancelled"
    assert rt.get_flow(read["id"])["status"] == "running"
    memory.release["read"].set()
    await status(rt, read, "completed")
    rt.resume_flow(effect["id"])
    assert rt.get_flow(effect["id"])["status"] == "cancelled"
    assert memory.calls.count(("effect", "effect")) == 1


async def test_stop_drains_tasks_and_restart_never_replays_unknown_effect(runtime):
    rt, memory = runtime
    memory.delay("effect")
    effect = flow(rt, "effect")
    await rt.start()
    await asyncio.wait_for(memory.entered["effect"].wait(), 5)
    await rt.stop()
    assert rt._flow_tasks == rt._step_tasks == rt._approved_tasks == {}
    assert rt._effect_owner is None
    assert rt.get_flow(effect["id"])["steps"][0]["status"] == "outcome_unknown"
    read = flow(rt, "after-restart", read=True)
    await rt.start()
    await status(rt, read, "completed")
    assert not rt._http.is_closed
    assert memory.calls.count(("effect", "effect")) == 1


async def test_concurrent_calls_claim_same_flow_once(runtime):
    rt, memory = runtime
    memory.delay("effect")
    item = flow(rt, "effect")
    first = asyncio.create_task(rt._run_flow(item["id"]))
    try:
        await asyncio.wait_for(memory.entered["effect"].wait(), 5)
        assert rt.resume_flow(item["id"])["status"] == "running"
        await rt._run_flow(item["id"])
        assert memory.calls == [("effect", "effect")]
        memory.release["effect"].set()
        await first
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_recovery_replays_only_proven_read_and_preserves_uncertain_effect(runtime):
    rt, memory = runtime
    read, effect = flow(rt, "read", read=True), flow(rt, "effect")
    with rt._conn:
        rt._conn.execute("UPDATE taskflows SET status = 'running'")
        rt._conn.execute("UPDATE taskflow_steps SET status = 'running'")
    await rt.start()
    await status(rt, read, "completed")
    assert rt.get_flow(effect["id"])["steps"][0]["status"] == "outcome_unknown"
    assert memory.calls == [("read", "read")]


@pytest_asyncio.fixture
async def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_AUTONOMY", "hybrid")
    registry = SkillRegistry()
    registry.register(SkillManifest(skill_id="notes_memory", brand=BrandProfile(name="Lane fixture"),
        description="inert executor fixture", endpoints=[
            SkillEndpoint(id=name, method="PYTHON", url="", description=name,
                          safety_tier=tier, read_only_hint=read,
                          params=[EndpointParam(name="value", type="string", required=True)])
            for name, tier, read in [("search_notes", "safe", True),
                                     ("save_note", "confirm", False),
                                     ("list_recent", "deny", False)]
        ]))
    orch = Orchestrator.__new__(Orchestrator)
    orch.skills = registry
    orch._mcp_client = None
    orch._session_surfaces = {}
    orch._active_turns = {}
    orch.memory = None
    orch.executor = SimpleNamespace(execute=AsyncMock(return_value={"success": True, "data": "read"}))
    orch.tool_runner = ToolRunner(orch, approval_manager=ApprovalManager())
    for name in ["_emit_tool_start", "_emit_tool_result", "_try_genui_for_result", "_send_text", "_push_approval_resolved"]:
        setattr(orch, name, AsyncMock())
    orch.tool_runner._notify_user_of_pending_approval = AsyncMock()
    orch._summarize_action_result = lambda *_: "fixture receipt"
    memory = MemoryBoundary()
    rt = TaskFlowRuntime(db_path=str(tmp_path / "wired.db"), memory_store=memory,
                         skill_registry=registry, orchestrator=orch)
    orch.taskflows = rt
    yield rt, memory, orch
    await rt.stop()
    rt._conn.close()


def skill_flow(rt, endpoint, *, following=False):
    steps = [{"type": "skill.invoke", "skill_id": "notes_memory", "endpoint": endpoint,
              "args": {"value": "exact input"}}]
    if following:
        steps.append({"type": "noop"})
    return rt.create_flow(session_id="owner", title=endpoint, steps=steps, origin_surface="local_cli")


async def test_registered_read_dispatch_progresses_through_actual_toolrunner(wired):
    rt, memory, orch = wired
    memory.delay("held-effect")
    flow(rt, "held-effect")
    read = skill_flow(rt, "search_notes")
    await rt.start()
    await asyncio.wait_for(memory.entered["held-effect"].wait(), 5)
    await status(rt, read, "completed")
    call = orch.executor.execute.call_args.kwargs
    assert call["skill"].skill_id == "notes_memory" and call["endpoint"].id == "search_notes"
    assert call["args"] == {"value": "exact input"}


def test_missing_registry_or_read_hint_cannot_grant_independent_skill_lane(runtime):
    rt, _ = runtime
    item = skill_flow(rt, "search_notes")
    assert not rt._independent_step(item["steps"][0])


async def test_denied_action_no_effect_and_no_automatic_replay(wired):
    rt, _, orch = wired
    item = skill_flow(rt, "list_recent")
    await rt.start()
    await status(rt, item, "failed")
    assert orch.executor.execute.await_count == 0
    await rt._run_flow(item["id"])
    assert orch.executor.execute.await_count == 0
    assert rt._effect_owner is None


async def test_busy_lane_refuses_approved_dispatch_before_consuming_grant(wired):
    rt, memory, orch = wired
    item = skill_flow(rt, "save_note")
    await rt._run_flow(item["id"])
    pending = orch.tool_runner.list_pending()[0]
    memory.delay("held-effect")
    flow(rt, "held-effect")
    await rt.start()
    await asyncio.wait_for(memory.entered["held-effect"].wait(), 5)
    answer = await orch.resolve_tool_approval_request(pending["request_id"], approved=True,
                                                     session_id="owner")
    assert answer["status"] == "rejected"
    assert orch.executor.execute.await_count == 0
    assert rt.get_flow(item["id"])["steps"][0]["result"]["dispatch_started"] is False
    assert rt._approved_tasks == {}


async def test_stopped_runtime_cannot_claim_workflow_approval(wired):
    rt, _, orch = wired
    item = skill_flow(rt, "save_note")
    await rt._run_flow(item["id"])
    pending = orch.tool_runner.list_pending()[0]
    await rt.stop()
    assert not rt.prepare_approved_dispatch(pending)
    assert not rt.approved_dispatch_allowed(pending)
    assert orch.executor.execute.await_count == 0
    assert rt.get_flow(item["id"])["steps"][0]["status"] == "waiting"


async def test_foreign_receipt_cannot_release_active_approval_lane(wired):
    rt, _, orch = wired
    item = skill_flow(rt, "save_note")
    await rt._run_flow(item["id"])
    pending = orch.tool_runner.list_pending()[0]
    assert rt.prepare_approved_dispatch(pending)
    owner = rt._effect_owner
    held_task = rt._approved_tasks[item["id"]]
    foreign = json.loads(json.dumps(pending))
    foreign["request_id"] = "other-request"
    assert not await rt.finish_approved_dispatch(foreign, rejected=True)
    assert rt._effect_owner == owner
    assert rt._approved_tasks[item["id"]] is held_task
    assert await rt.finish_approved_dispatch(pending, rejected=True)
    assert rt._effect_owner is None


async def test_approved_effect_owns_lane_while_local_read_can_finish(wired):
    rt, memory, orch = wired
    approved = skill_flow(rt, "save_note")
    await rt._run_flow(approved["id"])
    pending = orch.tool_runner.list_pending()[0]
    await rt.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def effect(**kwargs):
        entered.set()
        await release.wait()
        return {"success": True, "data": "approved effect receipt"}

    orch.executor.execute.side_effect = effect
    dispatch = asyncio.create_task(orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id="owner"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = flow(rt, "second-effect")
        read = flow(rt, "independent", read=True)
        await status(rt, read, "completed")
        assert ("effect", "second-effect") not in memory.calls
        assert rt.get_flow(second["id"])["steps"][0]["status"] == "pending"
        release.set()
        await dispatch
        await status(rt, second, "completed")
        assert orch.executor.execute.await_count == 1
        assert rt._approved_tasks == rt._approved_lane_owners == {}
    finally:
        release.set()
        dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)


async def test_step_error_records_unknown_without_stalling_other_flows(runtime):
    rt, memory = runtime
    original = memory.save

    async def broken(content, **kwargs):
        if content == "broken":
            memory.calls.append(("effect", content))
            raise RuntimeError("inert collaborator failed after attempted effect")
        return await original(content, **kwargs)

    memory.save = broken
    broken_flow = flow(rt, "broken")
    other = flow(rt, "other")
    await rt.start()
    await status(rt, other, "completed")
    row = rt.get_flow(broken_flow["id"])
    assert row["status"] == "waiting" and row["steps"][0]["status"] == "outcome_unknown"
    rt.resume_flow(broken_flow["id"])
    await rt._run_flow(broken_flow["id"])
    assert memory.calls.count(("effect", "broken")) == 1


async def test_start_never_recovers_over_an_active_manual_dispatch(runtime):
    rt, memory = runtime
    memory.delay("effect")
    item = flow(rt, "effect")
    dispatch = asyncio.create_task(rt._run_flow(item["id"]))
    try:
        await asyncio.wait_for(memory.entered["effect"].wait(), 5)
        with pytest.raises(RuntimeError, match="owned dispatch is active"):
            await rt.start()
        assert rt.get_flow(item["id"])["steps"][0]["status"] == "running"
        memory.release["effect"].set()
        await dispatch
        assert rt.get_flow(item["id"])["status"] == "completed"
        await rt.start()
    finally:
        dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)


async def test_stop_before_lane_claim_recovers_pending_flow_without_effect_replay(runtime):
    rt, memory = runtime
    memory.delay("active")
    active = flow(rt, "active")
    await rt.start()
    await asyncio.wait_for(memory.entered["active"].wait(), 5)
    waiting = flow(rt, "waiting-for-lane")
    # Simulate an admitted worker whose resource was claimed by another
    # dispatcher before this worker ran. Ownership/claim methods are real.
    task = asyncio.create_task(rt._run_retained_flow(waiting["id"]))
    rt._flow_tasks[waiting["id"]] = task
    await status(rt, waiting, "running")
    assert rt.get_flow(waiting["id"])["steps"][0]["status"] == "pending"
    await rt.stop()
    assert rt._flow_tasks == rt._step_tasks == {}
    assert rt.get_flow(active["id"])["steps"][0]["status"] == "outcome_unknown"
    assert rt.get_flow(waiting["id"])["steps"][0]["status"] == "pending"
    recovered_memory = MemoryBoundary()
    reopened = TaskFlowRuntime(db_path=rt._db_path, memory_store=recovered_memory)
    try:
        await reopened.start()
        await status(reopened, waiting, "completed")
        assert recovered_memory.calls == [("effect", "waiting-for-lane")]
        assert reopened.get_flow(active["id"])["steps"][0]["status"] == "outcome_unknown"
    finally:
        await reopened.stop()
        reopened._conn.close()
