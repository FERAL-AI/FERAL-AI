---
id: deployment
title: Deployment & Operations
sidebar_position: 3
slug: /deployment
---

# Deployment & Operations

This guide covers running FERAL in production — Docker Compose, environment variables, systemd services, reverse proxy, and a production checklist.

## Docker Compose

The recommended production setup uses Docker Compose to run the Brain, web UI, and optional services.

```yaml
# docker-compose.yml
version: "3.9"

services:
  brain:
    image: ghcr.io/feral-ai/feral-brain:latest
    ports:
      - "9090:9090"
    volumes:
      - feral-data:/data
      - ./config:/config:ro
    env_file: .env
    environment:
      FERAL_DATA_DIR: /data
      FERAL_CONFIG_DIR: /config
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9090/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
    restart: unless-stopped

volumes:
  feral-data:
  redis-data:
```

```bash
cp .env.example .env    # fill in API keys
docker compose up -d
docker compose logs -f brain
```

## Environment Variables

| Variable | Required | Default | Description |
|:---------|:---------|:--------|:------------|
| `FERAL_LLM_PROVIDER` | yes | — | `openai`, `anthropic`, `gemini`, `groq`, `ollama` |
| `OPENAI_API_KEY` | if openai | — | OpenAI API key |
| `ANTHROPIC_API_KEY` | if anthropic | — | Anthropic API key |
| `GEMINI_API_KEY` | if gemini | — | Google AI API key |
| `GROQ_API_KEY` | if groq | — | Groq API key |
| `OLLAMA_BASE_URL` | if ollama | `http://localhost:11434` | Ollama endpoint |
| `FERAL_DATA_DIR` | no | `~/.feral` | Data directory |
| `FERAL_CONFIG_DIR` | no | `~/.feral` | Config directory |
| `FERAL_PORT` | no | `9090` | Brain HTTP/WS port |
| `FERAL_HOST` | no | `0.0.0.0` | Bind address |
| `FERAL_AUTONOMY` | no | `hybrid` | `strict`, `hybrid`, `loose` |
| `FERAL_LOG_LEVEL` | no | `info` | `debug`, `info`, `warning`, `error` |
| `FERAL_VOICE_MODE` | no | `disabled` | `realtime`, `whisper`, `disabled` |
| `FERAL_CORS_ORIGINS` | no | `*` | Comma-separated allowed origins |
| `TELEGRAM_BOT_TOKEN` | no | — | Telegram channel token |
| `SLACK_BOT_TOKEN` | no | — | Slack bot token |
| `SLACK_APP_TOKEN` | no | — | Slack app-level token |
| `DISCORD_BOT_TOKEN` | no | — | Discord bot token |
| `REDIS_URL` | no | — | Redis URL for caching/pubsub |

## Production Checklist

Before going live:

- [ ] Set `FERAL_AUTONOMY=strict` or `hybrid` (never `loose` in multi-user)
- [ ] Store all API keys via `feral key add` or env vars — never in config files
- [ ] Set `FERAL_CORS_ORIGINS` to your actual domains
- [ ] Enable HTTPS via reverse proxy (see below)
- [ ] Set `FERAL_LOG_LEVEL=warning` to reduce log volume
- [ ] Mount `feral-data` on persistent storage with backups
- [ ] Set resource limits on Docker containers
- [ ] Enable health checks for orchestrator restarts
- [ ] Review the [Security Model](./guides/security.md) and configure a SandboxPolicy
- [ ] Test channel integrations (Telegram, Slack) in a staging environment first

## Systemd Service

For bare-metal deployments without Docker:

```ini
# /etc/systemd/system/feral.service
[Unit]
Description=FERAL AI Brain
After=network.target

[Service]
Type=exec
User=feral
Group=feral
WorkingDirectory=/opt/feral
EnvironmentFile=/opt/feral/.env
ExecStart=/opt/feral/venv/bin/feral start --host 127.0.0.1 --port 9090
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/opt/feral/data
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now feral
sudo journalctl -u feral -f
```

## Reverse Proxy

### Nginx

```nginx
# /etc/nginx/sites-available/feral
upstream feral_brain {
    server 127.0.0.1:9090;
}

server {
    listen 443 ssl http2;
    server_name feral.example.com;

    ssl_certificate     /etc/letsencrypt/live/feral.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/feral.example.com/privkey.pem;

    location / {
        proxy_pass http://feral_brain;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket endpoints
    location ~ ^/(v1/session|v1/node|sync) {
        proxy_pass http://feral_brain;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }
}
```

### Caddy

```
feral.example.com {
    reverse_proxy localhost:9090
}
```

Caddy handles TLS automatically via Let's Encrypt and upgrades WebSocket connections without extra configuration.

### Trusted proxy browser authentication

FERAL's trusted reverse-proxy browser authentication is disabled by default.
When enabled, the backend accepts the proxy assertion only when the exact
socket peer is in `FERAL_PROXY_AUTH_TRUSTED_PROXIES`; forwarded client-IP
headers never create trust. Configure a shared assertion secret and canonical
allowed origins. Optional user and group allowlists can further restrict
access. A group assertion may contain multiple groups separated by `|` (or by
the configured separator).

The reverse proxy must authenticate the browser first, remove any client-
supplied copies of the assertion and identity headers, and then inject fresh
identity headers. Never expose `FERAL_API_KEY` or
`FERAL_PROXY_AUTH_SECRET` to browser code. Keep WebSocket upgrade forwarding
enabled for `/v1/session`.

Set these variables in the backend environment:

| Variable | Default | Description |
|:---------|:--------|:------------|
| `FERAL_PROXY_AUTH_ENABLED` | `false` | Enable trusted-proxy browser authentication. |
| `FERAL_PROXY_AUTH_TRUSTED_PROXIES` | — | Comma-separated trusted socket-peer IPs or CIDR ranges. |
| `FERAL_PROXY_AUTH_SECRET` | — | Server-side shared assertion secret. |
| `FERAL_PROXY_AUTH_SECRET_HEADER` | `X-FERAL-Proxy-Secret` | Assertion header name. |
| `FERAL_PROXY_AUTH_IDENTITY_HEADER` | `X-FERAL-Proxy-User` | Authenticated user header name. |
| `FERAL_PROXY_AUTH_GROUPS_HEADER` | `X-FERAL-Proxy-Groups` | Group header name. |
| `FERAL_PROXY_AUTH_GROUPS_SEPARATOR` | `\|` | Single-character group separator; defaults to `|`. |
| `FERAL_PROXY_AUTH_ALLOWED_USERS` | — | Optional comma-separated user allowlist. |
| `FERAL_PROXY_AUTH_ALLOWED_GROUPS` | — | Optional comma-separated group allowlist; any intersection is accepted. |
| `FERAL_PROXY_AUTH_ALLOWED_ORIGINS` | — | Required comma-separated canonical browser origins. |

Use placeholders in proxy configuration and keep the backend listener off the
public Internet. A private direct route may remain for API-key, session-token,
or phone-bearer clients, but it must not accept proxy identity from any peer
outside `FERAL_PROXY_AUTH_TRUSTED_PROXIES`. A generic proxy needs to pass the
configured assertion headers only after its own auth check and retain the
WebSocket `Upgrade`/`Connection` headers.

This feature adds an authentication method to protected FERAL routes; it does
not redefine intentionally open pairing, health, or signed-webhook endpoints.
Apply the proxy's own access policy to the whole operator hostname unless a
specific bootstrap or webhook endpoint is deliberately public.

## Monitoring

FERAL exposes a health endpoint and optional Prometheus metrics:

```bash
# Health check
curl http://localhost:9090/health
# {"status": "healthy", "uptime_seconds": 3600, "active_sessions": 2}

# Prometheus metrics (if enabled)
curl http://localhost:9090/metrics
```

Enable metrics in config:

```json
// ~/.feral/settings.json — "monitoring" section
{
  "monitoring": {
    "prometheus": true,
    "port": 9091
  }
}
```

Key metrics exported:

| Metric | Type | Description |
|:-------|:-----|:------------|
| `feral_requests_total` | counter | Total API requests by endpoint |
| `feral_llm_latency_seconds` | histogram | LLM call duration |
| `feral_tool_executions_total` | counter | Tool calls by name and status |
| `feral_active_sessions` | gauge | Current open sessions |
| `feral_memory_entries` | gauge | Memory entries by tier |
| `feral_device_connections` | gauge | Connected HUP devices |

## Backup & Restore

FERAL stores all per-user state under `~/.feral/` — back it up like any other directory. There is no dedicated CLI verb; standard `tar`/`rsync` is the supported path.

```bash
# Backup memory and config (stop the brain first for a consistent snapshot)
feral stop
tar czf /backups/feral-$(date +%F).tar.gz -C ~/.feral .
feral start

# Restore (also brain-offline)
feral stop
tar xzf /backups/feral-2025-06-15.tar.gz -C ~/.feral
feral start

# Automated daily backup (cron) — uses --skip-checkpoint snapshots
0 3 * * * tar czf /backups/feral-$(date +\%F).tar.gz -C ~/.feral .
```

The directory includes `memory.db` (or `memory.db.enc` if you ran `feral memory encrypt`), `settings.json`, `USER.md`, `SOUL.md`, `credentials.enc` (ChaCha20-Poly1305 encrypted vault), wiki pages, and skill manifests.
