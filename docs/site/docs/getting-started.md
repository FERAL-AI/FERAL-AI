---
id: getting-started
title: Getting Started
sidebar_position: 1
slug: /getting-started
---

# Quick Start

```bash
# Install
pip install "feral-ai[llm]"

# Start (opens browser, runs setup wizard on first run)
feral start
```

That's it. The Brain starts on `http://localhost:9090` with the web dashboard bundled.

## What Happens

1. `feral start` detects first run and launches the setup wizard.
2. You pick an LLM provider (OpenAI, Anthropic, Ollama, etc.) and enter your API key.
3. The Brain starts on port 9090 with the web dashboard.
4. Your browser opens automatically.

No API key is required for local models — Ollama on `localhost:11434` is auto-detected.

## First Chat (CLI)

You can also talk to FERAL from the terminal:

```bash
feral "What files are in my home directory?"
feral "Search the web for latest AI news"
feral "Remember that my favorite color is blue"
feral "What's my favorite color?"
```

## First Chat (SDK)

```python
from feral_sdk import FeralClient

async with FeralClient("http://localhost:9090") as client:
    reply = await client.chat("Hello! What can you do?")
    print(reply)
```

## Development Mode

If you're developing FERAL itself:

```bash
git clone https://github.com/FERAL-AI/FERAL-AI.git
cd FERAL-AI
make dev           # installs both brain + client
feral serve        # brain on :9090 (headless)
cd feral-client-v2 && npm run dev  # Vite on :5173
```

## Docker Alternative

```bash
git clone https://github.com/FERAL-AI/FERAL-AI.git && cd FERAL-AI
cp .env.example .env   # fill in your API keys
docker compose up -d
```

| Service | URL |
|:--------|:----|
| Brain + Web UI | http://localhost:9090 |

## What's Next

- [Architecture](./architecture.md) — understand how the brain, memory, and device mesh fit together
- [Python SDK](./sdk/python.md) — build plugins and automate FERAL programmatically
- [Write a Skill](./guides/skills.md) — add new capabilities to your agent
- [Connect a Device](./guides/devices.md) — bring hardware into the mesh
