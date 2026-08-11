"""`feral doctor` must not green-check a vector backend that cannot run.

The probe used to read ``settings.memory.backend`` and, seeing the
built-in default, print

    ✔  Memory vector backend  sqlite_vec (built-in default)

Selecting sqlite_vec is not the same as running it. sqlite-vec is a
loadable SQLite EXTENSION, and an interpreter built without
``enable_load_extension`` (pyenv's default on macOS) can never load it:
``SQLiteVecIndex.indexed`` stays False and every vector query is served
over numpy instead. On such a host the checkmark named a backend that
was not running.

It must not be a warning either. Doctor used to render this state yellow
and put "rebuild CPython with --enable-loadable-sqlite-extensions" in
"Suggested fixes:". Measured on this machine, sqlite-vec 0.1.9 builds no
ANN index, both paths are O(n), and numpy runs 0.46ms vs vec0's 7.08ms
at 12k chunks (3.97ms vs 56.99ms at 100k) for identical top-5. The
prescribed fix was a 10x slowdown, so the row is now informational and
the rebuild is offered for the reason that survives measurement:
resident memory on a large store.

These tests drive the real ``cmd_doctor`` with the extension probe
pinned in both directions.
"""
from __future__ import annotations

import io
import json
import re

import pytest

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def doctor_env(monkeypatch, tmp_path):
    """FERAL_HOME with sqlite_vec selected, plus the console capture."""
    home = tmp_path / "doctor-home"
    home.mkdir()
    monkeypatch.setenv("FERAL_HOME", str(home))
    (home / "settings.json").write_text(
        json.dumps({"memory": {"backend": "sqlite_vec"}})
    )
    (home / "USER.md").write_text("Test operator.\n")
    return home


@pytest.fixture
def run_doctor(monkeypatch):
    def _run(*, sqlite_vec_loads: bool) -> str:
        from rich.console import Console as _RichConsole

        from memory.vector_index_backends import sqlite_vec as _sqlite_vec_mod

        monkeypatch.setattr(
            _sqlite_vec_mod, "sqlite_vec_available", lambda: sqlite_vec_loads
        )

        buf = io.StringIO()
        console = _RichConsole(
            file=buf, force_terminal=False, color_system=None, width=400
        )
        monkeypatch.setattr("rich.console.Console", lambda *a, **kw: console)

        from cli.main import cmd_doctor

        try:
            cmd_doctor()
        except SystemExit:
            pass
        return ANSI_RE.sub("", buf.getvalue())

    return _run


def _row(text: str, label: str) -> str:
    for line in text.splitlines():
        body = line.strip()
        if len(body) > 2 and body[1:].strip().startswith(label):
            return body
    raise AssertionError(f"no doctor row for {label!r} in:\n{text}")


def test_doctor_does_not_greencheck_an_unloadable_sqlite_vec(doctor_env, run_doctor):
    out = run_doctor(sqlite_vec_loads=False)
    row = _row(out, "Memory vector backend")

    assert not row.startswith("✔"), (
        f"doctor passed a backend whose extension cannot load: {row}"
    )
    assert row.startswith("ℹ"), row
    # It has to name what is ACTUALLY running, not just withhold the tick.
    assert "numpy_fallback" in row
    assert "cannot load" in row


def test_doctor_does_not_prescribe_an_interpreter_rebuild_as_a_fix(
    doctor_env, run_doctor
):
    """The measured numbers say numpy is the faster path, so this state is
    not a defect and must not appear under "Suggested fixes:". Sending a
    user to rebuild CPython here buys them a ~10x slowdown."""
    out = run_doctor(sqlite_vec_loads=False)

    fixes = out.split("Suggested fixes:", 1)[1] if "Suggested fixes:" in out else ""
    assert "enable-loadable-sqlite-extensions" not in fixes, (
        "doctor is still prescribing an interpreter rebuild as a remedy"
    )


def test_doctor_still_tells_the_operator_how_to_get_sqlite_vec(doctor_env, run_doctor):
    """Not over-corrected into silence: an operator whose store is large
    enough for resident memory to matter still needs the instructions, and
    the reason has to be memory rather than speed."""
    row = _row(run_doctor(sqlite_vec_loads=False), "Memory vector backend")

    assert "enable-loadable-sqlite-extensions" in row
    assert "memory" in row.lower()


def test_doctor_does_not_call_the_numpy_path_slow_or_degraded(doctor_env, run_doctor):
    """The wording is the defect being fixed. "degraded" and a bare
    "O(n) per query" both told the user the wrong thing: vec0 is O(n)
    too, and measures slower."""
    row = _row(run_doctor(sqlite_vec_loads=False), "Memory vector backend")

    assert "degraded" not in row.lower(), row
    assert "O(n) per query" not in row, row


def test_doctor_still_passes_when_the_extension_loads(doctor_env, run_doctor):
    """No over-correction: a host that CAN load sqlite-vec keeps its
    green row and emits no suggested fix for this probe."""
    out = run_doctor(sqlite_vec_loads=True)
    row = _row(out, "Memory vector backend")

    assert row.startswith("✔"), row
    assert "vec0 index active" in row
    assert "numpy_fallback" not in row
