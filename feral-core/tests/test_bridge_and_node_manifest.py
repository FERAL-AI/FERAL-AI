"""Tests for bridged-peripheral execution + node-supplied self-description.

Together these prove the brain side of "self-describing peripherals": a phone
bridges a device's HUP manifest, the brain builds tools/safety/honesty from it
generically, and actions route back through the bridge node."""

from __future__ import annotations

import pytest

from hardware.adapters.bridge import BridgedPeripheralAdapter
from hardware.mesh import HardwareMesh
from hardware.protocol import DeviceManifest, HUPAction, HUPActionType


class _FakeMesh:
    def __init__(self, ok: bool = True):
        self._daemons = {"iphone-1": object()}
        self.invocations: list[tuple[str, str, dict]] = []
        self._ok = ok

    async def invoke(self, node_id, command, params=None, timeout=10.0):
        self.invocations.append((node_id, command, dict(params or {})))
        if not self._ok:
            return {"success": False, "error": "peripheral offline"}
        return {"success": True, "data": {"command": command, "echo": params}}


class TestBridgedPeripheralAdapter:
    @pytest.mark.asyncio
    async def test_execute_routes_through_bridge_with_device_id(self):
        mesh = _FakeMesh()
        adapter = BridgedPeripheralAdapter("veepoo-band", node_id="iphone-1", mesh=mesh)
        res = await adapter.execute(HUPAction(
            device_id="veepoo-band", capability_id="vibrate",
            action_type=HUPActionType.EXECUTE, parameters={"ms": 200},
        ))
        assert res.status == "success"
        node_id, command, params = mesh.invocations[-1]
        assert node_id == "iphone-1"
        assert command == "vibrate"
        # The bridge tags the action with its target sub-device.
        assert params["device_id"] == "veepoo-band"
        assert params["ms"] == 200

    @pytest.mark.asyncio
    async def test_execute_reports_bridge_failure(self):
        mesh = _FakeMesh(ok=False)
        adapter = BridgedPeripheralAdapter("veepoo-band", node_id="iphone-1", mesh=mesh)
        res = await adapter.execute(HUPAction(
            device_id="veepoo-band", capability_id="vibrate",
            action_type=HUPActionType.EXECUTE,
        ))
        assert res.status == "failure"
        assert "offline" in res.error

    @pytest.mark.asyncio
    async def test_execute_without_mesh_is_honest(self):
        adapter = BridgedPeripheralAdapter("x", node_id="n", mesh=None)
        res = await adapter.execute(HUPAction(
            device_id="x", capability_id="vibrate",
            action_type=HUPActionType.EXECUTE,
        ))
        assert res.status == "failure"


class TestNodeSuppliedManifest:
    def test_actions_envelope_becomes_real_manifest(self):
        supplied = {
            "device_type": "glasses",
            "name": "W610 Open Glasses",
            "manufacturer": "OpenGlass",
            "sensors": ["imu"],
            "actions": [
                {"name": "capture_photo", "category": "sensor",
                 "permission_tier": "passive", "description": "photo"},
                {"name": "set_led", "category": "actuator",
                 "permission_tier": "passive", "description": "led",
                 "params": [{"name": "r", "type": "integer"}]},
            ],
        }
        m = HardwareMesh._manifest_from_supplied("w610", "glasses", "ios", supplied)
        assert isinstance(m, DeviceManifest)
        assert m.name == "W610 Open Glasses"
        assert {c.id for c in m.capabilities} == {"capture_photo", "set_led"}

    def test_full_manifest_dict_is_accepted(self):
        supplied = {
            "device_type": "wristband",
            "name": "Band",
            "capabilities": [
                {"id": "read_hr", "name": "HR", "description": "hr",
                 "category": "sensor", "permission_tier": "passive"},
            ],
        }
        m = HardwareMesh._manifest_from_supplied("band", "wristband", "ios", supplied)
        assert isinstance(m, DeviceManifest)
        assert m.capabilities[0].id == "read_hr"

    def test_non_dict_falls_back(self):
        assert HardwareMesh._manifest_from_supplied("x", "y", "ios", None) is None
        assert HardwareMesh._manifest_from_supplied("x", "y", "ios", "nope") is None
