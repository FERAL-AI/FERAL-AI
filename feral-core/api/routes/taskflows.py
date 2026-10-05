"""TaskFlow CRUD endpoints."""

import asyncio
import hashlib
import json

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
# becomes an ``llm.chat`` step through the existing bounded runner. Creation
# does not grant action permission or certify an external outcome.

_ORIGIN_FIELDS = {"origin", "handoff_key", "terms_digest", "request_id", "turn_id",
                  "input_revision", "context_checkpoint", "owner_id", "tool_call_id"}
_CREATION_WORKERS: set[asyncio.Task] = set()


class _TaskRequestError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _failure(code, message):
    return {"ok": False, "error_code": code, "error": message}


async def _owned_thread_call(function, *args):
    """Retain bounded worker ownership when caller cancellation wins the await."""
    if len(_CREATION_WORKERS) >= 32:
        raise _TaskRequestError("task_runtime_busy", "Task control-plane workers are at capacity.")
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    _CREATION_WORKERS.add(worker)

    def completed(task):
        _CREATION_WORKERS.discard(task)
        if not task.cancelled():
            task.exception()  # Consume failures without private task details.
    worker.add_done_callback(completed)
    return await asyncio.shield(worker)


def _task_terms(body):
    if not isinstance(body, dict) or len(body) > 32 or _ORIGIN_FIELDS.intersection(body):
        raise _TaskRequestError("task_invalid_request", "Task origin is supplied by the runtime, not request fields.")
    goal = body.get("goal", "")
    raw_subtasks = body.get("subtasks", [])
    if raw_subtasks is None:
        raw_subtasks = []
    if not isinstance(goal, str) or len(goal) > 16384 or not isinstance(raw_subtasks, list) or len(raw_subtasks) > 32:
        raise _TaskRequestError("task_invalid_request", "Provide a bounded goal or at most 32 subtasks.")
    if any(not isinstance(p, str) or not p.strip() or len(p) > 16384 for p in raw_subtasks):
        raise _TaskRequestError("task_invalid_request", "Each subtask must be a non-empty bounded string.")
    goal = goal.strip()
    prompts = [p.strip() for p in raw_subtasks] or ([goal] if goal else [])
    if not prompts:
        raise _TaskRequestError("task_invalid_request", "provide a non-empty 'goal' string or 'subtasks' list")
    try:
        input_bytes = sum(len(p.encode("utf-8")) for p in prompts)
    except UnicodeEncodeError:
        raise _TaskRequestError("task_invalid_request", "Task input must be valid Unicode text.") from None
    if input_bytes > 65536:
        raise _TaskRequestError("task_invalid_request", "Task input exceeds the bounded creation budget.")
    title = body.get("title") or goal or "Background task"
    if not isinstance(title, str):
        raise _TaskRequestError("task_invalid_request", "Task title must be a string.")
    session_id = body.get("session_id", "")
    if not isinstance(session_id, str):
        raise _TaskRequestError("task_invalid_request", "Invalid task execution session.")
    if session_id:
        from security.session_identity import validate_session_id
        try:
            validate_session_id(session_id)
        except (ValueError, TypeError):
            raise _TaskRequestError("task_invalid_request", "Invalid task execution session.") from None
    return {"goal": goal, "title": title[:120], "session_id": session_id,
            "steps": [{"type": "llm.chat", "prompt": p} for p in prompts]}


async def _skill_origin(endpoint_id, args=None):
    """Correlate actual tracked receipts, without inventing authentication.

    The executor's child task inherits the audit. Revalidate its live record and
    connection after the receipt read; the context alone is never authority.
    """
    from agents import chat_turns
    from agents.chat_turns import ChatTurnManager, exact_uuid, turn_audit
    from security.dangerous_tools import known_surfaces
    from security.agent_turn_lease import guard_agent_dispatch
    from skills.call_context import context_enabled, current_context
    if not context_enabled():
        raise _TaskRequestError("task_origin_unavailable", "Task identity context is disabled.")
    ctx = current_context()
    if endpoint_id == "start":
        from agents.chat_turns import ChatTurnError, claim_task_origin_transfer
        try:
            transferred = claim_task_origin_transfer(args)
        except ChatTurnError as exc:
            raise _TaskRequestError("task_origin_superseded", "Original task approval is no longer current.") from exc
        if transferred is not None:
            return transferred
    caller_task = asyncio.current_task()

    def caller_guard():
        guard_agent_dispatch()
        if caller_task is None or caller_task.cancelling() or caller_task.done():
            raise asyncio.CancelledError()
    if ctx.session_id and ctx.tool_name != f"background_task__{endpoint_id}":
        raise _TaskRequestError("task_origin_invalid", "Background task caller identity does not match.")
    audit = turn_audit(ctx.session_id)
    if chat_turns._audit.get() is not None and audit is None:
        raise _TaskRequestError("task_origin_superseded", "The originating tracked turn does not match this caller.")
    if audit is None:
        if endpoint_id in {"status", "list"} and not ctx.session_id:
            raise _TaskRequestError("task_origin_unavailable", "Agent task inspection requires an originating session.")
        if ctx.surface not in known_surfaces():
            raise _TaskRequestError("task_origin_unavailable", "Agent handoff requires a known originating surface.")
        caller_guard()
        return {"contract_version": 1, "source": "legacy_agent_context", "owner_verified": False,
                "session_id": ctx.session_id or None, "request_id": None, "turn_id": None,
                "surface": ctx.surface, "input_revision": None}, caller_guard
    manager = getattr(state, "chat_turns", None)

    def guard():
        caller_guard()
        live = manager._live.get((audit.session_id, audit.turn_id)) if isinstance(manager, ChatTurnManager) else None
        if (not isinstance(manager, ChatTurnManager) or manager.state is not state
                or manager._store is not getattr(state, "memory", None)
                or live is None or live.audit is not audit or live.request_id != audit.request_id
                or live.task is None or live.task.done() or live.task.cancelling()
                or audit.closed or audit.cancel_requested
                or getattr(state, "sessions", {}).get(audit.session_id) is not live.owner):
            raise _TaskRequestError("task_origin_superseded", "The originating tracked turn is no longer current.")
    guard()
    if ctx.surface not in known_surfaces():
        raise _TaskRequestError("task_origin_invalid", "Tracked handoff requires a known execution surface.")
    try:
        exact_uuid(audit.request_id)
        exact_uuid(audit.turn_id)
        receipt = await state.memory.chat_turn_get(session_id=audit.session_id,
                                                   request_id=audit.request_id, turn_id=audit.turn_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _TaskRequestError("task_origin_unavailable", "The originating receipt could not be read.") from None
    guard()
    if (not isinstance(receipt, dict) or receipt.get("contract_version") != 1 or receipt.get("durable") is not True
            or receipt.get("session_id") != audit.session_id or receipt.get("request_id") != audit.request_id
            or receipt.get("turn_id") != audit.turn_id or receipt.get("status") not in {"accepted", "running"}):
        raise _TaskRequestError("task_origin_unavailable", "The originating durable receipt is not active.")
    if endpoint_id == "start" and (not isinstance(ctx.call_id, str) or not ctx.call_id
                                   or len(ctx.call_id) > 256 or any(ord(c) < 32 for c in ctx.call_id)):
        raise _TaskRequestError("task_origin_unavailable", "A stable tool-call identity is required for tracked handoff.")
    return {"contract_version": 1, "source": "tracked_chat_turn", "owner_verified": True,
            "session_id": audit.session_id, "request_id": audit.request_id, "turn_id": audit.turn_id,
            "tool_call_id": ctx.call_id, "surface": ctx.surface,
            "input_revision": None, "context_commit": "pending"}, guard


def _start_task(body, origin, guard, runtime):
    if not runtime:
        return _failure("task_runtime_unavailable", "TaskFlow runtime not initialized")
    try:
        terms = _task_terms(body)
        # Origin identifies the conversation; detached execution uses its own SID.
        if origin["source"] != "legacy_local_operator":
            if terms["session_id"]:
                raise _TaskRequestError("task_invalid_request", "Agent background work must use a dedicated execution session.")
        digest_terms = {**terms, "origin_surface": origin["surface"]} if origin["source"] == "tracked_chat_turn" else terms
        digest = hashlib.sha256(json.dumps(digest_terms, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode()).hexdigest()
        context = {"goal": terms["goal"] or terms["title"], "task_origin": origin}
        kwargs = {}
        from agents.taskflow import TaskFlowRuntime
        if origin["source"] != "legacy_local_operator" and not isinstance(runtime, TaskFlowRuntime):
            return _failure("task_runtime_unavailable", "Agent handoff requires the durable TaskFlow creation contract.")
        if isinstance(runtime, TaskFlowRuntime):
            kwargs = {"creation_guard": guard}
            if origin.get("session_id"):
                kwargs["origin_session_id"] = origin["session_id"]
            if origin["source"] != "legacy_local_operator":
                kwargs["origin_surface"] = origin["surface"]
        if origin["source"] == "tracked_chat_turn":
            if not isinstance(runtime, TaskFlowRuntime):
                return _failure("task_runtime_unavailable", "Tracked handoff requires the durable TaskFlow creation contract.")
            identity = [origin[k] for k in ("session_id", "request_id", "turn_id", "tool_call_id")]
            kwargs.update(handoff_key=hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest(),
                          terms_digest=digest)
            context["task_origin"] = {**origin, "handoff_key": kwargs["handoff_key"], "terms_digest": digest}
        guard()
        flow = runtime.create_flow(session_id=terms["session_id"], title=terms["title"],
                                          steps=terms["steps"], context=context, **kwargs)
        readback = runtime.get_flow(flow["id"])
        if (not readback or readback.get("context", {}).get("task_origin") != context["task_origin"]
                or isinstance(runtime, TaskFlowRuntime) and (
                    runtime.origin_session_for_flow(flow["id"]) != origin.get("session_id")
                    or runtime.origin_surface_for_flow(flow["id"]) != origin.get("surface"))):
            return _failure("task_creation_outcome_unknown", "Creation readback failed. Inspect task status before retrying.")
        return {"ok": True, "flow_id": readback["id"], "steps": len(terms["steps"]),
                "status": readback["status"], "title": readback["title"],
                "handoff": {"contract_version": 1, "accepted": True, "durable": True,
                            "replayed": flow.get("handoff_replayed", False), "origin": context["task_origin"],
                            "replay_protected": origin["source"] == "tracked_chat_turn",
                            "execution_session": "dedicated" if not terms["session_id"] else "legacy_explicit",
                            "action_outcome": "not_asserted", "result_subscription": "not_attached"}}
    except _TaskRequestError as exc:
        return _failure(exc.code, str(exc))
    except Exception as exc:
        from agents.taskflow import TaskFlowHandoffConflict
        if isinstance(exc, TaskFlowHandoffConflict):
            return _failure("task_handoff_conflict", "This handoff identity was already accepted with different terms.")
        return _failure("task_creation_outcome_unknown", "Task creation could not be confirmed. Inspect status before retrying.")


async def execute_background_task_skill(endpoint_id, args):
    """In-process backing adapter. Existing executor remains the policy owner."""
    try:
        if not isinstance(args, dict):
            raise _TaskRequestError("task_invalid_request", "Task arguments must be an object.")
        origin, guard = await _skill_origin(endpoint_id, args)
        brain = state
        runtime = state.taskflows
        source_guard = guard

        def guard():
            source_guard()
            if state is not brain or state.taskflows is not runtime:
                raise _TaskRequestError("task_origin_superseded", "The originating TaskFlow runtime changed.")
        if endpoint_id == "start":
            result = await _owned_thread_call(_start_task, args, origin, guard, runtime)
            try:
                guard()
            except _TaskRequestError:
                if result.get("ok"):
                    return {**_failure("task_creation_outcome_unknown", "Creation committed after its owner changed. Reconcile the recorded flow."),
                            "flow_id": result["flow_id"]}
                raise
            return result
        if endpoint_id == "status":
            flow_id = args.get("flow_id", "")
            if not isinstance(flow_id, str) or not flow_id or len(flow_id) > 128:
                raise _TaskRequestError("task_invalid_request", "A bounded flow_id is required.")
            def read_status():
                if not runtime:
                    return _failure("task_runtime_unavailable", "TaskFlow runtime not initialized")
                if origin.get("session_id") and runtime.origin_session_for_flow(flow_id) != origin["session_id"]:
                    return _failure("task_not_found", "Task not found in the originating session.")
                return _task_status(runtime.get_flow(flow_id), flow_id)
            result = await _owned_thread_call(read_status)
            guard()
            return result
        if endpoint_id == "list":
            limit = args.get("limit", 20)
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise _TaskRequestError("task_invalid_request", "Task list limit must be an integer.")
            if not runtime:
                return {"tasks": []}
            def read_flows():
                if origin.get("session_id"):
                    return runtime.list_origin_flows(origin["session_id"], limit=max(1, min(limit, 100)))
                return runtime.list_flows(limit=max(1, min(limit, 100)))
            flows = await _owned_thread_call(read_flows)
            guard()
            return {"tasks": [_task_summary(f) for f in flows]}
        return _failure("task_invalid_endpoint", "Unknown background task endpoint.")
    except _TaskRequestError as exc:
        return _failure(exc.code, str(exc))
    except Exception:
        return _failure("task_runtime_unavailable", "Task status could not be read from the current runtime.")

@router.post("/internal/task/start")
async def start_background_task(body: dict):
    """Launch a persistent background task from a natural-language goal.

    Body: ``{goal?: str, subtasks?: [str], title?: str, session_id?: str}``.
    Provide either a single ``goal`` (one autonomous background turn) or
    an ordered list of ``subtasks`` (run sequentially, each its own
    autonomous turn). Returns ``{ok, flow_id, steps, status, title}``.
    """
    caller_task = asyncio.current_task()
    brain = state
    runtime = state.taskflows
    def guard():
        if caller_task is None or caller_task.cancelling() or caller_task.done():
            raise asyncio.CancelledError()
        if state is not brain or state.taskflows is not runtime:
            raise _TaskRequestError("task_origin_superseded", "The originating TaskFlow runtime changed.")
    try:
        return await _owned_thread_call(_start_task, body, {"contract_version": 1, "source": "legacy_local_operator",
                             "owner_verified": False, "session_id": None, "request_id": None,
                             "turn_id": None, "input_revision": None}, guard, runtime)
    except _TaskRequestError as exc:
        return _failure(exc.code, str(exc))


def _task_summary(flow):
    return {"flow_id": flow["id"], "title": flow["title"], "status": flow["status"],
            "current_step": flow["current_step"]}


@router.get("/internal/task/status")
async def background_task_status(flow_id: str):
    """Check a background task's progress: status + per-step state."""
    if not state.taskflows:
        return {"error": "TaskFlow runtime not initialized"}
    flow = state.taskflows.get_flow(flow_id)
    return _task_status(flow, flow_id)


def _task_status(flow, flow_id):
    if not flow:
        return {"error": f"task not found: {flow_id}"}
    steps = flow.get("steps", [])
    done = sum(1 for s in steps if s.get("status") == "completed")
    return {
        "ok": True,
        "flow_id": flow["id"],
        "title": flow["title"],
        "status": flow["status"],
        "current_step": flow["current_step"],
        "steps_total": len(steps),
        "steps_completed": done,
        "error": flow.get("error"),
        "origin": flow.get("context", {}).get("task_origin"),
        "action_outcome": "not_asserted",
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
