# CuteBot & External Hardware Architecture Plan

**Status:** Investigation + design (no code changes)  
**Date:** 2026-06-12  
**Scope:** Extend FERAL's existing HUP/hardware stack for robots and IoT; CuteBot (Elecfreaks Smart Cutebot / micro:bit V2) as first adapter.

---

## Executive Summary

FERAL already implements roughly **70% of the target "Device Registry → Adapter → Transport" stack** under `ASOS/feral-core/hardware/`. The brain has `DeviceRegistry`, `DeviceManifest`, `DeviceAdapter`, `HardwareMesh` (WebSocket node invoke + peripheral discovery), a durable `CommandLedger`, REST/gateway execute paths, and safety gating via `ToolRunner` + `ApprovalManager`. **Do not build a parallel hardware system.**

The cleanest extension point is: **Orchestrator → skill tool (or `hardware.execute`) → `DeviceRegistry.execute_action` → brain-local `CuteBotAdapter` → USB serial (`cuteferalbot.QtBot`)**. CuteBot is **USB serial only today** (115200 baud); there is no BLE control path in the cuteferalbot repo. For demo day, **USB serial is the pragmatic transport**; the end-state for wireless robots is a **HUP bridge node** (phone or Pi) that owns the transport and speaks `device_event` / `hup_action_request` to the brain.

---

## 1. Current FERAL Architecture

### 1.1 Layer map

| Layer | Location | Role |
|-------|----------|------|
| **Brain / API** | `api/server.py`, `api/state.py` | FastAPI + WebSocket `/v1/node` (:9090); boot wires subsystems |
| **Orchestrator** | `agents/orchestrator.py` | Routes prompts → skills → LLM tools → `ToolRunner` |
| **Multi-agent** | `agents/multi_agent.py` | Optional worker routing (health/home/research/creative/general); no dedicated robotics worker |
| **Skills** | `skills/registry.py`, `skills/manifests/*.json`, `skills/executor.py` | Declarative tools exposed to LLM; HTTP/LOCAL/WS_EXECUTE dispatch |
| **Tool execution & safety** | `agents/tool_runner.py`, `security/safety_resolver.py`, `security/exec_approvals.py` | Autonomy modes (strict/hybrid/loose), approval cards, speed limits |
| **HUP protocol models** | `hardware/protocol.py`, `models/protocol.py` | Device manifests, actions, WS message payloads |
| **Hardware mesh** | `hardware/mesh.py` | WS daemon auto-registration, `invoke()`, `device_announce` ingest |
| **Command lifecycle** | `hardware/command_contract.py` | SQLite `CommandLedger`, idempotency, timeouts |
| **Perception / world state** | `perception/fusion.py` | `PerceptionFrame` fused into LLM system context |
| **Integrations** | `integrations/mqtt_bridge.py`, `integrations/home_assistant.py`, etc. | MQTT IoT, smart home |
| **Node SDKs** | `ASOS/feral-nodes/ios-node-sdk/` | iOS/browser nodes connect over WS, emit `device_event` |
| **Gateway** | `gateway/protocol.py` | JSON-RPC: `node.invoke`, `hardware.execute` |

### 1.2 How an agent decides to call external capability

1. User message enters `Orchestrator.handle_command_stream` (or `MultiAgentOrchestrator`).
2. **`_route_prompt`** (`orchestrator.py:2897–2969`) selects up to ~5 relevant `SkillManifest` entries via heuristic + optional cheap LLM disambiguation.
3. **`skills.get_tools_for_skills(relevant_skills)`** builds LLM tool definitions (capped at 64 tools in `ToolRunner.assemble_llm_tool_list`).
4. LLM emits a tool call (e.g. `robot_ext__robot_move`).
5. **`ToolRunner.enforce_safety`** (`tool_runner.py:193–279`) runs `resolve_policy` → may return `pending_approval` (CONFIRM) or deny (DENY).
6. **`SkillExecutor.execute`** (`skills/executor.py`) dispatches by endpoint method:
   - `WS_EXECUTE` → WebSocket `execute` message to matching daemon (`executor.py:400–465`)
   - `LOCAL` / impl class → in-process Python (`skills/impl/robot_action.py`)
7. Alternative path: REST **`POST /api/hardware/execute`** or gateway **`hardware.execute`** → `DeviceRegistry.execute_action` (`security_and_hardware.py:114–131`, `gateway/protocol.py:498–511`).

### 1.3 Tools / actions / devices concepts (already exist)

| Concept | Exists? | Where |
|---------|---------|-------|
| **Skill tools** (LLM-facing) | ✅ | `skills/manifests/*.json` → `{skill_id}__{endpoint_id}` |
| **HUP capabilities** (device-facing) | ✅ | `DeviceCapability` in `hardware/protocol.py:38–50` |
| **Device registry** | ✅ | `DeviceRegistry` in `hardware/protocol.py:120–244` |
| **Device adapters** | ✅ | `DeviceAdapter` base + reference adapters in `hardware/adapters/` |
| **WS node capabilities** (flat strings) | ✅ | `NodeRegisterPayload.capabilities` (`models/protocol.py:434`) |
| **Command ledger** | ✅ | `CommandLedger` in `hardware/command_contract.py:88–375` |
| **Peripheral discovery** | ✅ | `device_announce` → `HardwareMesh.ingest_device_announce` (`mesh.py:339–413`) |
| **Robot skill** | ✅ (partial) | `robot_ext` in `skills/manifests/robot_action.json` |
| **Dedicated hardware orchestrator** | ❌ | No class between planner and adapter for sensor-monitored sequences |
| **Robot fields in PerceptionFrame** | ❌ | Only biometrics, vision, gesture, `connected_nodes` (`fusion.py:134–178`) |

### 1.4 Memory, state, task planning

- **Session memory:** `MemoryStore` (episodes, conversation, knowledge graph) — MockRoomba writes actuator episodes (`mock_roomba.py:167–187`).
- **World state:** `PerceptionEngine.get_frame(session_id)` → `PerceptionFrame.to_system_context()` injected into LLM prompts.
- **Multi-step execution:** `TaskFlowRuntime` (`agents/taskflow.py`) — SQLite-backed flows; could host behavior sequences but is not wired to hardware today.
- **Intent compilation:** `agents/intent_compiler.py` validates actions against skill registry; not robot-specific.

### 1.5 Boot report subsystems (hardware-relevant)

From `api/state.py`:

```1443:1473:ASOS/feral-core/api/state.py
        with boot_subsystem(self._boot_report, "HardwareMesh"):
            self.hardware_mesh = HardwareMesh(
                device_registry=self.device_registry,
                daemons=self.daemons,
            )
        ...
        with boot_subsystem(self._boot_report, "MockRoomba"):
            ...
                self.mock_roomba = MockRoomba(memory=self.memory)
                _register_mock_roomba(self.hardware_mesh, self.mock_roomba)
```

```999:1004:ASOS/feral-core/api/state.py
        self.device_registry = DeviceRegistry()
        self.mcp_server = FeralMCPServer(
            device_registry=self.device_registry,
            memory=self.memory,
            perception=self.perception,
        )
```

```1731:1734:ASOS/feral-core/api/state.py
        with boot_subsystem(self._boot_report, "MQTTBridge"):
            self.mqtt_bridge = MQTTBridge()
            if self.mqtt_bridge.configured:
                await self.mqtt_bridge.start()
```

---

## 2. Best Extension Point

### 2.1 Target shape (mapped to existing code)

```
Feral Agent / Planner
    ↓  tool call (skill or hardware.execute)
ToolRunner (safety / approval)
    ↓
SkillExecutor OR DeviceRegistry.execute_action
    ↓
Hardware Orchestrator (NEW — thin command-sequence layer)
    ↓
DeviceRegistry + Adapter
    ↓
Transport (USB serial | WebSocket node | MQTT)
    ↓
Physical device
```

**Recommendation:** Implement the **delta** as:

1. **`hardware/orchestrator.py`** (net new) — `HardwareOrchestrator`: translates high-level intents into command sequences with `poll_events` stop conditions; uses `CommandLedger` for lifecycle.
2. **`hardware/adapters/cutebot.py`** (net new) — wraps `cuteferalbot.device.QtBot`; implements extended `DeviceAdapter` interface.
3. **Boot wiring in `api/state.py`** (exists, needs change) — USB discovery loop registers CuteBot in `device_registry`.
4. **`skills/manifests/cutebot.json`** (net new) — LLM tools mapped to HUP capability IDs; safer than overloading `robot_ext` (which targets WS robot daemons).

**Why not extend `robot_ext` alone?** `robot_ext` uses `WS_EXECUTE` to a connected daemon with `node_type="robot"` (`skills/executor.py:404–420`). CuteBot is a **brain-attached USB device**, not a WS node. The manifest's `RobotActionSkill` impl (`skills/impl/robot_action.py`) points at `RobotArmAdapter` and is **not** on the WS_EXECUTE path used in integration tests.

**Why not a parallel "IoT service"?** `DeviceRegistry`, `HardwareMesh`, HUP REST API, gateway methods, and `CommandLedger` already cover registry, invoke, discovery, and audit.

### 2.2 What HardwareMesh already does (reuse)

| Feature | Status | Reference |
|---------|--------|-----------|
| Auto-register WS nodes as HUP devices | ✅ Reuse | `mesh.py:167–203` `on_node_connected` |
| `node.invoke` with timeout + ledger | ✅ Reuse | `mesh.py:212–310` |
| `device_announce` peripheral table + KG | ✅ Reuse | `mesh.py:339–465` |
| `WebSocketNodeAdapter` | ✅ Reuse for phone/robot WS nodes | `mesh.py:473–532` |
| Brain-local USB/serial devices | ❌ Net new | Register adapter directly on `device_registry` |

---

## 3. Hardware Abstraction Design

### 3.1 Extended `DeviceAdapter` interface

**Already exists** (`hardware/protocol.py:251–270`): `execute`, `get_status`, `disconnect`.

**Proposed extension** (net new methods on base class or `ManagedDeviceAdapter` mixin):

```python
class ManagedDeviceAdapter(DeviceAdapter):
    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    def get_capabilities(self) -> DeviceManifest: ...  # alias for .manifest property
    async def execute(self, action: HUPAction) -> HUPResult: ...
    async def get_state(self) -> dict: ...
    async def subscribe_to_events(self, callback: Callable[[dict], Awaitable[None]]) -> str: ...
    async def unsubscribe(self, subscription_id: str) -> None: ...
    async def emergency_stop(self) -> HUPResult: ...
```

**Classification:**

| Method | Status |
|--------|--------|
| `execute(HUPAction) → HUPResult` | ✅ Exists on `RobotArmAdapter` (`robot_arm.py:150–214`); ⚠️ `DeviceRegistry.execute_action` expects `dict` return from adapter (`protocol.py:203–207`) — **needs fix** |
| `get_status()` | ✅ Exists on base adapter |
| `connect` / `disconnect` | ✅ Exists on `RobotArmAdapter.connect` (`robot_arm.py:134–148`); not on base class |
| `subscribe_to_events` | ❌ Net new |
| `emergency_stop` | ✅ Partial — `estop` capability on `RobotArmAdapter`; CuteBot maps to `halt` |

### 3.2 Transport abstraction

| Transport | Existing support | CuteBot |
|-----------|------------------|---------|
| **WebSocket** (HUP nodes) | ✅ `WebSocketNodeAdapter`, `HardwareMesh.invoke` | ❌ Not today |
| **Serial/USB** | ✅ Reference in `RobotArmAdapter` (`connection_type="serial"`) | ✅ **Primary** — `cutebot/serial_client.py` @ 115200 |
| **BLE** | ✅ Manifest field `connection_type="ble"` (glasses) | ❌ Not in cuteferalbot |
| **MQTT** | ✅ `MQTTBridge` (`integrations/mqtt_bridge.py`) | ❌ Not needed for CuteBot v1 |

---

## 4. Device Capability Schema

### 4.1 FERAL canonical schema (reuse)

**Primary format:** `DeviceManifest` + `DeviceCapability` (`hardware/protocol.py:38–71`).

Example CuteBot manifest (proposed, aligned with existing types):

```json
{
  "device_id": "cutebot-usb-0",
  "device_type": "robot",
  "name": "QtBot (CuteBot)",
  "manufacturer": "Elecfreaks",
  "model": "EF08209",
  "connection_type": "serial",
  "location": "desk",
  "battery_powered": true,
  "sensors": ["sonar_cm", "line_left", "line_right", "light", "pitch_mg", "battery"],
  "actuators": ["motors", "headlights", "neopixels"],
  "capabilities": [
    {
      "id": "follow_line",
      "name": "Follow Line",
      "description": "Autonomous line follow (black on white track)",
      "category": "actuator",
      "permission_tier": "active",
      "requires_confirmation": true,
      "reversible": true,
      "safety_notes": "Ensure track is clear; firmware handles obstacle reflex."
    },
    {
      "id": "explore",
      "name": "Explore Table",
      "description": "Roam open surface with edge + obstacle safety",
      "category": "actuator",
      "permission_tier": "active",
      "requires_confirmation": true
    },
    {
      "id": "halt",
      "name": "Emergency Stop",
      "description": "Stop motors immediately",
      "category": "actuator",
      "permission_tier": "passive",
      "reversible": false
    },
    {
      "id": "drive",
      "name": "Manual Drive",
      "description": "Direct wheel speeds -100..100",
      "category": "actuator",
      "permission_tier": "dangerous",
      "requires_confirmation": true,
      "parameters": [
        {"name": "left", "type": "integer", "required": true},
        {"name": "right", "type": "integer", "required": true}
      ],
      "safety_notes": "Speed clamp enforced by adapter; auto-reverts after 1.5s."
    },
    {
      "id": "read_telemetry",
      "name": "Read Telemetry",
      "description": "Snapshot: sonar, line sensors, mode, state, battery",
      "category": "sensor",
      "permission_tier": "passive"
    }
  ]
}
```

### 4.2 HUP node capabilities (flat strings) — complementary, not replacement

WS nodes register flat capability strings (`models/protocol.py:434`):

```python
# NodeRegisterPayload (excerpt)
capabilities: list[str] = []  # e.g. ["camera", "heart_rate", "gpio"]
```

iOS SDK sends these on `node_register` (`feral-nodes/ios-node-sdk/Sources/FeralNodeSDK/FeralNode.swift:96–115`).

**Mapping strategy:**

| Device class | Capability declaration | Command path |
|--------------|------------------------|--------------|
| WS node (phone, robot daemon) | `node_register.capabilities` + optional `skills[]` | `HardwareMesh.invoke` / `WS_EXECUTE` |
| Brain-local USB (CuteBot) | `DeviceManifest` in `DeviceRegistry` | `DeviceRegistry.execute_action` |
| Phone-bridged BLE peripheral | `peripheral_bridge_register` (`server.py:2509–2556`) | Manifest registered on brain; phone owns transport |
| Discovered but not connected | `device_announce` (`mesh.py:339`) | KG + UI only until adapter attached |

### 4.3 CuteBot native schema (reference)

`QtBot.capabilities()` (`cuteferalbot/cutebot/device.py:46–65`) returns a simpler dict — the adapter should **translate** this into `DeviceManifest` at registration time, not expose two schemas to the LLM.

---

## 5. Command Translation Layer

### 5.1 Problem

User intent: *"Move forward slowly until you detect an obstacle"* does not map 1:1 to CuteBot commands. CuteBot firmware exposes **behaviors** (`follow_line`, `explore`, `drive`, `halt`) not odometry (`DEVICE_API.md:84–86`).

### 5.2 Proposed `HardwareOrchestrator` (net new)

```python
# hardware/orchestrator.py (proposed)

class HardwareOrchestrator:
    def __init__(self, registry: DeviceRegistry, ledger: CommandLedger, perception: PerceptionEngine):
        ...

    async def execute_intent(
        self,
        session_id: str,
        device_id: str,
        intent: str,
        *,
        params: dict | None = None,
        stop_conditions: list[dict] | None = None,
        timeout_s: float = 30.0,
    ) -> dict:
        """
        1. Resolve device + manifest capabilities
        2. LLM or rule table maps intent → command plan (list of steps)
        3. For each step: registry.execute_action + poll adapter events
        4. Stop on: obstacle, gave_up, timeout, halt, disconnect
        5. Update perception frame with robot state
        """
```

**Example plan for CuteBot** (rule-based v1, LLM planner v2):

| User intent | Translated sequence |
|-------------|---------------------|
| "Patrol the track" | `follow_line` → poll until `state_changed:gave_up` or timeout → `halt` |
| "Explore the table carefully" | `explore` → poll `obstacle` events → optional notify user (firmware already avoids) |
| "Back up a bit" | `drive(left=-30, right=-30)` × 1s → `halt` |
| "Stop everything" | `halt` (priority: safety) |

**Stop condition schema (proposed):**

```json
{"type": "event", "name": "obstacle", "param": "distance_cm", "op": "<", "value": 20}
{"type": "event", "name": "state_changed", "param": "state", "op": "==", "value": "gave_up"}
{"type": "timeout", "seconds": 15}
{"type": "sensor", "param": "sonar_cm", "op": "<", "value": 10}
```

**Reuse:** `CommandEnvelope` + `CommandLedger` for each step (`command_contract.py:39–50`); `TaskFlowRuntime` for durable multi-minute patrols (optional phase 2).

---

## 6. Feedback Loop → World State

### 6.1 Existing pipeline (wearables)

```
WS device_event (heart_rate, spo2, gesture, …)
    → api/server.py:2766–2794
    → _handle_biometric_device_event (server.py:3389–3514)
    → perception.update_sensors(session_id, sensors)
    → PerceptionFrame.to_system_context()
```

**Example HUP frame** (heart rate):

```json
{
  "type": "device_event",
  "payload": {
    "event_type": "heart_rate",
    "bpm": 72,
    "source": "veepoo_wristband",
    "sample_ts": 1743000000.0
  }
}
```

### 6.2 Proposed robot feedback path

**Option A (recommended for phase 1):** Brain-side telemetry task in `CuteBotAdapter`:

```
QtBot.poll_events(1.0) / status()
    → normalize to sensors dict
    → perception.update_sensors(session_id, {"robot": {...}})
    → optional: memory.episode_save on significant events (gave_up, battery low)
    → broadcast state_push to WebUI (/api/hardware/mesh enrichment)
```

**Option B (end-state for wireless):** Bridge node emits HUP `device_event`:

```json
{
  "type": "device_event",
  "payload": {
    "event_type": "robot_telemetry",
    "device_id": "cutebot-usb-0",
    "mode": "line_follow",
    "state": "ok",
    "sonar_cm": 14.0,
    "battery": true
  }
}
```

Requires **exists, needs change:** extend `server.py:2778–2794` dispatch table to handle `robot_telemetry`, `robot_event` event types (currently unknown types are dropped at line 2791–2794).

### 6.3 PerceptionFrame extension

**Exists, needs change:** Add optional robot block to `PerceptionFrame` (`fusion.py:134–178`) or use generic nested sensor keys:

```python
# fusion.py update_sensors — proposed branch
robot = sensors.get("robot", {})
if robot:
    frame.robot_mode = robot.get("mode", "")
    frame.robot_state = robot.get("state", "")
    frame.robot_sonar_cm = robot.get("sonar_cm", 0)
    frame.robot_online = robot.get("online", False)
```

Include in `to_system_context()` so the LLM sees: *"Robot: line_follow, ok, sonar=14cm, online"*.

---

## 7. Safety

### 7.1 Existing mechanisms (reuse)

| Mechanism | Location | CuteBot applicability |
|-----------|----------|----------------------|
| **Autonomy modes** | `ToolRunner` strict/hybrid/loose (`tool_runner.py:100–102`) | Gate `drive`, `explore`, `follow_line` as CONFIRM in hybrid |
| **ApprovalManager** | `security/exec_approvals.py` | Standing approvals per session |
| **Policy resolver** | `security/safety_resolver.py` | `robot_ext__robot_move` → CONFIRM; speed > 80 → DENY (`safety_resolver.py:103–104`) |
| **Sandbox policy** | `SandboxPolicy` via `/api/hardware/execute` | `can_read_sensor` check (`security_and_hardware.py:127–128`) |
| **Command timeout** | `HardwareMesh.invoke` default 10s (`mesh.py:217`); ledger `TIMED_OUT` | Apply to behavior sequences |
| **Emergency stop** | `RobotArmAdapter` `estop` capability | Map to CuteBot `halt` — always allowed, bypass approval |
| **Firmware safety** | CuteBot autonomous reflexes | Edge, obstacle, tilt — **cannot be disabled** (`DEVICE_API.md:78–80`) |

### 7.2 Proposed CuteBot-specific rules (net new config)

```yaml
# ~/.feral/hardware/cutebot.yaml (proposed)
demo_mode: true          # blocks drive(); allows follow_line/explore/halt only
max_drive_speed: 40      # clamp |left|,|right| in adapter
command_timeout_s: 30
require_battery_ok: true # refuse motion if status.battery == false
disconnect_policy: halt  # send halt on USB disconnect if last mode was autonomous
```

**Tool naming:** Prefer `cutebot__follow_line` with manifest `safety_tier: confirm` over generic `robot_ext__robot_move` to avoid WS daemon confusion.

### 7.3 Connection failure

| Scenario | Behavior |
|----------|----------|
| USB unplugged | `QtBot.status()` → `online: false` (`device.py:114–115`); adapter marks device disconnected; perception clears robot_online |
| Command timeout | `CommandLedger` → `TIMED_OUT`; orchestrator reports failure to user |
| `gave_up` event | Surface to user — robot needs repositioning on track (`DEVICE_API.md:69`) |
| Battery off | Commands ack but wheels won't move (`DEVICE_API.md:71`) — adapter checks `battery` before motion |

---

## 8. Folder / Code Structure

### 8.1 Current `hardware/` layout

```
ASOS/feral-core/hardware/
├── __init__.py
├── protocol.py           # DeviceRegistry, HUPAction, DeviceManifest  ✅ reuse
├── mesh.py               # HardwareMesh, WebSocketNodeAdapter         ✅ reuse
├── command_contract.py   # CommandLedger, NodeHealth                   ✅ reuse
├── mock_roomba.py        # Demo vacuum                               ✅ pattern to follow
└── adapters/
    ├── __init__.py
    ├── robot_arm.py      # Reference dangerous-tier adapter            ✅ template
    ├── wristband.py
    └── smart_home.py
```

### 8.2 Proposed additions

```
ASOS/feral-core/hardware/
├── orchestrator.py              # NEW — intent → command sequences
├── discovery.py                 # NEW — USB scan, optional mDNS
├── transports/
│   ├── __init__.py
│   └── serial_transport.py      # NEW — thin wrapper over pyserial (optional)
└── adapters/
    └── cutebot.py                 # NEW — CuteBotAdapter wraps QtBot

ASOS/feral-core/skills/
├── manifests/cutebot.json       # NEW — LLM tools for QtBot behaviors
└── impl/cutebot_skill.py        # NEW — routes to DeviceRegistry (LOCAL method)

# Dependency (pick one):
# - pip install -e ~/Desktop/cuteferalbot
# - git submodule ASOS/feral-core/vendor/cuteferalbot
```

**Do not** duplicate `cutebot/serial_client.py` inside feral-core — import `cuteferalbot.device.QtBot` per `DEVICE_API.md:88–95`.

### 8.3 Boot wiring (exists, needs change)

In `BrainState._boot_subsystems` after `HardwareMesh`:

```python
# Pseudocode — api/state.py
from hardware.discovery import discover_brain_local_devices
from hardware.adapters.cutebot import CuteBotAdapter

for adapter in discover_brain_local_devices():
    self.device_registry.register_device(adapter.manifest, adapter)
    asyncio.create_task(adapter.start_telemetry_loop(self.perception, self.sessions))
```

---

## 9. Demo Path (Phase 1)

### 9.1 Transport recommendation

| Option | Demo day | End state |
|--------|----------|-----------|
| **USB serial** | ✅ **Use this** — robot already cabled; `QtBot.available()` detects micro:bit VID/PID (`serial_client.py:18–40`) | Acceptable for desk demos |
| **BLE** | ❌ Not implemented in cuteferalbot | Would require firmware + bridge node |
| **HUP WS bridge** | ⚠️ Overkill for day 1 | Pi/phone owns USB/BLE, brain uses existing `HardwareMesh.invoke` |

**Critical hardware note** (`README.md:20–21`): Motors do **not** run from USB power — CuteBot battery switch must be ON.

### 9.2 Phase 1 demo script (15-minute proof)

1. **Flash firmware** — `./flash.sh` in cuteferalbot (already done).
2. **Start FERAL brain** — verify boot report shows `HardwareMesh` + new `CuteBotAdapter` (once implemented).
3. **Verify registration** — `GET /api/hardware/devices` returns `cutebot-usb-0` with capabilities; `GET /api/hardware/context` lists follow_line/explore/halt in LLM context string (`protocol.py:221–234`).
4. **Natural language** — User: *"Hey FERAL, start line following on the QtBot"*
   - Orchestrator routes to `cutebot` skill
   - Approval card (hybrid mode) → user confirms
   - `CuteBotAdapter.execute(follow_line)` → USB `A,F,OK` ack
5. **Feedback** — Telemetry task updates perception; user asks *"How's the robot doing?"* → LLM reads `sonar_cm`, `state` from context.
6. **Stop** — *"Stop the robot"* → `halt` without extra approval (passive tier).
7. **Explain** — LLM summarizes: mode, sonar, any `gave_up` event from episodic memory.

### 9.3 Manual API smoke test (before LLM)

```bash
curl -s localhost:9090/api/hardware/execute -H 'Content-Type: application/json' -d '{
  "device_id": "cutebot-usb-0",
  "capability_id": "follow_line",
  "action_type": "execute",
  "parameters": {}
}'
```

---

## 10. Implementation Plan, Risks, Adapter Split

### 10.1 Phased plan

| Phase | Scope | Effort | Outcome |
|-------|-------|--------|---------|
| **1 — Demo** | `CuteBotAdapter`, boot USB discovery, `cutebot.json` skill, telemetry → perception, REST execute | 2–3 days | End-to-end NL → robot → feedback on USB |
| **2 — Orchestrator** | `HardwareOrchestrator`, stop conditions, demo_mode config, fix `DeviceRegistry.execute_action` HUPResult handling | 2 days | Intent-level commands with sensor monitoring |
| **3 — UI** | Lane 12 Devices page shows CuteBot status; live telemetry via `state_push` | 1 day | Operator visibility |
| **4 — Wireless** | Pi/phone HUP bridge node wrapping QtBot; `device_event` robot types | 3–5 days | Unplug USB; reuse `HardwareMesh` |
| **5 — Generalize** | Adapter plugin loader, MQTT→HUP mapping, second device type | ongoing | IoT scale |

### 10.2 CuteBot-specific vs generic

| Component | Generic (all hardware) | CuteBot-specific |
|-----------|------------------------|------------------|
| `DeviceRegistry`, `DeviceManifest`, `HUPAction` | ✅ | |
| `CommandLedger`, `NodeHealth` | ✅ | |
| `HardwareOrchestrator` | ✅ | |
| `ManagedDeviceAdapter` interface | ✅ | |
| `CuteBotAdapter` | | ✅ maps HUP caps → `QtBot.execute` |
| Manifest / skill `cutebot.json` | | ✅ behavior names, safety notes |
| USB VID/PID discovery | | ✅ `0x0D28/0x0204` |
| Telemetry normalization | ✅ pattern | ✅ sonar/line/mode/phase fields |
| Command translation rules | ✅ framework | ✅ no odometry; behavior-oriented |
| Transport | ✅ serial transport interface | ✅ 115200 protocol in cuteferalbot |

### 10.3 Risks & blockers

| Risk | Severity | Mitigation |
|------|----------|------------|
| `DeviceRegistry.execute_action` assumes adapter returns `dict` but `RobotArmAdapter` returns `HUPResult` | Medium | Normalize in registry (phase 2) |
| `robot_ext` WS path confuses operators | Medium | New `cutebot` skill; document difference |
| PerceptionFrame lacks robot fields | Low | Phase 1: nested `sensors.robot`; phase 2: typed fields |
| USB-only tethers robot to Mac | Demo OK | Phase 4 bridge node |
| Dark table / wrong track surface | Demo | Use official white-track map; README warns (`cuteferalbot/README.md:28–30`) |
| Battery off → silent motor failure | Demo | Pre-flight `status()` check in adapter |
| Unknown `device_event` types dropped | Medium for WS path | Add `robot_*` types to dispatcher |
| `robot_action.json` missing `daemon_node_type` | Low | Defaults to `"robot"` in executor (`executor.py:404`) |

---

## Appendix A: Key Code References

| Topic | File:lines |
|-------|------------|
| HUP device manifest schema | `hardware/protocol.py:38–71` |
| DeviceRegistry.execute_action | `hardware/protocol.py:171–219` |
| HardwareMesh.invoke lifecycle | `hardware/mesh.py:212–310` |
| WS node auto-registration | `hardware/mesh.py:167–203` |
| device_announce ingest | `hardware/mesh.py:339–413` |
| node_register handler | `api/server.py:1783–1828` |
| device_event dispatch | `api/server.py:2766–2794` |
| Biometric → perception | `api/server.py:3389–3514` |
| robot_ext manifest | `skills/manifests/robot_action.json` |
| WS_EXECUTE dispatch | `skills/executor.py:400–465` |
| Robot safety (speed > 80 deny) | `security/safety_resolver.py:103–104` |
| ToolRunner approval flow | `agents/tool_runner.py:193–279` |
| REST hardware execute | `api/routes/security_and_hardware.py:114–131` |
| Gateway hardware.execute | `gateway/protocol.py:498–511` |
| PerceptionFrame | `perception/fusion.py:134–178` |
| QtBot device API | `cuteferalbot/cutebot/device.py:31–172` |
| USB serial protocol | `cuteferalbot/cutebot/serial_client.py:1–80` |
| DEVICE_API contract | `cuteferalbot/DEVICE_API.md` |

---

## Appendix B: Status Legend

| Tag | Meaning |
|-----|---------|
| ✅ **Already exists, reuse** | Ship as-is or call directly |
| ⚠️ **Exists, needs change** | Extend or fix before relying on it |
| ❌ **Net new** | Greenfield code |

---

*Document produced by architecture investigation. No source code was modified.*
