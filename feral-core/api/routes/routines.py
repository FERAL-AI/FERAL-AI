"""Routine (cron job) CRUD endpoints."""

from fastapi import APIRouter
from starlette.responses import JSONResponse

from agents.scheduler import UnparseableCronExpression
from api.state import state

router = APIRouter()


def _job_to_dict(job) -> dict:
    return {
        "id": job.id,
        "job_type": job.job_type.value if hasattr(job.job_type, 'value') else str(job.job_type),
        "cron_expr": job.cron_expr,
        "description": job.description,
        "payload": job.payload,
        "session_id": job.session_id,
        "created_at": job.created_at,
        "last_run": job.last_run,
        "next_run": job.next_run,
        "enabled": job.enabled,
        "run_count": job.run_count,
        # Why the runtime turned this routine off, empty when the user did it
        # or when it is still on. Without this the UI can only say "disabled",
        # and the user has to read the server log to find out why a routine
        # they set up stopped.
        "disabled_reason": getattr(job, "disabled_reason", ""),
    }


@router.post("/api/routines")
async def create_routine(body: dict):
    if not state.scheduler:
        return {"error": "Scheduler not initialized"}
    from agents.scheduler import JobType
    job_type = body.get("job_type", "scheduled")
    try:
        jt = JobType(job_type)
    except ValueError:
        jt = JobType.CUSTOM
    cron_expr = body.get("cron_expr", body.get("schedule", "every 60m"))
    description = body.get("description", "")
    payload = body.get("payload", {})
    if body.get("skill"):
        payload["skill"] = body["skill"]
    if body.get("endpoint"):
        payload["endpoint"] = body["endpoint"]
    if body.get("prompt"):
        payload["prompt"] = body["prompt"]
    session_id = body.get("session_id", "")
    # tz_name defaults to the scheduler's timezone (the host local tz unless
    # overridden) so chat- and REST-created routines share wall-clock
    # semantics; callers may still pass an explicit IANA tz_name.
    tz_name = body.get("tz_name") or None
    recurring = bool(body.get("recurring", True))
    # A schedule the parser cannot read is a client error, not a server one.
    # Reject it here so the operator sees it while writing the routine —
    # the scheduler no longer silently re-arms unparseable jobs at 60s.
    try:
        job = state.scheduler.create_job(
            jt, cron_expr, description, payload, session_id,
            recurring=recurring, tz_name=tz_name,
        )
    except UnparseableCronExpression as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": str(exc), "field": "cron_expr"},
        )
    return {"ok": True, "routine": _job_to_dict(job)}


@router.get("/api/routines")
async def list_routines(session_id: str = ""):
    if not state.scheduler:
        return {"routines": []}
    sid = session_id or None
    jobs = state.scheduler.list_jobs(sid)
    return {"routines": [_job_to_dict(j) for j in jobs]}


@router.get("/api/routines/{routine_id}")
async def get_routine(routine_id: int):
    if not state.scheduler:
        return {"error": "Scheduler not initialized"}
    job = state.scheduler.get_job(routine_id)
    if not job:
        return {"error": "Routine not found"}
    runs = state.scheduler.get_runs(routine_id, limit=20)
    return {"routine": _job_to_dict(job), "runs": runs}


@router.post("/api/routines/{routine_id}/pause")
async def pause_routine(routine_id: int):
    if not state.scheduler:
        return {"error": "Scheduler not initialized"}
    ok = state.scheduler.pause_job(routine_id)
    return {"ok": ok}


@router.post("/api/routines/{routine_id}/resume")
async def resume_routine(routine_id: int):
    if not state.scheduler:
        return {"error": "Scheduler not initialized"}
    ok = state.scheduler.resume_job(routine_id)
    return {"ok": ok}


@router.delete("/api/routines/{routine_id}")
async def delete_routine(routine_id: int):
    if not state.scheduler:
        return {"error": "Scheduler not initialized"}
    ok = state.scheduler.delete_job(routine_id)
    return {"ok": ok}


@router.get("/api/routines/{routine_id}/runs")
async def get_routine_runs(routine_id: int, limit: int = 20):
    if not state.scheduler:
        return {"runs": []}
    return {"runs": state.scheduler.get_runs(routine_id, limit=limit)}
