"""FERAL Workflows skill — author/run multi-step workflows as an LLM tool.

Thin delegate over :class:`agents.taskflow.TaskFlowRuntime`. Authoring a
workflow from chat creates the same TaskFlow rows the REST/pack paths create,
so the runner, templating (L4), and safety pre-flight (L2) all apply uniformly.
"""
from __future__ import annotations

from typing import Any, Dict

from skills.base import BaseSkill
from skills.impl import register_skill
from skills.impl.todo_store import TodoValidationError, get_store


# Optional test/embedding overrides.
_TASKFLOWS_OVERRIDE: Any = None
_PACKS_OVERRIDE: Any = None
_TODO_STORE_OVERRIDE: Any = None


def set_overrides(taskflows: Any = None, packs: Any = None) -> None:
    global _TASKFLOWS_OVERRIDE, _PACKS_OVERRIDE
    _TASKFLOWS_OVERRIDE = taskflows
    _PACKS_OVERRIDE = packs


def _get_todo_store() -> Any:
    return _TODO_STORE_OVERRIDE if _TODO_STORE_OVERRIDE is not None else get_store()


def _todo_session_id(args: Dict[str, Any]) -> str:
    """Prefer the ambient call context over the model-supplied value, so
    the model cannot write a list into someone else's session."""
    try:
        from skills.call_context import current_context
        bound = getattr(current_context(), "session_id", "") or ""
    except Exception:
        bound = ""
    return bound or str(_arg_value(args, "session_id") or "")


def _get_taskflows() -> Any:
    if _TASKFLOWS_OVERRIDE is not None:
        return _TASKFLOWS_OVERRIDE
    try:
        from api.state import state as _state
    except Exception:
        return None
    return getattr(_state, "taskflows", None)


def _get_packs() -> dict:
    if _PACKS_OVERRIDE is not None:
        return _PACKS_OVERRIDE
    try:
        from api.state import state as _state
    except Exception:
        return {}
    return getattr(_state, "workflow_packs", {}) or {}


def _arg_value(args: Dict[str, Any], *keys: str) -> Any:
    values = args.get("values") if isinstance(args.get("values"), dict) else None
    for key in keys:
        if key in args and args.get(key) is not None:
            return args.get(key)
        if values is not None and key in values and values.get(key) is not None:
            return values.get(key)
    return None


@register_skill
class FeralWorkflowsSkill(BaseSkill):
    name = "FERAL Workflows"
    description = "Author and run multi-step workflows."
    safety_level = "SAFE"

    def __init__(self) -> None:
        super().__init__(skill_id="feral_workflows")

    @staticmethod
    def _intent_compiler():
        from api.state import state
        return getattr(state, "intent_compiler", None)

    def _list_commitments(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Everything the user has promised and not yet done.

        The briefing shows at most three (api/routes/ambient.py:103), so
        this is the only way to see the rest, and the count makes the
        truncation visible instead of silent.
        """
        compiler = self._intent_compiler()
        if compiler is None:
            return {"success": False, "status_code": 503, "data": None,
                    "error": "Intent compiler is not available."}
        include_done = bool(args.get("include_completed"))
        rows = []
        for plan in compiler.list_plans():
            if plan["status"] != "active" and not include_done:
                continue
            rows.append({
                "commitment": plan["intent"],
                "status": plan["status"],
                "created": plan["created"],
                "plan_id": plan["plan_id"],
            })
        rows.sort(key=lambda r: r["created"], reverse=True)
        return {
            "success": True, "status_code": 200,
            "data": {"commitments": rows, "count": len(rows)},
            "error": None,
        }

    def _complete_commitment(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Mark a promise done by describing it, not by id.

        Refuses an ambiguous match rather than guessing: completing the
        wrong commitment removes it from every surface silently, which
        is worse than asking again.
        """
        compiler = self._intent_compiler()
        if compiler is None:
            return {"success": False, "status_code": 503, "data": None,
                    "error": "Intent compiler is not available."}
        text = str(args.get("text") or "").strip()
        if not text:
            return {"success": False, "status_code": 400, "data": None,
                    "error": "`text` is required: describe the commitment to complete."}
        result = compiler.complete_commitment(text)
        if result is None:
            active = [p["intent"] for p in compiler.list_plans() if p["status"] == "active"]
            return {
                "success": False, "status_code": 404, "data": {"active": active[:10]},
                "error": (
                    f"No single active commitment matches {text!r}. "
                    "Say more of the wording, or pick one of the active ones."
                ),
            }
        return {"success": True, "status_code": 200, "data": result, "error": None}

    # Endpoints that need the TaskFlow runtime, and the full set this skill
    # answers. Kept as data so the 404 message can name what IS available and
    # so a declared-but-unrouted endpoint is impossible to add silently.
    TASKFLOW_ENDPOINTS = frozenset({
        "create", "list", "get", "cancel", "instantiate_pack",
    })
    LOCAL_ENDPOINTS = frozenset({
        "todo_write", "list_commitments", "complete_commitment",
    })
    ALL_ENDPOINTS = TASKFLOW_ENDPOINTS | LOCAL_ENDPOINTS

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        del vault
        # Handled BEFORE the TaskFlow-runtime check on purpose. The todo
        # list is the agent's own scratch state, not a TaskFlow, so it must
        # not 503 on an install where the runtime is absent.
        if endpoint_id == "todo_write":
            return self._todo_write(args)

        # Commitments live on IntentCompiler, not in the TaskFlow
        # runtime, so like todo_write they are handled before the
        # runtime check and must not 503 when it is absent. They are
        # here rather than on a new top-level skill because the router
        # already reaches this manifest for "what am I meant to be
        # doing" phrasings, and one more skill competing on those
        # phrases costs more than it buys.
        if endpoint_id == "list_commitments":
            return self._list_commitments(args)
        if endpoint_id == "complete_commitment":
            return self._complete_commitment(args)

        # Validate the endpoint name BEFORE the runtime check. Otherwise a
        # typo returns 503 "TaskFlow runtime is not available", which blames
        # a subsystem for a caller error and sends whoever is debugging it
        # looking at Docker. On macOS, where the runtime is absent by
        # default, every unknown endpoint reported the wrong cause.
        if endpoint_id not in self.TASKFLOW_ENDPOINTS:
            return {
                "success": False,
                "status_code": 404,
                "data": None,
                "error": (
                    f"Unknown endpoint: {endpoint_id}. This skill exposes: "
                    + ", ".join(sorted(self.ALL_ENDPOINTS))
                ),
            }

        taskflows = _get_taskflows()
        if taskflows is None:
            return {
                "success": False,
                "status_code": 503,
                "data": None,
                "error": "TaskFlow runtime is not available.",
            }

        if endpoint_id == "create":
            return self._create(taskflows, args)
        if endpoint_id == "list":
            return self._list(taskflows, args)
        if endpoint_id == "get":
            return self._get(taskflows, args)
        if endpoint_id == "cancel":
            return self._cancel(taskflows, args)
        if endpoint_id == "instantiate_pack":
            return self._instantiate_pack(taskflows, args)

        return {"success": False, "status_code": 404, "data": None, "error": f"Unknown endpoint: {endpoint_id}"}

    def _create(self, taskflows, args: Dict[str, Any]) -> Dict[str, Any]:
        title = str(_arg_value(args, "title") or "").strip()
        steps = _arg_value(args, "steps")
        if not isinstance(steps, list) or not steps:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": "Workflow requires a non-empty 'steps' list.",
                "reason": "missing_required_field",
                "field": "steps",
            }
        session_id = str(_arg_value(args, "session_id") or "")
        context = _arg_value(args, "context")
        context = context if isinstance(context, dict) else None
        try:
            flow = taskflows.create_flow(
                session_id=session_id,
                title=title or "Workflow",
                steps=steps,
                context=context,
            )
        except ValueError as exc:
            return {"success": False, "status_code": 400, "data": None, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "status_code": 500, "data": None, "error": str(exc)}
        return {"success": True, "status_code": 200, "data": {"flow": flow}, "error": None}

    def _list(self, taskflows, args: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(_arg_value(args, "session_id") or "")
        status = str(_arg_value(args, "status") or "")
        flows = taskflows.list_flows(session_id=session_id, status=status)
        return {
            "success": True,
            "status_code": 200,
            "data": {"flows": flows, "count": len(flows)},
            "error": None,
        }

    def _get(self, taskflows, args: Dict[str, Any]) -> Dict[str, Any]:
        flow_id = str(_arg_value(args, "flow_id", "id") or "").strip()
        if not flow_id:
            return {"success": False, "status_code": 400, "data": None, "error": "flow_id is required", "field": "flow_id"}
        flow = taskflows.get_flow(flow_id)
        if flow is None:
            return {"success": False, "status_code": 404, "data": None, "error": f"Workflow {flow_id} not found"}
        return {"success": True, "status_code": 200, "data": {"flow": flow}, "error": None}

    def _cancel(self, taskflows, args: Dict[str, Any]) -> Dict[str, Any]:
        flow_id = str(_arg_value(args, "flow_id", "id") or "").strip()
        if not flow_id:
            return {"success": False, "status_code": 400, "data": None, "error": "flow_id is required", "field": "flow_id"}
        flow = taskflows.cancel_flow(flow_id)
        if flow is None:
            return {"success": False, "status_code": 404, "data": None, "error": f"Workflow {flow_id} not found"}
        return {"success": True, "status_code": 200, "data": {"flow": flow}, "error": None}

    def _todo_write(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Replace this session's todo list wholesale and echo it back.

        Full replacement rather than incremental patches is the point: the
        model rewrites the list from what it last saw, so an incremental
        API guarantees the model's view and the store drift apart. The
        server enforces at most one ``in_progress`` item because a focus
        mechanism that only asks nicely is a wish list.

        The echo is what the model reads on the next turn, so it must
        survive the result budget intact. ``todo_write`` declares the
        ``feed`` tier in the manifest (50 list items) and
        ``todo_store.MAX_TODO_ITEMS`` caps the list at 40, strictly under
        it. Without both halves the endpoint would inherit ``standard`` at
        20 items and a longer list would come back truncated, after which
        the model rewrites from the truncated view and silently loses the
        tail.
        """
        raw = _arg_value(args, "todos")
        if raw is None:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": "todo_write requires a 'todos' array (the COMPLETE list).",
                "error_code": "missing_required_field",
                "field": "todos",
            }

        session_id = _todo_session_id(args)
        store = _get_todo_store()
        try:
            rows = store.replace(session_id, raw)
        except TodoValidationError as exc:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": exc.message,
                "error_code": exc.error_code,
                "field": "todos",
            }

        return {
            "success": True,
            "status_code": 200,
            "data": {
                "todos": rows,
                "counts": store.counts(rows),
                "session_id": session_id,
            },
            "error": None,
        }

    def _instantiate_pack(self, taskflows, args: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = str(_arg_value(args, "workflow_id", "pack_id") or "").strip()
        if not workflow_id:
            return {"success": False, "status_code": 400, "data": None, "error": "workflow_id is required", "field": "workflow_id"}
        packs = _get_packs()
        pack = packs.get(workflow_id)
        if pack is None:
            return {"success": False, "status_code": 404, "data": None, "error": f"Unknown workflow pack '{workflow_id}'"}
        session_id = _arg_value(args, "session_id")
        title = _arg_value(args, "title")
        try:
            steps = [step.model_dump() for step in pack.steps]
            flow = taskflows.create_flow(
                session_id=str(session_id) if session_id else f"pack-{workflow_id}",
                title=str(title) if title else pack.name,
                steps=steps,
                context={"instantiated_from": "workflow_pack", "workflow_id": workflow_id},
            )
        except Exception as exc:
            return {"success": False, "status_code": 500, "data": None, "error": str(exc)}
        return {"success": True, "status_code": 200, "data": {"flow": flow, "workflow_id": workflow_id}, "error": None}
