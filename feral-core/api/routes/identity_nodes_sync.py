"""Identity (SOUL/MEMORY), node/device listing, and federated sync HTTP endpoints."""

from fastapi import APIRouter

from api.state import state
from security.peer_roster import (
    get_peer_roster,
    identity_mode,
    load_outbound_grants,
    store_outbound_grant,
)

router = APIRouter()


# ─────────────────────────────────────────────
# Identity API (enhanced)
# ─────────────────────────────────────────────


@router.get("/api/identity/soul")
async def get_soul():
    """Get the agent's SOUL.md (personality)."""
    if state.identity_workspace:
        return {"soul": state.identity_workspace.read_soul()}
    return {"soul": ""}


@router.post("/api/identity/soul")
async def update_soul(body: dict):
    """Update the agent's SOUL.md."""
    if state.identity_workspace:
        if body.get("append"):
            state.identity_workspace.append_soul(body["append"])
        elif body.get("content"):
            state.identity_workspace.write_soul(body["content"])
        return {"ok": True}
    return {"error": "Identity workspace not initialized"}


@router.get("/api/identity/memory_md")
async def get_memory_md():
    """Get the agent's MEMORY.md (long-term curated memory)."""
    if state.identity_workspace:
        return {"memory": state.identity_workspace.read_memory()}
    return {"memory": ""}


@router.get("/api/nodes")
async def list_nodes():
    """List all connected hardware nodes."""
    nodes = []
    for node_id, ws in state.daemons.items():
        nodes.append({
            "node_id": node_id,
            "connected": True,
            "sessions": list(state.get_sessions_for_daemon(node_id)),
        })
    return {"nodes": nodes, "count": len(nodes)}


@router.get("/api/devices")
async def list_devices():
    """List all connected hardware nodes / daemons."""
    nodes = []
    for node_id, info in state.daemons.items():
        _ = info if not isinstance(info, dict) else None
        nodes.append({
            "node_id": node_id,
            "connected": True,
            "type": state.devices.get(node_id, {}).get("device_type", "unknown"),
            "capabilities": state.devices.get(node_id, {}).get("capabilities", []),
        })
    for dev_id, dev_info in state.devices.items():
        if dev_id not in [n["node_id"] for n in nodes]:
            nodes.append({
                "node_id": dev_id,
                "connected": dev_id in state.daemons,
                "type": dev_info.get("device_type", "unknown"),
                "capabilities": dev_info.get("capabilities", []),
            })
    return {"devices": nodes, "total": len(nodes)}


# ─────────────────────────────────────────────
# Federated Sync API
# ─────────────────────────────────────────────


@router.get("/api/sync/status")
async def sync_status():
    """Engine + per-peer health snapshot. v2026.5.34 (PR 2 D12)
    embeds the SyncScheduler's per-peer status alongside the legacy
    engine stats so the dashboard / CLI can render lag, backoff,
    and op counters without two round-trips.
    """
    if not state.sync_engine:
        return {"enabled": False}
    body = {
        "enabled": True,
        "node_id": state.sync_engine.node_id,
        **state.sync_engine.stats,
    }
    scheduler = getattr(state, "sync_scheduler", None)
    if scheduler is not None:
        body["scheduler"] = {
            "enabled": scheduler.config.enabled,
            "cadence_seconds": scheduler.config.cadence_seconds,
            "peers": scheduler.peer_status(),
        }
    # Identity posture. Reported on the status endpoint rather than
    # buried in the roster route so an operator who checks "is sync
    # healthy" cannot come away believing peers are
    # identity-authenticated while any of them is still presenting the
    # shared passphrase. ``shared_secret_peers`` is the list that has to
    # reach zero before ``per_peer`` is honest.
    roster = _roster()
    if roster is not None:
        stragglers = roster.shared_secret_peers()
        body["identity_mode"] = identity_mode(roster)
        body["enrolled_peers"] = len(
            [p for p in roster.list_peers() if p["status"] == "active"]
        )
        body["shared_secret_peers"] = stragglers
        body["identity_note"] = _IDENTITY_NOTES[body["identity_mode"]]
    return body


def _roster():
    """The peer roster, or ``None`` when it cannot be reached.

    Status must still render on a brain whose roster DB is unwritable;
    what degrades is the identity report, not the whole endpoint.
    """
    roster = getattr(state, "peer_roster", None)
    if roster is not None:
        return roster
    try:
        return get_peer_roster()
    except Exception:  # noqa: BLE001
        return None


_IDENTITY_NOTES = {
    "shared_passphrase": (
        "Every peer authenticates with one shared passphrase. No peer has "
        "its own identity yet. Run `feral sync peer invite <name>` to start."
    ),
    "mixed": (
        "Some peers are enrolled, but the shared passphrase is STILL accepted, "
        "so peers are not identity-authenticated. Enrol every brain in "
        "shared_secret_peers, then set FERAL_SYNC_REQUIRE_PEER_IDENTITY=1."
    ),
    "per_peer": (
        "The shared passphrase is refused. Every peer presents its own grant, "
        "bound to its node_id, in a window that lapses if it stops syncing."
    ),
}


@router.get("/api/sync/roster")
async def sync_roster_list():
    """Per-peer identity roster: who may sync with this brain."""
    roster = _roster()
    if roster is None:
        return {"ok": False, "error": "peer roster unavailable", "peers": []}
    return {
        "ok": True,
        "identity_mode": identity_mode(roster),
        "peers": roster.list_peers(),
        "shared_secret_peers": roster.shared_secret_peers(),
        "outbound_grants": [
            {"label": label, "address": rec.get("address", ""), "name": rec.get("name", "")}
            for label, rec in load_outbound_grants().items()
        ],
    }


@router.post("/api/sync/roster/invite")
async def sync_roster_invite(body: dict):
    """Mint a grant for one peer brain.

    The plaintext grant is in THIS response and nowhere else, ever. The
    operator carries it to the other brain, which posts it to
    ``/api/sync/roster/accept``.
    """
    roster = _roster()
    if roster is None:
        return {"ok": False, "error": "peer roster unavailable"}
    name = (body or {}).get("name", "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    kwargs = {}
    for key in ("ttl_seconds", "invite_ttl_seconds"):
        if (body or {}).get(key):
            kwargs[key] = int(body[key])
    try:
        return {"ok": True, **roster.invite_peer(name, **kwargs)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/api/sync/roster/accept")
async def sync_roster_accept(body: dict):
    """Store a grant another brain issued us, so we present it when we
    dial that peer."""
    payload = body or {}
    label = (payload.get("label") or payload.get("address") or "").strip()
    secret = (payload.get("secret") or payload.get("grant") or "").strip()
    if not label or not secret:
        return {"ok": False, "error": "label (host:port or node_id) and secret required"}
    try:
        stored = store_outbound_grant(
            label,
            secret,
            address=payload.get("address", "") or label,
            name=payload.get("name", ""),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **stored}


@router.delete("/api/sync/roster/{peer_row_id}")
async def sync_roster_revoke(peer_row_id: str):
    """Revoke a peer's grant.

    Stops future exchanges. Does NOT recall memory the peer already
    holds: replicated data cannot be un-sent, and the response says so
    rather than letting "revoke" imply otherwise.
    """
    roster = _roster()
    if roster is None:
        return {"ok": False, "error": "peer roster unavailable"}
    revoked = roster.revoke_peer(peer_row_id)
    return {
        "ok": revoked,
        "peer_row_id": peer_row_id,
        "note": (
            "Future exchanges refused. Memory already replicated to that peer "
            "stays on its disk; revocation is not recall."
        ),
    }


@router.post("/api/sync/now")
async def sync_now(body: dict | None = None):
    """Trigger an immediate sync. With ``{"peer": "<peer_id>"}`` syncs
    one peer; without a body syncs every known peer.
    """
    scheduler = getattr(state, "sync_scheduler", None)
    if scheduler is None:
        return {"ok": False, "error": "sync_scheduler not running"}
    peer = (body or {}).get("peer")
    if peer:
        return await scheduler.sync_one_peer_now(peer)
    return {"ok": True, "results": await scheduler.sync_all_peers_now()}


@router.get("/api/sync/peers")
async def sync_peers_list():
    """Enumerate every known peer (mDNS-discovered + manually added)."""
    scheduler = getattr(state, "sync_scheduler", None)
    if scheduler is None:
        return {"ok": False, "error": "sync_scheduler not running", "peers": []}
    return {"ok": True, "peers": scheduler.list_peers()}


@router.post("/api/sync/peers")
async def sync_peers_add(body: dict):
    """Add a peer by ``host:port``. Persists for the lifetime of the
    process; restart reinjects manual peers via FERAL_SYNC_PEERS."""
    scheduler = getattr(state, "sync_scheduler", None)
    if scheduler is None:
        return {"ok": False, "error": "sync_scheduler not running"}
    addr = (body or {}).get("address", "").strip()
    if not addr:
        return {"ok": False, "error": "address required"}
    return scheduler.add_peer(addr)


@router.delete("/api/sync/peers/{peer_id}")
async def sync_peers_remove(peer_id: str):
    scheduler = getattr(state, "sync_scheduler", None)
    if scheduler is None:
        return {"ok": False, "error": "sync_scheduler not running"}
    return scheduler.remove_peer(peer_id)


@router.get("/api/sync/node-id")
async def sync_node_id():
    """Return the persistent HLC node id. Surfaced for backups +
    duplicate-id triage (operator runs this on every brain and
    confirms the values differ before troubleshooting sync drift)."""
    engine = state.sync_engine
    return {
        "node_id": engine.node_id if engine else "",
        "note": "Persisted at ~/.feral/sync_node_id. Rotate by deleting the file and restarting.",
    }


@router.get("/api/sync/export")
async def sync_export():
    """Export memory bundle for manual federated sync."""
    if not state.sync_engine:
        return {"error": "Sync engine not running"}
    return state.sync_engine.export_to_bundle()


@router.post("/api/sync/import")
async def sync_import(body: dict):
    """Import a memory bundle from another node."""
    if not state.sync_engine:
        return {"error": "Sync engine not running"}
    applied = await state.sync_engine.import_from_bundle(body)
    return {"applied": applied}
