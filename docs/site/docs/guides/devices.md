---
id: devices
title: Device Adapters (HUP)
sidebar_position: 2
slug: /guides/devices
---

# Writing a HUP Device Adapter

> Looking to pair a phone from the dashboard? Use the Pairing & Access guide in the Mintlify docs. This page is for building custom HUP adapters.

The **Hardware Unification Protocol (HUP)** is how physical devices — wristbands, smart glasses, robots, sensors — join the FERAL mesh. Each device runs a small adapter that connects to the Brain over WebSocket, registers its capabilities, streams telemetry, and responds to commands.

## Concepts

| Term | Meaning |
|:-----|:--------|
| **Device Mesh** | The Brain's registry of connected hardware nodes |
| **Device Manifest** | A declarative description of a device's type, name, and capabilities |
| **Telemetry** | Periodic sensor data pushed from device → Brain |
| **HUP Action** | A command sent from Brain → device (read, write, execute, stream, configure) |

## Quick Start (Python SDK)

```bash
pip install feral-sdk
```

```python
import asyncio
from feral_sdk import HUPDevice

class TemperatureSensor(HUPDevice):
    device_type = "sensor"
    device_name = "Office Temp Sensor"
    capabilities = ["temperature", "humidity"]
    telemetry_interval_s = 10.0

    async def read_telemetry(self) -> dict:
        # Read from your actual hardware here
        return {
            "temperature_c": 22.5,
            "humidity_pct": 45,
        }

    async def execute_action(self, action: str, params: dict) -> dict:
        if action == "set_interval":
            self.telemetry_interval_s = params.get("seconds", 10.0)
            return {"ok": True, "interval": self.telemetry_interval_s}
        return {"error": f"Unknown action: {action}"}

sensor = TemperatureSensor()
asyncio.run(sensor.run("ws://localhost:9090/v1/daemon"))
```

When the adapter calls `run()`:

1. It opens a WebSocket to the Brain's `/v1/daemon` endpoint.
2. It sends a `node_register` message with its manifest.
3. It starts two concurrent loops:
   - **Telemetry loop** — calls `read_telemetry()` every `telemetry_interval_s` seconds and pushes results.
   - **Command loop** — listens for `node.invoke` messages and dispatches to `execute_action()`.

## Raw WebSocket (No SDK)

You can also connect with plain WebSocket messages:

```python
import asyncio, json, websockets

async def main():
    uri = f"ws://localhost:9090/v1/node?api_key={os.environ['NODE_API_KEY']}"
    async with websockets.connect(uri) as ws:
        # 1. Register
        await ws.send(json.dumps({
            "hop": "daemon",
            "type": "node_register",
            "payload": {
                "node_id": "my-sensor",
                "node_type": "sensor",
                "capabilities": ["temperature", "humidity"],
            },
        }))

        # 2. Telemetry loop
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

asyncio.run(main())
```

## Device Manifest

The manifest tells the Brain what the device can do:

```json
{
  "device_type": "wearable",
  "name": "Feral Wristband",
  "capabilities": ["heart_rate", "spo2", "skin_temp", "accelerometer"],
  "telemetry_interval_s": 5.0,
  "actions": [
    {
      "id": "vibrate",
      "type": "execute",
      "description": "Trigger a vibration alert",
      "params": { "pattern": "string", "duration_ms": "integer" }
    },
    {
      "id": "set_display",
      "type": "write",
      "description": "Update the device display",
      "params": { "text": "string", "icon": "string" }
    }
  ]
}
```

### HUP Action Types

| Type | Direction | Use Case |
|:-----|:----------|:---------|
| `read` | Brain → Device → Brain | Request a specific sensor reading |
| `write` | Brain → Device | Set a value (display, config) |
| `execute` | Brain → Device → Brain | Trigger an action (vibrate, beep) |
| `stream` | Device → Brain | Start a continuous data stream |
| `configure` | Brain → Device | Change device settings |

## Viewing Connected Devices

```bash
# API
curl http://localhost:9090/api/devices/connected

# Dashboard
# The web UI at localhost:9090 shows a hardware panel with connected devices,
# their capabilities, and live telemetry.
```

## Supported Devices

FERAL ships with reference adapters for:

| Device | Adapter Location |
|:-------|:-----------------|
| Smart Glasses (BLE) | `feral-nodes/` |
| Wristband (HR, SpO2, temp) | `feral-nodes/` |
| iOS Bluetooth Bridge | `feral-nodes/phone-bridge/` |
| Home Assistant | `feral-core/integrations/` |

Use these as templates for your own hardware adapters.

## TypeScript / Node Adapter

```typescript
import { FeralNode } from '@feral/sdk';

const node = new FeralNode({
  nodeId: 'garage-door',
  nodeType: 'actuator',
  capabilities: ['open', 'close', 'status'],
});

await node.connect();

setInterval(() => {
  node.sendTelemetry({ status: 'closed', battery_pct: 85 });
}, 10000);

node.onCommand(async (action, params) => {
  if (action === 'open') {
    // trigger hardware
    return { ok: true };
  }
  return { error: `Unknown: ${action}` };
});
```

See the [Node SDK reference](../sdk/node.md) for the full `FeralNode` API.
