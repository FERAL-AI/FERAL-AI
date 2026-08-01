"""Contract for checkpoint capture and revert (skills/checkpoints.py).

The properties worth pinning: content addressing works with or without a
repository, no git command is ever run, drift is refused by default, and
every response says out loud that bash writes are not covered.
"""

from __future__ import annotations

import sqlite3
import subprocess

import pytest

from skills.checkpoints import BASH_NOT_COVERED_NOTE, CheckpointStore, checkpoint_root


@pytest.fixture
def store(tmp_path) -> CheckpointStore:
    return CheckpointStore(tmp_path / "checkpoints")


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


def _write(store: CheckpointStore, path, content: str, *, turn_id: str, session_id: str = "s1"):
    cp = store.capture(path, turn_id=turn_id, session_id=session_id,
                       tool_name="coding_tools__write_file")
    path.write_text(content)
    store.record_after(cp, path)
    return cp


# ── capture / restore ─────────────────────────────────────────────────


def test_revert_restores_modified_content(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    assert f.read_text() == "agent edit\n"

    result = store.revert_turn("t1")
    assert result["success"] is True
    assert result["reverted_count"] == 1
    assert f.read_text() == "original\n"


def test_revert_deletes_a_file_the_turn_created(store, work):
    f = work / "new.txt"
    _write(store, f, "brand new\n", turn_id="t1")
    assert f.exists()

    store.revert_turn("t1")
    assert not f.exists()


def test_revert_uses_the_earliest_pre_turn_state(store, work):
    """Two edits to the same file in one turn revert to the state before
    the first, not the state between them."""
    f = work / "a.txt"
    f.write_text("v0\n")
    _write(store, f, "v1\n", turn_id="t1")
    _write(store, f, "v2\n", turn_id="t1")
    store.revert_turn("t1")
    assert f.read_text() == "v0\n"


def test_turns_are_independent(store, work):
    f = work / "a.txt"
    f.write_text("v0\n")
    _write(store, f, "v1\n", turn_id="t1")
    _write(store, f, "v2\n", turn_id="t2")
    store.revert_turn("t2")
    assert f.read_text() == "v1\n"


# ── refuse on drift (the important one) ───────────────────────────────


def test_drift_is_refused_by_default(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    f.write_text("the user edited this afterwards\n")

    result = store.revert_turn("t1")
    assert result["success"] is False
    assert result["error"]
    assert [e["status"] for e in result["files"]] == ["drifted"]
    # The whole point: nothing was clobbered.
    assert f.read_text() == "the user edited this afterwards\n"


def test_force_overrides_drift(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    f.write_text("user work\n")

    result = store.revert_turn("t1", force=True)
    assert result["success"] is True
    assert result["forced"] is True
    assert f.read_text() == "original\n"


def test_dry_run_touches_nothing(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")

    result = store.revert_turn("t1", dry_run=True)
    assert result["dry_run"] is True
    assert result["reverted_count"] == 0
    assert f.read_text() == "agent edit\n"


def test_already_reverted_files_are_skipped(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    store.revert_turn("t1")

    again = store.revert_turn("t1")
    assert [e["status"] for e in again["files"]] == ["already_reverted"]
    assert again["reverted_count"] == 0


# ── bash is never claimed ─────────────────────────────────────────────


@pytest.mark.parametrize("call", [
    lambda s: s.revert_turn("t1"),
    lambda s: s.revert_turn("t1", dry_run=True),
    lambda s: s.plan_revert("t1"),
    lambda s: s.revert_turn("nonexistent-turn"),
])
def test_every_revert_response_says_bash_is_not_covered(store, work, call):
    """A partial revert that reads as complete is worse than none."""
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    result = call(store)
    assert result["bash_not_covered"] is True
    assert result["note"] == BASH_NOT_COVERED_NOTE
    assert "bash" in result["note"]


# ── no git, no repo needed ────────────────────────────────────────────


def test_works_outside_any_git_repository(store, work, monkeypatch):
    """FERAL writes routinely land outside a repo. Content addressing
    behaves identically either way, so there is no second code path."""
    assert not (work / ".git").exists()

    def _explode(*a, **kw):  # pragma: no cover - only runs on regression
        raise AssertionError("checkpoints must never invoke git")

    monkeypatch.setattr(subprocess, "run", _explode)
    monkeypatch.setattr(subprocess, "Popen", _explode)
    monkeypatch.setattr(subprocess, "check_output", _explode)

    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    assert store.revert_turn("t1")["success"] is True
    assert f.read_text() == "original\n"


def test_module_never_imports_a_subprocess_facility():
    """Structural, not textual: the module's own docstring discusses git
    at length (explaining why it is not used), so grepping the source
    would only test the prose. Checking the import graph tests the code."""
    import ast
    import inspect

    import skills.checkpoints as mod

    tree = ast.parse(inspect.getsource(mod))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"subprocess", "shutil", "pty", "commands"}, (
        f"checkpoints must not be able to run a git command: {sorted(imported)}"
    )


# ── listing / CLI-facing reads ────────────────────────────────────────


def test_list_turns_and_latest(store, work):
    f = work / "a.txt"
    f.write_text("v0\n")
    _write(store, f, "v1\n", turn_id="t1")
    _write(store, f, "v2\n", turn_id="t2")

    turns = store.list_turns()
    assert [t["turn_id"] for t in turns] == ["t2", "t1"]
    assert store.latest_turn() == "t2"
    assert store.latest_turn("nobody") is None


def test_cli_can_read_the_sqlite_directly(store, work):
    """The CLI must not depend on the brain's REST surface, because the
    moment you need an undo is the moment the brain is wedged."""
    f = work / "a.txt"
    f.write_text("v0\n")
    _write(store, f, "v1\n", turn_id="t1")

    conn = sqlite3.connect(str(store.db_path))
    try:
        rows = conn.execute(
            "SELECT turn_id, path FROM checkpoints"
        ).fetchall()
    finally:
        conn.close()
    assert rows and rows[0][0] == "t1"


def test_checkpoint_root_honours_feral_home(tmp_path, monkeypatch):
    """Safety: never touch a real ~/.feral during tests."""
    monkeypatch.delenv("FERAL_CHECKPOINT_DIR", raising=False)
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "isolated"))
    assert checkpoint_root() == tmp_path / "isolated" / "checkpoints"


def test_unrecoverable_when_the_blob_was_never_stored(store, work, monkeypatch):
    monkeypatch.setenv("FERAL_CHECKPOINT_MAX_BLOB_BYTES", "1")
    f = work / "a.txt"
    f.write_text("a much longer body than one byte\n")
    _write(store, f, "agent edit\n", turn_id="t1")

    result = store.revert_turn("t1")
    assert [e["status"] for e in result["files"]] == ["unrecoverable"]
    assert f.read_text() == "agent edit\n"
