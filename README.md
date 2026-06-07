<p align="center">
  <img src="feral-banner.png" width="640" alt="FERAL" />
</p>

<h3 align="center">One local brain for apps, devices, and memory.</h3>
<p align="center"><em>Install FERAL on your machine. It connects software and hardware, keeps long-lived memory, learns your baseline, and executes with explicit control.</em></p>

<p align="center">
  <strong>🚧 Public beta — macOS &amp; Linux supported. We are looking for contributors:</strong>
  <a href="CONTRIBUTING.md">CONTRIBUTING.md</a> · <a href="https://github.com/FERAL-AI/FERAL-AI/issues">file an issue</a> · <a href="https://x.com/FeralAi67724">@FeralAi67724</a>
</p>

<p align="center">
  <a href="#quickstart-pypi-first">Quickstart</a> &nbsp;·&nbsp;
  <a href="#pair-your-phone-lan-vs-anywhere">Pairing</a> &nbsp;·&nbsp;
  <a href="#what-gen-ui-actually-does">Gen-UI</a> &nbsp;·&nbsp;
  <a href="#stable-today">Stability</a> &nbsp;·&nbsp;
  <a href="#develop-from-source">Develop</a> &nbsp;·&nbsp;
  <a href="#contribute">Contribute</a>
</p>

<p align="center">
  <!-- sync-versions:badge -->
  <img src="https://img.shields.io/badge/version-2026.6.6-06b6d4?style=flat-square" alt="Version" />
  <!-- /sync-versions:badge -->
  <a href="https://github.com/FERAL-AI/FERAL-AI/stargazers"><img src="https://img.shields.io/github/stars/FERAL-AI/FERAL-AI?style=flat-square&color=06b6d4" alt="Stars" /></a>
  <a href="https://github.com/FERAL-AI/FERAL-AI/commits/main"><img src="https://img.shields.io/github/last-commit/FERAL-AI/FERAL-AI?style=flat-square&color=06b6d4" alt="Last Commit" /></a>
  <img src="https://img.shields.io/badge/license-Apache%202.0-06b6d4?style=flat-square" alt="License" />
  <img src="https://img.shields.io/badge/python-3.11+-06b6d4?style=flat-square" alt="Python" />
</p>

---

## What FERAL Is

FERAL is a local-first brain that sits in the middle of your software and physical devices. You run it on your own machine and connect apps, channels, and hardware through one runtime.

Core model:

- 4-layer memory: working context, episodic events, semantic/graph retrieval, and execution history.
- Baseline learning: rolling metrics and anomaly/trend detection for what "normal" looks like for you.
- Digital twin actions: policy-gated autonomy with approval, time-window, and daily-cap controls.
- Publisher model: developers ship headless API/CLI contracts and app manifests; FERAL renders structured Gen-UI surfaces locally.
- Registry review gate: submissions are not user-installable until approved by FERAL org reviewers.

Today this ships as `feral-core` (brain runtime), `feral-client-v2` (web control surface), and `feral-nodes` (device/node bridges).

## Status

- Package: [`feral-ai`](https://pypi.org/project/feral-ai/) on PyPI. Current CalVer is shown in the version badge above and tracks `feral-core/pyproject.toml`.
- Maturity: **Public beta**. Single-user local deployment is the primary target. Multi-user / HA scenarios are not in scope yet.
- Supported hosts: **macOS 13+** and **modern Linux** (Ubuntu 22.04+, Fedora 40+, Arch). Windows is not supported yet — use WSL2.
- Default startup mode: "This Mac only" pairing until you opt into LAN or Anywhere.

## Quickstart (PyPI first)

Requires Python 3.11+.

```bash
pip install "feral-ai[all]"
feral setup
feral start
```

`feral setup` is an arrow-key driven wizard (space to mark, enter to confirm). It walks you through:

1. **LLM provider** (OpenAI, Anthropic, Ollama, LM Studio, Together, OpenRouter, Fireworks, Bedrock, …) with masked API key paste.
2. **Model** (type to filter through hundreds of model ids).
3. **Speech in / out** (cloud or fully local).
4. **Identity** (so the agent knows who it is talking to).
5. **Network access** — `localhost` (default), `LAN` (`0.0.0.0` so phones on the same Wi-Fi can pair), or `Tailscale Funnel` (free public DNS for cross-internet pairing).
6. **Optional**: Home Assistant, messaging channels.

Then open `http://localhost:9090`.

What this gives you:

- A local brain server on port `9090`.
- Bundled Web UI v2 served by the brain.
- Local config under `~/.feral/` (settings + encrypted vault for keys).

Useful commands:

```bash
feral serve            # headless brain only (no chat / no client)
feral status           # runtime status
feral doctor           # diagnostics — what's reachable, what needs setup
feral access status    # current pairing / network mode
feral key paste        # add or rotate a credential without re-running setup
feral memory status    # backend, KG entity/relation counts, decay schedule
feral sync status      # federated peers + per-peer lag + last-sync HLC
```

If you prefer the installer script:

```bash
curl -sSL https://raw.githubusercontent.com/FERAL-AI/FERAL-AI/main/scripts/install.sh | bash
source ~/.feral-env/bin/activate
feral start
```

## Pair Your Phone: LAN vs Anywhere

FERAL exposes three pairing modes:

| Mode | UI label | Best for | Requirement |
|---|---|---|---|
| `local` | Same WiFi | Phone and brain on same network | Brain must be reachable on LAN |
| `remote` | Anywhere | Pair/use from outside your LAN | Tailscale installed and Funnel enabled |
| `localhost` | This Mac only | No phone pairing yet | No extra setup |

### LAN (Same WiFi)

1. In setup, choose **Same WiFi**.
2. Open `Devices` -> `Pair new device` -> `Web phone`.
3. Click **Generate one-time link** and scan the QR from your phone.
4. If PIN is enabled, enter the 4-digit PIN shown on the Mac.

If the generated LAN URL is unreachable from your phone, restart the brain on all interfaces:

```bash
FERAL_HOST=0.0.0.0 feral start
```

### Anywhere (Remote via Tailscale)

Setup now attempts this automatically when you choose **Anywhere**.
You can also manage it later in `Settings` -> `Access`.

1. In setup, choose **Anywhere**.
2. If setup reports a tunnel error, run:

```bash
feral access remote-up
```

3. Complete any Tailscale prompts (`tailscale up`, Funnel enable URL) if requested.
4. Generate a new pairing link from `Devices` and scan it from anywhere.

Check status any time:

```bash
feral access status
```

Disable remote mode:

```bash
feral access remote-down
```

### This Mac only

Use this if you want local dashboard/chat without phone pairing yet.

## What Gen-UI Actually Does

Gen-UI in FERAL is server-driven UI (SDUI), not freeform frontend generation.

- The brain emits structured UI payloads; the client renders known component types.
- Payload updates can be streamed as `sdui_patch` deltas.
- Third-party app surfaces run in a sandboxed model with explicit contracts.
- The `/canvas` view is a live inspector/debug surface for SDUI frames.

What it is not yet:

- Not a native iOS/Android SDUI renderer parity story.
- Not a fully signed marketplace trust model end-to-end.

## Stable Today

<!-- sync-versions:test-counts pytest=4723 vitest=403 -->
Current CI snapshot: **3722 backend + 312 frontend tests**.
<!-- /sync-versions:test-counts -->

| Area | Current state |
|---|---|
| Chat and LLM orchestration | Stable |
| Memory core (episodic/semantic/graph) | Stable |
| Setup + CLI runtime control | Stable |
| Web UI v2 core flows | Stable |
| Pairing lifecycle (token, claim, prune) | Stable |
| Voice, channels, and integrations | Stable with provider/runtime dependencies |
| Gen-UI advanced app-platform features | Mixed (stable core renderer, evolving platform contracts) |
| Long-tail ecosystem claims | Vary by integration; verify before production commitments |

## Recent Release Focus

For the full per-release breakdown see [`CHANGELOG.md`](CHANGELOG.md). Highlights from the last few releases:

- **`v2026.5.36`** — `feral doctor` honesty + first-run pairing dependency closure. A clean install used to produce ~5 yellow warnings and 1 red failure from `feral doctor` for things that are *expected* to be absent on a fresh machine (Chrome CDP not running, Local STT/TTS not installed, no voice key, no workspace grants, PyObjC ApplicationServices missing). v2026.5.36 introduces a fourth `_info` severity tier for "not configured yet" / "opt-in" probes, demotes the six false-positive warnings into it, and closes two real packaging gaps: PyObjC ApplicationServices + Quartz are now base dependencies on Darwin (so TCC probes return real `granted`/`denied`), and `qrcode[pil]` moves from the `[discovery]`/`[all]` extras into base deps so the first-run "pair a device" path doesn't 500 on bare installs.
- **`v2026.5.35`** — F1 unified knowledge graph. `MemoryStore.knowledge_store/query/search/about` route through the entity-relation `KnowledgeGraph` when `settings.memory.kg.unified` is true (default). The flat `knowledge` table is migrated on boot and renamed to `knowledge__deprecated`; HLC LWW sync is extended to the `entities` and `relations` tables so KG-native writes replicate across federated brains. `Learner.extract_knowledge` delegates to `KnowledgeGraph.extract_and_store` — one extraction surface, one prompt, entity types preserved.
- **`v2026.5.34`** — Memory v2 truth. Ebbinghaus decay (D11) runs an hourly background sweep that recomputes `decay_factor` per episode with an SM-2 access boost and marks rows below the forget threshold as forgotten; HLC last-write-wins federated sync (D12) lets multiple brains reconcile via a deterministic per-row id without coordination; real session compaction (F2) promotes summarisable turns into episode rows with structured `participants` / `time_range` / `key_entities` / `source_turn_ids` metadata, triggered end-of-session or every `turns_threshold` turns. `feral memory` and `feral sync` subcommands surface all of it.
- **`v2026.5.33`** — Async-native `MemoryStore` (Option C) on `aiosqlite` with a pre-warmed connection pool in WAL mode. Pure refactor, zero behaviour change. Per-call p50 search latency drops 42% and aggregate wall-clock under K=32 concurrent searches drops 53% versus the legacy `sqlite3.connect`-per-call path. Reference numbers and acceptance benchmark in `feral-core/tests/perf/test_memory_latency.py`.
- **`v2026.5.32`** — Audit-r12 systemic remediation: phone-bearer allowlist coherence (D1), HUP wire-version coherence with a static-AST guard (D3), real `settings.memory.backend` selector with sqlite-vec / Chroma / Qdrant adapters (D4), MCP HTTP transport implementation against spec rev `2025-06-18` (D6), canonical `MCPServerConfig` + `MCPClientManager.connect_server` API (D7), Bedrock Converse `chat()` and streaming path (D8), and Whoop / Oura OAuth registration with a static-AST coherence guard (D9).
- **`v2026.5.23`** — fixes a P0 in `feral setup` where the InquirerPy arrow-key picker silently fell back to a typed numeric prompt because the wizard ran inside `asyncio.run()`. Adds the raccoon ASCII logo banner, "── Step N of M ──" indicators, and the space-to-mark + enter-to-confirm picker pattern.

## Architecture in 60 Seconds

```mermaid
flowchart TB
    subgraph brain [FERAL Brain - feral-core]
        ORCH["Orchestrator + Supervisor"]
        MEM["Memory + Retrieval"]
        GEN["Gen-UI / SDUI"]
        DEV["Device + Pairing APIs"]
        CH["Channels + Integrations"]
    end

    subgraph clients [Clients]
        WEB["Web UI v2"]
        CLI["CLI"]
        PHN["Phone pair page / bridges"]
    end

    subgraph nodes [Nodes]
        HUP["HUP daemons and hardware bridges"]
    end

    clients <--> brain
    nodes <--> brain
```

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

Run the test suite locally:

```bash
cd feral-core && python -m pytest tests/ --no-cov -q
cd ../feral-client-v2 && npm test
```

## Docs

- User docs: `docs/mintlify/` (also published at <https://docs.feral.sh>)
- Architecture deep dive: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/orchestration.md`](docs/orchestration.md)
- Capability status: <https://docs.feral.sh/reference/capability-status>
- Roadmap: [`docs/ROADMAP.md`](docs/ROADMAP.md)
- Contribution guide: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Security policy: [`SECURITY.md`](SECURITY.md)

## Contribute

FERAL is **public beta** and we are actively looking for contributors across every layer:

- **Runtime / orchestrator** — agent loop, LLM routing, multi-agent dispatch, security enforcement.
- **Memory / knowledge** — 4-tier memory store, ingest pipelines, knowledge graph.
- **GenUI / provider surfaces** — SDUI engine, third-party app contracts, client renderer.
- **Hardware / daemons** — Node WebSocket protocol, BLE / MQTT / serial / ROS bridges.
- **Voice / perception** — realtime voice proxy, wake word, vision pipeline.
- **Channels / providers** — Telegram, Slack, Discord, Matrix, Signal, Feishu, Zalo + LLM provider adapters.
- **Frontend / shell** — web UI, Tauri desktop wrapper, mobile bridges.
- **Packaging / release** — wheel build, version coherence, NixOS flake, HA add-on.

How to start:

1. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) — picks the lane that matches your interest and lists the canonical entry files.
2. Browse [open issues](https://github.com/FERAL-AI/FERAL-AI/issues) or open a new one with `feral doctor` output + repro.
3. Join the conversation on [GitHub Discussions](https://github.com/FERAL-AI/FERAL-AI/discussions).
4. Follow [@FeralAi67724](https://x.com/FeralAi67724) on X for release drops.

The website ([feral.sh](https://feral.sh), source: [FERAL-AI/Feral-web](https://github.com/FERAL-AI/Feral-web)) is also open and welcomes design + copy + accessibility PRs.

## What FERAL Is Not

- Not a managed cloud service.
- Not a guaranteed multi-tenant/high-availability platform today.
- Not a claim that every listed integration is equal maturity in every environment.

## Created By

**[Mahmoud Omar](https://github.com/mahmoudomar)** and **[Alpay Kasal](https://github.com/alpaykasal)**

Contact: [info@feral.sh](mailto:info@feral.sh) | Website: [feral.sh](https://feral.sh) | GitHub: [FERAL-AI](https://github.com/FERAL-AI)
