"""FERAL Workflows skill — author/run multi-step workflows as an LLM tool.

Thin delegate over :class:`agents.taskflow.TaskFlowRuntime`. Authoring a
workflow from chat creates the same TaskFlow rows the REST/pack paths create,
so the runner, templating (L4), and safety pre-flight (L2) all apply uniformly.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from skills.base import BaseSkill
from skills.impl import register_skill


# Optional test/embedding overrides.
_TASKFLOWS_OVERRIDE: Any = None
_PACKS_OVERRIDE: Any = None


def set_overrides(taskflows: Any = None, packs: Any = None) -> None:
    global _TASKFLOWS_OVERRIDE, _PACKS_OVERRIDE
    _TASKFLOWS_OVERRIDE = taskflows
    _PACKS_OVERRIDE = packs


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

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        del vault
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
