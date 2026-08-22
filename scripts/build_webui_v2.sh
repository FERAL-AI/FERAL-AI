#!/usr/bin/env bash
# Build feral-client-v2 and bundle it into feral-core/webui_v2.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLIENT="$ROOT/feral-client-v2"
WEBUI="$ROOT/feral-core/webui_v2"

echo "Building FERAL web client v2..."

if [ ! -d "$CLIENT" ]; then
    echo "Error: feral-client-v2 directory not found at $CLIENT"
    exit 1
fi

cd "$CLIENT"

# Keep local runs fast while still allowing CI to force a clean install.
if [ "${FERAL_FORCE_NPM_CI:-0}" = "1" ] || [ ! -d node_modules ]; then
    echo "Installing dependencies (npm ci)..."
    npm ci
fi

npm run build

echo "Syncing build output to $WEBUI..."
rm -rf "$WEBUI"
mkdir -p "$WEBUI"
cp -R dist/. "$WEBUI/"

# Ensure setuptools discovers webui_v2 as a package.
cat > "$WEBUI/__init__.py" <<'PY'
"""FERAL v2 ambient-OS client bundle.

This package exists solely so setuptools discovers the directory via
`find_packages(include=["webui_v2*"])` and bundles its static assets
(index.html + assets/*) into the distributed wheel.

The Brain serves these files at `/` (default UI) and `/v2/` (alias) via
StaticFiles mounts configured in feral-core/api/server.py.
"""
PY
mkdir -p "$WEBUI/assets"
touch "$WEBUI/assets/__init__.py"

# Same reason, for the PWA icons. Without an __init__.py setuptools does
# not discover `webui_v2/icons` as a package, so no package-data glob can
# reach it and the wheel shipped none of the three icons that
# manifest.webmanifest and index.html both reference. Only created when
# the directory exists, so a bundle without icons is not given an empty
# package.
if [ -d "$WEBUI/icons" ]; then
  touch "$WEBUI/icons/__init__.py"
fi

echo "Done. v2 web UI bundled at $WEBUI"
