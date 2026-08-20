"""ProcessSupervisor — the top-level run() / scope_cancel() surface.

Two kinds of timeout ride the supervisor; each kills the child and
records a distinct ``kill_reason`` on the :class:`RunRecord`:

* ``overall_timeout_sec`` — hard wall-clock kill
  (``kill_reason="overall-timeout"``).
* ``no_output_timeout_sec`` — fires when stdout *and* stderr go silent
  for that many seconds (``kill_reason="no-output-timeout"``).

The ``scope_key`` argument composes with 
``agents/subagent_spawner.py`` scope semantics — same string shape
(arbitrary caller-chosen key), same "kill the whole family with one
call" guarantee. Cancellation propagates within ~few-ms of
``scope_cancel`` for processes that respect SIGTERM (the 
acceptance budget is 200ms).

This PR ships the abstraction READY for downstream wiring (voice
service-restart, future Codex CLI / Claude Code CLI integrations,
ffmpeg pipelines). It is **not wired** into ``agents/orchestrator``
or any service in this PR — the spec is explicit that no callers
are added here.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional, Sequence, Union
from uuid import uuid4

from .adapters.child import ChildAdapter, create_child_adapter
from .adapters.pty import PtyAdapter, create_pty_adapter
from .buffer import BoundedLineBuffer
from .registry import KillReason, RunRecord, RunRegistry

CmdType = Union[str, Sequence[str]]
AdapterType = Union[ChildAdapter, PtyAdapter]


class RunHandle:
    """Caller-facing handle for a single supervised run.

    Carries the ``run_id``, the child ``pid``, and both ``wait`` and
    ``cancel`` callables. ``await wait()`` resolves to the finalized
    :class:`RunRecord` (so callers do not need to round-trip through
    the registry to learn the kill_reason / exit_code).
    """

    def __init__(
        self,
        *,
        run_id: str,
        pid: int,
        scope_key: Optional[str],
        adapter: AdapterType,
        registry: RunRegistry,
        on_finalize,
        max_buffered_lines: int = 0,
        max_line_chars: int = 0,
    ) -> None:
        self.run_id = run_id
        self.pid = pid
        self.scope_key = scope_key
        self._adapter = adapter
        self._registry = registry
        self._on_finalize = on_finalize
        self._kill_reason: Optional[KillReason] = None
        # 0/0 == unbounded, which is what every pre-existing caller gets:
        # short-lived runs that are read once after exit. Background /
        # streaming callers pass real bounds so a process that prints
        # forever cannot grow the brain's heap forever.
        self._stdout_lines = BoundedLineBuffer(max_buffered_lines, max_line_chars)
        self._stderr_lines = BoundedLineBuffer(max_buffered_lines, max_line_chars)
        self._last_output_at = time.monotonic()
        self._final_record: Optional[RunRecord] = None
        self._finalize_event = asyncio.Event()
        self._monitor_task: Optional[asyncio.Task] = None
        self._cancelled = False

    # ── public read-only views ───────────────────────────────────────

    @property
    def stdout(self) -> str:
        return self._stdout_lines.text()

    @property
    def stderr(self) -> str:
        return self._stderr_lines.text()

    @property
    def stdout_buffer(self) -> BoundedLineBuffer:
        """The live stdout ring buffer (incremental cursor reads)."""
        return self._stdout_lines

    @property
    def stderr_buffer(self) -> BoundedLineBuffer:
        return self._stderr_lines

    @property
    def kill_reason(self) -> Optional[KillReason]:
        return self._kill_reason

    @property
    def finished(self) -> bool:
        """True once the run is finalized (record published)."""
        return self._final_record is not None

    @property
    def record(self) -> Optional[RunRecord]:
        """The finalized record, or None while the run is still going.

        Lets a poller read the outcome without ``await wait()`` blocking
        it for the lifetime of a long background job.
        """
        return self._final_record

    # ── lifecycle ────────────────────────────────────────────────────

    def _start(
        self,
        *,
        overall_timeout_sec: Optional[float],
        no_output_timeout_sec: Optional[float],
    ) -> None:
        self._monitor_task = asyncio.create_task(
            self._monitor(
                overall_timeout_sec=overall_timeout_sec,
                no_output_timeout_sec=no_output_timeout_sec,
            )
        )

    async def _monitor(
        self,
        *,
        overall_timeout_sec: Optional[float],
        no_output_timeout_sec: Optional[float],
    ) -> None:
        consumer_tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(
                self._consume(self._adapter.stdout_queue, self._stdout_lines)
            ),
            asyncio.create_task(
                self._consume(self._adapter.stderr_queue, self._stderr_lines)
            ),
        ]
        wait_task = asyncio.create_task(self._adapter.wait())

        timer_tasks: list[asyncio.Task[Any]] = []
        if overall_timeout_sec is not None and overall_timeout_sec > 0:
            timer_tasks.append(
                asyncio.create_task(self._overall_timer(overall_timeout_sec))
            )
        if no_output_timeout_sec is not None and no_output_timeout_sec > 0:
            timer_tasks.append(
                asyncio.create_task(
                    self._no_output_watcher(no_output_timeout_sec)
                )
            )

        exit_code: Optional[int] = None
        try:
            exit_code, _signal = await wait_task
        finally:
            for t in timer_tasks:
                if not t.done():
                    t.cancel()
            for t in consumer_tasks:
                if not t.done():
                    t.cancel()
            # Let cancellations settle and consumers drain remaining
            # queue items so ``stdout`` / ``stderr`` are fully populated
            # before we publish the finalized record.
            await asyncio.gather(*timer_tasks, return_exceptions=True)
            await asyncio.gather(*consumer_tasks, return_exceptions=True)

        record = await self._registry.finalize(
            self.run_id,
            exit_code=exit_code,
            kill_reason=self._kill_reason or "exit",
        )
        self._final_record = record
        self._finalize_event.set()
        await self._on_finalize(self.run_id)

    async def _consume(
        self, queue: asyncio.Queue, sink: BoundedLineBuffer
    ) -> None:
        while True:
            line = await queue.get()
            if line is None:
                return
            sink.append(line)
            self._last_output_at = time.monotonic()

    async def _overall_timer(self, sec: float) -> None:
        await asyncio.sleep(sec)
        self._trigger_kill("overall_timeout")

    async def _no_output_watcher(self, sec: float) -> None:
        # Poll the last-output timestamp; if the gap reaches ``sec``,
        # fire the kill. We use a poll interval of ``min(sec, 0.05)``
        # so short timeouts (used in tests + tight deadlines) stay
        # responsive without burning CPU on long timeouts.
        poll = max(0.01, min(0.05, sec / 4))
        while True:
            gap = time.monotonic() - self._last_output_at
            if gap >= sec:
                self._trigger_kill("no_output_timeout")
                return
            await asyncio.sleep(min(poll, sec - gap))

    def _trigger_kill(self, reason: KillReason) -> None:
        if self._kill_reason is not None:
            return
        self._kill_reason = reason
        # ``grace_sec=5.0`` matches the spec ladder (TERM then KILL
        # after 5s grace); for processes that respect SIGTERM (the
        # common case — sleep, curl, ffmpeg, codex CLI) this dies
        # within milliseconds and well within the 200ms budget.
        self._adapter.kill(grace_sec=5.0)

    # ── public control ───────────────────────────────────────────────

    def cancel(self, reason: KillReason = "manual_cancel") -> None:
        """Synchronously trigger termination. Idempotent."""
        if self._cancelled:
            return
        self._cancelled = True
        self._trigger_kill(reason)

    async def wait(self) -> RunRecord:
        """Block until the run is finalized; return the RunRecord."""
        await self._finalize_event.wait()
        # _final_record is set before _finalize_event.set() — the
        # assertion below is a defensive sanity check, not a TODO.
        assert self._final_record is not None
        return self._final_record


class ProcessSupervisor:
    """Top-level supervisor — owns the registry + the active-run map.

    One instance per integration boundary (e.g. one for the future
    voice subprocess pool, one for the future Codex CLI integration).
    Cheap to construct; expensive callers should keep a single
    supervisor in module scope rather than re-creating per call.
    """

    def __init__(self) -> None:
        self._registry = RunRegistry()
        self._active: dict[str, RunHandle] = {}
        self._lock = asyncio.Lock()

    @property
    def registry(self) -> RunRegistry:
        return self._registry

    async def run(
        self,
        cmd: CmdType,
        *,
        scope_key: Optional[str] = None,
        overall_timeout_sec: Optional[float] = None,
        no_output_timeout_sec: Optional[float] = None,
        env: Optional[dict] = None,
        cwd: Optional[str] = None,
        adapter: str = "child",
        max_buffered_lines: int = 0,
        max_line_chars: int = 0,
        start_new_session: bool = False,
    ) -> RunHandle:
        """Spawn a supervised process and return its :class:`RunHandle`.

        ``cmd`` is a list/tuple for ``adapter='child'`` and a string
        for ``adapter='pty'`` (the PTY adapter always runs the command
        through a login shell; passing argv here would be a category
        error). Validates this at the boundary so callers fail loudly.

        ``max_buffered_lines`` / ``max_line_chars`` bound the retained
        output (0 = unbounded, the historical behaviour). A bounded
        handle keeps the newest lines and reports how many it dropped -
        see :class:`process.supervisor.buffer.BoundedLineBuffer`.

        ``start_new_session=True`` (child adapter only) makes kills
        reach the child's whole process group, so a shell command's
        grandchildren cannot outlive the run.
        """
        if adapter == "child":
            if isinstance(cmd, str) or not isinstance(cmd, (list, tuple)):
                raise TypeError(
                    "adapter='child' requires cmd as a list/tuple of args"
                )
            adapter_obj: AdapterType = await create_child_adapter(
                list(cmd),
                env=env,
                cwd=cwd,
                start_new_session=start_new_session,
            )
        elif adapter == "pty":
            if not isinstance(cmd, str):
                raise TypeError(
                    "adapter='pty' requires cmd as a string (login-shell -c)"
                )
            if start_new_session:
                # The PTY adapter already calls setsid() in the forked
                # child; asking for a second one would be a lie about
                # what this flag does here.
                raise ValueError(
                    "start_new_session is implicit for adapter='pty' "
                    "(the fork already calls setsid)"
                )
            adapter_obj = await create_pty_adapter(cmd, env=env, cwd=cwd)
        else:
            raise ValueError(f"unknown adapter: {adapter!r}")

        run_id = str(uuid4())
        record = RunRecord(
            run_id=run_id,
            pid=adapter_obj.pid,
            scope_key=scope_key,
            started_at=time.monotonic(),
        )
        await self._registry.register(record)

        handle = RunHandle(
            run_id=run_id,
            pid=adapter_obj.pid,
            scope_key=scope_key,
            adapter=adapter_obj,
            registry=self._registry,
            on_finalize=self._on_finalize,
            max_buffered_lines=max_buffered_lines,
            max_line_chars=max_line_chars,
        )
        async with self._lock:
            self._active[run_id] = handle
        handle._start(
            overall_timeout_sec=overall_timeout_sec,
            no_output_timeout_sec=no_output_timeout_sec,
        )
        return handle

    async def _on_finalize(self, run_id: str) -> None:
        async with self._lock:
            self._active.pop(run_id, None)

    async def scope_cancel(
        self,
        scope_key: str,
        reason: KillReason = "manual_cancel",
    ) -> int:
        """Cancel every running child whose ``scope_key`` matches.

        Synchronous-style: fires SIGTERM on each child and returns the
        count immediately. Callers wanting to block until every child
        has actually exited should iterate ``registry.list_by_scope``
        and ``registry.wait_for_finish``.
        """
        if not scope_key or not scope_key.strip():
            return 0
        async with self._lock:
            handles = [
                h for h in self._active.values() if h.scope_key == scope_key
            ]
        for h in handles:
            h.cancel(reason)
        return len(handles)


def create_process_supervisor() -> ProcessSupervisor:
    """Factory for :class:`ProcessSupervisor`.

    Returns a fresh, isolated supervisor — registries are not shared
    across calls.
    """
    return ProcessSupervisor()
