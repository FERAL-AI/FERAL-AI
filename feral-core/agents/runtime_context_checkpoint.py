"""Inactive-until-installed runtime checkpoint lifecycle for trusted writers.

Entry preparation, voice/manual writers and cleanup must adopt this same scope
before production activation. The coordinator never replays a request or tool.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from memory.runtime_session_checkpoint import (
    CheckpointFence, CheckpointStatus,
    encode_context, validate_session_id,
)

if TYPE_CHECKING:
    from memory.store import MemoryStore


class RuntimeContextError(RuntimeError):
    """A redacted failure, never a suggestion to retry already executed effects."""

    def __init__(self, code: str, *, effects_may_have_occurred: bool = False):
        self.code = code
        self.effects_may_have_occurred = effects_may_have_occurred
        self.retry_safe = False
        super().__init__(f"Runtime context is not ready ({code}); inspect the exact thread and its action receipts before continuing.")


@dataclass
class _OwnedScope:
    task: asyncio.Task[object]
    session_id: str
    command_handoff: bool
    command_claimed: bool = False


class RuntimeContextCoordinator:
    def __init__(self, store: MemoryStore, *,
                 history: dict[str, list[dict[str, object]]],
                 lock_for: Callable[[str], asyncio.Lock],
                 image_call_ids: Callable[[str], frozenset[str]],
                 clear_images: Callable[[str], None]):
        self.store = store
        self.history = history
        self.lock_for = lock_for  # Existing exact-SID orchestration lock only.
        self.image_call_ids = image_call_ids
        self.clear_images = clear_images
        self._fences: dict[str, CheckpointFence | None] = {}
        self._owner: ContextVar[_OwnedScope | None] = ContextVar("runtime_context_owner", default=None)

    async def _restore_locked(self, session_id: str) -> CheckpointFence | None:
        read = await self.store.runtime_checkpoint_read(session_id)
        cached = self._fences.get(session_id)
        if read.status == CheckpointStatus.ABSENT:
            if cached is not None or self.history.get(session_id):
                raise RuntimeContextError("unmanaged_runtime_context")
            ui = await self.store.conversation_get(session_id)
            if ui is not None and ui.get("messages"):
                raise RuntimeContextError("legacy_context_unavailable")
            return None
        if read.status != CheckpointStatus.READY or read.record is None or read.record.context is None:
            raise RuntimeContextError(f"checkpoint_{read.status.value}")
        record = read.record
        context = record.context
        if context is None:
            raise RuntimeContextError("checkpoint_corrupt")
        if cached is not None and cached != record.fence:
            raise RuntimeContextError("checkpoint_conflict")
        if cached is None and self.history.get(session_id):
            raise RuntimeContextError("unmanaged_runtime_context")
        if cached is None or session_id not in self.history:
            self.history[session_id] = context.history()
            self.store.working_replace(session_id, context.working())
            self.clear_images(session_id)
        self._fences[session_id] = record.fence
        return record.fence

    @asynccontextmanager
    async def write_scope(self, session_id: str, *, command_handoff: bool = False) -> AsyncIterator[None]:
        """Fence before preparation; allow at most one explicit same-task command.

        A child task inheriting the context variable owns no scope. Any nested
        writer or recursive command is refused rather than made reentrant.
        Exceptions/cancellation leave pending state and are propagated unchanged.
        """
        validate_session_id(session_id)
        task = asyncio.current_task()
        if task is None:
            raise RuntimeContextError("missing_context_task")
        owner = self._owner.get()
        if owner is not None and owner.task is task:
            raise RuntimeContextError("nested_context_writer")
        # Bound retained SID locks without evicting a held/queued lock. Failed
        # identities retain their slot for this coordinator's lifetime too.
        if session_id not in self._fences:
            if len(self._fences) >= self.store._runtime_checkpoint_limits.sessions:
                raise RuntimeContextError("context_session_quota")
            self._fences[session_id] = None
        async with self.lock_for(session_id):
            expected = await self._restore_locked(session_id)
            started = await self.store.runtime_checkpoint_begin(session_id, attempt_id=str(uuid4()), expected=expected)
            if started.status != CheckpointStatus.APPLIED or started.record is None:
                raise RuntimeContextError(f"checkpoint_{started.status.value}")
            pending = started.record.fence
            self._fences[session_id] = pending
            token = self._owner.set(_OwnedScope(task, session_id, command_handoff))
            try:
                yield
                try:
                    context = encode_context(self.history.get(session_id, []), self.store.working_get(session_id, limit=50),
                                             limits=self.store._runtime_checkpoint_limits,
                                             tool_image_call_ids=self.image_call_ids(session_id))
                    committed = await self.store.runtime_checkpoint_commit(pending, context)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise RuntimeContextError("checkpoint_commit_unverified", effects_may_have_occurred=True) from None
                if committed.status != CheckpointStatus.APPLIED or committed.record is None:
                    raise RuntimeContextError(f"checkpoint_commit_{committed.status.value}", effects_may_have_occurred=True)
                ready = committed.record
                updated = ready.fence
                if (ready.state != CheckpointStatus.READY or ready.context != context
                        or updated.session_id != pending.session_id or updated.generation != pending.generation
                        or updated.attempt_id != pending.attempt_id or updated.revision != pending.revision + 1):
                    raise RuntimeContextError("checkpoint_commit_unverified", effects_may_have_occurred=True)
                self._fences[session_id] = updated
            finally:
                self._owner.reset(token)

    @asynccontextmanager
    async def command_scope(self, session_id: str) -> AsyncIterator[None]:
        validate_session_id(session_id)
        owner = self._owner.get()
        if owner is not None and owner.task is asyncio.current_task():
            if owner.session_id != session_id or not owner.command_handoff or owner.command_claimed:
                raise RuntimeContextError("nested_context_command")
            owner.command_claimed = True
            yield
        else:
            async with self.write_scope(session_id):
                yield
