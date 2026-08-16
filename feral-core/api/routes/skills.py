"""Skill generation, approval, listing, and API-key endpoints."""

import logging

from fastapi import APIRouter, Response

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


# 409 Conflict for every "the reload did not happen" outcome: the request
# was well formed and the route exists, the brain's own state is what makes
# it impossible (no source on disk for that id, or a source it cannot
# parse). The machine-readable difference stays in the ``code`` field.
#
# Not 404: the v2 client maps 404 to "this brain build does not serve that
# route, your client and brain versions disagree" and drops the body's
# reason on the floor (ui/ErrorState.jsx describeApiError), which would
# send the operator to check versions over a skill that simply has no file.
# Not 422 either: FastAPI already answers 422 for its own request
# validation, so a hand-set 422 here would be indistinguishable from a
# malformed ``skill_id``.
_RELOAD_CONFLICT = 409


@router.post("/api/skills/reload")
async def reload_skill(skill_id: str, response: Response):
    """Hot-reload a skill manifest + impl from disk.

    Thin wrapper around ``state.skill_registry.reload_skill_detail`` used
    by ``feral install`` after extracting a new skill bundle, and by the
    Skills page's per-skill Hot-reload button.

    A failed reload answers with a 4xx/5xx AND an ``error`` string. It
    used to answer ``{"ok": false, "skill_id": ...}`` with HTTP 200 and no
    ``error`` key, which is the one shape the client cannot see: the v2
    ``apiFetch`` only raises on a non-2xx status or on a 2xx body carrying
    ``error``, so ``Skills.jsx`` fell through to its success branch and
    rendered "Hot-reloaded <id>" for a reload that had done nothing at
    all. Reporting success without checking the result is the defect class
    this repo has spent two releases removing, and it survived inside the
    change that was written to close it: the commit added the error state,
    the retry affordance and the "Whatever code the brain had loaded
    before is still what is running" hint, then awaited the response
    without ever reading it. An unread body is an unchecked result.

    A success status for a failed operation does not merely mislead one
    caller, it disables every generic caller at once, which is why this
    stayed invisible for two releases while looking well handled.
    """
    registry = getattr(state, "skill_registry", None)
    if not registry:
        response.status_code = 503
        return {"ok": False, "skill_id": skill_id, "error": "skill registry not initialized"}

    try:
        ok, code, reason = _reload(registry, skill_id)
    except Exception as exc:
        logger.warning("reload_skill(%s) raised: %s", skill_id, exc)
        response.status_code = 500
        return {"ok": False, "skill_id": skill_id, "error": str(exc)}

    if not ok:
        response.status_code = _RELOAD_CONFLICT
        return {
            "ok": False,
            "skill_id": skill_id,
            "code": code,
            "error": reason or f"reload of '{skill_id}' did not happen",
        }
    return {"ok": True, "skill_id": skill_id}


def _reload(registry, skill_id: str) -> tuple[bool, str, str]:
    """``(ok, code, reason)`` from whichever reload API the registry has.

    ``reload_skill_detail`` is the one that can say why. The boolean
    ``reload_skill`` is kept as a fallback so a registry double, or an
    older bundled copy of ``skills/registry.py`` under ``desktop/``, still
    works; it just cannot name a reason, and we do not invent one.
    """
    detail = getattr(registry, "reload_skill_detail", None)
    if callable(detail):
        result = detail(skill_id)
        if isinstance(result, tuple) and len(result) == 3:
            return bool(result[0]), str(result[1]), str(result[2])
    ok = bool(registry.reload_skill(skill_id))
    if ok:
        return True, "", ""
    return False, "", f"the skill registry did not reload '{skill_id}' and gave no reason"


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
