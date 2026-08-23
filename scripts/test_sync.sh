#!/usr/bin/env bash
#
# FERAL Federated Sync — Two-Node Test
# ========================================
# Spins up two Brain instances on different ports, saves a note on
# instance A, triggers sync, and verifies the note appears on B.
#
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'
BOLD='\033[1m'

PORT_A=9091
PORT_B=9092
PASSPHRASE="test-sync-passphrase"

cleanup() {
    echo -e "\n${YELLOW}Cleaning up...${NC}"
    kill "$PID_A" 2>/dev/null || true
    kill "$PID_B" 2>/dev/null || true
    rm -rf /tmp/feral-sync-test-a /tmp/feral-sync-test-b
}
trap cleanup EXIT

echo -e "${BOLD}FERAL Federated Sync Test${NC}"
echo "=========================="

# Create isolated data directories
mkdir -p /tmp/feral-sync-test-a /tmp/feral-sync-test-b

CORE_DIR="$(cd "$(dirname "$0")/../feral-core" && pwd)"

echo -e "\n[1/5] Starting Brain A on port $PORT_A..."
FERAL_HOME=/tmp/feral-sync-test-a \
FERAL_SYNC_PASSPHRASE="$PASSPHRASE" \
    python -m uvicorn api.server:uvicorn_app --no-proxy-headers --host 127.0.0.1 --port "$PORT_A" \
    --log-level warning &
PID_A=$!

echo "[2/5] Starting Brain B on port $PORT_B..."
FERAL_HOME=/tmp/feral-sync-test-b \
FERAL_SYNC_PASSPHRASE="$PASSPHRASE" \
    python -m uvicorn api.server:uvicorn_app --no-proxy-headers --host 127.0.0.1 --port "$PORT_B" \
    --log-level warning &
PID_B=$!

echo "Waiting for servers to start..."
sleep 8

# Health check
for port in $PORT_A $PORT_B; do
    if ! curl -sf "http://localhost:$port/health" > /dev/null 2>&1; then
        echo -e "${RED}Brain on port $port failed to start${NC}"
        exit 1
    fi
done
echo -e "${GREEN}Both instances running.${NC}"

echo -e "\n[3/5] Saving a note on instance A..."
NOTE_RESULT=$(curl -sf -X POST "http://localhost:$PORT_A/internal/memory/save" \
    -H "Content-Type: application/json" \
    -d '{"content": "Sync test note from instance A", "tags": ["sync-test"], "importance": "normal"}')
echo "  Result: $NOTE_RESULT"

echo -e "\n[4/5] Exporting memory bundle from A..."
BUNDLE=$(curl -sf "http://localhost:$PORT_A/api/sync/export")
echo "  Bundle size: $(echo "$BUNDLE" | wc -c) bytes"

echo "[4/5] Importing bundle into B..."
IMPORT_RESULT=$(curl -sf -X POST "http://localhost:$PORT_B/api/sync/import" \
    -H "Content-Type: application/json" \
    -d "$BUNDLE")
APPLIED=$(echo "$IMPORT_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('applied',0))" 2>/dev/null || echo "0")
echo "  Applied: $APPLIED operations"

echo -e "\n[5/5] Verifying note on instance B..."
SEARCH_RESULT=$(curl -sf "http://localhost:$PORT_B/internal/memory/search?query=sync+test&limit=5")
FOUND=$(echo "$SEARCH_RESULT" | python3 -c "import sys,json; r=json.load(sys.stdin); print(len(r) if isinstance(r,list) else 0)" 2>/dev/null || echo "0")

if [ "$FOUND" -gt 0 ]; then
    echo -e "${GREEN}${BOLD}  PASS — Note from A found on B! ($FOUND results)${NC}"
else
    echo -e "${RED}${BOLD}  FAIL — Note not found on B${NC}"
    echo "  Search result: $SEARCH_RESULT"
    exit 1
fi

echo -e "\n${GREEN}Sync test completed successfully.${NC}"
