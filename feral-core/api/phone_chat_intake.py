"""Owned interactive phone work; the HUP receive loop remains available.

This changes scheduling only. Existing context fences, tools and approvals still
own admission. Cancellation cannot undo an external effect or grant replay.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from security.agent_turn_lease import bind_agent_dispatch_owner, spawn_agent_turn

logger = logging.getLogger(__name__)
MAX_PHONE_CHAT_TASKS = 16


@dataclass
class _LockSlot:
    lock: asyncio.Lock
    users: int = 0


@asynccontextmanager
async def phone_scope_lock(state, kind: str, identity: str):
    """Serialize exact session work or node lifecycle, releasing unused slots."""
    slots = getattr(state, "_phone_intake_locks", None)
    if not isinstance(slots, dict):
        slots = state._phone_intake_locks = {}
    key = (kind, identity)
    slot = slots.setdefault(key, _LockSlot(asyncio.Lock()))
    slot.users += 1
    try:
        async with slot.lock:
            yield
    finally:
        slot.users -= 1
        if slot.users == 0 and slots.get(key) is slot:
            slots.pop(key)


class PhoneChatIntake:
    def __init__(self, state, ws, current_state: Callable[[], object]):
        self.state = state
        self.ws = ws
        self.current_state = current_state
        self.node_id: str | None = None
        self.closed = False
        self.tasks: set[asyncio.Task] = set()
        self.tracked_tasks: set[asyncio.Task] = set()

    def current(self) -> bool:
        return (not self.closed and self.current_state() is self.state
                and (self.node_id is None or getattr(self.state, "daemons", {}).get(self.node_id) is self.ws))

    def guard(self) -> None:
        if not self.current():
            raise asyncio.CancelledError("Phone connection ownership changed")

    async def run_tracked(self, session_id: str, operation, principal):
        """Hold the existing session lane for the actual CTM-owned runner.

        Admission returns before the tracked runner finishes. Its copied lease
        must therefore retain the session lock and live peer checks itself.
        """
        orchestrator, memory = self.state.orchestrator, self.state.memory

        def owner_current():
            return (self.current() and self.state.orchestrator is orchestrator
                    and self.state.memory is memory and principal.is_current() is True)

        principal.require_current()
        task = asyncio.current_task()
        if task is None:
            raise asyncio.CancelledError("Tracked phone runner unavailable")
        self.tracked_tasks.add(task)
        try:
            async with phone_scope_lock(self.state, "session", session_id):
                self.guard()
                principal.require_current()
                with bind_agent_dispatch_owner(owner_current):
                    result = await operation()
                    principal.require_current()
                    self.guard()
                    return result
        finally:
            self.tracked_tasks.discard(task)

    def submit(self, session_id: str, operation: Callable[[], Awaitable[object]], *,
               admission_current: Callable[[], bool] | None = None) -> bool:
        self.guard()
        if len(self.tasks) >= MAX_PHONE_CHAT_TASKS:
            return False

        orchestrator = self.state.orchestrator
        memory = self.state.memory

        def owner_current():
            return (
                self.current()
                and self.state.orchestrator is orchestrator
                and self.state.memory is memory
                and (admission_current is None or admission_current() is True)
            )

        async def run():
            async with phone_scope_lock(self.state, "session", session_id):
                self.guard()
                # The existing central dispatch guard consults this task-local
                # owner even when a collaborator suppresses cancellation.
                with bind_agent_dispatch_owner(owner_current):
                    await operation()

        task = spawn_agent_turn(self.state, run())
        self.tasks.add(task)
        self.state.register_background_task(task)

        def completed(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.warning("Owned phone chat ended without a confirmed reply")

        task.add_done_callback(completed)
        return True

    def stop(self) -> None:
        self.closed = True
        for task in tuple(self.tasks | self.tracked_tasks):
            if not task.done():
                task.cancel()

    async def drain(self) -> None:
        self.stop()
        remaining = {task for task in self.tasks | self.tracked_tasks if not task.done()}
        if remaining:
            _, pending = await asyncio.wait(remaining, timeout=2)
            if pending:
                # Strong references remain in this intake and BrainState until
                # completion. A deadline is not a verified cancellation receipt.
                logger.warning("Disconnected phone work is still draining; earlier effects remain uncertain")
