"""audit-r14 / lane-07 (R2-002) — pure-local CLI commands MUST NOT
touch the network.

The pre-Lane-07 CLI silently routed unknown args to ``one_shot()``,
which opened a WebSocket to ``ws://localhost:9090/v1/session`` and
printed ``Cannot connect to FERAL Brain at ...`` for ``feral
--version`` on a fresh venv with no brain running. This file pins:

1. ``feral --version`` prints the version + exits 0 with no socket
   connection attempt and no ``Cannot connect`` text on stderr.
2. ``feral --version`` returns quickly (wall-clock + post-import time
   target). Python interpreter startup itself is unavoidable, so the
   meaningful gate is "no network call, no retry/timeout sleep."
3. ``feral --foo`` (flag typo) raises a parser error rather than
   routing to ``one_shot()``.
4. The ``PURE_LOCAL_SUBCOMMANDS`` / ``NEEDS_BRAIN_SUBCOMMANDS`` lists
   in ``cli/main.py`` cover every registered top-level subcommand.
"""

from __future__ import annotations

import io
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


@contextmanager
def _block_all_socket_connect(monkeypatch):
    """Patch ``socket.socket.connect`` so any attempt raises.

    Used to prove that pure-local commands NEVER open a TCP socket
    during their dispatch. We don't patch ``socket.socket.__init__``
    itself because rich/InquirerPy/argparse may indirectly poke a
    pty/stdio fd; only ``connect()`` is meaningful for "did we try to
    talk to a brain?".
    """
    real_connect = socket.socket.connect

    attempts: list = []

    def _refused(self, addr, *args, **kwargs):
        attempts.append(addr)
        raise AssertionError(
            f"pure-local command attempted to connect to {addr!r} — "
            "this violates R2-002. Stack: see test traceback."
        )

    monkeypatch.setattr(socket.socket, "connect", _refused, raising=True)
    try:
        yield attempts
    finally:
        monkeypatch.setattr(socket.socket, "connect", real_connect, raising=True)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_version_flag_prints_version_with_no_network(monkeypatch, capsys):
    """``feral --version`` short-circuits before any network call."""
    from cli import main as cli_main

    monkeypatch.setattr(sys, "argv", ["feral", "--version"])
    with _block_all_socket_connect(monkeypatch):
        rc = cli_main.main()

    out = capsys.readouterr()
    combined = (out.out or "") + (out.err or "")
    assert "feral-ai" in combined, f"expected 'feral-ai <version>', got {combined!r}"
    assert "Cannot connect to FERAL Brain" not in combined
    # The fast path returns 0 from ``main()``; argparse's
    # ``action="version"`` raises ``SystemExit(0)`` instead.
    assert rc == 0 or rc is None


def test_short_version_flag_prints_version(monkeypatch, capsys):
    """``feral -V`` is the documented short alias (``feral --help``
    lists both). Same fast-path semantics."""
    from cli import main as cli_main

    monkeypatch.setattr(sys, "argv", ["feral", "-V"])
    with _block_all_socket_connect(monkeypatch):
        rc = cli_main.main()
    out = capsys.readouterr()
    combined = (out.out or "") + (out.err or "")
    assert "feral-ai" in combined
    assert rc == 0 or rc is None


def test_version_flag_via_argparse_path_no_network(monkeypatch):
    """``feral --host x --port y --version`` flows through argparse's
    ``action="version"`` — that path also must not open a socket."""
    from cli import main as cli_main

    monkeypatch.setattr(sys, "argv", ["feral", "--host", "x", "--port", "9090", "--version"])
    with _block_all_socket_connect(monkeypatch):
        with pytest.raises(SystemExit) as excinfo:
            cli_main.main()
    assert excinfo.value.code == 0


def test_help_flag_does_not_touch_network(monkeypatch):
    """``feral --help`` is pure-local — argparse's ``--help`` action
    prints + exits before any dispatch. Pins parent ack reminder #1."""
    from cli import main as cli_main

    monkeypatch.setattr(sys, "argv", ["feral", "--help"])
    with _block_all_socket_connect(monkeypatch):
        with pytest.raises(SystemExit) as excinfo:
            cli_main.main()
    assert excinfo.value.code == 0


def test_unknown_flag_does_not_route_to_brain(monkeypatch, capsys):
    """``feral --verison`` (typo) MUST surface a parser error, not
    silently send the literal string to the brain."""
    from cli import main as cli_main

    monkeypatch.setattr(sys, "argv", ["feral", "--verison"])
    with _block_all_socket_connect(monkeypatch):
        with pytest.raises(SystemExit) as excinfo:
            cli_main.main()
    # argparse exits 2 on parser_error
    assert excinfo.value.code == 2
    out = capsys.readouterr()
    combined = (out.out or "") + (out.err or "")
    assert "Cannot connect to FERAL Brain" not in combined
    assert "unrecognized" in combined.lower()


def test_version_subprocess_under_timeout_no_brain():
    """End-to-end: spawn ``python -m`` style process, time it, assert
    no ``Cannot connect to FERAL Brain`` text and reasonable wall-clock.

    Python interpreter startup is ~80-150ms — the original R2-002
    spec target was <100ms which a CPython entry point can't hit, but
    the *intent* is "no brain dependency". We pin <2000ms (loose
    enough for slow CI runners; tight enough to catch a 5s WS
    connect-timeout regression like the pre-fix behaviour).
    """
    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.argv=['feral','--version']; "
        "from cli.main import main; raise SystemExit(main() or 0)",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"non-zero exit: rc={proc.returncode}, out={combined!r}"
    assert "feral-ai" in combined
    assert "Cannot connect to FERAL Brain" not in combined
    assert elapsed_ms < 2000.0, (
        f"feral --version took {elapsed_ms:.0f}ms — exceeds 2000ms ceiling. "
        "A regression here usually means we re-introduced an eager network call."
    )


_HELP_RUNNER = (
    "import sys\n"
    "sys.argv = ['feral', '--help']\n"
    "from cli.main import main\n"
    "try:\n"
    "    main()\n"
    "except SystemExit:\n"
    "    pass\n"
)


def _capture_help() -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _HELP_RUNNER],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def test_pure_local_lists_cover_every_registered_subcommand():
    """Every subcommand registered in argparse MUST appear in either
    ``PURE_LOCAL_SUBCOMMANDS`` or ``NEEDS_BRAIN_SUBCOMMANDS``.

    This is the one-shot guard against a future agent registering a
    new subcommand without classifying it. Phantom-commands tests
    cover docs↔CLI parity; this covers CLI↔classification parity.
    """
    from cli import main as cli_main

    help_text = _capture_help()
    # argparse renders subcommands as ``{cmd1,cmd2,...}`` on the
    # ``positional arguments`` block. The block can wrap onto multiple
    # lines for long lists, so we use re.DOTALL.
    import re
    match = re.search(r"\{([^}]+)\}", help_text, flags=re.DOTALL)
    assert match, f"could not find subcommand list in --help output: {help_text!r}"
    raw = match.group(1).replace("\n", "").replace(" ", "")
    registered = {x for x in raw.split(",") if x}

    classified = cli_main.PURE_LOCAL_SUBCOMMANDS | cli_main.NEEDS_BRAIN_SUBCOMMANDS
    missing = registered - classified
    assert not missing, (
        f"subcommands registered but not classified in "
        f"PURE_LOCAL_SUBCOMMANDS / NEEDS_BRAIN_SUBCOMMANDS: {missing!r}. "
        "Add each to the right set in cli/main.py."
    )


def test_version_from_help_is_listed():
    """``feral --help`` MUST advertise ``--version`` (so users can
    discover it without prior knowledge)."""
    out = _capture_help()
    assert "--version" in out, f"--version not in help output: {out!r}"
