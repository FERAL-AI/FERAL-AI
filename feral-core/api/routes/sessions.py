"""Sessions REST surface.

Currently exposes a single endpoint::

    POST /api/sessions/{session_id}/spawn
        body: {kind, scope_key, model_override?}
        returns: {child_session_id}

The route is GATED by the Supervisor: if the Supervisor is paused or a
``policy_gate`` is registered and returns ``"denied"``, the request is
audited and rejected before the spawner runs. This keeps the human-in-
the-loop "big red pause button" semantics intact for any future
session-management endpoints we add here.

Implements the no-bypass doctrine.
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from api.state import state
from agents.subagent_spawner import SubagentNotAllowed

logger = logging.getLogger("feral.api.sessions")

router = APIRouter(tags=["sessions"])


@router.get("/api/sessions/primary/transcript")
async def get_primary_transcript(limit: int = 100, since_ms: int = 0):
    """Phase 9 (audit-r10 overhaul) — read the live primary-session
    conversation history.

    The iOS chat client calls this on reconnect (`scenePhase: .active`)
    to recover messages the brain emitted while the WebSocket was
    torn down for background. Without this round-trip those messages
    were lost — the operator's "chat stops after a single answer
    instead of continuing" complaint when they backgrounded the app
    mid-response.

    Returns ``{messages: [{role, text, ts_ms}, ...], primary_session_id}``.
    `ts_ms` is the message's logical position in the thread expressed
    as milliseconds; clients can pass it back as `since_ms` for an
    incremental refresh that doesn't re-download the full history.

    `limit` caps the most-recent messages returned (default 100,
    clamped 1..500). Read-only; no auth — LAN-public like the other
    capability probes.
    """
    limit = max(1, min(limit, 500))
    sid = state.primary_session_id

    orchestrator = getattr(state, "orchestrator", None)
    if orchestrator is None:
        return {"primary_session_id": sid, "messages": [], "error": "orchestrator not ready"}

    raw_history = []
    try:
        raw_history = orchestrator.conversation_history.get(sid, []) or []
    except Exception as exc:
        return {"primary_session_id": sid, "messages": [], "error": f"history read failed: {exc}"}

    # The orchestrator stores OpenAI-shaped dicts: `{role, content, ...}`.
    # Filter to user / assistant turns only — tool calls + system
    # prompts aren't useful to the client and would just inflate the
    # payload. Position-based ts_ms is enough for the iOS client to
    # do `since_ms` incremental reads; we don't have wall-clock
    # timestamps on every history entry.
    out: list[dict] = []
    for idx, entry in enumerate(raw_history):
        role = entry.get("role") if isinstance(entry, dict) else None
        if role not in ("user", "assistant"):
            continue
        content = entry.get("content")
        if isinstance(content, list):
            # OpenAI vision-style content arrays — join the text
            # segments so the client gets a plain string.
            text_parts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            ]
            text = "".join(text_parts).strip()
        else:
            text = str(content or "").strip()
        if not text:
            continue
        ts_ms = idx + 1  # monotonic position; 1-based
        if ts_ms <= since_ms:
            continue
        out.append({"role": role, "text": text, "ts_ms": ts_ms})

    out = out[-limit:]
    return {
        "primary_session_id": sid,
        "messages": out,
        "count": len(out),
    }


@router.get("/api/sessions/primary")
async def get_primary_session():
    """Return the per-install shared chat session id.

    Audit-r9 fix — operator: "the chat and memory should be the same
    for my phone chat and the webui for feral brain on the local brain".

    Both `/v1/session` (web WebSocket) and HUP `chat_request` (phone
    nodes) now default to `state.primary_session_id`. Clients that
    want a "new chat" thread can fetch this id, branch off it (e.g.
    by appending `:tabA`), and pass the new id explicitly. Web does
    so via the `?session_id=` WebSocket query param; iOS does so via
    `BrainClient.chatSessionId`.

    Returns:
        {"session_id": str}
    """
    sid = getattr(state, "primary_session_id", "")
    if not sid:
        raise HTTPException(
            status_code=503,
            detail="primary_session_id not initialised — brain not booted yet",
        )
    return {"session_id": sid}


def _require_supervisor():
    sup = getattr(state, "supervisor", None)
    if sup is None:
        raise HTTPException(status_code=503, detail="Supervisor not initialised")
    return sup


def _gate_through_supervisor(
    sup,
    *,
    session_id: str,
    kind: str,
    scope_key: str,
    model_override: str | None,
) -> None:
    """Apply the supervisor pause + policy_gate before any spawn work.

    This mirrors the gate pattern in ``agents/supervisor.py`` so the
    route does not bypass approval. We construct a SupervisorEvent
    on the same shape the wrapper uses, then check pause and policy.
    """
    from agents.supervisor import (
        SupervisorBlocked,
        SupervisorEvent,
        _hash_payload,
        _summarise,
    )

    payload = {
        "kind": kind,
        "scope_key": scope_key,
        "model_override": model_override,
    }
    event = SupervisorEvent(
        event_id=str(uuid4()),
        ts=time.time(),
        source="api",
        kind="sessions_spawn",
        session_id=str(session_id or ""),
        actor="user",
        payload_hash=_hash_payload(payload),
        payload_summary=_summarise(payload),
        decision="allowed",
        latency_ms=0,
        detail={"route": "POST /api/sessions/{id}/spawn"},
    )

    if getattr(sup, "paused", False):
        event.decision = "denied"
        event.detail["reason"] = "supervisor_paused"
        sup._record(event)
        raise HTTPException(status_code=423, detail="Supervisor is paused")

    gate = getattr(sup, "policy_gate", None)
    if gate is not None:
        try:
            verdict = gate(event) or "allowed"
        except Exception as exc:
            logger.exception("policy_gate raised: %s", exc)
            verdict = "allowed"
        event.decision = verdict
        if verdict == "denied":
            event.detail["reason"] = "policy_denied"
            sup._record(event)
            raise HTTPException(status_code=403, detail="Policy denied")
        if verdict == "queued":
            event.detail["reason"] = "policy_queued"
            sup._record(event)
            raise HTTPException(status_code=202, detail="Spawn queued by policy")

    sup._record(event)


@router.post("/api/sessions/{session_id}/spawn")
async def post_spawn_subsession(session_id: str, body: dict | None = None):
    """Spawn a child subsession of *session_id*.

    Body shape: ``{"kind": str, "scope_key": str, "model_override"?: str}``.
    Returns ``{"child_session_id": str}`` on success.
    """
    body = body or {}
    kind = body.get("kind")
    scope_key = body.get("scope_key")
    model_override = body.get("model_override")

    if not isinstance(kind, str) or not kind.strip():
        raise HTTPException(status_code=400, detail="'kind' is required")
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise HTTPException(status_code=400, detail="'scope_key' is required")
    if model_override is not None and not isinstance(model_override, str):
        raise HTTPException(status_code=400, detail="'model_override' must be a string")

    sup = _require_supervisor()
    _gate_through_supervisor(
        sup,
        session_id=session_id,
        kind=kind,
        scope_key=scope_key,
        model_override=model_override,
    )

    orch = getattr(state, "orchestrator", None)
    if orch is None or not hasattr(orch, "spawn_subsession"):
        raise HTTPException(status_code=503, detail="Orchestrator not initialised")

    try:
        child_id = await orch.spawn_subsession(
            session_id,
            kind,
            scope_key=scope_key,
            model_override=model_override,
        )
    except SubagentNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.exception("spawn_subsession failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"spawn failed: {exc}") from exc

    return {"child_session_id": child_id}


# ────────────────────────────────────────────────────────────────────
# PR 7: List / cancel / steer for live  subsessions
# ────────────────────────────────────────────────────────────────────


def _record_subsession_audit(
    sup,
    *,
    session_id: str,
    child_id: str,
    decision: str,
    detail: dict,
) -> None:
    """Best-effort supervisor audit for cancel/steer outcomes.

    Used so the supervisor's persistent record (which the UI/status
    endpoint reads) reflects subsession lifecycle events even though
    the in-memory registry is process-local."""
    if sup is None or not hasattr(sup, "record"):
        return
    try:
        sup.record(
            source="api",
            kind="subsession_lifecycle",
            session_id=str(session_id or ""),
            actor="user",
            payload={"child_id": child_id, **detail},
            decision=decision,
            detail={"child_id": child_id, **detail},
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("subsession audit record failed: %s", exc)


def _serialize_child(child: dict) -> dict:
    task = child.get("task")
    return {
        "child_id": child.get("child_id"),
        "parent_id": child.get("parent_id"),
        "kind": child.get("kind"),
        "scope_key": child.get("scope_key"),
        "model_override": child.get("model_override"),
        "started_at": child.get("started_at"),
        "running": bool(task and not task.done()),
        "done": bool(task and task.done()),
    }


@router.get("/api/sessions/{session_id}/subsessions")
async def list_subsessions(session_id: str):
    """List active  child subsessions for *session_id*.

    Returns ``{"subsessions": [{...}, ...]}``. Unknown parents return
    an empty list rather than 404 — the absence of children is a
    valid, non-erroneous state."""
    try:
        from agents.subagent_spawner import get_registry
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"spawner unavailable: {exc}") from exc

    children = get_registry().children_of(session_id)
    return {"subsessions": [_serialize_child(c) for c in children]}


@router.post("/api/sessions/{session_id}/subsessions/{child_id}/cancel")
async def cancel_subsession(session_id: str, child_id: str):
    """Cancel a single child subsession (does not affect siblings)."""
    sup = _require_supervisor()
    if getattr(sup, "paused", False):
        raise HTTPException(status_code=423, detail="Supervisor is paused")

    try:
        from agents.subagent_spawner import get_registry
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"spawner unavailable: {exc}") from exc

    registry = get_registry()
    target = None
    for child in registry.children_of(session_id):
        if child.get("child_id") == child_id:
            target = child
            break
    if target is None:
        raise HTTPException(status_code=404, detail="Unknown subsession")

    cancelled = await registry._cancel_targets(session_id, [target])  # type: ignore[attr-defined]
    _record_subsession_audit(
        sup,
        session_id=session_id,
        child_id=child_id,
        decision="allowed",
        detail={"event": "cancelled", "kind": target.get("kind"), "scope_key": target.get("scope_key")},
    )
    return {"cancelled": cancelled, "child_id": child_id}


@router.post("/api/sessions/{session_id}/subsessions/cancel-all")
async def cancel_all_subsessions(session_id: str):
    """Cancel every child subsession registered under *session_id*."""
    sup = _require_supervisor()
    if getattr(sup, "paused", False):
        raise HTTPException(status_code=423, detail="Supervisor is paused")

    try:
        from agents.subagent_spawner import get_registry
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"spawner unavailable: {exc}") from exc

    cancelled = await get_registry().cancel_all_children(session_id)
    _record_subsession_audit(
        sup,
        session_id=session_id,
        child_id="*",
        decision="allowed",
        detail={"event": "cancel_all", "count": cancelled},
    )
    return {"cancelled": cancelled}


@router.post("/api/sessions/{session_id}/subsessions/{child_id}/steer")
async def steer_subsession_route(session_id: str, child_id: str, body: dict | None = None):
    """Push a steer message to a running subagent.

    Body shape: ``{"message": str}``."""
    body = body or {}
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=400, detail="'message' is required")

    sup = _require_supervisor()
    if getattr(sup, "paused", False):
        raise HTTPException(status_code=423, detail="Supervisor is paused")

    try:
        from agents.subagent_spawner import get_registry
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"spawner unavailable: {exc}") from exc

    registry = get_registry()
    try:
        outcome = await registry.steer_subsession(session_id, child_id, message)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        # The spawner raises RuntimeError("no steer hook registered")
        # when the supervisor doesn't expose a steer callback. Surface
        # that truthfully — don't pretend the steer succeeded.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("steer_subsession failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"steer failed: {exc}") from exc

    _record_subsession_audit(
        sup,
        session_id=session_id,
        child_id=child_id,
        decision="allowed",
        detail={"event": "steered", "message_len": len(message)},
    )
    return {"steered": True, "child_id": child_id, "outcome": outcome}


# ────────────────────────────────────────────────────────────────────
# PR 7: Active-work status endpoint
# ────────────────────────────────────────────────────────────────────


@router.get("/api/agents/active")
async def get_active_agents():
    """Combined snapshot of long-running agent work.

    Returns three buckets:

    * ``subsessions``: live  children across every registered parent.
    * ``taskflows``: open/running/waiting taskflow rows.
    * ``intent_plans``: active IntentCompiler plans.

    Used by the UI's "agent activity" panel and by `feral doctor` to
    answer "what is the agent actually doing right now?" honestly,
    without polling three separate endpoints."""
    out: dict = {
        "subsessions": [],
        "taskflows": [],
        "intent_plans": [],
        "warnings": [],
    }

    # Subsessions
    try:
        from agents.subagent_spawner import get_registry
        reg = get_registry()
        all_children: list[dict] = []
        for parent_id, children in reg._by_parent.items():  # type: ignore[attr-defined]
            for c in children:
                all_children.append(_serialize_child(c))
        out["subsessions"] = all_children
    except Exception as exc:
        out["warnings"].append(f"subsessions unavailable: {exc}")

    # TaskFlows (waiting/running/queued only — completed/cancelled excluded)
    try:
        tf = getattr(state, "taskflows", None)
        if tf is not None and hasattr(tf, "list_flows"):
            open_statuses = {"queued", "running", "waiting"}
            rows = tf.list_flows(limit=200)
            out["taskflows"] = [
                r for r in rows
                if str(r.get("status", "")).lower() in open_statuses
            ]
    except Exception as exc:
        out["warnings"].append(f"taskflows unavailable: {exc}")

    # Intent plans (active only)
    try:
        compiler = getattr(state, "intent_compiler", None)
        if compiler is not None and hasattr(compiler, "list_plans"):
            plans = compiler.list_plans() or []
            out["intent_plans"] = [
                p for p in plans
                if str(p.get("status", "")).lower() == "active"
            ]
    except Exception as exc:
        out["warnings"].append(f"intent_plans unavailable: {exc}")

    return out
