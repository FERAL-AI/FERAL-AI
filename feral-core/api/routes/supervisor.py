"""REST routes for the Supervisor / Oversight surface.

Exposes:
  * GET    /api/supervisor/events?limit=&source=&actor=&decision=
  * GET    /api/supervisor/stats
  * POST   /api/supervisor/pause    {paused: bool}
  * POST   /api/supervisor/record   — explicit record for non-orchestrator
                                       sources (twin, proactive, cron, …)

The v2 /oversight page reads the first two, toggles the kill-switch via
the third, and the digital-twin engine (Commit 7) uses the fourth.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool, ValidationError

from api.state import state

router = APIRouter(tags=["supervisor"])


class PauseRequest(BaseModel):
    """Body for ``POST /api/supervisor/pause``.

    ``paused`` is a ``StrictBool`` on purpose. The pre-fix handler took a
    bare ``dict`` and ran ``bool((body or {}).get("paused", False))``,
    which is Python truthiness, not JSON booleans: ``{"paused": "no"}``,
    ``{"paused": "false"}`` and ``{"paused": 0.1}`` all coerced to
    ``True`` and paused the brain. This is the kill switch, so the wire
    type has to be exactly ``true`` or ``false``.

    ``models/protocol.py`` is canonical for HUP frames only; this is a
    REST request body, so it follows the ``api/routes/*.py`` convention
    of a local ``BaseModel`` (see ``apps.ValidateRequest``,
    ``audio.AudioConfigRequest``). Nothing here duplicates a wire
    constant that lives in the protocol module.
    """

    paused: StrictBool


def _require_supervisor():
    sup = getattr(state, "supervisor", None)
    if sup is None:
        raise HTTPException(status_code=503, detail="Supervisor not initialised")
    return sup


@router.get("/api/supervisor/events")
async def get_events(
    limit: int = 50,
    source: str = "",
    actor: str = "",
    decision: str = "",
):
    sup = _require_supervisor()
    events = sup.recent(limit=limit, source=source, actor=actor, decision=decision)
    return {"count": len(events), "events": events}


@router.get("/api/supervisor/stats")
async def get_stats():
    sup = _require_supervisor()
    return sup.stats()


@router.post("/api/supervisor/pause")
async def set_paused(request: Request):
    """Toggle the supervisor kill switch.

    The body is read raw and validated through :class:`PauseRequest`
    rather than declared as a handler parameter so a malformed body
    answers 400 with a sentence naming the field and the type we got.
    Declaring the model directly would hand the caller FastAPI's default
    422 with a nested ``loc``/``msg``/``input`` array, which is not a
    useful message for the one control that stops the brain.
    """
    sup = _require_supervisor()

    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_json",
                "field": "body",
                "message": "Body must be JSON shaped {\"paused\": true|false}.",
            },
        )

    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_body",
                "field": "body",
                "message": (
                    f"Body must be a JSON object shaped "
                    f"{{\"paused\": true|false}}, got {type(raw).__name__}."
                ),
            },
        )

    try:
        req = PauseRequest.model_validate(raw)
    except ValidationError:
        got = raw.get("paused", None)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_paused",
                "field": "paused",
                "message": (
                    f"paused must be the JSON boolean true or false, got "
                    f"{got!r}. It is NOT coerced: a truthy string would "
                    f"otherwise pause the brain."
                ),
            },
        )

    sup.set_paused(req.paused)
    return {"paused": sup.paused}


@router.post("/api/supervisor/record")
async def record(body: dict):
    """Record a supervisor event for a non-orchestrator source.

    Body shape: ``{source, kind, session_id?, actor?, payload?, decision?,
    detail?}``. Useful for cron, proactive, twin, channels — anything that
    bypasses the wrapped orchestrator entry points.
    """
    body = body or {}
    source = body.get("source")
    kind = body.get("kind")
    if not source or not kind:
        raise HTTPException(status_code=400, detail="source and kind required")
    sup = _require_supervisor()
    ev = sup.record(
        source=source,
        kind=kind,
        session_id=body.get("session_id", ""),
        actor=body.get("actor", "system"),
        payload=body.get("payload"),
        decision=body.get("decision", "allowed"),
        detail=body.get("detail") or {},
    )
    return {"success": True, "event_id": ev.event_id}
