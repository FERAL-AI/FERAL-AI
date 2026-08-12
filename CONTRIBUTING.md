# Contributing to FERAL

FERAL is in **public beta** and we are actively looking for contributors. Whether you want to add a new LLM provider, ship a hardware daemon, harden security, or just file good bug reports — every lane below has a clear entry point.

> **Where the project runs today**
> macOS 13+ and modern Linux (Ubuntu 22.04+, Fedora 40+, Arch). Windows is not supported as a host yet — use WSL2 if you must. The CLI ships on PyPI as `feral-ai`.

## Development setup

Prerequisites:

- **Node.js 20+** (for `feral-client-v2`).
- **Git**, **make**, **curl**, a working C toolchain (Xcode CLT on macOS, `build-essential` on Debian-likes).
- No system Python is required. `make dev` fetches the pinned interpreter itself.

Clone and bootstrap:

```bash
git clone https://github.com/FERAL-AI/FERAL-AI.git
cd FERAL-AI
make dev                                 # builds .venv from .python-pin, installs feral-core[all,dev] editable
```

`make dev` builds `.venv/` from the CPython version named in `.python-pin` (3.11.15, from python-build-standalone), installs `feral-core[all,dev]` with `--constraint feral-core/requirements.lock` (the same extras and constraint CI uses, so the local suite and CI agree), and finishes by printing the interpreter's real SQLite feature set:

```
  interpreter : /path/to/.venv/bin/python (Python 3.11.15)
  sqlite      : 3.53.1
  fts5        : OK
  loadable ext: OK
  Environment verified.
```

The pin is not a style preference. FERAL's memory store creates SQLite FTS5 virtual tables during construction, so an interpreter without FTS5 cannot run the brain at all, and `sqlite-vec` needs an interpreter built with `--enable-loadable-sqlite-extensions`. Common interpreters ship one and not the other (pyenv's macOS default has FTS5 but no extensions; python-build-standalone 3.11.13 has extensions but no FTS5). `make dev` fails rather than build an environment where the brain cannot boot.

It needs `uv >= 0.12` and downloads a pinned copy into `.uv/` if your machine's uv is older or absent; your global `uv` is not touched. `make dev-reset` rebuilds `.venv`, `make dev-verify` reprints the report, `make clean-uv` drops the local uv.

Always run Python through `.venv/bin/python` or the `make` targets. This repo deliberately ships **no** `.python-version`: pyenv reads that filename, and a pin it cannot satisfy makes every bare `python3`, `pip`, `pytest` and `ruff` inside the tree resolve to an unrelated interpreter with no error. `make dev` refuses to run if it finds one.

CI runs 3.11 / 3.12 / 3.13; the pin applies to local development and to the desktop bundle.

Or install the published package and run from anywhere:

```bash
pip install "feral-ai[all]"
feral setup                              # interactive wizard: provider, model, network, identity
feral start                              # brain + dashboard + chat
```

The wizard renders an arrow-key picker (space to mark, enter to confirm). API key paste is masked. If your shell does not advertise itself as a TTY (some CI runners, raw `ssh` without `-t`) the wizard prints a typed-fallback hint.

For the web client live dev:

```bash
cd feral-client-v2
npm install
npm run dev
```

Project layout:

| Directory | What it contains |
|:----------|:-----------------|
| `feral-core/` | Python brain runtime — orchestrator, memory, voice, security, GenUI, hardware protocol |
| `feral-client-v2/` | React web UI (Vite + Tailwind), bundled into `feral-core/webui_v2/dist/` for release |
| `feral-nodes/` | Hardware daemon SDKs (Python, iOS Swift, Android Kotlin) + HUP protocol spec |
| `desktop/` | Tauri desktop wrapper |
| `feral-ha-addon/` | Home Assistant add-on packaging |
| `feral-extension/` | Browser extension surface |
| `registry/`, `feral-registry/` | Skill / app marketplace + signing flow |
| `scripts/` | Install, release, sync, audit scripts |
| `docs/` | Architecture, capability status, roadmap |

## Contributor lanes

Pick the lane that matches your interest. Each lane lists the canonical entry files.

### Runtime / orchestrator

Agent loop, LLM routing, multi-agent dispatch, TaskFlows, session lifecycle, security enforcement.

- `feral-core/agents/orchestrator.py`
- `feral-core/agents/multi_agent.py`
- `feral-core/api/server.py`
- `feral-core/security/`

### Memory / knowledge

4-tier memory store, wiki compilation, ingest pipelines, federated sync, knowledge graph.

- `feral-core/memory/store.py`
- `feral-core/memory/sync.py`

### GenUI / provider surfaces

SDUI engine, provider contract lifecycle, surface caching, client renderer, component library.

- `feral-core/genui/generator.py`
- `feral-client-v2/src/components/SduiRenderer.jsx`
- See [`docs/GENUI_PROVIDER_SPEC.md`](docs/GENUI_PROVIDER_SPEC.md) for the contract format.

### Hardware / daemons

Node WebSocket protocol, daemon SDKs, device profiles, edge bridges (BLE, MQTT, serial, ROS).

- `feral-core/hardware/protocol.py`
- `feral-core/hardware/mesh.py`
- `feral-nodes/`
- See [`docs/HARDWARE_ECOSYSTEM.md`](docs/HARDWARE_ECOSYSTEM.md) for the daemon contract.

### Voice / perception

Realtime voice proxy, wake word detection, vision pipeline, multimodal sensor fusion.

- `feral-core/voice/`
- `feral-core/perception/`

### Channels / providers

Telegram, Slack, Discord, Matrix, Signal, voice-call (Twilio), Feishu, Zalo + LLM provider adapters (OpenAI, Anthropic, Ollama, Together, OpenRouter, Fireworks, Bedrock).

- `feral-core/channels/` — see `base.py` (`Channel`) + `telegram.py` for the working exemplar.
- `feral-core/providers/` — see `openai_provider.py` for the working exemplar.

### Frontend / shell

Web UI pages, dashboard, Tauri desktop wrapper, mobile bridges.

- `feral-client-v2/src/`
- `desktop/`

### Packaging / release

Wheel build, version coherence, sync_versions, PyPI publish workflow, Home Assistant add-on, NixOS flake.

- `feral-core/pyproject.toml`
- `scripts/sync_versions.py`
- `.github/workflows/`
- `flake.nix`

## Running tests

```bash
cd feral-core
python -m pytest tests/ -v --no-cov           # backend unit tests
ruff check .                                  # lint
```

```bash
cd feral-client-v2
npm test                                      # vitest
npm run build                                 # vite production bundle
```

CI runs the same commands plus an architecture-boundary check, version-coherence check, and webui_v2 bundled-asset coherence check.

## Code style

- Python: follow existing patterns. Type hints encouraged. `ruff` is the source of truth.
- JavaScript / React: existing JSX patterns, Tailwind for styling.
- Don't add comments that just narrate what the code does. Comments should capture intent, trade-offs, or constraints the code itself cannot convey.

## Commit messages & naming

Public commit messages, PR titles, code comments, and test names are part of the project's permanent record. Keep them professional and self-describing:

- Describe what the change **does** in neutral terms. Avoid framing that implies prior work was fake — no "remove placeholder", "kill the theatre", "no more invented X", "zero fakes", "real product". State the behavior directly (e.g. "report unwired sensors honestly" rather than "kill the fake sensor").
- Don't name third-party / reference projects in shipped artifacts (commit messages, code, docs, tests). Describe what FERAL does on its own terms.
- Don't reference internal-only docs or internal workstream numbering in shipped code comments — they're dangling pointers for anyone outside the team. Capture the intent inline instead.
- Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`) are encouraged.

## Pull requests

- One focused change per PR.
- Reference the contributor lane or area touched.
- Briefly describe what changed and why; call out any new dependency or migration.
- If you add a new extension surface (provider, daemon, skill), include a minimal working example.
- Be honest about limitations. We prefer "X works on macOS, Linux untested" over a silent assumption.

## Reporting bugs

Open an issue at <https://github.com/FERAL-AI/FERAL-AI/issues> with:

- `feral doctor` output.
- Reproduction steps.
- What you expected vs. what happened.
- OS + Python version + `pip show feral-ai` output.

## Key documentation

- [`docs/DEVELOPER_MISSION.md`](docs/DEVELOPER_MISSION.md) — what we are building and why
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system architecture overview
- [`docs/GENUI_PROVIDER_SPEC.md`](docs/GENUI_PROVIDER_SPEC.md) — GenUI provider surface contract
- [`docs/HARDWARE_ECOSYSTEM.md`](docs/HARDWARE_ECOSYSTEM.md) — hardware daemon contract
- [Capability status](https://docs.feral.sh/reference/capability-status) — what's available today, what needs operator setup, what's coming
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — strategic execution order

## Community

- Issues: <https://github.com/FERAL-AI/FERAL-AI/issues>
- Discussions: <https://github.com/FERAL-AI/FERAL-AI/discussions>
- Follow on X: [@FeralAi67724](https://x.com/FeralAi67724)
- Web: <https://feral.sh>

We answer every PR. If a maintainer hasn't responded in 5 days, ping the PR — your message didn't get lost, we just got buried.
