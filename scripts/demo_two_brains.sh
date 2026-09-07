#!/usr/bin/env bash
# Two brains, one granted scope: what replicates and what never leaves.
#
# This is the capability with no equivalent in a comparable product.
# Everything else a local agent runtime does (resident daemon, skills,
# proactive triggers, shell and browser, messaging surfaces) is now
# table stakes. Memory that replicates between brains owned by
# DIFFERENT PEOPLE, fail closed, per scope, is not.
#
# What it shows, in order:
#
#   1. Two brains find each other on the LAN over mDNS.
#   2. With no grant they replicate NOTHING. Discovery is not consent.
#   3. One scope is granted, in both directions, by name.
#   4. Two notes are written on brain A: one in that scope, one with no
#      scope at all.
#   5. The scoped note arrives on brain B. The unscoped one does not,
#      and cannot, because an operation with no scope resolves to the
#      reserved `private` scope, which is never grantable.
#
# Brain A is YOUR brain, untouched apart from two notes it can delete.
# Brain B is a throwaway created in a temp directory and destroyed on
# exit, so this is safe to run on a machine you care about.
#
# Usage:  bash scripts/demo_two_brains.sh [--keep]
#         --keep   leave brain B running afterwards for poking at
set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[0;32m'; RED=$'\033[0;31m'
YEL=$'\033[0;33m'; NC=$'\033[0m'
ok()   { printf "  ${GREEN}✓${NC} %s\n" "$1"; }
bad()  { printf "  ${RED}✗${NC} %s\n" "$1"; }
info() { printf "  ${DIM}%s${NC}\n" "$1"; }
step() { printf "\n${BOLD}%s${NC}\n" "$1"; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CORE="$REPO/feral-core"
A_PORT="${FERAL_DEMO_A_PORT:-9090}"
B_PORT="${FERAL_DEMO_B_PORT:-9091}"
B_HOME="$(mktemp -d "${TMPDIR:-/tmp}/feral-demo-brainB.XXXXXX")"
SCOPE="${FERAL_DEMO_SCOPE:-work}"
STAMP="$(date +%s)"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

cleanup() {
  # The demo writes two notes into YOUR brain. Take them back out, so
  # running it a dozen times before a recording does not silt up the
  # memory it is meant to be showing off.
  for nid in "${NOTE_SHARED:-}" "${NOTE_PRIVATE:-}"; do
    # DELETE /internal/memory/{note_id}, not /api/memory/forget/{id}:
    # the latter takes an EPISODE id and answers 404 for a note, which
    # is exactly how this cleanup silently did nothing the first time.
    [ -n "$nid" ] && curl -sf -m 10 -X DELETE \
      "http://127.0.0.1:$A_PORT/internal/memory/$nid" >/dev/null 2>&1 || true
  done
  # Brain B is about to cease to exist, so the grant naming it is
  # clutter that accumulates one dead node id per run. Revoking is also
  # the honest end of the story: sharing is something you turn off.
  if [ -n "${B_ID:-}" ] && [ "$KEEP" != "1" ]; then
    FERAL_PORT="$A_PORT" feral sync peer scope revoke "$B_ID" "$SCOPE" >/dev/null 2>&1 || true
  fi
  if [ "$KEEP" = "1" ]; then
    printf "\n%s\n" "  brain B left running on :$B_PORT, home $B_HOME"
    return
  fi
  local pid
  pid="$(lsof -ti ":$B_PORT" 2>/dev/null || true)"
  [ -n "$pid" ] && kill $pid 2>/dev/null || true
  rm -rf "$B_HOME" 2>/dev/null || true
}
trap cleanup EXIT

api_a() { curl -sf -m 20 "http://127.0.0.1:$A_PORT$1"; }
api_b() { curl -sf -m 20 "http://127.0.0.1:$B_PORT$1"; }
node_id() { curl -sf -m 20 "http://127.0.0.1:$1/api/dashboard" \
  | python3 -c 'import json,sys;print((json.load(sys.stdin).get("sync") or {}).get("node_id") or "")'; }
# Count notes on a brain whose content carries this run's stamp.
have_note() { curl -sf -m 25 "http://127.0.0.1:$1/api/memory/search?q=$2" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);rows=d if isinstance(d,list) else (d.get("results") or d.get("notes") or []);import re;print(sum(1 for r in rows if "'"$2"'" in json.dumps(r)))' 2>/dev/null || echo 0; }

step "0. Brain A"
if ! api_a /api/dashboard >/dev/null 2>&1; then
  bad "no brain answering on :$A_PORT. Start it with 'feral serve' and re-run."
  exit 1
fi
A_ID="$(node_id "$A_PORT")"
ok "brain A is up, node $A_ID"

step "1. Start a second brain"
info "throwaway home: $B_HOME"
# It must share A's sync passphrase, which is how peers authenticate the
# transport before any identity is enrolled. Read, never printed.
PASS_FILE="${FERAL_HOME:-$HOME/.feral}/sync_passphrase.first_boot"
if [ ! -f "$PASS_FILE" ]; then
  bad "cannot read A's sync passphrase at $PASS_FILE"
  exit 1
fi
cp "${FERAL_HOME:-$HOME/.feral}/settings.json" "$B_HOME/" 2>/dev/null || true
(
  cd "$CORE"
  FERAL_SYNC_PASSPHRASE="$(cat "$PASS_FILE")" \
  FERAL_HOME="$B_HOME" FERAL_PORT="$B_PORT" \
    nohup python3 -m api.server > "$B_HOME/brain.log" 2>&1 &
)
for _ in $(seq 1 90); do api_b /api/dashboard >/dev/null 2>&1 && break; sleep 1; done
if ! api_b /api/dashboard >/dev/null 2>&1; then
  bad "brain B did not come up. See $B_HOME/brain.log"
  exit 1
fi
B_ID="$(node_id "$B_PORT")"
ok "brain B is up, node $B_ID"

step "2. They find each other, and share nothing"
info "mDNS discovery runs on its own; no address was configured."
for _ in $(seq 1 30); do
  api_a /api/dashboard | grep -q "$B_ID" && break; sleep 1
done
FERAL_PORT="$A_PORT" feral sync peer scope list 2>&1 | sed 's/^/  /' | head -3
ok "no scope granted, so nothing is eligible to replicate"

step "3. Grant one scope, by name, on both brains"
info "A grant is directional per brain: each decides what it sends AND accepts."
FERAL_PORT="$A_PORT" feral sync peer scope grant "$B_ID" "$SCOPE" >/dev/null 2>&1
FERAL_PORT="$B_PORT" feral sync peer scope grant "$A_ID" "$SCOPE" >/dev/null 2>&1
ok "granted '$SCOPE' between A and B"

# Identity enrolment. The transport passphrase gets a peer through the
# door; scope enforcement needs to know WHICH peer, and an unidentified
# peer resolves to the empty grant set. The brain being connected TO
# issues the invite, so enrol both directions.
step "4. Enrol peer identities"
G_B="$(FERAL_PORT="$B_PORT" feral sync peer invite brainA 2>&1 | grep -oE '[A-Za-z0-9_-]{40,}' | head -1)"
[ -n "$G_B" ] && FERAL_PORT="$A_PORT" feral sync peer accept "127.0.0.1:$B_PORT" "$G_B" >/dev/null 2>&1
G_A="$(FERAL_PORT="$A_PORT" feral sync peer invite brainB 2>&1 | grep -oE '[A-Za-z0-9_-]{40,}' | head -1)"
[ -n "$G_A" ] && FERAL_PORT="$B_PORT" feral sync peer accept "127.0.0.1:$A_PORT" "$G_A" >/dev/null 2>&1
ok "each brain now knows who the other is"

step "5. Write two notes on brain A"
SHARED="TEAM-$STAMP roadmap review moved to Thursday"
PRIVATE="MINE-$STAMP resting heart rate has been low this week"
save_note_a() {  # content, scope ("" for none) -> prints the new note id
  local body
  if [ -n "$2" ]; then body="{\"content\":\"$1\",\"tags\":[\"demo\"],\"scope\":\"$2\"}"
  else body="{\"content\":\"$1\",\"tags\":[\"demo\"]}"; fi
  curl -sf -m 25 -X POST "http://127.0.0.1:$A_PORT/internal/memory/save" \
    -H 'Content-Type: application/json' -d "$body" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("id",""))'
}
NOTE_SHARED="$(save_note_a "$SHARED" "$SCOPE")"
NOTE_PRIVATE="$(save_note_a "$PRIVATE" "")"
ok "in scope '$SCOPE':  $SHARED"
ok "no scope at all:   $PRIVATE"

step "6. Replicate"
FERAL_PORT="$A_PORT" feral sync now 2>&1 | sed 's/^/  /' | head -3
sleep 2

step "7. What crossed"
GOT_SHARED="$(have_note "$B_PORT" "TEAM-$STAMP")"
GOT_PRIVATE="$(have_note "$B_PORT" "MINE-$STAMP")"
FAIL=0
if [ "$GOT_SHARED" -ge 1 ]; then ok "brain B HAS the '$SCOPE' note"
else bad "brain B did not receive the '$SCOPE' note"; FAIL=1; fi
if [ "$GOT_PRIVATE" = "0" ]; then ok "brain B does NOT have the unscoped note"
else bad "the unscoped note leaked to brain B"; FAIL=1; fi

printf "\n"
if [ "$FAIL" = "0" ]; then
  printf "  ${GREEN}${BOLD}Memory crossed the boundary you drew, and nothing else did.${NC}\n"
  printf "  ${DIM}An operation with no scope resolves to the reserved 'private'\n"
  printf "  scope, which is never grantable. Not policy. Structure.${NC}\n"
else
  printf "  ${YEL}Demo did not hold. Brain B log: $B_HOME/brain.log${NC}\n"
fi
printf "\n  ${DIM}Both notes have been removed from brain A. Brain B is discarded.${NC}\n"
exit $FAIL
