"""Background scene suggestions obey real ToolRunner policy before any skill I/O."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.proactive_engine import Priority, ProactiveEngine, ProactiveMessage
from agents.tool_runner import ToolRunner
from security.exec_approvals import ApprovalManager
from skills.registry import SkillRegistry


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("FERAL_AUTONOMY", raising=False)
    registry = SkillRegistry()
    registry.load_from_file(Path(__file__).parents[1] / "skills/manifests/smart_home.json")
    orch = SimpleNamespace(
        _primary_session_id_resolver=lambda: "primary-test", skills=registry,
        executor=SimpleNamespace(execute=AsyncMock(return_value={"success": True, "data": {"called": True}})),
        _mcp_client=None, _session_surfaces={}, _active_turns={}, _send_text=AsyncMock(),
    )
    approvals = ApprovalManager(db_path=":memory:")
    trust = Mock()
    trust.is_trusted.return_value = False
    orch.tool_runner = ToolRunner(orch, autonomy_mode="hybrid", trust_ledger=trust, approval_manager=approvals)
    engine = ProactiveEngine(orchestrator=orch)
    from api.state import state
    supervisor = Mock()
    monkeypatch.setattr(state, "supervisor", supervisor)
    yield engine, orch, approvals, supervisor
    approvals.close()


def message(kind="set_scene", **payload):
    return ProactiveMessage("test-alert", Priority.IMPORTANT, "Test", "Suggestion", action_payload={"smart_home": kind, **payload})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,entity", [("set_scene", "scene.calming"), ("breathing_exercise", "scene.breathing")])
async def test_real_policy_review_blocks_direct_implementation(setup, kind, entity):
    engine, orch, _, supervisor = setup
    result = await engine._execute_automation(message(kind))
    assert result["status"] == "pending_approval"
    orch.executor.execute.assert_not_awaited()
    pending = orch.tool_runner.pending_for_session("primary-test")
    assert len(pending) == 1
    assert pending[0]["args"] == {"domain": "scene", "service": "turn_on", "entity_id": entity}
    assert pending[0]["session_id"] == "primary-test"
    assert supervisor.record.call_args.kwargs["decision"] == "pending_approval"
    orch._send_text.assert_awaited()


@pytest.mark.asyncio
async def test_real_reviewed_grant_executes_only_exact_scene(setup):
    engine, orch, approvals, supervisor = setup
    approvals.grant_approval("smart_home_hue__call_service", "primary-test", scope="session")
    result = await engine._execute_automation(message(scene="calming"))
    assert result["success"] is True
    call = orch.executor.execute.await_args.kwargs
    assert call["tool_name"] == "smart_home_hue__call_service"
    assert call["args"] == {"domain": "scene", "service": "turn_on", "entity_id": "scene.calming"}
    assert supervisor.record.call_args.kwargs["decision"] == "allowed"


@pytest.mark.asyncio
async def test_loose_mode_is_not_unattended_scene_consent(setup):
    engine, orch, _, supervisor = setup
    orch.tool_runner._autonomy_mode = "loose"
    result = await engine._execute_automation(message())
    assert result["status"] == "blocked"
    orch.executor.execute.assert_not_awaited()
    assert supervisor.record.call_args.kwargs["decision"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["", None, 42, " ", "x" * 257])
async def test_missing_owner_never_borrows_recent_session(setup, owner):
    engine, orch, _, _ = setup
    orch._primary_session_id_resolver = lambda: owner
    result = await engine._execute_automation(message())
    assert result["success"] is False
    orch.executor.execute.assert_not_awaited()
    assert not orch.tool_runner.pending_for_session("primary-test")


@pytest.mark.asyncio
async def test_plan_mode_denies_without_skill_dispatch(setup):
    engine, orch, _, supervisor = setup
    orch.tool_runner.plan_mode.enter("primary-test")
    result = await engine._execute_automation(message())
    from agents.plan_mode import PLAN_REFUSAL_CODE
    assert result["error_code"] == PLAN_REFUSAL_CODE
    orch.executor.execute.assert_not_awaited()
    assert supervisor.record.call_args.kwargs["decision"] != "allowed"


@pytest.mark.asyncio
async def test_failed_provider_not_a_success_receipt_and_notification_survives(setup):
    engine, orch, approvals, supervisor = setup
    approvals.grant_approval("smart_home_hue__call_service", "primary-test", scope="session")
    orch.executor.execute.side_effect = RuntimeError("synthetic provider unavailable")
    callback = AsyncMock()
    engine.on_message(callback)
    await engine._deliver(message())
    assert supervisor.record.call_args.kwargs["decision"] == "error"
    callback.assert_awaited_once()
    assert orch.executor.execute.await_count == 1


@pytest.mark.asyncio
async def test_notification_and_unknown_action_never_dispatch(setup):
    engine, orch, _, _ = setup
    assert (await engine._execute_automation(message("notification")))["notification_only"] is True
    assert (await engine._execute_automation(message("purchase")))["success"] is False
    orch.executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_manifest_denial_is_enforced(setup):
    engine, orch, _, supervisor = setup
    endpoint = next(ep for ep in orch.skills.skills["smart_home_hue"].endpoints if ep.id == "call_service")
    endpoint.requires_user_approval = False
    endpoint.safety_tier = "deny"
    result = await engine._execute_automation(message())
    assert result["status"] == "PermissionOutcome::Deny"
    orch.executor.execute.assert_not_awaited()
    assert supervisor.record.call_args.kwargs["decision"] == "denied"


@pytest.mark.asyncio
async def test_foreign_session_grant_does_not_authorize_primary(setup):
    engine, orch, approvals, _ = setup
    approvals.grant_approval("smart_home_hue__call_service", "other-thread", scope="session")
    assert (await engine._execute_automation(message()))["status"] == "pending_approval"
    orch.executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_pending_suggestion_does_not_replay_action(setup):
    engine, orch, _, _ = setup
    first = await engine._execute_automation(message())
    second = await engine._execute_automation(message())
    assert first["request_id"] == second["request_id"]
    orch.executor.execute.assert_not_awaited()
    assert len(orch.tool_runner.pending_for_session("primary-test")) == 1


@pytest.mark.asyncio
async def test_missing_runner_and_unknown_receipt_fail_closed(setup):
    engine, orch, _, supervisor = setup
    orch.tool_runner = None
    assert (await engine._execute_automation(message()))["success"] is False
    orch.executor.execute.assert_not_awaited()
    dispatch = AsyncMock(return_value=None)
    orch.tool_runner = SimpleNamespace(execute_tool_call_for_llm=dispatch, autonomy_mode="hybrid")
    assert (await engine._execute_automation(message()))["success"] is False
    assert supervisor.record.call_args.kwargs["decision"] == "error"
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_dispatch_never_retried_or_claimed_success(setup):
    import asyncio
    engine, orch, approvals, supervisor = setup
    approvals.grant_approval("smart_home_hue__call_service", "primary-test", scope="session")
    orch.executor.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await engine._execute_automation(message())
    assert orch.executor.execute.await_count == 1
    supervisor.record.assert_not_called()


@pytest.mark.asyncio
async def test_state_primary_fallback_requires_same_orchestrator(setup, monkeypatch):
    engine, orch, _, _ = setup
    from api.state import state
    del orch._primary_session_id_resolver
    monkeypatch.setattr(state, "primary_session_id", "primary-test")
    monkeypatch.setattr(state, "orchestrator", object())
    assert (await engine._execute_automation(message()))["success"] is False
    orch.executor.execute.assert_not_awaited()
    monkeypatch.setattr(state, "orchestrator", orch)
    assert (await engine._execute_automation(message()))["status"] == "pending_approval"
    orch.executor.execute.assert_not_awaited()
