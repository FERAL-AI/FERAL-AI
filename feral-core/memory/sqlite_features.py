"""Interpreter-level SQLite capability probes.

FERAL needs two SQLite build-time features, and they are independent.
Stock interpreters routinely ship one without the other, so they have to
be probed and reported separately.

**FTS5** (``--enable-fts5`` in the SQLite amalgamation the interpreter was
linked against). Required, not optional. ``MemoryStore._init_db`` and
``KnowledgeGraph`` create five ``CREATE VIRTUAL TABLE ... USING fts5``
tables at construction time. Without FTS5 the store does not degrade, it
fails to boot. Measured on python-build-standalone 3.11.13
(SQLite 3.49.1), which is the build an earlier desktop-bundling spike
selected::

    Traceback (most recent call last):
      File "<string>", line 6, in <module>
      File ".../memory/store.py", line 443, in __init__
        self._init_db()
      File ".../memory/store.py", line 890, in _init_db
        conn.execute(\"\"\"
    sqlite3.OperationalError: no such module: fts5

That traceback names ``conn.execute`` and a triple-quoted string. It does
not say "your interpreter cannot run FERAL", which is the actual cause,
so ``require_fts5`` converts it into an error that does.

**Loadable extensions** (``--enable-loadable-sqlite-extensions`` when
CPython itself is configured). Optional. It gates ``sqlite-vec``. pyenv's
default macOS build omits it, so ``sqlite3.Connection`` has no
``.enable_load_extension`` attribute at all; ``pip install sqlite-vec``
and ``import sqlite_vec`` both still succeed, which is why this one is
invisible rather than loud. The consequence is a numpy vector scan
instead of a vec0 virtual table. Per the F-17 measurements that scan is
*faster* (0.46ms vs 7.08ms for top-5 over 12k 384-dim vectors), so the
absence of this feature is an INFO, never a failure, and the only honest
reason to want it is resident memory on a large store.

Measured combinations on macOS arm64::

    pyenv 3.11.11            (SQLite 3.51.0)  fts5 YES  loadable NO
    python-build-standalone
      3.11.13                (SQLite 3.49.1)  fts5 NO   loadable YES
      3.11.15                (SQLite 3.53.1)  fts5 YES  loadable YES  <- pinned

Probes are cached: they run at boot on every MemoryStore construction and
in ``feral doctor``, and the answer cannot change inside a process.
"""

from __future__ import annotations

import sqlite3
import sys

__all__ = [
    "SQLiteFeatureError",
    "fts5_available",
    "loadable_extensions_available",
    "interpreter_sqlite_report",
    "require_fts5",
    "FTS5_REMEDY",
    "LOADABLE_EXTENSIONS_REMEDY",
]


# Kept as module constants so `feral doctor`, MemoryStore's boot error and
# the docs cannot drift into prescribing three different remedies for the
# same host.
FTS5_REMEDY = (
    "Use an interpreter whose SQLite has FTS5. The repo pin "
    "(see .python-pin) is CPython 3.11.15 from python-build-standalone: "
    "run `make dev` and use .venv/bin/python, or "
    "`uv python install 3.11.15 && uv venv --python 3.11.15`."
)

LOADABLE_EXTENSIONS_REMEDY = (
    'rebuild with PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions", '
    "or use the repo-pinned python-build-standalone interpreter (make dev)"
)


class SQLiteFeatureError(RuntimeError):
    """This interpreter's SQLite cannot support a feature FERAL requires.

    A dedicated type rather than a bare RuntimeError so callers that want
    to render a setup-help screen (the CLI, the desktop shell) can tell
    "wrong interpreter" apart from "database is corrupt", which is the
    other thing a boot-time sqlite3 error usually means.
    """


_FTS5_AVAILABLE: bool | None = None
_LOADABLE_EXTENSIONS_AVAILABLE: bool | None = None


def fts5_available() -> bool:
    """True when this interpreter's SQLite can create FTS5 virtual tables.

    Probes on a throwaway in-memory database so it can never touch, lock
    or half-migrate the real store. Checking the compile options via
    ``PRAGMA compile_options`` was rejected: it reports what the SQLite
    library was built with, and a Python linked against a *different*
    system SQLite at runtime can disagree with it. Creating the table is
    the only answer that is about the code path that actually runs.
    """
    global _FTS5_AVAILABLE
    if _FTS5_AVAILABLE is not None:
        return _FTS5_AVAILABLE
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _feral_fts5_probe USING fts5(x)")
        finally:
            conn.close()
        _FTS5_AVAILABLE = True
    except sqlite3.Error:
        _FTS5_AVAILABLE = False
    return _FTS5_AVAILABLE


def loadable_extensions_available() -> bool:
    """True when this interpreter can load SQLite extensions at all.

    Distinct from ``memory.vector_index_backends.sqlite_vec.sqlite_vec_available``,
    which answers "can we load *sqlite-vec*" and therefore returns False
    both when the interpreter lacks the feature and when the wheel is
    simply not installed. Doctor needs to tell those two apart: one is a
    rebuild, the other is a pip install.

    On an interpreter built without ``--enable-loadable-sqlite-extensions``
    the method is absent entirely, so ``hasattr`` is the primary check;
    the call is still made because some builds expose the method and then
    raise ``NotSupportedError`` from it.
    """
    global _LOADABLE_EXTENSIONS_AVAILABLE
    if _LOADABLE_EXTENSIONS_AVAILABLE is not None:
        return _LOADABLE_EXTENSIONS_AVAILABLE
    try:
        conn = sqlite3.connect(":memory:")
        try:
            if not hasattr(conn, "enable_load_extension"):
                _LOADABLE_EXTENSIONS_AVAILABLE = False
                return False
            conn.enable_load_extension(True)
            conn.enable_load_extension(False)
        finally:
            conn.close()
        _LOADABLE_EXTENSIONS_AVAILABLE = True
    except (AttributeError, sqlite3.Error):
        _LOADABLE_EXTENSIONS_AVAILABLE = False
    return _LOADABLE_EXTENSIONS_AVAILABLE


def interpreter_sqlite_report() -> dict[str, object]:
    """One dict with everything a diagnostic needs to print.

    Single source so `feral doctor`, `make dev`'s probe and any future
    support-bundle dump report the same fields with the same names.
    """
    return {
        "executable": sys.executable,
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "sqlite_version": sqlite3.sqlite_version,
        "fts5": fts5_available(),
        "loadable_extensions": loadable_extensions_available(),
    }


def require_fts5(component: str) -> None:
    """Raise a diagnosable error instead of letting sqlite raise a cryptic one.

    Called before the first ``CREATE VIRTUAL TABLE ... USING fts5``, not
    after, so the store never leaves a half-created schema behind: the
    plain tables and their triggers are created in the same DDL run, and a
    failure partway through leaves a database whose triggers reference an
    FTS table that does not exist.
    """
    if fts5_available():
        return
    raise SQLiteFeatureError(
        f"{component} requires SQLite FTS5, and this interpreter does not have it.\n"
        f"  interpreter : {sys.executable} (Python {'%d.%d.%d' % sys.version_info[:3]})\n"
        f"  sqlite      : {sqlite3.sqlite_version} (built without FTS5)\n"
        f"FERAL's memory store creates FTS5 virtual tables for notes, episodes, "
        f"knowledge, wiki pages and entities, so it cannot start on this interpreter.\n"
        f"Fix: {FTS5_REMEDY}"
    )


def _reset_probe_cache_for_tests() -> None:
    """Clear the memoised answers.

    Only for tests that monkeypatch ``sqlite3`` to simulate an interpreter
    they cannot otherwise obtain. Production code must never call this:
    the underlying capability is fixed for the life of the process, and a
    reset only makes the probe cost reappear on a hot path.
    """
    global _FTS5_AVAILABLE, _LOADABLE_EXTENSIONS_AVAILABLE
    _FTS5_AVAILABLE = None
    _LOADABLE_EXTENSIONS_AVAILABLE = None
