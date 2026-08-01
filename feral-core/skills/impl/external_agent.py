"""external_agent: hand a coding task to opencode, Claude Code or Codex.

Four endpoints, deliberately. This skill is NOT in
``Orchestrator.ALWAYS_INCLUDE_SKILLS``: the chat path applies no tool cap
and the always-included set already spends about 60 tools per turn, so a
skill that is only relevant when the user actually wants an external
coding agent has to be pulled in by retrieval, not pinned.

The permission dance
====================
An ACP agent asks permission mid-turn, and FERAL's tool loop runs one
call at a time. A ``run_task`` that blocked until the turn finished could
therefore never be unblocked by the ``respond_permission`` that unblocks
it. So ``run_task`` starts the turn in the background and returns the
moment a permission question lands; ``respond_permission`` answers it and
resumes the same wait. Nothing is ever auto-allowed: an unanswered
question times out into a rejection inside
``bridges.permissions.QueueingBroker``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.external_agent")

# How long a single skill call may sit waiting before it reports back
# "still running". Kept short relative to a coding turn: the caller polls
# with another run_task/respond_permission rather than holding the tool
# loop open for minutes.
DEFAULT_WAIT_SECONDS = 90.0
MAX_WAIT_SECONDS = 600.0

VALID_DECISIONS = ("allow_once", "allow_always", "reject_once", "reject_always")


def _ok(data: Any) -> Dict[str, Any]:
    return {"success": True, "status_code": 200, "data": data, "error": None}


def _err(message: str, status: int = 400, data: Any = None) -> Dict[str, Any]:
    return {"success": False, "status_code": status, "data": data, "error": message}


def _clamp_wait(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_WAIT_SECONDS
    return max(1.0, min(MAX_WAIT_SECONDS, value))


class ExternalAgentSkill(BaseSkill):
    """Drives an external ACP coding agent as a subprocess."""

    def __init__(self):
        super().__init__("external_agent")

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]
    ) -> Dict[str, Any]:
        dispatch = {
            "list_agents": self.list_agents,
            "run_task": self.run_task,
            "respond_permission": self.respond_permission,
            "close_session": self.close_session,
        }
        handler = dispatch.get(endpoint_id)
        if handler is None:
            return _err(f"unknown endpoint {endpoint_id!r}", status=404)
        try:
            return await handler(args or {})
        except Exception as exc:
            logger.exception("external_agent.%s failed", endpoint_id)
            return _err(f"{type(exc).__name__}: {exc}", status=500)

    # ------------------------------------------------------------------

    async def list_agents(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from bridges import catalog

        conf = catalog.external_agent_settings()
        agents = [resolved.to_dict() for resolved in catalog.resolve_all()]
        return _ok(
            {
                "default_agent": conf["default_agent"],
                "agents": agents,
                "hermes": catalog.detect_hermes(),
                "live_sessions": [m.to_dict() for m in _registry().list()],
            }
        )

    # ------------------------------------------------------------------

    async def run_task(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from bridges import catalog

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _err("prompt is required")

        wait = _clamp_wait(args.get("wait_seconds", DEFAULT_WAIT_SECONDS))
        handle = str(args.get("session_handle") or "").strip()
        registry = _registry()

        if handle:
            managed = registry.get(handle)
            if managed is None:
                return _err(f"unknown session handle {handle!r}", status=404)
            if managed.turn_running:
                return _err(
                    "a turn is already running on that session; wait for it or "
                    "close the session",
                    status=409,
                )
        else:
            conf = catalog.external_agent_settings()
            agent_id = str(args.get("agent_id") or conf["default_agent"])
            workspace = str(args.get("workspace_dir") or os.getcwd())
            if not os.path.isdir(workspace):
                return _err(f"workspace_dir does not exist: {workspace}")

            resolved = catalog.resolve(agent_id)
            if not resolved.available:
                return _err(resolved.reason, status=424, data=resolved.to_dict())

            managed = await registry.open(
                agent_id=agent_id,
                command=resolved.command,
                cwd=workspace,
                env=dict(os.environ),
                permission_timeout=conf["permission_timeout_seconds"],
                approval_manager=_approval_manager(),
            )

        managed.start_turn(prompt)
        state = await managed.wait_for_turn(wait)
        return _ok(self._turn_payload(managed, state))

    # ------------------------------------------------------------------

    async def respond_permission(self, args: Dict[str, Any]) -> Dict[str, Any]:
        request_id = str(args.get("request_id") or "").strip()
        decision = str(args.get("decision") or "").strip()
        if not request_id:
            return _err("request_id is required")
        if decision not in VALID_DECISIONS:
            return _err(
                f"decision must be one of {', '.join(VALID_DECISIONS)}"
            )

        registry = _registry()
        managed = registry.find_by_permission(request_id)
        if managed is None:
            return _err(
                f"no pending permission request {request_id!r}", status=404
            )

        pending = next(
            (r for r in managed.pending_permissions() if r.request_id == request_id),
            None,
        )
        if pending is None:
            return _err(f"no pending permission request {request_id!r}", status=404)

        option = pending.first_of((decision,))
        if option is None:
            return _err(
                f"the agent did not offer a {decision!r} option for this request",
                status=409,
                data=pending.to_dict(),
            )

        if not registry.answer_permission(request_id, option.option_id):
            return _err("permission request was already resolved", status=409)

        wait = _clamp_wait(args.get("wait_seconds", DEFAULT_WAIT_SECONDS))
        state = await managed.wait_for_turn(wait)
        payload = self._turn_payload(managed, state)
        payload["answered"] = {"request_id": request_id, "decision": decision}
        return _ok(payload)

    # ------------------------------------------------------------------

    async def close_session(self, args: Dict[str, Any]) -> Dict[str, Any]:
        handle = str(args.get("session_handle") or "").strip()
        if not handle:
            return _err("session_handle is required")
        registry = _registry()
        managed = registry.get(handle)
        if managed is None:
            return _err(f"unknown session handle {handle!r}", status=404)

        if args.get("cancel_first"):
            await registry.cancel(handle)
        closed = await registry.close(handle)
        return _ok({"handle": handle, "closed": closed})

    # ------------------------------------------------------------------

    def _turn_payload(self, managed, state: str) -> Dict[str, Any]:
        events = managed.turn_events()
        payload: Dict[str, Any] = {
            "status": state,
            "session_handle": managed.handle,
            "agent_id": managed.agent_id,
            "workspace_dir": managed.cwd,
            "events": [e.to_dict() for e in events],
            "tool_calls": [
                e.to_dict() for e in events if e.is_tool_call and e.tool_call_id
            ],
            "text": "".join(
                e.text for e in events if e.kind == "agent_message_chunk"
            ),
            "pending_permissions": [
                r.to_dict() for r in managed.pending_permissions()
            ],
        }

        if state == "completed" and managed.turn is not None:
            error = managed.turn.exception() if not managed.turn.cancelled() else None
            if error is not None:
                payload["status"] = "failed"
                payload["error"] = f"{type(error).__name__}: {error}"
                payload["agent_stderr"] = managed.process.stderr_tail[-2000:]
            else:
                result = managed.turn.result()
                payload["stop_reason"] = getattr(result, "stop_reason", "")
        return payload


def _registry():
    from bridges.sessions import registry

    return registry()


def _approval_manager():
    """FERAL's own approval store, when this process has one.

    ``approval_manager`` is an attribute of the ``BrainState`` instance,
    not of ``api.state`` the module, so it has to be reached through
    ``api.state.state``. Reading it off the module returns ``None``
    forever and quietly disables the whole ApprovalManager integration.

    Looked up through ``sys.modules`` rather than imported: importing
    ``api.state`` pulls in the entire brain, and a skill call has no
    business doing that. If the brain is not running there is no store to
    consult, and ``None`` means every permission still has to be answered
    by a human. Nothing is auto-allowed either way.
    """
    module = sys.modules.get("api.state")
    if module is None:
        return None
    brain_state = getattr(module, "state", None)
    if brain_state is None:
        return None
    return getattr(brain_state, "approval_manager", None)


register_skill(ExternalAgentSkill)
