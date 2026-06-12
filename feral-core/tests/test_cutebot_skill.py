"""Tests for the CuteBot skill manifest, implementation, and safety policy."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hardware.protocol import HUPAction, HUPActionType, HUPResult  # noqa: E402
from security.safety_resolver import (  # noqa: E402
    LEVEL_AUTO,
    LEVEL_CONFIRM,
    LEVEL_DENY,
    resolve_policy,
)
from skills.impl.cutebot_skill import (  # noqa: E402
    DEVICE_ID,
    NOT_CONNECTED_ERROR,
    CuteBotSkill,
)
from skills.registry import SkillRegistry  # noqa: E402

MANIFEST_PATH = ROOT / "skills" / "manifests" / "cutebot.json"

EXPECTED_TOOLS = {
    "cutebot__follow_line",
    "cutebot__explore",
    "cutebot__drive",
    "cutebot__halt",
    "cutebot__status",
}

ENDPOINT_TO_CAPABILITY = {
    "follow_line": ("follow_line", HUPActionType.EXECUTE),
    "explore": ("explore", HUPActionType.EXECUTE),
    "drive": ("drive", HUPActionType.EXECUTE),
    "halt": ("halt", HUPActionType.EXECUTE),
    "status": ("read_telemetry", HUPActionType.READ),
}


class _MockDeviceRegistry:
    def __init__(self, *, connected: bool = True):
        self.connected = connected
        self.calls: list[HUPAction] = []

    def get_device(self, device_id: str):
        if self.connected and device_id == DEVICE_ID:
            return MagicMock(device_id=device_id)
        return None

    async def execute_action(self, action: HUPAction) -> HUPResult:
        self.calls.append(action)
        return HUPResult(
            action_id=action.action_id,
            device_id=action.device_id,
            status="success",
            data={"ok": True, "capability": action.capability_id},
        )


@pytest.fixture
def cutebot_registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.load_from_file(MANIFEST_PATH)
    return reg


def test_cutebot_manifest_loads_and_exposes_five_tools(cutebot_registry: SkillRegistry):
    assert "cutebot" in cutebot_registry.skills
    tools = cutebot_registry.get_tools_for_skills([cutebot_registry.skills["cutebot"]])
    tool_names = {t["function"]["name"] for t in tools}
    assert tool_names == EXPECTED_TOOLS
    assert len(tools) == 5


@pytest.mark.parametrize("endpoint_id", list(ENDPOINT_TO_CAPABILITY.keys()))
@pytest.mark.asyncio
async def test_impl_maps_endpoint_to_capability(endpoint_id: str):
    capability_id, action_type = ENDPOINT_TO_CAPABILITY[endpoint_id]
    registry = _MockDeviceRegistry(connected=True)
    skill = CuteBotSkill()
    skill.set_device_registry(registry)

    args = {"left": 30, "right": 30} if endpoint_id == "drive" else {}
    result = await skill.execute(endpoint_id, args, {})

    assert result["success"] is True
    assert len(registry.calls) == 1
    action = registry.calls[0]
    assert action.device_id == DEVICE_ID
    assert action.capability_id == capability_id
    assert action.action_type == action_type
    if endpoint_id == "drive":
        assert action.parameters == {"left": 30, "right": 30}


@pytest.mark.asyncio
async def test_impl_unplugged_device_returns_clear_error():
    registry = _MockDeviceRegistry(connected=False)
    skill = CuteBotSkill()
    skill.set_device_registry(registry)

    result = await skill.execute("follow_line", {}, {})
    assert result["success"] is False
    assert result["error"] == NOT_CONNECTED_ERROR
    assert registry.calls == []


@pytest.mark.asyncio
async def test_impl_halt_attempts_even_when_unplugged():
    registry = _MockDeviceRegistry(connected=False)
    skill = CuteBotSkill()
    skill.set_device_registry(registry)

    await skill.execute("halt", {}, {})
    assert len(registry.calls) == 1
    assert registry.calls[0].capability_id == "halt"


@pytest.mark.parametrize(
    ("tool_name", "args", "expected_level"),
    [
        ("cutebot__follow_line", {}, LEVEL_CONFIRM),
        ("cutebot__explore", {}, LEVEL_CONFIRM),
        ("cutebot__drive", {"left": 40, "right": 40}, LEVEL_CONFIRM),
        ("cutebot__drive", {"left": 81, "right": 10}, LEVEL_DENY),
        ("cutebot__drive", {"left": 10, "right": -90}, LEVEL_DENY),
        ("cutebot__halt", {}, LEVEL_AUTO),
        ("cutebot__status", {}, LEVEL_AUTO),
    ],
)
def test_safety_resolver_cutebot_tiers(
    cutebot_registry: SkillRegistry,
    tool_name: str,
    args: dict,
    expected_level: str,
):
    decision = resolve_policy(tool_name, args, surface="websocket", registry=cutebot_registry)
    assert decision.level == expected_level


def test_safety_drive_speed_deny_beats_manifest_confirm(cutebot_registry: SkillRegistry):
    """Dangerous speeds must DENY even though the manifest declares confirm."""
    decision = resolve_policy(
        "cutebot__drive",
        {"left": 100, "right": 0},
        surface="websocket",
        registry=cutebot_registry,
    )
    assert decision.level == LEVEL_DENY
    assert decision.sources.get("cutebot_speed_limit") is True
    assert "80" in decision.deny_reason


def test_cutebot_skill_registered_in_impl_registry():
    from skills.impl import get_implementation

    impl = get_implementation("cutebot")
    assert impl is not None
    assert impl.skill_id == "cutebot"
