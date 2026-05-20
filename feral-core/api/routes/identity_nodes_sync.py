"""Identity (SOUL/MEMORY), node/device listing, and federated sync HTTP endpoints."""

from fastapi import APIRouter

from api.state import state

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
    return body


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
