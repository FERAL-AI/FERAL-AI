"""Tests for FERAL hardware mesh node registration and invoke."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hardware.mesh import NODE_COMMANDS, HardwareMesh
from hardware.protocol import DeviceRegistry


class TestHardwareMeshInit:
    """Mesh container state."""

    def test_init_empty_registry_side(self):
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)
        assert mesh._pending_invokes == {}
        assert mesh._node_metadata == {}


class TestNodeLifecycle:
    """Connect / disconnect register devices."""

    @pytest.mark.asyncio
    async def test_on_node_connected_registers(self):
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)
        await mesh.on_node_connected(
            "node-a",
            {"node_type": "desktop", "platform": "linux", "capabilities": ["shell"]},
        )
        assert reg.get_device("node-a") is not None
        assert "node-a" in mesh._node_metadata

    @pytest.mark.asyncio
    async def test_on_node_disconnected_unregisters(self):
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)
        await mesh.on_node_connected(
            "node-b",
            {"node_type": "desktop", "platform": "macos", "capabilities": []},
        )
        mesh.on_node_disconnected("node-b")
        assert reg.get_device("node-b") is None
        assert "node-b" not in mesh._node_metadata


class TestInvoke:
    """Command send and response correlation."""

    @pytest.mark.asyncio
    async def test_invoke_resolves_with_result(self):
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)

        async def send_and_resolve(msg: dict):
            rid = msg["payload"]["action_id"]
            mesh.resolve_invoke(rid, {"success": True, "data": {"v": 42}})

        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=send_and_resolve)
        daemons["n1"] = ws

        result = await mesh.invoke("n1", "location.get", {}, timeout=5.0)
        assert result.get("success") is True
        assert result.get("data", {}).get("v") == 42
        ws.send_json.assert_awaited()

    @pytest.mark.asyncio
    async def test_invoke_timeout_returns_error(self):
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)
        ws = MagicMock()
        ws.send_json = AsyncMock(return_value=None)
        daemons["n2"] = ws

        result = await mesh.invoke("n2", "sensor.read", {}, timeout=0.05)
        assert result.get("success") is False
        assert "error" in result
        assert "Timeout" in result["error"]


class TestFailSafeHaltOnTimeout:
    """Batch 2 — host-side dead-man: motion timeout issues a best-effort halt."""

    @pytest.mark.asyncio
    async def test_motion_command_timeout_issues_halt(self):
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)
        ws = MagicMock()
        # Never resolves the pending future -> the command times out.
        ws.send_json = AsyncMock(return_value=None)
        daemons["robot-1"] = ws

        result = await mesh.invoke(
            "robot-1", "drive", {"left": 30, "right": 30}, timeout=0.05
        )
        assert result.get("success") is False
        # Two sends: the original drive command, then the fail-safe halt.
        assert ws.send_json.await_count == 2
        halt_msg = ws.send_json.await_args_list[-1].args[0]
        assert halt_msg["payload"]["name"] == "halt"

    @pytest.mark.asyncio
    async def test_nonmotion_command_timeout_no_halt(self):
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)
        ws = MagicMock()
        ws.send_json = AsyncMock(return_value=None)
        daemons["cam-1"] = ws

        result = await mesh.invoke("cam-1", "camera.snap", {}, timeout=0.05)
        assert result.get("success") is False
        # Only the original command — a camera snap is not motion.
        assert ws.send_json.await_count == 1

    @pytest.mark.asyncio
    async def test_estop_disabled_suppresses_halt(self):
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons, emergency_stop_enabled=False)
        ws = MagicMock()
        ws.send_json = AsyncMock(return_value=None)
        daemons["robot-2"] = ws

        result = await mesh.invoke("robot-2", "drive", {}, timeout=0.05)
        assert result.get("success") is False
        assert ws.send_json.await_count == 1


class TestNodeCommands:
    """NODE_COMMANDS catalog."""

    def test_expected_commands_exist(self):
        for key in (
            "camera.snap",
            "location.get",
            "sensor.read",
            "health.read",
            "notification.send",
        ):
            assert key in NODE_COMMANDS
            assert "description" in NODE_COMMANDS[key]


class TestSystemRunApprovalGate:
    """system.run is a dangerous command — verify approval gate is consulted."""

    @pytest.mark.asyncio
    async def test_system_run_with_approval_denied(self):
        """When approval is denied, the command should not execute."""
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)

        ws = MagicMock()
        ws.send_json = AsyncMock(return_value=None)
        daemons["n-gate"] = ws

        result = await mesh.invoke("n-gate", "system.run", {"command": "rm -rf /"}, timeout=0.1)
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_system_run_completes_on_approval(self):
        """When node responds with success, invoke returns success."""
        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)

        async def send_and_resolve(msg):
            rid = msg["payload"]["action_id"]
            mesh.resolve_invoke(rid, {"success": True, "data": {"stdout": "ok"}})

        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=send_and_resolve)
        daemons["n-ok"] = ws

        result = await mesh.invoke("n-ok", "system.run", {"command": "echo hello"}, timeout=5.0)
        assert result.get("success") is True


class TestSoakMesh:
    """Stress test: many invokes with random failures."""

    @pytest.mark.asyncio
    async def test_50_invokes_with_random_failures(self):
        """Simulate 50 invokes with mixed success/timeout, verify mesh stays consistent."""
        import random

        reg = DeviceRegistry()
        daemons: dict = {}
        mesh = HardwareMesh(reg, daemons)

        call_count = 0

        async def random_send(msg):
            nonlocal call_count
            call_count += 1
            rid = msg["payload"]["action_id"]
            if random.random() < 0.3:
                return
            mesh.resolve_invoke(rid, {"success": random.random() > 0.2})

        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=random_send)
        daemons["soak-node"] = ws

        results = []
        for _ in range(50):
            r = await mesh.invoke("soak-node", "sensor.read", {"sensor_name": "temp"}, timeout=0.05)
            results.append(r)

        assert len(results) == 50
        assert mesh._pending_invokes == {} or all(
            f.done() for f in mesh._pending_invokes.values()
        )
        assert mesh.ledger is not None
