"""Tests for the generic device-manifest → LLM-skill bridge.

This is the HUP "self-describing" path: ANY DeviceManifest becomes LLM tools
+ a safe, telemetry-verified dispatcher with no per-device code. The CuteBot
is just the first witness; the same path must work for any device.
"""

from __future__ import annotations

import pytest

from hardware.capability_skill import (
    GenericHardwareSkill,
    device_manifest_to_skill_manifest,
    skill_id_for_device,
)
from hardware.protocol import (
    DeviceCapability,
    DeviceManifest,
    HUPAction,
    HUPActionType,
    HUPResult,
)


def _manifest(verify_on_motion: bool = False) -> DeviceManifest:
    motion = DeviceCapability(
        id="follow_line",
        name="Follow Line",
        description="Autonomous line follow.",
        category="actuator",
        permission_tier="active",
        requires_confirmation=True,
        safety_notes="Track must be clear.",
        # Optional verification contract — read 'mode' back, expect line_follow.
        verify=(
            {"via": "status", "field": "mode", "expect": ["line_follow", "T"]}
            if verify_on_motion
            else None
        ),
    )
    return DeviceManifest(
        device_id="cutebot-usb-0",
        device_type="robot",
        name="QtBot (CuteBot)",
        manufacturer="Elecfreaks",
        model="EF08209",
        connection_type="serial",
        capabilities=[
            motion,
            DeviceCapability(
                id="drive",
                name="Manual Drive",
                description="Direct wheel speeds.",
                category="actuator",
                permission_tier="dangerous",
                requires_confirmation=True,
                parameters=[
                    {"name": "left", "type": "integer", "required": True},
                    {"name": "right", "type": "integer", "required": True},
                ],
            ),
            DeviceCapability(
                id="halt",
                name="Halt",
                description="Stop motors.",
                category="actuator",
                permission_tier="passive",
            ),
            DeviceCapability(
                id="set_lights",
                name="Set Lights",
                description="RGB expression.",
                category="actuator",
                permission_tier="passive",
                parameters=[{"name": "r", "type": "integer", "required": True}],
            ),
            DeviceCapability(
                id="status",
                name="Status",
                description="Telemetry snapshot.",
                category="sensor",
                permission_tier="passive",
            ),
        ],
    )


class _FakeRegistry:
    """Acks every action; serves a scripted telemetry snapshot for reads."""

    def __init__(self, telemetry: dict | None = None):
        self.actions: list[HUPAction] = []
        self.telemetry = telemetry or {"online": True, "mode": "line_follow", "battery": True}

    async def execute_action(self, action: HUPAction) -> HUPResult:
        self.actions.append(action)
        if action.capability_id == "status":
            return HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="success", data=dict(self.telemetry),
            )
        return HUPResult(
            action_id=action.action_id, device_id=action.device_id,
            status="success", data={"ok": True, "command": action.capability_id},
        )


class TestManifestGeneration:
    def test_skill_id_is_tool_name_safe(self):
        sid = skill_id_for_device("cutebot-usb-0")
        assert sid == "hwdev_cutebot_usb_0"
        assert all(c.isalnum() or c == "_" for c in sid)

    def test_every_capability_becomes_an_endpoint(self):
        skill = device_manifest_to_skill_manifest(_manifest())
        ep_ids = {e.id for e in skill.endpoints}
        assert ep_ids == {"follow_line", "drive", "halt", "set_lights", "status"}
        assert skill.skill_id == "hwdev_cutebot_usb_0"

    def test_generic_safety_tier_mapping(self):
        skill = device_manifest_to_skill_manifest(_manifest())
        eps = {e.id: e for e in skill.endpoints}
        # active actuator -> confirm
        assert eps["follow_line"].safety_tier == "confirm"
        # dangerous actuator -> confirm + approval
        assert eps["drive"].safety_tier == "confirm"
        assert eps["drive"].requires_user_approval is True
        # passive actuator -> safe, no approval (lights, halt)
        assert eps["set_lights"].safety_tier == "safe"
        assert eps["halt"].safety_tier == "safe"
        # sensor -> safe + read-only
        assert eps["status"].safety_tier == "safe"
        assert eps["status"].read_only_hint is True

    def test_params_are_carried_through(self):
        skill = device_manifest_to_skill_manifest(_manifest())
        drive = next(e for e in skill.endpoints if e.id == "drive")
        names = {p.name for p in drive.params}
        assert names == {"left", "right"}
        assert all(p.type == "integer" for p in drive.params)


class TestGenericDispatch:
    @pytest.mark.asyncio
    async def test_sensor_read_returns_data_directly(self):
        reg = _FakeRegistry()
        skill = GenericHardwareSkill(
            device_id="cutebot-usb-0", device_registry=reg, manifest=_manifest()
        )
        out = await skill.execute("status", {}, {})
        assert out["success"] is True
        assert out["data"]["mode"] == "line_follow"
        # A pure read makes exactly one wire call (no verification round-trip).
        assert len(reg.actions) == 1
        assert reg.actions[0].action_type.value == "read"

    @pytest.mark.asyncio
    async def test_actuator_success_reads_telemetry_back(self):
        reg = _FakeRegistry()
        skill = GenericHardwareSkill(
            device_id="cutebot-usb-0", device_registry=reg, manifest=_manifest()
        )
        out = await skill.execute("set_lights", {"r": 255}, {})
        assert out["success"] is True
        # No verification contract on lights -> honest "unknown" + telemetry.
        assert out["data"]["verified"] is None
        assert out["data"]["telemetry"]["online"] is True
        # The dispatcher pre-confirms so the registry gate doesn't dead-end.
        exec_action = next(a for a in reg.actions if a.capability_id == "set_lights")
        assert exec_action.confirmed is True

    @pytest.mark.asyncio
    async def test_verification_contract_pass(self):
        reg = _FakeRegistry(telemetry={"online": True, "mode": "line_follow"})
        skill = GenericHardwareSkill(
            device_id="cutebot-usb-0", device_registry=reg,
            manifest=_manifest(verify_on_motion=True),
        )
        out = await skill.execute("follow_line", {}, {})
        assert out["success"] is True
        assert out["data"]["verified"] is True
        assert out["data"]["observed"] == "line_follow"

    @pytest.mark.asyncio
    async def test_verification_contract_fail_is_honest(self):
        reg = _FakeRegistry(telemetry={"online": True, "mode": "stopped"})
        skill = GenericHardwareSkill(
            device_id="cutebot-usb-0", device_registry=reg,
            manifest=_manifest(verify_on_motion=True),
        )
        out = await skill.execute("follow_line", {}, {})
        assert out["success"] is False
        assert out["data"]["verified"] is False
        assert out["data"]["observed"] == "stopped"
        assert "actual state" in out["error"]

    @pytest.mark.asyncio
    async def test_unknown_capability_errors(self):
        reg = _FakeRegistry()
        skill = GenericHardwareSkill(
            device_id="cutebot-usb-0", device_registry=reg, manifest=_manifest()
        )
        out = await skill.execute("teleport", {}, {})
        assert out["success"] is False
        assert out["status_code"] == 400


class TestRealCuteBotManifestIsSelfDescribing:
    """The real adapter manifest must drive the generic path end-to-end with
    no cutebot-specific code — proving the self-describing HUP contract."""

    def test_real_manifest_generates_tools_with_verify_contracts(self):
        from hardware.adapters.cutebot import CuteBotAdapter

        manifest = CuteBotAdapter().manifest
        skill = device_manifest_to_skill_manifest(manifest)
        ep_ids = {e.id for e in skill.endpoints}
        assert {"follow_line", "explore", "halt", "drive", "set_lights"} <= ep_ids

        eps = {e.id: e for e in skill.endpoints}
        # Motion requires confirmation; lights/status are auto.
        assert eps["follow_line"].safety_tier == "confirm"
        assert eps["set_lights"].safety_tier == "safe"

        # The robot declares HOW to verify motion — the generic dispatcher
        # will enforce it, so the honesty loop is not cutebot-specific code.
        caps = {c.id: c for c in manifest.capabilities}
        assert caps["follow_line"].verify["field"] == "mode"
        assert "line_follow" in caps["follow_line"].verify["expect"]
        assert caps["halt"].verify["expect"] == ["stopped", "M"]

    def test_manifest_built_from_device_self_description(self):
        """When connected, the adapter builds its manifest from the device's
        OWN capabilities()['actions'] — no hardcoded capability list."""
        from hardware.adapters.cutebot import CuteBotAdapter

        class _FakeBot:
            def capabilities(self):
                return {
                    "device_type": "qtbot",
                    "sensors": ["sonar_cm", "battery"],
                    "actions": [
                        {
                            "name": "follow_line",
                            "category": "actuator",
                            "permission_tier": "active",
                            "requires_confirmation": True,
                            "description": "Line follow.",
                            "verify": {"via": "read_telemetry", "field": "mode",
                                       "expect": ["line_follow", "T"]},
                        },
                        {
                            "name": "set_lights",
                            "category": "actuator",
                            "permission_tier": "passive",
                            "params": [{"name": "r", "type": "integer",
                                        "required": True}],
                            "description": "RGB.",
                        },
                        {
                            "name": "read_telemetry",
                            "category": "sensor",
                            "permission_tier": "passive",
                            "description": "Telemetry.",
                        },
                    ],
                }

        adapter = CuteBotAdapter(bot=_FakeBot())
        manifest = adapter.manifest
        # device_type qtbot is normalized to robot; caps come from the device.
        assert manifest.device_type == "robot"
        caps = {c.id: c for c in manifest.capabilities}
        assert set(caps) == {"follow_line", "set_lights", "read_telemetry"}
        assert caps["follow_line"].verify["expect"] == ["line_follow", "T"]
        assert caps["read_telemetry"].category == "sensor"
        assert caps["set_lights"].parameters[0]["name"] == "r"

        # And it flows through the generic bridge into LLM tools.
        skill = device_manifest_to_skill_manifest(manifest)
        assert {e.id for e in skill.endpoints} == {
            "follow_line", "set_lights", "read_telemetry"
        }


class _FakeNavBot:
    """Bot whose advertised commands depend on navigator attachment, like the
    real QtBot (go_to/patrol/stop_navigation only appear once attached)."""

    def __init__(self, nav: bool = True):
        self._nav = nav
        self.calls: list[tuple[str, dict]] = []

    def capabilities(self):
        actions = [
            {"name": "drive", "category": "actuator", "permission_tier": "dangerous",
             "params": [{"name": "left", "type": "integer"},
                        {"name": "right", "type": "integer"}]},
            {"name": "read_telemetry", "category": "sensor",
             "permission_tier": "passive"},
        ]
        if self._nav:
            actions += [
                {"name": "go_to", "category": "actuator", "permission_tier": "dangerous",
                 "params": [{"name": "x_cm", "type": "number"},
                            {"name": "y_cm", "type": "number"}]},
                {"name": "patrol", "category": "actuator", "permission_tier": "dangerous",
                 "params": [{"name": "waypoints", "type": "array"}]},
                {"name": "stop_navigation", "category": "actuator",
                 "permission_tier": "passive"},
            ]
        return {"device_type": "qtbot", "sensors": ["sonar_cm", "battery"],
                "actions": actions}

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        cmds = {a["name"] for a in self.capabilities()["actions"]}
        if command not in cmds:
            return {"ok": False, "error": f"unknown command: {command}"}
        return {"ok": True, "command": command, **kwargs}

    def status(self):
        return {"online": True, "mode": "stopped", "battery": True, "sonar_cm": 50.0}

    def attach_navigator(self, pose_source):
        self._nav = True
        return {"ok": True, "command": "attach_navigator"}


class TestNavigationHarvest:
    """go_to/patrol harvested from cuteferalbot brain/ flow through the SAME
    generic self-describing path — no per-command FERAL skill code."""

    def test_nav_caps_absent_without_navigator(self):
        from hardware.adapters.cutebot import CuteBotAdapter

        adapter = CuteBotAdapter(bot=_FakeNavBot(nav=False))
        caps = {c.id for c in adapter.manifest.capabilities}
        assert not ({"go_to", "patrol", "stop_navigation"} & caps)

    @pytest.mark.asyncio
    async def test_nav_caps_exposed_and_routed_when_attached(self):
        from hardware.adapters.cutebot import CuteBotAdapter

        bot = _FakeNavBot(nav=True)
        adapter = CuteBotAdapter(bot=bot)

        manifest = adapter.manifest
        caps = {c.id for c in manifest.capabilities}
        assert {"go_to", "patrol", "stop_navigation"} <= caps

        # Auto-exposed as LLM tools with no per-command code.
        skill = device_manifest_to_skill_manifest(manifest)
        assert {"go_to", "patrol", "stop_navigation"} <= {e.id for e in skill.endpoints}

        # The adapter routes go_to through to the bot with mapped params.
        res = await adapter.execute(HUPAction(
            device_id="cutebot-usb-0", capability_id="go_to",
            action_type=HUPActionType.EXECUTE,
            parameters={"x_cm": 40, "y_cm": 25}, confirmed=True,
        ))
        assert res.status == "success"
        assert ("go_to", {"x_cm": 40.0, "y_cm": 25.0}) in bot.calls

    @pytest.mark.asyncio
    async def test_attach_navigator_delegates_to_bot(self):
        from hardware.adapters.cutebot import CuteBotAdapter

        bot = _FakeNavBot(nav=False)
        adapter = CuteBotAdapter(bot=bot)
        out = adapter.attach_navigator(pose_source=object())
        assert out["ok"] is True
        # After attach, nav caps now appear in the (re-read) manifest.
        assert "go_to" in {c.id for c in adapter.manifest.capabilities}
