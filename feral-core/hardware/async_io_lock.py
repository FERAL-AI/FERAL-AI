"""Thread-backed async lock for serial device I/O.

``asyncio.Lock`` binds to the event loop that first awaits it. Brain-local
USB adapters are touched from the FastAPI loop (telemetry), the voice
realtime loop, and cron/routine loops — so an asyncio lock raises
``RuntimeError: ... is bound to a different event loop`` on actuator
paths. Actual bot I/O already runs in worker threads via
``asyncio.to_thread``; a ``threading.Lock`` serializes correctly across
loops.
"""

from __future__ import annotations

import asyncio
import threading
from types import TracebackType
from typing import Optional, Type


class ThreadAsyncIOLock:
    """Async context manager wrapping ``threading.Lock``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def __aenter__(self) -> "ThreadAsyncIOLock":
        await asyncio.to_thread(self._lock.acquire)
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self._lock.release()
