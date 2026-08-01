"""Process-wide registry of live external-agent sessions.

A skill call is a single request/response, but an ACP session is a long
lived subprocess. Something has to hold the process between the turn that
starts a task and the turn that answers its permission prompt, and that
something is this registry.

Sessions are keyed by an opaque handle FERAL mints (not the agent's own
session id) so a handle from one agent can never be replayed against
another. Handles are dropped on close, on process death, and by
:func:`sweep` once they go idle, so a forgotten task cannot leave a
coding agent running against the user's repo forever.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from bridges.acp import AcpAgentProcess, AcpSession
from bridges.continuity import SessionIndex, SessionRecord, index as default_index
from bridges.permissions import (
    ApprovalManagerBroker,
    PermissionBroker,
    PermissionRequest,
    QueueingBroker,
)

logger = logging.getLogger("feral.bridges.sessions")

# A session with no activity for this long is abandoned. Long enough for
# a human to think about a permission prompt and come back; short enough
# that a crashed UI does not leave an agent resident all day.
DEFAULT_IDLE_TIMEOUT = 900.0


@dataclass
class ManagedSession:
    handle: str
    agent_id: str
    cwd: str
    process: AcpAgentProcess
    session: AcpSession
    broker: QueueingBroker
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    turns: int = 0
    # Which FERAL conversation this belongs to, when the calling surface
    # knows. Skills are not handed one today, so this is best-effort and
    # continuity falls back to (agent, workspace); see
    # ``bridges.continuity.SessionIndex.find``.
    conversation_id: str = ""
    # How this session came to exist: "new", "resume" or "load" for a
    # session reattached after its process died. Reported to the user
    # because a reattach is not proof that history survived.
    origin: str = "new"
    # State for the digest that gets written to episodic memory when a
    # turn ends. Held here because a turn spans several skill calls.
    turn_prompt: str = ""
    turn_started_at: float = 0.0
    turn_started_writes: int = 0
    turn_recorded: bool = False
    answered_permissions: list[dict] = field(default_factory=list)
    # The in-flight ``session/prompt``. A turn outlives the skill call
    # that started it, because a permission question has to be answered
    # by a *different* tool call: the FERAL tool loop is one call at a
    # time, so a run_task that blocked until the turn ended could never
    # be unblocked by the respond_permission that unblocks it.
    turn: Optional[asyncio.Task] = None
    turn_started_events: int = 0

    def touch(self) -> None:
        self.last_active = time.time()

    @property
    def turn_running(self) -> bool:
        return self.turn is not None and not self.turn.done()

    def start_turn(self, prompt: str, timeout: Optional[float] = None) -> None:
        """Fire off one prompt turn in the background."""
        if self.turn_running:
            raise RuntimeError("a turn is already running on this session")
        self.turn_started_events = len(self.session.transcript)
        self.turn_started_writes = len(self.session.written_paths)
        self.turn_prompt = prompt
        self.turn_started_at = time.time()
        self.turn_recorded = False
        self.answered_permissions = []
        self.turns += 1
        self.touch()
        self.turn = asyncio.ensure_future(
            self.session.prompt(prompt, timeout=timeout)
        )

    def turn_events(self) -> list:
        """Everything streamed since the current turn began."""
        return self.session.transcript[self.turn_started_events:]

    def turn_written_paths(self) -> list[str]:
        """Files FERAL wrote on the agent's behalf during this turn."""
        return self.session.written_paths[self.turn_started_writes:]

    def note_permission_answer(
        self, request: PermissionRequest, decision: str, allowed: bool
    ) -> None:
        """Remember a permission question and its verdict for the digest.

        Recorded even when the answer was no. A refusal is the most
        useful thing to be able to recall later, and it is the one thing
        the event stream does not contain: the agent narrates what it
        did, never what it was stopped from doing.
        """
        self.answered_permissions.append(
            {
                "request_id": request.request_id,
                "tool_name": request.tool_name,
                "title": request.title,
                "decision": decision,
                "allowed": allowed,
            }
        )

    async def wait_for_turn(self, wait_seconds: float) -> str:
        """Wait for the turn to end, a permission to arrive, or a timeout.

        Returns one of ``"completed"``, ``"awaiting_permission"`` or
        ``"running"``. Deliberately does NOT raise on turn failure: the
        caller reads ``turn.exception()`` so a crashed agent surfaces as
        a tool result rather than a traceback in the tool loop.
        """
        deadline = time.time() + max(0.0, wait_seconds)
        while True:
            if self.turn is not None and self.turn.done():
                self.touch()
                return "completed"
            if self.broker.pending:
                self.touch()
                return "awaiting_permission"
            remaining = deadline - time.time()
            if remaining <= 0:
                return "running"
            await asyncio.sleep(min(0.2, remaining))

    @property
    def alive(self) -> bool:
        return self.process.alive

    def pending_permissions(self) -> list[PermissionRequest]:
        return self.broker.pending

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "agent_id": self.agent_id,
            "cwd": self.cwd,
            "acp_session_id": self.session.session_id,
            "alive": self.alive,
            "turns": self.turns,
            "conversation_id": self.conversation_id,
            "origin": self.origin,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "pending_permissions": [
                r.to_dict() for r in self.pending_permissions()
            ],
        }

    def to_record(self) -> SessionRecord:
        return SessionRecord(
            handle=self.handle,
            agent_id=self.agent_id,
            cwd=self.cwd,
            acp_session_id=self.session.session_id,
            conversation_id=self.conversation_id,
            created_at=self.created_at,
            last_active=self.last_active,
            turns=self.turns,
        )


class SessionRegistry:
    """Holds every live external-agent session in this process."""

    def __init__(
        self,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        *,
        index: Optional[SessionIndex] = None,
    ):
        self._sessions: dict[str, ManagedSession] = {}
        self._lock = asyncio.Lock()
        self.idle_timeout = idle_timeout
        # Injectable so a test never writes into the operator's real
        # FERAL home just by opening a session.
        self.index: SessionIndex = index if index is not None else default_index()

    def get(self, handle: str) -> Optional[ManagedSession]:
        return self._sessions.get(handle)

    def list(self) -> list[ManagedSession]:
        return list(self._sessions.values())

    def find_by_permission(self, request_id: str) -> Optional[ManagedSession]:
        for managed in self._sessions.values():
            for pending in managed.pending_permissions():
                if pending.request_id == request_id:
                    return managed
        return None

    def find_persisted(
        self, *, conversation_id: str = "", agent_id: str = "", cwd: str = ""
    ) -> Optional[SessionRecord]:
        """A session this process no longer holds but could reattach to."""
        record = self.index.find(
            conversation_id=conversation_id, agent_id=agent_id, cwd=cwd
        )
        if record is not None and record.handle in self._sessions:
            return None
        return record

    async def open(
        self,
        *,
        agent_id: str,
        command: list[str],
        cwd: str,
        env: Optional[dict[str, str]] = None,
        permission_timeout: float = 120.0,
        approval_manager: Any = None,
        conversation_id: str = "",
        resume: Optional[SessionRecord] = None,
    ) -> ManagedSession:
        """Spawn an agent, initialize, and open one session.

        ``approval_manager`` is FERAL's own
        :class:`~security.exec_approvals.ApprovalManager`. When supplied,
        an existing grant answers without re-prompting and an
        ``allow_always`` answer is persisted back into it. Without one,
        every permission still has to be answered by a human through
        :meth:`answer_permission`; nothing is auto-allowed either way.

        ``resume`` names a session from a previous process. The handle it
        carries is reused, so an LLM holding a handle from an hour ago
        keeps a working one across an agent crash, an idle sweep or a
        FERAL restart. Whether the agent actually restored any history is
        reported as :attr:`ManagedSession.origin` rather than assumed:
        see :meth:`bridges.acp.AcpAgentProcess.reattach_session`.
        """
        queueing = QueueingBroker(timeout_seconds=permission_timeout)
        # ``queueing`` is always the innermost broker, because it is the
        # one ``ManagedSession.pending_permissions`` and
        # ``answer_permission`` reach through. Swapping it out would make
        # a parked request invisible to the surface that answers it.
        broker: PermissionBroker = queueing
        if approval_manager is not None:
            broker = ApprovalManagerBroker(
                approval_manager,
                session_key=f"external_agent:{agent_id}",
                fallback=broker,
            )

        process = await AcpAgentProcess.spawn(
            command, cwd=cwd, env=env, broker=broker
        )
        origin = "new"
        try:
            await process.initialize()
            if resume is not None and resume.acp_session_id and process.can_resume():
                session, origin = await process.reattach_session(
                    resume.acp_session_id, cwd
                )
            else:
                session = await process.new_session(cwd)
        except Exception:
            await process.close()
            raise

        managed = ManagedSession(
            handle=(
                resume.handle if resume is not None else f"ext-{uuid.uuid4().hex[:12]}"
            ),
            agent_id=agent_id,
            cwd=session.cwd,
            process=process,
            session=session,
            broker=queueing,
            conversation_id=conversation_id or (
                resume.conversation_id if resume is not None else ""
            ),
            origin=origin,
            turns=resume.turns if resume is not None else 0,
        )
        async with self._lock:
            self._sessions[managed.handle] = managed
        self.index.remember(managed.to_record())
        logger.info(
            "opened external agent session %s (%s, origin=%s) in %s",
            managed.handle,
            agent_id,
            origin,
            session.cwd,
        )
        return managed

    def answer_permission(self, request_id: str, option_id: str) -> bool:
        """Resolve a parked permission. Returns False when it is unknown."""
        managed = self.find_by_permission(request_id)
        if managed is None:
            return False
        managed.touch()
        return managed.broker.resolve(request_id, option_id)

    async def cancel(self, handle: str) -> bool:
        managed = self._sessions.get(handle)
        if managed is None:
            return False
        managed.touch()
        await managed.session.cancel()
        return True

    async def close(self, handle: str, *, forget: bool = True) -> bool:
        """Tear down a session.

        ``forget`` drops the persisted pointer as well, which is right
        for an explicit close: the user said they were done, so a later
        follow-up should not silently resurrect a session they closed. It
        is wrong for the sweeper, which reaps a session the user never
        asked to end, so :meth:`sweep` passes ``forget=False`` and leaves
        the pointer behind for a resume.
        """
        async with self._lock:
            managed = self._sessions.pop(handle, None)
        if managed is None:
            return False
        managed.broker.reject_all("session closed")
        if managed.turn is not None and not managed.turn.done():
            managed.turn.cancel()
        await managed.process.close()
        if forget:
            self.index.forget(handle)
        else:
            self.index.touch(handle, turns=managed.turns)
        logger.info("closed external agent session %s (forget=%s)", handle, forget)
        return True

    async def close_all(self) -> None:
        # Shutdown is not the user saying "done", so the pointers stay
        # and every session is resumable when FERAL comes back up.
        for handle in list(self._sessions):
            await self.close(handle, forget=False)

    async def sweep(self, now: Optional[float] = None) -> list[str]:
        """Close sessions whose process died or which have gone idle."""
        now = now if now is not None else time.time()
        doomed = [
            managed.handle
            for managed in list(self._sessions.values())
            if not managed.alive or (now - managed.last_active) > self.idle_timeout
        ]
        for handle in doomed:
            await self.close(handle, forget=False)
        return doomed


_REGISTRY: Optional[SessionRegistry] = None


def registry() -> SessionRegistry:
    """The process-wide registry. Lazily built so import stays cheap."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SessionRegistry()
    return _REGISTRY
