"""Tests for FERAL hardware adapters — wristband, smart home, robot arm."""

import pytest
from hardware.protocol import HUPAction, HUPActionType


# ── Wristband ─────────────────────────────────────────────────────


class TestWristbandAdapter:
    def test_init_and_manifest(self):
        from hardware.adapters.wristband import WristbandAdapter

        wb = WristbandAdapter()
        assert wb.device_id == "wristband-01"
        m = wb.manifest
        assert m.device_type == "wearable"
        # Sensor reads only. vibrate/set_led were removed because they were
        # a logger.info plus success=True with no GATT write.
        assert len(m.capabilities) == 3
        assert {c.id for c in m.capabilities} == {"heart_rate", "spo2", "skin_temp"}

    def test_actuator_capabilities_not_advertised(self):
        """Unimplemented haptics/LED must not appear in the manifest."""
        from hardware.adapters.wristband import WristbandAdapter

        ids = {c.id for c in WristbandAdapter().manifest.capabilities}
        assert "vibrate" not in ids
        assert "set_led" not in ids

    @pytest.mark.asyncio
    async def test_connect_without_ble_address_reports_failure(self):
        """No BLE address means not connected — not "simulation mode"."""
        from hardware.adapters.wristband import WristbandAdapter

        wb = WristbandAdapter()
        assert await wb.connect() is False
        assert wb._client is None

    @pytest.mark.asyncio
    async def test_read_heart_rate_without_device_fails(self):
        """A read with no device must fail, not report 0 bpm as a success."""
        from hardware.adapters.wristband import WristbandAdapter

        wb = WristbandAdapter()
        await wb.connect()
        action = HUPAction(
            device_id="wristband-01",
            capability_id="heart_rate",
            action_type=HUPActionType.READ,
        )
        result = await wb.execute(action)
        assert result.status == "failure"
        assert "not connected" in (result.error or "").lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cap", ["vibrate", "set_led"])
    async def test_actuators_fail_loudly(self, cap):
        """Calling them directly must not report success."""
        from hardware.adapters.wristband import WristbandAdapter

        wb = WristbandAdapter()
        action = HUPAction(
            device_id="wristband-01",
            capability_id=cap,
            action_type=HUPActionType.EXECUTE,
        )
        result = await wb.execute(action)
        assert result.status == "failure"
        assert "not implemented" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_unknown_capability(self):
        from hardware.adapters.wristband import WristbandAdapter

        wb = WristbandAdapter()
        action = HUPAction(
            device_id="wristband-01",
            capability_id="nonexistent",
            action_type=HUPActionType.READ,
        )
        result = await wb.execute(action)
        assert result.status == "failure"
        assert "Unknown" in result.error


# ── Smart Home ────────────────────────────────────────────────────


class TestSmartHomeAdapter:
    def test_init_and_manifest(self):
        from hardware.adapters.smart_home import SmartHomeAdapter

        sh = SmartHomeAdapter()
        assert sh.device_id == "smart-home-01"
        m = sh.manifest
        assert m.device_type == "smart_home"
        assert len(m.capabilities) >= 5

    def test_thermostat_set_not_advertised(self):
        """It was never wired to a thermostat, so it must not be offered."""
        from hardware.adapters.smart_home import SmartHomeAdapter

        ids = {c.id for c in SmartHomeAdapter().manifest.capabilities}
        assert "thermostat_set" not in ids
        assert "thermostat_read" in ids

    @pytest.mark.asyncio
    async def test_thermostat_set_fails_instead_of_faking_success(self):
        """Was: success=True with a "requires Home Assistant" note."""
        from hardware.adapters.smart_home import SmartHomeAdapter

        sh = SmartHomeAdapter()
        action = HUPAction(
            device_id="smart-home-01",
            capability_id="thermostat_set",
            action_type=HUPActionType.EXECUTE,
            parameters={"temperature_c": 21.0},
        )
        result = await sh.execute(action)
        assert result.status == "failure"
        assert "not implemented" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_thermostat_read_unconfigured_is_failure(self):
        """A {"error": ...} from the Hue helper is not a temperature."""
        from hardware.adapters.smart_home import SmartHomeAdapter

        sh = SmartHomeAdapter()
        if sh._hue.configured:
            pytest.skip("A real Hue bridge is configured in this environment")
        action = HUPAction(
            device_id="smart-home-01",
            capability_id="thermostat_read",
            action_type=HUPActionType.READ,
        )
        result = await sh.execute(action)
        assert result.status == "failure"
        assert result.error

    @pytest.mark.asyncio
    async def test_lights_toggle_off_without_bridge(self):
        from hardware.adapters.smart_home import SmartHomeAdapter

        sh = SmartHomeAdapter()
        action = HUPAction(
            device_id="smart-home-01",
            capability_id="lights_toggle",
            action_type=HUPActionType.EXECUTE,
            parameters={"state": "off"},
        )
        result = await sh.execute(action)
        # Without a configured Hue bridge, should return a clear error
        if not sh._hue.configured:
            assert result.status == "failure"
            assert "not configured" in (result.error or "").lower() or "hue" in (result.error or "").lower()
        else:
            assert result.status == "success"

    @pytest.mark.asyncio
    async def test_unknown_capability(self):
        from hardware.adapters.smart_home import SmartHomeAdapter

        sh = SmartHomeAdapter()
        action = HUPAction(
            device_id="smart-home-01",
            capability_id="nonexistent",
            action_type=HUPActionType.EXECUTE,
        )
        result = await sh.execute(action)
        assert result.status == "failure"
        assert "Unknown" in result.error


# ── Robot Arm ─────────────────────────────────────────────────────


class TestRobotArmAdapter:
    def test_init_and_manifest(self):
        from hardware.adapters.robot_arm import RobotArmAdapter

        ra = RobotArmAdapter()
        assert ra.device_id == "robot-arm-01"
        assert ra.dof == 6
        m = ra.manifest
        assert m.device_type == "robot"

    @pytest.mark.asyncio
    async def test_connect_without_serial_port_reports_failure(self):
        """No port means not connected — there is no "simulation mode"."""
        from hardware.adapters.robot_arm import RobotArmAdapter

        ra = RobotArmAdapter()
        assert await ra.connect() is False
        assert ra._serial is None

    @pytest.mark.asyncio
    async def test_read_position_without_serial_fails(self):
        """_position is adapter bookkeeping, not where the arm actually is."""
        from hardware.adapters.robot_arm import RobotArmAdapter

        ra = RobotArmAdapter()
        action = HUPAction(
            device_id="robot-arm-01",
            capability_id="read_position",
            action_type=HUPActionType.READ,
        )
        result = await ra.execute(action)
        assert result.status == "failure"
        assert "not connected" in (result.error or "").lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cap,params",
        [
            ("move_joints", {"joints": [10, 0, 0, 0, 0, 0], "speed_pct": 50}),
            ("move_cartesian", {"x": 1, "y": 2, "z": 3}),
            ("gripper", {"state": "close"}),
            ("home", {}),
            ("estop", {}),
        ],
    )
    async def test_actuation_without_serial_never_reports_success(self, cap, params):
        """The core regression: no port must never mean "moved successfully".

        Every one of these used to return status="success" (move_joints even
        returned a fabricated joint array) while _send_gcode did nothing but
        logger.debug("Simulated G-code").
        """
        from hardware.adapters.robot_arm import RobotArmAdapter

        ra = RobotArmAdapter()
        action = HUPAction(
            device_id="robot-arm-01",
            capability_id=cap,
            action_type=HUPActionType.EXECUTE,
            parameters=params,
        )
        result = await ra.execute(action)
        assert result.status == "failure", f"{cap} fabricated success"
        assert "not connected" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_send_gcode_without_serial_raises(self):
        """Backstop: no silent no-op path can be reintroduced."""
        from hardware.adapters.robot_arm import RobotArmAdapter

        ra = RobotArmAdapter()
        with pytest.raises(RuntimeError, match="not connected"):
            await ra._send_gcode("G28")

    def test_no_local_robot_action_skill_shadows_the_daemon_path(self):
        """skills.impl.robot_action registered itself on import and, because
        SkillExecutor checks Python impls before WS_EXECUTE, shadowed the
        daemon path with an adapter that had no serial port."""
        import importlib

        with pytest.raises(ImportError):
            importlib.import_module("skills.impl.robot_action")

    @pytest.mark.asyncio
    async def test_unknown_capability(self):
        from hardware.adapters.robot_arm import RobotArmAdapter

        ra = RobotArmAdapter()
        action = HUPAction(
            device_id="robot-arm-01",
            capability_id="nonexistent",
            action_type=HUPActionType.EXECUTE,
        )
        result = await ra.execute(action)
        assert result.status == "failure"
        assert "Unknown" in result.error
