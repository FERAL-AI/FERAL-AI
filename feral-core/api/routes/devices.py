"""Device mesh, session handoff, command ledger, node health, and pairing endpoints."""

import base64
import io
import json
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.device_view import (
    build_device_view,
    describe_pairing_row,
    node_type_from_id,
)
from api.middleware.rate_limit import code_claim_limiter
from api.state import state
from config.access_mode import LOOPBACK_HOSTS, AccessMode, coerce
from config.runtime import bound_host, brain_port, brain_public_base_url
from services.netinfo import detect_lan_ipv4

logger = logging.getLogger("feral.pair")
router = APIRouter()


# ─────────────────────────────────────────────
# Pair URL resolver — Mode A (LAN) / B (localhost) / C (remote)
# ─────────────────────────────────────────────


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower().strip("[]")
    return h in {"", "localhost", "::1", "0.0.0.0"} or h.startswith("127.")


def _detect_lan_ip() -> str:
    """This machine's best LAN address, or "" if it has none.

    Thin shim over :mod:`services.netinfo`, kept because tests and the
    diagnostic patch this name. The implementation it replaced connected
    to ``8.8.8.8:80`` with **no timeout**, inside the request that mints
    a pairing QR: behind a captive portal that call blocks, and the pair
    modal hangs with it. It also fell back to ``gethostbyname``, which
    on a misconfigured host cheerfully returns a loopback address.
    """
    return detect_lan_ipv4()


def _normalize_origin(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    host = parsed.hostname or ""
    if not host:
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    suffix = "" if port in (None, default_port) else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


class PairUnavailable(Exception):
    """Raised when the configured access mode cannot emit a pair URL.

    Carries a machine-readable ``code`` and, where one exists, a
    ``fix``: the single action that would make pairing possible. The
    default access mode is deliberately private, so "pairing is off" is
    the expected state on a fresh install rather than an error, and the
    UI should be able to offer one button instead of asking the user to
    understand access modes and go find Settings.
    """

    def __init__(self, message: str, *, code: str = "pair_unavailable", fix: dict | None = None):
        super().__init__(message)
        self.code = code
        self.fix = fix

    def as_detail(self) -> dict:
        detail = {"code": self.code, "message": str(self)}
        if self.fix:
            detail["fix"] = self.fix
        return detail


# The one-tap consent. Offered wherever pairing is refused because the
# brain is private, which is the default and therefore the common case.
_ENABLE_LAN_FIX = {
    "action": "set_access_mode",
    "mode": AccessMode.LAN.value,
    "label": "Enable same-WiFi pairing",
    "consequence": (
        "Your brain becomes reachable by other devices on whatever "
        "network this computer is joined to. On an untrusted network "
        "(hotel, cafe) turn it off again afterwards."
    ),
}


@dataclass(frozen=True)
class PairCandidate:
    """One address a phone could try, and what is true about it."""

    kind: str
    url: str
    encrypted: bool
    caveat: str = ""

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "url": self.url, "encrypted": self.encrypted}
        if self.caveat:
            out["caveat"] = self.caveat
        return out


# The order candidates are offered in.
#
# A raw LAN literal leads because it needs no name resolution, and
# because App Transport Security does not apply to it: on iOS 10 and
# later ATS is simply not evaluated for requests targeting an IP
# address. An earlier revision of this comment had that backwards,
# treating the literal as the disputed case and ``.local`` as the safe
# one. It is the other way round. ``NSAllowsLocalNetworking`` exists for
# unqualified host names and ``.local`` names, which is precisely what
# Bonjour discovery hands back, so the mDNS candidate still earns its
# place, and the plist key still earns its place, for that path rather
# than as insurance for this one.
#
# ATS is not the only mechanism in play. Local Network privacy is
# separate and unverified on current iOS, so if a device test shows the
# literal failing, that is a permission-prompt problem and not an
# ordering problem, and swapping these entries would not fix it.
CANDIDATE_ORDER = ("lan", "mdns", "tailscale", "relay")


def _pair_scheme() -> str:
    """``https`` when the brain terminates TLS, else ``http``.

    The scheme was hardcoded to ``http://`` in the LAN branch, so a
    TLS-enabled brain advertised a URL on the wrong scheme and every
    phone failed the handshake.
    """
    try:
        from config.runtime import brain_tls_enabled

        return "https" if brain_tls_enabled() else "http"
    except Exception:  # pragma: no cover - defensive
        return "http"


def _tls_caveat() -> str:
    """Why TLS on the LAN is currently worse than cleartext on the LAN.

    ``_ensure_tls_certs`` generates a self-signed certificate, and iOS
    has no trust override and no pinning. So a TLS-enabled LAN brain is
    refused outright by the phone, where a cleartext one connects. Said
    out loud rather than discovered.
    """
    return (
        "TLS is enabled with a self-signed certificate. iOS will refuse "
        "this connection. Disable TLS for same-WiFi pairing."
    )


def _assert_listener_agrees(mode: AccessMode) -> None:
    """Refuse to advertise a LAN address the live process is not serving.

    ``bind_host`` is read once, at bind time, so applying a mode to a
    running brain persists the setting without moving the listener. The
    reported bug was exactly this gap: settings said "Same WiFi", the
    process was still on loopback, and the QR advertised a LAN address
    nothing answered on. Comparing intent against
    :func:`config.runtime.bound_host` closes it *before* the restart,
    rather than emitting a URL that cannot work and blaming the network.

    ``None`` means no listener in this process (a CLI invocation, a
    test), so there is nothing to contradict.
    """
    if mode is not AccessMode.LAN:
        return
    actual = bound_host()
    if actual is None or actual not in LOOPBACK_HOSTS:
        return
    raise PairUnavailable(
        "Configured for same-WiFi pairing, but this brain is currently "
        f"listening on {actual}, so nothing outside this machine can reach "
        "it. Restart the brain (`feral restart`) to apply the change, then "
        "pair again.",
        code="restart_required",
    )


def _resolve_pair_origin() -> str:
    """Pick the pair-URL origin based on the configured access mode.

    "localhost" → unavailable; pairing requires network exposure
    "local"     → http://<lan-ip>:<brain-port>
    "relay"     → unavailable until the relay tunnel ships
    "remote"    → access.tailscale.tailnet_url, falling back to
                  FERAL_PUBLIC_BASE_URL

    Never falls back to a loopback URL silently — emitting
    http://127.0.0.1:9090 to a phone is the bug we are killing.

    Dispatch is on :class:`AccessMode` rather than raw strings so a mode
    added to the enum cannot quietly inherit the LAN branch by falling
    off the end of the ``if`` chain. That is not hypothetical: ``relay``
    was added to the enum bound to loopback, and under the old string
    comparisons it landed in the LAN branch and advertised an address
    nothing was listening on — the very bug this resolver exists to
    prevent.
    """
    cfg = getattr(state, "config", None)
    mode = coerce(cfg.access_pairing_mode if cfg else AccessMode.LOCALHOST.value)

    if mode is AccessMode.LOCALHOST:
        raise PairUnavailable(
            "This brain is private, so phones cannot reach it yet. "
            "Mode B (localhost) does not expose pairing.",
            code="pairing_disabled",
            fix=_ENABLE_LAN_FIX,
        )

    if mode is AccessMode.RELAY:
        raise PairUnavailable(
            "Any-network (relay) access is selected, but the relay tunnel "
            "is not implemented yet, so there is no address to advertise.",
            code="relay_not_implemented",
            fix=_ENABLE_LAN_FIX,
        )

    if mode is AccessMode.TAILSCALE:
        configured = cfg.access_remote_url if cfg else ""
        url = _normalize_origin(configured) or _normalize_origin(brain_public_base_url())
        if not url:
            raise PairUnavailable(
                "Mode C (remote) is selected but no public URL is configured. "
                "Run `feral access remote-up` to bring up Tailscale Funnel, "
                "or set FERAL_PUBLIC_BASE_URL."
            )
        # Reject loopback in remote mode (can happen if FERAL_PUBLIC_BASE_URL
        # was left on a default and the operator forgot to override it).
        host = (urlparse(url).hostname or "").lower()
        if _is_loopback_host(host):
            raise PairUnavailable(
                "Mode C (remote) resolved to a loopback URL. "
                "Configure FERAL_PUBLIC_BASE_URL or run `feral access remote-up`."
            )
        return url

    # Mode A — LAN
    _assert_listener_agrees(mode)
    ip = _detect_lan_ip()
    if not ip:
        raise PairUnavailable(
            "LAN IP not detected. Are you connected to a network? "
            "Switch to localhost or remote mode if not."
        )
    return f"{_pair_scheme()}://{ip}:{brain_port()}"


def _resolve_pair_candidates() -> list[PairCandidate]:
    """Every address a phone could try, best first.

    One address was never enough. A multi-homed machine is reachable on
    several and the resolver returned whichever the kernel picked; a
    phone whose ATS policy refuses a raw literal needs the ``.local``
    name; and a remote transport does not remove the LAN path, which is
    faster and keeps working when the tunnel is down.

    Raises :class:`PairUnavailable` when the list would be empty, with
    the same message the single-origin resolver used, so the failure
    text a stuck user sees is unchanged.
    """
    cfg = getattr(state, "config", None)
    mode = coerce(cfg.access_pairing_mode if cfg else AccessMode.LOCALHOST.value)
    scheme = _pair_scheme()
    port = brain_port()
    caveat = _tls_caveat() if scheme == "https" else ""
    encrypted = scheme == "https"

    by_kind: dict[str, list[PairCandidate]] = {k: [] for k in CANDIDATE_ORDER}

    if mode is AccessMode.TAILSCALE:
        # Delegates to the single-origin resolver so the remote branch
        # keeps exactly one implementation, including its refusals.
        by_kind["tailscale"].append(
            PairCandidate(kind="tailscale", url=_resolve_pair_origin(), encrypted=True)
        )
    else:
        # Raises for localhost and relay, and for a LAN mode whose
        # listener disagrees. Do this before building anything.
        _resolve_pair_origin()

        from services.netinfo import detect_lan_ipv4s, mdns_hostname

        for ip in detect_lan_ipv4s():
            by_kind["lan"].append(
                PairCandidate(
                    kind="lan",
                    url=f"{scheme}://{ip}:{port}",
                    encrypted=encrypted,
                    caveat=caveat,
                )
            )

        host = mdns_hostname()
        if host and host != ".local":
            by_kind["mdns"].append(
                PairCandidate(
                    kind="mdns",
                    url=f"{scheme}://{host}:{port}",
                    encrypted=encrypted,
                    caveat=caveat,
                )
            )

    candidates = [c for kind in CANDIDATE_ORDER for c in by_kind[kind]]
    if not candidates:
        raise PairUnavailable(
            "LAN IP not detected. Are you connected to a network? "
            "Switch to localhost or remote mode if not."
        )
    return candidates


def _build_diagnostic(origin_url: str) -> dict:
    """Honest reachability diagnostic for the pair modal.

    The brain CANNOT test from the phone's perspective. We only report
    what we know — that we successfully resolved a URL — and surface
    common failure modes (AP isolation, CGNAT, Funnel propagation) as
    text the UI shows verbatim.
    """
    cfg = getattr(state, "config", None)
    mode = cfg.access_pairing_mode if cfg else "localhost"
    parsed = urlparse(origin_url)
    diagnostic = {
        "mode": mode,
        "advertised_url": origin_url,
        "advertised_lan_ip": parsed.hostname or "",
        # What the live process actually bound, not what settings say it
        # would bind next boot. Null when nothing is serving in this
        # process. Surfaced so a stuck user can see the mismatch that
        # ``_assert_listener_agrees`` guards against.
        "listening_on": bound_host(),
        "honest_caveats": [],
    }
    if mode == "local":
        diagnostic["honest_caveats"].append(
            "I cannot test from your phone's perspective."
        )
        diagnostic["honest_caveats"].append(
            "If your phone gets connection refused, your WiFi may have "
            "AP / client isolation enabled (common in coffee shops and hotels)."
        )
    elif mode == "remote":
        diagnostic["honest_caveats"].append(
            "Funnel URLs may take up to 30 seconds to propagate after first enable."
        )
    return diagnostic


def _pair_link_blob(
    mode: str, brain_id: str, result: dict, candidates: list | None = None
) -> str:
    """Base64url-encode the identity fields for carrying inside the URL.

    The QR encodes ``payload["url"]`` and nothing else, so every field
    outside that string is invisible to anything that scans it. That is
    why ``brain_id`` never reaches the phone, and why the comment on
    ``ConfigLoader.brain_id`` describing phones as refusing to re-pair
    against a different brain describes a check the transport could not
    support.

    Carried as a ``p=`` query parameter rather than by encoding the whole
    JSON document into the QR, so the URL keeps working when scanned by a
    plain camera app (which opens the ``/pair`` page) while a client that
    knows to look gets the identity fields too. Deliberately compact: the
    reachability diagnostic stays out of it, because it is prose destined
    for the pair modal, and inflating the QR hurts scan reliability.
    """
    payload = {
        "v": 1,
        "mode": mode,
        "brain_id": brain_id,
        "expires": int(result.get("expires_at") or 0),
        "device_id": result["device_id"],
        "name": "FERAL Brain",
    }

    # Candidate URLs ride along because the QR is the only channel a
    # scanned pairing has. Without them a phone learns exactly one
    # address and stores it, which is why re-pairing on a second network
    # used to clobber the first: the store had nowhere to put a second
    # endpoint because it was never given one.
    #
    # Kinds and order only, no caveat text: the QR pays for every byte in
    # scan reliability, and the caveats are prose for the pair modal,
    # which already has the full payload over HTTP.
    if candidates:
        payload["urls"] = [
            {"kind": c.kind, "url": c.url} for c in candidates
        ]

    blob = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def _pair_payload(result: dict, origin: str | None = None) -> dict:
    """Build the unified v1 pair payload (single shape for QR + URL).

    See ``A4-pairing-redesign.md`` §4. Replaces the legacy mode=app /
    mode=web fork; clients always get the same JSON.
    """
    # `v` stays 1 forever. FeralPairingPayload.swift guards on
    # `version == 1`, so bumping it breaks every shipped iOS build.
    # `schema` is the additive signal: a client that understands it
    # reads `urls`, one that does not reads `url` and is unaffected.
    # Candidates are computed whether or not the caller resolved an
    # origin. The routes resolve one first so a PairUnavailable becomes
    # a 409 *before* a token is minted, which is what keeps 409s from
    # leaving orphan rows in paired_devices; passing it here must not
    # then suppress the candidate list.
    try:
        candidates = _resolve_pair_candidates()
    except PairUnavailable:
        if origin is None:
            raise
        # The origin already resolved, so the caller is committed and a
        # token exists. Degrade to describing that one address rather
        # than failing a request that has already had side effects.
        candidates = [
            PairCandidate(kind="lan", url=origin, encrypted=origin.startswith("https"))
        ]

    if origin is None:
        origin = candidates[0].url
    elif origin not in [c.url for c in candidates]:
        # The caller resolved something the candidate builder did not
        # produce. Trust the caller: it is the value the QR encodes.
        candidates = [
            PairCandidate(kind="lan", url=origin, encrypted=origin.startswith("https"))
        ] + candidates

    cfg = getattr(state, "config", None)
    mode = cfg.access_pairing_mode if cfg else "localhost"
    brain_id = cfg.brain_id if cfg else ""
    blob = _pair_link_blob(mode, brain_id, result, candidates)
    return {
        "v": 1,
        "schema": 2,
        "mode": mode,
        "urls": [c.as_dict() for c in candidates],
        "url": f"{origin.rstrip('/')}/pair?t={result['token']}&p={blob}",
        "token": result["token"],
        "brain_id": brain_id,
        "expires": int(result.get("expires_at") or 0),
        "name": "FERAL Brain",
        "device_id": result["device_id"],
        "diagnostic": _build_diagnostic(origin),
    }


def _infer_node_type(node_id: str, ws) -> str:
    """Pick the most honest node_type label for a connected daemon.

    Priority:
    1. ``ws._feral_node_type`` — set at ``node_register`` time from the
       HUP payload. This is the authoritative source.
    2. ``state.skill_executor._daemon_types[node_id]`` — a mirror set at
       the same moment; used as fallback if the ws attr is missing for
       any reason.
    3. A node_id prefix heuristic (``feral-w300-*`` → glasses,
       ``feral-wristband-*`` → wearable). Last-resort.
    4. ``"unknown"`` when nothing else fits. We never silently label
       something "phone" again.
    """
    declared = getattr(ws, "_feral_node_type", None)
    if declared:
        return declared
    if state.skill_executor is not None:
        mirror = getattr(state.skill_executor, "_daemon_types", {}).get(node_id)
        if mirror:
            return str(mirror).lower()
    # Single id heuristic, shared with the offline half of the tree in
    # `api/device_view.py`. Keeping two copies is how the dashboard and
    # the prompt ended up disagreeing about what a device was.
    #
    # "phone" is still never guessed from a loose substring; the only
    # phone-shaped rule is this repo's own iOS SDK prefix
    # (`feral-iphone-`, FeralBrainClient.swift:179), which is our
    # namespace, not a guess.
    return node_type_from_id(node_id)


def _describe_device(node_id: str, ws) -> dict:
    return {
        "node_id": node_id,
        "type": _infer_node_type(node_id, ws),
        "capabilities": list(getattr(ws, "_feral_capabilities", []) or []),
        "platform": getattr(ws, "_feral_platform", "") or "",
        "manufacturer": getattr(ws, "_feral_manufacturer", "") or "",
        "model": getattr(ws, "_feral_model", "") or "",
        "status": "connected",
        "subdevices": _subdevices_for(node_id),
    }


def _subdevices_for(node_id: str) -> list[dict]:
    """Return the list of sub-device records owned by ``node_id``.

    Empty list ONLY when the truth store has no rows for that node.
    Returns an empty list when the store is unavailable (boot still
    in flight); a hard read-failure is propagated up to the FastAPI
    error handler rather than silently swallowed — surfacing the
    failure beats showing the dashboard an empty tree as if the
    node had no sub-devices.

    Truth-in-status: callers should consume each entry's ``live``
    flag, not assume "row exists ⇒ live".
    """
    store = getattr(state, "node_subdevices", None)
    if store is None or not node_id:
        return []
    return store.list_for_node(node_id)


def _all_subdevice_rows() -> list[dict]:
    store = getattr(state, "node_subdevices", None)
    if store is None:
        return []
    return store.list_all()


@router.get("/api/devices/connected")
async def connected_devices():
    """The whole device tree: what is live, and what dropped.

    ``devices[]`` is unchanged and still selection-bound to open
    WebSockets in ``state.daemons``. Every existing client keeps
    working. Two keys are added:

    * ``offline[]``: nodes the brain knows about that are NOT holding
      a socket right now, each with ``status: "disconnected"``,
      ``last_seen`` and a ``reconnect`` block. This exists because the
      disconnect teardown (``api/server.py`` ``except
      WebSocketDisconnect``) pops ``state.daemons`` AND unregisters the
      node from ``hardware_mesh``, so a phone that dropped previously
      did not become "disconnected", it stopped existing. A UI cannot
      render "your glasses went offline" from an empty list, and the
      owner reported exactly that: devices that dropped still read as
      connected because nothing ever said otherwise.
    * ``heartbeat_window_s``: the single node staleness window
      (3 x the HUP ``heartbeat_ms`` default), so no surface has to
      pick its own number.

    Entries on both lists carry ``connected: bool``, ``last_seen`` and
    ``subdevices[]``. Sub-devices are grouped per physical peripheral,
    so one pair of glasses seen through six install-scoped node ids is
    one row with ``also_seen_via`` naming the other five. Nothing is
    deleted to achieve that.

    Empty ``devices`` still means "no daemon WebSocket open right now".
    Empty ``devices`` AND empty ``offline`` means "nothing has ever
    paired".
    """
    # `state.daemons` is the ONLY structure the /v1/node handler writes on
    # `node_register` (api/server.py `state.daemons[node_id] = ws`).
    # `SessionHandoffManager.register_device` is called from exactly one
    # place in the brain (api/state.py, the messaging-channel bridge), so
    # its registry never contains a HUP daemon.
    #
    # This branch used to *replace* the daemon-derived list with the
    # handoff registry whenever `state.session_handoff` was set, which it
    # always is after boot. Measured against a live brain holding three
    # registered daemons (a phone, a wearable and a pair of glasses, all
    # visible in /api/hardware/mesh, /api/hardware/devices and
    # /api/hardware/fleet), this endpoint answered `{"devices": [],
    # "offline": []}`. Per this function's own contract that reads as
    # "nothing has ever paired", so the v2 Devices page rendered no Live
    # pane at all and the topology drew "Awaiting node".
    #
    # Both sources are now merged, keyed by node_id, with the daemon view
    # winning on conflict because it is the one backed by an open socket.
    live_by_id: dict[str, dict] = {}
    extra_rows: list = []

    for nid, ws in state.daemons.items():
        live_by_id[nid] = _describe_device(nid, ws)

    if state.session_handoff:
        for d in state.session_handoff.get_active_devices() or []:
            if not isinstance(d, dict):
                # Preserve any non-dict row the handoff manager returned
                # rather than dropping it: this endpoint has never
                # validated that shape and silently losing a row would be
                # a new defect.
                extra_rows.append(d)
                continue
            nid = d.get("node_id", "")
            if nid and nid in live_by_id:
                # The daemon row already describes this node from its own
                # node_register payload. Keep it.
                continue
            # Sanity-check the 'type' field isn't a hardcoded "phone"
            # default and attach the sub-device tree from our truth store.
            if not d.get("type") or d.get("type") == "phone":
                ws = state.daemons.get(nid)
                if ws is not None:
                    d = {**d, "type": _infer_node_type(nid, ws)}
            live_by_id[nid or d.get("session_id", "")] = d

    view = build_device_view(
        live_nodes=list(live_by_id.values()),
        subdevice_rows=_all_subdevice_rows(),
    )
    view["devices"].extend(extra_rows)
    return view


@router.get("/api/devices/{node_id}/subdevices")
async def list_node_subdevices(node_id: str):
    """Return the sub-device tree for a single node.

    Used by detail views that don't want the full
    ``/api/devices/connected`` payload. Returns ``{"subdevices": []}``
    when the node has never reported any. Each row exposes:

    * ``capability``, ``status`` — the canonical capability id and the
      domain status the iOS adapter reported (``"ready"`` / ``"failed"``
      / ``"connecting"`` / etc.).
    * ``live`` — *true only* when ``now - last_seen`` is inside the
      provenance heartbeat window. Surfaces consume this for the dot.
    * ``provenance`` — ``"ble"`` / ``"cloud"`` / ``"host"`` /
      ``"synthetic"``. Determines the heartbeat window.
    * ``first_seen`` / ``last_seen`` / ``liveness_window_s`` — operator
      tooling can render "last seen 12 s ago".
    * ``attrs`` — adapter-specific extras: ``device_name``, ``rssi``,
      ``battery_level``, ``reason`` for failures, etc.
    """
    return {"subdevices": _subdevices_for(node_id)}


@router.post("/api/devices/handoff")
async def session_handoff(request: Request):
    """Initiate a session handoff between devices."""
    body = await request.json()
    from_session = body.get("from_session", "")
    to_node_type = body.get("to_node_type", "desktop")

    if not state.session_handoff:
        return {"ok": False, "error": "Session handoff manager not available"}

    result = await state.session_handoff.handoff(from_session, to_node_type)
    return {"ok": bool(result.get("success")), **result}


@router.post("/api/proactive/dismiss")
async def dismiss_proactive(request: Request):
    """User dismissed a proactive alert — learn from it."""
    body = await request.json()
    trigger_id = body.get("trigger_id", "")
    if state.proactive and trigger_id:
        state.proactive.record_dismiss(trigger_id)
    return {"ok": True}


# ─────────────────────────────────────────────
# Command Ledger & Node Health endpoints
# ─────────────────────────────────────────────
# (Demo-mode routes /api/demo/status + /api/demo/scenario have moved
# to the optional `feral-demo-data` package and are mounted by the
# brain at boot only when FERAL_DEV_DEMO=1 + that package is
# installed. See packages/feral-demo-data/src/feral_demo_data/_integration.py
# `status_routes()`.)


@router.get("/api/commands/recent")
async def recent_commands(limit: int = 50):
    """Recent commands with full lifecycle state."""
    if not state.hardware_mesh:
        return {"commands": [], "error": "hardware mesh not initialised"}
    records = state.hardware_mesh.ledger.get_recent(limit=limit)
    return {
        "commands": [
            {
                "command_id": r.envelope.command_id,
                "node_id": r.envelope.node_id,
                "action": r.envelope.action,
                "priority": r.envelope.priority,
                "state": r.state.value,
                "created_at": r.envelope.created_at,
                "ack_at": r.ack_at,
                "completed_at": r.completed_at,
                "retries": r.retries,
                "correlation_id": r.envelope.correlation_id,
            }
            for r in records
        ],
        "stats": state.hardware_mesh.ledger.stats(),
    }


@router.get("/api/commands/{command_id}")
async def command_detail(command_id: str):
    """Single command full detail including state history and result."""
    if not state.hardware_mesh:
        return {"error": "hardware mesh not initialised"}
    record = state.hardware_mesh.ledger.get(command_id)
    if record is None:
        return {"error": "command not found"}
    return {
        "command_id": record.envelope.command_id,
        "node_id": record.envelope.node_id,
        "action": record.envelope.action,
        "params": record.envelope.params,
        "priority": record.envelope.priority,
        "state": record.state.value,
        "state_history": record.state_history,
        "created_at": record.envelope.created_at,
        "deadline": record.envelope.deadline,
        "ack_at": record.ack_at,
        "completed_at": record.completed_at,
        "result": record.result,
        "retries": record.retries,
        "idempotency_key": record.envelope.idempotency_key,
        "correlation_id": record.envelope.correlation_id,
    }


@router.get("/api/nodes/health")
async def nodes_health():
    """All node health status with heartbeat freshness."""
    if not state.hardware_mesh:
        return {"nodes": {}, "error": "hardware mesh not initialised"}
    return {"nodes": state.hardware_mesh.node_health.get_all()}


# ─────────────────────────────────────────────
# Device Pairing REST Endpoints
# ─────────────────────────────────────────────


@router.get("/api/devices/paired")
async def list_paired_devices(include_unclaimed: bool = False):
    """List paired edge-node devices — with typed metadata.

    By default only **claimed** rows are returned (those whose
    ``claimed_at`` is non-null), so the v2 Devices page no longer
    flashes phantom "device showed up the moment I clicked Pair"
    entries that were token-issuance side effects rather than real
    device attaches.

    Set ``?include_unclaimed=true`` to get every row, including
    unclaimed pair tokens. That mode is intended for admin / cleanup
    flows (e.g. the "Clear all unclaimed" button which feeds the
    ``/api/devices/pair/prune`` endpoint).

    The payload shape is unchanged — every key the v1/v2 client
    already reads (``device_id``, ``name``, ``paired_at``, ``last_seen``,
    ``kind``, ``node_id``, ``claimed_at``, ``platform``,
    ``capabilities``) is still present; only the row count is filtered.

    Three derived keys are added by ``describe_pairing_row``:
    ``is_device`` (False for a token nobody ever claimed), ``label``
    (what the thing IS, resolved from the claimant's platform rather
    than from the transport that carried the token) and ``explain``.
    The owner's install carries 61 rows, all ``kind='browser'``, 43 of
    them never claimed. Those are pairing codes, not browsers he
    paired. They are still returned; they are simply no longer counted
    as devices.
    """
    store = state.device_pairing_store
    devices = store.list_devices(include_unclaimed=bool(include_unclaimed))
    safe = [
        describe_pairing_row({
            "device_id": d["device_id"],
            "name": d["name"],
            "paired_at": d["paired_at"],
            "last_seen": d["last_seen"],
            "kind": d.get("kind", ""),
            "node_id": d.get("node_id", ""),
            "claimed_at": d.get("claimed_at"),
            "platform": d.get("platform", ""),
            "capabilities": d.get("capabilities", []),
        })
        for d in devices
    ]
    return {"devices": safe}


@router.post("/api/devices/pair")
async def pair_device(request: Request):
    """Pair a new edge-node device.

    Typed body — every pairing flow goes through this endpoint:

        {"kind": "name"}                    — label-only pair, generic QR
        {"kind": "hup", "node_id": "...",   — daemon / node SDK pair, declares
         "capabilities": [...] }              its node_id + capabilities up front
        {"kind": "browser",                 — browser-Node pair (Pair page)
         "platform": "...",                   includes user-agent hint
         "capabilities": [...] }
        {"kind": "pending"}                 - token issued, claimant unknown
                                              (what the QR / URL flows mint)

    All kinds accept an optional ``name`` label. Legacy body {name: ...}
    without ``kind`` is still honoured (falls back to kind="name").

    Returns the pairing record — token is included exactly once; clients
    must store it immediately because it won't be returned again.
    """
    body = await request.json() if await request.body() else {}
    name = body.get("name", "unnamed")
    kind = (body.get("kind") or "name").lower()
    if kind not in {"name", "hup", "browser", "browser_node_v2", "pending"}:
        raise HTTPException(status_code=400, detail=f"unknown pair kind: {kind}")
    node_id = body.get("node_id") or ""
    platform = body.get("platform") or ""
    capabilities = body.get("capabilities") or []
    if not isinstance(capabilities, list):
        raise HTTPException(status_code=400, detail="capabilities must be a list")

    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")

    return store.pair_device(
        name,
        kind=kind,
        node_id=node_id,
        platform=platform,
        capabilities=capabilities,
    )


@router.get("/api/devices/pair/qr")
async def pair_device_qr(request: Request, name: str = "unnamed", mode: str = "web"):
    """Generate a QR code PNG that encodes the unified v1 pair payload.

    The ``mode`` query parameter is **deprecated**. Both ``mode=app``
    and ``mode=web`` now emit the same v1 JSON; the old ``app``-shape
    ``{host, port, token, name}`` is no longer emitted. The legacy
    decoder in mobile clients accepts the old shape during the
    deprecation window (sunset 2026.7.0). When ``mode=app`` is passed
    we log so operators can find their stale callers.
    """
    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")
    if mode not in {"app", "web"}:
        raise HTTPException(status_code=400, detail="mode must be 'app' or 'web'")
    if mode == "app":
        logger.warning(
            "feral.pair.deprecated_mode_app_query — caller passed ?mode=app; "
            "the legacy shape is gone, emitting unified v1 payload anyway. "
            "Sunset: 2026.7.0."
        )

    try:
        origin = _resolve_pair_origin()
    except PairUnavailable as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail())
    # kind="pending", NOT "browser". At QR-mint time the brain does not
    # know what will scan it. Stamping "browser" here is what put 61
    # rows of `kind='browser'` into the owner's paired_devices.db, 43 of
    # them never claimed by anything: opening the pair screen recorded a
    # browser pairing he never made. The claimant declares what it is at
    # /api/devices/pair/complete.
    result = store.pair_device(name, kind="pending")
    payload = _pair_payload(result, origin=origin)

    encoded = payload["url"]
    try:
        import qrcode  # type: ignore[import-not-found]
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(encoded)
        qr.make(fit=True)
        img = qr.make_image(fill_color="white", back_color="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="image/png",
            headers={"X-Feral-Device-Id": result["device_id"]},
        )
    except ImportError:
        return {
            "pairing_info": payload,
            "note": "Install qrcode package for QR image",
        }


@router.get("/api/devices/pair/url")
async def pair_device_url(
    request: Request,
    name: str = "unnamed",
    pin: bool = False,
):
    """Return the web-pair URL + token WITHOUT an image — handy for tests
    and for the ``/pair`` landing page needing the token to render.

    ``pin=true`` (pair-pin-confirm PR) requests a 4-digit PIN second
    factor. When set, the response includes a ``pin`` field with the
    plaintext PIN — the dashboard MUST show it to the operator AT
    ISSUE TIME. After the response returns, the PIN can only be
    verified, not retrieved.
    """
    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")
    try:
        origin = _resolve_pair_origin()
    except PairUnavailable as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail())
    # See pair_device_qr: "pending" until a device claims the token.
    result = store.pair_device(
        name,
        kind="pending",
        require_pin=bool(pin),
    )
    payload = _pair_payload(result, origin=origin)
    payload["pin_required"] = result.get("pin_required", False)
    if result.get("pin"):
        # Plaintext PIN included for the operator's dashboard ONCE.
        # Phone never sees this in any subsequent request — the form
        # learns that a PIN is required via /api/devices/pair/check
        # and prompts the user to enter it manually.
        payload["pin"] = result["pin"]
    return payload


# ─────────────────────────────────────────────
# Code-pair flow (SDK polling)
# ─────────────────────────────────────────────


@router.post("/api/devices/pair/announce")
async def pair_announce(request: Request):
    """Daemon announces a 6-character base32 code it just generated.

    Body: ``{"code": "...", "node_id": "...", "name": "..."}``. The
    operator types the code into the dashboard "Type a pair code" field;
    the dashboard then claims it and the daemon's polling
    ``/api/devices/pair/status`` flips from ``pending`` → ``paired``
    with the issued token.

    The 8-char base32 code (~38 bits of entropy) plus the 600s TTL plus
    the 5-attempt-per-IP rate limit on ``/code/claim`` make brute force
    infeasible.
    """
    body = await request.json() if await request.body() else {}
    code = (body or {}).get("code", "").strip()
    node_id = (body or {}).get("node_id", "").strip()
    name = (body or {}).get("name", "").strip() or node_id or "unnamed"
    if not code or not node_id:
        raise HTTPException(status_code=400, detail="code and node_id required")

    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")
    store.announce_pending_code(code=code, node_id=node_id, name=name)
    return {"accepted": True}


@router.get("/api/devices/pair/status")
async def pair_status(code: str = "", node_id: str = ""):
    """Daemon polls this until the operator claims the announced code.

    Returns ``{"status": "pending" | "paired" | "expired", "token"?: ...}``.
    Honest 404 if the code is unknown — no SPA-HTML masking.
    """
    code = (code or "").strip()
    node_id = (node_id or "").strip()
    if not code or not node_id:
        raise HTTPException(status_code=400, detail="code and node_id required")

    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")
    record = store.lookup_pending_code(code=code, node_id=node_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown pairing code")
    if record["expires_at"] <= record["_now"]:
        return {"status": "expired"}
    if record.get("token"):
        return {"status": "paired", "token": record["token"]}
    return {"status": "pending"}


@router.post("/api/devices/pair/code/claim")
async def pair_code_claim(request: Request):
    """Operator claims an announced code from the dashboard.

    Body: ``{"code": "..."}``. On match: mints a real device-pairing
    token, writes it back to the pending row, returns the token.

    Rate-limited to 5 wrong attempts per source IP per 15 minutes; on
    over-cap the IP gets a 429 with a Retry-After header. >10 wrong
    attempts against a single code → server-side invalidates the code
    (anti-correlation).
    """
    client_host = request.client.host if request.client else "unknown"
    if not code_claim_limiter.allow(client_host):
        retry_after = code_claim_limiter.retry_after(client_host)
        raise HTTPException(
            status_code=429,
            detail="too many pair-code claim attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )

    body = await request.json() if await request.body() else {}
    code = (body or {}).get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")

    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")

    outcome = store.claim_pending_code(code=code)
    if outcome is None:
        # Wrong code → bump per-IP counter; surface honest 404 not 401
        # (avoids leaking whether a code exists in any state).
        code_claim_limiter.record_failure(client_host)
        raise HTTPException(status_code=404, detail="unknown or expired pairing code")
    return {
        "token": outcome["token"],
        "device_id": outcome["device_id"],
        "expires_at": outcome["expires_at"],
    }


# ─────────────────────────────────────────────
# PIN second-factor (pair-pin-confirm PR)
# ─────────────────────────────────────────────


@router.get("/api/devices/pair/check")
async def pair_device_check(t: str = ""):
    """Phone calls this BEFORE rendering the pair form.

    Returns {pin_required, pin_length}. Open-listed (the response
    leaks nothing beyond pin-or-not, harmless given the phone has the
    URL token). Unknown tokens look the same as no-PIN tokens.
    """
    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")
    return {
        "pin_required": store.token_requires_pin(t or ""),
        "pin_length": store.PIN_DIGITS,
    }


@router.post("/api/devices/pair/verify_pin")
async def pair_device_verify_pin(body: dict):
    """Phone submits the PIN before completing the pair."""
    token = (body or {}).get("token", "").strip()
    pin = str((body or {}).get("pin", "")).strip()
    if not token or not pin:
        raise HTTPException(status_code=400, detail="token and pin required")

    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")

    ok, reason = store.verify_pin(token, pin)
    if ok:
        return {"ok": True, "verified": True}

    if reason == "wrong_pin":
        raise HTTPException(
            status_code=401,
            detail={
                "code": "wrong_pin",
                "attempts_remaining": f"capped at {store.PIN_MAX_ATTEMPTS}",
            },
        )
    if reason == "no_pin_required":
        raise HTTPException(status_code=409, detail={"code": "no_pin_required"})
    if reason in ("exhausted", "expired", "unknown_token"):
        raise HTTPException(status_code=404, detail={"code": reason})
    raise HTTPException(status_code=400, detail={"code": "verification_failed"})




@router.post("/api/devices/pair/prune")
async def prune_unclaimed_pairings(body: dict = None):
    """Bulk-revoke pairing tokens that were issued but never attached.

    Body shape::
        { "older_than_seconds": 1800 }  # default: 30 minutes

    A token becomes "claimed" only when a daemon / browser-node
    connects to /v1/node with it AND `/api/devices/pair/complete` is
    hit. The v2 Devices page calls this on the "Clear all unclaimed"
    button so legacy rows named ``phone`` / ``unnamed`` /
    ``browser_camera_share`` can be scrubbed in one click.
    """
    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")
    older = float((body or {}).get("older_than_seconds", 1800))
    result = store.revoke_unclaimed(older_than_seconds=older)
    return {"success": True, **result}


@router.post("/api/devices/pair/complete")
async def pair_device_complete(body: dict):
    """Mark a pairing token as claimed by the device that just attached.

    Called by BrowserNode.js the moment its WebSocket register succeeds;
    the UI on the brain-side then shows "device connected" instead of
    "token issued, no attach yet".

    ``kind`` here is the TRANSPORT the claimant speaks
    (``browser_node_v2``), not what the claimant IS. An iPhone running
    the /pair page in Safari sends exactly that string, which is why
    the owner's phone was recorded as a browser connection he says he
    never made. ``platform`` (the claimant's user agent) and
    ``node_id`` are now threaded through so the row records the real
    device. Both are optional: a client that sends only ``token``
    behaves exactly as before.
    """
    token = (body or {}).get("token") or ""
    kind = ((body or {}).get("kind") or "").strip().lower()
    platform = str((body or {}).get("platform") or "").strip()
    claim_node_id = str((body or {}).get("node_id") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token required")
    store = state.device_pairing_store
    if not store:
        raise HTTPException(status_code=503, detail="Pairing store not initialized")
    # PIN gate (pair-pin-confirm PR): tokens with require_pin=True must
    # have called /verify_pin first; legacy tokens (no PIN) skip the
    # gate so backward-compat is preserved. Unknown-token check still
    # fires next so 404 contract is preserved.
    if store.token_requires_pin(token) and not store.token_pin_verified(token):
        raise HTTPException(
            status_code=401,
            detail={"code": "pin_not_verified"},
        )
    device_id = store.mark_claimed(
        token,
        kind=kind,
        platform=platform,
        node_id=claim_node_id,
    )
    if device_id is None:
        raise HTTPException(status_code=404, detail="unknown pairing token")

    response = {
        "success": True,
        "device_id": device_id,
        "paired_device_id": device_id,
        "pair_claim_marker": f"claim-{secrets.token_hex(12)}",
    }
    if kind == "browser_node_v2":
        rotated = store.rotate_phone_bearer(device_id)
        if not rotated:
            raise HTTPException(
                status_code=500,
                detail="failed to issue phone bearer",
            )
        response.update({
            "phone_bearer": rotated["phone_bearer"],
            "phone_bearer_expires_at": rotated["expires_at"],
            "phone_bearer_ttl_seconds": rotated["ttl_seconds"],
        })
    return response


@router.delete("/api/devices/{device_id}")
async def revoke_device(device_id: str):
    """Revoke (un-pair) a device."""
    store = state.device_pairing_store
    ok = store.revoke_device(device_id)
    if not ok:
        return {"ok": False, "error": "device not found"}
    return {"ok": True}
