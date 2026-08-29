"""`feral doctor` reports cua-driver, and never in yellow.

cua-driver is an OPTIONAL MCP server. Two things it needs in order to
work are invisible until a tool call fails halfway through a turn: a
daemon that is not running, and a macOS TCC grant that was never given.
Doctor is the place those become visible.

The severity contract is the hard part. Per the v2026.5.36 doctor-honesty
rules (see ``tests/test_doctor_severity.py``), an opt-in feature the
operator has not enabled is ``_info`` - never ``_warn``, never ``_fail``.
That has to hold on BOTH kinds of machine this suite runs on: a CI box
with no cua-driver, and the developer laptop where it is installed and
its daemon is live. So every branch of the block is asserted here, with
the probe stubbed, rather than left to whichever machine runs the suite.
"""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
from unittest.mock import patch

import pytest

import cli.main as m


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _doctor_cua_lines(probe_result: dict, *, system: str = "Darwin") -> list[str]:
    """Run doctor with the cua-driver probe stubbed and return its rows.

    The Console is replaced with a wide, colourless one for the same
    reason ``tests/test_doctor_severity.py`` does it: at the default
    80 columns Rich wraps a detail string across lines, and a row
    assertion would then be testing the terminal width rather than the
    text doctor chose.
    """
    from rich.console import Console as _RichConsole

    buf = io.StringIO()
    wide = _RichConsole(file=buf, force_terminal=False, color_system=None, width=400)
    with patch.object(m, "_cua_driver_probe", return_value=probe_result), \
            patch.object(m.platform, "system", return_value=system), \
            patch("rich.console.Console", lambda *a, **kw: wide):
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                m.cmd_doctor()
            except SystemExit:
                pass
    plain = ANSI_RE.sub("", buf.getvalue())
    return [ln.strip() for ln in plain.splitlines() if "cua-driver" in ln]


def _probe(**over) -> dict:
    base = {
        "binary": None,
        "version": "",
        "daemon": "unknown",
        "permissions": {"accessibility": None, "screen_recording": None},
        "detail": "",
    }
    base.update(over)
    return base


# ── 1. The rows exist at all ─────────────────────────────────────────


def test_doctor_reports_cua_driver_when_it_is_absent():
    lines = _doctor_cua_lines(_probe())
    assert lines, "doctor said nothing about cua-driver"
    assert any("not installed" in ln for ln in lines)


def test_doctor_reports_the_binary_and_version_when_present():
    lines = _doctor_cua_lines(_probe(
        binary="/opt/bin/cua-driver", version="0.22.2", daemon="running",
        permissions={"accessibility": True, "screen_recording": True},
    ))
    assert any("/opt/bin/cua-driver" in ln for ln in lines)
    assert any("0.22.2" in ln for ln in lines)


def test_doctor_reports_daemon_state():
    running = _doctor_cua_lines(_probe(
        binary="/opt/bin/cua-driver", daemon="running",
        permissions={"accessibility": True, "screen_recording": True},
    ))
    assert any("daemon" in ln and "✔" in ln for ln in running)

    stopped = _doctor_cua_lines(_probe(
        binary="/opt/bin/cua-driver", daemon="stopped",
        permissions={"accessibility": True, "screen_recording": True},
    ))
    assert any("daemon" in ln and "not running" in ln for ln in stopped)


def test_doctor_reports_permission_state():
    denied = _doctor_cua_lines(_probe(
        binary="/opt/bin/cua-driver", daemon="running",
        permissions={"accessibility": False, "screen_recording": True},
    ))
    perm = [ln for ln in denied if "permissions" in ln]
    assert perm, denied
    assert "accessibility" in perm[0]
    # The remedy has to name the command that actually prompts.
    assert "permissions grant" in perm[0]


def test_permission_row_is_skipped_off_macos():
    """TCC is a macOS concept; printing a grant row on Linux would be
    describing a permission model that host does not have."""
    lines = _doctor_cua_lines(
        _probe(binary="/opt/bin/cua-driver", daemon="running"),
        system="Linux",
    )
    assert not any("permissions" in ln for ln in lines)


# ── 2. Severity: every branch stays out of yellow and red ────────────


@pytest.mark.parametrize("result", [
    # Not installed: the state of every operator who has never heard of it.
    _probe(),
    # Installed, daemon idle.
    _probe(binary="/opt/bin/cua-driver", version="0.22.2", daemon="stopped"),
    # Installed, daemon live, everything granted.
    _probe(binary="/opt/bin/cua-driver", version="0.22.2", daemon="running",
           permissions={"accessibility": True, "screen_recording": True}),
    # Installed, daemon live, grants refused.
    _probe(binary="/opt/bin/cua-driver", version="0.22.2", daemon="running",
           permissions={"accessibility": False, "screen_recording": False}),
    # Probe could not answer (wedged binary).
    _probe(binary="/opt/bin/cua-driver", daemon="unknown",
           detail="`cua-driver status` did not answer within 2.5s"),
])
def test_no_cua_row_is_ever_a_warning_or_a_failure(result):
    lines = _doctor_cua_lines(result)
    assert lines
    offenders = [ln for ln in lines if "⚠" in ln or "✘" in ln]
    assert not offenders, (
        f"cua-driver is opt-in; these rows must be ℹ or ✔: {offenders}"
    )


def test_missing_cua_driver_is_specifically_an_info_row():
    lines = _doctor_cua_lines(_probe())
    assert any("ℹ" in ln for ln in lines), lines


# ── 3. The probe itself ──────────────────────────────────────────────


def test_probe_returns_the_not_installed_shape_when_binary_is_absent():
    with patch.object(m.shutil, "which", return_value=None):
        out = m._cua_driver_probe()
    assert out["binary"] is None
    assert out["daemon"] == "unknown"
    assert out["permissions"] == {"accessibility": None, "screen_recording": None}


def test_probe_parses_a_live_daemon_and_grants():
    """Shapes taken verbatim from cua-driver 0.22.2 on macOS."""
    perms_json = (
        '{"accessibility": true, "screen_recording": true, '
        '"source": {"attribution": "driver-daemon", '
        '"bundle_id": "com.trycua.driver"}}'
    )

    def _fake_run(cmd, **kw):
        args = cmd[1:]
        if args == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, "cua-driver 0.22.2\n", "")
        if args == ["status"]:
            return subprocess.CompletedProcess(cmd, 0, "Cua Driver daemon is running\n", "")
        if args == ["permissions", "status", "--json"]:
            return subprocess.CompletedProcess(cmd, 0, perms_json, "")
        raise AssertionError(f"probe ran an unexpected subcommand: {args}")

    with patch("subprocess.run", side_effect=_fake_run):
        out = m._cua_driver_probe(binary="/opt/bin/cua-driver")

    assert out["version"] == "0.22.2"
    assert out["daemon"] == "running"
    assert out["permissions"] == {"accessibility": True, "screen_recording": True}


def test_probe_reads_rc1_as_a_stopped_daemon_not_an_error():
    """`cua-driver status` exits 1 when the socket does not answer.
    Verified against the real binary with a bogus --socket."""
    def _fake_run(cmd, **kw):
        args = cmd[1:]
        if args == ["status"]:
            return subprocess.CompletedProcess(
                cmd, 1, "Cua Driver daemon is not running\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=_fake_run):
        out = m._cua_driver_probe(binary="/opt/bin/cua-driver")

    assert out["daemon"] == "stopped"
    assert out["detail"] == ""


def test_probe_only_ever_runs_read_only_subcommands():
    """`permissions grant` opens system dialogs and moves focus.
    A diagnostic command must never trigger it, and neither may any of
    the actuating tools."""
    seen: list[list[str]] = []

    def _record(cmd, **kw):
        seen.append(cmd[1:])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=_record):
        m._cua_driver_probe(binary="/opt/bin/cua-driver")

    allowed = {("--version",), ("status",), ("permissions", "status", "--json")}
    assert {tuple(a) for a in seen} <= allowed, seen


def test_probe_survives_a_wedged_binary():
    """A hung cua-driver must not hang `feral doctor`, and must not take
    the other 60 rows down with it."""
    def _hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 2.5))

    with patch("subprocess.run", side_effect=_hang):
        out = m._cua_driver_probe(binary="/opt/bin/cua-driver")

    assert out["binary"] == "/opt/bin/cua-driver"
    assert out["daemon"] == "unknown"
    assert "did not answer" in out["detail"]


def test_probe_survives_garbage_on_stdout():
    def _garbage(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "not json at all", "")

    with patch("subprocess.run", side_effect=_garbage):
        out = m._cua_driver_probe(binary="/opt/bin/cua-driver")

    assert out["permissions"] == {"accessibility": None, "screen_recording": None}


def test_probe_never_raises_on_a_broken_exec():
    with patch("subprocess.run", side_effect=OSError("Exec format error")):
        out = m._cua_driver_probe(binary="/opt/bin/cua-driver")
    assert out["daemon"] == "unknown"
