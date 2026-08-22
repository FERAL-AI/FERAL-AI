# FERAL Architecture Overview

FERAL decouples the reasoning engine (Brain) from the physical executors (Nodes) and visual layer (Client).

## The Tri-Node Paradigm

FERAL uses three main components communicating strictly via our JSON WebSocket Protocol.

1. **FERAL Brain (`/feral-core`)**: The absolute center of authority. It retains long-term memory, maintains conversation sliding windows, and drives the LLM tool invocation loop.
2. **FERAL Client (`/feral-client-v2`)**: A "dumb" renderer. It connects via `ws://.../v1/session` to provide audio/text input. It simply renders the SDUI payloads requested by the Brain.
3. **Hardware Nodes (`/feral-nodes`)**: Actuators and Sensors. They connect via `ws://.../v1/node`. They register "Capabilities" with the Brain upon connection.

## Agentic Loop Lifecycle

Below is the standard lifecycle of a single user request:

1. **Input Phase**: User says "Run diagnostics on the arm."
2. **Context Compaction**: The Brain gathers the recent sliding-window context up to maximum token limits, and queries current streaming telemetry (e.g., HR `95bpm`).
3. **Semantic Tool Routing (RoutePrompt)**: Before dumping 100+ hardware instructions into the LLM context, the orchestrator invokes a cheap classifier LLM call to select only the top 5 relevant sub-skills (e.g., `['robot_diagnostic']`).
4. **LLM Invocation**: The Orchestrator passes the selected tools and context to the LLM.
5. **Safety Hook Evaluation (Pre-Tool Use)**: The LLM selects `robot_ext__robot_move(speed: 100)`.
   - The Orchestrator catches the tool invocation stream.
   - The `PermissionDenial` hook identifies the action exceeds a safe threshold.
   - The stream is instantly aborted and an SDUI exception card is sent to the client requesting user verification.
6. **Execution Pipeline**: If approved, the Orchestrator maps the tool to an async node execution payload, sends it to the Hardware Daemon, and waits for `stdout`.
7. **Synthesis Phase**: Results return. The orchestrator instructs the LLM to format the success state as an SDUI visual packet, dispatching it perfectly to the client.

## Node WebSocket Protocol

All messages adhere to a lightweight schema, preventing packet-fragmentation issues found in bloated RPC environments.

**Daemon Registration:**
```json
{
  "hop": "daemon",
  "type": "node_register",
  "payload": {
    "node_id": "robot_123",
    "node_type": "actuator",
    "capabilities": ["telemetry", "robot_move", "robot_grip"]
  }
}
```

**Brain to Daemon Command Execution:**
```json
{
  "type": "command",
  "request_id": "req_881",
  "command": "robot_move",
  "args": {
    "direction": "forward",
    "speed": 35
  }
}
```

**Daemon to Brain Execution Result:**
```json
{
  "hop": "daemon",
  "type": "execute_result",
  "payload": {
    "request_id": "req_881",
    "success": true,
    "data": {"stdout": "Robot chassis verified translation."},
    "error": ""
  }
}
```

## Security Posture

We use explicitly scoped variables (e.g., `.env`) without leaking system-level variables to spawned threads. All safety evaluations are processed by pure Python hooks *before* bridging network connections locally.
