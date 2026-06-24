"""First-party persona + workflow-pack REST routes.

Exposes the manifests loaded by ``agents.persona_loader`` so the v2
Agents + Flows pages can browse + instantiate them. Distinct from
``api/routes/agent_mitosis.py`` which surfaces runtime Mitosis
specialists stored in SQLite — the personas here are the curated
first-party catalog, not user-spawned specialists.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from api.state import state

logger = logging.getLogger("feral.api.personas")

router = APIRouter(tags=["personas", "workflow-packs"])


def _persona_dict(persona: Any) -> dict:
    return persona.model_dump()


def _workflow_dict(pack: Any) -> dict:
    return pack.model_dump()


def instantiate_pack(
    workflow_id: str,
    *,
    session_id: str | None = None,
    title: str | None = None,
    context: dict | None = None,
) -> dict:
    """Materialise a workflow pack into a live TaskFlow row.

    Shared by the REST route below and the cron ``_routine_executor`` so a
    ``workflow_id`` payload on a routine reuses the exact same instantiate
    semantics (no duplicated step-building logic).

    Raises ``KeyError`` for an unknown pack id and ``RuntimeError`` if the
    TaskFlow runtime is not ready, so callers can map to the right surface
    (HTTP status vs cron run record).
    """
    packs = getattr(state, "workflow_packs", {}) or {}
    pack = packs.get(workflow_id)
    if pack is None:
        raise KeyError(workflow_id)
    if state.taskflows is None:
        raise RuntimeError("TaskFlow runtime is not ready")

    resolved_session = session_id or f"pack-{workflow_id}"
    resolved_title = title or pack.name
    resolved_context = context or {
        "instantiated_from": "workflow_pack",
        "workflow_id": workflow_id,
    }
    steps = [step.model_dump() for step in pack.steps]
    return state.taskflows.create_flow(
        session_id=resolved_session,
        title=resolved_title,
        steps=steps,
        context=resolved_context,
    )


@router.get("/api/agents/personas")
async def list_personas() -> dict:
    """Return the catalog of first-party agent personas."""
    personas = getattr(state, "personas", {}) or {}
    return {
        "count": len(personas),
        "personas": [_persona_dict(p) for p in personas.values()],
    }


@router.get("/api/agents/personas/{agent_id}")
async def get_persona(agent_id: str) -> dict:
    personas = getattr(state, "personas", {}) or {}
    persona = personas.get(agent_id)
    if persona is None:
        raise HTTPException(status_code=404, detail=f"Unknown persona {agent_id!r}")
    return _persona_dict(persona)


@router.get("/api/workflows/packs")
async def list_workflow_packs() -> dict:
    """Return the catalog of first-party workflow packs."""
    packs = getattr(state, "workflow_packs", {}) or {}
    return {
        "count": len(packs),
        "packs": [_workflow_dict(p) for p in packs.values()],
    }


@router.get("/api/workflows/packs/{workflow_id}")
async def get_workflow_pack(workflow_id: str) -> dict:
    packs = getattr(state, "workflow_packs", {}) or {}
    pack = packs.get(workflow_id)
    if pack is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown workflow pack {workflow_id!r}",
        )
    return _workflow_dict(pack)


@router.post("/api/workflows/packs/{workflow_id}/instantiate")
async def instantiate_workflow_pack(workflow_id: str, body: dict | None = None) -> dict:
    """Materialise a pack into a live TaskFlow.

    The pack is a template. Instantiating it creates an actual TaskFlow
    row via the existing ``TaskFlowRuntime.create_flow`` API, returning
    the new ``flow_id`` plus the runtime dict.
    """
    body = body or {}
    try:
        flow = instantiate_pack(
            workflow_id,
            session_id=body.get("session_id"),
            title=body.get("title"),
            context=body.get("context"),
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown workflow pack {workflow_id!r}",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to instantiate workflow pack %s", workflow_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "success": True,
        "workflow_id": workflow_id,
        "flow": flow,
    }
