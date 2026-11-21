"""Ingress integration for the existing trusted runtime-context coordinator."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from agents.runtime_context_checkpoint import (
    ContextReadinessState,
    RuntimeContextAttachment,
    RuntimeContextCoordinator,
    RuntimeContextError,
    RuntimeContextReadiness,
    RuntimeContextScopeReceipt,
)
from memory.runtime_session_checkpoint import validate_session_id
from memory.runtime_session_checkpoint import CheckpointFence
from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)
_cleanup_tasks: set[asyncio.Task[None]] = set()

if TYPE_CHECKING:
    from api.state import BrainState


def coordinator_for(state: BrainState) -> RuntimeContextCoordinator | None:
    value = getattr(getattr(state, "orchestrator", None), "_context_checkpoints", None)
    return value if isinstance(value, RuntimeContextCoordinator) else None


def session_query(query: Mapping[str, str], primary: str) -> tuple[str, bool, int | None]:
    """Preserve exact supplied identity; absence and explicit empty are different."""
    explicit = "session_id" in query
    sid = query["session_id"] if explicit else primary
    try:
        validate_session_id(sid)
    except ValueError:
        raise RuntimeContextError("context_session_invalid") from None
    if "context_checkpoint_version" not in query:
        version = None
    elif query["context_checkpoint_version"] == "1":
        version = 1
    else:
        raise RuntimeContextError("context_contract_unsupported")
    return sid, explicit, version


@asynccontextmanager
async def prepared_scope(state: BrainState, session_id: str, *,
                         attachment: RuntimeContextAttachment | None = None,
                         current_owner: Callable[[], bool] | None = None,
                         expected_fence: CheckpointFence | None = None
                         ) -> AsyncIterator[RuntimeContextScopeReceipt | None]:
    """Keep preparation and the one command in the same exact task and SID lock."""
    if expected_fence is not None and attachment is None:
        raise RuntimeContextError("context_review_invalid")
    coordinator = coordinator_for(state)
    if coordinator is None:
        if attachment is not None:
            raise RuntimeContextError("context_unavailable")
        yield None
    elif attachment is not None:
        if attachment.token.session_id != session_id or current_owner is None:
            raise RuntimeContextError("context_attachment_invalid")
        store = state.memory
        def owner_current() -> bool:
            return (coordinator_for(state) is coordinator and state.memory is store
                    and current_owner() is True)
        async with coordinator.attached_write_scope(attachment.token, expected_store=store,
                current_owner=owner_current, command_handoff=True, expected_fence=expected_fence) as receipt:
            yield receipt
    else:
        async with coordinator.write_scope(session_id, command_handoff=True) as receipt:
            yield receipt


async def attachment_readiness(coordinator: RuntimeContextCoordinator,
                               attachment: RuntimeContextAttachment) -> RuntimeContextReadiness:
    try:
        return await asyncio.wait_for(coordinator.readiness(attachment.token), timeout=10)
    except asyncio.CancelledError:
        raise
    except Exception:
        return RuntimeContextReadiness(attachment.token.session_id, ContextReadinessState.UNAVAILABLE, True)


async def require_attachment_ready(coordinator: RuntimeContextCoordinator | None,
                                   attachment: RuntimeContextAttachment | None,
                                   version: int | None) -> None:
    if coordinator is None or attachment is None:
        if version is not None:
            raise RuntimeContextError("context_unavailable")
        return
    readiness = await attachment_readiness(coordinator, attachment)
    # An initially refused/unknown managed attachment can never later downgrade
    # into legacy merely because a read returned absent or failed.
    requires_ready = version is not None or attachment.readiness.managed or readiness.managed
    if requires_ready:
        if not readiness.ready:
            raise RuntimeContextError(f"context_{readiness.state.value}")
    elif readiness.state != ContextReadinessState.LEGACY:
        raise RuntimeContextError(f"context_{readiness.state.value}")


async def established_legacy_media_readiness(state: BrainState, coordinator: RuntimeContextCoordinator,
                                           attachment: RuntimeContextAttachment) -> RuntimeContextReadiness:
    if attachment.readiness.managed:
        raise RuntimeContextError("managed_voice_unsupported")
    if coordinator_for(state) is not coordinator or state.memory is not coordinator.store:
        raise RuntimeContextError("context_unavailable")
    ready = await coordinator.established_legacy_media_readiness(attachment.token)
    if coordinator_for(state) is not coordinator or state.memory is not coordinator.store:
        raise RuntimeContextError("context_unavailable")
    return ready


async def close_surface(state: BrainState, *, ws: WebSocket, session_id: str,
                        chat_tasks: set[asyncio.Task], coordinator: RuntimeContextCoordinator | None,
                        attachment: RuntimeContextAttachment | None, legacy_attached: bool) -> None:
    """One retained cleanup path, including setup failure and superseded sockets.

    Deadlines do not cancel a still-owned cleanup or erase its context unlocked.
    The coordinator owns all managed/installed volatile history cleanup.
    """
    async def settle() -> None:
        from agents.chat_turns import get_chat_turn_manager

        drained = await get_chat_turn_manager(state).detach(ws)
        tasks = {task for task in chat_tasks if not task.done()}
        pending: set[asyncio.Task] = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=2)
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=2)
        chat_tasks.difference_update(task for task in chat_tasks if task.done())
        remaining = None
        if legacy_attached:
            try:
                remaining = state.detach_session(session_id)
            except Exception:
                logger.warning("Surface attachment bookkeeping could not be confirmed")
        # A replaced socket cannot remove the new route or its audio/perception.
        if state.sessions.get(session_id) is ws:
            voice = state.voice_router
            if voice is not None:
                nodes = getattr(voice, "nodes_bound_to_session", None)
                try:
                    bound = nodes(session_id) if callable(nodes) else []
                except Exception:
                    bound = None  # Unknown ownership cannot authorize teardown.
                if bound == []:
                    try:
                        await voice.stop_session_voice(session_id)
                    except Exception:
                        logger.warning("Owned voice teardown was not confirmed")
            if state.sessions.get(session_id) is ws:
                state.sessions.pop(session_id, None)
                try:
                    state.audio.clear_session(session_id)
                    state.perception.clear(session_id)
                except Exception:
                    logger.warning("Owned surface buffers could not be cleared")
        clear = remaining == 0 and drained and not pending and state.should_clear_on_disconnect(session_id)
        if coordinator is not None and attachment is not None:
            await coordinator.detach(attachment.token, clear_legacy=clear)
            return
        if clear:
            if state.orchestrator is not None:
                await state.orchestrator.on_session_disconnect(session_id)
            if state.identity_workspace is not None:
                await state.identity_workspace.maintenance_cycle(
                    memory_store=state.memory,
                    llm=state.orchestrator.llm if state.orchestrator is not None else None,
                    session_id=session_id,
                )
            state.memory.working_clear(session_id)
        elif remaining == 0 and drained and not pending and session_id == state.primary_session_id:
            state.snapshot_primary_thread(force=True)

    task = asyncio.create_task(settle())
    _cleanup_tasks.add(task)

    def settled(completed: asyncio.Task[None]) -> None:
        _cleanup_tasks.discard(completed)
        if not completed.cancelled() and completed.exception() is not None:
            logger.warning("Surface cleanup was not confirmed; volatile context was not reset")

    task.add_done_callback(settled)
    state.register_background_task(task)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("Surface cleanup is still pending; exact owned cleanup remains retained")
    except asyncio.CancelledError:
        # The retained task still owns cleanup. A second cancellation cannot
        # turn it into a blind refcount/working-memory reset.
        raise
    except Exception:
        logger.warning("Surface cleanup could not be confirmed")
