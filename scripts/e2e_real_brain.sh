#!/usr/bin/env bash
# Run the v2 e2e specs that talk to a REAL brain.
#
# Every other spec under feral-client-v2/e2e runs against `vite preview`
# with `**/api/**` stubbed. That server answers index.html for any path
# at status 200 and hosts no API, so a route the brain does not serve, a
# JSON fetch answered with HTML, a `/api/*` path nobody registered, and a
# control that reports success while its request failed are all
# structurally invisible there. This script closes that gap locally, the
# same way .github/workflows/v2-real-brain-e2e.yml does in CI.
#
# WARNING: the control walk MUTATES the brain it points at. It clicks
# every control that is not on the destructive blocklist in
# feral-client-v2/e2e/real_brain_util.ts. FERAL_HOME therefore defaults
# to a throwaway directory, and the script refuses to use your real one.
#
# Usage:
#   bash scripts/e2e_real_brain.sh                  # boot a brain, run, tear down
#   FERAL_E2E_URL=http://host:port bash scripts/e2e_real_brain.sh
#                                                   # use a brain that is already up
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT="${FERAL_E2E_PORT:-9461}"
HOME_DIR="${FERAL_E2E_HOME:-/tmp/feral-e2e-home}"
BRAIN_PID=""

if [ "$HOME_DIR" = "$HOME/.feral" ]; then
    echo "  [error] refusing to run the control walk against $HOME/.feral."
    echo "          It clicks things. Set FERAL_E2E_HOME to a throwaway path."
    exit 1
fi

cleanup() {
    if [ -n "$BRAIN_PID" ] && kill -0 "$BRAIN_PID" 2>/dev/null; then
        echo "Stopping the brain (pid $BRAIN_PID)..."
        kill "$BRAIN_PID" 2>/dev/null || true
        wait "$BRAIN_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if [ -z "${FERAL_E2E_URL:-}" ]; then
    # The brain serves the dashboard out of feral-core/webui_v2, not out
    # of feral-client-v2/dist, so a plain `npm run build` is not enough:
    # without this the specs run against whatever bundle was last
    # committed and a change under src/ is not under test at all.
    echo "Building and bundling the v2 client..."
    bash "$SCRIPT_DIR/build_webui_v2.sh"

    mkdir -p "$HOME_DIR"
    echo "Starting a brain on 127.0.0.1:$PORT with FERAL_HOME=$HOME_DIR ..."
    (
        cd "$ROOT/feral-core"
        FERAL_HOME="$HOME_DIR" exec python -m uvicorn api.server:app \
            --host 127.0.0.1 --port "$PORT"
    ) > /tmp/feral-e2e-brain.log 2>&1 &
    BRAIN_PID=$!

    up=0
    for _ in $(seq 1 90); do
        code=$(curl -s -o /dev/null -w '%{http_code}' \
            "http://127.0.0.1:$PORT/api/setup/status" || true)
        if [ "$code" = "200" ]; then up=1; break; fi
        sleep 1
    done
    if [ "$up" != "1" ]; then
        echo "  [error] the brain never answered on port $PORT."
        tail -50 /tmp/feral-e2e-brain.log || true
        exit 1
    fi

    # An incomplete setup makes bootstrap.js redirect every route to
    # /setup, so the walk would measure the wizard 28 times.
    curl -sf -X POST "http://127.0.0.1:$PORT/api/setup/complete" \
        -H 'Content-Type: application/json' \
        -d '{"settings":{},"credentials":{},"identity":{}}' > /dev/null
    export FERAL_E2E_URL="http://127.0.0.1:$PORT"
fi

echo "Running the real-brain specs against $FERAL_E2E_URL ..."
cd "$ROOT/feral-client-v2"
FERAL_E2E_REAL_BRAIN=1 npm run e2e:real-brain
