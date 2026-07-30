#!/usr/bin/with-contenv bashio

# Read config from Home Assistant
LLM_PROVIDER=$(bashio::config 'llm_provider')
OLLAMA_URL=$(bashio::config 'ollama_url')
OPENAI_KEY=$(bashio::config 'openai_api_key')
ANTHROPIC_KEY=$(bashio::config 'anthropic_api_key')

# Export as env vars for FERAL
export FERAL_LLM_PROVIDER="${LLM_PROVIDER}"
export OLLAMA_BASE_URL="${OLLAMA_URL}"
export OPENAI_API_KEY="${OPENAI_KEY}"
export ANTHROPIC_API_KEY="${ANTHROPIC_KEY}"

# Persist FERAL state on the add-on's /config volume. Without this the
# loader falls back to ~/.feral, which is ephemeral inside the container
# and wiped on every add-on restart/upgrade. /config/feral is the durable
# location promised by UPGRADE.md and matches config.yaml's persisted map.
export FERAL_HOME="/config/feral"
mkdir -p "${FERAL_HOME}"

# Use HA Supervisor token for Home Assistant integration
export FERAL_HA_URL="http://supervisor/core"
export FERAL_HA_TOKEN="${SUPERVISOR_TOKEN}"

# Bind to all interfaces inside the container
export FERAL_HOST="0.0.0.0"
export FERAL_PORT="9090"

bashio::log.info "Starting FERAL Brain (provider: ${LLM_PROVIDER})..."

exec feral serve --bind 0.0.0.0 --serve-port 9090
