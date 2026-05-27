# FERAL Developer Mission

## What We Are Building

FERAL is an open-source agent-native computing platform. The goal is not another AI chatbot. The goal is a system where intelligence is the operating layer, not a feature inside one app.

Today, most AI products are chat windows. They forget between sessions. They cannot act beyond their sandbox. They render plain text. They do not bridge software and hardware. They are stateless and disposable.

FERAL is different because it is building the foundations of a persistent, capable, extensible agent system:

- **Memory is a system service.** Notes, episodes, knowledge graph, and Memory Wiki persist locally across sessions and reboots.
- **Workflows are durable.** TaskFlows can pause, wait, resume, and survive server restarts.
- **Interfaces are generated from contracts.** Service providers describe surfaces in JSON. FERAL compiles, caches, and hydrates them. No separate app binary needed.
- **Hardware devices are first-class nodes.** Wearables, robotics, home appliances, and IoT devices connect through one authenticated WebSocket protocol.
- **The user controls everything.** Data, keys, runtime, and identity stay local under `~/.feral/`.

## The Destination

The long-term goal is an agent-native operating system built on **NixOS minimal**.

```
Platform Core (now)
  Brain + Memory + GenUI + Workflows + Hardware + Voice
    |
System Substrate (next)
  NixOS modules + Permission plane + Shell host + Browser runtime
    |
Agent-Native OS (later)
  Installer + First boot + Rollback + Device profiles
```

This is not a weekend project. It is an operating system vision that compounds over time. The current release is the working platform core. Contributors join at the ground floor.

## Why NixOS

NixOS gives us:
- **Declarative system configuration**: the entire OS state is described in code.
- **Reproducible builds**: every dependency is pinned and every build is deterministic.
- **Atomic upgrades and rollback**: safe system updates with instant recovery.
- **Module composition**: system services, hardware profiles, and user environments compose cleanly.

For an agent-native OS, these properties are not nice-to-haves. They are requirements. An AI system that modifies its own environment needs deterministic packaging and safe rollback.

## Why GenUI Replaces Hardcoded Apps

Traditional operating systems ship apps as separate compiled binaries. Each app has its own UI framework, its own update cycle, and its own data silo.

FERAL takes a different path:
1. A service provider submits a JSON contract describing endpoints, brand rules, layout rules, and named surfaces.
2. FERAL compiles the surface once into SDUI (Server-Driven UI) JSON.
3. The compiled layout is cached locally and reused on every subsequent open.
4. Runtime data hydrates placeholders in the cached layout.

This means:
- Providers control their brand through explicit tokens and theme rules.
- Users get stable, predictable layouts (muscle memory is preserved).
- Performance is solved by static caching (no LLM regeneration per open).
- New services can appear instantly without installing a binary.

See [`GENUI_PROVIDER_SPEC.md`](./GENUI_PROVIDER_SPEC.md) for the full contract format.

## Why the Hardware Daemon Model

Instead of building drivers for every device, FERAL defines one protocol:
1. A device connects as a daemon over authenticated WebSocket.
2. It registers its identity, type, and capabilities.
3. The brain can send commands; the daemon returns results.
4. The daemon can stream telemetry, vision, and sensor data.

This means:
- Any device class (wearable, robot, home appliance, IoT sensor, phone bridge) uses the same integration contract.
- New device categories do not require changes to the brain.
- Edge adapters (BLE, MQTT, serial, ROS) translate into the same daemon protocol.

See [`HARDWARE_ECOSYSTEM.md`](./HARDWARE_ECOSYSTEM.md) for the full daemon contract and reference profiles.

## What Exists Today

| Capability | Status |
|:-----------|:-------|
| Chat (text + realtime voice) | Shipped |
| Computer use (shell, files, search, web) | Shipped |
| 4-tier memory + Memory Wiki + ingest | Shipped |
| TaskFlows (durable background workflows) | Shipped |
| Session snapshots, branching, restore | Shipped |
| GenUI (cards, maps, charts, forms, provider surfaces) | Shipped |
| Multi-provider LLM (OpenAI, Anthropic, Gemini, Groq, Ollama) | Shipped |
| Local vision (Ollama VLM) | Shipped |
| MCP server + client | Shipped |
| Hardware daemon protocol + node SDKs | Partial |
| Channels (Telegram, Discord, Slack, WhatsApp) | Partial |
| Security (vault, sandbox, policy) | Partial |
| NixOS foundation | Partial |
| Managed browser runtime | Planned |
| Linux permission plane | Planned |
| Installer and first-boot experience | Planned |

See the [Capability status](https://docs.feral.sh/reference/capability-status) page for the full matrix.

## How To Contribute

Pick a lane and start building.

| Lane | Scope | Entry points |
|:-----|:------|:-------------|
| Runtime / Orchestrator | Agent loop, LLM routing, TaskFlows, sessions, security | `feral-core/agents/`, `feral-core/api/server.py` |
| Memory / Knowledge | 4-tier store, wiki, ingest, sync | `feral-core/memory/` |
| GenUI / Surfaces | SDUI engine, provider contracts, surface caching, renderer | `feral-core/genui/`, `feral-client/src/components/` |
| Hardware / Daemons | Node protocol, daemon SDKs, device profiles, edge bridges | `feral-core/hardware/`, `feral-nodes/` |
| Voice / Perception | Realtime voice, wake word, vision, sensor fusion | `feral-core/voice/`, `feral-core/perception/` |
| Nix / Packaging | Flake, NixOS modules, reproducible builds | `flake.nix` |
| Frontend / Shell | Web UI, dashboard, desktop app, mobile bridges | `feral-client/`, `desktop/` |

See [`../CONTRIBUTING.md`](../CONTRIBUTING.md) for development setup and PR guidelines.

## The Mission In One Paragraph

FERAL is building an open-source agent-native computing platform. The system can talk, act, remember, render structured interfaces, and connect to hardware. Instead of shipping separate apps, service providers describe their surfaces in JSON and the platform handles the rest. Instead of forgetting between sessions, the system persists memory, workflows, and knowledge locally. Instead of relying on a single cloud provider, the system supports multiple LLMs including fully local inference. The destination is a NixOS-native operating system where intelligence is the computing layer, not a feature inside one app. We are building the foundations now.
