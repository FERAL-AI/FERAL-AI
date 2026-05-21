"""Unit tests for ToolDispatchValidator + tool_runner schema gate."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import skills.impl  # noqa: F401 — register backing skills

from agents.tool_dispatch_validator import ToolDispatchValidator  # noqa: E402
from agents.tool_runner import ToolRunner, MAX_LLM_TOOLS  # noqa: E402
from models.skill_manifest import (  # noqa: E402
    BrandProfile,
    EndpointParam,
    SkillEndpoint,
    SkillManifest,
)
from skills.registry import SkillRegistry  # noqa: E402


def _make_runner(autonomy: str = "loose") -> ToolRunner:
    orch = MagicMock()
    orch.skills = MagicMock()
    orch.skills.skills = {}
    orch.executor = MagicMock()
    orch.executor.execute = AsyncMock(return_value={"success": True, "data": {}})
    orch._mcp_client = None
    orch.daemons = {}
    orch._session_surfaces = {}
    return ToolRunner(orch, autonomy_mode=autonomy)


def _desktop_control_manifest() -> SkillManifest:
    return SkillManifest(
        skill_id="desktop_control",
        brand=BrandProfile(name="Desktop Control"),
        description="desktop",
        endpoints=[
            SkillEndpoint(
                id="system_info",
                method="POST",
                url="daemon://local/shell",
                description="System info",
                params=[
                    EndpointParam(
                        name="command",
                        type="string",
                        required=False,
                        default="echo test",
                        description="shell command",
                    ),
                ],
            ),
            SkillEndpoint(
                id="shell_command",
                method="POST",
                url="daemon://local/shell",
                description="Run shell",
                params=[
                    EndpointParam(
                        name="command",
                        type="string",
                        required=True,
                        description="shell command",
                    ),
                ],
            ),
        ],
    )


@pytest.fixture
def validator() -> ToolDispatchValidator:
    reg = SkillRegistry()
    reg.load_builtin_skills()
    return ToolDispatchValidator(registry=reg)


class TestToolDispatchValidator:
    def test_empty_args_required_field(self, validator: ToolDispatchValidator):
        manifest = _desktop_control_manifest()
        v = ToolDispatchValidator(manifests={"desktop_control": manifest})
        result = v.validate("desktop_control", "shell_command", {})
        assert not result.ok
        assert result.error_code == "missing_required_field"

    def test_mismatched_type(self, validator: ToolDispatchValidator):
        result = validator.validate(
            "feral_reminders", "create",
            {"title": "buy milk", "due": 12345},
        )
        assert not result.ok
        assert result.error_code == "invalid_args"

    def test_unknown_endpoint(self, validator: ToolDispatchValidator):
        result = validator.validate(
            "smart_home_hue", "list_lights", {"username": "x"},
        )
        assert not result.ok
        assert result.error_code == "unknown_endpoint"

    def test_happy_path_applies_defaults(self, validator: ToolDispatchValidator):
        manifest = _desktop_control_manifest()
        v = ToolDispatchValidator(manifests={"desktop_control": manifest})
        result = v.validate("desktop_control", "system_info", {})
        assert result.ok
        assert result.fixed_args is not None
        assert result.fixed_args.get("command") == "echo test"


class TestExecuteToolCallSchemaGate:
    async def test_empty_args_returns_tool_error(self):
        runner = _make_runner()
        manifest = _desktop_control_manifest()
        runner._orch.skills.skills = {"desktop_control": manifest}
        runner._dispatch_validator = ToolDispatchValidator(
            manifests={"desktop_control": manifest},
        )

        result = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "desktop_control__shell_command", "args": {}, "id": "tc1"},
            [],
        )
        assert result.get("is_error") is True
        assert result.get("error_code") == "missing_required_field"
        assert result.get("tool_call_id") == "tc1"
        runner._orch.executor.execute.assert_not_called()

    async def test_type_mismatch_returns_tool_error(self):
        runner = _make_runner()
        reg = SkillRegistry()
        reg.load_builtin_skills()
        runner._orch.skills = reg
        runner._dispatch_validator = ToolDispatchValidator(registry=reg)

        result = await runner.execute_tool_call_for_llm(
            "s1",
            {
                "name": "feral_reminders__create",
                "args": {"title": "x", "due": 999},
                "id": "tc2",
            },
            [],
        )
        assert result.get("is_error") is True
        assert result.get("error_code") == "invalid_args"

    async def test_unknown_endpoint_returns_tool_error(self):
        runner = _make_runner()
        reg = SkillRegistry()
        reg.load_builtin_skills()
        runner._orch.skills = reg
        runner._dispatch_validator = ToolDispatchValidator(registry=reg)

        result = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "smart_home_hue__list_lights", "args": {}, "id": "tc3"},
            [],
        )
        assert result.get("is_error") is True
        assert result.get("error_code") == "unknown_endpoint"

    async def test_happy_path_dispatches(self):
        runner = _make_runner()
        reg = SkillRegistry()
        reg.load_builtin_skills()
        runner._orch.skills = reg
        runner._dispatch_validator = ToolDispatchValidator(registry=reg)
        runner._orch.executor.execute = AsyncMock(
            return_value={"success": True, "data": {"items": []}},
        )

        result = await runner.execute_tool_call_for_llm(
            "s1",
            {
                "name": "feral_reminders__list",
                "args": {"include_completed": False},
                "id": "tc4",
            },
            [],
        )
        assert result.get("success") is True
        runner._orch.executor.execute.assert_called_once()


class TestToolListCap:
    def test_truncates_to_max_and_emits_metric(self):
        reg = MagicMock()
        tools_a = [{"function": {"name": f"skill_a__ep{i}"}} for i in range(40)]
        tools_b = [{"function": {"name": f"skill_b__ep{i}"}} for i in range(40)]
        reg.get_tools_for_skills = MagicMock(side_effect=[tools_a, tools_b])

        skill_a = MagicMock(skill_id="skill_a")
        skill_b = MagicMock(skill_id="skill_b")

        with patch("observability.metrics.increment") as inc:
            capped = ToolRunner.assemble_llm_tool_list(
                reg, [skill_a, skill_b], max_tools=MAX_LLM_TOOLS,
            )
        assert len(capped) == MAX_LLM_TOOLS
        inc.assert_called_once()
        assert inc.call_args[0][0] == "feral_tool_list_truncated_total"
