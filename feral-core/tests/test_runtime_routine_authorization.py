"""Routine admission through actual policy, runner, executor and inert skill.

No models, networking, app launches, account actions or physical effects.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import api.server as server
from agents.orchestrator import Orchestrator
from agents.scheduler import CronService, JobType
from agents.tool_runner import ToolRunner
from models.skill_manifest import BrandProfile, EndpointParam, SkillEndpoint, SkillManifest
from security.exec_approvals import ApprovalManager
from skills.base import BaseSkill
from skills.call_context import current_context
from skills.executor import SkillExecutor
from skills.impl import SKILL_IMPLEMENTATIONS
from skills.registry import SkillRegistry


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_AUTONOMY", "hybrid")
    monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "on")
    registry = SkillRegistry()
    registry.register(SkillManifest(
        skill_id="notes_memory", brand=BrandProfile(name="Routine fixture"),
        description="inert production dispatch fixture",
        endpoints=[SkillEndpoint(id=endpoint, method="PYTHON", url="",
                                 description=endpoint, safety_tier=tier,
                                 read_only_hint=readonly,
                                 params=[EndpointParam(name="value", type="string", required=True)])
                   for endpoint, tier, readonly in (
                       ("search_notes", "safe", True), ("save_note", "confirm", False),
                       ("list_recent", "deny", False), ("fixture_ping", "safe", False),
                   )],
    ))
    seen = []

    class InertSkill(BaseSkill):
        async def execute(self, endpoint_id, args, vault):
            seen.append((endpoint_id, args, current_context()))
            return {"success": True, "status_code": 200,
                    "data": {"fixture": args["value"]}, "error": None}

    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, "notes_memory", InertSkill("notes_memory"))
    cron = CronService(db_path=str(tmp_path / "cron.db"))
    orch = Orchestrator.__new__(Orchestrator)
    orch.skills = registry
    orch._mcp_client = None
    orch._session_surfaces = {}
    orch._active_turns = {}
    orch.memory = None
    orch._background_tasks = set()
    orch.executor = SkillExecutor()
    orch.tool_runner = ToolRunner(orch, approval_manager=ApprovalManager())
    orch._send_text = AsyncMock()
    for key, value in {
        "cron_service": cron, "skill_registry": registry, "orchestrator": orch,
        "tool_runner": orch.tool_runner, "taskflows": None, "cron_cost_guard": None,
        "memory": None,
    }.items():
        monkeypatch.setattr(server.state, key, value, raising=False)
    yield SimpleNamespace(cron=cron, orch=orch, registry=registry, seen=seen)
    asyncio.run(orch.executor.close())
    cron.close()


def fire(wired, endpoint="search_notes", *, args=None, auto=False, session="routine-owner"):
    job = wired.cron.create_job(
        JobType.SCHEDULED, "every 1m", "inert fixture",
        {"skill": "notes_memory", "endpoint": endpoint,
         "args": {"value": "exact fixture"} if args is None else args,
         "auto_confirm": auto}, session,
    )
    server.execute_routine_job(job)
    rows = wired.cron.get_runs(job.id)
    return job, rows[0] if rows else None


def test_registered_auto_uses_real_central_executor_and_bound_run_identity(wired):
    job, run = fire(wired)
    assert run["status"] == "success"
    assert len(wired.seen) == 1
    endpoint, args, ctx = wired.seen[0]
    assert endpoint == "search_notes" and args == {"value": "exact fixture"}
    assert ctx.session_id == "routine-owner" and ctx.surface == "cron"
    assert ctx.tool_name == "notes_memory__search_notes"
    assert ctx.call_id == f"routine:{job.id}:run:{run['id']}"
    assert current_context().session_id == ""


def test_empty_session_uses_explicit_routine_namespace(wired):
    job, run = fire(wired, session="")
    assert run["status"] == "success"
    assert wired.seen[0][2].session_id == f"routine-{job.id}"


def test_policy_exception_dispatches_zero_and_redacts_details(wired, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private policy detail")
    monkeypatch.setattr("security.safety_resolver.resolve_policy", fail)
    _, run = fire(wired, auto=True)
    assert wired.seen == []
    assert run["status"] == "error" and run["result"]["reason"] == "policy_unavailable"
    assert "private policy detail" not in str(run)


@pytest.mark.parametrize("mode", ["strict", "hybrid", "loose"])
@pytest.mark.parametrize("auto", [False, True])
def test_confirm_has_no_exact_durable_grant_even_with_standing_permission(wired, mode, auto):
    wired.orch.tool_runner.set_autonomy_mode(mode)
    wired.orch.tool_runner.grant_session_approval("notes_memory__save_note", "routine-owner")
    _, run = fire(wired, "save_note", auto=auto)
    assert wired.seen == []
    assert run["status"] == "skipped"
    assert run["result"]["reason"] == "routine_exact_approval_unavailable"
    assert wired.orch.tool_runner.list_pending() == []


@pytest.mark.parametrize("auto", [False, True])
def test_deny_is_final_even_auto_confirm(wired, auto):
    _, run = fire(wired, "list_recent", auto=auto)
    assert wired.seen == [] and run["status"] == "skipped"
    assert run["result"]["policy"]["level"] == "deny"


@pytest.mark.parametrize("skill,endpoint,args,source", [
    ("coding_tools", "bash", {"command": "inert"}, "surface_deny"),
    ("cutebot", "drive", {"left": 99, "right": 0}, "cutebot_speed_limit"),
])
def test_actual_hard_denials_remain_non_overridable(wired, skill, endpoint, args, source):
    wired.registry.register(SkillManifest(
        skill_id=skill, brand=BrandProfile(name="Denied inert fixture"),
        description="No backing implementation can run",
        endpoints=[SkillEndpoint(id=endpoint, method="PYTHON", url="", description="fixture")],
    ))
    job = wired.cron.create_job(JobType.SCHEDULED, "every 1m", "hard denial",
                               {"skill": skill, "endpoint": endpoint,
                                "args": args, "auto_confirm": True}, "routine-owner")
    server.execute_routine_job(job)
    run = wired.cron.get_runs(job.id)[0]
    assert wired.seen == [] and run["status"] == "skipped"
    assert run["result"]["policy"]["sources"][source] is True


@pytest.mark.parametrize("bad_return", [None, 0, True])
def test_no_persisted_run_identity_dispatches_zero(wired, monkeypatch, bad_return):
    monkeypatch.setattr(wired.cron, "record_run_start", lambda _: bad_return)
    _, run = fire(wired)
    assert run is None and wired.seen == []


def test_bookkeeping_exception_dispatches_zero(wired, monkeypatch):
    def fail(*args):
        raise RuntimeError("inert persistence failure")
    monkeypatch.setattr(wired.cron, "record_run_start", fail)
    _, run = fire(wired)
    assert run is None and wired.seen == []


def test_missing_full_dispatcher_fails_closed(wired, monkeypatch):
    monkeypatch.setattr(server.state, "orchestrator", None)
    _, run = fire(wired)
    assert wired.seen == [] and run["status"] == "error"
    assert run["result"]["reason"] == "dispatcher_unavailable"


def test_central_plan_gate_and_schema_validation_preserved(wired):
    _, invalid = fire(wired, args={"value": {"bad": "type"}})
    assert invalid["status"] == "error" and wired.seen == []
    wired.orch.tool_runner.plan_mode.enter("routine-owner")
    _, blocked = fire(wired, "fixture_ping")
    assert blocked["status"] != "success" and wired.seen == []
    _, readable = fire(wired)
    assert readable["status"] == "success" and len(wired.seen) == 1


@pytest.mark.parametrize("session", [" padded ", "bad\x00owner"])
def test_invalid_owner_identity_fails_closed(wired, session):
    _, run = fire(wired, session=session)
    assert wired.seen == [] and run["status"] == "error"
    assert run["result"]["reason"] == "invalid_session"


def test_disabled_call_context_fails_closed(wired, monkeypatch):
    monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "off")
    _, run = fire(wired)
    assert wired.seen == [] and run["status"] == "error"
    assert run["result"]["dispatch_started"] is False


@pytest.mark.parametrize("mode", ["strict", "hybrid", "loose"])
@pytest.mark.parametrize("change_at", ["dispatch", "executor"])
def test_auto_preflight_cannot_bypass_later_confirm_policy(wired, monkeypatch, mode, change_at):
    from security.safety_resolver import PolicyDecision
    runner = wired.orch.tool_runner
    runner.set_autonomy_mode(mode)
    runner.grant_session_approval("notes_memory__search_notes", "routine-owner")
    original = runner.policy_for
    calls = []

    def changing(tool, args, *, surface):
        calls.append(surface)
        if change_at == "dispatch" or len(calls) > 1:
            return PolicyDecision(tool, surface, "confirm", {"fixture_policy_changed": True})
        return original(tool, args, surface=surface)

    monkeypatch.setattr(runner, "policy_for", changing)
    _, run = fire(wired)
    assert run["status"] == "error" and wired.seen == []
    assert run["result"]["error_code"] == "routine_exact_approval_unavailable"
    assert calls == (["cron"] if change_at == "dispatch" else ["cron", "cron"])
    assert runner.list_pending() == []


@pytest.mark.parametrize("mode", ["strict", "hybrid", "loose"])
def test_direct_central_cron_confirm_needs_durable_grant(wired, mode):
    runner = wired.orch.tool_runner
    runner.set_autonomy_mode(mode)
    runner.grant_session_approval("notes_memory__save_note", "routine-owner")
    result = asyncio.run(runner.execute_tool_call_for_llm(
        "routine-owner", {"id": "inert-direct-cron", "name": "notes_memory__save_note",
                          "args": {"value": "exact fixture"}}, [], surface="cron",
    ))
    assert result["success"] is False
    assert result["error_code"] == "routine_exact_approval_unavailable"
    assert wired.seen == [] and runner.list_pending() == []


def test_scheduler_thread_handoff_preserves_owner_on_brain_loop(wired, monkeypatch):
    async def exercise():
        loop = asyncio.get_running_loop()
        wired.orch._owning_loop = loop
        implementation = SKILL_IMPLEMENTATIONS["notes_memory"]
        original = implementation.execute

        async def checked(*args, **kwargs):
            assert asyncio.get_running_loop() is loop
            return await original(*args, **kwargs)

        monkeypatch.setattr(implementation, "execute", checked)
        return await asyncio.to_thread(fire, wired)

    job, run = asyncio.run(exercise())
    assert run["status"] == "success" and len(wired.seen) == 1
    ctx = wired.seen[0][2]
    assert ctx.session_id == "routine-owner" and ctx.surface == "cron"
    assert ctx.call_id == f"routine:{job.id}:run:{run['id']}"


def test_enclosing_foreign_call_identity_is_not_inherited(wired):
    from skills.call_context import bind_context
    with bind_context(session_id="foreign-owner", surface="http_api", call_id="foreign-call"):
        _, run = fire(wired)
        assert current_context().session_id == "foreign-owner"
    assert run["status"] == "success"
    ctx = wired.seen[0][2]
    assert ctx.session_id == "routine-owner" and ctx.surface == "cron"
    assert ctx.call_id != "foreign-call"
