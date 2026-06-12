"""Tests for rule-based HardwareOrchestrator."""

from __future__ import annotations

import pytest

from hardware.adapters.cutebot import CuteBotAdapter, DEFAULT_DEVICE_ID
from hardware.command_contract import CommandLedger
from hardware.orchestrator import HardwareOrchestrator
from hardware.protocol import DeviceCapability, DeviceManifest, DeviceRegistry


class FakeQtBot:
    def __init__(self):
        self.mode = "explore"
        self.state = "ok"
        self.sonar_cm = 30.0
        self.online = True
        self.battery = True
        self.line_left = False
        self.line_right = False
        self.commands: list[str] = []

    def status(self) -> dict:
        return {
            "online": self.online,
            "mode": self.mode,
            "state": self.state,
            "sonar_cm": self.sonar_cm,
            "line_left": self.line_left,
            "line_right": self.line_right,
            "battery": self.battery,
        }

    def poll_events(self, seconds: float = 0.5):
        del seconds
        return []

    def execute(self, command: str, **params):
        self.commands.append(command)
        return {"ok": True, "command": command, **params}

    def halt(self):
        return self.execute("halt")

    def explore(self):
        return self.execute("explore")

    def drive(self, left: int, right: int):
        return self.execute("drive", left=left, right=right)

    def close(self):
        pass


def _orch_manifest() -> DeviceManifest:
    """Manifest without confirmation gates so orchestrator tests hit the adapter."""
    return DeviceManifest(
        device_id=DEFAULT_DEVICE_ID,
        device_type="robot",
        name="Test QtBot",
        connection_type="serial",
        capabilities=[
            DeviceCapability(
                id="follow_line",
                name="Follow Line",
                description="test",
                category="actuator",
                permission_tier="active",
            ),
            DeviceCapability(
                id="explore",
                name="Explore",
                description="test",
                category="actuator",
                permission_tier="active",
            ),
            DeviceCapability(
                id="halt",
                name="Halt",
                description="test",
                category="actuator",
                permission_tier="passive",
            ),
            DeviceCapability(
                id="drive",
                name="Drive",
                description="test",
                category="actuator",
                permission_tier="dangerous",
                parameters=[
                    {"name": "left", "type": "integer", "required": True},
                    {"name": "right", "type": "integer", "required": True},
                ],
            ),
        ],
    )


@pytest.fixture
def orchestrator_setup(tmp_path):
    registry = DeviceRegistry()
    adapter = CuteBotAdapter(bot=FakeQtBot())
    registry.register_device(_orch_manifest(), adapter)

    class FakePerception:
        def __init__(self):
            self.updates = []

        def update_sensors(self, sid, sensors):
            self.updates.append((sid, sensors))

    perception = FakePerception()
    db_path = str(tmp_path / "ledger.db")
    orch = HardwareOrchestrator(
        registry=registry,
        ledger=CommandLedger(db_path=db_path),
        perception=perception,
    )
    return orch, registry, adapter, perception


class TestHardwareOrchestratorIntents:
    @pytest.mark.asyncio
    async def test_maps_patrol_to_follow_line(self, orchestrator_setup):
        orch, _, adapter, _ = orchestrator_setup
        out = await orch.execute_intent("s1", DEFAULT_DEVICE_ID, "patrol the track", timeout_s=0.1)
        assert out["ok"] is True
        assert out["capability_id"] == "follow_line"
        assert adapter._bot.commands[-1] == "follow_line"

    @pytest.mark.asyncio
    async def test_maps_explore_intent(self, orchestrator_setup):
        orch, _, adapter, _ = orchestrator_setup
        out = await orch.execute_intent("s1", DEFAULT_DEVICE_ID, "explore the table", timeout_s=0.1)
        assert out["capability_id"] == "explore"
        assert "explore" in adapter._bot.commands

    @pytest.mark.asyncio
    async def test_maps_stop_to_halt(self, orchestrator_setup):
        orch, _, adapter, _ = orchestrator_setup
        out = await orch.execute_intent("s1", DEFAULT_DEVICE_ID, "stop everything")
        assert out["capability_id"] == "halt"
        assert adapter._bot.commands[-1] == "halt"

    @pytest.mark.asyncio
    async def test_maps_back_up_to_drive(self, orchestrator_setup):
        orch, _, adapter, _ = orchestrator_setup
        out = await orch.execute_intent("s1", DEFAULT_DEVICE_ID, "back up a bit", timeout_s=0.1)
        assert out["capability_id"] == "drive"
        assert "drive" in adapter._bot.commands

    @pytest.mark.asyncio
    async def test_unknown_intent(self, orchestrator_setup):
        orch, _, _, _ = orchestrator_setup
        out = await orch.execute_intent("s1", DEFAULT_DEVICE_ID, "fly to the moon")
        assert out["ok"] is False
        assert "Unknown intent" in out["error"]


class TestHardwareOrchestratorStopConditions:
    @pytest.mark.asyncio
    async def test_timeout_stop_condition(self, orchestrator_setup):
        orch, _, _, _ = orchestrator_setup
        out = await orch.execute_intent(
            "s1",
            DEFAULT_DEVICE_ID,
            "explore",
            stop_conditions=[{"type": "timeout", "seconds": 0.15}],
            timeout_s=5.0,
        )
        assert out["ok"] is True
        assert out["stop_reason"] is not None
        assert out["stop_reason"]["reason"] == "timeout"

    @pytest.mark.asyncio
    async def test_sensor_stop_condition(self, orchestrator_setup):
        orch, _, adapter, _ = orchestrator_setup
        adapter._bot.sonar_cm = 8.0
        out = await orch.execute_intent(
            "s1",
            DEFAULT_DEVICE_ID,
            "explore",
            stop_conditions=[
                {"type": "sensor", "param": "sonar_cm", "op": "<", "value": 10},
                {"type": "timeout", "seconds": 2.0},
            ],
        )
        assert out["stop_reason"] is not None
        assert out["stop_reason"]["reason"] in {"sensor", "timeout"}
