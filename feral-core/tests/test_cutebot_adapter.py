"""Tests for CuteBotAdapter and DeviceRegistry HUPResult normalization."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from hardware.adapters.cutebot import (
    CuteBotAdapter,
    DEFAULT_DEVICE_ID,
    TELEMETRY_FRESH_S,
)
from hardware.protocol import (
    DeviceCapability,
    DeviceManifest,
    DeviceRegistry,
    HUPAction,
    HUPActionType,
    HUPResult,
)


class FakeQtBot:
    """In-memory QtBot stand-in — no USB serial required."""

    def __init__(self, *, online: bool = True, battery: bool = True):
        self.online = online
        self.battery = battery
        self.mode = "line_follow"
        self.state = "ok"
        self.sonar_cm = 14.0
        self.line_left = True
        self.line_right = False
        self.commands: list[tuple[str, dict]] = []
        self.closed = False
        self._events: list[dict] = []

    @staticmethod
    def available() -> bool:
        return True

    def status(self) -> dict:
        if not self.online:
            return {"online": False}
        return {
            "online": True,
            "mode": self.mode,
            "state": self.state,
            "sonar_cm": self.sonar_cm,
            "line_left": self.line_left,
            "line_right": self.line_right,
            "light": 12,
            "pitch_mg": 992,
            "battery": self.battery,
        }

    def poll_events(self, seconds: float = 1.0) -> list[dict]:
        del seconds
        events, self._events = self._events, []
        return events

    def queue_event(self, event: dict) -> None:
        self._events.append(event)

    def execute(self, command: str, **params):
        self.commands.append((command, params))
        if command == "drive":
            return {"ok": True, "command": "drive", **params}
        return {"ok": True, "command": command}

    def follow_line(self):
        return self.execute("follow_line")

    def explore(self):
        return self.execute("explore")

    def halt(self):
        return self.execute("halt")

    def drive(self, left: int, right: int):
        return self.execute("drive", left=left, right=right)

    def close(self) -> None:
        self.closed = True
        self.online = False


def _action(capability_id: str, **params) -> HUPAction:
    return HUPAction(
        device_id=DEFAULT_DEVICE_ID,
        capability_id=capability_id,
        action_type=HUPActionType.EXECUTE,
        parameters=params,
    )


@pytest.fixture
def adapter() -> CuteBotAdapter:
    bot = FakeQtBot()
    return CuteBotAdapter(bot=bot, max_drive_speed=40)


class TestCuteBotManifest:
    def test_manifest_matches_plan(self, adapter: CuteBotAdapter):
        m = adapter.manifest
        assert m.device_id == "cutebot-usb-0"
        assert m.device_type == "robot"
        assert m.connection_type == "serial"
        assert m.name == "QtBot (CuteBot)"
        assert m.manufacturer == "Elecfreaks"
        assert m.model == "EF08209"
        assert m.location == "desk"
        assert m.battery_powered is True
        assert set(m.sensors) == {
            "sonar_cm", "line_left", "line_right", "light", "pitch_mg", "battery"
        }
        assert set(m.actuators) == {"motors", "headlights", "neopixels"}

        caps = {c.id: c for c in m.capabilities}
        assert set(caps) == {"follow_line", "explore", "halt", "drive", "read_telemetry"}

        assert caps["follow_line"].permission_tier == "active"
        assert caps["follow_line"].requires_confirmation is True
        assert caps["explore"].requires_confirmation is True
        assert caps["halt"].permission_tier == "passive"
        assert caps["halt"].requires_confirmation is False
        assert caps["drive"].permission_tier == "dangerous"
        assert caps["drive"].requires_confirmation is True
        drive_params = {p["name"] for p in caps["drive"].parameters}
        assert drive_params == {"left", "right"}
        assert caps["read_telemetry"].category == "sensor"
        assert caps["read_telemetry"].permission_tier == "passive"


class TestCuteBotExecute:
    @pytest.mark.asyncio
    async def test_drive_clamps_to_max_speed(self, adapter: CuteBotAdapter):
        result = await adapter.execute(_action("drive", left=90, right=-80))
        assert result.status == "success"
        assert adapter._bot.commands[-1] == ("drive", {"left": 40, "right": -40})

    @pytest.mark.asyncio
    async def test_refuses_motion_when_battery_off(self, adapter: CuteBotAdapter):
        adapter._bot.battery = False
        for cap in ("follow_line", "explore", "drive"):
            result = await adapter.execute(_action(cap, left=10, right=10))
            assert result.status == "failure"
            assert "battery" in result.error.lower()

    @pytest.mark.asyncio
    async def test_halt_always_works_without_battery(self, adapter: CuteBotAdapter):
        adapter._bot.battery = False
        result = await adapter.execute(_action("halt"))
        assert result.status == "success"
        assert adapter._bot.commands[-1][0] == "halt"

    @pytest.mark.asyncio
    async def test_get_state_shape(self, adapter: CuteBotAdapter):
        state = await adapter.get_state()
        assert set(state.keys()) == {
            "online", "mode", "state", "sonar_cm", "line_left", "line_right", "battery"
        }


class TestCuteBotTelemetry:
    @pytest.mark.asyncio
    async def test_telemetry_robot_contract(self, adapter: CuteBotAdapter):
        perception = MagicMock()
        session_ids = ["sess-1"]

        async def run_once():
            state = await adapter.get_state()
            payload = adapter._telemetry_payload(state)
            for sid in session_ids:
                perception.update_sensors(sid, {"robot": payload})

        await run_once()
        perception.update_sensors.assert_called_once()
        sensors = perception.update_sensors.call_args[0][1]
        assert set(sensors.keys()) == {"robot"}
        robot = sensors["robot"]
        assert set(robot.keys()) == {"mode", "state", "sonar_cm", "online", "battery"}
        assert robot["mode"] == "line_follow"
        assert robot["state"] == "ok"
        assert robot["sonar_cm"] == 14.0
        assert robot["online"] is True
        assert robot["battery"] is True

    @pytest.mark.asyncio
    async def test_telemetry_loop_pushes_robot_key(self, adapter: CuteBotAdapter):
        perception = MagicMock()
        adapter._telemetry_running = True

        async def stop_soon():
            await asyncio.sleep(0.05)
            adapter.stop_telemetry_loop()

        stopper = asyncio.create_task(stop_soon())
        await adapter.start_telemetry_loop(
            perception,
            lambda: ["sess-a"],
            memory=None,
        )
        await stopper

        assert perception.update_sensors.called
        robot = perception.update_sensors.call_args[0][1]["robot"]
        assert set(robot.keys()) == {"mode", "state", "sonar_cm", "online", "battery"}


class SerialContentionQtBot(FakeQtBot):
    """Fake that detects concurrent serial access (the real bug).

    The real pyserial connection raises ("device reports readiness to read
    but returned no data") when two threads readline() at once. This fake
    records overlap instead, so the test fails if the adapter ever lets the
    telemetry loop and an on-demand read touch the port simultaneously.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._busy = threading.Lock()
        self.overlap_detected = False

    def _enter_port(self):
        if not self._busy.acquire(blocking=False):
            self.overlap_detected = True
            raise RuntimeError(
                "device reports readiness to read but returned no data"
            )

    def status(self) -> dict:
        self._enter_port()
        try:
            time.sleep(0.02)
            return super().status()
        finally:
            self._busy.release()

    def poll_events(self, seconds: float = 1.0) -> list[dict]:
        self._enter_port()
        try:
            time.sleep(0.02)
            return super().poll_events(seconds)
        finally:
            self._busy.release()


class TestSerialAccessSerialization:
    """Pin the live-hardware bug: concurrent serial access reported offline."""

    @pytest.mark.asyncio
    async def test_concurrent_reads_never_overlap_on_port(self):
        bot = SerialContentionQtBot()
        adapter = CuteBotAdapter(bot=bot)
        results = await asyncio.gather(
            *(adapter.get_state() for _ in range(5)),
            adapter.poll_events(0.01),
            adapter.get_state(),
        )
        assert bot.overlap_detected is False
        states = [r for r in results if isinstance(r, dict)]
        assert all(s["online"] is True for s in states)

    @pytest.mark.asyncio
    async def test_read_during_telemetry_loop_stays_online(self):
        bot = SerialContentionQtBot()
        adapter = CuteBotAdapter(bot=bot)
        adapter._connected = True
        perception = MagicMock()

        loop_task = asyncio.create_task(
            adapter.start_telemetry_loop(perception, lambda: ["sess-1"])
        )
        try:
            await asyncio.sleep(0.05)
            # On-demand reads racing the loop, like the HTTP execute path.
            for _ in range(5):
                state = await adapter.get_state()
                assert state["online"] is True
                await asyncio.sleep(0.02)
        finally:
            adapter.stop_telemetry_loop()
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        assert bot.overlap_detected is False

    @pytest.mark.asyncio
    async def test_transient_read_failure_serves_fresh_cache(self):
        bot = FakeQtBot()
        adapter = CuteBotAdapter(bot=bot)
        good = await adapter.get_state()
        assert good["online"] is True

        bot.status = MagicMock(side_effect=RuntimeError("raced read"))
        state = await adapter.get_state()
        assert state["online"] is True
        assert state["sonar_cm"] == good["sonar_cm"]

    @pytest.mark.asyncio
    async def test_stale_cache_reports_offline(self):
        bot = FakeQtBot()
        adapter = CuteBotAdapter(bot=bot)
        assert (await adapter.get_state())["online"] is True

        bot.status = MagicMock(side_effect=RuntimeError("link gone"))
        adapter._last_good_at -= TELEMETRY_FRESH_S + 1
        state = await adapter.get_state()
        assert state["online"] is False

    @pytest.mark.asyncio
    async def test_failed_read_does_not_disconnect_bot(self):
        bot = FakeQtBot()
        adapter = CuteBotAdapter(bot=bot)
        bot.status = MagicMock(side_effect=RuntimeError("raced read"))
        await adapter.get_state()
        assert adapter._bot is bot
        assert bot.closed is False


class TestDeviceRegistryHUPResultNormalization:
    @pytest.mark.asyncio
    async def test_dict_adapter_return(self):
        class DictAdapter:
            async def execute(self, action):
                return {"mock": True, "cap": action.capability_id}

        manifest = DeviceManifest(
            device_id="dict-dev",
            device_type="robot",
            name="Dict Dev",
            capabilities=[
                DeviceCapability(
                    id="ping",
                    name="Ping",
                    description="test",
                    category="sensor",
                    permission_tier="passive",
                )
            ],
        )
        reg = DeviceRegistry()
        reg.register_device(manifest, DictAdapter())
        result = await reg.execute_action(
            HUPAction(
                device_id="dict-dev",
                capability_id="ping",
                action_type=HUPActionType.READ,
            )
        )
        assert result.status == "success"
        assert result.data["mock"] is True

    @pytest.mark.asyncio
    async def test_hupresult_adapter_return(self):
        class ResultAdapter:
            async def execute(self, action):
                return HUPResult(
                    action_id=action.action_id,
                    device_id=action.device_id,
                    status="success",
                    data={"joints": [0, 0, 0]},
                )

        manifest = DeviceManifest(
            device_id="result-dev",
            device_type="robot",
            name="Result Dev",
            capabilities=[
                DeviceCapability(
                    id="read_position",
                    name="Read",
                    description="test",
                    category="sensor",
                    permission_tier="passive",
                )
            ],
        )
        reg = DeviceRegistry()
        reg.register_device(manifest, ResultAdapter())
        result = await reg.execute_action(
            HUPAction(
                device_id="result-dev",
                capability_id="read_position",
                action_type=HUPActionType.READ,
            )
        )
        assert result.status == "success"
        assert result.data["joints"] == [0, 0, 0]
