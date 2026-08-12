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

import asyncio
import json
import logging
import re
import time
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


def _returns_description(cap: DeviceCapability) -> str:
    """Build the LLM return-shape hint, using the device's own ``returns``.

    The dispatcher always wraps results as ``{success, verified, telemetry,
    data}``; when the device declared a ``returns`` shape we surface those keys
    too so the LLM knows what to read out of ``data``/``telemetry``.
    """
    base = "{success, verified, telemetry, data}"
    if isinstance(cap.returns, dict) and cap.returns:
        fields = ", ".join(str(k) for k in cap.returns.keys())
        return f"{base}; device fields: {fields}"
    return base


def _capability_to_endpoint(skill_id: str, cap: DeviceCapability) -> SkillEndpoint:
    tier, read_only, requires_approval = _safety_for(cap)
    desc = cap.description or cap.name or cap.id
    if not cap.reversible:
        desc = f"{desc} (IRREVERSIBLE — cannot be undone.)"
    if cap.rate_limit_per_minute:
        desc = f"{desc} Rate limit: {cap.rate_limit_per_minute}/min."
    if cap.safety_notes:
        desc = f"{desc} Safety: {cap.safety_notes}"
    return SkillEndpoint(
        id=cap.id,
        method="PYTHON",
        url=f"python://{skill_id}/{cap.id}",
        description=desc,
        params=[_param_to_endpoint_param(p) for p in (cap.parameters or [])],
        returns_description=_returns_description(cap),
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
        memory: Any = None,
        knowledge_graph: Any = None,
        session_id: Optional[str] = None,
    ):
        super().__init__(skill_id=skill_id or skill_id_for_device(device_id))
        self.device_id = device_id
        self._registry = device_registry
        self._manifest = manifest
        self._caps: dict[str, DeviceCapability] = {c.id: c for c in manifest.capabilities}
        # First sensor capability is the default telemetry read-back source
        # (used when a verify contract doesn't name an explicit `via` sensor).
        self._read_cap_id: Optional[str] = next(
            (c.id for c in manifest.capabilities if (c.category or "").lower() == "sensor"),
            None,
        )
        self._memory = memory
        self._kg = knowledge_graph
        # Episodes need a session to hang off; recall is by semantic
        # similarity (cross-session), so a stable per-device anchor is fine.
        self._session_id = session_id or f"hwdev-{device_id}"
        # Generic per-capability rate limiting: cap_id -> recent call epochs.
        self._rate_hits: dict[str, list[float]] = {}
        # Action + verify history for the fleet/honesty UI (bounded).
        self.verify_history: list[dict[str, Any]] = []

    def _resolve_action_type(
        self, cap: DeviceCapability, is_sensor: bool
    ) -> HUPActionType:
        explicit = getattr(cap, "action_type", None)
        if explicit:
            try:
                return HUPActionType(str(explicit).lower())
            except ValueError:
                logger.debug("Unknown action_type %r on %s; inferring", explicit, cap.id)
        return HUPActionType.READ if is_sensor else HUPActionType.EXECUTE

    def _check_rate_limit(self, cap: DeviceCapability) -> Optional[dict[str, Any]]:
        limit = cap.rate_limit_per_minute
        if not limit or limit <= 0:
            return None
        now = time.monotonic()
        window = [t for t in self._rate_hits.get(cap.id, []) if now - t < 60.0]
        if len(window) >= limit:
            self._rate_hits[cap.id] = window
            retry_in = max(0.0, 60.0 - (now - window[0]))
            return {
                "success": False,
                "status_code": 429,
                "data": {"rate_limit_per_minute": limit, "retry_after_s": round(retry_in, 1)},
                "error": (
                    f"{cap.id} is rate-limited to {limit}/min; this call was "
                    f"NOT sent. Wait ~{retry_in:.0f}s before retrying."
                ),
            }
        window.append(now)
        self._rate_hits[cap.id] = window
        return None

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

        rate_denied = self._check_rate_limit(cap)
        if rate_denied is not None:
            return rate_denied

        is_sensor = (cap.category or "").lower() == "sensor"
        action_type = self._resolve_action_type(cap, is_sensor)
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
            out = {
                "success": success,
                "status_code": 200 if success else 500,
                "data": data,
                "error": error,
            }
        else:
            out = await self._verify_actuator(cap, args, data)

        await self._record_episode(cap, args, out, is_sensor=is_sensor)
        self._record_verify_history(cap, args, out)
        return out

    async def _verify_actuator(
        self, cap: DeviceCapability, args: dict[str, Any], ack_data: Any
    ) -> dict[str, Any]:
        """Honor the device's full verify contract after an actuator ack.

        Contract fields (all optional): ``via`` (which sensor capability to
        read back — not just "first sensor"), ``field``/``expect`` (what the
        telemetry must show), ``delay_ms`` (settle time before reading),
        ``retries`` (re-issue + re-read on mismatch), and ``transient``
        (the effect self-reverts, e.g. a drive pulse — report post-state
        without asserting a held mode).
        """
        base: dict[str, Any] = dict(ack_data) if isinstance(ack_data, dict) else {"ack": ack_data}
        verify = getattr(cap, "verify", None)
        verify = verify if isinstance(verify, dict) else None

        via = (verify or {}).get("via")
        delay_ms = int((verify or {}).get("delay_ms", 0) or 0)
        retries = int((verify or {}).get("retries", 0) or 0)
        transient = bool((verify or {}).get("transient", False))

        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        telemetry = await self._read_state(via)
        base["telemetry"] = telemetry

        # Transient effect (e.g. drive auto-revert): the post-state is ground
        # truth; success means we could read the device back, not that a mode
        # is held.
        if transient:
            base.update({
                "verified": telemetry is not None,
                "transient": True,
                "verify_note": (
                    "Transient effect — the device self-reverts, so the "
                    "post-action state is the ground truth."
                    if telemetry is not None
                    else "Acked but post-action telemetry could not be read; "
                         "device state unknown."
                ),
            })
            return {"success": True, "status_code": 200, "data": base, "error": None}

        if verify and ("field" in verify) and telemetry is not None:
            field = verify.get("field")
            expect = verify.get("expect")
            expect_set = set(expect) if isinstance(expect, (list, tuple, set)) else {expect}
            observed = telemetry.get(field) if field else None
            verified = observed in expect_set

            # Retry: re-issue the command and re-read, up to `retries` times.
            attempt = 0
            while not verified and attempt < retries:
                attempt += 1
                logger.warning(
                    "%s %s acked but %s=%r (want %s) — retry %d/%d",
                    self.device_id, cap.id, field, observed,
                    sorted(map(str, expect_set)), attempt, retries,
                )
                retry_status, _d, _e = await self._dispatch(
                    cap.id, self._resolve_action_type(cap, False), args
                )
                if retry_status != "success":
                    break
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000.0)
                telemetry = await self._read_state(via)
                base["telemetry"] = telemetry
                observed = telemetry.get(field) if (field and telemetry) else None
                verified = observed in expect_set

            base.update({
                "verified": verified,
                "verify_field": field,
                "observed": observed,
                "expected": list(expect_set),
                "retried": attempt,
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

    async def _read_state(self, via: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Read a sensor capability back. Prefers the verify contract's
        explicit ``via`` sensor; falls back to the device's first sensor."""
        read_cap = via or self._read_cap_id
        if not read_cap or read_cap not in self._caps:
            read_cap = self._read_cap_id
        if not read_cap:
            return None
        try:
            status, data, _err = await self._dispatch(
                read_cap, HUPActionType.READ, {}
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("%s telemetry read-back failed: %s", self.device_id, exc)
            return None
        if status == "success" and isinstance(data, dict):
            return data
        return None

    # ── memory parity + honesty history ──────────────────────────────

    def _record_verify_history(
        self, cap: DeviceCapability, args: dict[str, Any], out: dict[str, Any]
    ) -> None:
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        entry = {
            "ts": time.time(),
            "device_id": self.device_id,
            "capability": cap.id,
            "args": dict(args or {}),
            "success": bool(out.get("success")),
            "verified": data.get("verified"),
            "observed": data.get("observed"),
            "expected": data.get("expected"),
            "error": out.get("error"),
        }
        self.verify_history.append(entry)
        if len(self.verify_history) > 200:
            self.verify_history = self.verify_history[-100:]
        # Surface the latest state to the registry for the unified fleet API.
        recorder = getattr(self._registry, "record_verification", None)
        if callable(recorder):
            try:
                recorder(self.device_id, entry)
            except Exception:  # pragma: no cover - defensive
                pass

    async def _record_episode(
        self,
        cap: DeviceCapability,
        args: dict[str, Any],
        out: dict[str, Any],
        *,
        is_sensor: bool,
    ) -> None:
        """Feed every self-describing device action into episodic memory.

        Mirrors the CuteBot adapter's ``_save_episode`` and mock_roomba's
        ``_record_event`` so the orchestrator's memory-of-actions answers
        "did the brain do X?" for ANY HUP device, not just the wired ones.
        """
        memory = self._memory
        if memory is None:
            return
        episode_save = getattr(memory, "episode_save", None)
        if episode_save is None:
            return
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        verified = data.get("verified")
        success = bool(out.get("success"))
        event_type = "sensor" if is_sensor else "actuator"
        verdict = (
            "verified" if verified is True
            else "UNVERIFIED" if verified is False
            else "ok" if success else "failed"
        )
        label = self._manifest.name or self.device_id
        summary = f"{label}: {cap.id} {verdict}"
        detail = {
            "category": event_type,
            "device_id": self.device_id,
            "device_type": self._manifest.device_type,
            "capability": cap.id,
            "params": dict(args or {}),
            "success": success,
            "verified": verified,
            "observed": data.get("observed"),
            "expected": data.get("expected"),
            "source": "hup_generic",
            "ts": time.time(),
        }
        importance = 0.3 if is_sensor else (0.7 if not success or verified is False else 0.6)
        try:
            await episode_save(
                session_id=self._session_id,
                event_type=event_type,
                summary=summary,
                detail=json.dumps(detail, default=str),
                location=self._manifest.location or self._manifest.device_type or "device",
                importance=importance,
            )
        except Exception as exc:
            # Warning, not debug: see the same handler in
            # hardware/adapters/cutebot.py. A silently dropped device
            # episode is history recall will never show again.
            logger.warning("%s episode_save failed: %s", self.device_id, exc)

    async def announce_device(self) -> None:
        """Upsert a knowledge-graph entity for this device on registration.

        Mirrors ``HardwareMesh._write_kg_entity_for_announce`` so a
        brain-local self-describing device is queryable in chat ("what
        hardware do I have?") exactly like a mesh-announced peripheral.
        """
        kg = self._kg
        if kg is None:
            return
        add_entity = getattr(kg, "add_entity", None)
        if not callable(add_entity):
            return
        m = self._manifest
        metadata = {
            "category": "device",
            "device_id": self.device_id,
            "device_type": m.device_type,
            "manufacturer": m.manufacturer,
            "model": m.model,
            "connection_type": m.connection_type,
            "location": m.location,
            "capabilities": [c.id for c in m.capabilities],
            "tags": [t for t in [m.device_type, "hup", "self_describing"] if t],
        }
        try:
            await add_entity(name=m.name or self.device_id, entity_type="device", metadata=metadata)
        except Exception as exc:
            logger.debug("%s kg.add_entity failed: %s", self.device_id, exc)

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
