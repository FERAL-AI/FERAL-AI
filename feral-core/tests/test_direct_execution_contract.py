"""A failed model must not turn the core tool bag into a guessed action."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.direct_execution import direct_execute, extract_args_from_text
from agents.tool_dispatch_validator import ToolDispatchValidator
from agents.tool_runner import ToolRunner
from models.skill_manifest import BrandProfile, EndpointParam, SkillEndpoint, SkillManifest
from skills.call_context import current_context
from skills.registry import SkillRegistry


def manifest(skill_id="web_search", triggers=None, endpoints=None):
    return SkillManifest(
        skill_id=skill_id, brand=BrandProfile(name="Probe"), description="test",
        trigger_phrases=triggers or ["what's the weather"],
        endpoints=endpoints or [SkillEndpoint(
            id="web_search", method="POST", url="https://example.invalid/weather", description="read",
            params=[EndpointParam(name="query", type="string", required=True)],
            read_only_hint=True, safety_tier="safe",
        )],
    )


def orchestrator(skills):
    orch = MagicMock()
    orch.skills = SkillRegistry.__new__(SkillRegistry)
    orch.skills.skills = {skill.skill_id: skill for skill in skills}
    orch._send_text = AsyncMock()
    orch.send = AsyncMock()
    orch.executor.execute = AsyncMock(return_value={"success": True, "data": {"temperature": 20}})
    orch.genui.generate.return_value = {"type": "Text", "value": "20"}
    orch._mcp_client = None
    orch.daemons = {}
    orch._session_surfaces = {}
    orch._active_turns = {}
    orch.tool_runner = ToolRunner(orch, autonomy_mode="loose")
    orch.tool_runner._dispatch_validator = ToolDispatchValidator(manifests=orch.skills.skills)
    return orch


async def test_write_request_does_not_execute_weather_or_first_bash_from_tool_bag():
    weather = manifest("weather_current")
    coding = manifest("coding_tools", ["write file"], [
        SkillEndpoint(id="bash", method="PYTHON", url="", description="shell", safety_tier="confirm"),
        SkillEndpoint(id="read_file", method="PYTHON", url="", description="read", read_only_hint=True),
    ])
    orch = orchestrator([weather, coding])
    await direct_execute(orch, "owner-a", "Write ACCEPTANCE_ONLY to a file", [weather, coding])
    orch.executor.execute.assert_not_awaited()
    assert "No fallback action has run" in orch._send_text.call_args.args[1]


@pytest.mark.parametrize("prompt", ["Tell me a story about what's the weather", "what's the weatherproof case price?"])
async def test_trigger_must_match_literal_prefix_with_word_boundary(prompt):
    skill = manifest()
    orch = orchestrator([skill])
    await direct_execute(orch, "owner-a", prompt, [skill])
    orch.executor.execute.assert_not_awaited()


async def test_ambiguous_skill_trigger_refuses_before_dispatch():
    skills = [manifest(), manifest("notes_memory")]
    orch = orchestrator(skills)
    await direct_execute(orch, "owner-a", "what's the weather in Cairo", skills)
    orch.executor.execute.assert_not_awaited()


async def test_explicit_read_uses_real_dispatcher_and_trusted_session_context():
    skill = manifest()
    orch = orchestrator([skill])
    observed = []

    async def execute(**kwargs):
        observed.append(current_context())
        return {"success": True, "data": {"temperature": 20}}

    orch.executor.execute.side_effect = execute
    await direct_execute(orch, "owner-a", "Please what's the weather in Cairo", [skill])
    assert len(observed) == 1
    assert observed[0].session_id == "owner-a"
    assert observed[0].tool_name == "web_search__web_search"
    assert observed[0].call_id.startswith("direct-")
    assert current_context().session_id == ""
    orch.genui.generate.assert_called_once()


@pytest.mark.parametrize("result", [
    {"status": "pending_approval", "request_id": "review-a"},
    {"status": "denied", "error": "blocked"},
    {"error": "invalid args"},
    None,
])
async def test_non_success_envelopes_never_crash_or_render_completion(result):
    skill = manifest()
    orch = orchestrator([skill])
    orch.tool_runner = SimpleNamespace(execute_tool_call_for_llm=AsyncMock(return_value=result))
    await direct_execute(orch, "owner-a", "what's the weather", [skill])
    orch.genui.generate.assert_not_called()
    if isinstance(result, dict) and result.get("status") == "pending_approval":
        assert "sent for your approval" in orch._send_text.call_args.args[1]


@pytest.mark.parametrize("kind", ["untrusted", "approval", "missing-args", "daemon", "missing-runner"])
async def test_unsupported_fallbacks_do_not_bypass_dispatch_policy(kind):
    skill = manifest("third_party_probe" if kind == "untrusted" else "web_search")
    if kind == "approval":
        skill.endpoints[0].requires_user_approval = True
    if kind == "missing-args":
        skill.endpoints[0].params.append(EndpointParam(name="account_id", type="string", required=True))
    if kind == "daemon":
        skill.requires_daemon = True
    orch = orchestrator([skill])
    if kind == "missing-runner":
        orch.tool_runner = None
    await direct_execute(orch, "owner-a", "what's the weather", [skill])
    orch.executor.execute.assert_not_awaited()
    orch._execute_daemon_command.assert_not_called()


async def test_plan_mode_still_controls_direct_dispatch():
    skill = manifest()
    orch = orchestrator([skill])
    orch.tool_runner.enforce_plan_mode = MagicMock(return_value={"error": "plan mode blocked"})
    await direct_execute(orch, "owner-a", "what's the weather", [skill])
    orch.executor.execute.assert_not_awaited()
    orch.genui.generate.assert_not_called()


def test_declared_parameter_defaults_survive():
    endpoint = manifest().endpoints[0]
    endpoint.params = [
        EndpointParam(name="enabled", type="boolean", required=False, default="false"),
        EndpointParam(name="limit", type="integer", required=False, default="0"),
    ]
    assert extract_args_from_text("read", endpoint) == {"enabled": "false", "limit": "0"}
