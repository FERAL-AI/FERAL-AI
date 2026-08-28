"""Thread-backed async lock for serial device I/O, with a safety lane.

``asyncio.Lock`` binds to the event loop that first awaits it. Brain-local
USB adapters are touched from the FastAPI loop (telemetry), the voice
realtime loop, and cron/routine loops, so an asyncio lock raises
``RuntimeError: ... is bound to a different event loop`` on actuator
paths. Actual bot I/O already runs in worker threads via
``asyncio.to_thread``; a thread-backed lock serializes correctly across
loops.

Why there are two lanes
=======================
Mutual exclusion on the port is not negotiable: two concurrent
``readline()``s on one ``Serial`` interleave and corrupt the framing, and
a half-parsed frame on a robot is worse than a slow one. So preemption
here never means "interrupt the thread that owns the port". It means:

1. **Priority.** An urgent waiter (an emergency stop) is handed the lock
   ahead of every normal waiter already queued, whatever order they
   arrived in. A plain ``threading.Lock`` has no ordering guarantee at
   all, so ``priority=2 if capability_id == "halt"`` in the hardware
   orchestrator never reached anything that could act on it: a halt
   queued behind the very commands it exists to abort.

2. **A yield signal.** ``preempt_requested`` is true while an urgent
   waiter is pending. A holder that can be chopped into short, individually
   complete device operations (the telemetry drain, which is one blocking
   read with a timeout) checks it between slices and releases early. That
   bounds how long an emergency stop can be stuck behind routine
   telemetry, without ever leaving a partial transaction on the wire.

What this deliberately does NOT do
==================================
It does not abort a vendor call that is already in flight. A closed-loop
``go_to`` blocks inside the device library and there is no safe way to
reach into it from outside: issuing a second command on the same port
concurrently is exactly the corruption this lock exists to prevent.
An urgent waiter behind one of those still waits, and the wait is logged
so it is visible rather than mysterious.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import TracebackType
from typing import Optional, Type

logger = logging.getLogger("feral.hup.iolock")

# How long an urgent waiter may sit behind an uninterruptible holder before
# we say so. Not a timeout: the wait continues, because returning failure
# from an emergency stop would be a lie and abandoning the acquire would
# put two writers on the port.
_URGENT_WAIT_WARN_S = 1.0


class PriorityIOLock:
    """Async context manager over a two-lane, thread-backed mutex."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._held = False
        self._urgent_waiting = 0

    # -- introspection -------------------------------------------------

    @property
    def preempt_requested(self) -> bool:
        """True while an urgent acquirer is queued.

        Long holders that can be split into complete units poll this and
        give the port up at the next boundary.
        """
        return self._urgent_waiting > 0

    @property
    def locked(self) -> bool:
        return self._held

    # -- blocking core (runs on a worker thread) -----------------------

    def _acquire(self, urgent: bool) -> None:
        started = time.monotonic()
        warned = False
        with self._cond:
            if urgent:
                self._urgent_waiting += 1
            try:
                while self._held or (not urgent and self._urgent_waiting):
                    # ``wait`` releases the condition's mutex, so a holder
                    # can release and other waiters can re-evaluate.
                    self._cond.wait(timeout=_URGENT_WAIT_WARN_S)
                    if urgent and not warned and self._held:
                        warned = True
                        logger.warning(
                            "Emergency device command has waited %.1fs for the "
                            "serial port; a command already in flight cannot be "
                            "interrupted safely",
                            time.monotonic() - started,
                        )
            finally:
                if urgent:
                    self._urgent_waiting -= 1
            # Still holding the condition's mutex here, so the transition
            # from "chosen" to "holding" is atomic with respect to every
            # other waiter's re-check.
            self._held = True

    def _release(self) -> None:
        with self._cond:
            self._held = False
            self._cond.notify_all()

    # -- async surface -------------------------------------------------

    async def acquire(self, *, urgent: bool = False) -> None:
        await asyncio.to_thread(self._acquire, urgent)

    def release(self) -> None:
        self._release()

    async def __aenter__(self) -> "PriorityIOLock":
        await self.acquire(urgent=False)
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self._release()

    def urgent(self) -> "_UrgentAcquire":
        """``async with lock.urgent():`` for safety-critical commands."""
        return _UrgentAcquire(self)


class _UrgentAcquire:
    """Async context manager that takes the priority lane."""

    __slots__ = ("_lock",)

    def __init__(self, lock: PriorityIOLock) -> None:
        self._lock = lock

    async def __aenter__(self) -> PriorityIOLock:
        await self._lock.acquire(urgent=True)
        return self._lock

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self._lock.release()


# The original name. Every existing call site uses it as a plain
# ``async with``, which is still the normal lane, so nothing changes for
# adapters that have no safety-critical capability.
ThreadAsyncIOLock = PriorityIOLock
