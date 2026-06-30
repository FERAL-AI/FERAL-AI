#!/usr/bin/env bash
# Regenerate feral-core/requirements.lock from feral-core/pyproject.toml.
#
# CI-flake fix (P1, dependency reproducibility): the release wheel-smoke
# (scripts/release_wheel_smoke.py) is a bare `pip install` from PyPI;
# without a constraint file a clean resolve a week from now is allowed
# to pull a freshly-published next-major that breaks the import surface
# (this has bitten us at least once already — fastapi==0.137 regressed
# `app.include_router`, see the inline comment in
# feral-core/pyproject.toml). The committed lockfile pins every direct
# AND transitive dep to a single resolution so:
#
#   1. CI installs (.github/workflows/ci.yml) feed it via
#      `--constraint requirements.lock`, eliminating "works locally,
#      breaks on the ubuntu runner because the runner cache had a
#      newer transitive".
#   2. The release wheel-smoke (.github/workflows/publish.yml) does the
#      same so the wheel is built and tested against the exact graph
#      we shipped.
#
# Run this script whenever you bump a runtime dep, add a new dep, or
# raise a ceiling. Re-commit the resulting requirements.lock with the
# change.
#
# Requirements: a Python 3.11+ venv with `pip-tools` installed. If
# pip-tools is unavailable, you can fall back to:
#   python -m pip install --upgrade .[all,dev]
#   python -m pip freeze > feral-core/requirements.lock
# (less precise — pip freeze records the *current* env, not the
#  reproducible resolve, so prefer pip-compile when possible).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."  # repo root

if ! python -m piptools --version >/dev/null 2>&1; then
    echo "Installing pip-tools..."
    python -m pip install --quiet pip-tools
fi

cd feral-core
python -m piptools compile \
    --extra dev \
    --extra all \
    --resolver backtracking \
    --strip-extras \
    --output-file requirements.lock \
    pyproject.toml

echo
echo "  ✓ feral-core/requirements.lock regenerated."
echo "    Review the diff (especially major-version bumps), commit the"
echo "    change, and re-run the broad pytest suite to confirm nothing"
echo "    in the resolution broke."
