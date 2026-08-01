"""Skill generation, approval, listing, and API-key endpoints."""

import logging

from fastapi import APIRouter

from api.state import state

logger = logging.getLogger("feral.api.skills")

router = APIRouter()


@router.post("/api/skills/generate")
async def generate_skill(body: dict):
    """Generate a new skill from a capability description."""
    capability = body.get("capability", "")
    service = body.get("service", "")
    if not capability:
        return {"error": "capability is required"}
    if not state.skill_gen:
        return {"error": "Skill generator not initialized"}
    manifest = await state.skill_gen.generate_skill(capability, service)
    if manifest:
        return {"ok": True, "manifest": manifest, "needs_approval": True}
    return {"ok": False, "error": "Failed to generate skill"}


@router.post("/api/skills/approve")
async def approve_skill(body: dict):
    """Approve a pending generated skill — registers it live."""
    skill_id = body.get("skill_id", "")
    if not skill_id:
        return {"error": "skill_id is required"}
    success = await state.skill_gen.approve_skill(skill_id)
    return {"ok": success, "skill_id": skill_id, "registered": success}


@router.post("/api/skills/reject")
async def reject_skill(body: dict):
    """Reject a pending generated skill."""
    skill_id = body.get("skill_id", "")
    state.skill_gen.reject_skill(skill_id)
    return {"ok": True, "skill_id": skill_id}


@router.get("/api/skills/pending")
async def pending_skills():
    """Get all skills waiting for user approval."""
    if not state.skill_gen:
        return {"pending": []}
    return {"pending": state.skill_gen.get_pending_skills()}


@router.post("/api/skills/reload")
async def reload_skill(skill_id: str):
    """Hot-reload a skill manifest + impl from disk.

    Thin wrapper around ``state.skill_registry.reload_skill`` used by
    ``feral install`` after extracting a new skill bundle.
    """
    if not state.skill_registry:
        return {"ok": False, "error": "skill registry not initialized"}
    try:
        ok = state.skill_registry.reload_skill(skill_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": bool(ok), "skill_id": skill_id}


@router.get("/skills")
async def list_skills():
    return [
        {
            "skill_id": s.skill_id,
            "name": s.brand.name,
            "description": s.description,
            "endpoints": len(s.endpoints),
            "trigger_phrases": s.trigger_phrases,
        }
        for s in state.skill_registry.skills.values()
    ]


# ─────────────────────────────────────────────
# Skill API keys
# ─────────────────────────────────────────────
#
# The default (v2) UI had no way to give a skill its API key: the only
# surface was the v1 Settings page POSTing ``{skill_keys: {...}}`` to
# /api/config/credentials, and that write landed in
# ``ConfigLoader._credentials`` where nothing on the execution path read
# it. The documented-nowhere workaround was ``FERAL_KEY_<SKILL_ID>`` plus
# a brain restart.
#
# These three routes are the round trip: write goes to the SkillExecutor
# (encrypted vault + live in-process cache), and the read-back reports
# which skills are configured without ever echoing a secret.


def _key_auth_skills() -> list:
    """Registered manifests whose auth type needs an operator-supplied key."""
    registry = getattr(state, "skill_registry", None)
    if registry is None:
        return []
    out = []
    for manifest in getattr(registry, "skills", {}).values():
        auth = getattr(manifest, "auth", None)
        if getattr(auth, "type", "none") in ("api_key", "bearer"):
            out.append(manifest)
    return out


@router.get("/api/skills/keys")
async def list_skill_keys():
    """Which skills need an API key, and which already have one.

    Never returns key material. ``needs_key`` lists every registered
    manifest declaring ``api_key`` / ``bearer`` auth so the UI can render
    a row per skill instead of asking the operator to know skill ids.
    """
    executor = getattr(state, "skill_executor", None)
    if executor is None:
        return {"ok": False, "error": "skill executor not initialized",
                "needs_key": [], "configured": []}
    configured = set(executor.key_ids())
    needs_key = [
        {
            "skill_id": m.skill_id,
            "name": getattr(getattr(m, "brand", None), "name", "") or m.skill_id,
            "auth_type": m.auth.type,
            "has_key": m.skill_id in configured or executor.has_key(m.skill_id),
        }
        for m in _key_auth_skills()
    ]
    return {
        "ok": True,
        "needs_key": sorted(needs_key, key=lambda r: r["skill_id"]),
        "configured": sorted(configured),
    }


@router.post("/api/skills/{skill_id}/key")
async def set_skill_key(skill_id: str, body: dict):
    """Store a skill's API key so the executor uses it on the next call.

    Body: ``{"key": "..."}``. The value is written to the encrypted
    vault's ``skill_keys`` namespace (survives a restart) and to the
    executor's in-process cache (no restart needed).

    ``persisted`` is False when the brain is running without a vault: the
    key still works for this process, and saying so is the point, a
    silent "ok" would send the operator away believing it was durable.
    """
    executor = getattr(state, "skill_executor", None)
    if executor is None:
        return {"ok": False, "error": "skill executor not initialized"}
    key = (body or {}).get("key") or (body or {}).get("api_key") or ""
    try:
        persisted = executor.store_key(skill_id, key)
    except ValueError as exc:
        return {"ok": False, "skill_id": skill_id, "error": str(exc)}
    except Exception as exc:  # pragma: no cover, defensive
        logger.warning("set_skill_key(%s) failed: %s", skill_id, exc)
        return {"ok": False, "skill_id": skill_id, "error": str(exc)}
    return {
        "ok": True,
        "skill_id": skill_id,
        "persisted": persisted,
        "has_key": executor.has_key(skill_id),
    }


@router.delete("/api/skills/{skill_id}/key")
async def delete_skill_key(skill_id: str):
    """Forget a skill's API key (vault + in-process cache)."""
    executor = getattr(state, "skill_executor", None)
    if executor is None:
        return {"ok": False, "error": "skill executor not initialized"}
    removed = executor.remove_key(skill_id)
    return {
        "ok": True,
        "skill_id": skill_id,
        "removed": bool(removed),
        "has_key": executor.has_key(skill_id),
    }
