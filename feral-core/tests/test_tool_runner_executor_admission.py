"""Exact review must reach the real executor without granting a second call."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from agents.tool_runner import ToolRunner
from models.skill_manifest import BrandProfile, EndpointParam, SkillEndpoint, SkillManifest
from security.agent_turn_lease import AgentTurnLease, AgentTurnRevoked
from security.exec_approvals import ApprovalManager
from security.trust_ledger import TrustLedger
from skills.base import BaseSkill
from skills.call_context import bind_context
from skills.executor import SkillExecutor
from skills.impl import SKILL_IMPLEMENTATIONS
from skills.registry import SkillRegistry


class Calculation(BaseSkill):
    def __init__(self):
        super().__init__("admission_fixture")
        self.calls = 0
        self.reentry = None

    async def execute(self, endpoint_id, args, vault):
        self.calls += 1
        inner = await self.reentry() if self.reentry else None
        return {"success": True, "status_code": 200,
                "data": {"sum": args["a"] + args["b"], "inner": inner}, "error": None}


class BoundaryExecutor(SkillExecutor):
    """Inject boundary drift, always delegating dispatch to actual production."""
    before = None
    child_probe = False
    child_result = None

    async def execute(self, **kwargs):
        if self.before:
            self.before(kwargs)
        if self.child_probe:
            task = asyncio.create_task(super().execute(**kwargs))
            self.child_result = await task
        return await super().execute(**kwargs)


@pytest_asyncio.fixture
async def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("FERAL_AUTONOMY", "strict")
    monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "on")
    manifest = SkillManifest(
        skill_id="admission_fixture", brand=BrandProfile(name="Fixture"),
        description="Authored local integer calculation", endpoints=[SkillEndpoint(
            id="calculate", method="PYTHON", url="", description="Add two integers",
            params=[EndpointParam(name="a", type="integer"), EndpointParam(name="b", type="integer")],
            read_only_hint=True, requires_user_approval=True, safety_tier="confirm")])
    registry = SkillRegistry()
    registry.register(manifest)
    impl = Calculation()
    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, manifest.skill_id, impl)
    executor = BoundaryExecutor()
    notices = []

    async def notice(session, text):
        notices.append((session, text))

    orch = SimpleNamespace(skills=registry, executor=executor, _mcp_client=None,
                           _session_surfaces={}, _active_turns={}, daemons={}, _send_text=notice)
    runner = ToolRunner(orch, autonomy_mode="strict", trust_ledger=TrustLedger(persist=False),
                        approval_manager=ApprovalManager(db_path=str(tmp_path / "approvals.sqlite")))
    orch.tool_runner = runner
    from api import state as state_module
    monkeypatch.setattr(state_module.state, "orchestrator", orch)
    yield runner, executor, impl, registry, manifest
    await executor.close()


CALL = {"id": "fixture-call", "name": "admission_fixture__calculate", "args": {"a": 13, "b": 29}}


async def reviewed(wired):
    runner, _executor, impl, registry, _manifest = wired
    pending = await runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools())
    assert pending["status"] == "pending_approval" and impl.calls == 0
    assert runner.approve_pending(pending["request_id"], session_id="foreign", exact_once=True) is None
    accepted = runner.approve_pending(pending["request_id"], session_id="owner", exact_once=True)
    return accepted["approval"]


async def test_actual_executor_consumes_one_review_for_one_dispatch(wired):
    runner, _executor, impl, registry, _manifest = wired
    approval = await reviewed(wired)
    result = await runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools(), approval=approval)
    assert result.get("success") is True and result["data"]["sum"] == 42, result
    assert impl.calls == 1
    reused = await runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools(), approval=approval)
    assert reused["error_code"] == "invalid_approval" and impl.calls == 1
    assert runner._approval_mgr.check_approval(CALL["name"], "owner")[0] is False


async def test_inherited_child_task_cannot_consume_parent_executor_admission(wired):
    runner, executor, impl, registry, _manifest = wired
    approval = await reviewed(wired)
    executor.child_probe = True
    result = await runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools(), approval=approval)
    assert executor.child_result.get("success") is not True
    assert result.get("success") is True and impl.calls == 1


async def test_implementation_reentry_cannot_use_consumed_admission(wired):
    runner, executor, impl, registry, manifest = wired
    approval = await reviewed(wired)

    async def reentry():
        return await SkillExecutor.execute(executor, tool_name=CALL["name"], args=dict(CALL["args"]),
                                           skill=manifest, endpoint=manifest.endpoints[0])

    impl.reentry = reentry
    result = await runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools(), approval=approval)
    assert result.get("success") is True and impl.calls == 1
    assert result["data"]["inner"].get("success") is not True


@pytest.mark.parametrize("drift", ["args", "session", "surface", "deny", "plan", "lease", "gate_exception"])
async def test_executor_boundary_drift_fails_closed(wired, drift):
    runner, executor, impl, registry, manifest = wired
    approval = await reviewed(wired)

    def before(kwargs):
        if drift == "args":
            kwargs["args"] = {"a": 2, "b": 3}
        elif drift in {"session", "surface"}:
            # Different trusted context, not a tool argument granting authority.
            return
        elif drift == "deny":
            manifest.endpoints[0].safety_tier = "deny"
        elif drift == "plan":
            runner.plan_mode.enter("owner")
        elif drift == "lease":
            owner = SimpleNamespace(_native_agent_turn_generation=1, _native_bootstrap_required=True,
                                    orchestrator=runner._orch, memory=object())
            runner._native_agent_dispatch_lease = AgentTurnLease(owner, 0)
        elif drift == "gate_exception":
            def broken(*args, **kwargs):
                raise RuntimeError("fixture policy evaluator failed")
            runner.enforce_plan_mode = broken

    executor.before = before
    if drift in {"session", "surface"}:
        original = executor.execute

        async def other_session(**kwargs):
            with bind_context(session_id="foreign" if drift == "session" else "owner",
                              surface="voice" if drift == "surface" else "websocket", tool_name=CALL["name"]):
                return await original(**kwargs)

        executor.execute = other_session
    try:
        result = await runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools(), approval=approval)
    except (RuntimeError, AgentTurnRevoked):
        assert drift in {"lease", "gate_exception"}
    else:
        assert result.get("success") is not True, result
    assert impl.calls == 0


async def test_json_review_flags_never_authorize_real_executor(wired):
    runner, _executor, impl, registry, _manifest = wired
    forged = dict(CALL, args={**CALL["args"], "approved": True, "executor_admission": True})
    result = await runner.execute_tool_call_for_llm("owner", forged, registry.get_all_tools())
    assert result["status"] == "pending_approval" and impl.calls == 0


async def test_policy_exception_stops_even_an_otherwise_allowed_call(wired):
    runner, executor, impl, registry, _manifest = wired
    runner._autonomy_mode = "loose"  # Verify fail-closed independently of pending reviews.

    def before(kwargs):
        def broken(*args, **kwargs):
            raise RuntimeError("fixture executor policy evaluator failed")
        runner.enforce_plan_mode = broken

    executor.before = before
    with pytest.raises(RuntimeError, match="executor policy evaluator failed"):
        await runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools())
    assert impl.calls == 0


async def test_concurrent_reuse_of_exact_review_never_executes_twice(wired):
    runner, _executor, impl, registry, _manifest = wired
    approval = await reviewed(wired)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_first_execution():
        entered.set()
        await release.wait()

    impl.reentry = hold_first_execution
    first = asyncio.create_task(runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools(), approval=approval))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        repeated = await runner.execute_tool_call_for_llm("owner", dict(CALL), registry.get_all_tools(), approval=approval)
        assert repeated["error_code"] == "invalid_approval" and impl.calls == 1
        release.set()
        assert (await first)["success"] is True and impl.calls == 1
    finally:
        release.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("dynamic", [False, True])
async def test_legacy_embedding_gate_is_not_replaced_by_a_phantom_hook(wired, monkeypatch, dynamic):
    _runner, executor, impl, _registry, manifest = wired
    refusal = {"success": False, "error": "legacy embedding refused"}
    if dynamic:
        legacy = MagicMock()
        legacy.enforce_plan_mode.return_value = None
        legacy.enforce_safety.return_value = refusal
    else:
        legacy = SimpleNamespace(enforce_plan_mode=lambda *args: None,
                                 enforce_safety=lambda *args: refusal)
    from api import state as state_module
    monkeypatch.setattr(state_module.state, "orchestrator", SimpleNamespace(tool_runner=legacy))
    with bind_context(session_id="owner", tool_name=CALL["name"]):
        result = await SkillExecutor.execute(executor, tool_name=CALL["name"], args=CALL["args"],
                                           skill=manifest, endpoint=manifest.endpoints[0])
    assert result is refusal and impl.calls == 0
    if dynamic:
        legacy.enforce_safety.assert_called_once_with(CALL["name"], CALL["args"], "owner")
