"""Per-device capability grants — the operator's answer to "may THIS device?"

HUP_SPEC.md section 6 has said since v1.0 that per-device capability
gating happens at **Settings -> Devices -> <device> -> Capabilities**, and
that the brain:

* MUST NOT issue ``hup_action_request`` for a capability that is not in
  ``granted_capabilities`` from the ``node_ack``;
* MUST drop ``camera_frame`` / ``microphone_chunk`` from nodes whose
  ``camera`` / ``audio`` tier is disabled.

Neither was implemented. ``api/server.py`` answered every ``node_register``
with ``granted_capabilities = list(payload.capabilities)`` and
``denied_capabilities = []`` -- the node's own self-declaration echoed
back, with no store behind it and no operator input into it. The client
drew the capability list as read-only chips. Both node SDKs then read that
ack as ``granted or self.capabilities``, so a brain that ever sent an empty
grant list as a full deny would have been read as "everything I declared".
Two published MUSTs, no code, and a fail-open on the far side.

This module is the store the spec always described.

**Why this is not a duplicate of ``security/hardware_policy``.** That
module answers "may this capability run *at all*, and unattended?" from
``~/.feral/policies/default.yaml`` -- ``hardware.sensors.allowed``,
``hardware.actuators.allowed``, ``hardware.cameras.allowed``. Those keys
are global: they are lists of capability ids with no device in them. An
operator with a work phone and a personal phone paired to the same brain
has no way to say "no camera on the work one". That question is
per-device, it has no answer anywhere else in the system, and it is the
one section 6 is about. The two layers compose: ``hardware_policy`` is the
global floor, this is the per-device gate on top, and a capability has to
clear both.

**Default is granted, not denied.** HUP_SPEC.md section 5.1's tier table
used to claim ``camera`` and ``audio`` were "requires user opt-in"; the
shipped policy has ``hardware.cameras.allowed: true`` and nothing gating
the microphone, so the claim was false when it was written. Defaulting
those tiers to denied here would have made it true by breaking every
already-paired phone on upgrade: vision-context-attach and ambient
transcription both live on frames this gate can drop, and they would have
gone dark with no operator action and no error. So a capability a node
declares is granted until the operator denies it, the spec now says that
in those words, and the store holds explicit rows for BOTH answers so
"the operator allowed this" stays distinguishable from "nobody has been
asked".

**Grants are keyed by ``node_id``**, the identity in ``node_register``
that every subsequent frame carries and that every enforcement site
already has in hand. ``paired_devices.device_id`` is a different
identifier that only the pairing flow sees.

Nothing here imports from the rest of FERAL, for the reason
``security/hardware_policy`` gives for the same trick: the enforcement
sites sit under ``api``, ``agents`` and ``hardware``, and a repo-level
import would close a cycle. :func:`live_grants` reaches the running
brain's store through ``sys.modules``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("feral.security.capability_grants")


# ─────────────────────────────────────────────
# Tiers (HUP_SPEC.md section 5.1)
# ─────────────────────────────────────────────
# The spec groups capability strings into five tiers and hangs the
# per-device toggles off the tier, not off the individual capability. The
# store is keyed by capability so a vendor string outside the enum still
# gets a row; the tier is what the UI groups by and what the
# frame-ingress rules ("drop camera_frame from nodes whose camera tier is
# disabled") are written against.

TIER_PASSIVE_SENSOR = "passive_sensor"
TIER_CAMERA = "camera"
TIER_AUDIO = "audio"
TIER_ACTIVE_ACTUATOR = "active_actuator"
TIER_MOTOR = "motor"

#: Capability string -> tier, verbatim from HUP_SPEC.md section 5.1.
CAPABILITY_TIERS: dict = {
    # passive_sensor
    "heart_rate": TIER_PASSIVE_SENSOR,
    "spo2": TIER_PASSIVE_SENSOR,
    "temperature": TIER_PASSIVE_SENSOR,
    "uv": TIER_PASSIVE_SENSOR,
    "accelerometer": TIER_PASSIVE_SENSOR,
    "gyroscope": TIER_PASSIVE_SENSOR,
    "ambient_light": TIER_PASSIVE_SENSOR,
    "steps": TIER_PASSIVE_SENSOR,
    "battery": TIER_PASSIVE_SENSOR,
    "gps": TIER_PASSIVE_SENSOR,
    "telemetry": TIER_PASSIVE_SENSOR,
    "passive_sensor": TIER_PASSIVE_SENSOR,
    # camera
    "camera": TIER_CAMERA,
    # audio
    "microphone": TIER_AUDIO,
    "speaker": TIER_AUDIO,
    # active_actuator
    "display": TIER_ACTIVE_ACTUATOR,
    "haptic": TIER_ACTIVE_ACTUATOR,
    "buzzer": TIER_ACTIVE_ACTUATOR,
    "led": TIER_ACTIVE_ACTUATOR,
    "keyboard": TIER_ACTIVE_ACTUATOR,
    "applescript": TIER_ACTIVE_ACTUATOR,
    "filesystem": TIER_ACTIVE_ACTUATOR,
    "gpio": TIER_ACTIVE_ACTUATOR,
    "shell": TIER_ACTIVE_ACTUATOR,
    "active_actuator": TIER_ACTIVE_ACTUATOR,
    # motor
    "motor": TIER_MOTOR,
    "relay": TIER_MOTOR,
    "valve": TIER_MOTOR,
    "vehicle": TIER_MOTOR,
}

#: Substring hints for vendor capability strings outside the section 5.1
#: enum. The spec says brains MAY ignore unknown capabilities for gating;
#: a self-describing name is better classified than dropped into the
#: default tier, and a wrong guess here only ever changes which toggle
#: the operator finds the capability under, never whether it is allowed.
_TIER_HINTS: tuple = (
    (TIER_CAMERA, ("camera", "photo", "video", "vision", "snapshot")),
    (TIER_AUDIO, ("microphone", "mic_", "audio", "speaker", "tts", "voice")),
    (TIER_MOTOR, ("motor", "servo", "wheel", "drive", "steer", "valve", "relay")),
)

DEFAULT_TIER = TIER_ACTIVE_ACTUATOR


def tier_for(capability: str) -> str:
    """The HUP_SPEC section 5.1 tier a capability string belongs to."""
    cap = (capability or "").strip().lower()
    if not cap:
        return DEFAULT_TIER
    known = CAPABILITY_TIERS.get(cap)
    if known:
        return known
    # ``read_`` is the prefix ``hardware/protocol.py`` puts on sensor
    # capability ids; ``security/hardware_policy._sensor_allowed``
    # tolerates it for the same reason.
    if cap.startswith("read_"):
        stripped = CAPABILITY_TIERS.get(cap[len("read_"):])
        if stripped:
            return stripped
    for tier, tokens in _TIER_HINTS:
        if any(token in cap for token in tokens):
            return tier
    if cap.startswith("read_") or cap.endswith("_sensor"):
        return TIER_PASSIVE_SENSOR
    return DEFAULT_TIER


class CapabilityGrantStore:
    """SQLite-backed per-device capability grants.

    Default path: ``~/.feral/capability_grants.db`` (overridable for
    tests). A row exists only once the operator has answered; absence
    means "granted, nobody was asked", which :meth:`is_granted` reports
    as granted and the API reports with ``explicit=False`` so the UI can
    tell the two apart.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            home = os.environ.get("FERAL_HOME", str(Path.home() / ".feral"))
            Path(home).mkdir(parents=True, exist_ok=True)
            db_path = str(Path(home) / "capability_grants.db")
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS device_capability_grants (
                        node_id    TEXT NOT NULL,
                        capability TEXT NOT NULL,
                        granted    INTEGER NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (node_id, capability)
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    # ── reads ──────────────────────────────────────────────────────

    def is_granted(self, node_id: str, capability: str) -> bool:
        """Whether ``capability`` may be used on ``node_id``.

        No row means granted. See the module docstring for why the
        default is not deny.
        """
        node_id = (node_id or "").strip()
        capability = (capability or "").strip()
        if not node_id or not capability:
            # A send with no node or no capability name is not something
            # this gate can answer; the caller's own validation owns it.
            return True
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT granted FROM device_capability_grants "
                    "WHERE node_id = ? AND capability = ?",
                    (node_id, capability),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return True
        return bool(row["granted"])

    def denied_for(self, node_id: str) -> set:
        """Every capability the operator has explicitly denied on a node."""
        node_id = (node_id or "").strip()
        if not node_id:
            return set()
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT capability FROM device_capability_grants "
                    "WHERE node_id = ? AND granted = 0",
                    (node_id,),
                ).fetchall()
            finally:
                conn.close()
        return {str(r["capability"]) for r in rows}

    def partition(
        self, node_id: str, declared: Iterable[str],
    ) -> tuple[list, list]:
        """Split a node's declared capabilities into (granted, denied).

        This is what ``node_ack`` puts on the wire. Order follows the
        node's own declaration so a daemon can diff the two lists.
        """
        denied_set = self.denied_for(node_id)
        granted: list = []
        denied: list = []
        for cap in declared or []:
            cap = str(cap)
            (denied if cap in denied_set else granted).append(cap)
        return granted, denied

    def grants_for(self, node_id: str, declared: Iterable[str]) -> list:
        """UI-facing rows: capability, tier, granted, explicit."""
        node_id = (node_id or "").strip()
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT capability, granted FROM device_capability_grants "
                    "WHERE node_id = ?",
                    (node_id,),
                ).fetchall()
            finally:
                conn.close()
        explicit = {str(r["capability"]): bool(r["granted"]) for r in rows}
        out: list = []
        seen: set = set()
        for cap in list(declared or []) + sorted(explicit):
            cap = str(cap)
            if cap in seen:
                continue
            seen.add(cap)
            out.append({
                "capability": cap,
                "tier": tier_for(cap),
                "granted": explicit.get(cap, True),
                "explicit": cap in explicit,
            })
        return out

    def tier_enabled(self, node_id: str, tier: str) -> bool:
        """Whether ANY capability in ``tier`` is still granted on a node.

        The frame-ingress rule in HUP_SPEC section 6 is written per tier
        ("drop camera_frame from nodes whose camera tier is disabled"),
        not per capability, so a node that declared both ``camera`` and a
        vendor ``camera_ir`` keeps streaming while one of them is
        allowed. A tier with no explicit deny is enabled.
        """
        denied = self.denied_for(node_id)
        if not denied:
            return True
        tier_denied = {cap for cap in denied if tier_for(cap) == tier}
        if not tier_denied:
            return True
        granted_in_tier = {
            cap for cap in self._explicit_grants(node_id)
            if tier_for(cap) == tier
        }
        return bool(granted_in_tier)

    def _explicit_grants(self, node_id: str) -> set:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT capability FROM device_capability_grants "
                    "WHERE node_id = ? AND granted = 1",
                    ((node_id or "").strip(),),
                ).fetchall()
            finally:
                conn.close()
        return {str(r["capability"]) for r in rows}

    # ── writes ─────────────────────────────────────────────────────

    def set_grant(self, node_id: str, capability: str, granted: bool) -> None:
        """Record the operator's answer for one capability on one node."""
        node_id = (node_id or "").strip()
        capability = (capability or "").strip()
        if not node_id or not capability:
            raise ValueError("node_id and capability are both required")
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO device_capability_grants "
                    "(node_id, capability, granted, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(node_id, capability) DO UPDATE SET "
                    "granted = excluded.granted, updated_at = excluded.updated_at",
                    (node_id, capability, 1 if granted else 0, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(
            "capability grant: node=%s capability=%s -> %s",
            node_id, capability, "granted" if granted else "DENIED",
        )

    def set_tier(self, node_id: str, tier: str, granted: bool,
                 declared: Iterable[str]) -> list:
        """Set every declared capability in a tier at once.

        This is the toggle HUP_SPEC section 6 describes ("each capability
        tier has a per-device toggle"). Returns the capabilities changed.
        """
        touched = [
            str(cap) for cap in (declared or []) if tier_for(str(cap)) == tier
        ]
        for cap in touched:
            self.set_grant(node_id, cap, granted)
        return touched

    def clear_node(self, node_id: str) -> int:
        """Forget every answer for a node. Used when it is unpaired."""
        node_id = (node_id or "").strip()
        if not node_id:
            return 0
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "DELETE FROM device_capability_grants WHERE node_id = ?",
                    (node_id,),
                )
                conn.commit()
                return int(cur.rowcount or 0)
            finally:
                conn.close()


# ─────────────────────────────────────────────
# Process-wide store
# ─────────────────────────────────────────────
# Same shape as ``security/device_pairing``'s singleton, for the same
# reason: the store is a file in ``FERAL_HOME`` and two instances opening
# it is a lock contention nobody needs. ``BrainState`` holds this object,
# and ``tests/conftest.py`` resets it between tests so a denial written by
# one test cannot deny a capability in the next.

_SHARED_STORE: Optional[CapabilityGrantStore] = None
_SHARED_LOCK = threading.Lock()


def get_store(db_path: Optional[str] = None) -> CapabilityGrantStore:
    """The process-wide grant store, built on first use."""
    global _SHARED_STORE
    with _SHARED_LOCK:
        if _SHARED_STORE is None:
            _SHARED_STORE = CapabilityGrantStore(db_path=db_path)
        return _SHARED_STORE


def reset_store() -> None:
    """Drop the singleton so the next :func:`get_store` rebuilds it."""
    global _SHARED_STORE
    with _SHARED_LOCK:
        _SHARED_STORE = None


# ─────────────────────────────────────────────
# Live lookup + the two enforcement predicates
# ─────────────────────────────────────────────

def live_grants() -> CapabilityGrantStore:
    """The store every enforcement site reads. Never ``None``.

    Resolution order is the running brain's store, then the process-wide
    one. In a booted brain they are the same object --
    ``BrainState.__init__`` assigns ``get_store()`` -- so the branch only
    matters when ``api.state.state`` is not a real ``BrainState``: during
    boot before ``__init__`` has run, in an embedded import of
    ``api.server``, and under the test doubles this suite is full of.

    The type check is the point, not a formality. A security answer has to
    come from a real store. ``api/server.py``'s node_ack unpacks
    ``partition()``'s two lists, and a stand-in that answers with
    something else either raises inside an unrelated code path or, worse,
    answers "denied nothing" convincingly. That is trap 8 in CLAUDE.md one
    attribute deeper: the existing guard stops
    ``getattr(state, name, default)`` from taking a MagicMock, and nothing
    stopped ``state.store.method()`` from returning one.

    ``sys.modules`` rather than an import, for the reason
    ``security/hardware_policy.live_policy`` gives: ``api.state`` imports
    the orchestrator, which imports the modules that call this.
    """
    state_module = sys.modules.get("api.state")
    candidate: Any = None
    if state_module is not None:
        try:
            candidate = getattr(
                getattr(state_module, "state", None), "capability_grants", None,
            )
        except Exception as exc:                        # state mid-swap
            logger.debug("live capability-grant lookup failed: %s", exc)
    if isinstance(candidate, CapabilityGrantStore):
        return candidate
    return get_store()


def action_denied(node_id: str, capability: str, store: Any = None) -> Optional[str]:
    """``None`` when the brain may send this action to this node.

    Otherwise an operator-facing reason. This is the HUP_SPEC section 6
    "MUST NOT issue ``hup_action_request`` for a capability that is not in
    ``granted_capabilities``" rule, and every builder of that frame calls
    it through ``hardware/action_frames.build_action_request``.

    Fails OPEN on an unreadable store, unlike
    ``security/hardware_policy.permits_unattended``. The asymmetry is
    deliberate: that function decides whether a human sees an approval
    card, so "nobody has been asked yet" must not read as consent. This
    one decides whether a capability the operator has never denied gets
    sent at all, and a store that will not answer is not evidence anyone
    denied anything. Failing closed on a bad disk would take every
    actuator on the box offline, silently, which is the larger harm.
    """
    if store is None:
        store = live_grants()
    try:
        if store.is_granted(node_id, capability):
            return None
    except Exception as exc:                            # store unusable
        logger.warning(
            "capability grant lookup failed for %s/%s: %s",
            node_id, capability, exc,
        )
        return None
    return (
        f"'{capability}' is denied for device '{node_id}'. Re-enable it at "
        "Settings > Devices > this device > Capabilities."
    )


def frame_tier_enabled(node_id: str, tier: str, store: Any = None) -> bool:
    """Whether inbound frames of ``tier`` may be ingested from a node.

    The HUP_SPEC section 6 "MUST drop ``camera_frame`` and
    ``microphone_chunk`` from nodes whose ``camera`` / ``audio`` tier is
    disabled, even if the daemon sends them" rule. Same fail-open
    reasoning as :func:`action_denied`.
    """
    if store is None:
        store = live_grants()
    try:
        return bool(store.tier_enabled(node_id, tier))
    except Exception as exc:
        logger.warning(
            "capability tier lookup failed for %s/%s: %s", node_id, tier, exc,
        )
        return True


__all__ = [
    "CAPABILITY_TIERS",
    "CapabilityGrantStore",
    "DEFAULT_TIER",
    "TIER_ACTIVE_ACTUATOR",
    "TIER_AUDIO",
    "TIER_CAMERA",
    "TIER_MOTOR",
    "TIER_PASSIVE_SENSOR",
    "action_denied",
    "get_store",
    "reset_store",
    "frame_tier_enabled",
    "live_grants",
    "tier_for",
]
