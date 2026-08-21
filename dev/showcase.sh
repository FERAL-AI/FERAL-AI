#!/usr/bin/env bash
# Internal/dev use only. Not documented or shipped to users.
set -euo pipefail

# FERAL Showcase — one-command setup
# Usage: bash dev/showcase.sh

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

banner() {
  echo ""
  echo -e "${CYAN}${BOLD}"
  echo "  ╔══════════════════════════════════════╗"
  echo "  ║         F E R A L  Demo                ║"
  echo "  ║     Unleashed AI  v1.2.0              ║"
  echo "  ╚══════════════════════════════════════╝"
  echo -e "${NC}"
}

info()  { echo -e "  ${GREEN}✓${NC} $1"; }
warn()  { echo -e "  ${YELLOW}!${NC} $1"; }
fail()  { echo -e "  ${RED}✗${NC} $1"; exit 1; }

banner

# Step 1: Check Docker
if ! command -v docker &>/dev/null; then
  fail "Docker is not installed. Get it at https://docker.com"
fi
if ! docker info &>/dev/null 2>&1; then
  fail "Docker daemon is not running. Start Docker Desktop first."
fi
info "Docker is ready"

# Step 2: Check docker compose
if docker compose version &>/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE="docker-compose"
else
  fail "docker compose not found"
fi
info "Using: $COMPOSE"

# Step 3: Set up .env
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  info "Created .env from .env.example"

  echo ""
  echo -e "  ${BOLD}Configure your LLM:${NC}"
  echo -e "  ${DIM}Press Enter to skip (Ollama mode) or paste your key:${NC}"
  echo ""
  read -rp "  OPENAI_API_KEY (optional): " OPENAI_KEY

  if [ -n "$OPENAI_KEY" ]; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s|OPENAI_API_KEY=.*|OPENAI_API_KEY=$OPENAI_KEY|" .env
    else
      sed -i "s|OPENAI_API_KEY=.*|OPENAI_API_KEY=$OPENAI_KEY|" .env
    fi
    info "OpenAI key configured"
  else
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s|FERAL_LLM_PROVIDER=.*|FERAL_LLM_PROVIDER=ollama|" .env
    else
      sed -i "s|FERAL_LLM_PROVIDER=.*|FERAL_LLM_PROVIDER=ollama|" .env
    fi
    warn "No OpenAI key — using Ollama (make sure it's running: ollama serve)"
  fi
else
  info ".env already exists"
fi

# Step 4: Build and start
echo ""
echo -e "  ${BOLD}Starting FERAL...${NC}"
$COMPOSE up -d --build 2>&1 | while IFS= read -r line; do echo "  $line"; done

# Step 5: Wait for Brain health
echo ""
echo -ne "  Waiting for Brain to start"
for i in $(seq 1 30); do
  if curl -sf http://localhost:9090/ >/dev/null 2>&1; then
    echo ""
    info "Brain is running at http://localhost:9090"
    break
  fi
  echo -n "."
  sleep 2
done

if ! curl -sf http://localhost:9090/ >/dev/null 2>&1; then
  echo ""
  warn "Brain didn't start in time. Check: $COMPOSE logs feral-brain"
fi

# Step 6: Open browser
echo ""
# The dashboard used to be a separate nginx container on :3000 built from
# feral-client/ (the v1 client). That directory and its compose service
# were removed in 2026.8.12; the brain serves the v2 dashboard itself.
info "Dashboard is at http://localhost:9090"
echo ""

if command -v open &>/dev/null; then
  open "http://localhost:9090"
elif command -v xdg-open &>/dev/null; then
  xdg-open "http://localhost:9090"
else
  echo -e "  ${DIM}Open http://localhost:9090 in your browser${NC}"
fi

echo -e "  ${BOLD}Done!${NC} FERAL is running."
echo ""
echo -e "  ${DIM}Useful commands:${NC}"
echo -e "    $COMPOSE logs -f feral-brain    ${DIM}# Brain logs${NC}"
echo -e "    $COMPOSE down                  ${DIM}# Stop everything${NC}"
echo ""
