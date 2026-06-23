# FERAL Core (`feral-ai`)

FERAL Core is the Python runtime ("brain") for FERAL.

It provides:

- FastAPI + WebSocket orchestration runtime
- Tool, memory, and routing services
- Pairing + device access APIs
- Bundled `webui_v2` static client assets for dashboard startup

## Install

```bash
pip install "feral-ai[all]"
```

## Start

```bash
feral setup
feral start
```

Then open `http://localhost:9090`.

## Docs

- Main repo README: `../README.md`
- Mintlify docs: `../docs/mintlify`
- Hardware / HUP: `../docs/HARDWARE_ECOSYSTEM.md`, `../docs/mintlify/guides/hardware.mdx`
- Self-describing wire format: `hardware/protocol.py` (`HUP_ACTION_SCHEMA`, `device_manifest_from_capabilities`)
