---
id: hardware
title: Hardware Mesh Protocol
sidebar_position: 9
slug: /guides/hardware
---

# Hardware Mesh Protocol

FERAL controls physical devices through the **Hardware Unification Protocol (HUP)** — a generic, self-describing hardware hub. Devices declare what they can do via an `actions[]` envelope; the brain converts that into a `DeviceManifest`, registers a transport adapter, auto-generates LLM tools, and runs a closed-loop **honesty loop** when capabilities declare a `verify` contract. Communication stays local-first over USB, WebSocket mesh, or phone-bridged BLE.

## HUP Overview

HUP is to hardware what MCP is to software tools. Devices connect to the Brain (directly or through a bridge), announce their capabilities, and the agent invokes them as tools — without per-device skill code on the brain.

```
Device → Transport Adapter → DeviceRegistry → GenericHardwareSkill → Orchestrator
         ←commands←                              ↑ honesty loop (verify)
         →telemetry→
```

Key properties:
- **Local-first**: all communication stays on the LAN (or host-attached USB).
- **Self-describing**: devices publish an `actions[]` envelope; the brain maps it generically (`HUP_ACTION_SCHEMA`, `device_capability_from_action`, `device_manifest_from_capabilities` in `hardware/protocol.py`).
- **Bidirectional**: the Brain sends commands; devices push telemetry.
- **Hot-pluggable**: devices can join and leave; registration re-runs when manifests change.
- **Honest feedback**: actuator tools report `verified: true/false/none` based on post-action sensor read-back, not bare firmware acks.

### Ingress paths

| Path | Adapter | Example |
|:-----|:--------|:--------|
| Brain-local USB/serial | `GenericSelfDescribingAdapter` (+ optional `_preprocess` hooks) | CuteBot via `cuteferalbot.QtBot` |
| Mesh WebSocket node | `WebSocketNodeAdapter` | Phone daemon, desktop node |
| Phone-bridged BLE peripheral | `BridgedPeripheralAdapter` via `mesh.invoke` | Glasses/wristband through iPhone |

Every ingress calls `state.register_generic_hardware_skill_for(manifest, adapter)` unless `FERAL_GENERIC_HARDWARE_SKILLS=0`.

## Self-describing wire format (`actions[]`)

Companion libraries and bridge nodes return a `capabilities()` / `device_manifest` envelope. Each `actions[]` entry becomes one LLM tool and one safety policy — no hand-written skill manifest required.

```json
{
  "device_type": "robot",
  "transport": {"kind": "usb_serial"},
  "sensors": ["sonar_cm", "battery"],
  "actuators": ["motors"],
  "actions": [
    {
      "name": "drive",
      "category": "actuator",
      "permission_tier": "dangerous",
      "requires_confirmation": true,
      "description": "Direct wheel speeds -100..100",
      "params": [
        {"name": "left", "type": "integer", "required": true},
        {"name": "right", "type": "integer", "required": true}
      ]
    },
    {
      "name": "read_telemetry",
      "category": "sensor",
      "permission_tier": "passive",
      "description": "Snapshot: sonar, mode, battery"
    }
  ]
}
```

Optional per-action fields: `action_type`, `rate_limit_per_minute`, `verify`, `returns`, `safety_notes`. See `HUP_ACTION_SCHEMA` in `feral-core/hardware/protocol.py` for the full schema.

## Device Manifests

The in-brain model is `DeviceManifest` + `DeviceCapability` (`hardware/protocol.py`). Once registered, capabilities appear as LLM tools named **`hwdev_<device_id>__<capability_id>`** (device IDs are sanitized; e.g. `cutebot-usb-0` → `hwdev_cutebot_usb_0__drive`).

```json
{
  "device_id": "cutebot-usb-0",
  "device_type": "robot",
  "name": "QtBot (CuteBot)",
  "connection_type": "serial",
  "capabilities": [
    {
      "id": "follow_line",
      "name": "Follow Line",
      "category": "actuator",
      "permission_tier": "active",
      "requires_confirmation": true,
      "verify": {
        "via": "read_telemetry",
        "field": "mode",
        "expect": ["line_follow"],
        "delay_ms": 1600,
        "retries": 1
      }
    },
    {
      "id": "read_telemetry",
      "name": "Read Telemetry",
      "category": "sensor",
      "permission_tier": "passive"
    }
  ],
  "sensors": ["sonar_cm", "battery"],
  "actuators": ["motors"]
}
```

Once registered, the agent sees tools like:

```
Available tools:
  - hwdev_cutebot_usb_0__follow_line (actuator: confirm + approval)
  - hwdev_cutebot_usb_0__read_telemetry (sensor: read-only)
```

## Honesty loop (`verify` contract)

Actuator capabilities may declare a closed-loop verification contract on `DeviceCapability.verify`. After execution, `GenericHardwareSkill` re-reads the named sensor capability (`via`), checks `field` against `expect`, honors `delay_ms` and `retries`, and returns:

- `verified: true` — observed state matches expectation
- `verified: false` — action ran but state did not match (retries exhausted)
- `verified: none` — no contract declared; telemetry attached but success is not asserted

History is recorded on the `DeviceRegistry` and exposed via `GET /api/hardware/fleet`.

## Brain-local discovery

USB/host-attached devices are discovered via **`DEVICE_DISCOVERY_SPECS`** in `hardware/discovery.py`. Each entry names a Python module/class, availability probe, and adapter kind (`generic` or device-specific like `cutebot`). Set **`FERAL_CUTEBOT_PATH`** to point at the `cuteferalbot` repo when it is not installed as a package.

## Kill switch

**`FERAL_GENERIC_HARDWARE_SKILLS`** defaults to `"1"` (generic path enabled). Set to `"0"` to disable auto-generated tools and use hand-written skill manifests only (legacy `cutebot.json` remains as a fallback).

## Capability Types

| Type | Direction | Examples |
|:-----|:----------|:---------|
| `sensor` | Device → Brain | Heart rate, temperature, motion, ambient light |
| `actuator` | Brain → Device | Vibrate, LED color, lock/unlock, move servo |
| `state` | Bidirectional | On/off toggle, mode selection, brightness level |
| `stream` | Device → Brain (continuous) | Audio, video, raw IMU data |

## Built-in Adapters

### Wristband Adapter

For BLE-connected health wristbands. Bridges BLE GATT characteristics to HUP.

```python
from feral_core.hardware import WristbandAdapter

adapter = WristbandAdapter(
    ble_address="AA:BB:CC:DD:EE:FF",
    services={
        "heart_rate": "0x180D",
        "spo2": "0x1822",
    },
)
await adapter.connect()
await adapter.register_with_brain("http://localhost:9090")
```

### Smart Home Adapter

Bridges Zigbee/Z-Wave/WiFi smart home devices via Home Assistant or direct local APIs.

```yaml
# ~/.feral/hardware/smart_home.yaml
adapter: smart_home
source: homeassistant
ha_url: http://homeassistant.local:8123
ha_token: $CREDENTIAL:ha_long_lived_token

devices:
  - entity_id: light.living_room
    name: "Living Room Lights"
    capabilities:
      - id: toggle
        type: state
        states: ["on", "off"]
      - id: brightness
        type: state
        range: [0, 255]
      - id: color
        type: state
        format: hex
```

### Robot Arm Adapter

Controls articulated robot arms via serial or network protocols.

```yaml
adapter: robot_arm
protocol: serial
port: /dev/ttyUSB0
baud: 115200

capabilities:
  - id: move_joint
    type: actuator
    params:
      - name: joint
        type: integer
        min: 1
        max: 6
      - name: angle
        type: number
        min: -180
        max: 180
      - name: speed
        type: number
        min: 0
        max: 100
  - id: gripper
    type: actuator
    params:
      - name: action
        type: string
        enum: ["open", "close"]
  - id: position
    type: sensor
    description: "Current joint angles"
```

## Direct Local Control

HUP intentionally avoids cloud roundtrips. Commands go directly from the Brain to the device over the LAN. This gives:

- **Low latency**: sub-10ms for local WebSocket commands.
- **Privacy**: sensor data never leaves the home network.
- **Reliability**: works without internet.

The Brain can also run on the same device as the adapter (e.g., a Raspberry Pi with a BLE dongle), reducing the path to a local function call.

## Telemetry Ingestion

Devices push telemetry at their configured interval. The Brain routes it to:

1. **Working memory** — latest values available to the LLM.
2. **Execution log** — historical telemetry for trend analysis.
3. **Proactive engine** — triggers alerts when thresholds are crossed.

```json
{
  "type": "telemetry",
  "device_id": "wristband-001",
  "readings": [
    {"capability": "heart_rate", "value": 72, "timestamp": 1718450400.0},
    {"capability": "spo2", "value": 98, "timestamp": 1718450400.0}
  ]
}
```

## Writing a Custom Adapter

For a **self-describing** companion library, use `GenericSelfDescribingAdapter` — it passthrough-executes any capability the device declares. Override `_preprocess` / `_harden_params` only for device-specific safety:

```python
from hardware.adapters.generic import GenericSelfDescribingAdapter
from hardware.protocol import HUPAction, HUPResult

class MyRobotAdapter(GenericSelfDescribingAdapter):
    async def _preprocess(self, action: HUPAction, params: dict) -> dict | HUPResult:
        if action.capability_id == "drive" and not (await self.get_state()).get("battery"):
            return HUPResult(action_id=action.action_id, device_id=self.device_id,
                             status="failure", error="Battery off")
        return params

    def _harden_params(self, cap_id: str, params: dict) -> dict:
        if cap_id == "drive":
            for k in ("left", "right"):
                if k in params:
                    params[k] = max(-80, min(80, int(params[k])))
        return params
```

For **phone-bridged BLE peripherals**, the brain uses `BridgedPeripheralAdapter` — actions route through `mesh.invoke` to the bridge node. The peripheral's manifest (from `peripheral_bridge_register`) drives tools and the honesty loop with no per-device code.

Add brain-local discovery by appending a `DeviceDiscoverySpec` to `DEVICE_DISCOVERY_SPECS` in `hardware/discovery.py`.

## API Reference

| Endpoint | Method | Description |
|:---------|:-------|:------------|
| `/v1/node` | WebSocket | HUP device connection endpoint |
| `/api/hardware/devices` | GET | Registered HUP devices and manifests |
| `/api/hardware/execute` | POST | Execute a HUP action on a device |
| `/api/hardware/fleet` | GET | Unified fleet view — manifests, derived safety tiers, last verification state, mesh nodes, announced devices, stats |
| `/api/hardware/context` | GET | LLM-facing hardware capability summary |
| `/api/hardware/mesh` | GET | Hardware mesh state |
| `/api/devices/connected` | GET | List all connected devices with types and metrics |
| `/api/devices/paired` | GET | List all paired edge-node devices |
| `/api/devices/pair` | POST | Pair a new edge-node device |
| `/api/devices/{device_id}` | DELETE | Revoke (un-pair) a device |
| `/api/nodes/health` | GET | All node health status with heartbeat freshness |
| `/api/commands/recent` | GET | Recent commands with lifecycle state |
| `/api/commands/{command_id}` | GET | Single command detail with state history |
