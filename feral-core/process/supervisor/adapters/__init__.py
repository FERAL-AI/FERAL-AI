"""spawn-adapter implementations for the process supervisor.

Two adapters, picked per-run via the ``adapter`` argument to
:meth:`ProcessSupervisor.run`:

* :mod:`process.supervisor.adapters.child` — plain
  ``asyncio.create_subprocess_exec`` (stdout + stderr piped, no TTY).
  Use this for every well-behaved CLI that does NOT check ``isatty``.
* :mod:`process.supervisor.adapters.pty` — ``pty.openpty`` + raw
  ``os.fork`` + ``os.execvp`` so the child sees a real controlling
  terminal. Required for Codex CLI / Claude Code CLI / any tool that
  refuses to render TUI output without ``isatty(stdout) == True``.
"""
