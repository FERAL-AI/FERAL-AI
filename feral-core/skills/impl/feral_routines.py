"""FERAL Routines skill — create/manage recurring scheduled jobs as an LLM tool.

This is the first-class tool that lets the brain answer "do X every day at
5pm" with a *real* recurring routine instead of a one-shot reminder. It is a
thin delegate over :class:`agents.scheduler.CronService` and uses the SAME
payload + scheduling semantics as ``api/routes/routines.py`` so a routine
created from chat behaves identically to one created over REST.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from skills.base import BaseSkill
from skills.impl import register_skill


# Optional test/embedding override. When set, this scheduler instance is used
# instead of the live ``api.state.state.scheduler``. Production never sets this.
_SCHEDULER_OVERRIDE: Any = None


def set_scheduler_override(scheduler: Any) -> None:
    """Inject a CronService (used by tests / embedded harnesses)."""
    global _SCHEDULER_OVERRIDE
    _SCHEDULER_OVERRIDE = scheduler


def _get_scheduler() -> Any:
    if _SCHEDULER_OVERRIDE is not None:
        return _SCHEDULER_OVERRIDE
    try:
        from api.state import state as _state
    except Exception:
        return None
    return getattr(_state, "scheduler", None) or getattr(_state, "cron_service", None)


def _arg_value(args: Dict[str, Any], *keys: str) -> Any:
    """Return the first present value among *keys*, honouring a nested
    ``values`` dict the way feral_reminders does."""
    values = args.get("values") if isinstance(args.get("values"), dict) else None
    for key in keys:
        if key in args and args.get(key) is not None:
            return args.get(key)
        if values is not None and key in values and values.get(key) is not None:
            return values.get(key)
    return None


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalize_cron(raw: str) -> str:
    """Map natural-language schedules to canonical scheduler forms, reusing
    CronService's own NL parser so chat and REST stay in lock-step. Canonical
    expressions (``daily 17:00``, ``every 30m``, ``0 9 * * *``) pass through
    unchanged because the NL parser returns ``None`` for them."""
    text = (raw or "").strip()
    if not text:
        return text
    try:
        from agents.scheduler import CronService
        parsed = CronService._parse_nl_to_cron(text)
        if parsed:
            return parsed
    except Exception:
        pass
    return text


def _job_to_dict(job) -> dict:
    return {
        "id": job.id,
        "job_type": job.job_type.value if hasattr(job.job_type, "value") else str(job.job_type),
        "cron_expr": job.cron_expr,
        "description": job.description,
        "payload": job.payload,
        "session_id": job.session_id,
        "created_at": job.created_at,
        "last_run": job.last_run,
        "next_run": job.next_run,
        "enabled": job.enabled,
        "run_count": job.run_count,
        "recurring": getattr(job, "recurring", True),
        "tz_name": getattr(job, "tz_name", "UTC"),
    }


@register_skill
class FeralRoutinesSkill(BaseSkill):
    name = "FERAL Routines"
    description = "Create and manage recurring scheduled routines."
    safety_level = "SAFE"

    def __init__(self) -> None:
        super().__init__(skill_id="feral_routines")

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        del vault
        scheduler = _get_scheduler()
        if scheduler is None:
            return {
                "success": False,
                "status_code": 503,
                "data": None,
                "error": "Scheduler (CronService) is not available.",
            }

        if endpoint_id == "create":
            return self._create(scheduler, args)
        if endpoint_id == "list":
            return self._list(scheduler, args)
        if endpoint_id == "delete":
            return self._mutate(scheduler, args, "delete")
        if endpoint_id == "pause":
            return self._mutate(scheduler, args, "pause")
        if endpoint_id == "resume":
            return self._mutate(scheduler, args, "resume")

        return {"success": False, "status_code": 404, "data": None, "error": f"Unknown endpoint: {endpoint_id}"}

    def _create(self, scheduler, args: Dict[str, Any]) -> Dict[str, Any]:
        from agents.scheduler import JobType, UnparseableCronExpression

        raw_cron = _arg_value(args, "cron_expr", "schedule", "cron")
        cron_expr = _normalize_cron(str(raw_cron or "").strip())
        if not cron_expr:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": "Routine requires a schedule. Pass cron_expr (e.g. 'daily 17:00' or 'every day at 5pm').",
                "reason": "missing_required_field",
                "field": "cron_expr",
            }

        description = str(_arg_value(args, "description") or "").strip()
        skill_id = _arg_value(args, "skill_id", "skill")
        endpoint = _arg_value(args, "endpoint_id", "endpoint")
        call_args = _arg_value(args, "args")
        prompt = _arg_value(args, "prompt", "action_text")
        workflow_id = _arg_value(args, "workflow_id")
        flow_id = _arg_value(args, "flow_id")
        session_id = str(_arg_value(args, "session_id") or "")
        recurring = _to_bool(_arg_value(args, "recurring"), default=True)
        tz_name = _arg_value(args, "tz_name")
        # When the user explicitly asks the routine to run an action without a
        # confirmation prompt ("auto confirm it"), stamp the payload so the
        # cron executor dispatches the device action directly (the cron
        # surface has no human to approve at fire time).
        auto_confirm = _to_bool(_arg_value(args, "auto_confirm"), default=False)

        payload: Dict[str, Any] = {}
        if skill_id:
            payload["skill"] = str(skill_id)
        if endpoint:
            payload["endpoint"] = str(endpoint)
        if isinstance(call_args, dict):
            payload["args"] = call_args
        if prompt:
            payload["prompt"] = str(prompt)
        if auto_confirm:
            payload["auto_confirm"] = True
        if workflow_id:
            payload["workflow_id"] = str(workflow_id)
        if flow_id:
            payload["flow_id"] = str(flow_id)

        if not description:
            description = str(prompt or workflow_id or (f"{skill_id}.{endpoint}" if skill_id else cron_expr))

        try:
            job = scheduler.create_job(
                JobType.SCHEDULED,
                cron_expr,
                description,
                payload,
                session_id,
                recurring=recurring,
                tz_name=tz_name,
            )
        except UnparseableCronExpression as exc:
            # A schedule we cannot parse is the caller's mistake, not a
            # server fault — 400, with the supported forms in the message so
            # the LLM can retry with a valid expression instead of the user
            # ending up with a routine that fires every 60 seconds.
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": str(exc),
                "reason": "unparseable_schedule",
                "field": "cron_expr",
            }
        except Exception as exc:
            return {"success": False, "status_code": 500, "data": None, "error": str(exc)}

        # Honesty principle: verify the routine actually landed by re-reading
        # it from the store before reporting success. The brain must never
        # claim a routine was created when it is not in the list.
        confirmed = None
        try:
            confirmed = scheduler.get_job(job.id)
        except Exception:
            confirmed = None
        if confirmed is None:
            try:
                confirmed = next((j for j in scheduler.list_jobs(session_id or None) if j.id == job.id), None)
            except Exception:
                confirmed = None
        if confirmed is None:
            return {
                "success": False,
                "status_code": 500,
                "data": None,
                "error": (
                    "Routine creation could not be confirmed — it is not in the "
                    "routine list after write. Nothing was scheduled; please retry."
                ),
                "reason": "verify_after_write_failed",
            }

        return {
            "success": True,
            "status_code": 200,
            "data": {"routine": _job_to_dict(confirmed), "verified": True},
            "error": None,
        }

    def _list(self, scheduler, args: Dict[str, Any]) -> Dict[str, Any]:
        session_id = _arg_value(args, "session_id")
        sid = str(session_id) if session_id else None
        jobs = scheduler.list_jobs(sid)
        listed = [_job_to_dict(j) for j in jobs]
        return {
            "success": True,
            "status_code": 200,
            "data": {"routines": listed, "count": len(listed)},
            "error": None,
        }

    def _mutate(self, scheduler, args: Dict[str, Any], op: str) -> Dict[str, Any]:
        routine_id = _coerce_int(_arg_value(args, "routine_id", "id", "job_id"))
        if routine_id is None:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": "Routine id is required (numeric).",
                "reason": "missing_required_field",
                "field": "routine_id",
            }
        if op == "delete":
            ok = scheduler.delete_job(routine_id)
            key = "deleted"
        elif op == "pause":
            ok = scheduler.pause_job(routine_id)
            key = "paused"
        else:
            ok = scheduler.resume_job(routine_id)
            key = "resumed"
        return {
            "success": bool(ok),
            "status_code": 200 if ok else 404,
            "data": {key: bool(ok), "routine_id": routine_id},
            "error": None if ok else f"Routine {routine_id} not found",
        }
