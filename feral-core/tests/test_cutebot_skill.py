"""Tests for the CuteBot skill manifest, implementation, and safety policy."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hardware.protocol import (  # noqa: E402
    DeviceCapability,
    DeviceManifest,
    DeviceRegistry,
    HUPAction,
    HUPActionType,
    HUPResult,
)
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
    "cutebot__set_lights",
    "cutebot__status",
}

ENDPOINT_TO_CAPABILITY = {
    "follow_line": ("follow_line", HUPActionType.EXECUTE),
    "explore": ("explore", HUPActionType.EXECUTE),
    "drive": ("drive", HUPActionType.EXECUTE),
    "halt": ("halt", HUPActionType.EXECUTE),
    "set_lights": ("set_lights", HUPActionType.EXECUTE),
    "status": ("read_telemetry", HUPActionType.READ),
}

# Mode telemetry that makes closed-loop verification pass for each endpoint.
_GOOD_MODE = {
    "follow_line": "line_follow",
    "explore": "explore",
    "drive": "stopped",  # transient — firmware reverts after 1.5s
    "halt": "stopped",
}


def _telemetry(mode: str, *, online: bool = True) -> dict:
    return {
        "online": online,
        "mode": mode,
        "state": "ok" if online else "",
        "sonar_cm": 25.0,
        "line_left": False,
        "line_right": False,
        "battery": online,
    }


class _MockDeviceRegistry:
    """Fake DeviceRegistry: acks every command, serves scripted telemetry.

    ``telemetry_sequence`` is consumed one snapshot per read_telemetry call;
    the last snapshot repeats once the script runs out.
    """

    def __init__(self, *, connected: bool = True, telemetry_sequence: list[dict] | None = None):
        self.connected = connected
        self.calls: list[HUPAction] = []
        self.telemetry_sequence = list(telemetry_sequence or [_telemetry("stopped")])

    def get_device(self, device_id: str):
        if self.connected and device_id == DEVICE_ID:
            return MagicMock(device_id=device_id)
        return None

    async def execute_action(self, action: HUPAction) -> HUPResult:
        self.calls.append(action)
        if action.capability_id == "read_telemetry":
            snap = self.telemetry_sequence[0]
            if len(self.telemetry_sequence) > 1:
                self.telemetry_sequence.pop(0)
            return HUPResult(
                action_id=action.action_id,
                device_id=action.device_id,
                status="success",
                data=dict(snap),
            )
        return HUPResult(
            action_id=action.action_id,
            device_id=action.device_id,
            status="success",
            data={"ok": True, "capability": action.capability_id},
        )

    def commands_sent(self, capability_id: str) -> list[HUPAction]:
        return [c for c in self.calls if c.capability_id == capability_id]


def _make_skill(registry) -> CuteBotSkill:
    skill = CuteBotSkill()
    skill.set_device_registry(registry)
    skill.verify_delay_s = 0  # no real waiting in unit tests
    return skill


@pytest.fixture
def cutebot_registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.load_from_file(MANIFEST_PATH)
    return reg


def test_cutebot_manifest_loads_and_exposes_tools(cutebot_registry: SkillRegistry):
    assert "cutebot" in cutebot_registry.skills
    tools = cutebot_registry.get_tools_for_skills([cutebot_registry.skills["cutebot"]])
    tool_names = {t["function"]["name"] for t in tools}
    assert tool_names == EXPECTED_TOOLS
    assert len(tools) == 6


def test_cutebot_manifest_instructs_verified_execution(cutebot_registry: SkillRegistry):
    """The LLM-facing descriptions must teach the closed-loop contract."""
    skill = cutebot_registry.skills["cutebot"]
    assert "verified=true" in skill.description
    for ep_id in ("follow_line", "explore"):
        ep = next(e for e in skill.endpoints if e.id == ep_id)
        assert "verified" in ep.description.lower()


@pytest.mark.parametrize("endpoint_id", list(ENDPOINT_TO_CAPABILITY.keys()))
@pytest.mark.asyncio
async def test_impl_maps_endpoint_to_capability(endpoint_id: str):
    capability_id, action_type = ENDPOINT_TO_CAPABILITY[endpoint_id]
    good_mode = _GOOD_MODE.get(endpoint_id, "stopped")
    registry = _MockDeviceRegistry(
        connected=True, telemetry_sequence=[_telemetry(good_mode)]
    )
    skill = _make_skill(registry)

    args = {"left": 30, "right": 30} if endpoint_id == "drive" else {}
    result = await skill.execute(endpoint_id, args, {})

    assert result["success"] is True
    sent = registry.commands_sent(capability_id)
    assert len(sent) >= 1
    action = sent[0]
    assert action.device_id == DEVICE_ID
    assert action.capability_id == capability_id
    assert action.action_type == action_type
    # Pin the confirmation-gate fix: the skill must pre-confirm its actions
    # (ToolRunner already enforced the approval tier upstream).
    assert action.confirmed is True
    if endpoint_id == "drive":
        assert action.parameters == {"left": 30, "right": 30}
    if endpoint_id == "status":
        # Pure read — exactly one wire call, no verification round-trip.
        assert len(registry.calls) == 1


@pytest.mark.asyncio
async def test_impl_unplugged_device_returns_clear_error():
    registry = _MockDeviceRegistry(connected=False)
    skill = _make_skill(registry)

    result = await skill.execute("follow_line", {}, {})
    assert result["success"] is False
    assert result["error"] == NOT_CONNECTED_ERROR
    assert registry.calls == []


@pytest.mark.asyncio
async def test_impl_halt_attempts_even_when_unplugged():
    registry = _MockDeviceRegistry(
        connected=False, telemetry_sequence=[_telemetry("", online=False)]
    )
    skill = _make_skill(registry)

    result = await skill.execute("halt", {}, {})
    halts = registry.commands_sent("halt")
    assert len(halts) == 1
    # Offline robot: halt stays a no-op success, but it is honestly unverified.
    assert result["success"] is True
    assert result["data"]["verified"] is False


# ── Closed-loop verification ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explore_verification_pass_includes_verified_telemetry():
    registry = _MockDeviceRegistry(telemetry_sequence=[_telemetry("explore")])
    skill = _make_skill(registry)

    result = await skill.execute("explore", {}, {})

    assert result["success"] is True
    data = result["data"]
    assert data["verified"] is True
    assert data["mode"] == "explore"
    assert data["telemetry"]["online"] is True
    assert data["retried"] is False
    # One command + one telemetry read — no retry needed.
    assert len(registry.commands_sent("explore")) == 1
    assert len(registry.commands_sent("read_telemetry")) == 1


@pytest.mark.asyncio
async def test_follow_line_verification_pass():
    registry = _MockDeviceRegistry(telemetry_sequence=[_telemetry("line_follow")])
    skill = _make_skill(registry)

    result = await skill.execute("follow_line", {}, {})
    assert result["success"] is True
    assert result["data"]["verified"] is True


@pytest.mark.asyncio
async def test_explore_verification_fail_retries_once_then_fails_loudly():
    # Robot acks but stays stopped through both verification reads.
    registry = _MockDeviceRegistry(
        telemetry_sequence=[_telemetry("stopped"), _telemetry("stopped")]
    )
    skill = _make_skill(registry)

    result = await skill.execute("explore", {}, {})

    assert result["success"] is False
    # Exactly one retry: two explore commands on the wire, two telemetry reads.
    assert len(registry.commands_sent("explore")) == 2
    assert len(registry.commands_sent("read_telemetry")) == 2
    error = result["error"]
    assert "did not enter explore mode" in error
    assert "stopped" in error
    # The multi-agent path forwards only `data` to the LLM, so failure facts
    # must be embedded there too.
    data = result["data"]
    assert data["verified"] is False
    assert data["observed_mode"] == "stopped"
    assert data["error"] == error


@pytest.mark.asyncio
async def test_explore_verification_retry_recovers():
    # First read shows stopped (command didn't take), retry nudge works.
    registry = _MockDeviceRegistry(
        telemetry_sequence=[_telemetry("stopped"), _telemetry("explore")]
    )
    skill = _make_skill(registry)

    result = await skill.execute("explore", {}, {})

    assert result["success"] is True
    data = result["data"]
    assert data["verified"] is True
    assert data["retried"] is True
    assert len(registry.commands_sent("explore")) == 2


@pytest.mark.asyncio
async def test_drive_transient_reports_post_state_without_failing():
    # Firmware auto-reverts drive after 1.5s — "stopped" afterwards is normal.
    registry = _MockDeviceRegistry(telemetry_sequence=[_telemetry("stopped")])
    skill = _make_skill(registry)

    result = await skill.execute("drive", {"left": 30, "right": 30}, {})

    assert result["success"] is True
    data = result["data"]
    assert data["verified"] is True
    assert data["transient"] is True
    assert data["telemetry"]["mode"] == "stopped"
    # Transient command: never retried.
    assert len(registry.commands_sent("drive")) == 1


@pytest.mark.asyncio
async def test_halt_verification_pass():
    registry = _MockDeviceRegistry(telemetry_sequence=[_telemetry("stopped")])
    skill = _make_skill(registry)

    result = await skill.execute("halt", {}, {})

    assert result["success"] is True
    assert result["data"]["verified"] is True
    # Halt is single-check: one halt command, one telemetry read, no retry.
    assert len(registry.commands_sent("halt")) == 1
    assert len(registry.commands_sent("read_telemetry")) == 1


@pytest.mark.asyncio
async def test_halt_failure_is_never_masked():
    # Robot acks halt but telemetry shows it is still exploring.
    registry = _MockDeviceRegistry(telemetry_sequence=[_telemetry("explore")])
    skill = _make_skill(registry)

    result = await skill.execute("halt", {}, {})

    assert result["success"] is False
    assert "STILL IN MODE" in result["error"]
    assert result["data"]["verified"] is False
    assert result["data"]["observed_mode"] == "explore"
    # Single check — halt is not blindly re-issued by the skill (the LLM
    # decides, with the error text telling it to re-issue immediately).
    assert len(registry.commands_sent("halt")) == 1


# ── Confirmation-gate regression (end-to-end through a real registry) ───────


class _FakeMotionAdapter:
    """Real-DeviceRegistry adapter: acks motion, reports matching telemetry."""

    def __init__(self):
        self.executed: list[HUPAction] = []
        self.mode = "stopped"

    async def execute(self, action: HUPAction) -> HUPResult:
        self.executed.append(action)
        if action.capability_id == "read_telemetry":
            return HUPResult(
                action_id=action.action_id,
                device_id=action.device_id,
                status="success",
                data=_telemetry(self.mode),
            )
        if action.capability_id == "explore":
            self.mode = "explore"
        return HUPResult(
            action_id=action.action_id,
            device_id=action.device_id,
            status="success",
            data={"ok": True, "command": action.capability_id},
        )


def _real_registry_with_confirmation_gate() -> tuple[DeviceRegistry, _FakeMotionAdapter]:
    registry = DeviceRegistry()
    adapter = _FakeMotionAdapter()
    manifest = DeviceManifest(
        device_id=DEVICE_ID,
        device_type="robot",
        name="QtBot (CuteBot)",
        connection_type="serial",
        capabilities=[
            DeviceCapability(
                id="explore",
                name="Explore Table",
                description="Roam",
                category="actuator",
                permission_tier="active",
                requires_confirmation=True,
            ),
            DeviceCapability(
                id="read_telemetry",
                name="Read Telemetry",
                description="Snapshot",
                category="sensor",
                permission_tier="passive",
            ),
        ],
    )
    registry.register_device(manifest, adapter)
    return registry, adapter


@pytest.mark.asyncio
async def test_confirmed_flag_executes_motion_through_real_registry():
    """requires_confirmation=True must NOT dead-end when the skill calls it:
    the skill pre-confirms (ToolRunner already gated) and the registry honors
    action.confirmed. Regression for the silent pending_confirmation bug."""
    registry, adapter = _real_registry_with_confirmation_gate()
    skill = _make_skill(registry)

    result = await skill.execute("explore", {}, {})

    assert result["success"] is True
    assert result["data"]["verified"] is True
    assert any(a.capability_id == "explore" for a in adapter.executed)


@pytest.mark.asyncio
async def test_registry_gate_still_blocks_unconfirmed_actions():
    """The fix must not weaken the gate for callers that do NOT confirm."""
    registry, adapter = _real_registry_with_confirmation_gate()

    action = HUPAction(
        device_id=DEVICE_ID,
        capability_id="explore",
        action_type=HUPActionType.EXECUTE,
    )
    result = await registry.execute_action(action)

    assert result.status == "pending_confirmation"
    assert adapter.executed == []


# ── Safety policy tiers ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool_name", "args", "expected_level"),
    [
        ("cutebot__follow_line", {}, LEVEL_CONFIRM),
        ("cutebot__explore", {}, LEVEL_CONFIRM),
        ("cutebot__drive", {"left": 40, "right": 40}, LEVEL_CONFIRM),
        ("cutebot__drive", {"left": 81, "right": 10}, LEVEL_DENY),
        ("cutebot__drive", {"left": 10, "right": -90}, LEVEL_DENY),
        ("cutebot__halt", {}, LEVEL_AUTO),
        ("cutebot__set_lights", {"r": 0, "g": 255, "b": 0}, LEVEL_AUTO),
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
