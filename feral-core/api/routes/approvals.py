"""REST routes for execution-approval inbox (non-chat approval flow)."""

from __future__ import annotations

import asyncio
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from api.state import state
from agents.runtime_context_checkpoint import RuntimeContextError
from memory.runtime_session_checkpoint import CheckpointValidationError, validate_session_id
from models.protocol import TaskReviewPayload
from security.approval_ingress import PairedDevicePrincipal

router = APIRouter(tags=["approvals"])


def _require_approval_caller(request: Request) -> PairedDevicePrincipal | None:
    """Paired-device authentication alone is not request ownership.

    Only middleware's private current principal can access owned exact reviews.
    Session/body claims and broadcast membership cannot supply that authority.
    """
    if (getattr(request.state, "phone_device_id", None) is not None
            or getattr(request.state, "device_credential_kind", None) is not None):
        source = getattr(request.state, "paired_device_principal", None)
        if type(source) is not PairedDevicePrincipal:
            raise _device_unavailable()
        _current_source(source)
        return source
    return None


def _device_unavailable():
    return HTTPException(status_code=403, detail={
        "code": "approval_device_authority_unavailable",
        "message": "This device has no current authority for this review. Use its originating review or the operator inbox.",
    })


def _current_source(source):
    try:
        source.require_current()
    except (Exception, asyncio.CancelledError):
        raise _device_unavailable() from None


def _unavailable_after_decision(approved):
    return HTTPException(status_code=403, detail={
        "code": "approval_device_authority_unavailable",
        "processing_outcome": "outcome_unknown" if approved else "unavailable",
        "action_outcome": "unknown" if approved else "not_asserted",
        "effects_may_have_occurred": approved, "retry_safe": False,
        "message": "The decision response could not be confirmed. Inspect the recorded review and action before retrying.",
    })


def _source_after_decision(source, approved):
    try:
        source.require_current()
    except (Exception, asyncio.CancelledError):
        raise _unavailable_after_decision(approved) from None


def _unavailable_after_renewal():
    return HTTPException(status_code=403, detail={
        "code": "approval_device_authority_unavailable", "processing_outcome": "outcome_unknown",
        "action_outcome": "not_asserted", "effects_may_have_occurred": False, "retry_safe": False,
        "message": "Review renewal could not be confirmed. Refresh the originating inbox before continuing.",
    })


def _device_review(body: dict | None, request_id: str | None = None):
    try:
        if not isinstance(body, dict) or set(body) != {"session_id", "task_review"}:
            raise ValueError()
        validate_session_id(body["session_id"])
        review = TaskReviewPayload.model_validate(body["task_review"]).model_dump(exclude_none=True)
        if (review != body["task_review"] or review["origin_session_id"] != body["session_id"]
                or request_id is not None and review["request_id"] != request_id):
            raise ValueError()
        return review
    except (TypeError, ValueError, KeyError):
        raise HTTPException(status_code=422, detail={"code": "task_review_invalid"}) from None


def _pending_projection(runner, row):
    return {
        "request_id": str(row.get("request_id", "") or ""),
        "session_id": str(row.get("session_id", "") or ""),
        "tool_name": str(row.get("tool_name", "") or ""),
        "args": row.get("args") or {},
        "safety_level": str(row.get("safety_level", "") or ""),
        "created_at": float(row.get("created_at", 0.0) or 0.0),
        "status": "pending", "policy_sources": row.get("policy_sources") or {},
        **_browser_resource_projection(row), **_scope_projection(runner, row),
    }


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
async def list_pending_approvals(request: Request, session_id: str = "", limit: int = 100,
                                 task_review_version: str | None = None):
    source = _require_approval_caller(request)
    orch = _require_orchestrator()
    if source is not None:
        if task_review_version != "1":
            raise _device_unavailable()
        try:
            validate_session_id(session_id)
        except CheckpointValidationError:
            raise HTTPException(status_code=422, detail={"code": "context_invalid_session"}) from None
        projected = []
        for row in orch.tool_runner.list_pending(limit=500):
            descriptor = orch.tool_runner.phone_review_descriptor(row, source_principal=source)
            if descriptor is None or descriptor["origin_session_id"] != session_id:
                continue
            review = descriptor["task_review"]
            _device_review({"session_id": session_id, "task_review": review}, row["request_id"])
            if "browser_resource" in row and (not isinstance(row["browser_resource"], dict)
                    or row["browser_resource"].get("owner_session_id") != session_id):
                # Job-scoped Chrome authority is a separate contract. Never
                # rewrite physical ownership or disclose an execution SID.
                continue
            projected.append({**_pending_projection(orch.tool_runner, row),
                              "session_id": session_id, "task_review": review})
        runtime = getattr(orch, "taskflows", None)
        if runtime is not None:
            checkpoints = runtime.list_device_review_checkpoints(session_id,
                source_principal=source, limit=_normalize_limit(limit))
            present = {row["request_id"] for row in projected}
            for row in checkpoints:
                if row["request_id"] not in present:
                    _device_review({"session_id": session_id, "task_review": row["task_review"]}, row["request_id"])
                    projected.append({key: row[key] for key in (
                        "request_id", "tool_name", "args", "task_review", "status", "approval_scope",
                        "approval_available", "review_renewal_required") if key in row} | {"session_id": session_id})
        _current_source(source)
        projected = projected[:_normalize_limit(limit)]
        return {"count": len(projected), "approvals": projected}
    sid = session_id.strip() or None
    rows = orch.tool_runner.list_pending(
        session_id=sid,
        limit=_normalize_limit(limit),
    )
    return {
        "count": len(rows),
        "approvals": [
            _pending_projection(orch.tool_runner, row)
            for row in rows
        ],
    }


async def _resolve_request(request_id: str, *, approved: bool, request: Request, body: dict | None = None) -> dict:
    source = _require_approval_caller(request)
    orch = _require_orchestrator()
    payload = body or {}
    session_id = payload.get("session_id")
    review = _device_review(body, request_id) if source is not None else None
    if session_id is not None:
        try:
            validate_session_id(session_id)
        except CheckpointValidationError:
            raise HTTPException(status_code=422, detail={"code": "context_invalid_session"}) from None
    try:
        kwargs = {"source_principal": source, "task_review": review} if source is not None else {}
        outcome = await orch.resolve_tool_approval_request(request_id, approved=approved,
            session_id=session_id, actor="api", **kwargs)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if source is not None and task is not None and task.cancelling() == 0:
            raise _unavailable_after_decision(approved) from None
        raise
    except RuntimeContextError as exc:
        raise HTTPException(status_code=503 if exc.effects_may_have_occurred else 409, detail={
            "code": exc.code, "request_id": request_id,
            "processing_outcome": "outcome_unknown" if exc.effects_may_have_occurred else "unavailable",
            "action_outcome": "unknown" if exc.effects_may_have_occurred else "not_asserted",
            "effects_may_have_occurred": exc.effects_may_have_occurred, "retry_safe": False,
            "message": "Inspect the exact approval and earlier actions before continuing.",
        }) from None
    status = str(outcome.get("status", "") or "")
    if source is not None:
        _source_after_decision(source, approved)
        if status == "approval_device_authority_unavailable":
            raise _device_unavailable()
        if status in {"approved", "rejected"}:
            outcome = {**outcome, "session_id": session_id, "task_review": review}
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
        **({"task_review": outcome["task_review"]} if "task_review" in outcome else {}),
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
        **({"task_review": outcome["task_review"]} if "task_review" in outcome else {}),
    }


@router.post("/api/approvals/{request_id}/renew")
async def renew_request(request_id: str, request: Request, body: dict | None = None):
    source = _require_approval_caller(request)
    if source is None:
        raise _device_unavailable()
    review = _device_review(body, request_id)
    origin_session = review["origin_session_id"]
    if review["kind"] != "taskflow_action" or not review.get("flow_id"):
        raise HTTPException(status_code=409, detail={"code": "task_review_renewal_unavailable"})
    orch = _require_orchestrator()
    runtime = getattr(orch, "taskflows", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail={"code": "task_review_renewal_unavailable"})
    try:
        outcome = await runtime.renew_device_model_review(review["flow_id"],
            origin_session_id=origin_session, task_review=review, source_principal=source)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling() == 0:
            raise _unavailable_after_renewal() from None
        raise
    try:
        source.require_current()
    except (Exception, asyncio.CancelledError):
        raise _unavailable_after_renewal() from None
    renewal = outcome.get("review_renewal") or {}
    if renewal.get("status") != "waiting":
        raise HTTPException(status_code=409, detail={"code": "task_review_renewal_unavailable"})
    card = _device_review({"session_id": origin_session, "task_review": renewal.get("task_review")})
    return {"success": True, "status": "renewed", "session_id": origin_session,
            "request_id": card["request_id"], "task_review": card,
            "approval_scope": {"contract_version": 1, "kind": "exact_request"}}
