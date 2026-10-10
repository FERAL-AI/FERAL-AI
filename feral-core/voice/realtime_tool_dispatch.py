"""Bounded serial tool work, separate from realtime event intake.

Cancellation requests do not prove an external effect stopped. A runner which
resists cancellation retains the lane until it actually exits.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

MAX_PENDING_TOOLS = 8
MAX_TOOL_EVENT_CHARS = 65_536
MAX_TURN_TOOL_IDS = 256
TOOL_FAILURE_SEND_TIMEOUT = 0.05
REALTIME_SEND_TIMEOUT = 2.0
logger = logging.getLogger("feral.voice.tool_dispatch")


@dataclass(frozen=True)
class _Work:
    key: tuple[int, str]
    current: Callable[[], bool]
    run: Callable[[], Awaitable[None]]


class RealtimeToolLane:
    def __init__(self, retained: set[asyncio.Task], *, max_pending: int = MAX_PENDING_TOOLS):
        self._retained = retained
        self._max_pending = max_pending
        self._pending: deque[_Work] = deque()
        self._active: _Work | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._seen: set[tuple[int, str]] = set()
        self._seen_epoch: int | None = None
        self._cancel_requested = False

    def seen(self, key: tuple[int, str]) -> bool:
        return key in self._seen

    def submit(self, key: tuple[int, str], current: Callable[[], bool], run: Callable[[], Awaitable[None]]) -> str:
        if self._closed or not current():
            return "stale"
        if self._seen_epoch != key[0]:
            self._seen.clear()
            self._seen_epoch = key[0]
        if key in self._seen:
            return "duplicate"
        if len(self._pending) + int(self._active is not None) >= self._max_pending or len(self._seen) >= MAX_TURN_TOOL_IDS:
            return "full"
        self._seen.add(key)
        self._pending.append(_Work(key, current, run))
        self._start()
        return "accepted"

    def _start(self) -> None:
        if self._closed or self._task is not None or not self._pending:
            return
        self._task = asyncio.create_task(self._run(), name="realtime-tool-lane")
        self._retained.add(self._task)
        self._task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self._retained.discard(task)
        # Retrieve failures even when intake has already disconnected. Provider
        # wrappers turn execution failures into truthful bounded wire results.
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning("Realtime tool lane failed: %s", type(error).__name__)
        if self._task is task:
            self._task = None
            self._cancel_requested = False
            self._start()

    async def _run(self) -> None:
        try:
            while self._pending and not self._closed:
                work = self._pending.popleft()
                if not work.current():
                    continue
                self._active = work
                try:
                    await work.run()
                finally:
                    self._active = None
        except asyncio.CancelledError:
            # Remaining valid work restarts only after this runner has exited.
            return

    def discard_obsolete(self) -> None:
        self._pending = deque(work for work in self._pending if work.current())
        if (self._active is not None and not self._active.current()
                and self._task is not None and not self._cancel_requested):
            self._cancel_requested = True
            self._task.cancel()

    def close(self) -> None:
        self._closed = True
        self._pending.clear()
        if self._task is not None and not self._cancel_requested:
            self._cancel_requested = True
            self._task.cancel()
