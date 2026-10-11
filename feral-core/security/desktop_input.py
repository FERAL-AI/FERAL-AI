"""Physical input settlement, not desktop authorization or target selection.

The process-wide lane remains owned by the actual worker after its subscriber
is cancelled. Cancellation fences later input; it cannot undo an OS call.
AX readers/semantic actions use cancellable workers without taking this lane.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
import threading
from typing import Callable, Iterator, TypeVar


class DesktopInputBusy(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class DesktopInputRevoked(RuntimeError):
    pass


class DesktopInputCleanupFailed(RuntimeError):
    pass


@dataclass(eq=False)
class _Operation:
    revoked: threading.Event = field(default_factory=threading.Event)
    worker_ident: int | None = None


_current: ContextVar[_Operation | None] = ContextVar("feral_desktop_input", default=None)
_state = threading.Lock()
_owner: _Operation | None = None
_cleanup_unknown = False
# Retain real concurrent futures, not cancelled asyncio wrappers. Both queued
# and executing work are bounded, including semantic AX reads.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feral-desktop")
_slots = threading.BoundedSemaphore(8)
_workers: set[Future] = set()
T = TypeVar("T")


def check_input() -> None:
    operation = _current.get()
    if operation is not None and operation.revoked.is_set():
        raise DesktopInputRevoked("Desktop input cancelled; prior effects may need checking")


def input_pause(seconds: float) -> None:
    """Interruptible pacing; no further input after revocation is observed."""
    operation = _current.get()
    if operation is None:
        threading.Event().wait(seconds)
    elif operation.revoked.wait(seconds):
        check_input()


def _claim(operation: _Operation) -> None:
    global _owner
    with _state:
        if _cleanup_unknown:
            raise DesktopInputBusy("desktop_input_cleanup_unknown")
        if _owner is not None:
            raise DesktopInputBusy("desktop_input_draining" if _owner.revoked.is_set() else "desktop_input_busy")
        if operation.revoked.is_set():
            raise DesktopInputRevoked("Desktop input cancelled before dispatch")
        _owner = operation


def _release(operation: _Operation) -> None:
    global _owner
    with _state:
        if _owner is operation:
            _owner = None


def cleanup_failed() -> None:
    """Do not hand potentially held input to a new owner after failed release."""
    global _cleanup_unknown
    with _state:
        _cleanup_unknown = True
    raise DesktopInputCleanupFailed("Desktop input release unverified; no new input admitted")


@contextmanager
def physical_input_scope() -> Iterator[None]:
    """Used inside GUI workers and AX's coordinate-only fallback."""
    operation = _current.get()
    token = None
    if operation is None:
        operation = _Operation(worker_ident=threading.get_ident())
        token = _current.set(operation)
    check_input()
    with _state:
        inherited = _owner is operation and operation.worker_ident == threading.get_ident()
    claimed = False
    try:
        if not inherited:
            _claim(operation)
            claimed = True
        check_input()
        yield
    finally:
        if claimed:
            _release(operation)
        if token is not None:
            _current.reset(token)


async def run_worker(handler: Callable[..., T], *args, physical: bool = False) -> T:
    """Retain actual completion and request cooperative revoke on cancellation."""
    operation = _Operation()
    if physical:
        _claim(operation)
    if not _slots.acquire(blocking=False):
        if physical:
            _release(operation)
        raise DesktopInputBusy("desktop_worker_capacity")
    context = copy_context()

    def invoke():
        operation.worker_ident = threading.get_ident()
        token = _current.set(operation)
        try:
            check_input()
            return handler(*args)
        finally:
            _current.reset(token)
            if physical:
                _release(operation)

    try:
        worker = _pool.submit(context.run, invoke)
    except BaseException:
        _slots.release()
        if physical:
            _release(operation)
        raise
    with _state:
        _workers.add(worker)

    def settled(future):
        with _state:
            _workers.discard(future)
        _slots.release()

    worker.add_done_callback(settled)
    wrapped = asyncio.wrap_future(worker)
    # Consume exceptions even when the subscriber has already gone away.
    wrapped.add_done_callback(lambda future: None if future.cancelled() else future.exception())
    try:
        return await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        operation.revoked.set()
        raise


def input_failure(error: RuntimeError) -> dict:
    reason = error.reason if isinstance(error, DesktopInputBusy) else (
        "desktop_input_cleanup_unknown" if isinstance(error, DesktopInputCleanupFailed) else "desktop_input_cancelled")
    return {"success": False, "status_code": 409, "data": {"outcome": "unknown"} if isinstance(
        error, (DesktopInputRevoked, DesktopInputCleanupFailed)) else None,
        "reason": reason, "error": reason + "; no new input admitted; earlier effects may need checking"}
