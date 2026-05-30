"""PTY adapter — spawns commands inside a real controlling TTY.

FERAL uses the stdlib ``pty`` + raw ``os.fork`` + ``os.execvpe`` because
(a) we want zero non-stdlib dependencies for the supervisor, (b) the
use case is narrow (CLIs that check ``isatty()``: Codex CLI, Claude
Code CLI, ``ssh -t``, ``top`` smoke probes), and (c) Python's ``pty``
module hands us exactly the mechanism we need on POSIX.

Windows is explicitly unsupported — we raise ``NotImplementedError``
at adapter creation time. ConPTY would be the right Windows answer
later but it requires ``pywin32`` + a non-trivial implementation; out
of scope for .

The child runs under a **login shell** (``/bin/zsh -l`` on macOS,
``/bin/bash -l`` on Linux) so the child sees the operator's normal
``PATH`` / ``rc`` files — Codex CLI / Claude Code CLI need this to
find homebrew-installed binaries the way the operator runs them
interactively.
"""

from __future__ import annotations

import asyncio
import errno
import os
import pty
import signal as _signal
import sys
from typing import Optional

WINDOWS_NOT_SUPPORTED = (
    "PTY adapter requires POSIX (pty.openpty + os.fork are unavailable on "
    "Windows). Use the child adapter or wait for a future ConPTY adapter."
)


def _login_shell() -> str:
    """Return the absolute path to the platform's default login shell.

    macOS ships zsh as the default user shell since 10.15; mainstream
    Linux distros ship bash. We hard-code ``/bin/zsh`` and ``/bin/bash``
    because callers can override behavior by passing a custom command —
    the shell only matters for ``-l`` semantics.
    """
    if sys.platform == "darwin":
        return "/bin/zsh"
    return "/bin/bash"


class PtyAdapter:
    """PTY-backed process wrapper.

    The PTY merges the child's stdout and stderr into a single stream;
    we publish lines into ``stdout_queue`` and immediately push the EOF
    sentinel ``None`` onto ``stderr_queue`` so the supervisor's stderr
    consumer drains and exits cleanly.
    """

    def __init__(self, master_fd: int, pid: int) -> None:
        self._master_fd = master_fd
        self.pid = pid
        self.stdout_queue: asyncio.Queue = asyncio.Queue()
        self.stderr_queue: asyncio.Queue = asyncio.Queue()
        # PTYs are a single stream; signal EOF on stderr immediately so
        # callers that pump both queues do not block forever waiting
        # for stderr lines that will never arrive.
        self.stderr_queue.put_nowait(None)
        self._buf = b""
        self._exit_event = asyncio.Event()
        self._exit_status: tuple[Optional[int], Optional[int]] = (None, None)
        self._kill_task: Optional[asyncio.Task] = None
        self._sigterm_sent = False
        self._sigkill_sent = False
        self._reader_attached = False
        self._fd_closed = False
        self._wait_task = asyncio.create_task(self._await_child())
        self._attach_reader()

    def _attach_reader(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_reader(self._master_fd, self._on_readable)
        self._reader_attached = True

    def _detach_reader(self) -> None:
        if not self._reader_attached:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.remove_reader(self._master_fd)
        except RuntimeError:
            pass
        self._reader_attached = False

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master_fd, 4096)
        except OSError as exc:
            # EIO is the canonical "child closed the slave end" signal
            # on Linux; macOS sometimes returns EIO too. Treat as EOF.
            if exc.errno in (errno.EIO, errno.EBADF):
                self._handle_eof()
                return
            raise
        if not data:
            self._handle_eof()
            return
        self._buf += data
        while b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n")
            self.stdout_queue.put_nowait(
                line.decode("utf-8", errors="replace").rstrip("\r")
            )

    def _handle_eof(self) -> None:
        if self._buf:
            self.stdout_queue.put_nowait(
                self._buf.decode("utf-8", errors="replace").rstrip("\r\n")
            )
            self._buf = b""
        self.stdout_queue.put_nowait(None)
        self._detach_reader()
        if not self._fd_closed:
            self._fd_closed = True
            try:
                os.close(self._master_fd)
            except OSError:
                pass

    async def _await_child(self) -> None:
        # ``os.waitpid`` blocks the OS thread; running it in a worker
        # thread keeps the asyncio loop responsive (so ``kill`` and
        # ``wait`` can interleave without blocking).
        _pid, status = await asyncio.to_thread(os.waitpid, self.pid, 0)
        if os.WIFEXITED(status):
            code: Optional[int] = os.WEXITSTATUS(status)
            sig: Optional[int] = None
        elif os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            code = -sig
        else:
            code = None
            sig = None
        self._exit_status = (code, sig)
        self._exit_event.set()

    async def wait(self) -> tuple[Optional[int], Optional[int]]:
        await self._exit_event.wait()
        # Give the reader one tick to drain any final bytes the kernel
        # buffered after the child exited (TTYs are line-discipline-
        # buffered; without this hop, short commands like ``tty`` can
        # race the reader).
        await asyncio.sleep(0.05)
        if self._reader_attached:
            self._handle_eof()
        return self._exit_status

    def kill(self, grace_sec: float = 5.0) -> None:
        """SIGTERM, then SIGKILL after ``grace_sec``. Idempotent."""
        if self._sigkill_sent:
            return
        if grace_sec <= 0:
            self._send_sigkill()
            return
        if self._sigterm_sent:
            return
        self._sigterm_sent = True
        try:
            os.kill(self.pid, _signal.SIGTERM)
        except ProcessLookupError:
            return
        self._kill_task = asyncio.create_task(self._kill_after_grace(grace_sec))

    async def _kill_after_grace(self, grace_sec: float) -> None:
        try:
            await asyncio.wait_for(self._exit_event.wait(), timeout=grace_sec)
        except asyncio.TimeoutError:
            self._send_sigkill()

    def _send_sigkill(self) -> None:
        if self._sigkill_sent:
            return
        self._sigkill_sent = True
        try:
            os.kill(self.pid, _signal.SIGKILL)
        except ProcessLookupError:
            return


async def create_pty_adapter(
    cmd: str,
    *,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> PtyAdapter:
    """Fork+exec ``cmd`` under a login shell, attached to a fresh PTY.

    The command is passed as ``shell -l -c cmd`` — exactly the shape an
    operator would type at an interactive prompt. Raises
    ``NotImplementedError`` on Windows; ``ValueError`` on empty cmd.
    """
    if sys.platform == "win32":
        raise NotImplementedError(WINDOWS_NOT_SUPPORTED)
    if not cmd or not cmd.strip():
        raise ValueError("pty cmd must not be empty")

    master_fd, slave_fd = pty.openpty()
    pid = os.fork()
    if pid == 0:
        # ── child ────────────────────────────────────────────────
        # Detach from the parent's controlling terminal and adopt the
        # slave end as the new controlling TTY. ``setsid`` makes us a
        # session leader; ``TIOCSCTTY`` then claims the TTY. Without
        # this, ``isatty(0/1/2)`` would still be true on the parent's
        # TTY but the new PTY would not be the *controlling* TTY,
        # which is what TUI apps actually inspect.
        os.setsid()
        try:
            import fcntl
            import termios

            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except (ImportError, OSError):
            pass
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if cwd:
            try:
                os.chdir(cwd)
            except OSError:
                os._exit(126)
        shell = _login_shell()
        # Standard login(8)/getty convention: argv[0] gets a leading
        # dash so the shell sees itself as a login shell. This is in
        # addition to the explicit -l flag because on Linux bash the
        # -l flag DOES enter login mode but $0 stays "/bin/bash" —
        # the dash convention is what lets `[[ $0 == -* ]]`
        # (and `shopt -q login_shell` indirectly) detect it
        # consistently across macOS zsh and Linux bash.
        shell_basename = os.path.basename(shell)
        argv = [f"-{shell_basename}", "-l", "-c", cmd]
        child_env = dict(env) if env is not None else os.environ.copy()
        try:
            os.execvpe(shell, argv, child_env)
        except OSError:
            os._exit(127)
        # execvpe replaces the process; reaching this line means it
        # failed but the OSError handler above already exited.
        os._exit(127)

    # ── parent ───────────────────────────────────────────────────
    os.close(slave_fd)
    return PtyAdapter(master_fd, pid)
