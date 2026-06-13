"""CuteBot skill — routes LLM tool calls to the brain-local DeviceRegistry.

Motion commands are CLOSED-LOOP: a firmware ack alone is never trusted.
After dispatch the skill waits for the robot's own telemetry to confirm the
mode change, retries once if it didn't take, and otherwise fails loudly so
the LLM reports reality instead of celebrating a robot that never moved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.cutebot")

DEVICE_ID = "cutebot-usb-0"
NOT_CONNECTED_ERROR = "CuteBot is not connected (no USB robot found)"

# endpoint_id -> (capability_id, action_type_name)
_ENDPOINT_MAP: dict[str, Tuple[str, str]] = {
    "follow_line": ("follow_line", "execute"),
    "explore": ("explore", "execute"),
    "drive": ("drive", "execute"),
    "halt": ("halt", "execute"),
    "status": ("read_telemetry", "read"),
}

# Firmware mode strings come from cutebot.device.MODE_NAMES:
#   "T" -> "line_follow", "E" -> "explore", "M" -> "stopped", "R" -> "remote".
# Raw single-letter codes are accepted too in case a newer firmware adds a
# mode the host-side map doesn't know yet.
_EXPECTED_MODES: dict[str, tuple[str, ...]] = {
    "follow_line": ("line_follow", "track", "T"),
    "explore": ("explore", "table", "E"),
    "halt": ("stopped", "M"),
}

# How long to let the firmware settle into the new mode before reading
# telemetry back. Firmware acks in <100ms and streams telemetry at ~16Hz,
# but mode transitions (e.g. line calibration) can take a beat.
VERIFY_DELAY_S = 1.6
# Worst case added latency: delay + read + one retry + delay + read ≈ 4s.
VERIFY_RETRIES = 1


def _state():
    try:
        from api.state import state
        return state
    except Exception:
        return None


@register_skill
class CuteBotSkill(BaseSkill):
    def __init__(self):
        super().__init__(skill_id="cutebot")
        self._device_registry_override: Optional[Any] = None
        # Test hook — pytest sets this to 0 so verification is instant.
        self.verify_delay_s: float = VERIFY_DELAY_S

    def set_device_registry(self, registry: Any) -> None:
        """Test hook — inject a mock DeviceRegistry without booting the brain."""
        self._device_registry_override = registry

    def _get_device_registry(self) -> Optional[Any]:
        if self._device_registry_override is not None:
            return self._device_registry_override
        state = _state()
        if state is None:
            return None
        return getattr(state, "device_registry", None)

    def _is_device_registered(self, registry: Any) -> bool:
        if registry is None:
            return False
        if hasattr(registry, "get_device"):
            return registry.get_device(DEVICE_ID) is not None
        devices = getattr(registry, "_devices", None)
        if isinstance(devices, dict):
            return DEVICE_ID in devices
        return False

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]
    ) -> Dict[str, Any]:
        # Explicit chain (not `in _ENDPOINT_MAP`) so the ToolDispatchValidator
        # source scan keeps seeing each endpoint id — the manifest↔backend
        # contract gate parses this function's source.
        if endpoint_id == "follow_line":
            return await self._run_capability(endpoint_id, args)
        if endpoint_id == "explore":
            return await self._run_capability(endpoint_id, args)
        if endpoint_id == "drive":
            return await self._run_capability(endpoint_id, args)
        if endpoint_id == "halt":
            return await self._run_capability(endpoint_id, args)
        if endpoint_id == "status":
            return await self._run_capability(endpoint_id, args)
        return {
            "success": False,
            "status_code": 400,
            "data": None,
            "error": f"Unknown CuteBot endpoint: {endpoint_id}",
        }

    # ── HUP plumbing ─────────────────────────────────────────────────

    async def _dispatch(
        self,
        registry: Any,
        endpoint_id: str,
        capability_id: str,
        action_type_name: str,
        args: Dict[str, Any],
    ) -> Tuple[str, Any, Optional[str]]:
        """Send one HUPAction and normalize the result to (status, data, error)."""
        from hardware.protocol import HUPAction, HUPActionType, HUPResult

        action = HUPAction(
            action_id=f"skill_cutebot_{endpoint_id}_{uuid4().hex[:8]}",
            device_id=DEVICE_ID,
            capability_id=capability_id,
            action_type=HUPActionType(action_type_name),
            parameters=dict(args or {}),
            # ToolRunner's safety layer already enforced the per-tool
            # approval tier (CONFIRM/DENY) before this skill ran, so the
            # registry's own confirmation gate must not block us again —
            # it has no resume path and the command would silently die.
            confirmed=True,
        )
        result = await registry.execute_action(action)
        if isinstance(result, dict):
            return result.get("status", "failure"), result.get("data"), result.get("error") or None
        if isinstance(result, HUPResult):
            return result.status, result.data, result.error or None
        return "success", result, None

    async def _read_robot_state(self, registry: Any) -> Optional[Dict[str, Any]]:
        """Fresh telemetry snapshot via the same registry path, or None."""
        try:
            status, data, _error = await self._dispatch(
                registry, "status", "read_telemetry", "read", {}
            )
        except Exception as exc:
            logger.warning("CuteBot verification telemetry read failed: %s", exc)
            return None
        if status == "success" and isinstance(data, dict):
            return data
        return None

    # ── Closed-loop verification ─────────────────────────────────────

    async def _verify_motion(
        self,
        registry: Any,
        endpoint_id: str,
        capability_id: str,
        args: Dict[str, Any],
        ack_data: Any,
    ) -> Dict[str, Any]:
        """Confirm a motion command actually took effect on the robot.

        follow_line/explore: telemetry mode must match; one retry nudge.
        halt: single check, no retry — a halt failure is never masked.
        drive: transient by design (firmware auto-reverts to autonomous
        after 1.5s), so just report the post-state without failing.
        """
        base_data: Dict[str, Any] = dict(ack_data) if isinstance(ack_data, dict) else {}

        await asyncio.sleep(self.verify_delay_s)
        state = await self._read_robot_state(registry)

        if endpoint_id == "drive":
            # Drive cannot be mode-verified: the firmware holds the wheel
            # speeds for only 1.5s and then reverts, so "stopped" afterwards
            # is normal. Report the post-state as ground truth.
            base_data.update({
                "verified": state is not None,
                "transient": True,
                "telemetry": state,
            })
            if state is None:
                base_data["verify_note"] = (
                    "Drive was acked but post-action telemetry could not be "
                    "read; robot state unknown."
                )
            else:
                base_data["verify_note"] = (
                    "Drive pulses are transient — firmware auto-reverts after "
                    "1.5s, so a 'stopped' post-state is normal."
                )
            return {"success": True, "status_code": 200, "data": base_data, "error": None}

        expected = _EXPECTED_MODES.get(endpoint_id, ())
        observed = (state or {}).get("mode", "")
        online = bool((state or {}).get("online", False))

        if endpoint_id == "halt":
            if state is not None and online and observed not in expected:
                error = (
                    f"HALT WAS ACKED BUT THE ROBOT IS STILL IN MODE "
                    f"'{observed}' — it may still be moving. Re-issue "
                    f"cutebot__halt and warn the user immediately."
                )
                base_data.update({
                    "verified": False,
                    "expected_mode": "stopped",
                    "observed_mode": observed,
                    "telemetry": state,
                    "error": error,
                })
                return {"success": False, "status_code": 500, "data": base_data, "error": error}
            # Offline/unreadable telemetry after a halt ack: the link is
            # gone, not the stop — the adapter already treats offline halt
            # as a no-op success. Report what we know without inventing
            # a failure (but mark verified honestly).
            base_data.update({
                "verified": state is not None and observed in expected,
                "expected_mode": "stopped",
                "observed_mode": observed,
                "telemetry": state,
            })
            return {"success": True, "status_code": 200, "data": base_data, "error": None}

        # follow_line / explore — must observe the mode change.
        retried = False
        if not (online and observed in expected):
            for _ in range(VERIFY_RETRIES):
                retried = True
                logger.warning(
                    "CuteBot %s acked but mode is '%s' (want %s) — retrying once",
                    endpoint_id, observed, expected,
                )
                try:
                    retry_status, _retry_data, retry_error = await self._dispatch(
                        registry, endpoint_id, capability_id, "execute", args
                    )
                except Exception as exc:
                    retry_status, retry_error = "failure", str(exc)
                if retry_status != "success":
                    error = (
                        f"Command acked but robot did not enter "
                        f"{endpoint_id} mode (telemetry mode='{observed or 'unknown'}'), "
                        f"and the retry failed: {retry_error or 'unknown error'}."
                    )
                    base_data.update({
                        "verified": False,
                        "expected_mode": expected[0],
                        "observed_mode": observed,
                        "telemetry": state,
                        "error": error,
                    })
                    return {"success": False, "status_code": 500, "data": base_data, "error": error}
                await asyncio.sleep(self.verify_delay_s)
                state = await self._read_robot_state(registry)
                observed = (state or {}).get("mode", "")
                online = bool((state or {}).get("online", False))

        if online and observed in expected:
            base_data.update({
                "verified": True,
                "mode": observed,
                "telemetry": state,
                "retried": retried,
            })
            return {"success": True, "status_code": 200, "data": base_data, "error": None}

        error = (
            f"Command acked but robot did not enter {endpoint_id} mode "
            f"(telemetry mode='{observed or 'unknown'}', "
            f"online={online}) even after a retry. Possible causes: battery "
            f"switch off, robot lifted off the surface, USB link dropped, or "
            f"a firmware fault. Do NOT tell the user the robot is moving — "
            f"report this failure and suggest checking the robot."
        )
        base_data.update({
            "verified": False,
            "expected_mode": expected[0] if expected else "",
            "observed_mode": observed,
            "telemetry": state,
            "retried": retried,
            "error": error,
        })
        return {"success": False, "status_code": 500, "data": base_data, "error": error}

    # ── Main entry ───────────────────────────────────────────────────

    async def _run_capability(
        self, endpoint_id: str, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        mapping = _ENDPOINT_MAP.get(endpoint_id)
        if mapping is None:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": f"Unknown CuteBot endpoint: {endpoint_id}",
            }

        capability_id, action_type_name = mapping
        registry = self._get_device_registry()
        if registry is None:
            return {
                "success": False,
                "status_code": 503,
                "data": None,
                "error": "FERAL hardware registry not ready.",
            }

        # Halt is the emergency path — attempt even when the device looks offline.
        if endpoint_id != "halt" and not self._is_device_registered(registry):
            return {
                "success": False,
                "status_code": 503,
                "data": None,
                "error": NOT_CONNECTED_ERROR,
            }

        try:
            status, data, error = await self._dispatch(
                registry, endpoint_id, capability_id, action_type_name, args
            )

            success = status == "success"
            if status == "pending_confirmation" and not error:
                # Make the blockage explicit so the LLM reports reality
                # instead of claiming the robot is moving.
                error = (
                    "Command was NOT executed — it is stuck awaiting a "
                    "hardware confirmation. Tell the user the robot did not "
                    "receive the command."
                )

            # Closed-loop verification: never trust a bare ack for motion.
            if success and endpoint_id in ("follow_line", "explore", "drive", "halt"):
                return await self._verify_motion(
                    registry, endpoint_id, capability_id, args, data
                )

            return {
                "success": success,
                "status_code": 200 if success else 500,
                "data": data,
                "error": error,
            }
        except Exception as exc:
            logger.error("CuteBot skill error on %s: %s", endpoint_id, exc, exc_info=True)
            return {
                "success": False,
                "status_code": 500,
                "data": None,
                "error": str(exc),
            }
