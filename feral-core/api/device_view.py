"""One assembler for "what devices does this brain have", read by every surface.

Why this module exists
======================

Before it, four surfaces each answered the question their own way and
none of them could say the word "disconnected":

* ``/api/devices/connected`` listed ``state.daemons`` -- open WebSockets
  only. ``api/server.py:3533`` pops the daemon on disconnect and
  ``hardware_mesh.on_node_disconnected`` unregisters the node, so a
  phone that drops does not become "disconnected", it *ceases to exist*.
  The UI renders absence as an empty mesh, which reads as "you have
  never owned a device" rather than "your device dropped". The last
  thing the owner ever saw was a green dot.
* ``skills/impl/self_introspection.connected_devices`` read
  ``state.device_registry``, which is emptied by the same teardown, so
  the tool the model reads answered "nothing is connected" for a phone
  that had been connected ten seconds earlier.
* ``agents/identity_loader._build_connected_hardware_section`` read the
  sub-device store directly and flat-listed every row. On the owner's
  install that is 7 rows, 6 of which are the same pair of glasses seen
  through 5 different ``feral-iphone-*`` node ids. The prompt told the
  model the user owns six pairs of glasses.
* ``feral-client-v2/src/components/DeviceTopology.jsx`` hardcoded the
  node dot to ``tone="live"``.

The persistent truth about non-live nodes lives in
``memory/node_subdevices.py`` -- it is the only store that survives a
disconnect. This module joins it against the live daemon set and emits
one tree that every surface renders.

Nothing here deletes or rewrites a row. Grouping is presentation only;
``also_known_as`` / ``also_seen_via`` carry every node id that was
collapsed so no history is lost.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Iterable, Optional

# ─────────────────────────────────────────────────────────────
# The heartbeat window. ONE number, derived, not invented.
# ─────────────────────────────────────────────────────────────
#
# HUP_SPEC.md keepalive row: "node_heartbeat every heartbeat_ms
# (default 10000). Brain MAY close with ... code 4004 stale_heartbeat
# if 3x interval elapses with no frame."
#
# `models/protocol.py` NodeAckPayload.heartbeat_ms defaults to 10000
# and `api/server.py` sends that same 10000 in node_ack. Three missed
# heartbeats is therefore the protocol's own definition of a stale
# node, and it is what `memory/node_subdevices.LIVENESS_WINDOWS["ble"]`
# already uses for BLE peripherals. Using the same number for nodes
# means the phone and the glasses behind it derate on the same clock,
# so a surface can never show "phone offline, glasses live".
#
# Sub-device rows keep their provenance-specific window (ble 30 /
# host 60 / cloud 300) because a cloud-synced account genuinely does
# report on a slower cadence than a BLE link. That window travels with
# each row as `liveness_window_s`, so a surface never has to guess.
NODE_HEARTBEAT_WINDOW_S = 30.0


# A node id minted per install looks like `feral-iphone-6053b3cdc4ed`
# or `feral-iphone-2a210fa1`: a stable family prefix plus an install
# nonce. The owner's memory.db holds six such ids for ONE physical
# iPhone because the iOS app derives its node id from a per-install
# value (see the note in `node_family` below).
_INSTALL_NONCE = re.compile(r"^(?:[0-9a-f]{6,}|[0-9]{4,})$", re.IGNORECASE)


def node_family(node_id: str) -> str:
    """Collapse an install-scoped node id to the physical device it names.

    ``feral-iphone-6053b3cdc4ed`` -> ``feral-iphone``
    ``feral-wristband-0001``      -> ``feral-wristband``
    ``somebody-robot-01``         -> ``somebody-robot-01`` (unchanged)

    Only a trailing segment that looks like an install nonce (>=6 hex
    chars, or >=4 digits) is stripped, and never the last remaining
    segment. Anything shorter or more word-like is kept, because a
    two-character suffix is far more likely to be part of the device's
    real name than a generated id.

    This is deliberately conservative: over-collapsing would merge two
    real devices, which is a worse lie than showing one device twice.
    """
    parts = [p for p in (node_id or "").split("-") if p]
    while len(parts) > 1 and _INSTALL_NONCE.match(parts[-1]):
        parts.pop()
    return "-".join(parts)


def node_type_from_id(node_id: str) -> str:
    """Best-effort device type from a node id alone.

    Used when the node is NOT holding a socket, so there is no
    ``node_register`` payload to read the declared ``node_type`` from.

    The phone rules match the EXACT ``feral-iphone-`` / ``feral-ipad-``
    prefix minted by this repo's own iOS SDK
    (``feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift:179``).
    That is reading our own namespace, not guessing.

    A loose substring must never yield a phone:
    ``tests/test_no_phone_placeholder.py`` pins ``node-phone-5``,
    ``pixel-7-camera`` and ``iphone-15`` as "unknown", because a
    substring heuristic used to invent a phone the user had not paired.
    "unknown" remains the default and is the honest answer.
    """
    low = (node_id or "").lower()
    if low.startswith("feral-iphone-"):
        return "iphone"
    if low.startswith("feral-ipad-"):
        return "ipad"
    if "glasses" in low or "w300" in low or "w610" in low:
        return "glasses"
    if "wristband" in low or "watch" in low or "veepoo" in low:
        return "wearable"
    if "browser" in low and "camera" in low:
        return "browser_camera"
    if "browser" in low:
        return "browser_node"
    if "robot" in low:
        return "robot"
    return "unknown"


# ─────────────────────────────────────────────────────────────
# Sub-device grouping
# ─────────────────────────────────────────────────────────────

def _peripheral_name(row: dict) -> str:
    attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
    return str(attrs.get("device_name") or "").strip()


def group_subdevice_rows(rows: Iterable[dict], *, now: Optional[float] = None) -> list[dict]:
    """Collapse repeated observations of ONE peripheral into one record.

    The owner's install holds six ``jw_health_glasses`` rows spread over
    six node ids, because the phone minted a new node id on each install and the store is keyed
    ``(node_id, capability)``. Six rows, one pair of glasses.

    Identity rule, in order:

    1. Bucket by ``capability``. On a single-user local install (the
       only supported target, see CLAUDE.md) a capability id such as
       ``jw_health_glasses`` names one physical class of thing.
    2. If the bucket carries two or more DISTINCT non-empty
       ``attrs.device_name`` values, those are genuinely different
       units -- split by name so a W300 and a W610 never merge.
    3. Rows with no ``device_name`` join the single named unit when
       there is exactly one; otherwise they stay their own record.
       Silence about a name is not evidence of a second device, but it
       is not evidence for picking between two either.

    The newest observation wins for ``status`` / ``attrs`` /
    ``provenance`` / ``live``. Every node id the peripheral was ever
    seen through is preserved in ``also_seen_via``.
    """
    ts = float(now) if now is not None else time.time()
    by_capability: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_capability[str(row.get("capability") or "")].append(row)

    out: list[dict] = []
    for capability, bucket in by_capability.items():
        names = {_peripheral_name(r) for r in bucket if _peripheral_name(r)}
        if len(names) <= 1:
            groups = [bucket]
        else:
            named: dict[str, list[dict]] = defaultdict(list)
            unnamed: list[dict] = []
            for row in bucket:
                name = _peripheral_name(row)
                (named[name] if name else unnamed).append(row)
            groups = list(named.values())
            if unnamed:
                groups.append(unnamed)
        for group in groups:
            out.append(_merge_peripheral(capability, group, now=ts))
    out.sort(key=lambda r: (not r["live"], -r["last_seen"]))
    return out


def _merge_peripheral(capability: str, group: list[dict], *, now: float) -> dict:
    ordered = sorted(group, key=lambda r: float(r.get("last_seen") or 0.0), reverse=True)
    newest = ordered[0]
    attrs = newest.get("attrs") if isinstance(newest.get("attrs"), dict) else {}
    last_seen = float(newest.get("last_seen") or 0.0)
    # Prefer any name we have ever seen: the newest row is sometimes the
    # one that omitted `device_name` (feral-iphone-79447a4cd1ed reported
    # a battery level and an empty name), and dropping the label there
    # would make a known device render as a bare capability id.
    name = next((_peripheral_name(r) for r in ordered if _peripheral_name(r)), "")
    return {
        "capability": capability,
        "name": name,
        "status": newest.get("status"),
        "live": bool(newest.get("live")),
        "provenance": newest.get("provenance"),
        "liveness_window_s": newest.get("liveness_window_s"),
        "first_seen": min(float(r.get("first_seen") or 0.0) for r in ordered),
        "last_seen": last_seen,
        "last_seen_age_s": max(0.0, now - last_seen),
        "attrs": attrs,
        "via_node_id": newest.get("node_id"),
        # Every other install this peripheral was seen through. Kept so
        # grouping is reversible and the user can still audit history.
        "also_seen_via": [
            r.get("node_id") for r in ordered[1:] if r.get("node_id") != newest.get("node_id")
        ],
        "observations": len(ordered),
    }


# ─────────────────────────────────────────────────────────────
# Reconnect guidance
# ─────────────────────────────────────────────────────────────

# Verified on the owner's install: `~/.feral/data/push_tokens.db` does
# not exist, so `channels/push.py` has zero registered APNs/FCM tokens
# and no configured credentials. There is no other outbound path to a
# node that is not currently holding a WebSocket -- pairing is
# phone-initiated (`/api/devices/pair/url` mints a token the PHONE must
# fetch). So the brain genuinely cannot reconnect anything, and a
# "Reconnect" button on the dashboard would do nothing. Say that
# instead of shipping the button.
RECONNECT_BRAIN_CANNOT_INITIATE = (
    "The brain has no way to wake a node that is not already holding a "
    "WebSocket: reconnection is always initiated by the device. No push "
    "credentials are configured (channels/push.py) and the pairing "
    "handshake starts on the phone."
)


def reconnect_guidance(entry: dict) -> dict:
    """What the user must actually do to get this node back.

    ``brain_can_initiate`` is always False today and is a field rather
    than a comment so a surface cannot render an action the brain
    cannot perform.
    """
    label = entry.get("name") or entry.get("node_id") or "the device"
    return {
        "brain_can_initiate": False,
        "why": RECONNECT_BRAIN_CANNOT_INITIATE,
        "steps": [
            # No retry count on purpose. This said "the iOS client
            # retries up to 10 times with backoff", which is not what the
            # phone client does: it runs 6 reconnect sweeps with
            # full-jitter backoff. A specific number a reader can wait
            # out is worse than no number when it is the wrong number,
            # and it would go stale again the next time the client's
            # backoff is retuned, in a different repo from this one.
            f"Open the FERAL app on {label}. It reconnects on its own with "
            "backoff, using the pairing token it already holds.",
            f"If it has not come back within {int(NODE_HEARTBEAT_WINDOW_S)} s, "
            "its pairing token has probably expired -- tokens last 24 h by "
            "default (security/device_pairing.DEFAULT_TTL_SECONDS). Pair "
            "again: Devices -> Pair new device, then scan the QR with the "
            "phone.",
        ],
    }


# ─────────────────────────────────────────────────────────────
# The device tree
# ─────────────────────────────────────────────────────────────

def build_device_view(
    *,
    live_nodes: Iterable[dict],
    subdevice_rows: Iterable[dict],
    now: Optional[float] = None,
) -> dict:
    """Join live daemons with the persistent sub-device store.

    ``live_nodes`` are the descriptions of nodes currently holding a
    WebSocket (``state.daemons``). ``subdevice_rows`` is
    ``NodeSubdeviceStore.list_all()`` -- the only record that outlives a
    disconnect.

    Returns ``{"devices": [...live...], "offline": [...],
    "heartbeat_window_s": NODE_HEARTBEAT_WINDOW_S}``.

    ``devices`` keeps its existing contract exactly (one entry per open
    socket) so no client breaks; ``offline`` is additive and carries the
    nodes that used to disappear.

    Folding rules:

    * Two live nodes are NEVER merged. Two open sockets are two
      devices, whatever their ids look like.
    * An offline node folds into a live node of the same family when
      that family has exactly one live node. That is the reinstall
      case: the phone in the user's hand is the same phone that carried
      the glasses last month, and its old install ids belong in
      ``also_known_as``, not in the device list.
    * Otherwise offline nodes of a family fold into their most recently
      seen member.
    """
    ts = float(now) if now is not None else time.time()

    rows_by_node: dict[str, list[dict]] = defaultdict(list)
    for row in subdevice_rows:
        rows_by_node[str(row.get("node_id") or "")].append(row)

    live_entries: list[dict] = []
    live_by_family: dict[str, list[dict]] = defaultdict(list)
    for node in live_nodes:
        entry = _base_entry(node, connected=True, now=ts)
        live_entries.append(entry)
        live_by_family[entry["family"]].append(entry)

    # Every node id we know of that is not currently live.
    offline_ids = [
        nid for nid in rows_by_node
        if nid and not any(e["node_id"] == nid for e in live_entries)
    ]

    orphans_by_family: dict[str, list[str]] = defaultdict(list)
    for node_id in offline_ids:
        family = node_family(node_id)
        siblings = live_by_family.get(family) or []
        if len(siblings) == 1:
            # Same physical device, earlier install.
            siblings[0]["also_known_as"].append(node_id)
            siblings[0]["_rows"].extend(rows_by_node[node_id])
        else:
            orphans_by_family[family].append(node_id)

    offline_entries: list[dict] = []
    for family, node_ids in orphans_by_family.items():
        node_ids.sort(
            key=lambda nid: max(
                (float(r.get("last_seen") or 0.0) for r in rows_by_node[nid]),
                default=0.0,
            ),
            reverse=True,
        )
        primary, *superseded = node_ids
        entry = _base_entry(
            {"node_id": primary, "type": node_type_from_id(primary)},
            connected=False,
            now=ts,
        )
        entry["family"] = family
        entry["also_known_as"] = list(superseded)
        entry["_rows"] = list(rows_by_node[primary])
        for nid in superseded:
            entry["_rows"].extend(rows_by_node[nid])
        offline_entries.append(entry)

    for entry in live_entries:
        entry["_rows"].extend(rows_by_node.get(entry["node_id"], []))

    for entry in live_entries + offline_entries:
        _finalise(entry, now=ts)

    offline_entries.sort(key=lambda e: -(e["last_seen"] or 0.0))
    return {
        "devices": live_entries,
        "offline": offline_entries,
        "heartbeat_window_s": NODE_HEARTBEAT_WINDOW_S,
    }


def _base_entry(node: dict, *, connected: bool, now: float) -> dict:
    node_id = str(node.get("node_id") or "")
    entry = dict(node)
    entry.update({
        "node_id": node_id,
        "family": node_family(node_id),
        "type": node.get("type") or node_type_from_id(node_id),
        "connected": connected,
        "status": "connected" if connected else "disconnected",
        "also_known_as": [],
        "heartbeat_window_s": NODE_HEARTBEAT_WINDOW_S,
        "_rows": [],
    })
    if connected:
        # A node holding an open socket was, by definition, heard from
        # now. Recording it explicitly means every surface reads
        # last_seen the same way instead of special-casing live rows.
        entry["last_seen"] = now
        entry["last_seen_age_s"] = 0.0
    return entry


def _finalise(entry: dict, *, now: float) -> None:
    rows = entry.pop("_rows", [])
    entry["subdevices"] = group_subdevice_rows(rows, now=now)
    if not entry["connected"]:
        last_seen = max((float(r.get("last_seen") or 0.0) for r in rows), default=0.0)
        entry["last_seen"] = last_seen or None
        entry["last_seen_age_s"] = (now - last_seen) if last_seen else None
        entry["reconnect"] = reconnect_guidance(entry)
    entry.setdefault("name", "")
    entry["also_known_as"] = sorted(set(entry["also_known_as"]))


# ─────────────────────────────────────────────────────────────
# Pairing rows: what a `paired_devices` row actually represents
# ─────────────────────────────────────────────────────────────

# User-agent fragment -> what the claimant IS. Ordered: iPad must be
# tested before the generic Mac fragment because iPadOS Safari sends a
# desktop UA string containing "Macintosh".
_UA_KINDS = (
    ("ipad", "tablet"),
    ("iphone", "phone"),
    ("ipod", "phone"),
    ("android", "phone"),
    ("windows phone", "phone"),
)

_KIND_LABELS = {
    "phone": "Phone",
    "tablet": "Tablet",
    "browser": "Browser",
    "browser_node_v2": "Browser",
    "hup": "HUP node",
    "name": "Device",
    "pending": "Pairing code",
}


def kind_from_platform(platform: str, *, fallback: str = "browser") -> str:
    """What kind of device sent this user agent.

    ``/api/devices/pair/complete`` receives ``kind: "browser_node_v2"``
    from the /pair page -- that is the TRANSPORT the claimant speaks,
    not what the claimant is. An iPhone running the /pair page in Safari
    sends exactly that string, which is how a phone ended up recorded as
    a browser connection the owner says he never made.
    """
    low = (platform or "").lower()
    for fragment, kind in _UA_KINDS:
        if fragment in low:
            return kind
    if "ios" in low:
        return "phone"
    return fallback


def describe_pairing_row(row: dict) -> dict:
    """Say what a ``paired_devices`` row is, without deleting anything.

    43 of the owner's 61 rows were issued by opening the pair modal and
    never claimed by any device. They are pairing CODES, not devices,
    and counting them as devices is what filled the list with browsers
    he never paired. ``is_device`` separates the two; the row itself is
    left untouched so pairing history survives.
    """
    kind = str(row.get("kind") or "").strip().lower()
    claimed = bool(row.get("claimed_at") or row.get("last_seen"))
    if not claimed:
        return {
            **row,
            "is_device": False,
            "label": "Pairing code (unclaimed)",
            "explain": (
                "A pairing token was issued (someone opened the pair "
                "screen) and no device ever completed the handshake."
            ),
        }
    platform = str(row.get("platform") or "")
    resolved = kind_from_platform(platform, fallback=kind or "browser") if platform else kind
    label = _KIND_LABELS.get(resolved, resolved.title() if resolved else "Device")
    if resolved == "phone" and "iphone" in platform.lower():
        label = "iPhone"
    name = str(row.get("name") or "").strip()
    if name and name.lower() not in {"unnamed", "phone", "device", ""}:
        label = name
    return {**row, "is_device": True, "label": label, "explain": ""}
