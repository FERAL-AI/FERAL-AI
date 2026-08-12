#!/usr/bin/env bash
#
# Print the path to a uv new enough to install FERAL's pinned interpreter.
# Downloads a repo-local copy if the machine has none, or has an old one.
#
# WHY A MINIMUM VERSION EXISTS, and why it cannot be relaxed:
#
#   uv resolves `uv python install X.Y.Z` against a manifest baked into
#   its own binary. Every CPython 3.11 that uv 0.7.x can reach comes from
#   the python-build-standalone generation that shipped SQLite WITHOUT
#   FTS5 (verified: pbs 3.11.13 links SQLite 3.49.1, and
#   `CREATE VIRTUAL TABLE t USING fts5(x)` raises
#   `sqlite3.OperationalError: no such module: fts5`). FERAL's MemoryStore
#   creates five FTS5 virtual tables at boot, so an old uv does not build
#   a slower environment, it builds one where the brain does not start.
#   3.11.15, which has both FTS5 and loadable extensions, first appears in
#   pbs release 20260807 and needs a uv that knows about it.
#
# WHY THIS DOWNLOADS INSTEAD OF SAYING "run uv self update":
#
#   `make dev` is meant to be the one command a new contributor runs. An
#   instruction to go upgrade a global tool is a second command, and
#   `uv self update` also mutates a binary the developer may be using for
#   unrelated projects. A repo-local uv under .uv/ touches nothing else
#   and is removed by `make clean-uv`.
#
# The system uv is preferred whenever it is new enough, so the common case
# downloads nothing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Minimum that can reach pbs 3.11.15. Raise this only with a measurement.
UV_MIN="${UV_MIN:-0.12.0}"
# The exact version fetched when we have to download one. Pinned rather
# than "latest" so two contributors bootstrapping a week apart get the
# same resolver.
UV_FETCH_VERSION="${UV_FETCH_VERSION:-0.12.3}"
UV_LOCAL_DIR="${UV_LOCAL_DIR:-$REPO_ROOT/.uv}"

log() { printf '%s\n' "$*" >&2; }

# Return 0 when $1 (a version string) is >= $UV_MIN.
version_ok() {
    local have="$1"
    [ -n "$have" ] || return 1
    # sort -V puts the lower version first; if that is still UV_MIN then
    # `have` is at least UV_MIN. Equal versions also satisfy this.
    local oldest
    oldest="$(printf '%s\n%s\n' "$UV_MIN" "$have" | sort -V | head -1)"
    [ "$oldest" = "$UV_MIN" ]
}

uv_version() {
    "$1" --version 2>/dev/null | awk '{print $2}'
}

# 1. An explicitly nominated uv always wins, so CI and anyone with uv in
#    an unusual place can point at it without editing anything.
if [ -n "${UV:-}" ] && [ -x "${UV}" ]; then
    if version_ok "$(uv_version "$UV")"; then
        printf '%s\n' "$UV"
        exit 0
    fi
    log "  [warn] UV=$UV is $(uv_version "$UV"), older than $UV_MIN. Ignoring it."
fi

# 2. A repo-local uv we previously downloaded.
if [ -x "$UV_LOCAL_DIR/uv" ] && version_ok "$(uv_version "$UV_LOCAL_DIR/uv")"; then
    printf '%s\n' "$UV_LOCAL_DIR/uv"
    exit 0
fi

# 3. The system uv, when it is new enough. Preferred over downloading.
SYS_UV="$(command -v uv 2>/dev/null || true)"
if [ -n "$SYS_UV" ] && version_ok "$(uv_version "$SYS_UV")"; then
    printf '%s\n' "$SYS_UV"
    exit 0
fi

# 4. Download one. Astral's installer honours UV_INSTALL_DIR and installs
#    nothing outside it (INSTALLER_NO_MODIFY_PATH stops it editing shell
#    rc files, which a build step has no business doing).
if [ -n "$SYS_UV" ]; then
    log "  uv $(uv_version "$SYS_UV") found, but $UV_MIN+ is required to install"
    log "  FERAL's pinned interpreter. Fetching uv $UV_FETCH_VERSION into"
    log "  $UV_LOCAL_DIR (your system uv is left alone)."
else
    log "  uv not found. Fetching uv $UV_FETCH_VERSION into $UV_LOCAL_DIR."
fi

mkdir -p "$UV_LOCAL_DIR"
if ! command -v curl >/dev/null 2>&1; then
    log "  [error] curl is required to bootstrap uv. Install uv $UV_MIN+ yourself:"
    log "          https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

if ! curl -LsSf --retry 3 --max-time 180 \
        "https://astral.sh/uv/${UV_FETCH_VERSION}/install.sh" \
        | env UV_INSTALL_DIR="$UV_LOCAL_DIR" INSTALLER_NO_MODIFY_PATH=1 sh >&2; then
    log "  [error] could not download uv $UV_FETCH_VERSION."
    log "          Install uv $UV_MIN+ manually, then re-run:"
    log "          https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

if [ ! -x "$UV_LOCAL_DIR/uv" ]; then
    log "  [error] uv installer reported success but $UV_LOCAL_DIR/uv is missing."
    exit 1
fi

got="$(uv_version "$UV_LOCAL_DIR/uv")"
if ! version_ok "$got"; then
    log "  [error] downloaded uv reports version '$got', which is below $UV_MIN."
    exit 1
fi

log "  uv $got ready at $UV_LOCAL_DIR/uv"
printf '%s\n' "$UV_LOCAL_DIR/uv"
