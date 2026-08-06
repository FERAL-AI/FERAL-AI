"""REST routes for IdeasEngine — the "For you today" pane on v2 Home.

Routes
------
GET    /api/ideas/today           — list today's active ideas (max 20)
POST   /api/ideas/{id}/accept     — record acceptance + echo the idea
POST   /api/ideas/{id}/dismiss    — record dismissal (future ideas for that
                                    signal are suppressed after 3 dismissals)
POST   /api/ideas/refresh         — manually re-run triggers (used by the
                                    v2 pane on mount + pull-to-refresh)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from api.state import state

logger = logging.getLogger("feral.api.ideas")

router = APIRouter(tags=["ideas"])


def _require_engine():
    engine = getattr(state, "ideas_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="IdeasEngine not initialised")
    return engine


def _idea_to_dict(idea) -> dict:
    return idea.to_dict()


@router.get("/api/ideas/today")
async def ideas_today():
    engine = _require_engine()
    ideas = engine.list_today()
    return {
        "ideas": [_idea_to_dict(i) for i in ideas],
        "count": len(ideas),
    }


@router.post("/api/ideas/{idea_id}/accept")
async def accept_idea(idea_id: str):
    engine = _require_engine()
    idea = engine.accept(idea_id)
    if idea is None:
        raise HTTPException(status_code=404, detail=f"Unknown idea id {idea_id}")
    return {"success": True, "idea": _idea_to_dict(idea)}


@router.post("/api/ideas/{idea_id}/dismiss")
async def dismiss_idea(idea_id: str):
    engine = _require_engine()
    idea = engine.dismiss(idea_id)
    if idea is None:
        raise HTTPException(status_code=404, detail=f"Unknown idea id {idea_id}")
    return {"success": True, "idea": _idea_to_dict(idea)}


@router.post("/api/ideas/refresh")
async def refresh_ideas():
    """Run the three triggers synchronously and return the generated list.

    Endpoint exists so the v2 ForYouToday pane can pull on demand without
    waiting for the scheduler to hit 07:30. Safe to call frequently — the
    engine deduplicates identical source signals.
    """
    engine = _require_engine()
    bundle: list = []
    # Both generators used to fail into a debug line under an unconditional
    # success:True, so a pane with no ideas in it meant either "nothing to
    # suggest today" or "both generators are broken", with no way to tell.
    degraded: dict[str, str] = {}
    try:
        bundle.extend(engine.morning_brief())
    except Exception as exc:
        logger.warning("morning_brief raised: %s", exc)
        degraded["morning_brief"] = f"{type(exc).__name__}: {exc}"
    try:
        bundle.extend(engine.refresh_waiting_user())
    except Exception as exc:
        logger.warning("refresh_waiting_user raised: %s", exc)
        degraded["refresh_waiting_user"] = f"{type(exc).__name__}: {exc}"
    return {
        # False when every generator failed: there is no sense in which the
        # refresh the caller asked for succeeded.
        "success": len(degraded) < 2,
        "degraded": degraded,
        "new_ideas": [_idea_to_dict(i) for i in bundle],
        "today": [_idea_to_dict(i) for i in engine.list_today()],
    }
