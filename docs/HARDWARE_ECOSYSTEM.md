# FERAL Hardware Ecosystem

This document defines the contract for connecting hardware devices to FERAL. Any device class -- wearables, robotics, home appliances, IoT sensors, phone bridges -- uses the same protocol.

> **2026-06 update:** The brain now treats HUP as a **generic self-describing hardware hub**. A device's own `capabilities()` envelope (the `actions[]` wire format) drives LLM tools, safety tiers, and the closed-loop honesty loop — with no per-device skill code. See [Generic self-describing path](#generic-self-describing-path) below.

## Architecture

```mermaid
flowchart TD
  device[Physical Device] --> adapter[Edge / Transport Adapter]
  adapter --> ingress[Brain HUP Ingress]
  ingress --> registry[Device Registry]
  ingress --> genSkill[GenericHardwareSkill]
  genSkill --> orchestrator[Orchestrator]
  registry --> orchestrator
  mesh[Hardware Mesh] --> ingress
```

Devices reach the FERAL Brain through one of three **ingress paths**, all converging on the same generic registration:

| Ingress | Transport adapter | Typical example |
|:--------|:------------------|:----------------|
| Brain-local USB/serial | `GenericSelfDescribingAdapter` or bespoke wrapper (e.g. `CuteBotAdapter`) | CuteBot over USB |
| Mesh WebSocket node | `WebSocketNodeAdapter` | Phone, desktop daemon, robot bridge |
| Phone-bridged peripheral | `BridgedPeripheralAdapter` → `mesh.invoke` | BLE glasses/wristband via iPhone |

On every ingress the brain calls `state.register_generic_hardware_skill_for(manifest, adapter)`, which turns the manifest into LLM tools (`hwdev_<device_id>__<capability_id>`) unless `FERAL_GENERIC_HARDWARE_SKILLS=0`.

## Generic self-describing path

### Wire format (`actions[]`)

Self-describing devices return a `capabilities()` envelope. The schema is documented as `HUP_ACTION_SCHEMA` in `feral-core/hardware/protocol.py`. Each entry in `actions[]` becomes one `DeviceCapability` via `device_capability_from_action()`; the full manifest is built by `device_manifest_from_capabilities()`:

```json
{
  "device_type": "robot",
  "transport": {"kind": "usb_serial", "port": "/dev/ttyACM0"},
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
      ],
      "verify": {
        "via": "read_telemetry",
        "field": "mode",
        "expect": ["manual"],
        "delay_ms": 1600,
        "retries": 1,
        "transient": false
      }
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

Optional fields per action: `action_type`, `rate_limit_per_minute`, `reversible`, `returns`, `safety_notes`. Wire spellings `name`/`params` and model spellings `id`/`parameters` are both accepted.

### Generic transport adapters

**`GenericSelfDescribingAdapter`** (`hardware/adapters/generic.py`) is the default execute path for any companion library that exposes:

- `capabilities() -> dict`
- `execute(command, **params) -> dict`
- `status() -> dict` (optional)
- `poll_events(seconds) -> list[dict]` (optional)

Device-specific safety (battery gate, parameter clamping) belongs in thin subclasses overriding `_preprocess` / `_harden_params`. **`CuteBotAdapter`** is the reference: generic passthrough plus CuteBot-specific hardening.

**`BridgedPeripheralAdapter`** (`hardware/adapters/bridge.py`) forwards HUP actions to a peripheral through its bridge node (`mesh.invoke`), used when a phone announces a BLE sub-device via `peripheral_bridge_register`.

### LLM tools and the honesty loop

**`GenericHardwareSkill`** (`hardware/capability_skill.py`) generates a `SkillManifest` from any `DeviceManifest` at registration. Tool names follow `hwdev_<sanitized_device_id>__<capability_id>` (see `skill_id_for_device()`).

After actuator calls the skill:

1. Dispatches via `DeviceRegistry.execute_action`.
2. If the capability declares a `verify` contract, re-reads the named sensor (`via`), waits `delay_ms`, retries up to `retries` times, and returns `verified: true/false/none`.
3. Enforces `rate_limit_per_minute` per capability.
4. Records episodic memory and a knowledge-graph entity on register (memory parity with the legacy path).
5. Records action+verify history on the `DeviceRegistry` for the fleet API.

Safety tiers are derived generically from each capability's `category`, `permission_tier`, and `requires_confirmation`. Additive drive speed limits in `security/safety_resolver.py` apply to both legacy `cutebot__drive` and generic `hwdev_*__drive`.

### Brain-local discovery

USB/host-attached devices are discovered via **`DEVICE_DISCOVERY_SPECS`** in `hardware/discovery.py`. Each `DeviceDiscoverySpec` names a module/class, availability probe, adapter kind (`cutebot` or `generic`), and optional path candidates. Override the CuteBot repo location with **`FERAL_CUTEBOT_PATH`**.

### Kill switch

**`FERAL_GENERIC_HARDWARE_SKILLS`** defaults to `"1"` (generic path on). Set to `"0"` to disable auto-generated tools and rely on hand-written skill manifests only (e.g. legacy `skills/manifests/cutebot.json`). The generic and legacy paths can run alongside each other during A/B.

## The Three-Layer Contract

### Layer 1: Transport

All daemons connect via WebSocket to `wss://{brain_host}:{brain_port}/v1/node?api_key={key}`.

The `api_key` query parameter authenticates the daemon. Set via the `NODE_API_KEY` environment variable (no default — must be configured before deployment).

This is enforced, not just documented: if `NODE_API_KEY` is unset or empty, the brain **refuses every unpaired `/v1/node` connection** with close code `4003` and logs `feral.security.node_api_key_unset`. An empty key never grants access. Devices that have completed pairing authenticate with their pairing token instead and are unaffected by an unset `NODE_API_KEY`.

### Layer 2: Device Manifest

On connection, every daemon sends a registration message declaring its identity, type, and capabilities.

**Registration message:**
```json
{
  "hop": "daemon",
  "type": "node_register",
  "payload": {
    "node_id": "unique-device-id",
    "node_type": "sensor | robot | glasses | phone | actuator | desktop",
    "platform": "linux | ios | android | rtos | custom",
    "capabilities": ["temperature", "humidity", "motor_control"]
  }
}
```

For richer integration, include a **`device_manifest`** in the registration payload — either the preferred **`actions[]` self-description envelope** (converted by `device_manifest_from_capabilities()`) or a full `DeviceManifest` dict. When present, the brain builds LLM tools, safety tiers, and verify contracts from the device itself instead of coarse capability-name stubs.

### Layer 3: Execution

**Brain to daemon (command):**
```json
{
  "type": "command",
  "request_id": "abc123",
  "command": "sensor.read",
  "args": {"sensor_name": "temperature"}
}
```

**Daemon to brain (result):**
```json
{
  "hop": "daemon",
  "type": "execute_result",
  "payload": {
    "request_id": "abc123",
    "success": true,
    "data": {"temperature_c": 22.5}
  }
}
```

The `request_id` field correlates commands with results.

## Telemetry Streaming

Daemons can push telemetry data without being asked:

```json
{
  "hop": "daemon",
  "type": "telemetry",
  "payload": {
    "node_id": "wristband-01",
    "sensors": {
      "heart_rate": 72,
      "spo2": 98,
      "temperature_c": 36.5
    }
  }
}
```

Batch telemetry:
```json
{
  "hop": "daemon",
  "type": "sensor_batch",
  "payload": {
    "node_id": "wristband-01",
    "samples": [
      {"ts": 1712700000, "heart_rate": 71, "spo2": 98},
      {"ts": 1712700005, "heart_rate": 73, "spo2": 97}
    ]
  }
}
```

## Vision Frames

Devices with cameras can stream vision frames:

```json
{
  "hop": "daemon",
  "type": "vision_frame",
  "payload": {
    "node_id": "glasses-01",
    "image_b64": "base64-encoded-jpeg",
    "resolution": "640x480",
    "timestamp_ms": 1712700000000
  }
}
```

## HUP Device Manifest (Rich Registration)

For advanced integrations, daemons can declare a full HUP manifest:

```yaml
device_id: "robot-arm-01"
device_type: "robot"
name: "6-DOF Robot Arm"
manufacturer: "FERAL"
model: "ARM-600"
firmware_version: "1.2.0"
connection_type: "websocket"
battery_powered: false
location: "workshop"
tags: ["industrial", "actuator"]

capabilities:
  - id: "move_joint"
    name: "Move Joint"
    description: "Move a specific joint to a target angle"
    category: "actuator"
    permission_tier: "privileged"
    requires_confirmation: true
    reversible: true
    safety_notes: "Ensure workspace is clear before movement"
    parameters:
      - name: "joint_id"
        type: "integer"
        required: true
      - name: "angle_degrees"
        type: "number"
        required: true
      - name: "speed_pct"
        type: "number"
        default: 50
    returns:
      joint_id: "int"
      final_angle: "float"
      duration_ms: "int"

  - id: "read_position"
    name: "Read Position"
    description: "Get current joint positions"
    category: "sensor"
    permission_tier: "passive"
    returns:
      joints: "list[float]"
      timestamp: "float"

sensors: ["position", "force", "temperature"]
actuators: ["joint_motor", "gripper"]
```

## Permission Tiers

Every capability declares a permission tier:

| Tier | Allowed actions | Requires confirmation |
|:-----|:---------------|:---------------------|
| `passive` | Read-only: sensors, status, telemetry | No |
| `active` | Send data: notifications, display, audio | No |
| `privileged` | System modification: file access, commands | Yes |
| `dangerous` | Destructive: motor control at high speed, delete, financial | Yes |

The `ExecutionSandbox` in the Brain enforces these tiers and can apply per-skill rate limits.

## Reference Device Profiles

### Wearable Telemetry Node
A wristband or smart glasses that streams health and motion data.
- Type: `glasses` / `wristband`
- Capabilities: `heart_rate`, `spo2`, `temperature`, `steps`, `uv`, `accelerometer`
- Category: `sensor` (passive)
- Example: FERAL W300 glasses, health wristband

### Home Automation Bridge
A bridge to smart home devices (lights, HVAC, locks, appliances).
- Type: `home_bridge`
- Capabilities: `light_control`, `thermostat`, `lock`, `power_toggle`
- Category: `actuator` (active/privileged)
- Adapter: Home Assistant bridge, Zigbee/Z-Wave hub

### Robotics Actuator Node
A robot arm, drone, or mobile robot.
- Type: `robot`
- Capabilities: `move_joint`, `grip`, `navigate`, `read_position`, `read_force`
- Category: `actuator` (privileged/dangerous)
- Adapter: ROS bridge, serial, custom firmware

### Phone-as-Bridge Node
A smartphone that bridges BLE peripherals and provides camera/GPS/health.
- Type: `phone`
- Capabilities: `camera`, `gps`, `health_sensors`, `notification`, `haptic`
- Category: mixed
- SDKs: `feral-nodes/ios-bridge/`, `feral-nodes/android-bridge/`

## Building a Hardware Daemon

Minimal Python daemon:

```python
import asyncio
import json
import websockets

BRAIN_URL = f"ws://localhost:9090/v1/node?api_key={os.environ['NODE_API_KEY']}"

async def main():
    async with websockets.connect(BRAIN_URL) as ws:
        await ws.send(json.dumps({
            "hop": "daemon",
            "type": "node_register",
            "payload": {
                "node_id": "my-sensor",
                "node_type": "sensor",
                "capabilities": ["temperature", "humidity"],
            },
        }))

        async def send_telemetry():
            while True:
                await ws.send(json.dumps({
                    "hop": "daemon",
                    "type": "telemetry",
                    "payload": {
                        "node_id": "my-sensor",
                        "sensors": {"temperature_c": 22.5, "humidity": 45},
                    },
                }))
                await asyncio.sleep(5)

        async def handle_commands():
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "command":
                    result = execute_command(msg["command"], msg.get("args", {}))
                    await ws.send(json.dumps({
                        "hop": "daemon",
                        "type": "execute_result",
                        "payload": {
                            "request_id": msg["request_id"],
                            "success": True,
                            "data": result,
                        },
                    }))

        await asyncio.gather(send_telemetry(), handle_commands())

def execute_command(command, args):
    if command == "sensor.read":
        return {"temperature_c": 22.5, "humidity": 45}
    return {"error": f"Unknown command: {command}"}

asyncio.run(main())
```

## Edge Adapter Model

For devices that do not speak WebSocket natively, build an edge adapter:

```
Physical Device <--BLE/MQTT/Serial/ROS--> Edge Adapter <--WebSocket--> Brain
```

The edge adapter translates the device's native protocol into the daemon WebSocket contract. All edge adapters produce the same registration, telemetry, and command/result messages.

## REST API

| Endpoint | Method | Description |
|:---------|:-------|:------------|
| `/api/devices/connected` | GET | List all connected devices with types and metrics |
| `/api/devices/paired` | GET | List all paired edge-node devices |
| `/api/devices/pair` | POST | Pair a new edge-node device |
| `/api/devices/{device_id}` | DELETE | Revoke (un-pair) a device |
| `/api/nodes/health` | GET | All node health status with heartbeat freshness |
| `/api/commands/recent` | GET | Recent commands with lifecycle state |
| `/api/hardware/devices` | GET | Registered HUP devices and manifests |
| `/api/hardware/execute` | POST | Execute a HUP action on a device |
| `/api/hardware/fleet` | GET | Unified fleet view — manifests, safety tiers, last verification state, mesh nodes, announced devices, stats |
| `/api/hardware/context` | GET | LLM-facing hardware capability summary |
| `/api/hardware/mesh` | GET | Hardware mesh state |
| `/v1/node` | WS | Hardware daemon WebSocket channel |

## Implementation Reference

- Protocol: `feral-core/hardware/protocol.py` (`HUP_ACTION_SCHEMA`, `device_capability_from_action`, `device_manifest_from_capabilities`, `DeviceManifest`, `DeviceCapability`, `HUPAction`, `HUPResult`)
- Generic skill: `feral-core/hardware/capability_skill.py` (`GenericHardwareSkill`, `skill_id_for_device`)
- Adapters: `feral-core/hardware/adapters/generic.py`, `feral-core/hardware/adapters/bridge.py`, `feral-core/hardware/adapters/cutebot.py`
- Discovery: `feral-core/hardware/discovery.py` (`DEVICE_DISCOVERY_SPECS`, `DeviceDiscoverySpec`)
- Registration: `feral-core/api/state.py` (`register_generic_hardware_skill_for`)
- Mesh: `feral-core/hardware/mesh.py` (`HardwareMesh`, `WebSocketNodeAdapter`, node-supplied `device_manifest`)
- Fleet API: `feral-core/api/routes/security_and_hardware.py` (`GET /api/hardware/fleet`)
- Safety: `feral-core/security/safety_resolver.py` (additive `cutebot__drive` + `hwdev_*__drive` speed limit)
- Server handler: `feral-core/api/server.py` (`/v1/node` WebSocket, `peripheral_bridge_register`)
- Python SDK: `feral-nodes/python-node-sdk/`
- iOS bridge: `feral-nodes/ios-bridge/`
- Android bridge: `feral-nodes/android-bridge/`
