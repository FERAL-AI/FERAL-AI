"""REST routes for execution-approval inbox (non-chat approval flow)."""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from api.state import state
from agents.runtime_context_checkpoint import RuntimeContextError
from memory.runtime_session_checkpoint import CheckpointValidationError, validate_session_id

router = APIRouter(tags=["approvals"])


def _require_approval_caller(request: Request) -> None:
    """Paired-device authentication alone is not request ownership.

    Pending approvals do not yet retain an authenticated originating device
    principal. Session/body claims and broadcast membership cannot fill that
    gap. The existing profile operator inbox remains available.
    """
    if (getattr(request.state, "phone_device_id", None) is not None
            or getattr(request.state, "device_credential_kind", None) is not None):
        raise HTTPException(status_code=403, detail={
            "code": "approval_device_authority_unavailable",
            "message": "This device cannot resolve or inspect approvals yet. Use the operator approval inbox.",
        })


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


def _browser_resource_projection(row: dict) -> dict:
    """Preserve exact review scope; invalid metadata must not imply a grant."""
    if "browser_resource" not in row:
        return {}
    resource = row["browser_resource"]
    try:
        tool = row.get("tool_name")
        if (not isinstance(resource, dict)
                or set(resource) != {"connection_id", "target_id", "owner_session_id"}
                or not all(type(value) is str for value in resource.values())
                or len(resource["connection_id"]) != 36
                or str(UUID(resource["connection_id"])) != resource["connection_id"]
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", resource["target_id"]) is None
                or type(tool) is not str or not tool.startswith(("browser__", "web_actions__"))
                or resource["owner_session_id"] != row.get("session_id")):
            raise ValueError("Invalid approval resource")
        validate_session_id(resource["owner_session_id"])
    except (KeyError, TypeError, ValueError, CheckpointValidationError):
        raise HTTPException(status_code=503, detail={"code": "approval_resource_invalid"}) from None
    return {"browser_resource": dict(resource)}


def _validated_scope(value: object) -> dict:
    """Output description only; private admission remains owned by ToolRunner."""
    if (not isinstance(value, dict) or set(value) != {"contract_version", "kind"}
            or type(value["contract_version"]) is not int or value["contract_version"] != 1
            or type(value["kind"]) is not str or value["kind"] not in {"exact_request", "session"}):
        raise HTTPException(status_code=503, detail={"code": "approval_scope_unavailable"})
    return dict(value)


def _scope_projection(runner, row: dict) -> dict:
    try:
        review = runner.approval_review_for(row)
    except Exception:
        raise HTTPException(status_code=503, detail={"code": "approval_scope_unavailable"}) from None
    if (not isinstance(review, dict) or set(review) != {"approval_scope", "approval_available"}
            or type(review["approval_available"]) is not bool):
        raise HTTPException(status_code=503, detail={"code": "approval_scope_unavailable"})
    return {"approval_scope": _validated_scope(review["approval_scope"]),
            "approval_available": review["approval_available"]}


@router.get("/api/approvals")
async def list_pending_approvals(request: Request, session_id: str = "", limit: int = 100):
    _require_approval_caller(request)
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
                **_browser_resource_projection(row),
                **_scope_projection(orch.tool_runner, row),
            }
            for row in rows
        ],
    }


async def _resolve_request(request_id: str, *, approved: bool, request: Request, body: dict | None = None) -> dict:
    _require_approval_caller(request)
    orch = _require_orchestrator()
    payload = body or {}
    session_id = payload.get("session_id")
    if session_id is not None:
        try:
            validate_session_id(session_id)
        except CheckpointValidationError:
            raise HTTPException(status_code=422, detail={"code": "context_invalid_session"}) from None
    try:
        outcome = await orch.resolve_tool_approval_request(
            request_id, approved=approved, session_id=session_id, actor="api",
        )
    except RuntimeContextError as exc:
        raise HTTPException(status_code=503 if exc.effects_may_have_occurred else 409, detail={
            "code": exc.code, "request_id": request_id,
            "processing_outcome": "outcome_unknown" if exc.effects_may_have_occurred else "unavailable",
            "action_outcome": "unknown" if exc.effects_may_have_occurred else "not_asserted",
            "effects_may_have_occurred": exc.effects_may_have_occurred, "retry_safe": False,
            "message": "Inspect the exact approval and earlier actions before continuing.",
        }) from None
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
    if status in {"origin_unavailable", "origin_unsettled", "origin_superseded", "stale_context"}:
        raise HTTPException(status_code=409, detail={
            "code": status, "request_id": request_id,
            "processing_outcome": "unavailable", "action_outcome": "not_asserted",
            "retry_safe": False,
            "message": "This approval is not currently available. Refresh and review before continuing.",
        })
    if status in {"approved", "rejected"}:
        try:
            scope = _validated_scope(outcome.get("approval_scope"))
        except HTTPException:
            raise HTTPException(status_code=503, detail={
                "code": "approval_scope_unavailable", "request_id": request_id,
                "processing_outcome": "outcome_unknown", "action_outcome": "unknown" if approved else "not_asserted",
                "effects_may_have_occurred": approved, "retry_safe": False,
                "message": "The decision scope was not confirmed. Inspect the request and results before continuing.",
            }) from None
        outcome = {**outcome, "approval_scope": scope}
    return outcome


@router.get("/api/approvals/trust")
async def trust_state():
    """Which tools have stopped asking, and why.

    "It stopped asking" is only an acceptable behaviour if the operator
    can find out why, and take it back. This is the receipt: every tool
    eligible for earned autonomy, how many consecutive clean runs it
    has, the threshold, and whether it is currently trusted.

    Only tools ``skills/checkpoints.py`` can revert are ever eligible,
    so the list is deliberately short -- latitude never exceeds undo.
    See ``security/trust_ledger.py``.
    """
    orch = _require_orchestrator()
    rows = orch.tool_runner.trust_snapshot()
    return {
        "count": len(rows),
        "autonomy_mode": getattr(orch.tool_runner, "_autonomy_mode", ""),
        # Earned autonomy applies under hybrid only: strict always asks,
        # loose never does. Saying so here stops a reader concluding the
        # feature is broken when it is simply not in play.
        "active": getattr(orch.tool_runner, "_autonomy_mode", "") == "hybrid",
        "tools": rows,
    }


@router.post("/api/approvals/{request_id}/approve")
async def approve_request(request_id: str, request: Request, body: dict | None = None):
    outcome = await _resolve_request(request_id, approved=True, request=request, body=body)
    return {
        "success": True,
        "status": outcome.get("status", "approved"),
        "request_id": outcome.get("request_id", request_id),
        "session_id": outcome.get("session_id", ""),
        "tool_name": outcome.get("tool_name", ""),
        "summary": outcome.get("summary", ""),
        "result": outcome.get("result", {}),
        **({"approval_scope": outcome["approval_scope"]} if "approval_scope" in outcome else {}),
    }


@router.post("/api/approvals/{request_id}/reject")
async def reject_request(request_id: str, request: Request, body: dict | None = None):
    outcome = await _resolve_request(request_id, approved=False, request=request, body=body)
    return {
        "success": True,
        "status": outcome.get("status", "rejected"),
        "request_id": outcome.get("request_id", request_id),
        "session_id": outcome.get("session_id", ""),
        "tool_name": outcome.get("tool_name", ""),
        **({"approval_scope": outcome["approval_scope"]} if "approval_scope" in outcome else {}),
    }
