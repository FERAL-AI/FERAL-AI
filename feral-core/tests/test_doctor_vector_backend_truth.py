"""`feral doctor` must not green-check a vector backend that cannot run.

The probe used to read ``settings.memory.backend`` and, seeing the
built-in default, print

    ✔  Memory vector backend  sqlite_vec (built-in default)

Selecting sqlite_vec is not the same as running it. sqlite-vec is a
loadable SQLite EXTENSION, and an interpreter built without
``enable_load_extension`` (pyenv's default on macOS) can never load it:
``SQLiteVecIndex.indexed`` stays False and every vector query is served
by a numpy brute-force scan instead. On such a host the checkmark named
a backend that was not running and hid an O(n)-per-query scan behind it.

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
    assert row.startswith("⚠"), row
    # It has to name what is ACTUALLY running, not just withhold the tick.
    assert "numpy_fallback" in row
    assert "cannot load" in row


def test_doctor_offers_the_interpreter_fix_not_a_backend_switch(doctor_env, run_doctor):
    """The remediation for this state is rebuilding/replacing Python.
    Telling the operator to switch backends or restart would not help."""
    out = run_doctor(sqlite_vec_loads=False)

    assert "Suggested fixes:" in out
    fixes = out.split("Suggested fixes:", 1)[1]
    assert "enable-loadable-sqlite-extensions" in fixes


def test_doctor_still_passes_when_the_extension_loads(doctor_env, run_doctor):
    """No over-correction: a host that CAN load sqlite-vec keeps its
    green row and emits no suggested fix for this probe."""
    out = run_doctor(sqlite_vec_loads=True)
    row = _row(out, "Memory vector backend")

    assert row.startswith("✔"), row
    assert "vec0 index active" in row
    assert "numpy_fallback" not in row
