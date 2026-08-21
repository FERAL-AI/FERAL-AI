"""REST routes for execution-approval inbox (non-chat approval flow)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.state import state

router = APIRouter(tags=["approvals"])


def _require_orchestrator():
    orch = getattr(state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised")
    runner = getattr(orch, "tool_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="ToolRunner not initialised")
    return orch


def _normalize_limit(limit: int) -> int:
    if limit <= 0:
        return 100
    return min(limit, 500)


@router.get("/api/approvals")
async def list_pending_approvals(session_id: str = "", limit: int = 100):
    orch = _require_orchestrator()
    sid = session_id.strip() or None
    rows = orch.tool_runner.list_pending(
        session_id=sid,
        limit=_normalize_limit(limit),
    )
    return {
        "count": len(rows),
        "approvals": [
            {
                "request_id": str(row.get("request_id", "") or ""),
                "session_id": str(row.get("session_id", "") or ""),
                "tool_name": str(row.get("tool_name", "") or ""),
                "args": row.get("args") or {},
                "safety_level": str(row.get("safety_level", "") or ""),
                "created_at": float(row.get("created_at", 0.0) or 0.0),
                "status": "pending",
                # ToolRunner records this on every pending row specifically
                # so a renderer can answer "why are we asking?" without
                # re-running the resolver, and this projection was dropping
                # it, which made the field unreachable over HTTP for the
                # one client that would use it.
                "policy_sources": row.get("policy_sources") or {},
            }
            for row in rows
        ],
    }


async def _resolve_request(request_id: str, *, approved: bool, body: dict | None = None) -> dict:
    orch = _require_orchestrator()
    payload = body or {}
    session_id = str(payload.get("session_id", "") or "").strip() or None
    outcome = await orch.resolve_tool_approval_request(
        request_id,
        approved=approved,
        session_id=session_id,
        actor="api",
    )
    status = str(outcome.get("status", "") or "")
    if status == "not_found":
        raise HTTPException(status_code=404, detail="unknown approval request")
    if status == "session_mismatch":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "session_mismatch",
                "request_id": request_id,
                "session_id": outcome.get("session_id", ""),
                "pending_session_id": outcome.get("pending_session_id", ""),
            },
        )
    return outcome


@router.post("/api/approvals/{request_id}/approve")
async def approve_request(request_id: str, body: dict | None = None):
    outcome = await _resolve_request(request_id, approved=True, body=body)
    return {
        "success": True,
        "status": outcome.get("status", "approved"),
        "request_id": outcome.get("request_id", request_id),
        "session_id": outcome.get("session_id", ""),
        "tool_name": outcome.get("tool_name", ""),
        "summary": outcome.get("summary", ""),
        "result": outcome.get("result", {}),
    }


@router.post("/api/approvals/{request_id}/reject")
async def reject_request(request_id: str, body: dict | None = None):
    outcome = await _resolve_request(request_id, approved=False, body=body)
    return {
        "success": True,
        "status": outcome.get("status", "rejected"),
        "request_id": outcome.get("request_id", request_id),
        "session_id": outcome.get("session_id", ""),
        "tool_name": outcome.get("tool_name", ""),
    }
