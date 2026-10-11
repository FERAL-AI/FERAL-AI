"""Manifest escalation must reach central dispatch without read-name bypass."""
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agents.tool_runner import ToolRunner
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from security.exec_approvals import ApprovalManager
from security.trust_ledger import TrustLedger
from skills.base import BaseSkill
from skills.executor import SkillExecutor
from skills.impl import SKILL_IMPLEMENTATIONS
from skills.registry import SkillRegistry


class LocalRead(BaseSkill):
    def __init__(self, skill_id):
        super().__init__(skill_id)
        self.calls = 0

    async def execute(self, endpoint_id, args, vault):
        self.calls += 1
        return {"success": True, "status_code": 200, "data": 42, "error": None}


@pytest_asyncio.fixture
async def runtime(monkeypatch, tmp_path):
    executors = []

    def make(*, mode="strict", skill_id="approval_precedence_fixture", endpoint_id="read", **metadata):
        monkeypatch.setenv("FERAL_AUTONOMY", mode)
        monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "on")
        manifest = SkillManifest(
            skill_id=skill_id, brand=BrandProfile(name="Controlled policy fixture"),
            description="Local deterministic policy test", endpoints=[SkillEndpoint(
                id=endpoint_id, method="PYTHON", url="", description="Return a local constant", **metadata,
            )],
        )
        registry = SkillRegistry()
        registry.register(manifest)
        implementation = LocalRead(skill_id)
        monkeypatch.setitem(SKILL_IMPLEMENTATIONS, skill_id, implementation)
        executor = SkillExecutor()
        executors.append(executor)
        notices = []

        async def notice(session, text):
            notices.append((session, text))

        orch = SimpleNamespace(skills=registry, executor=executor, _mcp_client=None,
                               daemons={}, _session_surfaces={}, _active_turns={}, _send_text=notice)
        runner = ToolRunner(
            orch, autonomy_mode=mode, trust_ledger=TrustLedger(persist=False),
            approval_manager=ApprovalManager(db_path=str(tmp_path / f"approvals-{len(executors)}.sqlite")),
        )
        orch.tool_runner = runner
        from api import state as state_module
        monkeypatch.setattr(state_module.state, "orchestrator", orch)
        call = {"id": "policy-fixture-call", "name": f"{skill_id}__{endpoint_id}", "args": {}}
        return SimpleNamespace(runner=runner, executor=executor, implementation=implementation,
                               registry=registry, manifest=manifest, call=call, notices=notices)

    yield make
    for executor in executors:
        await executor.close()


REVIEW_METADATA = [
    pytest.param({"requires_user_approval": True, "read_only_hint": True}, id="approval-and-read-hint"),
    pytest.param({"requires_user_approval": True, "read_only_hint": True, "safety_tier": "safe"},
                 id="approval-over-safe-tier"),
    pytest.param({"read_only_hint": True, "safety_tier": "confirm"}, id="confirm-over-read-hint"),
]


@pytest.mark.parametrize("mode", ["strict", "hybrid"])
@pytest.mark.parametrize("skill_id", ["approval_precedence_fixture", "notes_memory"])
@pytest.mark.parametrize("metadata", REVIEW_METADATA)
async def test_manifest_review_beats_read_name_and_contradictory_hints(runtime, mode, skill_id, metadata):
    rt = runtime(mode=mode, skill_id=skill_id, **metadata)
    decision = rt.runner.policy_for(rt.call["name"], {})
    assert decision.level == "confirm"
    result = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    assert result.get("status") == "pending_approval", (result, rt.implementation.calls)
    assert result.get("success") is not True
    assert result["safety_level"] == "confirm"
    assert result["policy_sources"]["manifest"]["read_only_hint"] is True
    assert rt.implementation.calls == 0
    assert rt.notices and rt.notices[0][0] == "owner"
    assert rt.runner.get_pending(result["request_id"])["session_id"] == "owner"


async def test_strict_manifest_review_is_exact_owner_bound_one_use(runtime):
    rt = runtime(requires_user_approval=True, read_only_hint=True)
    pending = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    request_id = pending["request_id"]
    assert rt.runner.approve_pending(request_id, session_id="foreign", exact_once=True) is None
    assert rt.runner.get_pending(request_id)["session_id"] == "owner"
    accepted = rt.runner.approve_pending(request_id, session_id="owner", exact_once=True)
    approval = accepted["approval"]
    result = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools(), approval=approval)
    assert result.get("success") is True and result["data"] == 42
    assert rt.implementation.calls == 1
    reused = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools(), approval=approval)
    assert reused["error_code"] == "invalid_approval" and rt.implementation.calls == 1
    assert rt.runner._approval_mgr.check_approval(rt.call["name"], "owner")[0] is False
    fresh = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    assert fresh["status"] == "pending_approval" and rt.implementation.calls == 1


async def test_exact_review_rejects_foreign_dispatch_and_cannot_be_reused(runtime):
    rt = runtime(requires_user_approval=True, read_only_hint=True)
    pending = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    accepted = rt.runner.approve_pending(pending["request_id"], session_id="owner", exact_once=True)
    approval = accepted["approval"]
    foreign = await rt.runner.execute_tool_call_for_llm("foreign", rt.call, rt.registry.get_all_tools(), approval=approval)
    assert foreign["error_code"] == "invalid_approval" and rt.implementation.calls == 0
    # Exact receipts allow one dispatch attempt, including a mismatched attempt.
    reused = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools(), approval=approval)
    assert reused["error_code"] == "invalid_approval" and rt.implementation.calls == 0


@pytest.mark.parametrize("mode", ["strict", "hybrid", "loose"])
async def test_manifest_deny_stays_first_despite_approval_read_hint_and_standing_grant(runtime, mode):
    rt = runtime(mode=mode, requires_user_approval=True, read_only_hint=True, safety_tier="deny")
    rt.runner.grant_session_approval(rt.call["name"], "owner")
    result = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    assert result["status"] == "PermissionOutcome::Deny"
    assert rt.implementation.calls == 0


async def test_exact_review_cannot_override_new_manifest_deny(runtime):
    rt = runtime(requires_user_approval=True, read_only_hint=True)
    pending = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    approved = rt.runner.approve_pending(pending["request_id"], session_id="owner", exact_once=True)
    rt.manifest.endpoints[0].safety_tier = "deny"
    result = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools(), approval=approved["approval"])
    assert result["status"] == "PermissionOutcome::Deny" and rt.implementation.calls == 0


@pytest.mark.parametrize("plan", [False, True])
async def test_strict_trusted_read_safe_auto_control_still_executes(runtime, plan):
    rt = runtime(skill_id="notes_memory", read_only_hint=True, safety_tier="safe")
    if plan:
        rt.runner.plan_mode.enter("owner")
    assert rt.runner.policy_for(rt.call["name"], {}).level == "auto"
    result = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    assert result.get("success") is True and result["data"] == 42
    assert rt.implementation.calls == 1 and rt.runner.list_pending() == []


async def test_untrusted_safe_hint_cannot_promote_unknown_endpoint(runtime):
    rt = runtime(endpoint_id="calculate", read_only_hint=True, safety_tier="safe")
    assert rt.runner.policy_for(rt.call["name"], {}).level == "confirm"
    result = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    assert result["status"] == "pending_approval" and rt.implementation.calls == 0


@pytest.mark.parametrize("metadata", REVIEW_METADATA)
async def test_explicit_loose_operator_override_is_preserved(runtime, metadata):
    rt = runtime(mode="loose", **metadata)
    assert rt.runner.policy_for(rt.call["name"], {}).level == "confirm"
    result = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    assert result.get("success") is True and rt.implementation.calls == 1


async def test_strict_standing_operator_grant_preserves_exact_session_scope(runtime):
    rt = runtime(requires_user_approval=True, read_only_hint=True)
    rt.runner.grant_session_approval(rt.call["name"], "owner")
    first = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    second = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    assert first.get("success") is second.get("success") is True
    assert rt.implementation.calls == 2
    foreign = await rt.runner.execute_tool_call_for_llm("foreign", rt.call, rt.registry.get_all_tools())
    assert foreign["status"] == "pending_approval" and rt.implementation.calls == 2


async def test_manifest_required_review_is_not_plan_safe(runtime):
    rt = runtime(requires_user_approval=True, read_only_hint=True)
    rt.runner.plan_mode.enter("owner")
    result = await rt.runner.execute_tool_call_for_llm("owner", rt.call, rt.registry.get_all_tools())
    assert result["error_code"] == "plan_mode_blocked" and rt.implementation.calls == 0


async def test_model_approval_flag_never_overrides_explicit_review(runtime):
    rt = runtime(requires_user_approval=True, read_only_hint=True)
    call = {**rt.call, "args": {"approved": True}}
    result = await rt.runner.execute_tool_call_for_llm("owner", call, rt.registry.get_all_tools())
    assert result["status"] == "pending_approval" and rt.implementation.calls == 0
