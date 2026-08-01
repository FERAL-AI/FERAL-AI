"""external_agent: hand a coding task to opencode, Claude Code or Codex.

Five endpoints, deliberately. This skill is NOT in
``Orchestrator.ALWAYS_INCLUDE_SKILLS``: the chat path applies no tool cap
and the always-included set already spends about 60 tools per turn, so a
skill that is only relevant when the user actually wants an external
coding agent has to be pulled in by retrieval, not pinned. The fifth
endpoint, ``recall_activity``, is the one exception worth its slot: it is
the only way to ask about all four agents at once, and it rides in with
the skill that is already being retrieved.

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

Memory
======
Every turn that reaches a terminal state is distilled into one episode
(``memory/agent_activity.py``). Nothing new is stored: an episode is
already searchable, already decays, already syncs, and is already fanned
out by ``notes_memory__fused_timeline``, so "what did the coding agent do
in this repo yesterday" is answerable through the memory surfaces that
existed before this feature.

Continuity
==========
A follow-up continues the same agent session. The mapping outlives this
process (``bridges/continuity.py``), so a handle stays usable across an
agent crash, an idle sweep or a FERAL restart. When the agent can
reattach it does; when it cannot, the replacement session is briefed with
the previous turn's record and the payload says so rather than pretending
the agent remembers.
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

# Cap on the raw event list in a tool result. One real opencode run
# produced 1026 events for a single turn; handing that to an LLM that
# applies no tool-result cap is how a chat turn runs out of context in
# the middle of a coding task. The collapsed ``digest`` beside it carries
# the same facts in a bounded form, and the full stream is never the
# thing a caller needs: it needs the tail, where the outcome is.
MAX_EVENTS_RETURNED = 150

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
            "recall_activity": self.recall_activity,
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
        conversation_id = str(args.get("conversation_id") or "").strip()
        registry = _registry()
        continuity: Dict[str, Any] = {"reattached": False, "mechanism": "live"}
        record = None

        if handle:
            managed = registry.get(handle)
            if managed is not None and managed.turn_running:
                return _err(
                    "a turn is already running on that session; wait for it or "
                    "close the session",
                    status=409,
                )
            if managed is not None and not managed.alive:
                # The subprocess died between turns. Drop the corpse but
                # keep the pointer, then fall through to the reattach.
                await registry.close(handle, forget=False)
                managed = None
            if managed is None:
                record = registry.index.get(handle)
                if record is None:
                    return _err(
                        f"unknown session handle {handle!r}; it was never opened "
                        f"or it was explicitly closed. Start a new task without "
                        f"a session_handle.",
                        status=404,
                    )
                agent_id, workspace = record.agent_id, record.cwd
            else:
                agent_id, workspace = managed.agent_id, managed.cwd
        else:
            managed = None
            conf = catalog.external_agent_settings()
            agent_id = str(args.get("agent_id") or conf["default_agent"])
            workspace = str(args.get("workspace_dir") or os.getcwd())
            if not os.path.isdir(workspace):
                return _err(f"workspace_dir does not exist: {workspace}")
            if not args.get("fresh_session"):
                # No handle, but this may still be a follow-up: the chat
                # surface does not hand skills a conversation id, so the
                # fallback key is (agent, workspace), which is what a
                # user means by "keep going on that repo".
                record = registry.find_persisted(
                    conversation_id=conversation_id,
                    agent_id=agent_id,
                    cwd=workspace,
                )

        if managed is None:
            resolved = catalog.resolve(agent_id)
            if not resolved.available:
                return _err(resolved.reason, status=424, data=resolved.to_dict())
            conf = catalog.external_agent_settings()
            try:
                managed = await registry.open(
                    agent_id=agent_id,
                    command=resolved.command,
                    cwd=workspace,
                    env=dict(os.environ),
                    permission_timeout=conf["permission_timeout_seconds"],
                    approval_manager=_approval_manager(),
                    conversation_id=conversation_id,
                    resume=record,
                )
            except Exception as exc:
                if record is None:
                    raise
                return _err(
                    f"could not reattach to session {record.handle!r}: "
                    f"{type(exc).__name__}: {exc}",
                    status=502,
                )
            if record is not None:
                prompt, continuity = await self._reattachment(managed, record, prompt)

        managed.start_turn(prompt)
        state = await managed.wait_for_turn(wait)
        payload = await self._finish_turn(managed, state)
        payload["continuity"] = continuity
        return _ok(payload)

    # ------------------------------------------------------------------

    async def _reattachment(self, managed, record, prompt: str):
        """Describe a reattach honestly, and re-brief a cold restart.

        Three outcomes, and the caller is told which one happened rather
        than being left to assume the best:

        ``resume`` / ``load``
            The agent accepted the prior session id. Note that this is
            not proof any history came back: hermes' ``resume_session``
            creates a fresh session and returns success when the id is
            unknown (``acp_adapter/server.py`` lines 1413 to 1416), and a
            client cannot tell the difference from the response.
        ``new``
            The agent cannot reattach at all, so the replacement session
            is briefed with the previous turn's record out of episodic
            memory. That is a genuine restart, and saying so is the point
            of this dict.
        """
        mechanism = managed.origin
        continuity: Dict[str, Any] = {
            "reattached": True,
            "mechanism": mechanism,
            "previous_turns": record.turns,
            "session_handle": record.handle,
        }
        if mechanism in ("resume", "load"):
            continuity["note"] = (
                f"the agent accepted {record.acp_session_id} via session/"
                f"{mechanism}. Some agents create a fresh session instead of "
                f"failing on an unknown id, so treat restored history as "
                f"likely rather than certain."
            )
            return prompt, continuity

        from memory import agent_activity

        previous = await agent_activity.last_turn_summary(_memory(), record.handle)
        continuity["briefed_from_memory"] = bool(previous)
        continuity["note"] = (
            "this agent cannot reattach to a previous session, so it was "
            "restarted"
            + (
                " and briefed with FERAL's record of the last turn."
                if previous
                else " with no memory of the earlier turns, and FERAL had no "
                "record of them either."
            )
        )
        if previous:
            prompt = (
                "Context from your previous session in this workspace, "
                "recorded by FERAL (you will not remember it yourself):\n"
                f"{previous}\n\n"
                f"Continuing from there: {prompt}"
            )
        return prompt, continuity

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

        # Recorded before the wait, so a turn that then crashes still
        # remembers what was asked and what was said. The answer is the
        # one part of the turn that never appears in the event stream.
        managed.note_permission_answer(pending, decision, option.allows)

        wait = _clamp_wait(args.get("wait_seconds", DEFAULT_WAIT_SECONDS))
        state = await managed.wait_for_turn(wait)
        payload = await self._finish_turn(managed, state)
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
            # Not live, but it may still have a persisted pointer from a
            # previous process. Closing that is a real action, not a 404.
            if registry.index.forget(handle):
                return _ok({"handle": handle, "closed": True, "was_live": False})
            return _err(f"unknown session handle {handle!r}", status=404)

        # A turn still in flight is about to be cancelled, so record what
        # it managed to do first. Without this, closing mid-task loses
        # the whole turn from memory, which is precisely the run a user
        # is most likely to ask about afterwards.
        recorded = None
        if managed.turn is not None and not managed.turn_recorded:
            recorded = await self._record_turn(managed, "interrupted")

        if args.get("cancel_first"):
            await registry.cancel(handle)
        closed = await registry.close(handle)
        return _ok(
            {
                "handle": handle,
                "closed": closed,
                "was_live": True,
                "memory": recorded,
            }
        )

    # ------------------------------------------------------------------

    async def recall_activity(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """What the external coding agents have been doing, all of them.

        Reads back the same episodes everything else reads. There is no
        parallel store to fall out of sync with, which is why a user who
        instead asks the fused timeline for yesterday sees these same
        turns in the same card.
        """
        from memory import agent_activity
        from skills.impl.timeline_fusion import parse_window

        # Window parsing is reused rather than reimplemented: the
        # yesterday / last_tuesday / this_morning vocabulary already
        # exists for ``notes_memory__fused_timeline`` and two parsers
        # answering the same question differently is exactly the drift
        # this codebase keeps growing.
        window = parse_window(
            args.get("window_label"),
            from_ts=args.get("from_ts"),
            to_ts=args.get("to_ts"),
        )
        result = await agent_activity.recall(
            _memory(),
            query=str(args.get("query") or "").strip(),
            workspace_dir=str(args.get("workspace_dir") or "").strip(),
            agent_id=str(args.get("agent_id") or "").strip(),
            from_ts=window["from_ts"],
            to_ts=window["to_ts"],
            limit=int(args.get("limit") or 20),
        )
        result["window"] = {
            "from": window["from"],
            "to": window["to"],
            "label": window["label"],
        }
        result["live_sessions"] = [m.to_dict() for m in _registry().list()]
        result["resumable_sessions"] = [
            r.to_dict() for r in _registry().index.list()[:20]
        ]
        return _ok(result)

    # ------------------------------------------------------------------

    async def _finish_turn(self, managed, state: str) -> Dict[str, Any]:
        payload = self._turn_payload(managed, state)
        if payload["status"] in ("completed", "failed"):
            payload["memory"] = await self._record_turn(
                managed,
                payload["status"],
                stop_reason=str(payload.get("stop_reason") or ""),
                error=str(payload.get("error") or ""),
            )
        _registry().index.touch(managed.handle, turns=managed.turns)
        return payload

    async def _record_turn(
        self, managed, status: str, *, stop_reason: str = "", error: str = ""
    ):
        """Distil the turn into one episode. Idempotent per turn.

        ``stop_reason`` and ``error`` are passed in rather than
        recomputed: they are only derivable from the finished turn task,
        and re-deriving them here would silently produce empty strings
        for the ``interrupted`` case, which is the one where knowing the
        turn did not finish matters most.
        """
        if managed.turn_recorded:
            return None
        managed.turn_recorded = True

        from memory import agent_activity

        digest = agent_activity.digest_turn(
            agent_id=managed.agent_id,
            workspace_dir=managed.cwd,
            session_handle=managed.handle,
            acp_session_id=managed.session.session_id,
            events=managed.turn_events(),
            prompt=managed.turn_prompt,
            status=status,
            stop_reason=stop_reason,
            error=error,
            permissions=list(managed.answered_permissions),
            written_paths=managed.turn_written_paths(),
            started_at=managed.turn_started_at,
        )
        saved = await agent_activity.record_turn(_memory(), digest)
        return {
            "recorded": saved is not None,
            "episode_id": (saved or {}).get("id", ""),
            "digest": digest.to_dict(),
        }

    def _turn_payload(self, managed, state: str) -> Dict[str, Any]:
        events = managed.turn_events()
        # The tail, not the head: the outcome of a turn is at the end,
        # and a 1026-event stream truncated from the front would hand the
        # LLM a thousand frames of setup and none of the result.
        shown = events[-MAX_EVENTS_RETURNED:]
        payload: Dict[str, Any] = {
            "status": state,
            "session_handle": managed.handle,
            "agent_id": managed.agent_id,
            "workspace_dir": managed.cwd,
            "events": [e.to_dict() for e in shown],
            "events_total": len(events),
            "events_omitted": max(0, len(events) - len(shown)),
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


def _memory():
    """FERAL's ``MemoryStore``, when this process has one.

    Reached through ``sys.modules`` for the same reason as
    :func:`_approval_manager`: importing ``api.state`` pulls in the whole
    brain, and a skill call has no business doing that. No brain means no
    recording and no recall, which the caller sees as
    ``recorded: false`` rather than as an error.
    """
    module = sys.modules.get("api.state")
    if module is None:
        return None
    brain_state = getattr(module, "state", None)
    if brain_state is None:
        return None
    return getattr(brain_state, "memory", None)


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
