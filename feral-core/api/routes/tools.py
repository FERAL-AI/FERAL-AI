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

from fastapi import APIRouter

from api.state import state
from skills.call_context import bind_context

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

    # Bind session identity before dispatch. The executor's plan-mode and
    # approval gates read the session from the ToolCallContext contextvar,
    # so without this the route is invisible to plan mode: a live probe
    # showed a mutating call reaching the executor with session_id="" while
    # that session was demonstrably in plan mode. The route already accepts
    # a session_id in the body and was simply dropping it.
    #
    # surface is "http_api" to match the policy decision computed above, so
    # a refusal names the same surface the caller was judged on.
    _session_id = str(body.get("session_id") or "").strip()
    with bind_context(
        session_id=_session_id,
        surface="http_api",
        tool_name=canonical,
    ):
        result = await state.skill_executor.execute(canonical, args, manifest, endpoint)
    out = {"tool_name": canonical}
    if isinstance(result, dict):
        out.update(result)
    else:
        out["data"] = result
    return out
