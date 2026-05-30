"""W18: in-memory RunRegistry + RunRecord dataclass.

The registry is owned by per-process integrations; records die with the
process, so we do NOT bound the exited-record cache. We expose the four
methods named in the W18 spec (register / finalize / list_active /
list_by_scope) plus ``wait_for_finish`` so callers can block on a
specific run id.

Async-safe via ``asyncio.Lock``. Not thread-safe — the supervisor is
asyncio-native and all writes must flow through the event loop that
owns the registry.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Optional

KillReason = str  # "overall_timeout" | "no_output_timeout" | "manual_cancel" | "exit"


@dataclass
class RunRecord:
    """Snapshot of one supervised process.

    Field names follow the W18 spec verbatim: run_id, pid, scope_key,
    started_at, finished_at, exit_code, kill_reason. ``started_at`` and
    ``finished_at`` are ``time.monotonic()`` values (not wall-clock) —
    the supervisor uses them only for relative timing / timeout math.
    """

    run_id: str
    pid: Optional[int]
    scope_key: Optional[str]
    started_at: float
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    kill_reason: Optional[KillReason] = None
    extra: dict = field(default_factory=dict)


class RunRegistry:
    """Async-safe in-memory registry of RunRecords keyed by run_id.

    The four methods named in the W18 spec are :meth:`register`,
    :meth:`finalize`, :meth:`list_active`, :meth:`list_by_scope`. We
    additionally expose :meth:`wait_for_finish` (used by tests and by
    higher-level callers that need to block on a specific run) and
    :meth:`get`.
    """

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def register(self, record: RunRecord) -> None:
        async with self._lock:
            self._records[record.run_id] = replace(record)
            self._waiters.setdefault(record.run_id, asyncio.Event())

    async def finalize(
        self,
        run_id: str,
        *,
        exit_code: Optional[int],
        kill_reason: Optional[KillReason],
    ) -> RunRecord:
        async with self._lock:
            current = self._records.get(run_id)
            if current is None:
                raise KeyError(f"unknown run_id: {run_id}")
            if current.finished_at is None:
                current.finished_at = time.monotonic()
                current.exit_code = exit_code
                current.kill_reason = kill_reason
            event = self._waiters.setdefault(run_id, asyncio.Event())
            snapshot = replace(current)
        event.set()
        return snapshot

    async def get(self, run_id: str) -> Optional[RunRecord]:
        async with self._lock:
            current = self._records.get(run_id)
            return replace(current) if current is not None else None

    async def list_active(self) -> list[RunRecord]:
        async with self._lock:
            return [
                replace(r)
                for r in self._records.values()
                if r.finished_at is None
            ]

    async def list_by_scope(self, scope_key: str) -> list[RunRecord]:
        async with self._lock:
            return [
                replace(r)
                for r in self._records.values()
                if r.scope_key == scope_key
            ]

    async def wait_for_finish(self, run_id: str) -> RunRecord:
        async with self._lock:
            current = self._records.get(run_id)
            if current is None:
                raise KeyError(f"unknown run_id: {run_id}")
            if current.finished_at is not None:
                return replace(current)
            event = self._waiters.setdefault(run_id, asyncio.Event())
        await event.wait()
        async with self._lock:
            return replace(self._records[run_id])
