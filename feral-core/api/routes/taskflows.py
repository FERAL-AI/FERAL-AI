"""TaskFlow CRUD endpoints."""

from fastapi import APIRouter

from api.state import state

router = APIRouter()


@router.post("/api/taskflows")
async def create_taskflow(body: dict):
    """Create a persistent background TaskFlow."""
    if not state.taskflows:
        return {"error": "TaskFlow runtime not initialized"}
    steps = body.get("steps", [])
    if not isinstance(steps, list) or not steps:
        return {"error": "steps (non-empty list) is required"}
    session_id = body.get("session_id", "")
    title = body.get("title", "Background TaskFlow")
    context = body.get("context", {})
    try:
        flow = state.taskflows.create_flow(
            session_id=session_id,
            title=title,
            steps=steps,
            context=context,
        )
        return flow
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/taskflows")
async def list_taskflows(status: str = "", session_id: str = "", limit: int = 50):
    if not state.taskflows:
        return {"flows": []}
    return {"flows": state.taskflows.list_flows(status=status, session_id=session_id, limit=limit)}


@router.get("/api/taskflows/{flow_id}")
async def get_taskflow(flow_id: str):
    if not state.taskflows:
        return {"error": "TaskFlow runtime not initialized"}
    flow = state.taskflows.get_flow(flow_id)
    if not flow:
        return {"error": f"TaskFlow not found: {flow_id}"}
    return flow


@router.post("/api/taskflows/{flow_id}/resume")
async def resume_taskflow(flow_id: str):
    if not state.taskflows:
        return {"error": "TaskFlow runtime not initialized"}
    flow = state.taskflows.resume_flow(flow_id)
    if not flow:
        return {"error": f"TaskFlow not found: {flow_id}"}
    return flow


@router.post("/api/taskflows/{flow_id}/cancel")
async def cancel_taskflow(flow_id: str):
    if not state.taskflows:
        return {"error": "TaskFlow runtime not initialized"}
    flow = state.taskflows.cancel_flow(flow_id)
    if not flow:
        return {"error": f"TaskFlow not found: {flow_id}"}
    return flow


# ── Agent-facing convenience surface (long-horizon tasks) ──
#
# The raw /api/taskflows endpoint requires the caller to hand-author a
# list of step dicts. These thin wrappers let the LLM (via the `task`
# skill) kick off and monitor genuinely long-running, restart-safe
# background work from a plain natural-language goal. Each goal/subtask
# becomes an ``llm.chat`` step, which the TaskFlow runner drives through
# ``orchestrator.handle_command`` with the full (default 900s, unlimited
# iterations) tool budget — so the agent keeps working autonomously even
# after the user navigates away or the brain restarts.

@router.post("/internal/task/start")
async def start_background_task(body: dict):
    """Launch a persistent background task from a natural-language goal.

    Body: ``{goal?: str, subtasks?: [str], title?: str, session_id?: str}``.
    Provide either a single ``goal`` (one autonomous background turn) or
    an ordered list of ``subtasks`` (run sequentially, each its own
    autonomous turn). Returns ``{ok, flow_id, steps, status, title}``.
    """
    if not state.taskflows:
        return {"ok": False, "error": "TaskFlow runtime not initialized"}
    body = body or {}
    goal = str(body.get("goal", "")).strip()
    raw_subtasks = body.get("subtasks") or []
    prompts: list[str] = []
    if isinstance(raw_subtasks, list) and raw_subtasks:
        prompts = [str(s).strip() for s in raw_subtasks if str(s).strip()]
    elif goal:
        prompts = [goal]
    if not prompts:
        return {"ok": False, "error": "provide a non-empty 'goal' string or 'subtasks' list"}

    title = str(body.get("title") or goal or "Background task")[:120]
    # Empty session_id makes each llm.chat step run under a dedicated
    # ``taskflow-<id>`` session (see TaskFlowRuntime._execute_step) so
    # background work never blocks or pollutes the user's live chat
    # thread. Callers may pass an explicit session_id to attach it.
    session_id = str(body.get("session_id", "") or "")
    steps = [{"type": "llm.chat", "prompt": p} for p in prompts]
    try:
        flow = state.taskflows.create_flow(
            session_id=session_id,
            title=title,
            steps=steps,
            context={"goal": goal or title},
        )
        return {
            "ok": True,
            "flow_id": flow["id"],
            "steps": len(steps),
            "status": flow["status"],
            "title": flow["title"],
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/internal/task/status")
async def background_task_status(flow_id: str):
    """Check a background task's progress: status + per-step state."""
    if not state.taskflows:
        return {"error": "TaskFlow runtime not initialized"}
    flow = state.taskflows.get_flow(flow_id)
    if not flow:
        return {"error": f"task not found: {flow_id}"}
    steps = flow.get("steps", [])
    done = sum(1 for s in steps if s.get("status") == "completed")
    return {
        "flow_id": flow["id"],
        "title": flow["title"],
        "status": flow["status"],
        "current_step": flow["current_step"],
        "steps_total": len(steps),
        "steps_completed": done,
        "error": flow.get("error"),
    }


@router.get("/internal/task/list")
async def background_task_list(limit: int = 20):
    """List recent background tasks (most recent first)."""
    if not state.taskflows:
        return {"tasks": []}
    flows = state.taskflows.list_flows(limit=max(1, min(limit, 100)))
    return {
        "tasks": [
            {
                "flow_id": f["id"],
                "title": f["title"],
                "status": f["status"],
                "current_step": f["current_step"],
            }
            for f in flows
        ]
    }
