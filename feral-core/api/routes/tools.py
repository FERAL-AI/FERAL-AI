"""L6 — canonical external tool surface.

Lets an external agent (e.g. Claude Code) enumerate and invoke ANY registered
skill/device through REST, delegating to the SAME ``SkillExecutor`` the
in-process orchestrator uses. Auth is handled by the global APIKeyMiddleware
(these live under ``/api`` like every other protected route).

Endpoints:
  * ``GET  /api/tools``          — enumerate the registry's LLM tool catalog.
  * ``POST /api/tools/execute``  — invoke a tool by ``tool_name`` (skill__endpoint)
                                   or ``skill_id`` + ``endpoint``, behind the
                                   surface="http_api" safety policy.
"""

from contextlib import nullcontext
from typing import Any

from fastapi import APIRouter

from api.state import state
from memory.runtime_session_checkpoint import CheckpointValidationError, validate_session_id
from skills.call_context import bind_context, context_enabled, current_context

router = APIRouter(tags=["tools"])


def _resolve(skill_id: str, endpoint_id: str):
    """Return (manifest, endpoint) or (None, None)."""
    reg = state.skill_registry
    if reg is None:
        return None, None
    manifest = reg.skills.get(skill_id)
    if manifest is None:
        return None, None
    for ep in manifest.endpoints:
        if ep.id == endpoint_id:
            return manifest, ep
    return manifest, None


@router.get("/api/tools")
async def list_tools(skill_id: str = ""):
    """Enumerate registered tools in the LLM function-calling format.

    Optionally filter to a single ``skill_id``. This is the canonical
    capability view external agents read before invoking a tool.
    """
    reg = state.skill_registry
    if reg is None:
        return {"count": 0, "tools": []}
    if skill_id:
        tools = reg.get_tools_for_skills([reg.skills[skill_id]]) if skill_id in reg.skills else []
    else:
        tools = reg.get_all_tools()
    return {"count": len(tools), "tools": tools}


@router.post("/api/tools/execute")
async def execute_tool(body: dict):
    """Invoke a registered skill endpoint.

    Body:
      * ``tool_name``: "skill_id__endpoint_id" (preferred), OR
      * ``skill_id`` + ``endpoint`` (alias ``endpoint_id``)
      * ``args``: object passed to the endpoint
      * ``confirm``: bool — required to run a CONFIRM-tier tool over REST
      * ``session_id``: canonical caller session; required except for trusted reads
    """
    if state.skill_registry is None or state.skill_executor is None:
        return {"success": False, "status_code": 503, "error": "Skill subsystem not ready"}

    tool_name = (body.get("tool_name") or "").strip()
    skill_id = (body.get("skill_id") or "").strip()
    endpoint_id = (body.get("endpoint") or body.get("endpoint_id") or "").strip()
    args = body.get("args") or {}

    if tool_name and not (skill_id and endpoint_id):
        # Split on the LAST "__" so skill ids containing "__" still resolve.
        skill_id, sep, endpoint_id = tool_name.rpartition("__")
        if not sep:
            skill_id, endpoint_id = tool_name, ""
    if not skill_id or not endpoint_id:
        return {
            "success": False,
            "status_code": 400,
            "error": "Provide tool_name ('skill__endpoint') or skill_id + endpoint.",
        }

    canonical = f"{skill_id}__{endpoint_id}"
    manifest, endpoint = _resolve(skill_id, endpoint_id)
    if manifest is None:
        return {"success": False, "status_code": 404, "error": f"Unknown skill '{skill_id}'"}
    if endpoint is None:
        return {"success": False, "status_code": 404, "error": f"Unknown endpoint '{endpoint_id}' on '{skill_id}'"}

    # Safety policy on the external surface.
    try:
        from security.safety_resolver import resolve_policy, LEVEL_DENY, LEVEL_CONFIRM
        decision = resolve_policy(canonical, args, surface="http_api", registry=state.skill_registry)
    except Exception:
        decision = None

    if decision is not None and decision.level == LEVEL_DENY:
        return {
            "success": False,
            "status_code": 403,
            "error": f"Tool '{canonical}' denied on surface 'http_api': {decision.deny_reason}",
            "policy": decision.to_dict(),
        }
    if decision is not None and decision.level == LEVEL_CONFIRM and not body.get("confirm"):
        return {
            "success": False,
            "status_code": 412,
            "error": f"Tool '{canonical}' requires confirmation. Re-send with \"confirm\": true.",
            "policy": decision.to_dict(),
        }

    # Anonymous reads retain their legacy contract. Mutation and approval
    # dispatch must have the caller's real identity before reaching the executor.
    from security.safety_resolver import LEVEL_AUTO, is_read_only

    anonymous_read = (decision is not None and decision.level == LEVEL_AUTO
                      and is_read_only(canonical, registry=state.skill_registry, strict=True))
    _session_id: Any = body.get("session_id")
    if "session_id" in body or not anonymous_read:
        try:
            validate_session_id(_session_id)
        except CheckpointValidationError:
            return {
                "success": False, "status_code": 422,
                "error_code": "context_invalid_session",
                "error": "Supply the exact caller session_id before dispatching this tool.",
            }
    else:
        _session_id = ""
    if not anonymous_read and not context_enabled():
        return {
            "success": False, "status_code": 409,
            "error_code": "context_binding_disabled",
            "error": "Session identity binding is disabled. Enable it before dispatching this tool.",
        }

    # Bind session identity before dispatch. The executor's plan-mode and
    # approval gates read the session from the ToolCallContext contextvar,
    # so without this the route is invisible to plan mode: a live probe
    # showed a mutating call reaching the executor with session_id="" while
    # that session was demonstrably in plan mode. The route already accepts
    # a session_id in the body and was simply dropping it.
    #
    # surface is "http_api" to match the policy decision computed above, so
    # a refusal names the same surface the caller was judged on.
    with bind_context(
        session_id=_session_id,
        surface="http_api",
        tool_name=canonical,
    ):
        if not anonymous_read and current_context().session_id != _session_id:
            return {
                "success": False, "status_code": 409,
                "error_code": "context_binding_unavailable",
                "error": "The caller session could not be bound. No tool was dispatched.",
            }
        from agents.tool_runner import _BrowserResourceError
        runner = getattr(state.orchestrator, "tool_runner", None)
        scope = (runner.browser_resource_scope(canonical, _session_id)
                 if runner is not None and callable(getattr(type(runner), "browser_resource_scope", None)) else nullcontext())
        try:
            with scope:
                result = await state.skill_executor.execute(canonical, args, manifest, endpoint)
        except _BrowserResourceError:
            return {
                "success": False, "status_code": 409,
                "error_code": "browser_resource_changed",
                "error": "Browser admission changed. Inspect the connection and exact action before retrying.",
            }
    out = {"tool_name": canonical}
    if isinstance(result, dict):
        out.update(result)
    else:
        out["data"] = result
    return out
