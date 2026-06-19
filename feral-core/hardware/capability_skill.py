"""Generic device-manifest → LLM-skill bridge — the HUP self-describing path.

`DeviceManifest` already states the goal in its own docstring: *"Devices
self-describe — the agent reads this manifest to understand what the device
can do without any device-specific code."* Today that promise is only half
kept: the manifest reaches the safety layer, but the LLM's *tools* come from
hand-written skill manifests (e.g. ``skills/manifests/cutebot.json``) that
re-type the same capability list by hand.

This module closes the gap. It turns ANY registered ``DeviceManifest`` into:

1. A ``SkillManifest`` — so each capability becomes an LLM tool named
   ``<skill_id>__<capability_id>``, generated at runtime, never hand-written.
   Safety tiers are derived *generically* from the capability's own
   ``category`` / ``permission_tier`` / ``requires_confirmation`` fields, so
   the brain's existing safety resolver keeps being the single source of
   truth.

2. A single :class:`GenericHardwareSkill` that dispatches every tool call to
   ``DeviceRegistry.execute_action`` and — for actuator capabilities — reads
   the device's own state back afterwards so the agent reports what
   *actually* happened instead of trusting a bare firmware ack (the honesty
   loop, generalized to any device).

The same code path works for the CuteBot, a phone, a drone — anything that
self-describes over HUP. No per-device skill file required.

A capability may optionally declare a verification contract so the honesty
loop can confirm the *intended effect*, not just liveness::

    DeviceCapability(..., verify={"via": "read_telemetry",
                                  "field": "mode",
                                  "expect": ["line_follow", "T"]})

When no contract is declared, the dispatcher still reads device state back
and attaches it as ``telemetry`` with ``verified=None`` (honest "unknown")
rather than claiming success it cannot prove.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from hardware.protocol import (
    DeviceCapability,
    DeviceManifest,
    HUPAction,
    HUPActionType,
    HUPResult,
)
from models.skill_manifest import (
    BrandProfile,
    EndpointParam,
    SkillEndpoint,
    SkillManifest,
)
from skills.base import BaseSkill

logger = logging.getLogger("feral.hardware.capability_skill")

_SKILL_ID_PREFIX = "hwdev_"
_VALID_PARAM_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def skill_id_for_device(device_id: str) -> str:
    """Stable, tool-name-safe skill id for a device.

    LLM tool names are ``<skill_id>__<endpoint_id>`` and must be
    ``[A-Za-z0-9_]`` — sanitize the device id (which often contains
    hyphens, e.g. ``cutebot-usb-0``) into that alphabet.
    """
    safe = re.sub(r"[^0-9a-zA-Z]+", "_", (device_id or "device").strip().lower())
    safe = safe.strip("_") or "device"
    return f"{_SKILL_ID_PREFIX}{safe}"


def _safety_for(cap: DeviceCapability) -> tuple[str, bool, bool]:
    """Map a capability to ``(safety_tier, read_only_hint, requires_approval)``.

    Generic policy — no per-device knowledge:
      * sensor / read-only category      → safe, read-only
      * permission_tier passive          → safe (unless it asks to confirm)
      * permission_tier active           → confirm
      * permission_tier privileged       → confirm + approval
      * permission_tier dangerous        → confirm + approval
      * requires_confirmation=True       → at least confirm
    """
    category = (cap.category or "").lower()
    tier = (cap.permission_tier or "passive").lower()

    if category == "sensor":
        # A sensor read is side-effect-free regardless of tier.
        return "safe", True, False

    if tier in ("dangerous", "privileged"):
        return "confirm", False, True
    if tier == "active":
        return "confirm", False, False

    # passive (or unknown) — default safe, but honor an explicit confirm ask.
    if cap.requires_confirmation:
        return "confirm", False, False
    return "safe", False, False


def _param_to_endpoint_param(raw: dict) -> EndpointParam:
    ptype = str(raw.get("type", "string")).lower()
    if ptype not in _VALID_PARAM_TYPES:
        ptype = "string"
    default = raw.get("default")
    return EndpointParam(
        name=str(raw.get("name", "")),
        type=ptype,  # type: ignore[arg-type]
        required=bool(raw.get("required", True)),
        description=str(raw.get("description", "")),
        default=None if default is None else str(default),
        enum=[str(v) for v in (raw.get("enum") or [])],
    )


def _capability_to_endpoint(skill_id: str, cap: DeviceCapability) -> SkillEndpoint:
    tier, read_only, requires_approval = _safety_for(cap)
    desc = cap.description or cap.name or cap.id
    if cap.safety_notes:
        desc = f"{desc} Safety: {cap.safety_notes}"
    return SkillEndpoint(
        id=cap.id,
        method="PYTHON",
        url=f"python://{skill_id}/{cap.id}",
        description=desc,
        params=[_param_to_endpoint_param(p) for p in (cap.parameters or [])],
        returns_description="{success, verified, telemetry, data}",
        ui_hint="card",
        safety_tier=tier,  # type: ignore[arg-type]
        read_only_hint=read_only,
        requires_user_approval=requires_approval,
    )


def device_manifest_to_skill_manifest(manifest: DeviceManifest) -> SkillManifest:
    """Generate an LLM-facing SkillManifest from a self-describing device.

    Every capability becomes one endpoint; no capability list is typed by
    hand. This is the generic HUP path: hand the brain a device, it learns
    what the device can do from the device itself.
    """
    skill_id = skill_id_for_device(manifest.device_id)
    label = manifest.name or manifest.device_id
    description = (
        f"Control the {label}"
        + (f" ({manifest.manufacturer} {manifest.model})".rstrip() if manifest.manufacturer else "")
        + f", a {manifest.device_type} discovered over HUP. These tools were "
        "generated from the device's own self-description. Actuator commands "
        "are CLOSED-LOOP where the device exposes telemetry: the result "
        "includes 'verified' and a 'telemetry' read-back. Never tell the user "
        "an action worked unless the telemetry confirms it; if verified is "
        "false or unknown, report what the telemetry actually shows."
    )
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="feral-core",
        brand=BrandProfile(
            name=label,
            primary_color="#22C55E",
            secondary_color="#15803D",
            icon_set="sf_symbols",
        ),
        description=description,
        trigger_phrases=[label.lower(), manifest.device_type.lower()],
        categories=["hardware", manifest.device_type],
        endpoints=[_capability_to_endpoint(skill_id, c) for c in manifest.capabilities],
        permissions=["hardware"],
        requires_daemon=False,
        daemon_node_type=manifest.device_type,
    )


class GenericHardwareSkill(BaseSkill):
    """Dispatches any discovered device capability to the DeviceRegistry.

    One instance backs one device's auto-generated skill. It reuses the
    brain's existing skill/executor/safety machinery — the only device-
    specific knowledge it holds is the manifest it was built from, which
    the device itself provided.
    """

    def __init__(
        self,
        *,
        device_id: str,
        device_registry: Any,
        manifest: DeviceManifest,
        skill_id: Optional[str] = None,
    ):
        super().__init__(skill_id=skill_id or skill_id_for_device(device_id))
        self.device_id = device_id
        self._registry = device_registry
        self._caps: dict[str, DeviceCapability] = {c.id: c for c in manifest.capabilities}
        # First sensor capability is the default telemetry read-back source.
        self._read_cap_id: Optional[str] = next(
            (c.id for c in manifest.capabilities if (c.category or "").lower() == "sensor"),
            None,
        )

    async def execute(
        self, endpoint_id: str, args: dict[str, Any], vault: dict[str, str]
    ) -> dict[str, Any]:
        cap = self._caps.get(endpoint_id)
        if cap is None:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": f"Unknown capability '{endpoint_id}' for device {self.device_id}",
            }
        if self._registry is None:
            return {
                "success": False,
                "status_code": 503,
                "data": None,
                "error": "Hardware registry not ready.",
            }

        is_sensor = (cap.category or "").lower() == "sensor"
        action_type = HUPActionType.READ if is_sensor else HUPActionType.EXECUTE
        status, data, error = await self._dispatch(endpoint_id, action_type, args)

        if status == "pending_confirmation" and not error:
            error = (
                "Command was NOT executed — it is awaiting a hardware "
                "confirmation. Tell the user the device did not receive it."
            )
        success = status == "success"

        # Sensor reads and failures return as-is. Actuator successes get a
        # telemetry read-back so the agent reports reality, not a bare ack.
        if not success or is_sensor:
            return {
                "success": success,
                "status_code": 200 if success else 500,
                "data": data,
                "error": error,
            }

        return await self._verify_actuator(cap, data)

    async def _verify_actuator(
        self, cap: DeviceCapability, ack_data: Any
    ) -> dict[str, Any]:
        base: dict[str, Any] = dict(ack_data) if isinstance(ack_data, dict) else {"ack": ack_data}
        telemetry = await self._read_state()
        base["telemetry"] = telemetry

        verify = getattr(cap, "verify", None)
        if isinstance(verify, dict) and telemetry is not None:
            field = verify.get("field")
            expect = verify.get("expect")
            expect_set = set(expect) if isinstance(expect, (list, tuple, set)) else {expect}
            observed = telemetry.get(field) if field else None
            verified = observed in expect_set
            base.update({
                "verified": verified,
                "verify_field": field,
                "observed": observed,
                "expected": list(expect_set),
            })
            if not verified:
                msg = (
                    f"{cap.id} was acked but telemetry shows {field}={observed!r} "
                    f"(expected one of {sorted(map(str, expect_set))}). Do NOT claim "
                    f"it worked — report the device's actual state."
                )
                return {"success": False, "status_code": 500, "data": base, "error": msg}
            return {"success": True, "status_code": 200, "data": base, "error": None}

        # No verification contract declared: honest "unknown" with the
        # read-back attached so the LLM can describe what the device shows.
        base["verified"] = None
        base["verify_note"] = (
            "This capability declares no verification contract; reporting a "
            "device state read-back instead of asserting success."
            if telemetry is not None
            else "Could not read device telemetry back after the command."
        )
        return {"success": True, "status_code": 200, "data": base, "error": None}

    async def _read_state(self) -> Optional[dict[str, Any]]:
        if not self._read_cap_id:
            return None
        try:
            status, data, _err = await self._dispatch(
                self._read_cap_id, HUPActionType.READ, {}
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("%s telemetry read-back failed: %s", self.device_id, exc)
            return None
        if status == "success" and isinstance(data, dict):
            return data
        return None

    async def _dispatch(
        self, capability_id: str, action_type: HUPActionType, args: dict[str, Any]
    ) -> tuple[str, Any, Optional[str]]:
        action = HUPAction(
            device_id=self.device_id,
            capability_id=capability_id,
            action_type=action_type,
            parameters=dict(args or {}),
            # The brain's ToolRunner already enforced the approval tier before
            # this skill ran, so pre-confirm to avoid the registry's own gate
            # dead-ending with no resume path (same contract as cutebot_skill).
            confirmed=True,
        )
        result = await self._registry.execute_action(action)
        if isinstance(result, HUPResult):
            return result.status, result.data, result.error or None
        if isinstance(result, dict):
            return result.get("status", "failure"), result.get("data"), result.get("error") or None
        return "success", result, None


__all__ = [
    "skill_id_for_device",
    "device_manifest_to_skill_manifest",
    "GenericHardwareSkill",
]
