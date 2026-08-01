"""Plan skill: the one surface plan mode gives the model to make progress.

Shipped as a manifest in ``feral-core/skills/manifests/`` rather than
registered at runtime, for two reasons that outlived the original one:

* ``skills/result_budget.builtin_skill_ids()`` derives the trust set by
  globbing THAT directory, so a runtime-registered skill is clamped to
  the ``standard`` tier no matter what it declares. ``submit`` declares
  ``feed`` so a real plan is not cut to 2 000 chars.
* Boot-loading keeps the skill present before the first tool call, which
  is when the orchestrator's plan-mode filter first asks about it.

The original reason (a runtime-registered skill being invisible to the
dispatch validator's registry snapshot) no longer applies on its own:
``SkillRegistry.generation`` now increments on every ``register()`` and
``ToolDispatchValidator.refresh_if_stale()`` rebuilds when it moves, so a
runtime skill WOULD be seen. The budget clamp is the constraint that
still binds.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.plan")

# Test/embedding override for the PlanModeState the skill records into.
_PLAN_STATE_OVERRIDE: Any = None


def set_plan_state_override(state: Any) -> None:
    global _PLAN_STATE_OVERRIDE
    _PLAN_STATE_OVERRIDE = state


def _get_plan_state() -> Any:
    if _PLAN_STATE_OVERRIDE is not None:
        return _PLAN_STATE_OVERRIDE
    try:
        from api.state import state as _state
    except Exception:
        return None
    orchestrator = getattr(_state, "orchestrator", None)
    return getattr(orchestrator, "plan_mode", None)


def _session_id(args: Dict[str, Any]) -> str:
    """Prefer the ambient call context over anything the model supplied.

    The model does not reliably know its own session id, and letting it
    choose one would let it record a plan against a session other than
    the one it is in. ``skills/call_context`` carries the real identity.
    """
    try:
        from skills.call_context import current_context
        bound = getattr(current_context(), "session_id", "") or ""
    except Exception:
        bound = ""
    if bound:
        return bound
    return str(args.get("session_id") or "")


def _normalize_steps(raw: Any) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for index, item in enumerate(raw or []):
        if isinstance(item, dict):
            step = {
                "index": index,
                "title": str(item.get("title") or item.get("step") or "").strip(),
                "detail": str(item.get("detail") or item.get("description") or "").strip(),
                "tools": [str(t) for t in (item.get("tools") or []) if str(t).strip()],
            }
            if not step["title"] and step["detail"]:
                step["title"] = step["detail"][:120]
        else:
            step = {
                "index": index,
                "title": str(item).strip(),
                "detail": "",
                "tools": [],
            }
        if step["title"] or step["detail"]:
            steps.append(step)
    return steps


def _string_list(raw: Any) -> List[str]:
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


@register_skill
class PlanSkill(BaseSkill):
    name = "FERAL Plan"
    description = "Submit a proposed plan for user review."
    safety_level = "SAFE"

    def __init__(self) -> None:
        super().__init__(skill_id="plan")

    async def execute(
        self,
        endpoint_id: str,
        args: Dict[str, Any],
        vault: Dict[str, str],
    ) -> Dict[str, Any]:
        del vault
        if endpoint_id == "submit":
            return self._submit(args)
        return {
            "success": False,
            "status_code": 404,
            "data": None,
            "error": f"Unknown endpoint: {endpoint_id}",
        }

    def _submit(self, args: Dict[str, Any]) -> Dict[str, Any]:
        summary = str(args.get("summary") or "").strip()
        steps = _normalize_steps(args.get("steps"))
        if not summary:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": "A plan needs a 'summary'.",
                "error_code": "missing_required_field",
                "field": "summary",
            }
        if not steps:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": "A plan needs a non-empty 'steps' list.",
                "error_code": "missing_required_field",
                "field": "steps",
            }

        plan = {
            "summary": summary,
            "steps": steps,
            "open_questions": _string_list(args.get("open_questions")),
            "risks": _string_list(args.get("risks")),
        }

        session_id = _session_id(args)
        state = _get_plan_state()
        stored: Optional[Dict[str, Any]] = None
        if state is not None and session_id:
            try:
                stored = state.record_plan(session_id, plan)
            except Exception as exc:
                logger.warning("plan record failed for %s: %s", session_id[:8], exc)
        if stored is None:
            stored = dict(plan)

        return {
            "success": True,
            "status_code": 200,
            "data": {
                "plan": stored,
                "session_id": session_id,
                # Submitting does not leave plan mode and does not
                # pre-approve anything. Say so in the tool result too, not
                # just in the system prompt, because the tool result is
                # what the model reads most recently before it replies.
                "plan_mode": True,
                "awaiting": "user_review",
                "note": (
                    "Plan recorded and shown to the user. You are still in "
                    "plan mode and still cannot run mutating tools. Only the "
                    "user can leave plan mode, and approving this plan does "
                    "not pre-approve its steps."
                ),
            },
            "error": None,
        }
