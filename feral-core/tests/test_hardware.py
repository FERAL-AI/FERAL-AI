"""Tests for Hardware Unification Protocol (HUP)."""
import pytest
from hardware.protocol import (
    DeviceCapability,
    DeviceManifest,
    DeviceRegistry,
    DeviceAdapter,
    HUPAction,
    HUPActionType,
    HUPResult,
    FERAL_GLASSES_MANIFEST,
)


class MockAdapter(DeviceAdapter):
    def __init__(self):
        self.last_action = None

    async def execute(self, action):
        self.last_action = action
        return {"mock": True, "capability": action.capability_id}


class TestDeviceManifest:
    def test_feral_glasses_manifest(self):
        m = FERAL_GLASSES_MANIFEST
        assert m.device_id == "feral-glasses"
        assert m.device_type == "glasses"
        assert len(m.capabilities) == 8
        assert "heart_rate" in m.sensors
        assert m.battery_powered is True

    def test_custom_manifest(self):
        m = DeviceManifest(
            device_id="test-robot",
            device_type="robot",
            name="Test Robot",
            capabilities=[
                DeviceCapability(
                    id="move", name="Move",
                    description="Move the robot",
                    category="actuator",
                    permission_tier="active",
                ),
            ],
        )
        assert m.device_id == "test-robot"
        assert len(m.capabilities) == 1


class TestDeviceRegistry:
    def test_register_device(self):
        reg = DeviceRegistry()
        reg.register_device(FERAL_GLASSES_MANIFEST)
        assert len(reg.list_devices()) == 1
        assert reg.get_device("feral-glasses") is not None

    def test_unregister(self):
        reg = DeviceRegistry()
        reg.register_device(FERAL_GLASSES_MANIFEST)
        reg.unregister_device("feral-glasses")
        assert len(reg.list_devices()) == 0

    def test_find_by_capability(self):
        reg = DeviceRegistry()
        reg.register_device(FERAL_GLASSES_MANIFEST)
        sensors = reg.find_by_capability("sensor")
        assert len(sensors) == 1
        actuators = reg.find_by_capability("actuator")
        assert len(actuators) == 0

    def test_find_by_type(self):
        reg = DeviceRegistry()
        reg.register_device(FERAL_GLASSES_MANIFEST)
        glasses = reg.find_by_type("glasses")
        assert len(glasses) == 1
        robots = reg.find_by_type("robot")
        assert len(robots) == 0

    def test_find_by_location(self):
        reg = DeviceRegistry()
        reg.register_device(FERAL_GLASSES_MANIFEST)
        head = reg.find_by_location("head")
        assert len(head) == 1

    def test_to_llm_context(self):
        reg = DeviceRegistry()
        reg.register_device(FERAL_GLASSES_MANIFEST)
        ctx = reg.to_llm_context()
        assert "FERAL Smart Glasses" in ctx
        assert "heart_rate" in ctx.lower() or "Heart Rate" in ctx

    def test_stats(self):
        reg = DeviceRegistry()
        reg.register_device(FERAL_GLASSES_MANIFEST)
        s = reg.stats
        assert s["device_count"] == 1
        assert s["total_capabilities"] == 8
        assert s["total_sensors"] == 7

    @pytest.mark.asyncio
    async def test_execute_device_not_found(self):
        reg = DeviceRegistry()
        action = HUPAction(
            device_id="nonexistent",
            capability_id="read_hr",
            action_type=HUPActionType.READ,
        )
        result = await reg.execute_action(action)
        assert result.status == "failure"

    @pytest.mark.asyncio
    async def test_execute_with_adapter(self):
        reg = DeviceRegistry()
        adapter = MockAdapter()
        reg.register_device(FERAL_GLASSES_MANIFEST, adapter)
        action = HUPAction(
            device_id="feral-glasses",
            capability_id="read_heart_rate",
            action_type=HUPActionType.READ,
        )
        result = await reg.execute_action(action)
        assert result.status == "success"
        assert adapter.last_action is not None

    @pytest.mark.asyncio
    async def test_execute_confirmation_required(self):
        cap = DeviceCapability(
            id="dangerous_op", name="Dangerous",
            description="test", category="actuator",
            requires_confirmation=True,
        )
        manifest = DeviceManifest(
            device_id="test", device_type="robot",
            name="Test", capabilities=[cap],
        )
        reg = DeviceRegistry()
        adapter = MockAdapter()
        reg.register_device(manifest, adapter)
        action = HUPAction(
            device_id="test",
            capability_id="dangerous_op",
            action_type=HUPActionType.EXECUTE,
        )
        result = await reg.execute_action(action)
        assert result.status == "pending_confirmation"


class TestHUPAction:
    def test_defaults(self):
        a = HUPAction(
            device_id="test",
            capability_id="read_hr",
            action_type=HUPActionType.READ,
        )
        assert a.timeout_ms == 5000
        assert a.priority == 0
        assert a.action_id is not None
