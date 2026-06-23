"""Security vault/permissions/audit, HUP hardware API, sandbox policy, hardware mesh REST."""

import json

from fastapi import APIRouter

from api.state import state
from config.loader import feral_home
from hardware.protocol import HUPAction, HUPActionType
from security.sandbox_policy import SandboxPolicy
from security.vault import PermissionTier

router = APIRouter()


# ─────────────────────────────────────────────
# Security API
# ─────────────────────────────────────────────


@router.get("/api/security/vault")
async def vault_summary():
    """Key names + fingerprints — never raw values."""
    if not state.vault:
        return {"keys": {}}
    return {"keys": state.vault.to_safe_summary()}


@router.post("/api/security/vault/store")
async def vault_store(body: dict):
    """Store a credential in the blind vault."""
    name = body.get("key_name", "")
    value = body.get("value", "")
    if not name or not value:
        return {"error": "key_name and value are required"}
    state.vault.store(name, value, stored_by="api")
    return {"ok": True, "key_name": name, "fingerprint": state.vault.fingerprint(name)}


@router.delete("/api/security/vault/{key_name}")
async def vault_remove(key_name: str):
    removed = state.vault.remove(key_name, removed_by="api")
    return {"ok": removed}


@router.get("/api/security/permissions")
async def get_permissions():
    """Current permission tier and sandbox status."""
    return {
        "max_tier": state.sandbox.max_tier if state.sandbox else "active",
        "tiers": PermissionTier.TIER_ORDER,
        "tier_descriptions": {
            "passive": "Read-only, no side effects (weather, search)",
            "active": "Can send data (messaging, calendar)",
            "privileged": "Can modify system state (file access)",
            "dangerous": "Destructive operations (delete, financial)",
        },
    }


@router.post("/api/security/permissions/update")
async def update_permissions(body: dict):
    new_tier = body.get("max_tier", "active")
    if new_tier not in PermissionTier.TIER_ORDER:
        return {"error": f"Invalid tier: {new_tier}"}
    if state.sandbox:
        state.sandbox.max_tier = new_tier
    return {"ok": True, "max_tier": new_tier}


@router.get("/api/security/audit")
async def get_audit_log():
    """Get recent security audit entries.

    audit-r12 A5 (v2026.5.38) — delegates to
    :func:`security.audit_log.recent_events` so the dashboard
    surfaces "audit log unreadable" (HTTP 500 with a structured
    error) instead of pretending the log is empty when the file is
    present but unreadable / malformed.
    """
    from security.audit_log import recent_events, AuditFailure

    try:
        entries = recent_events(limit=100)
    except AuditFailure as exc:
        return {"error": "audit_log_unreadable", "detail": str(exc), "entries": []}
    return {"entries": entries}


# ─────────────────────────────────────────────
# Hardware Use Protocol (HUP) API
# ─────────────────────────────────────────────


@router.get("/api/hardware/devices")
async def list_hardware_devices():
    """List all registered hardware devices."""
    if not state.device_registry:
        return {"devices": []}
    devices = state.device_registry.list_devices()
    return {"devices": [d.model_dump() for d in devices]}


@router.get("/api/hardware/device/{device_id}")
async def get_hardware_device(device_id: str):
    if not state.device_registry:
        return {"error": "No device registry"}
    device = state.device_registry.get_device(device_id)
    if not device:
        return {"error": f"Device not found: {device_id}"}
    return device.model_dump()


@router.post("/api/hardware/execute")
async def execute_hardware_action(body: dict):
    """Execute a HUP action on a device."""
    if not state.device_registry:
        return {"error": "No device registry"}
    action = HUPAction(
        device_id=body.get("device_id", ""),
        capability_id=body.get("capability_id", ""),
        action_type=HUPActionType(body.get("action_type", "execute")),
        parameters=body.get("parameters", {}),
        timeout_ms=body.get("timeout_ms", 5000),
        confirmed=bool(body.get("confirmed", False)),
    )

    # The sensor allowlist only applies to read capabilities; actuator
    # capabilities (halt, drive, ...) are governed by the device registry's
    # permission tiers, not the sensor policy.
    if (
        state.policy
        and action.capability_id.startswith("read_")
        and not state.policy.can_read_sensor(action.capability_id.replace("read_", ""))
    ):
        return {"error": "Blocked by sandbox policy"}

    result = await state.device_registry.execute_action(action)
    return result.model_dump()


@router.get("/api/hardware/context")
async def hardware_llm_context():
    """Get hardware context string for LLM."""
    if not state.device_registry:
        return {"context": "No hardware devices connected."}
    return {"context": state.device_registry.to_llm_context()}


@router.get("/api/hardware/stats")
async def hardware_stats():
    if not state.device_registry:
        return {}
    return state.device_registry.stats


# ─────────────────────────────────────────────
# Sandbox Policy API
# ─────────────────────────────────────────────


@router.get("/api/policy")
async def get_policy():
    if not state.policy:
        return {}
    return state.policy.to_dict()


@router.post("/api/policy/update")
async def update_policy(body: dict):
    state.policy = SandboxPolicy(body)
    state.policy.save()
    return {"ok": True}


# ─────────────────────────────────────────────
# Workspace Folder Grants
# ─────────────────────────────────────────────
#
# Computer-use file tools refuse paths outside the policy's read/write
# lists. Operators grant explicit folders here (Desktop, Documents,
# project dirs) without globally widening the home directory.
# Persisted by ``SandboxPolicy.grant_folder`` to ``workspace_grants.json``.


def _grants_policy() -> SandboxPolicy:
    """Return the policy that owns folder grants. Falls back to a
    freshly-loaded one if the brain hasn't booted yet (CLI-style use)."""
    if state.policy is not None:
        return state.policy
    return SandboxPolicy.load_default()


@router.get("/api/security/grants")
async def list_workspace_grants():
    """List every folder grant currently honoured by the policy."""
    grants = _grants_policy().list_grants()
    return {"grants": grants}


@router.post("/api/security/grants")
async def grant_workspace_folder(body: dict):
    """Grant read/readwrite access to a folder.

    Body: ``{"path": "/Users/me/Desktop", "mode": "readwrite"}``.
    Mode defaults to ``read``; ``readwrite`` (or legacy ``write``) is
    required for ``computer_use__write_file`` / ``edit_file`` to succeed
    inside the folder.
    """
    path = (body or {}).get("path") or ""
    raw_mode = (body or {}).get("mode") or "read"
    mode = raw_mode.lower().strip()
    if mode not in ("read", "readwrite", "write"):
        return {"ok": False, "error": f"invalid mode: {raw_mode}"}
    if not path:
        return {"ok": False, "error": "path is required"}
    return _grants_policy().grant_folder(path, mode=mode)


@router.delete("/api/security/grants")
async def revoke_workspace_folder(path: str):
    """Revoke access to a previously granted folder by absolute path."""
    if not path:
        return {"ok": False, "error": "path is required"}
    removed = _grants_policy().revoke_folder(path)
    return {"ok": removed, "path": path}


# ─────────────────────────────────────────────
# Hardware Mesh API
# ─────────────────────────────────────────────


@router.post("/api/hardware/invoke")
async def hardware_invoke(body: dict):
    """Invoke a command on a connected node via the hardware mesh."""
    if not state.hardware_mesh:
        return {"error": "Hardware mesh not initialized"}
    return await state.hardware_mesh.invoke(
        node_id=body.get("node_id", ""),
        command=body.get("command", ""),
        params=body.get("params", {}),
        timeout=body.get("timeout", 10.0),
    )


@router.get("/api/hardware/mock_roomba")
async def mock_roomba_status():
    """Status of the brain-side mock Roomba (THESIS_SCENARIOS S5).

    Returns the mock's ``entity_id`` + ``is_running`` so the Lane 12
    Devices page can render the demo node. Returns
    ``{enabled: False}`` when the operator disabled the mock with
    ``FERAL_MOCK_ROOMBA=0``.
    """
    mock = getattr(state, "mock_roomba", None)
    if mock is None:
        return {"enabled": False}
    return mock.status()


@router.post("/api/hardware/mock_roomba/start")
async def mock_roomba_start(body: dict | None = None):
    """Start the brain-side mock Roomba.

    Body: ``{"entity_id": "vacuum.mock_roomba"}`` — optional; defaults
    to the mock's configured id. Returns the same structured shape as
    Lane 10's ``HomeAssistantIntegration.vacuum_start`` so the
    orchestrator's tool dispatch path can use either backend
    interchangeably:

    ``{success: True, data: {started: True, entity_id, service:
    "vacuum.start", duration_ms}}``

    Lane 11 SLA: < 500 ms on commodity hardware. Live verify in PR
    body asserts the elapsed time fits inside this budget.
    """
    mock = getattr(state, "mock_roomba", None)
    if mock is None:
        return {"success": False, "error": "mock_roomba disabled", "reason": "disabled"}
    entity_id = ""
    if isinstance(body, dict):
        entity_id = str(body.get("entity_id") or "")
    return await mock.start(entity_id=entity_id)


@router.post("/api/hardware/mock_roomba/stop")
async def mock_roomba_stop(body: dict | None = None):
    """Stop the brain-side mock Roomba. Parity with HA vacuum.stop."""
    mock = getattr(state, "mock_roomba", None)
    if mock is None:
        return {"success": False, "error": "mock_roomba disabled", "reason": "disabled"}
    entity_id = ""
    if isinstance(body, dict):
        entity_id = str(body.get("entity_id") or "")
    return await mock.stop(entity_id=entity_id)


@router.get("/api/hardware/mesh")
async def hardware_mesh_status():
    """Get hardware mesh status — connected daemons + discovered peripherals.

    Lane 12's Devices page consumes both fields:

    * ``nodes`` — daemons that registered over /v1/node and are still
      attached. Lane 12 lists them as the operator-actionable surface
      (rename / disconnect).
    * ``announced_devices`` — peripherals observed by any scanner-class
      daemon (typically the iOS companion app). Each entry carries
      ``device_id``, ``device_kind``, ``name``, ``manufacturer``,
      ``rssi_dbm``, ``advertised_services``, ``first_seen``,
      ``last_seen``, ``scanner_node_id``, ``metadata``. Lane 12 renders
      these grouped under their scanner so the operator sees "iPhone
      → AirPods, Apple Watch, …" without us inventing a separate
      grouping API.

    Both fields are always present (possibly empty) so the Lane 12
    client can do a single fetch.
    """
    if not state.hardware_mesh:
        return {"nodes": [], "announced_devices": []}
    return {
        "nodes": state.hardware_mesh.connected_nodes,
        "announced_devices": state.hardware_mesh.list_announced_devices(),
    }


def _serialize_capability(cap) -> dict:
    """JSON-safe capability + the generically-derived safety tier.

    The ``safety_tier`` / ``read_only`` / ``requires_approval`` come from the
    SAME generic resolver the LLM tool layer uses, so the companion's device
    card badges match exactly what the brain enforces."""
    from hardware.capability_skill import _safety_for

    try:
        tier, read_only, requires_approval = _safety_for(cap)
    except Exception:
        tier, read_only, requires_approval = "safe", False, False
    return {
        "id": cap.id,
        "name": cap.name,
        "description": cap.description,
        "category": cap.category,
        "permission_tier": cap.permission_tier,
        "action_type": getattr(cap, "action_type", None),
        "safety_tier": tier,
        "read_only": read_only,
        "requires_approval": requires_approval,
        "requires_confirmation": cap.requires_confirmation,
        "reversible": cap.reversible,
        "rate_limit_per_minute": cap.rate_limit_per_minute,
        "params": list(cap.parameters or []),
        "returns": cap.returns,
        "has_verify": isinstance(getattr(cap, "verify", None), dict),
    }


def _serialize_fleet_device(manifest, verification) -> dict:
    return {
        "device_id": manifest.device_id,
        "device_type": manifest.device_type,
        "name": manifest.name,
        "manufacturer": manifest.manufacturer,
        "model": manifest.model,
        "connection_type": manifest.connection_type,
        "location": manifest.location,
        "battery_powered": manifest.battery_powered,
        "tags": list(manifest.tags or []),
        "sensors": list(manifest.sensors or []),
        "actuators": list(manifest.actuators or []),
        "capabilities": [_serialize_capability(c) for c in manifest.capabilities],
        "last_verified": verification,
    }


@router.get("/api/hardware/fleet")
async def hardware_fleet():
    """Unified, server-driven fleet view for the companion app.

    One fetch returns every self-describing device the brain knows about —
    its full manifest (capabilities, params, generically-derived safety
    tiers, verify contracts), plus each device's last action+verify outcome
    (the live "honesty loop" state), mesh-announced peripherals, and connected
    nodes. The companion renders device cards directly from this; a newly
    registered device appears with no app change.
    """
    registry = state.device_registry
    if registry is None:
        return {
            "devices": [],
            "verifications": {},
            "mesh": {"nodes": [], "announced_devices": []},
            "stats": {},
            "primary_session_id": getattr(state, "primary_session_id", None),
        }

    verifications = {}
    try:
        verifications = registry.all_verifications()
    except Exception:
        verifications = {}

    devices = []
    for manifest in registry.list_devices():
        devices.append(
            _serialize_fleet_device(manifest, verifications.get(manifest.device_id))
        )

    mesh = {"nodes": [], "announced_devices": []}
    if state.hardware_mesh:
        try:
            mesh = {
                "nodes": state.hardware_mesh.connected_nodes,
                "announced_devices": state.hardware_mesh.list_announced_devices(),
            }
        except Exception:
            pass

    stats = {}
    try:
        stats = registry.stats
    except Exception:
        stats = {}

    return {
        "devices": devices,
        "verifications": verifications,
        "mesh": mesh,
        "stats": stats,
        "primary_session_id": getattr(state, "primary_session_id", None),
    }
