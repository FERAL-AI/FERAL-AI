"""Unified 'Active Jobs' aggregator — GET /api/jobs.

Today FERAL's in-flight operational state lives in many places:
* ``state.taskflows.list_flows(status="running")`` — multi-step runtimes
* ``state.cron_service.list_jobs()`` — recurring routines
* ``state.agent_mitosis.list_specialists()`` — permanent sub-agents
* ``state.tool_genesis.get_pending_skills()`` — Tool Genesis drafts
* ``state.daemons`` — live HUP daemons (already at /api/devices/connected)

Nothing merges them. The v2 UI has to hit five endpoints and reconcile
them manually — or, as today, show only TaskFlows and miss the rest.

This route returns a single flat list of ``{id, kind, name, status,
started_at, progress, context_session_id, cancellable_via}`` entries so
a single Home "Right now" pane can render everything, and so the
Consciousness Layer (coming next) has a single source of truth for
what the agent is doing right now.

Design notes:
* Each source is isolated — a misbehaving source can't take the whole
  endpoint down. It is reported rather than hidden: the failing kind is
  named in ``degraded`` and logged at warning, because an aggregator that
  returns [] on error is indistinguishable from a quiet system, and this
  endpoint's only job is telling those two apart.
* ``cancellable_via`` names the route a client can POST to for cancel
  (or null if the item isn't cancellable from the outside).
* ``progress`` is a 0.0-1.0 float when known, null otherwise.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter

from api.state import state

logger = logging.getLogger("feral.api.jobs")

router = APIRouter(tags=["jobs"])


def _taskflow_jobs() -> list[dict]:
    """Active TaskFlow runtimes — usually the biggest bucket."""
    runtime = getattr(state, "taskflows", None)
    if runtime is None or not hasattr(runtime, "list_flows"):
        return []
    try:
        active_statuses = ("queued", "running", "waiting", "paused")
        rows: list[dict] = []
        for status in active_statuses:
            rows.extend(runtime.list_flows(status=status, limit=25))
        out: list[dict] = []
        for flow in rows:
            flow_id = flow.get("id") or flow.get("flow_id")
            if not flow_id:
                continue
            step_idx = flow.get("current_step") or 0
            total = flow.get("step_count") or len(flow.get("steps") or [])
            progress = None
            if total:
                try:
                    progress = max(0.0, min(1.0, float(step_idx) / float(total)))
                except Exception:
                    progress = None
            out.append({
                "id": flow_id,
                "kind": "taskflow",
                "name": flow.get("title") or f"TaskFlow {flow_id[:8]}",
                "status": flow.get("status") or "running",
                "started_at": flow.get("created_at") or flow.get("updated_at"),
                "progress": progress,
                "context_session_id": flow.get("session_id") or None,
                "cancellable_via": f"POST /api/taskflows/{flow_id}/cancel",
                "detail": {"step": step_idx, "steps": total},
            })
        return out
    except Exception as exc:
        # Re-raised so the combiner can report this source as degraded.
        # Returning [] here made a dead source indistinguishable from an
        # idle one, which is the entire question this endpoint answers.
        logger.warning("taskflow aggregator failed: %s", exc)
        raise


def _routine_jobs() -> list[dict]:
    """Scheduled cron routines that are enabled + firing in the near window."""
    svc = getattr(state, "cron_service", None)
    if svc is None or not hasattr(svc, "list_jobs"):
        return []
    try:
        jobs = svc.list_jobs()
        now = time.time()
        window = now + 3600  # next hour
        out: list[dict] = []
        for job in jobs:
            if not getattr(job, "enabled", True):
                continue
            next_run = getattr(job, "next_run", 0.0) or 0.0
            if next_run <= 0 or next_run > window:
                continue
            out.append({
                "id": f"routine-{job.id}",
                "kind": "routine",
                "name": job.description or f"Routine {job.id}",
                "status": "scheduled",
                "started_at": job.created_at,
                "progress": None,
                "context_session_id": job.session_id or None,
                "cancellable_via": f"DELETE /api/routines/{job.id}",
                "detail": {"cron": job.cron_expr, "next_run": next_run},
            })
        return out
    except Exception as exc:
        # Re-raised so the combiner can report this source as degraded.
        # Returning [] here made a dead source indistinguishable from an
        # idle one, which is the entire question this endpoint answers.
        logger.warning("routine aggregator failed: %s", exc)
        raise


def _specialist_jobs() -> list[dict]:
    """Mitosis specialists — count each as a standing job 'ready to serve'.

    A specialist without a currently-assigned turn is still a long-lived
    job entity; listing it here lets the Home pane surface how many
    domain-limbs are active.
    """
    engine = getattr(state, "agent_mitosis", None)
    if engine is None or not hasattr(engine, "list_specialists"):
        return []
    try:
        out: list[dict] = []
        for spec in engine.list_specialists() or []:
            agent_id = spec.get("agent_id") or spec.get("id")
            if not agent_id:
                continue
            out.append({
                "id": f"specialist-{agent_id}",
                "kind": "specialist",
                "name": spec.get("name") or agent_id,
                "status": "ready",
                "started_at": spec.get("created_at"),
                "progress": None,
                "context_session_id": None,
                "cancellable_via": None,
                "detail": {
                    "tool_permissions": spec.get("tool_permissions") or [],
                    "memory_filter": spec.get("memory_filter"),
                    "tasks_completed": spec.get("tasks_completed") or 0,
                },
            })
        return out
    except Exception as exc:
        # Re-raised so the combiner can report this source as degraded.
        # Returning [] here made a dead source indistinguishable from an
        # idle one, which is the entire question this endpoint answers.
        logger.warning("specialist aggregator failed: %s", exc)
        raise


def _tool_genesis_jobs() -> list[dict]:
    """Pending / in-sandbox Tool Genesis drafts."""
    gen = getattr(state, "tool_genesis", None)
    if gen is None:
        return []
    try:
        method = getattr(gen, "get_pending_skills", None)
        if not callable(method):
            return []
        pending = method() or []
        out: list[dict] = []
        for draft in pending:
            draft_id = draft.get("id") or draft.get("skill_id") or draft.get("name")
            if not draft_id:
                continue
            out.append({
                "id": f"draft-{draft_id}",
                "kind": "tool_genesis",
                "name": draft.get("name") or f"Skill draft {draft_id}",
                "status": draft.get("status") or "pending_review",
                "started_at": draft.get("created_at") or draft.get("drafted_at"),
                "progress": None,
                "context_session_id": draft.get("session_id") or None,
                "cancellable_via": f"POST /api/tool-genesis/{draft_id}/reject",
                "detail": {"reason": draft.get("reason"), "risk": draft.get("risk_score")},
            })
        return out
    except Exception as exc:
        # Re-raised so the combiner can report this source as degraded.
        # Returning [] here made a dead source indistinguishable from an
        # idle one, which is the entire question this endpoint answers.
        logger.warning("tool_genesis aggregator failed: %s", exc)
        raise


def _daemon_jobs() -> list[dict]:
    """Live HUP daemons as persistent 'work-surfaces'."""
    daemons = getattr(state, "daemons", None) or {}
    if not daemons:
        return []
    out: list[dict] = []
    try:
        for node_id, ws in daemons.items():
            node_type = getattr(ws, "_feral_node_type", "unknown") or "unknown"
            out.append({
                "id": f"daemon-{node_id}",
                "kind": "daemon",
                "name": node_id,
                "status": "connected",
                "started_at": None,
                "progress": None,
                "context_session_id": None,
                "cancellable_via": None,
                "detail": {
                    "node_type": node_type,
                    "capabilities": list(getattr(ws, "_feral_capabilities", []) or []),
                },
            })
    except Exception as exc:
        # Re-raised so the combiner can report this source as degraded.
        # Returning [] here made a dead source indistinguishable from an
        # idle one, which is the entire question this endpoint answers.
        logger.warning("daemon aggregator failed: %s", exc)
        raise
    return out


@router.get("/api/jobs")
async def list_jobs(
    kind: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """Return the union of active operational entities across FERAL.

    Optional filter:
      * ``kind`` — one of taskflow, routine, specialist, tool_genesis, daemon
      * ``limit`` — cap the returned count (default 100)
    """
    aggregators = {
        "taskflow": _taskflow_jobs,
        "routine": _routine_jobs,
        "specialist": _specialist_jobs,
        "tool_genesis": _tool_genesis_jobs,
        "daemon": _daemon_jobs,
    }
    items: list[dict] = []
    counts: dict[str, int] = {}
    # A source that failed and a source with nothing to report both used to
    # come back as an empty list. This endpoint exists to answer "what is the
    # brain doing right now", so those two answers being identical is the one
    # thing it must not do: a dead aggregator reads as a calm system.
    degraded: dict[str, str] = {}
    for k, fn in aggregators.items():
        if kind and kind != k:
            continue
        try:
            rows = fn()
        except Exception as exc:
            # Isolation is still the contract, it just is not silent now.
            degraded[k] = f"{type(exc).__name__}: {exc}"
            rows = []
        counts[k] = len(rows)
        items.extend(rows)

    # Sort by started_at desc when present, tie-break by id so output is stable.
    def _sort_key(row: dict) -> tuple[Any, Any]:
        started = row.get("started_at")
        return (-(started or 0), row.get("id") or "")

    items.sort(key=_sort_key)
    lim = max(1, min(int(limit or 100), 500))
    return {
        "count": len(items),
        "counts_by_kind": counts,
        "items": items[:lim],
        "degraded": degraded,
        "as_of": time.time(),
    }
