"""child-process adapter built on ``asyncio.create_subprocess_exec``.

FERAL ships the POSIX-first, asyncio-native cut: asyncio gives us the
TERM/KILL/wait primitives natively, so the adapter stays small.
Detached runs, Windows tree-kill, and verbatim-args are explicitly
out of scope.

Contract (consumed by ``supervisor.RunHandle``):

* ``stdout_queue`` / ``stderr_queue`` — ``asyncio.Queue`` of decoded
  lines (newline-stripped). ``None`` is pushed once when the
  corresponding stream reaches EOF.
* ``await wait()`` returns ``(returncode, signal_number)``. When the
  child is killed by a signal, ``returncode`` is the negative of that
  signal (asyncio convention) and ``signal_number`` is the absolute
  signal value.
* ``kill(grace_sec=5.0)`` sends ``SIGTERM`` immediately and schedules
  a ``SIGKILL`` after ``grace_sec`` if the child has not exited yet.
  ``grace_sec=0`` skips ``SIGTERM`` and goes straight to ``SIGKILL``.
"""

from __future__ import annotations

import asyncio
import signal as _signal
from typing import Optional


class ChildAdapter:
    """Async subprocess wrapper with line-buffered stdout/stderr queues."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.pid: int = proc.pid if proc.pid is not None else -1
        self.stdout_queue: asyncio.Queue = asyncio.Queue()
        self.stderr_queue: asyncio.Queue = asyncio.Queue()
        self._stdout_pump = asyncio.create_task(
            self._pump(proc.stdout, self.stdout_queue)
        )
        self._stderr_pump = asyncio.create_task(
            self._pump(proc.stderr, self.stderr_queue)
        )
        self._kill_task: Optional[asyncio.Task] = None
        self._sigterm_sent = False
        self._sigkill_sent = False

    @staticmethod
    async def _pump(
        stream: Optional[asyncio.StreamReader],
        queue: asyncio.Queue,
    ) -> None:
        if stream is None:
            await queue.put(None)
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                await queue.put(None)
                return
            await queue.put(
                chunk.decode("utf-8", errors="replace").rstrip("\r\n")
            )

    async def wait(self) -> tuple[Optional[int], Optional[int]]:
        rc = await self._proc.wait()
        # Drain the pumps so ``stdout_queue`` / ``stderr_queue`` have
        # received their EOF sentinel before we return — otherwise a
        # consumer awaiting the queue can deadlock past process exit.
        await asyncio.gather(
            self._stdout_pump, self._stderr_pump, return_exceptions=True
        )
        sig = -rc if rc is not None and rc < 0 else None
        return rc, sig

    def kill(self, grace_sec: float = 5.0) -> None:
        """SIGTERM, then SIGKILL after ``grace_sec`` seconds.

        ``grace_sec <= 0`` skips SIGTERM and sends SIGKILL immediately.
        Idempotent — calling twice does not re-send signals.
        """
        if self._sigkill_sent:
            return
        if grace_sec <= 0:
            self._send_sigkill()
            return
        if self._sigterm_sent:
            return
        self._sigterm_sent = True
        try:
            self._proc.send_signal(_signal.SIGTERM)
        except ProcessLookupError:
            return
        self._kill_task = asyncio.create_task(self._kill_after_grace(grace_sec))

    async def _kill_after_grace(self, grace_sec: float) -> None:
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace_sec)
        except asyncio.TimeoutError:
            self._send_sigkill()

    def _send_sigkill(self) -> None:
        if self._sigkill_sent:
            return
        self._sigkill_sent = True
        try:
            self._proc.kill()
        except ProcessLookupError:
            return


async def create_child_adapter(
    argv: list[str],
    *,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> ChildAdapter:
    """Spawn a child process and wrap it in :class:`ChildAdapter`."""
    if not argv:
        raise ValueError("argv must not be empty")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
    )
    return ChildAdapter(proc)
