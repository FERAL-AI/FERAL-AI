"""Skill generation, approval, listing, and API-key endpoints."""

import logging

from fastapi import APIRouter, Response

from api.state import state

logger = logging.getLogger("feral.api.skills")

router = APIRouter()


# ── how this file reports a failure ──────────────────────────────
#
# One rule, applied to every route here, because a success status for a
# failed operation does not merely mislead one caller, it disables every
# generic caller at once. The v2 ``apiFetch`` raises on a non-2xx status,
# or on a 2xx body carrying an ``error`` key; a 200 with neither is the
# one shape no generic client can see, and it is the shape three routes
# in this file used to send.
#
# 400  the request itself is wrong (a required field is missing). The
#      caller can fix it without touching the brain.
# 409  the request is fine and the route exists, but the brain's own
#      state makes it impossible: no source on disk for that skill id, or
#      no draft with that id in the approval queue. The machine-readable
#      cause stays in ``code``.
# 500  the brain accepted the work and then failed at it (an exception
#      mid-flight, or a draft that was popped off the queue and did not
#      survive registration). The reason for that one is in the brain
#      log and nowhere else, and 500 is what sends an operator there.
# 503  the subsystem the route needs was never initialized. Nothing is
#      wrong with the request and retrying it changes nothing until the
#      brain is started with that subsystem.
#
# Not 404 for any of them: the v2 client maps 404 to "this brain build
# does not serve that route, your client and brain versions disagree" and
# drops the body's reason on the floor (ui/ErrorState.jsx
# describeApiError), which would send the operator to check versions over
# a skill that simply has no file. Not 422 either: FastAPI already
# answers 422 for its own request validation, so a hand-set 422 here
# would be indistinguishable from a malformed ``skill_id``.
_BAD_REQUEST = 400
_STATE_CONFLICT = 409
_BRAIN_FAILED = 500
_SUBSYSTEM_MISSING = 503


def _pending_ids() -> set[str] | None:
    """Skill ids currently in the approval queue.

    ``None`` means the queue could not be read at all, which is a
    different thing from an empty queue and must not be flattened into
    one: an empty queue proves an id was never pending, an unreadable
    queue proves nothing.
    """
    getter = getattr(getattr(state, "skill_gen", None), "get_pending_skills", None)
    if not callable(getter):
        return None
    try:
        rows = getter()
    except Exception as exc:  # pragma: no cover, defensive
        logger.warning("could not read the pending skill queue: %s", exc)
        return None
    if not isinstance(rows, list):
        return None
    ids = set()
    for row in rows:
        if isinstance(row, dict):
            sid = row.get("skill_id") or row.get("id")
            if sid:
                ids.add(str(sid))
    return ids


@router.post("/api/skills/generate")
async def generate_skill(body: dict, response: Response):
    """Generate a new skill from a capability description."""
    capability = body.get("capability", "")
    service = body.get("service", "")
    if not capability:
        response.status_code = _BAD_REQUEST
        return {"ok": False, "error": "capability is required"}
    if not state.skill_gen:
        response.status_code = _SUBSYSTEM_MISSING
        return {"ok": False, "error": "skill generator not initialized"}
    manifest = await state.skill_gen.generate_skill(capability, service)
    if manifest:
        return {"ok": True, "manifest": manifest, "needs_approval": True}
    response.status_code = _BRAIN_FAILED
    return {
        "ok": False,
        "code": "generation_failed",
        "error": f"the brain could not draft a skill for '{capability}'. See the brain log for what the generator returned.",
    }


@router.post("/api/skills/approve")
async def approve_skill(body: dict, response: Response):
    """Approve a pending generated skill, registering it live.

    It used to answer ``{"ok": false, "skill_id": ..., "registered":
    false}`` with HTTP 200 and no ``error`` key when
    ``SkillGenerator.approve_skill`` returned False, which is the same
    invisible-failure shape ``/api/skills/reload`` shipped for two
    releases: its client, ``pages/Forge.jsx``, awaited the call, never
    read the body, and treated "did not throw" as "approved". A draft
    that was never registered was rendered as promoted.

    ``approve_skill`` returns False for two very different reasons and
    they get two different statuses, because the operator's next move
    differs:

    * the id is not in the approval queue (a stale Forge tab, or a
      restart, since the queue is in-process only) -> 409, the queue
      the operator is looking at disagrees with the brain's;
    * the id was in the queue and registration or the write to
      ``~/.feral/skills/<id>/`` failed -> 500. ``approve_skill`` pops
      the draft before it registers it and logs the exception on the way
      out, so the draft is gone and the reason exists only in the brain
      log. 500 is what sends an operator to a log.

    When the queue cannot be read we cannot tell the two apart, so we do
    not assert either: 409 with ``code: approve_failed`` and a reason
    that names both possibilities.
    """
    skill_id = body.get("skill_id", "")
    if not skill_id:
        response.status_code = _BAD_REQUEST
        return {"ok": False, "registered": False, "error": "skill_id is required"}

    gen = getattr(state, "skill_gen", None)
    if not gen:
        response.status_code = _SUBSYSTEM_MISSING
        return {
            "ok": False,
            "skill_id": skill_id,
            "registered": False,
            "error": "skill generator not initialized, so nothing can be approved",
        }

    was_pending = _pending_ids()
    try:
        success = bool(await gen.approve_skill(skill_id))
    except Exception as exc:
        logger.warning("approve_skill(%s) raised: %s", skill_id, exc)
        response.status_code = _BRAIN_FAILED
        return {
            "ok": False,
            "skill_id": skill_id,
            "registered": False,
            "code": "approve_raised",
            "error": str(exc),
        }

    if success:
        return {"ok": True, "skill_id": skill_id, "registered": True}

    if was_pending is None:
        response.status_code = _STATE_CONFLICT
        code = "approve_failed"
        reason = (
            f"the brain did not register '{skill_id}'. It was either not in the approval "
            "queue, or it failed on the way to disk; the pending queue could not be read "
            "to tell which. The brain log has the answer."
        )
    elif skill_id not in was_pending:
        response.status_code = _STATE_CONFLICT
        code = "not_pending"
        reason = (
            f"'{skill_id}' is not in the approval queue, so there was nothing to approve. "
            "The queue lives in memory only and is emptied by a brain restart."
        )
    else:
        response.status_code = _BRAIN_FAILED
        code = "registration_failed"
        reason = (
            f"'{skill_id}' was in the approval queue and the brain failed to register or "
            "persist it. The draft has been dropped from the queue; see the brain log."
        )
    return {
        "ok": False,
        "skill_id": skill_id,
        "registered": False,
        "code": code,
        "error": reason,
    }


@router.post("/api/skills/reject")
async def reject_skill(body: dict, response: Response):
    """Reject a pending generated skill.

    It used to return an unconditional ``{"ok": true}`` without looking
    at what ``SkillGenerator.reject_skill`` returned, so rejecting an id
    the brain had never heard of reported success for a no-op.

    **Rejecting an unknown id is an error here, not an idempotent
    no-op.** Both are defensible for a delete-shaped operation, and the
    idempotent reading is the more usual one, but it is wrong for this
    queue: it lives in ``SkillGenerator._pending_skills``, in memory, and
    a brain restart empties it. "Reject" therefore has one common failure
    mode, a Forge tab left open across a restart, and under the
    idempotent reading that tab answers every click with a green tick
    while the drafts the operator is trying to discard are still queued
    somewhere else, or were already approved by another surface. An
    operator rejecting a draft is acting on a list they can see; if the
    brain's list disagrees with it, that is the single most useful thing
    we can tell them. 409 with ``code: not_pending``, same status and
    reasoning as a reload with no source, and the success path is
    unchanged.
    """
    skill_id = body.get("skill_id", "")
    if not skill_id:
        response.status_code = _BAD_REQUEST
        return {"ok": False, "rejected": False, "error": "skill_id is required"}

    gen = getattr(state, "skill_gen", None)
    if not gen:
        response.status_code = _SUBSYSTEM_MISSING
        return {
            "ok": False,
            "skill_id": skill_id,
            "rejected": False,
            "error": "skill generator not initialized, so nothing can be rejected",
        }

    before = _pending_ids()
    try:
        returned = gen.reject_skill(skill_id)
    except Exception as exc:
        logger.warning("reject_skill(%s) raised: %s", skill_id, exc)
        response.status_code = _BRAIN_FAILED
        return {
            "ok": False,
            "skill_id": skill_id,
            "rejected": False,
            "code": "reject_raised",
            "error": str(exc),
        }
    after = _pending_ids()

    rejected, known = _rejected(skill_id, returned, before, after)
    if rejected:
        return {"ok": True, "skill_id": skill_id, "rejected": True}

    response.status_code = _STATE_CONFLICT
    if known:
        reason = (
            f"'{skill_id}' is not in the approval queue, so there was nothing to reject. "
            "The queue lives in memory only and is emptied by a brain restart."
        )
        code = "not_pending"
    else:
        reason = (
            f"the skill generator did not report whether it rejected '{skill_id}', and the "
            "pending queue could not be read to check. Nothing is confirmed discarded."
        )
        code = "unconfirmed"
    return {
        "ok": False,
        "skill_id": skill_id,
        "rejected": False,
        "code": code,
        "error": reason,
    }


def _rejected(skill_id: str, returned, before, after) -> tuple[bool, bool]:
    """``(rejected, we_know_that)`` for one reject call.

    ``SkillGenerator.reject_skill`` returns a bool, and that is the
    answer whenever we get one. A registry double, or the older copy of
    ``agents/skill_generator.py`` bundled under ``desktop/``, may return
    something else; in that case we do not guess from a truthy object, we
    look at the queue before and after and report what we observed. With
    neither signal available we report failure, because the whole point
    of this route's fix is to never claim a rejection nobody saw happen.
    """
    if isinstance(returned, bool):
        return returned, True
    if before is not None and after is not None:
        return (skill_id in before and skill_id not in after), True
    return False, False


@router.get("/api/skills/pending")
async def pending_skills():
    """Get all skills waiting for user approval."""
    if not state.skill_gen:
        return {"pending": []}
    return {"pending": state.skill_gen.get_pending_skills()}


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
        response.status_code = _SUBSYSTEM_MISSING
        return {"ok": False, "skill_id": skill_id, "error": "skill registry not initialized"}

    try:
        ok, code, reason = _reload(registry, skill_id)
    except Exception as exc:
        logger.warning("reload_skill(%s) raised: %s", skill_id, exc)
        response.status_code = _BRAIN_FAILED
        return {"ok": False, "skill_id": skill_id, "error": str(exc)}

    if not ok:
        response.status_code = _STATE_CONFLICT
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
async def list_skills(response: Response):
    """Every registered skill manifest, as the Skills page renders it.

    The registry guard is not decoration. With ``state.skill_registry``
    unset this read ``None.skills`` and FastAPI turned the AttributeError
    into a 500 with a traceback in the brain log, and the v2 Skills page
    showed "No skills loaded / Check the Brain boot log" against a boot
    that was fine. 503 with a reason says which subsystem is missing, and
    the page renders that instead of a claim about the skill count.
    """
    registry = getattr(state, "skill_registry", None)
    if registry is None or getattr(registry, "skills", None) is None:
        response.status_code = _SUBSYSTEM_MISSING
        return {
            "ok": False,
            "skills": [],
            "error": "skill registry not initialized, so the brain cannot say which skills are loaded",
        }
    try:
        return [
            {
                "skill_id": s.skill_id,
                "name": s.brand.name,
                "description": s.description,
                "endpoints": len(s.endpoints),
                "trigger_phrases": s.trigger_phrases,
            }
            for s in registry.skills.values()
        ]
    except Exception as exc:
        logger.warning("list_skills failed: %s", exc)
        response.status_code = _BRAIN_FAILED
        return {"ok": False, "skills": [], "error": f"the skill registry could not be listed: {exc}"}


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
#
# They are the one deliberate exception to the status rule at the top of
# this file: their failures answer 200 with ``{"ok": false, "error":
# ...}``. That status is wrong, but the ``error`` key means no caller is
# blind to it (``apiFetch`` raises on a 2xx body carrying ``error``), so
# it is a cosmetic fault rather than the invisible-failure one this file
# was fixed for. Their only client is ``pages/Settings.jsx``
# (SkillKeysCard / SkillKeyRow), which reads the 200 body and branches on
# ``ok``; moving these to 503/400 would move that page onto a different
# code path and raise a global toast where it renders an inline chip
# today, which is a user-visible change in a page outside this change's
# scope. Left as found, deliberately.


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
