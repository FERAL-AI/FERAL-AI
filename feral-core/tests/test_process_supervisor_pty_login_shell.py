"""PTY adapter spawns the child under a real login shell.

Contract verified: "command runs under an interactive login shell". Spec:

* spawn ``tty`` via the PTY adapter; assert exit 0 + a ``/dev/`` path
  on stdout (proves the child has a real controlling terminal).
* spawn ``echo $0 -- $-`` via the PTY adapter; assert ``-l`` or a
  login-shell indicator in output (proves the shell received ``-l``).
* skip the whole module on Windows (PTY adapter is POSIX-only by
  design — see adapters/pty.py).
"""

from __future__ import annotations

import sys

import pytest

from process.supervisor import create_process_supervisor


pytestmark = [
    pytest.mark.no_auto_feral_home,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="PTY adapter is POSIX-only (raises NotImplementedError on Windows)",
    ),
]


async def test_pty_adapter_child_sees_real_tty() -> None:
    """``tty`` only exits 0 when stdin is a real terminal device."""
    supervisor = create_process_supervisor()
    handle = await supervisor.run(
        "tty",
        adapter="pty",
        overall_timeout_sec=5.0,
    )
    record = await handle.wait()

    assert record.exit_code == 0, (
        f"tty exited {record.exit_code}; stdout={handle.stdout!r}"
    )
    assert "/dev/" in handle.stdout, (
        f"expected /dev/... TTY path in stdout; got {handle.stdout!r}"
    )


async def test_pty_adapter_uses_login_shell() -> None:
    """``-l`` flag must be present (and/or visible to the child shell).

    A POSIX shell launched as ``shell -l -c CMD`` exposes the login
    bit two ways:

    * the ``l`` flag appears in ``$-`` (both bash and zsh include it
      when invoked with ``-l``)
    * ``$0`` may carry a leading ``-`` (the canonical Unix login-shell
      indicator)

    Either is acceptable per the spec wording (``"-l" or
    login-shell indicator in output``).
    """
    supervisor = create_process_supervisor()
    handle = await supervisor.run(
        "echo $0 -- $-",
        adapter="pty",
        overall_timeout_sec=5.0,
    )
    record = await handle.wait()

    assert record.exit_code == 0, (
        f"echo exited {record.exit_code}; stdout={handle.stdout!r}"
    )

    out = handle.stdout
    # Split on the ``--`` separator we asked echo to print so we can
    # inspect ``$0`` and ``$-`` independently.
    assert "--" in out, f"unexpected output shape: {out!r}"
    dollar_zero, _, dollar_dash = out.partition("--")
    dollar_zero = dollar_zero.strip()
    dollar_dash = dollar_dash.strip()

    login_indicators = (
        "l" in dollar_dash,            # zsh + bash both set 'l' in $- under -l
        dollar_zero.startswith("-"),   # canonical -bash / -zsh argv[0] form
        "-l" in out,                   # literal flag (covers shells that echo it)
    )
    assert any(login_indicators), (
        f"no login-shell indicator in output: $0={dollar_zero!r} "
        f"$-={dollar_dash!r}"
    )


async def test_pty_adapter_rejects_windows() -> None:
    """On Windows the adapter raises NotImplementedError, not a crash.

    This test runs only on POSIX (the module-level skipif keeps it off
    Windows), so we exercise the rejection by importing the adapter
    function and patching ``sys.platform`` — a "ConPTY-not-implemented"
    guard with a clear error instead of a crash.
    """
    import process.supervisor.adapters.pty as pty_mod

    original = pty_mod.sys.platform
    pty_mod.sys.platform = "win32"
    try:
        with pytest.raises(NotImplementedError):
            await pty_mod.create_pty_adapter("echo hi")
    finally:
        pty_mod.sys.platform = original
