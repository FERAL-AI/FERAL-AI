"""Computer-use actions retain central authority with inert effect boundaries."""

from collections import deque
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from models.skill_manifest import SkillManifest
from skills.call_context import bind_context, current_context
from skills.executor import SkillExecutor
from skills.impl.agentic_computer_use import AgenticComputerUseSkill


@pytest.fixture
def authority(monkeypatch, tmp_path):
    from agents.tool_runner import ToolRunner
    from security.exec_approvals import ApprovalManager
    from security.trust_ledger import TrustLedger
    from skills.registry import SkillRegistry

    manifests = {}
    for name in ("gui_computer_use", "desktop_control"):
        path = Path(__file__).parents[1] / "skills" / "manifests" / f"{name}.json"
        manifests[name] = SkillManifest(**json.loads(path.read_text()))
    effects = []
    policies = []
    control = {"refusal": None}

    class Runner(ToolRunner):
        def enforce_plan_mode(self, tool_name, session_id):
            policies.append(("plan", tool_name, session_id, current_context()))
            return None

        def enforce_safety(self, tool_name, args, session_id="", surface="websocket", **kwargs):
            policies.append(("safety", tool_name, session_id, current_context()))
            return control["refusal"]

    # Execute the actual central gate/rate/default path. Only the effect adapter
    # is inert; constructing no HTTP client avoids even dormant network wiring.
    executor = SkillExecutor.__new__(SkillExecutor)
    executor._skill_call_times = {name: deque() for name in manifests}

    async def inert_effect(tool_name, args, manifest, endpoint):
        effects.append((tool_name, dict(args), current_context()))
        return {"success": True, "status_code": 200,
                "data": {"message": "inert action accepted"}, "error": None}

    executor._execute_inner = AsyncMock(side_effect=inert_effect)
    monkeypatch.setattr("skills.executor._record_action_reversal", lambda *a: None)
    registry = SkillRegistry()
    for manifest in manifests.values():
        registry.register(manifest)
    orch = SimpleNamespace(skills=registry, executor=executor, _mcp_client=None, daemons={},
                           _active_turns={"exact-owner": [{"_feral_turn_id": "turn-owner"}]},
                           _session_surfaces={}, _send_text=AsyncMock())
    runner = Runner(orch, trust_ledger=TrustLedger(persist=False),
                    approval_manager=ApprovalManager(db_path=str(tmp_path / "fixture-approvals.sqlite")))
    orch.tool_runner = runner
    state = SimpleNamespace(skill_executor=executor, tool_runner=runner, skill_registry=registry)
    monkeypatch.setitem(sys.modules, "api.state", SimpleNamespace(state=state))
    raw = AsyncMock()
    monkeypatch.setattr("skills.impl.get_implementation", lambda _: SimpleNamespace(execute=raw))
    return SimpleNamespace(state=state, effects=effects, policies=policies,
                           control=control, raw=raw, skill=AgenticComputerUseSkill())


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["brain", "executor", "runner", "manifest", "endpoint"])
async def test_missing_authority_never_dispatches(authority, monkeypatch, missing):
    if missing == "brain":
        monkeypatch.delitem(sys.modules, "api.state")
    elif missing == "executor":
        authority.state.skill_executor = None
    elif missing == "runner":
        authority.state.tool_runner = None
    elif missing == "manifest":
        authority.state.skill_registry.skills.clear()
    else:
        authority.state.skill_registry.skills["gui_computer_use"].endpoints = []
    with bind_context(session_id="review-owner", surface="voice"):
        result = await authority.skill._execute_gated("mouse_click", {"x": 12, "y": 23})
    assert result["success"] is False
    assert result["error_code"] == "computer_use_authority_unavailable"
    assert authority.effects == []
    authority.raw.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["", " owner", "owner\x00", "owner\x85", "x" * 1025])
async def test_no_session_inference_or_identity_coercion(authority, session):
    with bind_context(session_id=session):
        result = await authority.skill._execute_gated("mouse_click", {"x": 12, "y": 23})
    assert result["success"] is False
    assert authority.effects == []
    assert authority.policies == []


@pytest.mark.asyncio
async def test_registered_action_uses_actual_gate_and_restores_parent(authority):
    with bind_context(session_id="exact-owner", surface="voice", turn_id="turn-owner",
                      call_id="call-owner", plan_mode=True,
                      tool_name="agentic_computer_use__execute_task") as parent:
        result = await authority.skill._execute_gated("mouse_click", {"x": 123, "y": 456})
        assert current_context() == parent
    assert result["success"] is True
    name, args, context = authority.effects[0]
    assert name == "gui_computer_use__mouse_click" and args == {"x": 123, "y": 456}
    assert (context.session_id, context.surface, context.turn_id, context.plan_mode) == (
        "exact-owner", "voice", "turn-owner", True)
    assert context.call_id.startswith("computer-use-") and context.call_id != "call-owner"
    assert [p[0] for p in authority.policies] == ["plan", "safety", "plan", "safety"]
    assert all(p[2] == "exact-owner" for p in authority.policies)
    authority.raw.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [
    {"success": False, "status_code": 403, "data": None, "error": "denied"},
    {"success": False, "status_code": 202, "data": {"request_id": "exact-review"},
     "error": "pending_approval", "pending_approval": True},
])
async def test_policy_refusal_preserved_without_fallback(authority, refusal):
    authority.control["refusal"] = refusal
    with bind_context(session_id="exact-owner", surface="voice"):
        result = await authority.skill._execute_gated("mouse_click", {"x": 1, "y": 2})
    assert result == refusal
    assert authority.effects == []
    authority.raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_failure_never_reaches_raw_adapter(authority):
    def broken(*args, **kwargs):
        raise RuntimeError("inert policy unavailable")
    authority.state.tool_runner.enforce_safety = broken
    with bind_context(session_id="exact-owner"):
        with pytest.raises(RuntimeError, match="inert policy unavailable"):
            await authority.skill._execute_gated("mouse_click", {"x": 1, "y": 2})
    assert authority.effects == []
    authority.raw.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "open -a Finder; echo inert", "open -a Finder && echo inert",
    "open -a Finder | cat", "open -a '$(echo Finder)'", "open -a `echo Finder`",
    "open -a Finder\necho inert", "open -a Finder > /tmp/inert",
    "open -a Finder --args inert", "open -a 'Finder\" to activate'",
    "osascript -e 'tell application \"Finder\" to activate'", "screencapture /tmp/inert",
    "/tmp/open -a Finder", "open ~/Desktop", "open -a ''",
])
async def test_shell_syntax_or_unsupported_commands_never_dispatch(authority, monkeypatch, command):
    shell = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_shell", shell)
    with bind_context(session_id="exact-owner"):
        message = await authority.skill._do_shell(command)
    assert "blocked" in message
    assert authority.effects == []
    shell.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["open -a 'Google Chrome'", "/usr/bin/open -a 'Google Chrome'"])
async def test_open_app_reuses_registered_reviewed_tool(authority, monkeypatch, command):
    shell = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_shell", shell)
    with bind_context(session_id="exact-owner", surface="voice"):
        message = await authority.skill._do_shell(command)
    assert "successful tool result" in message
    name, args, context = authority.effects[0]
    assert name == "desktop_control__open_app"
    assert args == {"script": 'tell application "Google Chrome" to activate'}
    assert context.session_id == "exact-owner" and context.surface == "voice"
    shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_drag_reports_unavailable_without_pointer_events(authority, monkeypatch):
    pointer = SimpleNamespace(moveTo=AsyncMock(), mouseDown=AsyncMock(), mouseUp=AsyncMock())
    monkeypatch.setitem(sys.modules, "pyautogui", pointer)
    with bind_context(session_id="exact-owner"):
        message = await authority.skill._execute_action({"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]})
    assert "drag failed" in message and "registered" in message
    assert authority.effects == []
    pointer.moveTo.assert_not_awaited()
    pointer.mouseDown.assert_not_awaited()
    pointer.mouseUp.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pending", "denied", "raised", "invalid_result", "invalid_action"])
async def test_loop_stops_before_generated_done_after_unsettled_action(authority, monkeypatch, mode):
    if mode == "pending":
        authority.control["refusal"] = {"success": False, "status_code": 202,
            "pending_approval": True, "data": {"request_id": "review-once"}, "error": "pending_approval"}
    elif mode == "denied":
        authority.control["refusal"] = {"success": False, "status_code": 403, "data": None, "error": "denied"}
    elif mode == "raised":
        authority.state.skill_executor._execute_inner.side_effect = RuntimeError("inert adapter lost response")
    elif mode == "invalid_result":
        authority.state.skill_executor._execute_inner.side_effect = None
        authority.state.skill_executor._execute_inner.return_value = None
    monkeypatch.setattr(authority.skill, "_get_vlm", AsyncMock(return_value=object()))
    capture = AsyncMock(return_value="inert-image")
    first = {"action": "unsupported"} if mode == "invalid_action" else {"action": "click", "x": 1, "y": 2}
    decision = AsyncMock(side_effect=[first,
                                     {"action": "done", "summary": "generated claim"}])
    monkeypatch.setattr(authority.skill, "_capture_screen", capture)
    monkeypatch.setattr(authority.skill, "_decide_action", decision)
    with bind_context(session_id="exact-owner", surface="voice"):
        result = await authority.skill.execute("execute_task", {"task": "inert task"}, {})
    assert result["success"] is False and result["data"]["completed"] is False
    assert decision.await_count == capture.await_count == 1
    assert result["data"]["steps"] == 1
    if mode == "pending":
        assert result["pending_approval"] is True
        assert result["data"]["action_result"] == {"request_id": "review-once"}
    elif mode in ("raised", "invalid_result"):
        assert result["outcome"] == "unknown"
    elif mode == "invalid_action":
        assert authority.effects == []
    authority.raw.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["session", "brain", "executor", "runner", "capture_manifest", "capture_endpoint"])
async def test_public_task_refuses_before_capture_or_model_when_authority_missing(authority, monkeypatch, missing):
    if missing == "brain":
        monkeypatch.delitem(sys.modules, "api.state")
    elif missing == "executor":
        authority.state.skill_executor = None
    elif missing == "runner":
        authority.state.tool_runner = None
    elif missing == "capture_manifest":
        authority.state.skill_registry.skills.clear()
    elif missing == "capture_endpoint":
        authority.state.skill_registry.skills["gui_computer_use"].endpoints = []
    capture, model = AsyncMock(), AsyncMock()
    monkeypatch.setattr(authority.skill, "_capture_screen", capture)
    monkeypatch.setattr(authority.skill, "_get_vlm", model)
    with bind_context(session_id="" if missing == "session" else "exact-owner"):
        result = await authority.skill.execute("execute_task", {"task": "inert task"}, {})
    assert result["success"] is False
    capture.assert_not_awaited()
    model.assert_not_awaited()
    assert authority.effects == []


@pytest.mark.asyncio
async def test_capture_uses_canonical_executor_and_keeps_owner(authority):
    authority.state.skill_executor._execute_inner.side_effect = None
    authority.state.skill_executor._execute_inner.return_value = {
        "success": True, "status_code": 200,
        "data": {"image_base64": "inert-jpeg-encoding", "format": "jpeg"}, "error": None}
    with bind_context(session_id="exact-owner", surface="voice"):
        image = await authority.skill._capture_screen(stop_on_refusal=True)
    assert image == "inert-jpeg-encoding"
    args = authority.state.skill_executor._execute_inner.await_args.args
    assert args[0] == "gui_computer_use__screenshot"
    assert authority.policies[0][2] == "exact-owner"
    authority.raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_capture_never_runs_vision_reasoning(authority, monkeypatch):
    authority.control["refusal"] = {"success": False, "status_code": 403,
                                    "data": None, "error": "capture denied"}
    monkeypatch.setattr(authority.skill, "_get_vlm", AsyncMock(return_value=object()))
    decision = AsyncMock()
    monkeypatch.setattr(authority.skill, "_decide_action", decision)
    with bind_context(session_id="exact-owner", surface="voice"):
        result = await authority.skill.execute("execute_task", {"task": "inert task"}, {})
    assert result["success"] is False and result["error"] == "capture denied"
    assert result["data"]["steps"] == 0
    assert authority.effects == []
    decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_dispatch_is_not_converted_to_success_or_retried(authority, monkeypatch):
    authority.state.skill_executor._execute_inner.side_effect = asyncio.CancelledError
    monkeypatch.setattr(authority.skill, "_get_vlm", AsyncMock(return_value=object()))
    monkeypatch.setattr(authority.skill, "_capture_screen", AsyncMock(return_value="inert-image"))
    decision = AsyncMock(side_effect=[{"action": "click", "x": x, "y": 2} for x in range(20)])
    monkeypatch.setattr(authority.skill, "_decide_action", decision)
    with bind_context(session_id="exact-owner", tool_name="outer-task") as parent:
        with pytest.raises(asyncio.CancelledError):
            await authority.skill.execute("execute_task", {"task": "inert task"}, {})
        assert current_context() == parent
    assert decision.await_count == authority.state.skill_executor._execute_inner.await_count == 1


@pytest.mark.asyncio
async def test_successful_actions_keep_the_existing_iteration_bound(authority, monkeypatch):
    monkeypatch.setattr(authority.skill, "_get_vlm", AsyncMock(return_value=object()))
    monkeypatch.setattr(authority.skill, "_capture_screen", AsyncMock(return_value="inert-image"))
    decision = AsyncMock(side_effect=[{"action": "click", "x": x, "y": 2} for x in range(20)])
    monkeypatch.setattr(authority.skill, "_decide_action", decision)
    monkeypatch.setattr("skills.impl.agentic_computer_use.asyncio.sleep", AsyncMock())
    with bind_context(session_id="exact-owner"):
        result = await authority.skill.execute("execute_task", {"task": "inert task", "max_steps": 100}, {})
    assert result["success"] is False
    assert result["data"]["completed"] is False
    assert result["data"]["steps"] == decision.await_count == len(authority.effects) == 15


@pytest.mark.asyncio
async def test_actual_tool_runner_plan_and_review_cannot_be_skipped(authority, monkeypatch, tmp_path):
    from agents.tool_runner import ToolRunner
    from security.exec_approvals import ApprovalManager
    from security.trust_ledger import TrustLedger
    from skills.registry import SkillRegistry

    monkeypatch.setenv("FERAL_AUTONOMY", "strict")
    registry = SkillRegistry()
    for manifest in authority.state.skill_registry.skills.values():
        registry.register(manifest)
    orch = SimpleNamespace(skills=registry, executor=authority.state.skill_executor,
                           _active_turns={}, _session_surfaces={}, _mcp_client=None,
                           daemons={}, _send_text=AsyncMock())
    runner = ToolRunner(orch, autonomy_mode="strict", trust_ledger=TrustLedger(persist=False),
                        approval_manager=ApprovalManager(db_path=str(tmp_path / "approvals.sqlite")))
    authority.state.tool_runner = runner
    authority.state.skill_registry = registry
    orch.tool_runner = runner
    runner.plan_mode.enter("exact-owner")
    with bind_context(session_id="exact-owner", surface="websocket"):
        plan = await authority.skill._execute_gated("mouse_click", {"x": 1, "y": 2})
    assert plan["error_code"] == "plan_mode_blocked"
    assert authority.effects == []
    runner.plan_mode.exit("exact-owner")
    with bind_context(session_id="exact-owner", surface="websocket"):
        pending = await authority.skill._execute_gated("mouse_click", {"x": 1, "y": 2})
    assert pending["status"] == "pending_approval"
    assert authority.effects == []
    assert runner.approve_pending(pending["request_id"], session_id="foreign", exact_once=True) is None
    exact = runner.approve_pending(pending["request_id"], session_id="exact-owner", exact_once=True)
    result = await runner.execute_tool_call_for_llm("exact-owner", {
        "id": "inert-reviewed-click", "name": "gui_computer_use__mouse_click",
        "args": {"x": 1, "y": 2}}, registry.get_all_tools(), approval=exact["approval"])
    assert result["success"] is True
    assert len(authority.effects) == 1
    assert authority.effects[0][2].session_id == "exact-owner"
    authority.raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_admission_never_grants_a_different_inner_action(authority, monkeypatch, tmp_path):
    from agents.tool_runner import ToolRunner, _bind_executor_admission
    from security.exec_approvals import ApprovalManager
    from security.trust_ledger import TrustLedger

    monkeypatch.setenv("FERAL_AUTONOMY", "strict")
    orch = SimpleNamespace(skills=authority.state.skill_registry, executor=authority.state.skill_executor,
                           _active_turns={}, _session_surfaces={}, _mcp_client=None,
                           daemons={}, _send_text=AsyncMock())
    runner = ToolRunner(orch, trust_ledger=TrustLedger(persist=False),
                        approval_manager=ApprovalManager(db_path=str(tmp_path / "approvals.sqlite")))
    authority.state.tool_runner = runner
    with bind_context(session_id="exact-owner", surface="websocket"), _bind_executor_admission(object()):
        result = await authority.skill._execute_gated("mouse_click", {"x": 1, "y": 2})
    assert result["status"] == "pending_approval"
    assert authority.effects == []
    authority.raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_reviewed_outer_task_does_not_authorize_its_inner_click(authority, monkeypatch, tmp_path):
    from agents.tool_runner import ToolRunner
    from security.exec_approvals import ApprovalManager
    from security.trust_ledger import TrustLedger

    monkeypatch.setenv("FERAL_AUTONOMY", "strict")
    registry = authority.state.skill_registry
    path = Path(__file__).parents[1] / "skills/manifests/agentic_computer_use.json"
    registry.register(SkillManifest(**json.loads(path.read_text())))
    # Explicit fixture policy exercises AUTO capture and exact CONFIRM input.
    # No production manifest or policy is loosened by the test.
    for endpoint in registry.skills["gui_computer_use"].endpoints:
        if endpoint.id == "screenshot":
            endpoint.read_only_hint = True
            endpoint.safety_tier = "auto"
        elif endpoint.id == "mouse_click":
            endpoint.requires_user_approval = True
            endpoint.safety_tier = "confirm"
    orch = SimpleNamespace(skills=registry, executor=authority.state.skill_executor,
                           _active_turns={"exact-owner": [{"_feral_turn_id": "outer-turn"}]},
                           _session_surfaces={}, _mcp_client=None, daemons={}, _send_text=AsyncMock())
    runner = ToolRunner(orch, autonomy_mode="strict", trust_ledger=TrustLedger(persist=False),
                        approval_manager=ApprovalManager(db_path=str(tmp_path / "outer-approvals.sqlite")))
    orch.tool_runner = runner
    authority.state.tool_runner = runner
    captures, clicks = [], []

    async def boundary(tool_name, args, manifest, endpoint):
        if tool_name == "agentic_computer_use__execute_task":
            return await authority.skill.execute("execute_task", args, {})
        if tool_name == "gui_computer_use__screenshot":
            captures.append(current_context())
            return {"success": True, "status_code": 200,
                    "data": {"image_base64": "inert-image", "format": "jpeg"}, "error": None}
        clicks.append(tool_name)
        return {"success": True, "data": None, "error": None}

    authority.state.skill_executor._execute_inner.side_effect = boundary
    model = AsyncMock(return_value=object())
    decision = AsyncMock(side_effect=[{"action": "click", "x": 4, "y": 7},
                                     {"action": "done", "summary": "generated done"}])
    monkeypatch.setattr(authority.skill, "_get_vlm", model)
    monkeypatch.setattr(authority.skill, "_decide_action", decision)
    call = {"id": "outer-call", "name": "agentic_computer_use__execute_task",
            "args": {"task": "inert reviewed task", "max_steps": 2}}
    pending = await runner.execute_tool_call_for_llm("exact-owner", call, registry.get_all_tools())
    assert pending["status"] == "pending_approval" and captures == clicks == []
    approval = runner.approve_pending(pending["request_id"], session_id="exact-owner", exact_once=True)
    result = await runner.execute_tool_call_for_llm("exact-owner", call, registry.get_all_tools(), approval=approval["approval"])
    assert result["success"] is False
    assert result["status"] == "pending_approval"
    assert len(captures) == 1 and clicks == []
    assert captures[0].session_id == "exact-owner" and captures[0].turn_id == "outer-turn"
    assert decision.await_count == 1
    assert runner.list_pending()[0]["tool_name"] == "gui_computer_use__mouse_click"
    assert runner.list_pending()[0]["args"] == {"x": 4, "y": 7}
    assert orch._send_text.await_count >= 2
