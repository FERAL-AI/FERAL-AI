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
    )

    if state.policy and not state.policy.can_read_sensor(action.capability_id.replace("read_", "")):
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


@router.get("/api/hardware/mesh")
async def hardware_mesh_status():
    """Get hardware mesh status with all connected nodes."""
    if not state.hardware_mesh:
        return {"nodes": []}
    return {"nodes": state.hardware_mesh.connected_nodes}
