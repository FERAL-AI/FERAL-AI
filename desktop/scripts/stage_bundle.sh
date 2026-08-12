#!/usr/bin/env bash
#
# Stage everything the desktop app needs to run the brain on a machine
# that has no FERAL checkout and no suitable Python.
#
# Produces, under desktop/src-tauri/resources/:
#
#   feral-core/   the brain source, because `api.server` is started as
#                 `python -m api.server` with cwd set to this directory.
#   python/       a relocatable python-build-standalone interpreter with
#                 feral-core's dependencies already installed into it.
#
# tauri.conf.json ships both via `bundle.resources`, and main.rs resolves
# them at run time through `app.path().resource_dir()`.
#
# WHY THE INTERPRETER IS BUNDLED AT ALL, rather than calling the user's
# python3: FERAL's SQLite needs FTS5 (required; without it MemoryStore
# raises during construction and the brain never serves a request) and
# benefits from loadable extensions (optional; gates sqlite-vec). Stock
# interpreters ship one or the other. Measured on macOS arm64:
#
#   pyenv 3.11.11                    sqlite 3.51.0  fts5 yes  loadable no
#   python-build-standalone 3.11.13  sqlite 3.49.1  fts5 NO   loadable yes
#   python-build-standalone 3.11.15  sqlite 3.53.1  fts5 yes  loadable yes
#
# A GUI app cannot ask a user to audit their interpreter's compile flags,
# so it brings one whose flags are known. The pin is read from the repo's
# .python-pin, the same file `make dev` uses, so the desktop bundle and
# the development environment cannot drift apart.
#
# Idempotent. Re-running with the staged tree already present and current
# re-syncs the source and leaves the interpreter alone.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$DESKTOP_DIR/.." && pwd)"
RESOURCES="$DESKTOP_DIR/src-tauri/resources"
STAGED_CORE="$RESOURCES/feral-core"
STAGED_PY="$RESOURCES/python"

PIN="$(cat "$REPO_ROOT/.python-pin")"
if [ -z "$PIN" ]; then
    echo "  [error] $REPO_ROOT/.python-pin is empty." >&2
    exit 1
fi

echo "  Staging FERAL desktop payload (Python $PIN)"

UV="$(bash "$REPO_ROOT/scripts/ensure_uv.sh")"

mkdir -p "$RESOURCES"

# ── 1. The brain source ──────────────────────────────────────────────
#
# `build/` is excluded deliberately: it is a stale duplicate of the whole
# source tree (see CLAUDE.md trap 1) and shipping it would put a second,
# older `agents/`, `api/` and `memory/` on the interpreter's path.
# `tests/` and caches are excluded because nothing at run time reads them.
echo "  -> $STAGED_CORE"
rsync -a --delete \
    --exclude 'build/' \
    --exclude 'dist/' \
    --exclude 'tests/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '*.egg-info/' \
    --exclude '.pytest_cache/' \
    "$REPO_ROOT/feral-core/" "$STAGED_CORE/"

# ── 2. The interpreter ───────────────────────────────────────────────
#
# python-build-standalone builds are relocatable, which is the property
# that makes this possible at all: the managed install is copied into the
# bundle and still works from its new location.
stage_python() {
    echo "  -> $STAGED_PY (fetching CPython $PIN)"
    "$UV" python install "$PIN" >&2

    local src
    src="$("$UV" python find "$PIN")"
    # `uv python find` returns .../bin/python3; the root is two up.
    src="$(cd "$(dirname "$src")/.." && pwd)"

    rm -rf "$STAGED_PY"
    mkdir -p "$STAGED_PY"
    # -L is wrong here: pbs uses internal symlinks (python3 -> python3.11)
    # and following them would triple the size. -a preserves them.
    rsync -a "$src/" "$STAGED_PY/"
}

staged_python_exe() {
    if [ -x "$STAGED_PY/bin/python3" ]; then
        printf '%s\n' "$STAGED_PY/bin/python3"
    else
        printf '%s\n' "$STAGED_PY/python.exe"
    fi
}

needs_python=1
if [ -x "$STAGED_PY/bin/python3" ]; then
    have="$("$STAGED_PY/bin/python3" -c 'import platform;print(platform.python_version())' 2>/dev/null || true)"
    if [ "$have" = "$PIN" ]; then
        echo "  -> $STAGED_PY already at $PIN, keeping it"
        needs_python=0
    fi
fi
[ "$needs_python" = "1" ] && stage_python

PYEXE="$(staged_python_exe)"

# ── 3. Dependencies, into the staged interpreter ─────────────────────
#
# Non-editable on purpose. An editable install writes the build machine's
# absolute source path into a .pth file, which is the same class of bug as
# the CARGO_MANIFEST_DIR one this bundle exists to fix.
#
# --constraint requirements.lock is what CI and `make dev` use, so the
# shipped app resolves the same versions a contributor tested against.
echo "  -> installing feral-core[llm] into the staged interpreter"
"$UV" pip install --python "$PYEXE" \
    --constraint "$REPO_ROOT/feral-core/requirements.lock" \
    "$REPO_ROOT/feral-core[llm]" >&2

# ── 4. Prove it, here, rather than at the user's first launch ────────
#
# The failure this guards against is silent by construction: a bundled
# interpreter without FTS5 produces an app that installs cleanly, launches
# cleanly, and whose health dot simply never turns green.
echo "  -> verifying the staged interpreter"
"$PYEXE" - <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(":memory:")
try:
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    fts5 = True
except Exception as exc:
    fts5 = False
    fts5_err = exc
loadable = hasattr(conn, "enable_load_extension")

print(f"     python  : {sys.version.split()[0]}")
print(f"     sqlite  : {sqlite3.sqlite_version}")
print(f"     fts5    : {'OK' if fts5 else 'MISSING'}")
print(f"     loadext : {'OK' if loadable else 'absent'}")
if not fts5:
    sys.exit(
        f"     [error] the staged interpreter has no FTS5 ({fts5_err}). "
        "The bundled brain would fail at MemoryStore construction."
    )
PY

# The dependency set has to import too. `pip install` succeeding says
# nothing about whether the wheels actually load on this interpreter.
#
# FERAL_HOME is redirected at a throwaway directory, and this is not
# cosmetic: importing api.server constructs real subsystems. Run against
# the default ~/.feral it opened the developer's live vault and rebuilt
# an FTS index in their real memory.db ("Repaired knowledge_fts ...").
# A build step must not touch the operator's data.
#
# Boot chatter goes to a log rather than the console: importing the brain
# emits vault, LLM-failover and web-UI lines that look like errors in a
# build transcript. The log is printed in full if the import fails.
STAGE_PROBE_HOME="$(mktemp -d)"
STAGE_PROBE_LOG="$(mktemp)"
trap 'rm -rf "$STAGE_PROBE_HOME" "$STAGE_PROBE_LOG"' EXIT
if ( cd "$STAGED_CORE" && FERAL_HOME="$STAGE_PROBE_HOME" "$PYEXE" -c "
import memory.store, memory.knowledge_graph, api.server  # noqa: F401
from memory.sqlite_features import interpreter_sqlite_report
assert interpreter_sqlite_report()['fts5']
" ) >"$STAGE_PROBE_LOG" 2>&1; then
    echo "     brain modules import cleanly under the staged interpreter"
else
    echo "  [error] the staged brain does not import. Full output:" >&2
    cat "$STAGE_PROBE_LOG" >&2
    exit 1
fi

echo "  Staged payload ready:"
du -sh "$STAGED_CORE" "$STAGED_PY" 2>/dev/null || true
