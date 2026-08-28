"""CuteBot (QtBot) HUP adapter — brain-local USB serial robot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

from hardware.async_io_lock import PriorityIOLock
from hardware.protocol import (
    DeviceCapability,
    DeviceManifest,
    HUPAction,
    HUPActionType,
    HUPResult,
    device_manifest_from_capabilities,
)

logger = logging.getLogger("feral.hup.cutebot")

MOTION_CAPABILITIES = frozenset({"follow_line", "explore", "drive", "resume"})
# Closed-loop navigation capabilities (harvested from cuteferalbot brain/).
# These only appear in the device manifest after a pose source is attached
# via attach_navigator(); the generic HUP skill then exposes them for free.
NAV_CAPABILITIES = frozenset({"go_to", "patrol", "stop_navigation"})
DEFAULT_DEVICE_ID = "cutebot-usb-0"
DEFAULT_MAX_DRIVE_SPEED = 40

# A telemetry read that succeeded within this window keeps the robot "online"
# even if the most recent read raced/timed out (firmware streams at ~16 Hz, so
# anything older than this means the link is genuinely gone).
TELEMETRY_FRESH_S = 10.0
# Consecutive failed reads before the telemetry loop tears down the serial
# connection and starts reconnect attempts.
OFFLINE_DISCONNECT_THRESHOLD = 3
# Longest single hold the telemetry drain may take on the serial port. It
# bounds how long an emergency stop can sit behind a routine read: the
# drain is chopped into slices this long and gives up the port between
# them. Each slice is still a complete device call, so nothing partial is
# ever left on the wire.
IO_SLICE_SECONDS = 0.2


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
        #
        # Two lanes: normal, and an urgent lane the halt path uses so an
        # emergency stop is handed the port ahead of everything queued.
        self._io_lock = PriorityIOLock()
        self._last_good_state: Optional[dict[str, Any]] = None
        self._last_good_at = 0.0
        self._offline_streak = 0

    @property
    def manifest(self) -> DeviceManifest:
        """The device's capability manifest.

        When the robot is connected we build it from the device's OWN
        self-description (``QtBot.capabilities()["actions"]``) — the generic
        HUP path where the brain learns the device from the device itself,
        not from hardcoded knowledge here. The rich static manifest below is
        the fallback used before connect or if the device predates the
        ``actions`` self-description.
        """
        bot = self._bot
        if bot is not None:
            try:
                caps = bot.capabilities()
                actions = caps.get("actions") if isinstance(caps, dict) else None
                if actions:
                    return self._manifest_from_descriptors(caps, actions)
            except Exception as exc:
                logger.debug(
                    "CuteBot self-description discovery failed (%s); "
                    "using static manifest",
                    exc,
                )
        return self._static_manifest()

    def _manifest_from_descriptors(
        self, caps: dict[str, Any], actions: list[dict[str, Any]]
    ) -> DeviceManifest:
        """Build a DeviceManifest from the device's own rich action descriptors.

        No per-capability knowledge lives here — the capability list is built
        by the SHARED, generic ``device_manifest_from_capabilities`` HUP
        converter (the same one any transport adapter uses); this method only
        supplies the CuteBot's static identity (name/model/location). This is
        the literal realization of the DeviceManifest docstring: "the agent
        reads this manifest to understand what the device can do without any
        device-specific code."
        """
        return device_manifest_from_capabilities(
            self.device_id,
            caps,
            name="QtBot (CuteBot)",
            manufacturer="Elecfreaks",
            model="EF08209",
            connection_type="serial",
            location="desk",
            battery_powered=True,
            type_aliases={"qtbot": "robot"},
            actuators=["motors", "headlights", "neopixels"],
        )

    def _static_manifest(self) -> DeviceManifest:
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
                    # Closed-loop honesty contract for the generic HUP skill:
                    # confirm the robot actually entered line-follow mode.
                    verify={
                        "via": "read_telemetry",
                        "field": "mode",
                        "expect": ["line_follow", "track", "T"],
                    },
                ),
                DeviceCapability(
                    id="explore",
                    name="Explore Table",
                    description="Roam open surface with edge + obstacle safety",
                    category="actuator",
                    permission_tier="active",
                    requires_confirmation=True,
                    verify={
                        "via": "read_telemetry",
                        "field": "mode",
                        "expect": ["explore", "table", "E"],
                    },
                ),
                DeviceCapability(
                    id="halt",
                    name="Emergency Stop",
                    description="Stop motors immediately",
                    category="actuator",
                    permission_tier="passive",
                    reversible=False,
                    verify={
                        "via": "read_telemetry",
                        "field": "mode",
                        "expect": ["stopped", "M"],
                    },
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
                    id="set_lights",
                    name="Set Lights",
                    description="Set headlight + underglow RGB color (0-255) for expression or signaling",
                    category="actuator",
                    permission_tier="passive",
                    reversible=True,
                    parameters=[
                        {"name": "r", "type": "integer", "required": True},
                        {"name": "g", "type": "integer", "required": True},
                        {"name": "b", "type": "integer", "required": True},
                    ],
                    safety_notes="Lights only; runs on USB power, no motion, no battery needed.",
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
        """Dispatch a capability to the bot.

        The body is GENERIC passthrough — ``QtBot.execute(cap_id, **params)``
        handles every self-described command, so a new capability the device
        advertises works with no new branch here. Only the CuteBot's own
        *safety* lives as explicit steps: an emergency-stop fast path, a
        motion battery gate, and parameter hardening (drive clamp / RGB clamp /
        nav coercion). Everything else falls through to passthrough.
        """
        cap_id = action.capability_id
        params = dict(action.parameters or {})

        # Emergency stop: works even offline; never blocked by the battery gate.
        if cap_id == "halt":
            return await self._execute_halt(action)

        # Motion safety: refuse to command motion when the robot is on USB
        # power only (no battery) — it cannot move and would dead-end.
        if cap_id in MOTION_CAPABILITIES or cap_id in NAV_CAPABILITIES:
            state = await self.get_state()
            if state.get("online") and not state.get("battery"):
                return HUPResult(
                    action_id=action.action_id,
                    device_id=self.device_id,
                    status="failure",
                    error="Battery power required for motion (USB-only power)",
                    data={"battery": False},
                )

        # Pure sensor read short-circuits to fresh telemetry.
        if cap_id == "read_telemetry":
            state = await self.get_state()
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="success",
                data=state,
            )

        if cap_id in NAV_CAPABILITIES:
            return await self._run_nav_command(action, cap_id, params)

        # Generic passthrough for every actuator capability (follow_line,
        # explore, resume, drive, set_lights, …) after device-specific
        # parameter hardening.
        params = self._harden_params(cap_id, params)
        return await self._run_bot_command(action, cap_id, **params)

    def _harden_params(self, cap_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Clamp safety-relevant parameters before passthrough (CuteBot-only)."""
        if cap_id == "drive":
            left, right = self._clamp_drive(
                int(params.get("left", 0)),
                int(params.get("right", 0)),
            )
            params["left"], params["right"] = left, right
        elif cap_id == "set_lights":
            for ch in ("r", "g", "b"):
                params[ch] = max(0, min(255, int(params.get(ch, 0))))
        return params

    async def _execute_halt(self, action: HUPAction) -> HUPResult:
        if self._bot is None:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="success",
                data={"command": "halt", "ok": True, "offline": True},
            )
        try:
            # The urgent lane. An emergency stop is handed the serial port
            # ahead of every queued telemetry read and every queued
            # actuator command, including the one it is meant to abort.
            async with self._io_lock.urgent():
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

    async def _run_nav_command(
        self, action: HUPAction, cap_id: str, params: dict[str, Any]
    ) -> HUPResult:
        """Dispatch a closed-loop navigation command to the bot.

        Delegates to ``QtBot.execute`` (which only advertises these when a
        navigator is attached, so an un-attached bot returns an honest
        "unknown command" / "no navigator" failure rather than pretending).
        """
        if cap_id == "go_to":
            kwargs: dict[str, Any] = {
                "x_cm": float(params.get("x_cm", 0.0)),
                "y_cm": float(params.get("y_cm", 0.0)),
            }
            if params.get("tolerance_cm") is not None:
                kwargs["tolerance_cm"] = float(params["tolerance_cm"])
            if params.get("timeout_s") is not None:
                kwargs["timeout_s"] = float(params["timeout_s"])
        elif cap_id == "patrol":
            kwargs = {"waypoints": list(params.get("waypoints", []) or [])}
            if params.get("repeat") is not None:
                kwargs["repeat"] = bool(params["repeat"])
        else:  # stop_navigation
            kwargs = {}
        return await self._run_bot_command(action, cap_id, **kwargs)

    def attach_navigator(self, pose_source: Any) -> dict[str, Any]:
        """Attach a closed-loop pose source so the bot offers go_to/patrol.

        Once attached, ``QtBot.capabilities()['actions']`` includes the
        navigation commands, which the generic HUP skill exposes to the LLM
        automatically — no FERAL skill code per command. ``pose_source`` must
        expose ``latest() -> Pose|None`` (e.g. cuteferalbot's ArUco
        ``Localizer``). Returns the bot's ack dict.

        NOTE: live closed-loop navigation requires coordinating serial access
        with the telemetry loop; callers should pause/own telemetry while a
        navigation task runs. Not attached at boot by default.
        """
        if self._bot is None:
            return {"ok": False, "error": "CuteBot not connected"}
        attach = getattr(self._bot, "attach_navigator", None)
        if attach is None:
            return {"ok": False, "error": "bot does not support navigation"}
        return attach(pose_source)

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
        """Drain queued device events for up to ``seconds``.

        Split into short slices instead of one long hold. The telemetry
        loop calls this with 1.0s once a second, forever, so holding the
        port for the whole second put an emergency stop behind a routine
        read: measured at 0.95s before this change. Each slice is a
        complete ``poll_events`` call under the lock, so the wire never
        sees a partial transaction; anything the firmware emits between
        slices sits in the OS serial buffer and is picked up by the next
        one. The drain gives up its remaining slices the moment a safety
        command is waiting.
        """
        bot = self._bot
        if bot is None:
            return []
        remaining = max(0.0, float(seconds))
        events: list[dict[str, Any]] = []
        try:
            while True:
                slice_s = min(IO_SLICE_SECONDS, remaining)
                async with self._io_lock:
                    chunk = await asyncio.to_thread(bot.poll_events, slice_s)
                if chunk:
                    events.extend(chunk)
                remaining -= slice_s
                if remaining <= 0:
                    break
                if self._io_lock.preempt_requested:
                    logger.debug(
                        "CuteBot telemetry drain yielding the port to a "
                        "safety command with %.2fs left", remaining,
                    )
                    break
        except Exception as exc:
            logger.debug("CuteBot poll_events failed: %s", exc)
            return events
        return events

    async def _save_episode(
        self,
        memory: Any,
        session_id: str,
        event_type: str,
        summary: str,
        detail: dict[str, Any] | None = None,
        *,
        importance: float = 0.6,
    ) -> None:
        if memory is None:
            return
        episode_save = getattr(memory, "episode_save", None)
        if episode_save is None:
            return
        meta = {
            "category": "actuator",
            "actuator": "robot",
            "device_id": self.device_id,
            "source": "cutebot",
            **(detail or {}),
        }
        try:
            # MemoryStore.episode_save(session_id, event_type, summary,
            # detail="", ..., importance=0.5). The structured context goes
            # into ``detail`` so it is embedded for semantic recall.
            await episode_save(
                session_id=session_id,
                event_type=event_type,
                summary=summary,
                detail=json.dumps(meta, default=str),
                location="robot",
                importance=importance,
            )
        except Exception as exc:
            # Warning, not debug. A dropped actuator episode is robot
            # history that recall can never surface again ("what did my
            # robot do yesterday?"), and debug is off in every normal
            # deployment, so the loss was unobservable.
            logger.warning("CuteBot episode_save failed: %s", exc)

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
                session_ids = get_session_ids()

                if not state.get("online", False):
                    # Only tear the port down after several consecutive
                    # misses — a single timeout/raced read is routine when
                    # on-demand reads interleave with this loop.
                    if self._offline_streak >= OFFLINE_DISCONNECT_THRESHOLD:
                        await self.disconnect()
                        for sid in session_ids:
                            perception.update_sensors(sid, {"robot": self._telemetry_payload(state)})
                        await asyncio.sleep(reconnect_interval)
                        continue

                robot_payload = self._telemetry_payload(state)
                for sid in session_ids:
                    perception.update_sensors(sid, {"robot": robot_payload})

                # Episodes need a session to hang off; recall is by semantic
                # similarity (cross-session), so this is just a stable anchor.
                episode_session = session_ids[0] if session_ids else "cutebot-autonomous"
                for event in events:
                    name = event.get("event", "")
                    if name == "state_changed" and event.get("state") == "gave_up":
                        await self._save_episode(
                            memory_handle,
                            episode_session,
                            "robot_event",
                            "The CuteBot robot lost the black line and gave up searching for it.",
                            {"event": "gave_up", "state": "gave_up", "ts": time.time()},
                            importance=0.7,
                        )
                    elif name == "obstacle":
                        await self._save_episode(
                            memory_handle,
                            episode_session,
                            "robot_event",
                            f"The CuteBot robot detected an obstacle {event.get('distance_cm')}cm ahead and stopped.",
                            {"event": "obstacle", **event, "ts": time.time()},
                            importance=0.7,
                        )
                    elif name == "mode_changed" and not state.get("online", True):
                        await self._save_episode(
                            memory_handle,
                            episode_session,
                            "robot_event",
                            "The CuteBot robot disconnected from the brain.",
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
