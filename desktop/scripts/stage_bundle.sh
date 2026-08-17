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

die() {
    echo "  [error] $*" >&2
    exit 1
}

UV="$(bash "$REPO_ROOT/scripts/ensure_uv.sh")"
# ensure_uv.sh logs to stderr and prints one path on stdout. If that
# contract ever changes, every later use of "$UV" turns into a confusing
# "command not found" attributed to the wrong step.
[ -x "$UV" ] || die "scripts/ensure_uv.sh did not yield an executable uv (got: '$UV')."

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
    --exclude '.mypy_cache/' \
    --exclude '.ruff_cache/' \
    --exclude '.venv/' \
    --exclude 'node_modules/' \
    --exclude '.git/' \
    "$REPO_ROOT/feral-core/" "$STAGED_CORE/"

# The v2 web UI is the app's entire visible surface, and it is the one
# part of the payload that is a BUILD ARTEFACT rather than source. A tree
# where `scripts/build_webui_v2.sh` has never run rsyncs perfectly
# happily; the resulting app then starts its brain, answers /health,
# turns the health dot green, and serves api/server.py's packaging-fault
# page where the dashboard should be. That is indistinguishable from a
# working install right up to the moment the user looks at it.
#
# The same omission has already shipped once in a wheel, which is what
# scripts/release_wheel_smoke.py exists to catch on the pip path. This is
# the desktop path's equivalent, and it is REQUIRED: there is no
# degraded-but-useful desktop app without a dashboard.
echo "  -> verifying the staged web UI bundle"
staged_webui="$STAGED_CORE/webui_v2"
[ -f "$staged_webui/index.html" ] || die \
    "the staged payload has no $staged_webui/index.html. The v2 dashboard was
    never built in this tree, so the app would ship without a UI. Run
    'scripts/build_webui_v2.sh' (or 'make bundle-webui') at the repo root and
    stage again."

# index.html without its hashed entry points renders a blank window. Read
# the references out of the built index rather than globbing the assets
# directory, so a stale index pointing at assets that are no longer on
# disk is caught too. Vite renames these on every build, which is exactly
# why the reference is read and not restated.
#
# `grep -oE`, not `sed`: BSD sed (the one on every macOS build machine)
# has no `\|` alternation in a BRE, so the sed spelling of this silently
# matched nothing and would have failed a perfectly good bundle. Matching
# on `assets/...` directly also makes the leading `./` or `/` irrelevant.
webui_refs="$(grep -oE 'assets/[^"]+\.(js|css)' "$staged_webui/index.html" | sort -u)"
[ -n "$webui_refs" ] || die \
    "$staged_webui/index.html references no assets/*.js or assets/*.css.
    That is not a built bundle, and the app would open a blank window."

webui_missing=""
while IFS= read -r ref; do
    [ -f "$staged_webui/$ref" ] || webui_missing="$webui_missing $ref"
done <<< "$webui_refs"
if [ -n "$webui_missing" ]; then
    die "$staged_webui/index.html references asset(s) that are not staged:$webui_missing.
    The bundle is stale against its own index. Rebuild it with
    'scripts/build_webui_v2.sh' and stage again."
fi

echo "     web UI bundle OK ($(echo "$webui_refs" | tr '\n' ' '))"


# ── 2. The interpreter ───────────────────────────────────────────────
#
# python-build-standalone builds are relocatable, which is the property
# that makes this possible at all: the managed install is copied into the
# bundle and still works from its new location.
#
# A VIRTUALENV IS NOT. This distinction is the whole of step 2, because
# getting it wrong is silent on the build machine and total everywhere
# else. A venv's `pyvenv.cfg` carries `home = <the real interpreter>` and
# its `bin/python` is a symlink to that same absolute path; the standard
# library is never inside it. Copy one into an .app and the payload's
# interpreter loads `os`, `encodings` and everything else from a path
# under the *builder's* home directory. Every check below this line then
# passes, on that machine, for that reason.
#
# That had shipped: `uv python find "$PIN"` resolves the ambient project
# environment before the managed install, so run from the repo root it
# answers `$REPO_ROOT/.venv/bin/python3`, and this script rsynced the
# development venv into the bundle. Measured on the .app in
# src-tauri/target/release/bundle at the time this was found:
#
#   Resources/python/pyvenv.cfg
#     home = /Users/<builder>/.local/share/uv/python/cpython-3.11.15-.../bin
#   Resources/python/lib/python3.11/   -> site-packages only, no stdlib
#   bundled python3 -c 'import os; print(os.__file__)'
#     /Users/<builder>/.local/share/uv/python/.../lib/python3.11/os.py
#
# This is the CARGO_MANIFEST_DIR defect that main.rs was rewritten to
# remove, reintroduced in the payload instead of the binary: a build
# machine's absolute path shipped to users, and an app that installs,
# launches, and cannot start its brain.
#
# `--managed-python --system --no-project` is what pins the answer to a
# uv-managed pbs installation: `--no-project` alone still finds the
# repo's .venv, `--system` alone would happily return `/opt/homebrew`.
# The result is then verified rather than trusted, because rsyncing a
# wrong answer is not a step you get to retry cheaply.

#: Prove a directory is a self-contained interpreter root rather than a
#: venv or a system prefix. Cheap, and the only thing standing between a
#: mis-resolved path and 400MB of the wrong tree in the bundle.
assert_self_contained_python() {
    local root="$1" exe="$2" what="$3"

    [ -x "$exe" ] || die "$what: no interpreter at $exe"
    [ ! -e "$root/pyvenv.cfg" ] || die \
        "$what: $root is a virtualenv (it has pyvenv.cfg), not a relocatable
    interpreter. Its standard library lives outside it, so bundling it ships
    an app that cannot start Python on any machine but this one."

    # Ask the interpreter itself. This is the assertion that holds no
    # matter how the tree was produced: sys.base_prefix is where the
    # stdlib comes from, and for a bundle it must be the bundle.
    #
    # The containment test runs in Python rather than the shell so both
    # sides go through realpath. A symlinked component anywhere in the
    # path (/tmp on macOS is /private/tmp) otherwise fails a string
    # comparison that is actually satisfied.
    local report
    if ! report="$("$exe" - "$root" <<'PY' 2>&1
import os, sys

root = os.path.realpath(sys.argv[1])


def inside(path):
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


stdlib = os.path.dirname(os.path.dirname(os.path.realpath(os.__file__)))
problems = []
if not inside(sys.base_prefix):
    problems.append(
        "sys.base_prefix is %s, outside the tree. The interpreter loads its "
        "standard library from a path that exists only on this machine."
        % sys.base_prefix
    )
if not inside(stdlib):
    problems.append("the standard library resolves to %s, outside the tree." % stdlib)
if problems:
    sys.exit("\n    ".join(problems))
PY
    )"; then
        die "$what ($root):
    $report"
    fi
}

stage_python() {
    echo "  -> $STAGED_PY (fetching CPython $PIN)"
    "$UV" python install "$PIN" >&2

    local src src_root
    src="$("$UV" python find --managed-python --system --no-project "$PIN")" \
        || die "uv could not find a managed CPython $PIN after installing it."
    [ -n "$src" ] || die "uv python find returned nothing for $PIN."
    # `uv python find` returns .../bin/python3.11; the root is two up.
    src_root="$(cd "$(dirname "$src")/.." && pwd)"

    # Before copying, not after: 'rsync -a /opt/homebrew/ ...' is not an
    # error you want to discover from the disk-usage line.
    assert_self_contained_python "$src_root" "$src" "resolved CPython $PIN"

    rm -rf "$STAGED_PY"
    mkdir -p "$STAGED_PY"
    # -L is wrong here: pbs uses internal symlinks (python3 -> python3.11)
    # and following them would triple the size. -a preserves them, and
    # pbs's are relative, so they stay inside the bundle. That is checked
    # in step 4 rather than assumed.
    rsync -a "$src_root/" "$STAGED_PY/"
}

staged_python_exe() {
    if [ -x "$STAGED_PY/bin/python3" ]; then
        printf '%s\n' "$STAGED_PY/bin/python3"
    elif [ -x "$STAGED_PY/python.exe" ]; then
        printf '%s\n' "$STAGED_PY/python.exe"
    else
        # Silence here used to hand a nonexistent path to `uv pip install`,
        # which fails several lines later attributing it to the wrong step.
        die "no interpreter was staged: neither $STAGED_PY/bin/python3 nor
    $STAGED_PY/python.exe exists after staging."
    fi
}

# Reuse is an optimisation, and it is only safe when the thing being
# reused is the thing that would have been produced. Matching on version
# alone kept the broken venv described above forever: it reported
# 3.11.15, so every subsequent build skipped restaging and inherited the
# fault. Self-containment is checked too, and a tree that fails it is
# rebuilt rather than reported.
needs_python=1
if [ -x "$STAGED_PY/bin/python3" ]; then
    have="$("$STAGED_PY/bin/python3" -c 'import platform;print(platform.python_version())' 2>/dev/null || true)"
    if [ "$have" = "$PIN" ] && [ ! -e "$STAGED_PY/pyvenv.cfg" ] && \
       "$STAGED_PY/bin/python3" -c 'import sys;sys.exit(0 if sys.base_prefix==sys.prefix else 1)' 2>/dev/null; then
        echo "  -> $STAGED_PY already at $PIN and self-contained, keeping it"
        needs_python=0
    elif [ -n "$have" ]; then
        echo "  -> $STAGED_PY is $have but not a usable relocatable interpreter; restaging"
    fi
fi
if [ "$needs_python" = "1" ]; then
    stage_python
fi

PYEXE="$(staged_python_exe)"

# ── 3. Dependencies, into the staged interpreter ─────────────────────
#
# Non-editable on purpose. An editable install writes the build machine's
# absolute source path into a .pth file, which is the same class of bug as
# the CARGO_MANIFEST_DIR one this bundle exists to fix.
#
# --constraint requirements.lock is what CI and `make dev` use, so the
# shipped app resolves the same versions a contributor tested against.
#
# --break-system-packages is REQUIRED and is not a workaround. uv marks
# its managed pbs installs "externally managed" and refuses to install
# into them, suggesting a virtualenv instead. A virtualenv is precisely
# what cannot be bundled (see step 2), so the flag says what is actually
# meant: install into this interpreter's own site-packages. $STAGED_PY is
# a private copy inside the build tree, so nothing shared is modified;
# the interpreter under ~/.local/share/uv is never written to.
echo "  -> installing feral-core[llm] into the staged interpreter"
"$UV" pip install --python "$PYEXE" --break-system-packages \
    --constraint "$REPO_ROOT/feral-core/requirements.lock" \
    "$REPO_ROOT/feral-core[llm]" >&2

# ── 4. Prove it, here, rather than at the user's first launch ────────
#
# The failure this guards against is silent by construction: a bundled
# interpreter without FTS5 produces an app that installs cleanly, launches
# cleanly, and whose health dot simply never turns green.
echo "  -> verifying the staged interpreter"

# 4a. Self-contained, again, AFTER the dependency install. The install
# rewrites site-packages and can leave a .pth behind; the property that
# matters is a property of the finished tree, so it is asserted on the
# finished tree and not inferred from the one that went in.
assert_self_contained_python "$STAGED_PY" "$PYEXE" "staged interpreter"

# 4b. Nothing in the payload may point outside the payload. pbs uses
# relative internal symlinks (bin/python3 -> python3.11); an absolute
# one, or a relative one that climbs out, is a build-machine path that
# survived into the bundle and will dangle on the user's disk. Both kinds
# are caught the same way, by resolving every link and checking where it
# lands, which is why this is a walk and not a `find -lname '/*'`.
if ! escaped="$("$PYEXE" - "$STAGED_PY" <<'PY' 2>&1
import os, sys

root = os.path.realpath(sys.argv[1])
bad = []
for dirpath, dirnames, filenames in os.walk(root):
    for name in dirnames + filenames:
        path = os.path.join(dirpath, name)
        if not os.path.islink(path):
            continue
        target = os.path.realpath(path)
        if target == root or target.startswith(root + os.sep):
            continue
        bad.append(
            "%s -> %s" % (os.path.relpath(path, root), os.readlink(path))
        )
if bad:
    sys.exit("\n    ".join(bad))
PY
)"; then
    die "the staged interpreter contains symlink(s) pointing outside the
    bundle. These resolve on this machine and dangle on every other one:
    $escaped"
fi

# 4c. No build-machine path may be recorded in site-packages. An editable
# install writes one into a .pth (or an __editable__ finder module), and
# the resulting app imports the builder's checkout instead of the payload
# whenever that path happens to exist, and fails opaquely when it does
# not. Step 3 installs non-editable on purpose; this proves it landed
# that way rather than trusting the flag.
leaked="$(grep -rIl --include='*.pth' --include='__editable__*' \
    -e "$REPO_ROOT" "$STAGED_PY" 2>/dev/null || true)"
[ -z "$leaked" ] || die "the staged interpreter records this machine's source
    path ($REPO_ROOT) in:
$leaked
    That is an editable install leaking into the bundle. Remove
    $STAGED_PY and stage again."

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
#
# The probe also asserts what the brain DECIDED about the web UI, not
# just that it imported. api/server.py picks its UI at import time and
# records the answer in `_webui_variant`; anything other than "v2" means
# the app would launch, go green, and serve a fault page. Checking the
# files exist (step 1) and checking the brain agrees they are servable
# are different assertions, and only the second one is the thing users
# experience.
if ( cd "$STAGED_CORE" && FERAL_HOME="$STAGE_PROBE_HOME" "$PYEXE" -c "
import memory.store, memory.knowledge_graph, api.server  # noqa: F401
from memory.sqlite_features import interpreter_sqlite_report
assert interpreter_sqlite_report()['fts5'], 'staged interpreter reports no FTS5'
variant = api.server._webui_variant
assert variant == 'v2', (
    'the staged brain would serve web UI variant %r, not v2. The bundled app '
    'would start, answer /health, and show a packaging-fault page where the '
    'dashboard belongs.' % (variant,)
)
print('     web UI variant resolved by the staged brain: %s' % variant)
" ) >"$STAGE_PROBE_LOG" 2>&1; then
    echo "     brain modules import cleanly under the staged interpreter"
    grep -a 'web UI variant resolved' "$STAGE_PROBE_LOG" || true
else
    echo "  [error] the staged brain does not import, or would not serve the" >&2
    echo "          v2 dashboard. Full output:" >&2
    cat "$STAGE_PROBE_LOG" >&2
    exit 1
fi

echo "  Staged payload ready:"
du -sh "$STAGED_CORE" "$STAGED_PY" 2>/dev/null || true
