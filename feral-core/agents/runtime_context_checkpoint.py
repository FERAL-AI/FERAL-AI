"""Runtime checkpoint lifecycle installed by mandatory brain bootstrap.

Entry preparation and cleanup share this scope. Managed voice and unsupported
manual writers are refused explicitly. The coordinator never replays a task.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from memory.runtime_session_checkpoint import (
    CheckpointFence, CheckpointRecord, CheckpointStatus, CheckpointValidationError,
    encode_context, validate_session_id,
)

if TYPE_CHECKING:
    from memory.store import MemoryStore

logger = logging.getLogger(__name__)
LEGACY_MEDIA_READ_TIMEOUT_SECONDS = 10.0


class RuntimeContextError(RuntimeError):
    """A redacted failure, never a suggestion to retry already executed effects."""

    def __init__(self, code: str, *, effects_may_have_occurred: bool = False):
        self.code = code
        self.effects_may_have_occurred = effects_may_have_occurred
        self.retry_safe = False
        super().__init__(f"Runtime context is not ready ({code}); inspect the exact thread and its action receipts before continuing.")


class ContextReadinessState(str, Enum):
    READY = "ready"
    LEGACY = "legacy"
    LEGACY_UNAVAILABLE = "legacy_unavailable"
    IN_PROGRESS = "in_progress"
    DELETED = "deleted"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    QUOTA = "quota"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RuntimeContextReadiness:
    session_id: str
    state: ContextReadinessState
    managed: bool
    fence: CheckpointFence | None = None
    initialized: bool = False
    omissions: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    recovery_fence: CheckpointFence | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "omissions", MappingProxyType(dict(self.omissions)))

    @property
    def ready(self) -> bool:
        return self.state == ContextReadinessState.READY and self.fence is not None


class RuntimeContextAttachmentToken:
    """Opaque object identity, never a wire credential or SID-only counter."""
    __slots__ = ("_session_id",)

    def __init__(self, session_id: str):
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id


@dataclass(frozen=True)
class RuntimeContextAttachment:
    token: RuntimeContextAttachmentToken
    readiness: RuntimeContextReadiness


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
                 clear_images: Callable[[str], None],
                 clear_session: Callable[[str], None] | None = None,
                 finalize_legacy: Callable[[str], Awaitable[None]] | None = None,
                 legacy_passthrough: bool = False):
        self.store = store
        self.history = history
        self._lock_source = lock_for  # Existing exact-SID orchestration lock only.
        self._retained_sids: set[str] = set()
        self.image_call_ids = image_call_ids
        self.clear_images = clear_images
        self.clear_session = clear_session
        self.finalize_legacy = finalize_legacy
        self.legacy_passthrough = legacy_passthrough
        self._attachments: dict[RuntimeContextAttachmentToken, bool] = {}
        self._attachment_managed: set[RuntimeContextAttachmentToken] = set()
        self._attachment_refusals: dict[RuntimeContextAttachmentToken, RuntimeContextReadiness] = {}
        self._writers: dict[str, int] = {}
        self._fences: dict[str, CheckpointFence | None] = {}
        self._recoveries: set[asyncio.Task[RuntimeContextReadiness]] = set()
        self._owner: ContextVar[_OwnedScope | None] = ContextVar("runtime_context_owner", default=None)

    def lock_for(self, session_id: str) -> asyncio.Lock:
        validate_session_id(session_id)
        if session_id not in self._retained_sids:
            if len(self._retained_sids) >= self.store._runtime_checkpoint_limits.sessions:
                raise RuntimeContextError("context_session_quota")
            self._retained_sids.add(session_id)
        return self._lock_source(session_id)

    async def is_managed(self, session_id: str) -> bool:
        validate_session_id(session_id)
        if session_id in self._fences:
            return True
        try:
            return (await self.store.runtime_checkpoint_read(session_id)).status != CheckpointStatus.ABSENT
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeContextError("checkpoint_unavailable") from None

    def known_managed(self, session_id: str) -> bool:
        """Positive cached knowledge only; false never proves absence in SQLite."""
        return session_id in self._fences

    def owns_writer(self, session_id: str) -> bool:
        owner = self._owner.get()
        return owner is not None and owner.task is asyncio.current_task() and owner.session_id == session_id

    def has_attachments(self, session_id: str) -> bool:
        return any(token.session_id == session_id for token in self._attachments)

    def has_writers(self, session_id: str) -> bool:
        return self._writers.get(session_id, 0) > 0

    def review_generation(self, session_id: str) -> str | None:
        """Trusted runtime generation only; never read a model-supplied field."""
        fence = self._fences.get(session_id)
        return fence.generation if fence is not None else None

    def review_generation_valid(self, session_id: str, generation: object) -> bool:
        if not self.known_managed(session_id):
            return generation is None
        current = self.review_generation(session_id)
        return current is not None and isinstance(generation, str) and generation == current

    def _publish_recovery_locked(self, session_id: str, record: CheckpointRecord) -> RuntimeContextReadiness:
        """Publish verified private data; retain fail-closed fences on failure."""
        if record.context is None:
            raise RuntimeContextError("context_recovery_unavailable", effects_may_have_occurred=True)
        try:
            self._reserve_managed(session_id)
            self.history[session_id] = record.context.history()
            self.store.working_replace(session_id, record.context.working())
            self.clear_images(session_id)
            self._fences[session_id] = record.fence
            for token, refusal in tuple(self._attachment_refusals.items()):
                if token.session_id == session_id and refusal.state == ContextReadinessState.IN_PROGRESS:
                    self._attachment_refusals.pop(token, None)
            for token in self._attachments:
                if token.session_id == session_id:
                    self._attachments[token] = False
            return RuntimeContextReadiness(session_id, ContextReadinessState.READY, True, record.fence,
                omissions=MappingProxyType({key: int(value) for key, value in record.context.omissions().items() if isinstance(value, int)}))
        except RuntimeContextError:
            raise
        except Exception:
            raise RuntimeContextError("context_recovery_unavailable", effects_may_have_occurred=True) from None

    async def recover(self, session_id: str, expected: CheckpointFence,
                      *, active_turn: Callable[[], bool]) -> RuntimeContextReadiness:
        """Recover one explicitly acknowledged SID under its existing writer lock.

        Cancellation of the surface cannot interrupt commit/cache publication.
        The retained operation restores data only; it never dispatches work.
        """
        if not isinstance(expected, CheckpointFence) or expected.session_id != session_id:
            raise RuntimeContextError("context_recovery_session_mismatch")
        if self.has_writers(session_id) or active_turn():
            raise RuntimeContextError("context_recovery_busy")

        async def recover_locked() -> RuntimeContextReadiness:
            async with self.lock_for(session_id):
                if self.has_writers(session_id) or active_turn():
                    raise RuntimeContextError("context_recovery_busy")
                try:
                    current = await self.store.runtime_checkpoint_read(session_id)
                    if current.status != CheckpointStatus.IN_PROGRESS:
                        raise RuntimeContextError(f"context_recovery_{current.status.value}")
                    cached = self._fences.get(session_id)
                    if current.record is None or current.record.fence != expected or (cached is not None and cached != expected):
                        raise RuntimeContextError("context_recovery_conflict")
                    if self.has_writers(session_id) or active_turn():
                        raise RuntimeContextError("context_recovery_busy")
                    recovered = await self.store.runtime_checkpoint_recover(expected)
                except asyncio.CancelledError:
                    raise
                except RuntimeContextError:
                    raise
                except Exception:
                    raise RuntimeContextError("context_recovery_unavailable", effects_may_have_occurred=True) from None
                if recovered.status != CheckpointStatus.APPLIED or recovered.record is None or recovered.record.context is None:
                    raise RuntimeContextError(f"context_recovery_{recovered.status.value}")
                record = recovered.record
                if (record.state != CheckpointStatus.READY or record.fence.session_id != session_id
                        or record.fence.revision != expected.revision + 1
                        or record.fence.generation == expected.generation or record.fence.attempt_id == expected.attempt_id):
                    raise RuntimeContextError("context_recovery_unavailable", effects_may_have_occurred=True)
                return self._publish_recovery_locked(session_id, record)

        task = asyncio.create_task(recover_locked())
        self._recoveries.add(task)

        def settled(completed: asyncio.Task[RuntimeContextReadiness]) -> None:
            self._recoveries.discard(completed)
            if not completed.cancelled():
                failure = completed.exception()
                if failure is not None:
                    logger.warning("Runtime context recovery refused (%s)", type(failure).__name__)

        task.add_done_callback(settled)
        return await asyncio.shield(task)

    async def recovery_status(self, session_id: str, expected: CheckpointFence) -> tuple[str, RuntimeContextReadiness]:
        """Read the exact deterministic target; never retry recovery or a task."""
        if expected.session_id != session_id:
            raise RuntimeContextError("context_recovery_session_mismatch")
        async with self.lock_for(session_id):
            try:
                read = await self.store.runtime_checkpoint_read(session_id)
                target = self.store.runtime_checkpoint_recovery_fence(expected)
            except Exception:
                raise RuntimeContextError("context_recovery_unavailable") from None
            if read.record is None:
                raise RuntimeContextError(f"context_recovery_{read.status.value}")
            fence = read.record.fence
            if fence.generation != target.generation:
                status = "not_recovered" if fence == expected and read.status == CheckpointStatus.IN_PROGRESS else "superseded"
                return status, RuntimeContextReadiness(session_id, ContextReadinessState.CONFLICT, True)
            if fence != target or read.status != CheckpointStatus.READY:
                return "superseded", RuntimeContextReadiness(session_id, ContextReadinessState.CONFLICT, True)
            cached = self._fences.get(session_id)
            if cached != target and not self.has_writers(session_id):
                if cached is not None and cached != expected:
                    raise RuntimeContextError("context_recovery_conflict")
                return "recovered", self._publish_recovery_locked(session_id, read.record)
            readiness = await self._readiness_locked(session_id)
            return "recovered", readiness

    @asynccontextmanager
    async def _writer_registration(self, session_id: str) -> AsyncIterator[None]:
        self._writers[session_id] = self._writers.get(session_id, 0) + 1
        try:
            yield
        finally:
            remaining = self._writers[session_id] - 1
            if remaining:
                self._writers[session_id] = remaining
            else:
                self._writers.pop(session_id)

    def _reserve_managed(self, session_id: str) -> None:
        if session_id not in self._fences:
            if len(self._fences) >= self.store._runtime_checkpoint_limits.sessions:
                raise RuntimeContextError("context_session_quota")
            self._fences[session_id] = None

    async def attach(self, session_id: str, *, checkpoint_version: int | None = None) -> RuntimeContextAttachment:
        validate_session_id(session_id)
        if len(self._attachments) >= 128 or sum(token.session_id == session_id for token in self._attachments) >= 8:
            raise RuntimeContextError("context_attachment_quota")
        token = RuntimeContextAttachmentToken(session_id)
        self._attachments[token] = False  # Reserve before waiting for the SID lock.
        try:
            async with self.lock_for(session_id):
                if checkpoint_version is not None and (type(checkpoint_version) is not int or checkpoint_version != 1):
                    result = RuntimeContextReadiness(session_id, ContextReadinessState.UNSUPPORTED, await self.is_managed(session_id))
                elif await self.is_managed(session_id):
                    result = await self._readiness_locked(session_id, restore=True)
                elif checkpoint_version == 1:
                    if self.history.get(session_id) or self.store.working_get(session_id, limit=1):
                        result = RuntimeContextReadiness(session_id, ContextReadinessState.LEGACY_UNAVAILABLE, False)
                    else:
                        # Check capacity before any initialization creates a ledger row.
                        if len(self._fences) >= self.store._runtime_checkpoint_limits.sessions:
                            result = RuntimeContextReadiness(session_id, ContextReadinessState.QUOTA, False)
                        else:
                            created = await self.store.runtime_checkpoint_initialize_empty(session_id)
                            initialized = created.status == CheckpointStatus.APPLIED
                            self._attachments[token] = initialized
                            if created.status == CheckpointStatus.CONFLICT and created.record is None:
                                result = RuntimeContextReadiness(session_id, ContextReadinessState.LEGACY_UNAVAILABLE, False)
                            elif created.status == CheckpointStatus.QUOTA:
                                result = RuntimeContextReadiness(session_id, ContextReadinessState.QUOTA, False)
                            else:
                                result = await self._readiness_locked(session_id, restore=True, initialized=initialized)
                else:
                    result = RuntimeContextReadiness(session_id, ContextReadinessState.LEGACY, False)
                if result.state not in {ContextReadinessState.READY, ContextReadinessState.LEGACY}:
                    result = RuntimeContextReadiness(session_id, result.state, True, recovery_fence=result.recovery_fence)
                    self._attachment_refusals[token] = result
                if result.managed:
                    self._attachment_managed.add(token)
                return RuntimeContextAttachment(token, result)
        except asyncio.CancelledError:
            self._attachments.pop(token, None)
            self._attachment_managed.discard(token)
            self._attachment_refusals.pop(token, None)
            raise
        except Exception:
            # An established authenticated attachment can still reconcile receipts.
            # It has no READY claim and can never select legacy after DB failure.
            self._attachment_managed.add(token)
            result = RuntimeContextReadiness(session_id, ContextReadinessState.UNAVAILABLE, True)
            self._attachment_refusals[token] = result
            return RuntimeContextAttachment(token, result)

    async def _readiness_locked(self, session_id: str, *, restore: bool = False, initialized: bool = False) -> RuntimeContextReadiness:
        read = await self.store.runtime_checkpoint_read(session_id)
        if read.status == CheckpointStatus.ABSENT:
            return RuntimeContextReadiness(session_id, ContextReadinessState.CONFLICT if session_id in self._fences else ContextReadinessState.LEGACY, session_id in self._fences)
        self._reserve_managed(session_id)
        if read.status != CheckpointStatus.READY or read.record is None or read.record.context is None:
            states = {CheckpointStatus.IN_PROGRESS: ContextReadinessState.IN_PROGRESS,
                      CheckpointStatus.DELETED: ContextReadinessState.DELETED, CheckpointStatus.CORRUPT: ContextReadinessState.CORRUPT,
                      CheckpointStatus.UNSUPPORTED: ContextReadinessState.UNSUPPORTED}
            return RuntimeContextReadiness(session_id, states.get(read.status, ContextReadinessState.UNAVAILABLE), True,
                recovery_fence=read.record.fence if read.status == CheckpointStatus.IN_PROGRESS and read.record is not None else None)
        cached = self._fences.get(session_id)
        if cached is not None and cached != read.record.fence:
            return RuntimeContextReadiness(session_id, ContextReadinessState.CONFLICT, True)
        if restore:
            try:
                await self._restore_locked(session_id)
            except RuntimeContextError:
                return RuntimeContextReadiness(session_id, ContextReadinessState.CONFLICT, True)
        elif cached is None:
            return RuntimeContextReadiness(session_id, ContextReadinessState.CONFLICT, True)
        values = read.record.context.omissions()
        omissions = {key: value for key, value in values.items() if type(value) is int and isinstance(value, int)}
        return RuntimeContextReadiness(session_id, ContextReadinessState.READY, True, read.record.fence, initialized, omissions)

    async def readiness(self, token: RuntimeContextAttachmentToken) -> RuntimeContextReadiness:
        if token not in self._attachments:
            raise RuntimeContextError("context_attachment_invalid")
        async with self.lock_for(token.session_id):
            if token not in self._attachments:
                raise RuntimeContextError("context_attachment_invalid")
            try:
                result = await self._readiness_locked(token.session_id, initialized=self._attachments[token])
                refusal = self._attachment_refusals.get(token)
                if refusal is not None and result.state in {ContextReadinessState.LEGACY, ContextReadinessState.READY}:
                    return refusal  # Read-only polling cannot repair a refused attachment.
                if result.managed:
                    self._attachment_managed.add(token)
                elif token in self._attachment_managed:
                    return RuntimeContextReadiness(token.session_id, ContextReadinessState.UNAVAILABLE, True)
                return result
            except asyncio.CancelledError:
                raise
            except Exception:
                return RuntimeContextReadiness(token.session_id, ContextReadinessState.UNAVAILABLE, True)

    async def established_legacy_media_readiness(self, token: RuntimeContextAttachmentToken) -> RuntimeContextReadiness:
        """Read-only legacy media gate, independent of an active history writer."""
        def refusal() -> RuntimeContextReadiness | None:
            if token not in self._attachments:
                raise RuntimeContextError("context_attachment_invalid")
            refused = self._attachment_refusals.get(token)
            if refused is not None:
                return refused
            if token in self._attachment_managed or self.known_managed(token.session_id):
                return RuntimeContextReadiness(token.session_id, ContextReadinessState.UNAVAILABLE, True)
            return None
        rejected = refusal()
        if rejected is not None:
            return rejected
        store = self.store
        try:
            read = await asyncio.wait_for(store.runtime_checkpoint_read(token.session_id), timeout=LEGACY_MEDIA_READ_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            refusal()  # Detached/refused attachments cannot become permissive on failure.
            return RuntimeContextReadiness(token.session_id, ContextReadinessState.UNAVAILABLE, True)
        rejected = refusal()
        if rejected is not None:
            return rejected
        if self.store is not store or read.status != CheckpointStatus.ABSENT:
            return RuntimeContextReadiness(token.session_id, ContextReadinessState.UNAVAILABLE, True)
        return RuntimeContextReadiness(token.session_id, ContextReadinessState.LEGACY, False)

    async def detach(self, token: RuntimeContextAttachmentToken, *, clear_legacy: bool = False) -> bool:
        if token not in self._attachments:
            return False
        self._attachments.pop(token)
        self._attachment_managed.discard(token)
        self._attachment_refusals.pop(token, None)
        return await self.evict_if_unattached(token.session_id, clear_legacy=clear_legacy)

    async def evict_if_unattached(self, session_id: str, *, clear_legacy: bool = False) -> bool:
        validate_session_id(session_id)
        async with self.lock_for(session_id):
            if self.has_attachments(session_id) or self.has_writers(session_id):
                return False
            managed = await self.is_managed(session_id)
            if not managed and not clear_legacy:
                return False
            if not managed and self.finalize_legacy is not None:
                await self.finalize_legacy(session_id)
                if self.has_attachments(session_id) or self.has_writers(session_id):
                    return False
            self.history.pop(session_id, None)
            self.store.working_clear(session_id)
            self.clear_images(session_id)
            if self.clear_session is not None:
                self.clear_session(session_id)
            return True

    @asynccontextmanager
    async def legacy_mutation_scope(self, session_ids: tuple[str, ...], operation: str,
                                    *, command_handoff: bool = False) -> AsyncIterator[None]:
        """Serialize legacy mutation, refuse known managed context before effects."""
        from contextlib import AsyncExitStack
        identities = sorted(set(session_ids))
        for sid in identities:
            validate_session_id(sid)
        async with AsyncExitStack() as stack:
            owner = self._owner.get()
            if owner is not None and owner.task is asyncio.current_task() and owner.session_id in identities:
                if await self.is_managed(owner.session_id):
                    raise RuntimeContextError(f"managed_{operation}_unsupported")
                if len(identities) > 1:
                    raise RuntimeContextError("nested_context_mutation")
            for sid in identities:
                await stack.enter_async_context(self._writer_registration(sid))
                if owner is None or owner.task is not asyncio.current_task() or owner.session_id != sid:
                    await stack.enter_async_context(self.lock_for(sid))
                if await self.is_managed(sid):
                    raise RuntimeContextError(f"managed_{operation}_unsupported")
            token = None
            task = asyncio.current_task()
            if task is not None and len(identities) == 1 and (owner is None or owner.task is not task):
                token = self._owner.set(_OwnedScope(task, identities[0], command_handoff))
            try:
                yield
            finally:
                if token is not None:
                    self._owner.reset(token)

    async def _restore_locked(self, session_id: str) -> CheckpointFence | None:
        read = await self.store.runtime_checkpoint_read(session_id)
        cached = self._fences.get(session_id)
        if read.status == CheckpointStatus.ABSENT:
            if cached is not None or self.history.get(session_id) or self.store.working_get(session_id, limit=1):
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
        if not self.legacy_passthrough:
            self._reserve_managed(session_id)
        async with self._writer_registration(session_id), self.lock_for(session_id):
            if self.legacy_passthrough and not await self.is_managed(session_id):
                token = self._owner.set(_OwnedScope(task, session_id, command_handoff))
                try:
                    yield
                finally:
                    self._owner.reset(token)
                return
            self._reserve_managed(session_id)
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


def runtime_coordinator(orchestrator: object) -> RuntimeContextCoordinator | None:
    value = getattr(orchestrator, "_context_checkpoints", None)
    return value if isinstance(value, RuntimeContextCoordinator) else None


@asynccontextmanager
async def legacy_context_mutation(orchestrator: object, memory: object,
                                  session_ids: tuple[str, ...], operation: str,
                                  *, command_handoff: bool = False) -> AsyncIterator[None]:
    """No managed legacy mutation, including adapters without an orchestrator."""
    try:
        for sid in session_ids:
            validate_session_id(sid)
    except CheckpointValidationError:
        raise RuntimeContextError("context_invalid_session") from None
    coordinator = runtime_coordinator(orchestrator)
    if coordinator is not None:
        async with coordinator.legacy_mutation_scope(session_ids, operation, command_handoff=command_handoff):
            yield
        return
    # Some adapters are constructed before orchestration. They must not treat a
    # known ledger row as legacy simply because their coordinator is unavailable.
    from memory.store import MemoryStore
    if isinstance(memory, MemoryStore):
        for sid in session_ids:
            try:
                if (await memory.runtime_checkpoint_read(sid)).status != CheckpointStatus.ABSENT:
                    raise RuntimeContextError(f"managed_{operation}_unsupported")
            except asyncio.CancelledError:
                raise
            except RuntimeContextError:
                raise
            except Exception:
                raise RuntimeContextError("checkpoint_unavailable") from None
    yield
