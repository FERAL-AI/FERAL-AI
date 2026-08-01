"""Late-registered skills must be callable without a process restart.

``ToolRunner._get_dispatch_validator`` memoises a ``ToolDispatchValidator``
on the first tool call, and the validator snapshots ``registry.skills`` plus
a precomputed schema per endpoint in its constructor. Anything registered
afterwards (marketplace installs, ``SkillRegistry.reload_skill``, Tool
Genesis output under ``~/.feral/skills/generated/``, hot-plugged hardware
devices) validated as ``unknown_endpoint`` forever, and the tool call was
hard-blocked before it reached the executor, even though ``registry.skills``
contained the manifest.

The fix is a generation counter on ``SkillRegistry`` bumped in
``register()``, which the validator uses to detect its own staleness.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import skills.impl  # noqa: F401,E402  register backing skills

from agents.tool_dispatch_validator import ToolDispatchValidator  # noqa: E402
from agents.tool_runner import ToolRunner  # noqa: E402
from models.skill_manifest import (  # noqa: E402
    BrandProfile,
    EndpointParam,
    SkillEndpoint,
    SkillManifest,
)
from skills.registry import SkillRegistry  # noqa: E402


def _late_manifest(skill_id: str = "late_arrival") -> SkillManifest:
    """Stand-in for a marketplace / Tool Genesis skill registered mid-session."""
    return SkillManifest(
        skill_id=skill_id,
        brand=BrandProfile(name="Late Arrival"),
        description="Registered after the validator was first built",
        endpoints=[
            SkillEndpoint(
                id="ping",
                method="POST",
                url="https://example.invalid/ping",
                description="Ping the late skill",
                params=[
                    EndpointParam(
                        name="message",
                        type="string",
                        required=True,
                        description="what to send",
                    ),
                ],
            ),
        ],
    )


def _make_runner(registry: SkillRegistry) -> ToolRunner:
    orch = MagicMock()
    orch.skills = registry
    orch.executor = MagicMock()
    orch.executor.execute = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {}, "error": None},
    )
    orch._mcp_client = None
    orch.daemons = {}
    orch._session_surfaces = {}
    return ToolRunner(orch, autonomy_mode="loose")


class TestRegistryGeneration:
    def test_generation_starts_at_zero_and_bumps_on_register(self):
        reg = SkillRegistry()
        assert reg.generation == 0
        reg.register(_late_manifest())
        assert reg.generation == 1
        # Re-registering the same id is still a change worth rebuilding for:
        # reload_skill hot-swaps a manifest under an existing skill_id.
        reg.register(_late_manifest())
        assert reg.generation == 2

    def test_load_builtin_skills_bumps_generation(self):
        reg = SkillRegistry()
        reg.load_builtin_skills()
        assert reg.generation >= len(reg.skills)


class TestValidatorRefresh:
    def test_validator_picks_up_skill_registered_after_construction(self):
        reg = SkillRegistry()
        reg.load_builtin_skills()
        validator = ToolDispatchValidator(registry=reg)

        # Not known yet: the snapshot predates registration.
        assert validator.validate("late_arrival", "ping", {"message": "hi"}).ok is False

        reg.register(_late_manifest())

        assert validator.refresh_if_stale() is True
        result = validator.validate("late_arrival", "ping", {"message": "hi"})
        assert result.ok is True, result.reason
        assert validator.get_endpoint_schema("late_arrival", "ping") is not None

    def test_refresh_is_a_noop_when_nothing_changed(self):
        reg = SkillRegistry()
        reg.load_builtin_skills()
        validator = ToolDispatchValidator(registry=reg)
        assert validator.refresh_if_stale() is False

    def test_explicit_manifest_snapshot_is_never_replaced(self):
        """A validator built with ``manifests=`` is the caller's fixed view."""
        reg = SkillRegistry()
        reg.load_builtin_skills()
        pinned = {"late_arrival": _late_manifest()}
        validator = ToolDispatchValidator(registry=reg, manifests=pinned)

        reg.register(_late_manifest("another_one"))

        assert validator.refresh_if_stale() is False
        assert validator.validate("another_one", "ping", {"message": "hi"}).ok is False
        assert validator.validate("late_arrival", "ping", {"message": "hi"}).ok is True


@pytest.mark.asyncio
class TestLateSkillDispatches:
    async def test_late_registered_skill_is_callable(self):
        reg = SkillRegistry()
        reg.load_builtin_skills()
        runner = _make_runner(reg)

        # Warm the memoised validator with a builtin call that predates the
        # new skill. This is the snapshot that used to be frozen forever.
        first = await runner.execute_tool_call_for_llm(
            "s1",
            {
                "name": "feral_reminders__list",
                "args": {"include_completed": False},
                "id": "tc0",
            },
            [],
        )
        assert first.get("success") is True, first
        assert runner._dispatch_validator is not None
        runner._orch.executor.execute.reset_mock()

        reg.register(_late_manifest())

        result = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "late_arrival__ping", "args": {"message": "hi"}, "id": "tc1"},
            [],
        )

        assert result.get("error_code") != "unknown_endpoint", result
        assert result.get("success") is True, result
        runner._orch.executor.execute.assert_called_once()

    async def test_reload_skill_style_hot_swap_is_callable(self):
        """``reload_skill`` pops then re-registers under the same id."""
        reg = SkillRegistry()
        reg.load_builtin_skills()
        reg.register(_late_manifest())
        runner = _make_runner(reg)

        ok = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "late_arrival__ping", "args": {"message": "hi"}, "id": "tc1"},
            [],
        )
        assert ok.get("success") is True

        # Hot-swap: same skill_id, new endpoint added.
        swapped = _late_manifest()
        swapped.endpoints.append(
            SkillEndpoint(
                id="pong",
                method="POST",
                url="https://example.invalid/pong",
                description="Added by the hot reload",
                params=[],
            ),
        )
        reg.skills.pop("late_arrival", None)
        reg.register(swapped)

        result = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "late_arrival__pong", "args": {}, "id": "tc2"},
            [],
        )
        assert result.get("error_code") != "unknown_endpoint", result
        assert result.get("success") is True, result
