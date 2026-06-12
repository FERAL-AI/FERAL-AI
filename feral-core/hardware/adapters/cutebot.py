"""CuteBot (QtBot) HUP adapter — brain-local USB serial robot."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from hardware.protocol import (
    DeviceCapability,
    DeviceManifest,
    HUPAction,
    HUPActionType,
    HUPResult,
)

logger = logging.getLogger("feral.hup.cutebot")

MOTION_CAPABILITIES = frozenset({"follow_line", "explore", "drive"})
DEFAULT_DEVICE_ID = "cutebot-usb-0"
DEFAULT_MAX_DRIVE_SPEED = 40

# A telemetry read that succeeded within this window keeps the robot "online"
# even if the most recent read raced/timed out (firmware streams at ~16 Hz, so
# anything older than this means the link is genuinely gone).
TELEMETRY_FRESH_S = 10.0
# Consecutive failed reads before the telemetry loop tears down the serial
# connection and starts reconnect attempts.
OFFLINE_DISCONNECT_THRESHOLD = 3


def _offline_state() -> dict[str, Any]:
    return {
        "online": False,
        "mode": "",
        "state": "",
        "sonar_cm": 0.0,
        "line_left": False,
        "line_right": False,
        "battery": False,
    }


class CuteBotAdapter:
    """Wraps ``cuteferalbot.device.QtBot`` as a HUP ``DeviceAdapter``."""

    def __init__(
        self,
        device_id: str = DEFAULT_DEVICE_ID,
        port: str | None = None,
        *,
        max_drive_speed: int = DEFAULT_MAX_DRIVE_SPEED,
        memory: Any = None,
        bot: Any = None,
    ):
        self.device_id = device_id
        self._port = port
        self.max_drive_speed = max(1, min(100, int(max_drive_speed)))
        self._memory = memory
        self._bot = bot
        self._connected = False
        self._telemetry_running = False
        # Serializes every touch of the underlying pyserial connection.
        # QtBot/CutebotClient are not thread-safe: the telemetry loop and
        # on-demand execute/read paths each run bot calls in worker threads,
        # and two concurrent readline()s on one Serial corrupt/raise.
        self._io_lock = asyncio.Lock()
        self._last_good_state: Optional[dict[str, Any]] = None
        self._last_good_at = 0.0
        self._offline_streak = 0

    @property
    def manifest(self) -> DeviceManifest:
        return DeviceManifest(
            device_id=self.device_id,
            device_type="robot",
            name="QtBot (CuteBot)",
            manufacturer="Elecfreaks",
            model="EF08209",
            connection_type="serial",
            location="desk",
            battery_powered=True,
            sensors=["sonar_cm", "line_left", "line_right", "light", "pitch_mg", "battery"],
            actuators=["motors", "headlights", "neopixels"],
            capabilities=[
                DeviceCapability(
                    id="follow_line",
                    name="Follow Line",
                    description="Autonomous line follow (black on white track)",
                    category="actuator",
                    permission_tier="active",
                    requires_confirmation=True,
                    reversible=True,
                    safety_notes="Ensure track is clear; firmware handles obstacle reflex.",
                ),
                DeviceCapability(
                    id="explore",
                    name="Explore Table",
                    description="Roam open surface with edge + obstacle safety",
                    category="actuator",
                    permission_tier="active",
                    requires_confirmation=True,
                ),
                DeviceCapability(
                    id="halt",
                    name="Emergency Stop",
                    description="Stop motors immediately",
                    category="actuator",
                    permission_tier="passive",
                    reversible=False,
                ),
                DeviceCapability(
                    id="drive",
                    name="Manual Drive",
                    description="Direct wheel speeds -100..100",
                    category="actuator",
                    permission_tier="dangerous",
                    requires_confirmation=True,
                    parameters=[
                        {"name": "left", "type": "integer", "required": True},
                        {"name": "right", "type": "integer", "required": True},
                    ],
                    safety_notes="Speed clamp enforced by adapter; auto-reverts after 1.5s.",
                ),
                DeviceCapability(
                    id="read_telemetry",
                    name="Read Telemetry",
                    description="Snapshot: sonar, line sensors, mode, state, battery",
                    category="sensor",
                    permission_tier="passive",
                ),
            ],
        )

    async def connect(self) -> bool:
        async with self._io_lock:
            if self._connected and self._bot is not None:
                return True
            if self._bot is not None:
                self._connected = True
                return True
            try:
                from cutebot.device import QtBot

                self._bot = await asyncio.to_thread(QtBot, port=self._port)
                self._connected = True
                self._offline_streak = 0
                logger.info("Connected CuteBot adapter %s", self.device_id)
                return True
            except Exception as exc:
                logger.warning("CuteBot connect failed for %s: %s", self.device_id, exc)
                self._connected = False
                self._bot = None
                return False

    async def disconnect(self) -> None:
        async with self._io_lock:
            bot = self._bot
            self._connected = False
            self._bot = None
            if bot is None:
                return
            try:
                await asyncio.to_thread(bot.close)
            except Exception as exc:
                logger.debug("CuteBot disconnect error: %s", exc)

    def _clamp_drive(self, left: int, right: int) -> tuple[int, int]:
        clamped_left = max(-100, min(100, int(left)))
        clamped_right = max(-100, min(100, int(right)))
        max_speed = self.max_drive_speed
        return (
            max(-max_speed, min(max_speed, clamped_left)),
            max(-max_speed, min(max_speed, clamped_right)),
        )

    def _normalize_status(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not raw or not raw.get("online", False):
            return _offline_state()
        return {
            "online": bool(raw.get("online", False)),
            "mode": str(raw.get("mode", "")),
            "state": str(raw.get("state", "")),
            "sonar_cm": float(raw.get("sonar_cm", 0.0) or 0.0),
            "line_left": bool(raw.get("line_left", False)),
            "line_right": bool(raw.get("line_right", False)),
            "battery": bool(raw.get("battery", False)),
        }

    def _telemetry_payload(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "mode": state.get("mode", ""),
            "state": state.get("state", ""),
            "sonar_cm": state.get("sonar_cm", 0.0),
            "online": state.get("online", False),
            "battery": state.get("battery", False),
        }

    def _cached_state_if_fresh(self) -> Optional[dict[str, Any]]:
        if (
            self._last_good_state is not None
            and (time.monotonic() - self._last_good_at) < TELEMETRY_FRESH_S
        ):
            return dict(self._last_good_state)
        return None

    async def get_state(self) -> dict[str, Any]:
        bot = self._bot
        if bot is None:
            return self._cached_state_if_fresh() or _offline_state()
        try:
            async with self._io_lock:
                raw = await asyncio.to_thread(bot.status)
            state = self._normalize_status(raw)
        except Exception as exc:
            logger.debug("CuteBot get_state failed: %s", exc)
            state = _offline_state()
        if state.get("online"):
            self._last_good_state = dict(state)
            self._last_good_at = time.monotonic()
            self._offline_streak = 0
            return state
        # One failed read is not proof the link is down — the firmware
        # streams ~16 Hz, so trust telemetry seen moments ago instead of
        # latching offline (and never tear down the connection from here;
        # the telemetry loop owns reconnect policy).
        self._offline_streak += 1
        return self._cached_state_if_fresh() or state

    async def get_status(self) -> dict[str, Any]:
        return await self.get_state()

    async def execute(self, action: HUPAction) -> HUPResult:
        cap_id = action.capability_id
        params = action.parameters or {}

        if cap_id == "halt":
            return await self._execute_halt(action)

        if cap_id in MOTION_CAPABILITIES:
            state = await self.get_state()
            if state.get("online") and not state.get("battery"):
                return HUPResult(
                    action_id=action.action_id,
                    device_id=self.device_id,
                    status="failure",
                    error="Battery power required for motion (USB-only power)",
                    data={"battery": False},
                )

        if cap_id == "follow_line":
            return await self._run_bot_command(action, "follow_line")
        if cap_id == "explore":
            return await self._run_bot_command(action, "explore")
        if cap_id == "drive":
            left, right = self._clamp_drive(
                int(params.get("left", 0)),
                int(params.get("right", 0)),
            )
            return await self._run_bot_command(action, "drive", left=left, right=right)
        if cap_id == "read_telemetry":
            state = await self.get_state()
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="success",
                data=state,
            )

        return HUPResult(
            action_id=action.action_id,
            device_id=self.device_id,
            status="failure",
            error=f"Unknown capability: {cap_id}",
        )

    async def _execute_halt(self, action: HUPAction) -> HUPResult:
        if self._bot is None:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="success",
                data={"command": "halt", "ok": True, "offline": True},
            )
        try:
            async with self._io_lock:
                result = await asyncio.to_thread(self._bot.halt)
            ok = bool(result.get("ok", False))
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="success" if ok else "failure",
                data=result,
                error="" if ok else "halt failed",
            )
        except Exception as exc:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="failure",
                error=str(exc),
            )

    async def _run_bot_command(
        self,
        action: HUPAction,
        command: str,
        **kwargs: Any,
    ) -> HUPResult:
        if self._bot is None:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="failure",
                error="CuteBot not connected",
            )
        try:
            async with self._io_lock:
                result = await asyncio.to_thread(self._bot.execute, command, **kwargs)
            ok = bool(result.get("ok", False))
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="success" if ok else "failure",
                data=result,
                error="" if ok else str(result.get("error", "command failed")),
            )
        except Exception as exc:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="failure",
                error=str(exc),
            )

    async def emergency_stop(self) -> HUPResult:
        action = HUPAction(
            device_id=self.device_id,
            capability_id="halt",
            action_type=HUPActionType.EXECUTE,
            priority=2,
        )
        return await self.execute(action)

    async def poll_events(self, seconds: float = 0.5) -> list[dict[str, Any]]:
        bot = self._bot
        if bot is None:
            return []
        try:
            async with self._io_lock:
                return await asyncio.to_thread(bot.poll_events, seconds)
        except Exception as exc:
            logger.debug("CuteBot poll_events failed: %s", exc)
            return []

    async def _save_episode(self, memory: Any, summary: str, metadata: dict[str, Any]) -> None:
        if memory is None:
            return
        episode_save = getattr(memory, "episode_save", None)
        if episode_save is None:
            return
        try:
            await episode_save(
                summary,
                metadata={
                    "category": "actuator",
                    "actuator": "robot",
                    "device_id": self.device_id,
                    "source": "cutebot",
                    **metadata,
                },
            )
        except Exception as exc:
            logger.debug("CuteBot episode_save failed: %s", exc)

    async def start_telemetry_loop(
        self,
        perception: Any,
        get_session_ids: Callable[[], list[str]],
        *,
        memory: Any = None,
    ) -> None:
        """Poll QtBot telemetry ~1 Hz and push ``{"robot": {...}}`` to perception."""
        self._telemetry_running = True
        memory_handle = memory if memory is not None else self._memory
        reconnect_interval = 5.0

        while self._telemetry_running:
            try:
                if not self._connected or self._bot is None:
                    if not await self.connect():
                        offline = _offline_state()
                        for sid in get_session_ids():
                            perception.update_sensors(sid, {"robot": self._telemetry_payload(offline)})
                        await asyncio.sleep(reconnect_interval)
                        continue

                events = await self.poll_events(1.0)
                state = await self.get_state()

                if not state.get("online", False):
                    # Only tear the port down after several consecutive
                    # misses — a single timeout/raced read is routine when
                    # on-demand reads interleave with this loop.
                    if self._offline_streak >= OFFLINE_DISCONNECT_THRESHOLD:
                        await self.disconnect()
                        for sid in get_session_ids():
                            perception.update_sensors(sid, {"robot": self._telemetry_payload(state)})
                        await asyncio.sleep(reconnect_interval)
                        continue

                robot_payload = self._telemetry_payload(state)
                for sid in get_session_ids():
                    perception.update_sensors(sid, {"robot": robot_payload})

                for event in events:
                    name = event.get("event", "")
                    if name == "state_changed" and event.get("state") == "gave_up":
                        await self._save_episode(
                            memory_handle,
                            "CuteBot lost the line (gave_up)",
                            {"event": "gave_up", "state": "gave_up", "ts": time.time()},
                        )
                    elif name == "obstacle":
                        await self._save_episode(
                            memory_handle,
                            f"CuteBot obstacle at {event.get('distance_cm')}cm",
                            {"event": "obstacle", **event, "ts": time.time()},
                        )
                    elif name == "mode_changed" and not state.get("online", True):
                        await self._save_episode(
                            memory_handle,
                            "CuteBot disconnected",
                            {"event": "disconnect", "ts": time.time()},
                        )

                # Breathing room so on-demand reads waiting on the io lock
                # are not starved by back-to-back loop iterations.
                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("CuteBot telemetry loop error: %s", exc)
                self._connected = False
                await asyncio.sleep(reconnect_interval)

    def stop_telemetry_loop(self) -> None:
        self._telemetry_running = False
