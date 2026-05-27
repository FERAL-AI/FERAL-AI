---
id: contributing
title: Contributing
sidebar_position: 10
slug: /contributing
---

# Contributing to FERAL

FERAL is building toward an agent-native operating system. That is a large mission and we need help from many kinds of engineers — backend, frontend, ML, hardware, Nix/packaging, and documentation.

## Development Setup

```bash
git clone https://github.com/FERAL-AI/FERAL-AI.git
cd FERAL-AI/feral-core
pip install -e ".[llm,dev]"
feral setup
feral serve    # brain running on localhost:9090
```

For the web UI (in a separate terminal):

```bash
cd FERAL-AI/feral-client
npm install
npm run dev     # dev server on localhost:5173
```

Or use Make targets from the repo root:

```bash
make dev        # install all deps
make serve      # brain
make client     # web UI dev server
make test       # run pytest
make help       # list all targets
```

## Code Style

### Python

- **Type hints** on all function signatures.
- **Ruff** for linting, **Black**-compatible formatting.
- Keep functions focused and short. Prefer composition over inheritance.
- Avoid broad `except:` blocks — catch specific exceptions.

### TypeScript / React

- **Strict mode** enabled.
- **Prettier** for formatting.
- Functional components with hooks.

### General

- No commented-out code in PRs.
- Comments explain *why*, not *what*.
- New features should include tests.

## Testing

```bash
# Unit + integration tests
pytest

# With coverage report
pytest --cov --cov-report=term

# Target: 70% coverage minimum
```

Test files live in `feral-core/tests/`. Use `pytest-asyncio` for async tests — the `asyncio_mode = "auto"` config handles the event loop.

## Pull Request Process

1. **Fork** the repository and create a feature branch:

   ```bash
   git checkout -b feat/my-feature
   ```

2. **Write tests** for new functionality.

3. **Run the test suite** and ensure it passes:

   ```bash
   pytest
   ```

4. **Open a PR** against `main` with a clear title and description:
   - What changed and why.
   - How to test the change.
   - Screenshots for UI changes.

5. **One approval** from a maintainer is required to merge.

## Contributor Lanes

Pick the area that matches your skills:

| Lane | What You'd Work On | Entry Points |
|:-----|:-------------------|:-------------|
| **Runtime / Orchestrator** | Agent loop, LLM routing, TaskFlows, sessions, security | `feral-core/agents/` |
| **Memory / Knowledge** | 4-tier memory, wiki compilation, ingest pipelines, federated sync | `feral-core/memory/` |
| **GenUI / Provider Surfaces** | SDUI engine, provider contracts, surface caching, client renderer | `feral-core/genui/`, `feral-client/src/components/SduiRenderer.jsx` |
| **Hardware / Daemons** | HUP protocol, daemon SDKs, device profiles, edge bridges | `feral-core/hardware/`, `feral-nodes/` |
| **Voice / Perception** | Realtime voice, Gemini Live, wake word, vision pipeline | `feral-core/voice/`, `feral-core/perception/` |
| **Nix / Packaging** | Flake outputs, NixOS modules, reproducible builds | `flake.nix` |
| **Frontend / Shell** | Web UI, dashboard, Tauri desktop, mobile bridges | `feral-client/`, `desktop/` |
| **Documentation** | Guides, API reference, architecture docs | `docs/` |

## Architecture Docs

Before diving in, read these to understand the system:

- [Architecture Overview](./architecture.md)
- [`docs/GENUI_PROVIDER_SPEC.md`](https://github.com/FERAL-AI/FERAL-AI/blob/main/docs/GENUI_PROVIDER_SPEC.md) — building GenUI provider surfaces
- [`docs/HARDWARE_ECOSYSTEM.md`](https://github.com/FERAL-AI/FERAL-AI/blob/main/docs/HARDWARE_ECOSYSTEM.md) — building hardware daemons
- [`docs/ROADMAP.md`](https://github.com/FERAL-AI/FERAL-AI/blob/main/docs/ROADMAP.md) — strategic execution order
- [Capability status](https://docs.feral.sh/reference/capability-status) — what's available today, what needs operator setup, what's coming

## Reporting Issues

Open an issue at [github.com/FERAL-AI/FERAL-AI/issues](https://github.com/FERAL-AI/FERAL-AI/issues) with:

- Steps to reproduce.
- Expected vs actual behavior.
- Python version, OS, and `feral --version` output.

## Contact

**Alpay Kasal** — [info@feral.io](mailto:info@feral.io)
