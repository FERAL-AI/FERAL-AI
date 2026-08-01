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
        self.turns += 1
        self.touch()
        self.turn = asyncio.ensure_future(
            self.session.prompt(prompt, timeout=timeout)
        )

    def turn_events(self) -> list:
        """Everything streamed since the current turn began."""
        return self.session.transcript[self.turn_started_events:]

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
            "created_at": self.created_at,
            "last_active": self.last_active,
            "pending_permissions": [
                r.to_dict() for r in self.pending_permissions()
            ],
        }


class SessionRegistry:
    """Holds every live external-agent session in this process."""

    def __init__(self, idle_timeout: float = DEFAULT_IDLE_TIMEOUT):
        self._sessions: dict[str, ManagedSession] = {}
        self._lock = asyncio.Lock()
        self.idle_timeout = idle_timeout

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

    async def open(
        self,
        *,
        agent_id: str,
        command: list[str],
        cwd: str,
        env: Optional[dict[str, str]] = None,
        permission_timeout: float = 120.0,
        approval_manager: Any = None,
        extra_broker: Optional[PermissionBroker] = None,
    ) -> ManagedSession:
        """Spawn an agent, initialize, and open one session.

        ``approval_manager`` is FERAL's own
        :class:`~security.exec_approvals.ApprovalManager`. When supplied,
        an existing grant answers without re-prompting and an
        ``allow_always`` answer is persisted back into it. Without one,
        every permission still has to be answered by a human through
        :meth:`answer_permission`; nothing is auto-allowed either way.
        """
        queueing = QueueingBroker(timeout_seconds=permission_timeout)
        broker: PermissionBroker = extra_broker or queueing
        if approval_manager is not None:
            broker = ApprovalManagerBroker(
                approval_manager,
                session_key=f"external_agent:{agent_id}",
                fallback=broker,
            )

        process = await AcpAgentProcess.spawn(
            command, cwd=cwd, env=env, broker=broker
        )
        try:
            await process.initialize()
            session = await process.new_session(cwd)
        except Exception:
            await process.close()
            raise

        managed = ManagedSession(
            handle=f"ext-{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            cwd=session.cwd,
            process=process,
            session=session,
            broker=queueing,
        )
        async with self._lock:
            self._sessions[managed.handle] = managed
        logger.info(
            "opened external agent session %s (%s) in %s",
            managed.handle,
            agent_id,
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

    async def close(self, handle: str) -> bool:
        async with self._lock:
            managed = self._sessions.pop(handle, None)
        if managed is None:
            return False
        managed.broker.reject_all("session closed")
        if managed.turn is not None and not managed.turn.done():
            managed.turn.cancel()
        await managed.process.close()
        logger.info("closed external agent session %s", handle)
        return True

    async def close_all(self) -> None:
        for handle in list(self._sessions):
            await self.close(handle)

    async def sweep(self, now: Optional[float] = None) -> list[str]:
        """Close sessions whose process died or which have gone idle."""
        now = now if now is not None else time.time()
        doomed = [
            managed.handle
            for managed in list(self._sessions.values())
            if not managed.alive or (now - managed.last_active) > self.idle_timeout
        ]
        for handle in doomed:
            await self.close(handle)
        return doomed


_REGISTRY: Optional[SessionRegistry] = None


def registry() -> SessionRegistry:
    """The process-wide registry. Lazily built so import stays cheap."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SessionRegistry()
    return _REGISTRY
