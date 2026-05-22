#!/usr/bin/env bash
# install-phone-bridge.sh — one-liner installer for the FERAL phone-bridge
# daemon on macOS / Linux. Lives in a persistent LaunchAgent / systemd
# user unit, streams browser-style sensor data to the Brain the same way
# BrowserNode does, but on a laptop / server where you'd rather run a
# persistent daemon than keep a tab open.
#
# Usage (from the Pair modal's "Daemon token" tab):
#   curl -fsSL http://brain.local:9090/install-phone-bridge.sh | \
#     bash -s -- --token "<TOKEN>" --brain-url "ws://brain.local:9090/v1/node"
#
# Flags:
#   --token      pairing token (required)
#   --brain-url  ws:// or wss:// URL to the Brain's /v1/node endpoint
#   --node-id    optional stable id; derived from hostname by default
#   --prefix     install prefix (default: $HOME/.feral/phone-bridge)

set -euo pipefail

BRAIN_URL=""
TOKEN=""
NODE_ID=""
PREFIX="${HOME}/.feral/phone-bridge"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)      TOKEN="$2"; shift 2;;
    --brain-url)  BRAIN_URL="$2"; shift 2;;
    --node-id)    NODE_ID="$2"; shift 2;;
    --prefix)     PREFIX="$2"; shift 2;;
    -h|--help)
      sed -n '2,30p' "$0" ; exit 0;;
    *)
      echo "unknown flag: $1" >&2 ; exit 2;;
  esac
done

if [[ -z "$TOKEN" || -z "$BRAIN_URL" ]]; then
  echo "error: --token and --brain-url are both required" >&2
  exit 2
fi

if [[ -z "$NODE_ID" ]]; then
  NODE_ID="bridge-$(hostname -s 2>/dev/null || hostname)"
fi

echo "== FERAL phone-bridge installer =="
echo "brain  : $BRAIN_URL"
echo "node   : $NODE_ID"
echo "prefix : $PREFIX"

mkdir -p "$PREFIX"

# Install feral-node-sdk into an isolated venv so the user's system Python
# stays pristine.
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found — install Python 3.11+ first" >&2
  exit 1
fi

python3 -m venv "$PREFIX/venv"
# shellcheck disable=SC1091
source "$PREFIX/venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet feral-node-sdk

# Persistent runner script
RUN="$PREFIX/run.sh"
cat > "$RUN" <<RUN_SCRIPT
#!/usr/bin/env bash
set -euo pipefail
cd "$PREFIX"
source "$PREFIX/venv/bin/activate"
# Lane 11 (audit-r14) — the daemon command is ``feral_node_sdk run``
# (added in v2026.5.39); previously the script invoked the bare
# module, which only exposed pair/discover/key — the actual long-
# lived bridge had no entrypoint and the LaunchAgent was a no-op.
exec python -m feral_node_sdk run \
    --node-id "$NODE_ID" \
    --brain-url "$BRAIN_URL" \
    --token "$TOKEN"
RUN_SCRIPT
chmod +x "$RUN"

# Persist via LaunchAgent (macOS) or systemd --user (Linux).
case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/ai.feral.phone-bridge.plist"
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.feral.phone-bridge</string>
  <key>ProgramArguments</key>
    <array><string>$RUN</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PREFIX/phone-bridge.out.log</string>
  <key>StandardErrorPath</key><string>$PREFIX/phone-bridge.err.log</string>
</dict>
</plist>
PLIST
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl load "$PLIST"
    echo "installed LaunchAgent — tailing $PREFIX/phone-bridge.out.log for first register..."
    ;;
  Linux)
    UNIT="$HOME/.config/systemd/user/feral-phone-bridge.service"
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<UNIT
[Unit]
Description=FERAL phone-bridge daemon
After=network-online.target

[Service]
ExecStart=$RUN
Restart=always
RestartSec=5
StandardOutput=append:$PREFIX/phone-bridge.out.log
StandardError=append:$PREFIX/phone-bridge.err.log

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now feral-phone-bridge.service
    echo "installed systemd user unit — journalctl --user -u feral-phone-bridge -f"
    ;;
  *)
    echo "installed run script at $RUN — start it however you like on $(uname -s)."
    ;;
esac

echo "done. node_id=$NODE_ID pairs via token ${TOKEN:0:8}..."
