"""The interpreter's two SQLite build features must be probed, reported
and enforced separately.

Background, measured on macOS arm64:

    pyenv 3.11.11            (SQLite 3.51.0)  fts5 YES  loadable NO
    python-build-standalone
      3.11.13                (SQLite 3.49.1)  fts5 NO   loadable YES
      3.11.15                (SQLite 3.53.1)  fts5 YES  loadable YES

Two independent flags, so a host can have either one without the other,
and the two have opposite consequences:

* **No FTS5**: the brain does not start. `MemoryStore._init_db` used to
  reach `CREATE VIRTUAL TABLE ... USING fts5` unguarded and die with
  `sqlite3.OperationalError: no such module: fts5`, a traceback pointing
  at a triple-quoted SQL string that never names the interpreter. It also
  fired after `notes` and its triggers had been committed, leaving a
  database whose triggers referenced a table that did not exist.
* **No loadable extensions**: nothing breaks. sqlite-vec cannot load and
  the vector leg runs over numpy, which F-17 measured as the *faster*
  path. This is an INFO, and prescribing an interpreter rebuild for it is
  a ~10x slowdown (pinned by test_doctor_vector_backend_truth.py).

`feral doctor` reported neither directly: it printed a green
"Python version" row and only exposed the loadable-extension state
indirectly, inside "Memory vector backend".

The no-FTS5 interpreter cannot be conjured inside a test run, so these
tests pin the probe seam (`memory.sqlite_features`) in both directions
rather than requiring a second interpreter on the machine. The real
3.11.13 reproduction is recorded in `memory/sqlite_features.py`.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3

import pytest

from memory import sqlite_features


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The probes memoise, and these tests flip the answer."""
    sqlite_features._reset_probe_cache_for_tests()
    yield
    sqlite_features._reset_probe_cache_for_tests()


# ── The probes themselves ─────────────────────────────────────────────


def test_this_interpreter_reports_both_features_as_booleans():
    """Whatever the host is, the report must be answerable and separate.

    Deliberately asserts the shape and not the values: CI, a contributor
    on pyenv and the pinned .venv legitimately differ on
    `loadable_extensions`.
    """
    report = sqlite_features.interpreter_sqlite_report()

    assert isinstance(report["fts5"], bool)
    assert isinstance(report["loadable_extensions"], bool)
    assert report["sqlite_version"] == sqlite3.sqlite_version
    # The report exists to tell an operator which interpreter is at
    # fault, so it has to name it.
    assert report["executable"]
    assert report["python_version"].startswith("3.")


def test_fts5_probe_leaves_no_table_behind_and_touches_no_file(tmp_path):
    """The probe runs at every MemoryStore construction. It must not be
    capable of writing to, locking or half-migrating the real store."""
    before = set(tmp_path.iterdir())
    assert sqlite_features.fts5_available() in (True, False)
    assert set(tmp_path.iterdir()) == before


def test_fts5_probe_reports_false_when_sqlite_rejects_the_virtual_table(monkeypatch):
    """Simulates python-build-standalone 3.11.13, whose SQLite 3.49.1
    raises `no such module: fts5` from the CREATE."""
    # A stand-in rather than a wrapped real connection: `execute` on a
    # real sqlite3.Connection is a read-only C slot and cannot be
    # monkeypatched ("attribute 'execute' is read-only").
    class _NoFts5Conn:
        def execute(self, sql, *a, **kw):
            if "fts5" in sql.lower():
                raise sqlite3.OperationalError("no such module: fts5")
            return None

        def close(self):
            pass

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: _NoFts5Conn())
    assert sqlite_features.fts5_available() is False


def test_loadable_extension_probe_reports_false_when_the_method_is_absent(monkeypatch):
    """pyenv's default macOS build does not merely fail the call, it omits
    `enable_load_extension` from the Connection object entirely, so a
    try/except around the call is not sufficient on its own."""

    class _NoExtConn:
        def close(self):
            pass

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: _NoExtConn())
    assert sqlite_features.loadable_extensions_available() is False


def test_the_two_probes_are_independent(monkeypatch):
    """The bug this whole module exists for: checking one and inferring
    the other. 3.11.13 has extensions and no FTS5; pyenv 3.11.11 has FTS5
    and no extensions. Neither implies the other."""
    monkeypatch.setattr(sqlite_features, "_FTS5_AVAILABLE", False)
    monkeypatch.setattr(sqlite_features, "_LOADABLE_EXTENSIONS_AVAILABLE", True)
    assert sqlite_features.fts5_available() is False
    assert sqlite_features.loadable_extensions_available() is True


# ── The boot guard ────────────────────────────────────────────────────


def test_require_fts5_is_a_noop_when_the_feature_is_present(monkeypatch):
    monkeypatch.setattr(sqlite_features, "_FTS5_AVAILABLE", True)
    sqlite_features.require_fts5("anything")  # must not raise


def test_require_fts5_raises_a_diagnosable_error(monkeypatch):
    """`sqlite3.OperationalError: no such module: fts5` names neither the
    cause nor a remedy. The replacement must name the interpreter, the
    SQLite version, what breaks, and how to fix it."""
    monkeypatch.setattr(sqlite_features, "_FTS5_AVAILABLE", False)

    with pytest.raises(sqlite_features.SQLiteFeatureError) as excinfo:
        sqlite_features.require_fts5("FERAL's memory store")

    msg = str(excinfo.value)
    assert "FERAL's memory store" in msg
    assert "FTS5" in msg
    assert sqlite3.sqlite_version in msg
    assert "Fix:" in msg
    # The remedy must be the interpreter, not a pip install: no wheel can
    # add FTS5 to the SQLite CPython is linked against.
    assert "3.11.15" in msg
    assert "pip install" not in msg


def test_memory_store_refuses_to_boot_without_fts5(monkeypatch, tmp_path):
    """End to end: the guard is actually wired into the constructor, and
    it fires before any DDL runs so no half-created file is left."""
    from memory import store as store_mod

    monkeypatch.setattr(sqlite_features, "_FTS5_AVAILABLE", False)
    db_path = tmp_path / "memory.db"

    with pytest.raises(sqlite_features.SQLiteFeatureError):
        store_mod.MemoryStore(db_path=str(db_path))

    assert not db_path.exists(), (
        "boot aborted but left a database behind; the triggers created "
        "before the FTS table would reference a missing table"
    )


def test_knowledge_graph_refuses_to_boot_without_fts5(monkeypatch, tmp_path):
    """The sibling site. KnowledgeGraph is constructed standalone by
    `feral memory` subcommands and by tests, so it can be the first FTS5
    statement a process runs and needs its own guard."""
    from memory import knowledge_graph as kg_mod
    from memory.embeddings import EmbeddingProvider

    monkeypatch.setattr(sqlite_features, "_FTS5_AVAILABLE", False)
    db_path = tmp_path / "kg.db"

    with pytest.raises(sqlite_features.SQLiteFeatureError):
        kg_mod.KnowledgeGraph(db_path=str(db_path), embedder=EmbeddingProvider())

    assert not db_path.exists()


def test_every_fts5_creation_site_is_behind_the_guard():
    """Structural. Five `USING fts5` sites exist across two modules; a
    sixth added to a third module would reintroduce the crash. Any module
    that creates an FTS5 table must import the guard."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] in {"build", "dist", "tests", ".venv"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "USING fts5" not in text:
            continue
        if "require_fts5" not in text:
            offenders.append(str(rel))

    assert not offenders, (
        f"these modules create FTS5 tables without calling require_fts5, so on "
        f"an interpreter without FTS5 they raise a bare "
        f"`sqlite3.OperationalError: no such module: fts5`: {offenders}"
    )


# ── `feral doctor` must report both, separately ───────────────────────


@pytest.fixture
def run_doctor(monkeypatch, tmp_path):
    """Drive the real ``cmd_doctor`` with both features pinned."""
    # Bound ONCE, before the first monkeypatch of `rich.console.Console`.
    # Importing it inside `_run` makes the second invocation pick up the
    # first invocation's patch, so `_RichConsole(file=buf2)` returns the
    # console still bound to buf1 and the second call reports an empty
    # transcript. Tests here deliberately call `_run` twice.
    from rich.console import Console as _RichConsole

    def _run(*, fts5: bool, loadable: bool) -> str:
        home = tmp_path / "doctor-home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("FERAL_HOME", str(home))
        (home / "settings.json").write_text(json.dumps({"memory": {"backend": "sqlite_vec"}}))
        (home / "USER.md").write_text("Test operator.\n")

        monkeypatch.setattr(sqlite_features, "fts5_available", lambda: fts5)
        monkeypatch.setattr(
            sqlite_features, "loadable_extensions_available", lambda: loadable
        )

        buf = io.StringIO()
        console = _RichConsole(file=buf, force_terminal=False, color_system=None, width=400)
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


def test_doctor_reports_fts5_and_loadable_extensions_as_two_rows(run_doctor):
    """The defect: doctor checked one and not the other, so an operator on
    an interpreter that cannot boot the brain saw an all-green report."""
    out = run_doctor(fts5=True, loadable=True)

    fts_row = _row(out, "SQLite FTS5")
    ext_row = _row(out, "SQLite loadable extensions")

    assert fts_row.startswith("✔"), fts_row
    assert ext_row.startswith("✔"), ext_row
    assert fts_row != ext_row


def test_doctor_fails_and_names_the_consequence_when_fts5_is_missing(run_doctor):
    out = run_doctor(fts5=False, loadable=True)
    row = _row(out, "SQLite FTS5")

    assert row.startswith("✘"), row
    # Must say what it breaks, not just that a feature is absent.
    assert "cannot start" in row
    assert sqlite3.sqlite_version in row

    fixes = out.split("Suggested fixes:", 1)[1] if "Suggested fixes:" in out else ""
    assert "3.11.15" in fixes, (
        "doctor reported the missing feature but offered no way out of it"
    )


def test_doctor_does_not_fail_when_only_loadable_extensions_are_missing(run_doctor):
    """pyenv 3.11.11, the common contributor host. The brain runs fine
    here, so this must not be red, and per F-17 must not be yellow."""
    out = run_doctor(fts5=True, loadable=False)
    row = _row(out, "SQLite loadable extensions")

    assert row.startswith("ℹ"), row
    assert not row.startswith("⚠"), row
    assert "numpy" in row


def test_doctor_never_offers_the_interpreter_rebuild_as_a_suggested_fix(run_doctor):
    """Same rule test_doctor_vector_backend_truth.py pins for the vector
    row, restated for the new row so the two cannot drift: the rebuild is
    reachable in the detail line, never in "Suggested fixes:"."""
    out = run_doctor(fts5=True, loadable=False)

    fixes = out.split("Suggested fixes:", 1)[1] if "Suggested fixes:" in out else ""
    assert "enable-loadable-sqlite-extensions" not in fixes, fixes
    assert "enable-loadable-sqlite-extensions" in _row(out, "SQLite loadable extensions")


def test_doctor_distinguishes_the_two_remedies(run_doctor):
    """The rows exist to be actionable. FTS5 is fixed by changing
    interpreter; loadable extensions by rebuilding one. Neither is fixed
    by pip, and saying so is the whole point."""
    missing_fts = _row(run_doctor(fts5=False, loadable=True), "SQLite FTS5")
    assert "pip" not in missing_fts.lower()

    missing_ext = _row(run_doctor(fts5=True, loadable=False), "SQLite loadable extensions")
    assert "pip" not in missing_ext.lower()
    assert "PYTHON_CONFIGURE_OPTS" in missing_ext
