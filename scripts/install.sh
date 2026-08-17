#!/usr/bin/env bash
#
# FERAL One-Line Installer
# ==========================
# curl -sSL https://raw.githubusercontent.com/FERAL-AI/FERAL-AI/main/scripts/install.sh | bash
#
set -euo pipefail

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

VENV_DIR="$HOME/.feral-env"

SKIP_WIZARD=0
for arg in "$@"; do
    [ "$arg" = "--skip-wizard" ] && SKIP_WIZARD=1
done

echo -e "${BOLD}${CYAN}"
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║            F E R A L  Installer                    ║"
echo "  ║   Unleashed AI · Privacy-First                   ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo -e "${NC}"

# ─── Check Python ───────────────────────────────────────

# Two requirements, not one. The version check alone used to be the whole
# gate, and it lets through interpreters FERAL cannot run on: the memory
# store creates five `CREATE VIRTUAL TABLE ... USING fts5` tables while it
# is being constructed, so an interpreter whose SQLite was built without
# FTS5 does not degrade, the brain fails at boot. Not hypothetical:
# python-build-standalone 3.11.13 links SQLite 3.49.1 and has no FTS5.
# Catching it here costs one subprocess and saves a confusing first run.
PYTHON=""
PYTHON_NO_FTS5=""
for cmd in python3 python python3.13 python3.12 python3.11; do
    command -v "$cmd" &> /dev/null || continue
    ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    [ "$major" -ge 3 ] && [ "$minor" -ge 11 ] || continue
    if "$cmd" -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')" >/dev/null 2>&1; then
        PYTHON="$cmd"
        break
    fi
    # Remember it so the failure message can be specific about why an
    # otherwise new-enough interpreter was rejected.
    [ -z "$PYTHON_NO_FTS5" ] && PYTHON_NO_FTS5="$cmd"
done

if [ -z "$PYTHON" ] && [ -n "$PYTHON_NO_FTS5" ]; then
    sqlite_ver=$("$PYTHON_NO_FTS5" -c "import sqlite3; print(sqlite3.sqlite_version)" 2>/dev/null || echo "unknown")
    echo -e "${RED}  Your Python is new enough but its SQLite has no FTS5.${NC}"
    echo ""
    echo "    interpreter: $(command -v "$PYTHON_NO_FTS5") ($("$PYTHON_NO_FTS5" --version 2>&1 | awk '{print $2}'))"
    echo "    sqlite:      $sqlite_ver (built without FTS5)"
    echo ""
    echo "  FERAL's memory store creates FTS5 tables at startup, so it cannot"
    echo "  run on this interpreter. No pip install can fix it: the feature is"
    echo "  compiled into SQLite. Install a different Python and re-run:"
    echo "    macOS:  brew install python@3.12"
    echo "    Ubuntu: sudo apt install python3.12"
    echo "    Other:  https://python.org/downloads"
    exit 1
fi

if [ -z "$PYTHON" ]; then
    echo -e "${RED}  Python 3.11+ is required.${NC}"
    echo ""
    echo "  Install it:"
    echo "    macOS:  brew install python@3.12"
    echo "    Ubuntu: sudo apt install python3.12"
    echo "    Other:  https://python.org/downloads"
    exit 1
fi

echo -e "  ${GREEN}✓${NC} Python $($PYTHON --version 2>&1 | awk '{print $2}') (SQLite $($PYTHON -c 'import sqlite3; print(sqlite3.sqlite_version)' 2>/dev/null), FTS5 available)"

# ─── Create Virtual Environment ──────────────────────────

if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    echo -e "  ${GREEN}✓${NC} Virtual environment exists at $VENV_DIR"
else
    echo -e "  ${DIM}Creating virtual environment at $VENV_DIR ...${NC}"
    $PYTHON -m venv "$VENV_DIR" 2>/dev/null || {
        echo -e "  ${YELLOW}⚠${NC} Could not create venv, installing globally"
        VENV_DIR=""
    }
fi

if [ -n "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    PYTHON="$VENV_DIR/bin/python"
    echo -e "  ${GREEN}✓${NC} Activated virtual environment"
fi

$PYTHON -m pip install --upgrade pip --quiet 2>/dev/null || true

# ─── Install ────────────────────────────────────────────

echo ""
echo -e "  Installing FERAL..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || SCRIPT_DIR=""

PIP_LOG=$(mktemp /tmp/feral-pip-XXXXXX.log 2>/dev/null || echo "/tmp/feral-pip-install.log")

install_success=false

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../feral-core/pyproject.toml" ]; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    echo -e "  ${DIM}From local repo: $REPO_ROOT${NC}"
    if $PYTHON -m pip install --upgrade --force-reinstall -e "$REPO_ROOT/feral-core[all]" 2>&1 | tee "$PIP_LOG" | tail -5; then
        install_success=true
    fi
else
    echo -e "  ${DIM}Installing from GitHub...${NC}"
    if $PYTHON -m pip install --upgrade --force-reinstall "feral-ai[all] @ git+https://github.com/FERAL-AI/FERAL-AI.git#subdirectory=feral-core" 2>&1 | tee "$PIP_LOG" | tail -5; then
        install_success=true
    else
        echo -e "  ${DIM}Git install failed. Trying PyPI...${NC}"
        if $PYTHON -m pip install --upgrade --force-reinstall "feral-ai[all]" 2>&1 | tee "$PIP_LOG" | tail -5; then
            install_success=true
        else
            echo -e "  ${DIM}PyPI failed. Cloning repo...${NC}"
            TMPDIR=$(mktemp -d)
            if git clone --depth 1 https://github.com/FERAL-AI/FERAL-AI.git "$TMPDIR/feral" 2>/dev/null; then
                if $PYTHON -m pip install --upgrade --force-reinstall -e "$TMPDIR/feral/feral-core[all]" 2>&1 | tee "$PIP_LOG" | tail -5; then
                    install_success=true
                fi
            fi
        fi
    fi
fi

if [ "$install_success" = false ]; then
    echo ""
    echo -e "  ${RED}Installation failed.${NC}"
    echo -e "  ${DIM}Full log: $PIP_LOG${NC}"
    echo ""
    echo "  Common fixes:"
    echo "    1. Upgrade pip:  $PYTHON -m pip install --upgrade pip"
    echo "    2. Retry:        source ~/.feral-env/bin/activate && pip install feral-ai[all]"
    echo "    3. Manual clone: git clone https://github.com/FERAL-AI/FERAL-AI.git && cd FERAL-AI/feral-core && pip install -e .[all]"
    exit 1
fi

rm -f "$PIP_LOG" 2>/dev/null || true

# ─── Browser Runtime (OPTIONAL) ─────────────────────────
# This download is genuinely optional: FERAL installs and runs without it,
# and it is ~150MB plus (on Linux) an apt step that needs sudo. But the
# previous form sent stdout AND stderr to /dev/null and ended in `|| true`,
# so a failure was indistinguishable from a success and the user was left
# believing they had a browser runtime. The skills that need it then fail
# much later with an error that names Playwright, not this install step.
#
# Same shape as `make dev-deps`: still non-fatal, but it says what was lost.
echo -e "  ${DIM}Installing Playwright Chromium runtime (optional)...${NC}"
PLAYWRIGHT_LOG=$(mktemp /tmp/feral-playwright-XXXXXX.log 2>/dev/null || echo "/tmp/feral-playwright.log")
if $PYTHON -m playwright install chromium --with-deps > "$PLAYWRIGHT_LOG" 2>&1; then
    echo -e "  ${GREEN}✓${NC} Playwright Chromium runtime installed"
    rm -f "$PLAYWRIGHT_LOG" 2>/dev/null || true
else
    echo -e "  ${YELLOW}!${NC} Playwright Chromium download failed (this is not fatal)."
    echo -e "    ${DIM}LOST: browser-driven skills (browser_use, agentic_computer_use,${NC}"
    echo -e "    ${DIM}      browser_memory) cannot run until a browser is present.${NC}"
    echo -e "    ${DIM}Everything else works. To retry:${NC}"
    echo -e "    ${DIM}  $PYTHON -m playwright install chromium --with-deps${NC}"
    echo -e "    ${DIM}Log: $PLAYWRIGHT_LOG${NC}"
fi

# ─── Installed Package Diagnostics ──────────────────────
PKG_INFO="$($PYTHON -m pip show feral-ai 2>/dev/null || true)"
if [ -n "$PKG_INFO" ]; then
    PKG_VERSION="$(printf '%s\n' "$PKG_INFO" | awk -F': ' '/^Version:/{print $2}')"
    PKG_LOCATION="$(printf '%s\n' "$PKG_INFO" | awk -F': ' '/^Location:/{print $2}')"
    if [ -n "${PKG_VERSION:-}" ]; then
        echo -e "  ${GREEN}✓${NC} Installed package: feral-ai ${PKG_VERSION}"
    fi
    if [ -n "${PKG_LOCATION:-}" ]; then
        echo -e "  ${DIM}Location: ${PKG_LOCATION}${NC}"
    fi
fi

# ─── Verify CLI ─────────────────────────────────────────

echo ""
if command -v feral &> /dev/null; then
    echo -e "  ${GREEN}✓${NC} feral command available"
else
    echo -e "  ${YELLOW}⚠${NC} 'feral' not found in PATH"
    if [ -n "$VENV_DIR" ]; then
        echo -e "  ${DIM}Activate your env first: source ~/.feral-env/bin/activate${NC}"
    else
        echo -e "  ${DIM}Try: $PYTHON -m cli.main${NC}"
    fi
fi

# ─── First-Time Setup ──────────────────────────────────

echo ""
echo -e "${GREEN}${BOLD}  Installed!${NC}"
echo ""

FERAL_CREDS="$HOME/.feral/credentials.json"

if ([ ! -f "$FERAL_CREDS" ] || [ ! -s "$FERAL_CREDS" ]) && [ "$SKIP_WIZARD" = 0 ]; then
    echo -e "  ${CYAN}Running setup wizard...${NC}"
    if command -v feral &> /dev/null; then
        feral setup || { echo "  Wizard failed, exiting"; exit 1; }
    else
        $PYTHON -m cli.setup_wizard || { echo "  Wizard failed"; exit 1; }
    fi
    echo ""
fi

# ─── Post-Install Summary ──────────────────────────────

echo -e "  ${BOLD}┌──────────────────────────────────────────────┐${NC}"
echo -e "  ${BOLD}│  Start FERAL:                               │${NC}"
echo -e "  ${BOLD}│                                              │${NC}"
if [ -n "$VENV_DIR" ]; then
echo -e "  ${BOLD}│    source ~/.feral-env/bin/activate         │${NC}"
fi
echo -e "  ${BOLD}│    feral start                              │${NC}"
echo -e "  ${BOLD}│                                              │${NC}"
echo -e "  ${BOLD}│  Brain + dashboard at localhost:9090          │${NC}"
echo -e "  ${BOLD}└──────────────────────────────────────────────┘${NC}"
echo ""
echo -e "  ${DIM}Other commands:${NC}"
echo "    feral setup      Re-run setup wizard"
echo "    feral doctor     Check what's working"
echo "    feral serve      Headless server mode"
echo "    feral status     Current brain status"
echo ""

# ─── Verify installed version matches pinned pyproject.toml ──
# Catches the "stale wheel from a prior install" foot-gun. Runs AFTER
# the pip step but BEFORE we exec into `feral start`, so we can bail out
# with a clean remediation hint instead of booting a mismatched brain.
PYPROJECT_PATH=""
for candidate in \
    "${SCRIPT_DIR:-}/../feral-core/pyproject.toml" \
    "./feral-core/pyproject.toml" \
    "./pyproject.toml"; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
        PYPROJECT_PATH="$candidate"
        break
    fi
done

if [ -n "$PYPROJECT_PATH" ]; then
    EXPECTED_VERSION="$(grep -E '^version = ' "$PYPROJECT_PATH" | head -n1 | awk -F'"' '{print $2}')"
    INSTALLED_VERSION="$($PYTHON -c 'import importlib.metadata as m; print(m.version("feral-ai"))' 2>/dev/null || echo unknown)"

    if [ -n "${EXPECTED_VERSION:-}" ] && [ "${INSTALLED_VERSION}" != "${EXPECTED_VERSION}" ]; then
        echo -e "  ${YELLOW}⚠${NC}  FERAL installed version ${INSTALLED_VERSION} does not match expected ${EXPECTED_VERSION}."
        echo "    This usually means a stale wheel from a prior install. Re-run with:"
        echo "      pip install --upgrade --force-reinstall feral-ai==${EXPECTED_VERSION}"
        exit 1
    fi

    if [ -n "${EXPECTED_VERSION:-}" ]; then
        echo -e "  ${GREEN}✓${NC} FERAL ${INSTALLED_VERSION} installed and version-verified."
    fi
else
    echo -e "  ${DIM}(skipped version verification — no pyproject.toml on disk)${NC}"
fi

# ─── Auto-Start FERAL Brain ──────────────────────────────
echo -e "  ${CYAN}Starting FERAL Brain...${NC}"
if command -v feral &> /dev/null; then
    exec feral start
else
    exec "$PYTHON" -m cli.main start
fi
