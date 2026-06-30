<p align="center">
  <img src="feral-banner.png" width="640" alt="FERAL" />
</p>

<h3 align="center">One local brain for your apps, devices, and memory.</h3>
<p align="center"><em>FERAL runs on your machine. It connects software and hardware, keeps long-lived memory, learns your baseline, and executes with explicit control.</em></p>

<p align="center">
  <strong>Public beta — macOS &amp; Linux.</strong>
  <a href="CONTRIBUTING.md">Contribute</a> ·
  <a href="https://github.com/FERAL-AI/FERAL-AI/issues">File an issue</a> ·
  <a href="https://x.com/FeralAi67724">@FeralAi67724</a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &nbsp;·&nbsp;
  <a href="#pair-your-phone">Pairing</a> &nbsp;·&nbsp;
  <a href="#what-works-today">What Works</a> &nbsp;·&nbsp;
  <a href="#architecture">Architecture</a> &nbsp;·&nbsp;
  <a href="#develop-from-source">Develop</a> &nbsp;·&nbsp;
  <a href="#contribute">Contribute</a>
</p>

<p align="center">
  <!-- sync-versions:badge -->
  <img src="https://img.shields.io/badge/version-2026.6.21-06b6d4?style=flat-square" alt="Version" />
  <!-- /sync-versions:badge -->
  <a href="https://github.com/FERAL-AI/FERAL-AI/stargazers"><img src="https://img.shields.io/github/stars/FERAL-AI/FERAL-AI?style=flat-square&color=06b6d4" alt="Stars" /></a>
  <a href="https://github.com/FERAL-AI/FERAL-AI/commits/main"><img src="https://img.shields.io/github/last-commit/FERAL-AI/FERAL-AI?style=flat-square&color=06b6d4" alt="Last Commit" /></a>
  <img src="https://img.shields.io/badge/license-Apache%202.0-06b6d4?style=flat-square" alt="License" />
  <img src="https://img.shields.io/badge/python-3.11+-06b6d4?style=flat-square" alt="Python" />
</p>

---

## What FERAL Is

FERAL is a local-first runtime that sits between your software and your physical devices. You run it on your own machine and reach it from a browser, a phone, the CLI, or your own integrations.

What's inside:

- **4-layer memory** — working context, episodic events, semantic / graph retrieval, and execution history.
- **Baseline learning** — rolling metrics and anomaly / trend detection for what "normal" looks like for you.
- **Policy-gated execution** — approvals, time windows, and daily caps gate every action that touches the outside world.
- **Server-driven UI (Gen-UI)** — the brain emits structured UI payloads; clients render them. Third-party apps ship contracts, not freeform frontends.
- **Reviewed extension registry** — community skills and apps go through reviewer approval before they're installable.

It ships as `feral-core` (the brain runtime), `feral-client-v2` (web control surface), and `feral-nodes` (device and hardware bridges).

## Quick Start

> **Requires Python 3.11+** on macOS 13+ or modern Linux (Ubuntu 22.04+, Fedora 40+, Arch). Windows is not supported as a host yet — use WSL2.

### Recommended: one-line install

```bash
curl -sSL https://raw.githubusercontent.com/FERAL-AI/FERAL-AI/main/scripts/install.sh | bash
```

This creates a virtualenv at `~/.feral-env`, installs `feral-ai` with all extras, runs the first-run setup wizard, and starts the brain. When it's done, open <http://localhost:9090>.

### Alternative: install from PyPI

```bash
pip install "feral-ai[all]"
feral setup
feral start
```

### What `feral setup` walks you through

`feral setup` is an arrow-key wizard (space to mark, enter to confirm):

1. **LLM provider** — OpenAI, Anthropic, Ollama, LM Studio, Together, OpenRouter, Fireworks, Bedrock, and more — with masked API key paste.
2. **Model** — type to filter through hundreds of model ids.
3. **Speech in / out** — cloud or fully local.
4. **Identity** — so the agent knows who it's talking to.
5. **Network access** — `localhost` (default), `LAN` so phones on the same Wi-Fi can pair, or `Tailscale Funnel` for free public DNS pairing from anywhere.
6. **Optional integrations** — Home Assistant, messaging channels.

You get a local brain on port `9090`, the bundled web UI, and a local config under `~/.feral/` (settings + an encrypted vault for keys).

### Useful CLI commands

```bash
feral serve            # headless brain only (no chat / no client)
feral status           # runtime status
feral doctor           # diagnostics — what's reachable, what needs setup
feral access status    # current pairing / network mode
feral key paste        # add or rotate a credential without re-running setup
feral memory status    # backend, knowledge graph counts, decay schedule
feral sync status      # federated peers + per-peer lag + last-sync clock
```

## Pair Your Phone

FERAL exposes three pairing modes:

| Mode | UI label | Best for | Requirement |
|---|---|---|---|
| `localhost` | This Mac only | No phone pairing yet | None |
| `local` | Same WiFi | Phone and brain on the same network | Brain reachable on LAN |
| `remote` | Anywhere | Pair from outside your LAN | Tailscale installed, Funnel enabled |

### Same WiFi

1. In setup, choose **Same WiFi**.
2. Open `Devices` → `Pair new device` → `Web phone`.
3. Click **Generate one-time link** and scan the QR from your phone.
4. If PIN is enabled, enter the 4-digit PIN shown on the Mac.

If the LAN URL is unreachable from your phone, restart the brain on all interfaces:

```bash
FERAL_HOST=0.0.0.0 feral start
```

### Anywhere (Tailscale)

Setup attempts this automatically when you choose **Anywhere**. You can also manage it later in `Settings` → `Access`.

1. In setup, choose **Anywhere**.
2. If setup reports a tunnel error, run:
   ```bash
   feral access remote-up
   ```
3. Complete any Tailscale prompts (`tailscale up`, Funnel enable URL).
4. Generate a new pairing link from `Devices` and scan it from anywhere.

Check status any time: `feral access status`. Disable with `feral access remote-down`.

### This Mac only

Use this if you just want the local dashboard and chat, no phone yet.

## What Works Today

FERAL is in **public beta**. Single-user local deployment is the primary target — multi-user / HA is not in scope yet. Here's what an operator can rely on, in plain language:

- **Chat, memory, and orchestration** — the core agent loop, the 4-layer memory store, and federated sync between brains are stable for day-to-day use.
- **Setup and CLI control** — the wizard, `feral start`, `feral doctor`, `feral memory`, `feral sync`, and `feral access` reflect real runtime state, not aspirational claims.
- **Web UI v2 core flows** — chat, devices, pairing, settings, and the SDUI inspector are stable.
- **Pairing lifecycle** — token issue, claim, expiry, and prune are stable.
- **Voice, channels, integrations** — usable, but their availability depends on the provider and runtime you've configured (some need keys, some need a local model, some need OAuth).
- **Gen-UI app platform** — the core renderer is stable; the third-party contract surface is still evolving.

For longer-tail integrations: try them, run `feral doctor`, and verify before depending on one in production.

Detailed history of every shipped change is in [`CHANGELOG.md`](CHANGELOG.md).

## Gen-UI in One Paragraph

Gen-UI in FERAL is **server-driven UI** (SDUI), not freeform frontend generation. The brain emits structured UI payloads; the client renders known component types. Payload updates stream as `sdui_patch` deltas. Third-party app surfaces run in a sandboxed model with explicit contracts. The `/canvas` view is a live inspector for SDUI frames. What it is *not* yet: native iOS / Android SDUI renderer parity, or a signed-marketplace trust model end to end.

## Architecture

```mermaid
flowchart LR
    USER([You]) --> CLIENT[Web UI · CLI · Phone]
    CLIENT <--> BRAIN[FERAL Brain<br/>orchestrator · memory · Gen-UI · policy]
    BRAIN <--> NODES[Devices · sensors · daemons]
    BRAIN <--> EXT[LLMs · channels · integrations]
```

- **Brain (`feral-core`)** — Python runtime: orchestrator, 4-layer memory, Gen-UI generator, pairing and policy, channel adapters, LLM router.
- **Clients (`feral-client-v2`, CLI, phone bridges)** — render the brain's SDUI surfaces and stream input back.
- **Nodes (`feral-nodes`)** — hardware daemons over a JSON WebSocket protocol (BLE / MQTT / serial / ROS bridges register their capabilities at connect time).
- **External** — LLM providers, messaging channels, OAuth integrations. All policy-gated.

Deeper reading: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/orchestration.md`](docs/orchestration.md).

## Develop From Source

```bash
git clone https://github.com/FERAL-AI/FERAL-AI.git
cd FERAL-AI
make dev

# brain (headless)
feral serve

# web client v2 (optional live dev)
cd feral-client-v2
npm run dev
```

Run the tests locally:

```bash
cd feral-core && python -m pytest tests/ --no-cov -q
cd ../feral-client-v2 && npm test
```

## Docs

- User docs: `docs/mintlify/` (published at <https://docs.feral.sh>)
- Architecture deep dive: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/orchestration.md`](docs/orchestration.md)
- Capability status: <https://docs.feral.sh/reference/capability-status>
- Roadmap: [`docs/ROADMAP.md`](docs/ROADMAP.md)
- Contribution guide: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Security policy: [`SECURITY.md`](SECURITY.md)
- Release history: [`CHANGELOG.md`](CHANGELOG.md)

## Contribute

FERAL is **public beta** and welcomes contributors at every layer:

- **Runtime / orchestrator** — agent loop, LLM routing, multi-agent dispatch, policy enforcement.
- **Memory / knowledge** — 4-tier memory store, ingest pipelines, knowledge graph.
- **Gen-UI / provider surfaces** — SDUI engine, third-party app contracts, client renderer.
- **Hardware / daemons** — Node WebSocket protocol, BLE / MQTT / serial / ROS bridges.
- **Voice / perception** — realtime voice proxy, wake word, vision pipeline.
- **Channels / providers** — Telegram, Slack, Discord, Matrix, Signal, Feishu, Zalo, plus LLM provider adapters.
- **Frontend / shell** — web UI, Tauri desktop wrapper, mobile bridges.
- **Packaging / release** — wheel build, version coherence, NixOS flake, Home Assistant add-on.

How to start:

1. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) — it picks the lane that matches your interest and lists canonical entry files.
2. Browse [open issues](https://github.com/FERAL-AI/FERAL-AI/issues) or open a new one with `feral doctor` output and a repro.
3. Join the conversation on [GitHub Discussions](https://github.com/FERAL-AI/FERAL-AI/discussions).
4. Follow [@FeralAi67724](https://x.com/FeralAi67724) on X for release notes.

The website ([feral.sh](https://feral.sh), source: [FERAL-AI/Feral-web](https://github.com/FERAL-AI/Feral-web)) is also open and welcomes design, copy, and accessibility PRs.

## What FERAL Is Not

- Not a managed cloud service.
- Not a multi-tenant / high-availability platform today.
- Not a claim that every listed integration is equally mature in every environment.

## Maintainers

- **[Mahmoud Omar](https://github.com/mahmoudomarus)** — founder and primary maintainer.
- **[Alpay Kasal](https://github.com/alpaykasal)** — co-founder, commercial and partnerships.

Contact: [info@feral.sh](mailto:info@feral.sh) · Website: [feral.sh](https://feral.sh) · GitHub: [FERAL-AI](https://github.com/FERAL-AI).

## License

Apache License 2.0 — see [`LICENSE`](LICENSE). Attribution requirements live in [`NOTICE`](NOTICE).

<!--
  Internal sync marker used by .github/workflows/version-coherence.yml to
  reconcile live pytest / vitest counts. Not for human eyes — please leave
  it in place and do not edit by hand.
-->
<!-- sync-versions:test-counts pytest=5058 vitest=410 -->
<!-- /sync-versions:test-counts -->
