# Hardware Unification Protocol (HUP) — Public Specification

**Version:** `HUP v1.3.0`
**Status:** Stable
**License:** Apache-2.0
**Canonical schemas:** this file (normative) + Pydantic mirror in
`feral-nodes/python-node-sdk/src/feral_node_sdk/schemas.py` + Zod mirror in
`feral-nodes/ts-node-sdk/src/schemas.ts` + Swift mirror in
`feral-nodes/ios-node-sdk/Sources/FeralNodeSDK/HUPFrame.swift`.

Every surface that participates in HUP MUST advertise `1.3.0` as
`hup_version` in every outbound envelope. The five surfaces that the
`feral-core/tests/test_hup_version_unified.py` CI test pins together
are: this spec, `feral-core/models/protocol.py` (`HUP_VERSION`), the
Python node SDK (`feral_node_sdk.schemas.HUP_VERSION`), the TypeScript
node SDK (`HUP_VERSION` in `schemas.ts`), and the iOS node SDK
(`FeralNodeSDKInfo.hupVersion`). The iOS companion app surfaces the
same value via `Info.plist` `FERALHUPVersion`.

HUP is FERAL's public wire contract between a "brain" (the FERAL orchestrator
runtime) and a "node daemon" (a process running on or near a piece of
hardware). It is the equivalent, for heterogeneous hardware, of what the USB
HID class spec was for input devices: a stable, versioned, vendor-neutral
protocol that lets any vendor plug hardware into any FERAL brain without
proprietary glue.

If you can terminate TLS and speak JSON over WebSocket, you can speak HUP.

---

## 1. Overview and Versioning

- HUP is a JSON message protocol carried over a single persistent WebSocket.
- Versioning follows semantic versioning (`MAJOR.MINOR.PATCH`):
  - **MAJOR** — breaking changes to message envelopes, handshake, or
    required field types. Clients MUST negotiate (see `node_register`).
  - **MINOR** — additive fields, new message types, or new capability
    categories. Clients MUST ignore unknown fields and unknown message
    types (forward-compatibility requirement).
  - **PATCH** — clarifications, non-normative edits.
- Daemons announce the spec they were built against in `node_register.hup_version`.
  Brains SHOULD accept any `HUP v1.*` daemon but MAY reject `HUP v2.*` with
  error code `1002 bad_schema`.
- **Backward-compat rule:** once a field is published in a minor version, it
  stays. New fields MUST be optional.

| Version | Status | Additions |
|---|---|---|
| `v1.3.0` | Stable | Phone-as-peer envelopes (§5.9): `chat_request`, `chat_response`, `voice_session_start`, `voice_interrupt`, `genui_push`, `genui_event`, `peripheral_bridge_register`, `backchannel_request`, `ambient_transcript` + `ambient_transcript_ack`, and the digest return leg `ambient_digest_request` (phone → brain) + `ambient_digest` (brain → phone). Strict Pydantic-v2 schemas: literal-typed `chat_request.reply_mode` + `chat_request.channel`, required `session_id` on `voice_session_start`, required `stream_id` + `channels` on `audio_chunk`. Smart-glasses vision streaming via `glasses_frame` (§5.4.3) + per-device circular buffer in `feral-core/perception/glasses_buffer.py`. Hardware peripheral memory via `device_announce` (§5.4.4) routed through `feral-core/hardware/mesh.py` into the knowledge graph. |
| `v1.2.0` | Stable | Canonical `node_ack`, `node_heartbeat`, `hup_action_request`, `hup_action_response`, and `node_bye` handling (§5.2-§5.8). |

---

## 2. Transport

| Property            | Value                                                        |
|---------------------|--------------------------------------------------------------|
| URL                 | `wss://<brain-host>:<port>/v1/node`                          |
| Subprotocol         | `feral.hup.v1` (optional — for middlebox negotiation)        |
| TLS                 | Required on non-loopback addresses. `ws://` allowed only on `localhost`, `127.0.0.1`, or `::1`. |
| Message format      | JSON text frames, UTF-8, one message per frame.              |
| Max frame size      | 1 MiB. `device_event` frames carrying binary (base64) MUST stay ≤ 512 KiB of decoded payload. |
| Connections         | Exactly **one** persistent WS per `node_id`. A second connect with the same `node_id` kicks the first. |
| Reconnect           | Client MUST reconnect with jittered exponential backoff: initial 100 ms, factor 2, cap 30 s, full jitter. |
| Keepalive           | `node_heartbeat` every `heartbeat_ms` (default 10000). Brain MAY close with `1001 unauthorized` style code `4004 stale_heartbeat` if 3× interval elapses with no frame. |

---

## 3. Handshake Sequence

```
daemon                                    brain
  |  --- WS upgrade (Authorization: Bearer <key>) --->
  |  <-- 101 Switching Protocols ----------------------
  |  --- node_register --------------------------->
  |  <-- node_ack  {node_id,session_token,heartbeat_ms}
  |
  |  === steady state ===
  |  --> node_heartbeat (every heartbeat_ms)
  |  --> device_event  (sensor pushes)
  |  <-- hup_action_request
  |  --> hup_action_response
  |  ...
  |  --> node_bye                 (graceful shutdown)
  |  <-- TCP FIN
```

1. Client opens WS. First message MUST be `node_register`.
2. Brain validates schema + credentials and replies with `node_ack` within
   5 s, or closes the socket with one of the error codes in §8.
3. After `node_ack`, either side MAY send any valid post-handshake message
   (`device_event`, `hup_action_request`, `hup_action_response`,
   `node_heartbeat`, `node_bye`).
4. If the daemon does not receive `node_ack` within 5 s it MUST close and
   reconnect with backoff.

---

## 4. Pairing and Authentication

HUP separates **first-time pairing** (how the daemon gets a long-lived API
key) from **steady-state auth** (how it authenticates each WS session).

### 4.1 First-time pairing

1. On first launch the daemon generates and prints a **6-digit numeric
   code** (uniform random, leading zeros preserved). Example: `417 392`.
2. The user opens the FERAL UI → **Settings → Devices → Pair**, types the
   6-digit code, optionally a friendly name (e.g. "Acme Wristband"), and
   hits *Pair*.
3. The brain calls `POST /api/devices/pair` with
   `{"code":"417392","name":"Acme Wristband","node_id":"acme-wb-001"}`
   and returns `{"token":"<api-key>","device_id":"..."}`.
4. The daemon polls `GET /api/devices/pair/status?code=417392` (or receives
   the token over mDNS — see §4.3) until it gets the token.
5. The daemon persists the token to `~/.feral/node-keys/<safe>.key`
   (mode `0600`) and forgets the 6-digit code. `<safe>` is **not** the raw
   `node_id`, see §4.1.1.

#### 4.1.1 Key filename derivation

An SDK MUST derive `<safe>` from `node_id` as follows, and MUST NOT invent its
own sanitisation. This section exists because it was previously left to prose:
the Python SDK dropped disallowed characters, the TypeScript SDK replaced them
with `_`, and this document said `<node_id>.key` with no sanitisation at all,
so the same node paired through one SDK re-paired under the other.

1. `sanitised` = `node_id` with every character **outside** the class
   `[A-Za-z0-9._:-]` replaced by `_`. The class is exactly the `node_id`
   pattern the brain accepts, and it is **ASCII**. Do not use a
   Unicode-aware "is alphanumeric" test: `é` and `日` are letters to such a
   test and are not members of this class. Replace per **code point**, not
   per UTF-16 code unit, or an emoji becomes two `_` instead of one (in
   JavaScript this means the `u` regex flag).
2. If `sanitised == node_id` **and** `1 <= len(node_id) <= 128`, then
   `<safe>` = `sanitised`. Every `node_id` the brain accepts takes this
   branch, so a paired node's filename never moves.
3. Otherwise `<safe>` = `sanitised` truncated to 128 characters, then `-`,
   then the first 8 hex characters of `sha256(node_id` encoded UTF-8`)`.

Step 3 is not decoration. Sanitising alone is many-to-one: without it `a b`
and `a_b` both resolve to `a_b.key`, and every all-punctuation `node_id`
resolves to a hidden file literally named `.key`. Two nodes sharing a key file
means one silently overwrites the other's API key.

`sanitised` is always pure ASCII after step 1, so truncation is unambiguous
whether the SDK's language counts code points, UTF-16 units, or bytes.

Conformance fixtures, which every SDK MUST pass:
`feral-nodes/spec-fixtures/node_key_filename.json`.

| `node_id` | `<safe>.key` |
|---|---|
| `wristband-01` | `wristband-01.key` |
| `acme:wb:001` | `acme:wb:001.key` |
| `sensor 01` | `sensor_01-46977d17.key` |
| `sensor_01` | `sensor_01.key` |
| `café` | `caf_-850f7dc4.key` |
| `日本語ノード` | `______-a30a928b.key` |

The pairing window is 5 minutes; codes expire after that or after one
successful redemption, whichever comes first. Failed codes rate-limit at
5 attempts / 5 minutes per source IP.

### 4.2 Steady-state

Every WS upgrade MUST carry the API key in exactly one of:

- `Authorization: Bearer <key>` header (preferred).
- `?api_key=<key>` query parameter (fallback for clients that cannot set
  headers, e.g. browser `WebSocket`).
- `Sec-WebSocket-Protocol: feral-token-<key>` (fallback for environments
  that only expose the subprotocol hook).

### 4.3 Discovery (mDNS)

Brains SHOULD advertise `_feral-brain._tcp.local.` with TXT records:

```
version=1
node_path=/v1/node
tls=1
```

Node SDKs SHOULD prefer discovered brains on the local network over any
hard-coded URL.

---

## 5. Message Envelope

Every HUP frame is a JSON object with:

```json
{
  "hup_version": "1.0.0",
  "type": "<message-type>",
  "ts": 1734369922.123,
  "payload": { ... }
}
```

- `type` — one of the types below. Unknown types MUST be ignored (not
  errored).
- `ts` — seconds since Unix epoch, float, millisecond precision.
- `payload` — per-message schema.

### 5.1 `node_register` (daemon → brain, first frame)

```json
{
  "hup_version": "1.0.0",
  "type": "node_register",
  "ts": 1734369920.001,
  "payload": {
    "node_id": "acme-wb-001",
    "node_type": "wearable",
    "name": "Acme Wristband",
    "manufacturer": "Acme Corp",
    "model": "WB-1",
    "firmware_version": "1.2.3",
    "platform": "zephyr",
    "os": "",
    "capabilities": ["heart_rate", "accelerometer", "buzzer", "battery"],
    "sensors": ["heart_rate", "accelerometer", "battery"],
    "actuators": ["buzzer"],
    "location": "wrist",
    "tags": ["wearable", "health"]
  }
}
```

JSON Schema:

```json
{
  "$id": "https://feral.ai/schemas/hup/v1/node_register.json",
  "type": "object",
  "required": ["node_id", "node_type", "capabilities"],
  "properties": {
    "node_id":          {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"},
    "node_type":        {"type": "string", "enum": [
      "desktop", "server", "rpi", "robot", "glasses", "phone",
      "actuator", "sensor", "wearable", "camera", "vehicle", "appliance"
    ]},
    "name":             {"type": "string", "maxLength": 128},
    "manufacturer":     {"type": "string", "maxLength": 128},
    "model":            {"type": "string", "maxLength": 128},
    "firmware_version": {"type": "string", "maxLength": 64},
    "platform":         {"type": "string"},
    "os":               {"type": "string"},
    "capabilities":     {"type": "array", "items": {"$ref": "#/$defs/capability"}},
    "sensors":          {"type": "array", "items": {"type": "string"}},
    "actuators":        {"type": "array", "items": {"type": "string"}},
    "location":         {"type": "string"},
    "tags":             {"type": "array", "items": {"type": "string"}}
  },
  "$defs": {
    "capability": {"type": "string", "enum": [
      "heart_rate", "spo2", "temperature", "uv", "accelerometer",
      "gyroscope", "ambient_light", "steps", "battery",
      "gps", "microphone", "camera",
      "display", "speaker", "haptic", "buzzer", "led", "motor",
      "relay", "valve", "keyboard", "applescript", "filesystem",
      "gpio", "shell", "telemetry", "passive_sensor", "active_actuator"
    ]}
  }
}
```

The capability vocabulary is derived verbatim from the `sensors`/
`actuators` and `category` fields in `ASOS/feral-core/hardware/protocol.py`
and from the raw capability string list in `NodeRegisterPayload`
(`ASOS/feral-core/models/protocol.py`). New vendors MAY add capability
strings outside the enum, but brains MAY ignore unknown capabilities for
gating. Each capability string maps to a **tier** for policy purposes:

| Tier                | Examples                                  | Default allowed |
|---------------------|-------------------------------------------|-----------------|
| `passive_sensor`    | heart_rate, spo2, temperature, accelerometer, ambient_light, battery | yes |
| `camera`            | camera                                    | requires user opt-in |
| `audio`             | microphone, speaker                       | requires user opt-in |
| `active_actuator`   | haptic, buzzer, led, display              | yes, rate-limited |
| `motor`             | motor, relay, valve, vehicle              | off by default — per-command confirmation |

### 5.2 `node_ack` (brain → daemon, REQUIRED)

Brain MUST reply to every valid `node_register` with a `node_ack`
within 5 seconds, or close the socket with an error code from §8.

```json
{
  "hup_version": "1.2.0",
  "type": "node_ack",
  "ts": 1734369920.040,
  "payload": {
    "node_id": "acme-wb-001",
    "session_token": "b58c2c34-...",
    "heartbeat_ms": 10000,
    "server_time": 1734369920.040,
    "granted_capabilities": ["heart_rate", "buzzer", "battery"],
    "denied_capabilities":  ["camera"]
  }
}
```

### 5.3 `node_heartbeat` (daemon → brain, every `heartbeat_ms`, canonical)

```json
{
  "hup_version": "1.0.0",
  "type": "node_heartbeat",
  "ts": 1734369930.000,
  "payload": {
    "ts": 1734369930.000,
    "battery_pct": 87,
    "rssi": -54
  }
}
```

Fields:

- `ts` (float, required) — daemon-local timestamp.
- `battery_pct` (int 0–100, optional).
- `rssi` (int, dB, optional) — radio signal strength if applicable.

### 5.4 `device_event` (daemon → brain)

```json
{
  "hup_version": "1.0.0",
  "type": "device_event",
  "ts": 1734369931.210,
  "payload": {
    "node_id": "acme-wb-001",
    "event_type": "heart_rate",
    "data": {"bpm": 72, "confidence": 0.94},
    "ts": 1734369931.210
  }
}
```

JSON Schema:

```json
{
  "$id": "https://feral.ai/schemas/hup/v1/device_event.json",
  "type": "object",
  "required": ["node_id", "event_type", "data", "ts"],
  "properties": {
    "node_id":    {"type": "string"},
    "event_type": {"type": "string",
                   "description": "Capability or sensor identifier.",
                   "examples": ["heart_rate","spo2","temperature","accelerometer","button_press","camera_frame","microphone_chunk"]},
    "data":       {"type": "object"},
    "ts":         {"type": "number"}
  }
}
```

Conventions for common events:

| `event_type`       | `data` shape                                                           |
|--------------------|------------------------------------------------------------------------|
| `heart_rate`       | `{"bpm": int, "confidence": float?}`                                   |
| `spo2`             | `{"current": int, "high": int?, "low": int?}`                          |
| `temperature`      | `{"celsius": float}`                                                   |
| `accelerometer`    | `{"x": float, "y": float, "z": float}`                                 |
| `button_press`     | `{"button": str, "pressed": bool, "count": int?}`                      |
| `camera_frame`     | `{"encoding": "jpeg", "resolution": [w,h], "data_b64": str (≤512KB)}`  |
| `microphone_chunk` | `{"encoding": "pcm16", "sample_rate": int, "data_b64": str}`           |
| `audio_frame`      | v1.1 media frame — see §5.4.1                                           |
| `video_frame`      | v1.1 media frame — see §5.4.2                                           |

`camera_frame` and `microphone_chunk` remain valid for v1.0.0 daemons.
New daemons SHOULD emit `audio_frame` / `video_frame` instead — those
names are first-class in v1.1 with explicit codec + sequence fields
for jitter buffering.

### 5.4.1 `audio_frame` (v1.1+)

Push audio samples from a daemon (glasses, wristband, phone-bridge,
room mic) to the brain. Rides inside the existing `device_event`
envelope; only `payload.event_type` and `payload` shape are new.

```json
{
  "hup_version": "1.1.0",
  "type": "device_event",
  "ts": 1734369931.210,
  "node_id": "feral-w300-0001",
  "seq": 842,
  "payload": {
    "event_type": "audio_frame",
    "codec": "opus",
    "sample_rate": 24000,
    "channels": 1,
    "frame_ms": 20,
    "sequence": 842,
    "data_b64": "…base64(opus packet)…"
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `codec` | `"opus" \| "pcm16"` | yes | Opus strongly preferred over wireless links |
| `sample_rate` | int | yes | Hz; SHOULD be 16000 or 24000 |
| `channels` | int | yes | 1 or 2 |
| `frame_ms` | int | no, default 20 | Duration of this frame |
| `sequence` | int | yes | Per-stream monotonic counter for jitter buffer |
| `data_b64` | string | yes | Base64 of the raw codec payload. Decoded size MUST be ≤ 64 KiB. |

Brain behaviour: sequence-number reorder buffer with ≤ 200 ms tolerance;
drop frames older than that. Route to `state.audio.ingest_frame(node_id, payload)`.

### 5.4.2 `video_frame` (v1.1+)

Push JPEG or H.264 video frames from a camera-capable node.

```json
{
  "hup_version": "1.1.0",
  "type": "device_event",
  "ts": 1734369931.250,
  "node_id": "feral-w300-0001",
  "seq": 843,
  "payload": {
    "event_type": "video_frame",
    "codec": "jpeg",
    "width": 1280,
    "height": 720,
    "sequence": 127,
    "keyframe": true,
    "data_b64": "…base64(frame)…"
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `codec` | `"jpeg" \| "h264"` | yes | JPEG easiest for glasses at 2-5 fps; H.264 for higher rates |
| `width` | int | yes | Pixels |
| `height` | int | yes | Pixels |
| `sequence` | int | yes | Per-stream monotonic counter |
| `keyframe` | bool | H.264 only | Required for H.264; ignored for JPEG (always keyframe) |
| `data_b64` | string | yes | Base64 of the codec payload. Decoded size MUST be ≤ 512 KiB per §2. |

Brain behaviour: drop non-keyframes that arrive before the first
keyframe of an H.264 stream. Route every decoded frame into
`state.vision_buffer.push(node_id, payload)`. Every 10 s, run a
vision-LLM caption on the most recent frame and store it in episodic
memory.

### 5.4.3 `glasses_frame` (v1.3.0+)

A first-class envelope for smart-glasses (and glasses-equivalent
phone-camera-fallback) vision streams. The brain stores incoming frames
in a per-device circular buffer (`feral-core/perception/glasses_buffer.py`)
that the orchestrator's vision-context-attach reads when the active
turn is in voice mode and recent frames exist. Existing `video_frame`
remains valid for non-glasses cameras (e.g. `feral-w300` USB UVC) —
the brain treats `glasses_frame` as the canonical channel for
"vision context for the assistant" and `video_frame` as the generic
camera channel.

```json
{
  "hup_version": "1.3.0",
  "type": "glasses_frame",
  "ts": 1734369931.250,
  "msg_id": "f2c3e1a2-...",
  "payload": {
    "device_id": "feral-iphone-abc123",
    "timestamp": 1734369931.123,
    "encoding": "jpeg",
    "data_b64": "…base64(frame)…",
    "width": 1280,
    "height": 720,
    "source": "camera_fallback",
    "sequence": 42
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `device_id` | string | yes | Stable id of the glasses (or glasses-equivalent) device. May differ from the HUP `node_id` that forwarded the frame — a phone forwarding W610 frames carries the phone as `node_id` and `w610-<serial>` as `device_id`. |
| `timestamp` | float | yes | Capture time (unix epoch seconds). Drives the 30s freshness gate in `context_attach.py`. |
| `encoding` | `"jpeg" \| "png" \| "webp"` | yes | JPEG is the recommended default for cost-budgeted vision-LLM input. |
| `data_b64` | string | yes | Base64 of the encoded image. Decoded size MUST be ≤ 512 KiB per §2 (shared with `video_frame`). |
| `width` | int | no | Pixels. Used for downscale heuristics. |
| `height` | int | no | Pixels. |
| `source` | string | no | Provenance label. One of `glasses`, `phone_camera`, `screen_loop`, `w610`, `camera_fallback`, `jw_w300`, `browser_camera`. Unknown values are accepted and forwarded verbatim. |
| `sequence` | int | no | Per-device monotonic counter (helps the buffer dedupe replays). |

Brain behaviour:

- Reject frames with decoded size > 512 KiB with HUP error code 4020
  (same cap as `video_frame`).
- Ingest accepted frames into `state.glasses_buffer.push(device_id,
  GlassesFrame(...))`. The buffer keeps the last 30 frames per
  `device_id` (configurable via the `vision.glasses_buffer.max_frames`
  setting) and exposes `latest(device_id, max_age_s=30)` for
  `perception/context_attach.py`.
- Frames older than `vision.glasses_buffer.max_age_s` (default 30 s)
  are still ingested but the read path filters them out so a momentarily
  paused stream doesn't surface stale context to the LLM.

### 5.4.4 `device_announce` (v1.3.0+)

A peripheral-discovery envelope. A node scans its local environment
(typically BLE, mDNS, USB) and emits `device_announce` for each
peripheral it sees. The brain routes each frame through
`feral-core/hardware/mesh.py` which records a memory entity
(`category=device`) and tracks the device under the announcing node's
mesh entry. This closes the "what BLE devices are around my phone right
now?" loop without exposing the peripheral discovery API surface to
every individual capability.

```json
{
  "hup_version": "1.3.0",
  "type": "device_announce",
  "ts": 1734369931.500,
  "msg_id": "1d6c0d4d-...",
  "payload": {
    "scanner_node_id": "feral-iphone-abc123",
    "device_id": "AA:BB:CC:DD:EE:FF",
    "device_kind": "bluetooth_le",
    "name": "AirPods Pro",
    "manufacturer": "Apple",
    "rssi_dbm": -54,
    "advertised_services": ["180F"],
    "first_seen": 1734369900.0,
    "last_seen": 1734369931.5,
    "metadata": {
      "tx_power": 4,
      "appearance": 961
    }
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `scanner_node_id` | string | yes | HUP node id of the daemon doing the scanning. Brain falls back to the WS-level `node_id` when omitted. |
| `device_id` | string | yes | Stable id of the discovered peripheral. BLE: MAC address or platform-stable UUID. mDNS: service instance name. USB: VID:PID + serial when available. |
| `device_kind` | string | yes | One of `bluetooth_le`, `bluetooth_classic`, `mdns`, `usb`, `airplay`, `homekit`, `unknown`. Unknown values are accepted. |
| `name` | string | no | Operator-readable label from the peripheral's advertisement. |
| `manufacturer` | string | no | Decoded from BLE manufacturer data when available. |
| `rssi_dbm` | int | no | Last-known received signal strength. |
| `advertised_services` | string[] | no | BLE GATT service UUIDs, mDNS service types, etc. |
| `first_seen` | float | no | Unix epoch seconds of first observation. Brain stamps server-side when omitted. |
| `last_seen` | float | no | Unix epoch seconds of most recent observation. Defaults to `ts`. |
| `metadata` | object | no | Vendor-specific scratch space. Brain stores verbatim under the memory entity's `attributes`. |

Brain behaviour:

- Upserts a knowledge-graph entity keyed by `device_id` with
  `category=device` and `tags=[device_kind, "peripheral",
  scanner_node_id]`. Repeated announcements update `last_seen` and
  `rssi_dbm` without creating duplicate entities.
- Records a `device.advertise` event with the timestamp pair so chat
  queries like "did my AirPods disconnect today?" can answer from the
  event log.
- Does NOT issue commands to the discovered peripheral — discovery is
  observation-only. To actuate, a separate `hup_action_request` flow
  must be set up by a vendor-specific daemon.

### 5.5 `hup_action_request` (brain → daemon, canonical)

Canonical name since v1.2.0. Legacy aliases `command`, `execute`, and
`hup_execute` are deprecated (see §5.8) and sunset in 2026.7.0.

```json
{
  "hup_version": "1.2.0",
  "type": "hup_action_request",
  "ts": 1734369940.000,
  "payload": {
    "action_id": "f8c3e1a2-...",
    "name": "buzz",
    "params": {"duration_ms": 250, "pattern": "double"},
    "timeout_ms": 5000,
    "requires_confirmation": false
  }
}
```

JSON Schema:

```json
{
  "$id": "https://feral.ai/schemas/hup/v1/hup_action_request.json",
  "type": "object",
  "required": ["action_id", "name", "params"],
  "properties": {
    "action_id":             {"type": "string", "minLength": 1, "maxLength": 64},
    "name":                  {"type": "string", "minLength": 1, "maxLength": 64},
    "params":                {"type": "object"},
    "timeout_ms":            {"type": "integer", "minimum": 1, "maximum": 120000, "default": 5000},
    "requires_confirmation": {"type": "boolean", "default": false}
  }
}
```

This is the direct wire form of `HUPAction` in
`ASOS/feral-core/hardware/protocol.py`; `action_id`, `name`, `params`,
and `timeout_ms` correspond to `HUPAction.action_id`,
`HUPAction.capability_id`, `HUPAction.parameters`, and
`HUPAction.timeout_ms` respectively.

### 5.6 `hup_action_response` (daemon → brain, canonical)

Canonical name since v1.2.0. Brain MUST consume `hup_action_response`
frames and resolve the matching mesh action future by `action_id`.

```json
{
  "hup_version": "1.2.0",
  "type": "hup_action_response",
  "ts": 1734369940.180,
  "payload": {
    "action_id": "f8c3e1a2-...",
    "success": true,
    "result": {"vibrated_ms": 250},
    "error": null,
    "duration_ms": 178
  }
}
```

JSON Schema:

```json
{
  "$id": "https://feral.ai/schemas/hup/v1/hup_action_response.json",
  "type": "object",
  "required": ["action_id", "success"],
  "properties": {
    "action_id":   {"type": "string"},
    "success":     {"type": "boolean"},
    "result":      {"type": "object"},
    "error":       {"type": ["string", "null"]},
    "duration_ms": {"type": "integer", "minimum": 0}
  }
}
```

This is the wire form of `HUPResult` (`hardware/protocol.py`). The
mapping is: `success = (HUPResult.status == "success")`,
`error = HUPResult.error or null`, `result = HUPResult.data`.

### 5.7 `node_bye` (either side)

```json
{
  "hup_version": "1.0.0",
  "type": "node_bye",
  "ts": 1734369999.000,
  "payload": {"reason": "shutdown", "restart_in_s": 0}
}
```

After sending `node_bye`, the sender SHOULD close the socket within 2 s.

### 5.8 Deprecation Policy

Legacy message type names are accepted as aliases for a deprecation
window spanning two minor versions (≈ two months under CalVer).

| Deprecated alias | Canonical type | Sunset version |
|---|---|---|
| `command` | `hup_action_request` | 2026.7.0 |
| `execute` | `hup_action_request` | 2026.7.0 |
| `hup_execute` | `hup_action_request` | 2026.7.0 |
| `heartbeat` | `node_heartbeat` | 2026.7.0 |

Brain behaviour during the deprecation window:
- Brain MUST accept the alias and treat it identically to the canonical
  type.
- Brain MUST log a structured `feral.hup.deprecated_alias` warning on
  each occurrence, including the alias used and the canonical
  replacement.
- After the sunset version, brain MAY reject the alias with error code
  `1002 bad_schema`.

SDK behaviour: SDKs SHOULD emit only canonical types. SDKs SHOULD
consume both canonical and aliased types during the window.

### 5.9 Phone-as-peer envelopes (v1.3.0)

The v1.3.0 release adds phone-specific envelopes while reusing the
existing `/v1/node` transport and authentication model. Directionality:

- `chat_request` (phone → brain)
- `chat_response` (brain → phone)
- `voice_session_start` (phone → brain)
- `voice_interrupt` (phone → brain)
- `genui_push` (brain → phone)
- `genui_event` (phone → brain)
- `peripheral_bridge_register` (phone → brain)
- `backchannel_request` (phone → brain)
- `ambient_transcript` (phone → brain)
- `ambient_transcript_ack` (brain → phone)
- `ambient_digest_request` (phone → brain)
- `ambient_digest` (brain → phone)

`ambient_transcript` (phone → brain):

A conversation the phone recorded and transcribed on device. The glasses
are the microphone; the phone is the recorder and the only thing that
talks to the brain, so `source` is provenance and never a route.

The phone queues transcripts while the brain is off, so one normally
arrives hours or days after the conversation happened. `started_at` is
the real capture time and the brain stores the episode against it;
without it, asking about "yesterday" would not find a conversation from
yesterday that was ingested this morning.

`transcript_id` is the replay key. A client that omits it gets one
minted, but a client that queues MUST send a stable id, because that is
what makes a resend after a lost ack cost nothing.

```json
{
  "hup_version": "1.3.0",
  "type": "ambient_transcript",
  "ts": 1755720000.0,
  "payload": {
    "transcript_id": "7f1c2a9e-...",
    "text": "full transcript text, unbounded",
    "session_id": "",
    "device_id": "theora-iphone-1",
    "started_at": 1755633600.0,
    "ended_at": 1755635400.0,
    "source": "glasses_mic",
    "language": "en-US",
    "speakers": ["Noah"]
  }
}
```

`ambient_transcript_ack` (brain → phone):

Sent once the transcript is durably stored, NOT once it has been
summarized. Summarization runs in the background and is retried from the
brain's own copy if it is interrupted; a phone that waited for the
summary would hold its queue open across every brain restart.

The phone may drop its copy on `accepted: true`. `duplicate: true` means
the brain already had this `transcript_id`, which is the expected answer
to a resend after a lost ack, and is not an error.

An error frame instead of an ack means the transcript was NOT stored and
must be resent.

```json
{
  "hup_version": "1.3.0",
  "type": "ambient_transcript_ack",
  "ts": 1755720000.5,
  "payload": {
    "transcript_id": "7f1c2a9e-...",
    "duplicate": false,
    "accepted": true,
    "detail": ""
  }
}
```

`ambient_digest_request` (phone → brain) and `ambient_digest` (brain → phone):

The return leg for `ambient_transcript`. This is the first pair in this
feature where one frame goes each way, so the direction of each is
stated above and repeated here.

`ambient_transcript_ack` fires as soon as the raw text is on disk,
deliberately, so that a brain dying mid-summarization cannot cost a
transcript the phone has already dropped. The summary therefore does not
exist yet at ack time, and by the time it does the phone is usually
gone. The digest MUST NOT be folded into the ack: that would trade the
durability property for latency the phone does not need.

Two delivery legs, both required, carrying the same `ambient_digest`
frame so the phone has one inbound handler:

- **push**: the brain sends `ambient_digest` unsolicited when
  summarization finishes, if the originating node is still connected.
  Best-effort by nature.
- **pull**: the phone sends `ambient_digest_request` on connect naming
  the transcripts it has synced but holds no digest for. The brain
  answers one `ambient_digest` per id, in request order.

Push alone loses every digest for a phone that has gone. Pull alone
means a recording made while the phone stays connected shows no summary
until the next reconnect.

```json
{
  "hup_version": "1.3.0",
  "type": "ambient_digest_request",
  "node_id": "phone-<id>",
  "ts": 1755720000.5,
  "payload": {
    "transcript_ids": ["7f1c2a9e-...", "b2d4e6f8-..."],
    "include_detail": false
  }
}
```

`transcript_ids` is capped at **64**, not the generic 512-item list
bound. Each reply may carry up to 20,000 characters of `detail`, so 512
ids is a multi-megabyte burst at the exact moment a phone reconnects,
which is when it is most likely to be on cellular. A phone with more
than 64 outstanding asks again; `remaining` tells it when the current
batch is done.

`include_detail` defaults to `false` and SHOULD stay false for the
connect-time sync: `summary` is what makes a card readable and `detail`
is what makes the burst expensive. Request detail one id at a time when
a card is opened.

```json
{
  "hup_version": "1.3.0",
  "type": "ambient_digest",
  "ts": 1755720000.9,
  "payload": {
    "transcript_id": "7f1c2a9e-...",
    "status": "ready",
    "summary": "Noah will send the SDK build by Friday.",
    "detail": "",
    "people": ["Noah"],
    "topics": ["sdk"],
    "commitments": [{"text": "Send the SDK build", "due_iso": "2026-08-28"}],
    "degraded": [],
    "episode_id": "ep-...",
    "processed_at": 1755720000.4,
    "remaining": 3
  }
}
```

Every status returns the same key set; only `status` and the populated
fields differ.

`status` values:

| value | meaning | what the phone does |
|---|---|---|
| `ready` | Summarized. Fields populated. | Store it. |
| `pending` | The brain HAS the transcript but has not finished with it: the background task is running, or it failed and the boot sweep will retry from the brain's copy. | Show the transcript without a summary. Ask again next connect. Do NOT resend. |
| `unknown` | No row **that this device owns**. | Treat as lost: clear `synced_at` so the outbox resends the transcript. |

`remaining` is how many more digests answer the same request, counting
down to `0` on the last one. A phone reconnecting after a week cannot
otherwise tell "your last digest" from "the first of forty" until the
frames simply stop, so it can only appear to hang. With `remaining` it
can tell the user it is fetching and show progress.

**Scoping is mandatory.** `transcript_id` is chosen by the phone, so a
lookup keyed on the id alone is not protected by any unguessability
argument, and the caller is another paired device on the same brain.
The brain MUST scope the lookup to the authenticated identity of the
requesting socket and MUST answer `unknown` for a transcript owned by a
different device, the same answer it gives for a transcript nobody
owns. Distinguishing the two would confirm the existence of another
device's recording to anyone who asked for it.

`injection_flags` is stored with the digest and MUST NOT be sent. It is
a signal about the transcript, useful in the brain's own logs; putting
it in a UI invites rendering a scare banner over something a colleague
said in a meeting.

`ambient_digest` carries the stored `TranscriptOutcome`, never the
episode fields. The episode is shaped for full-text search and for the
model's context block, which forces names and dates into prose and caps
`summary` at 500 characters; on a phone card that renders as a
duplicated date and a truncated sentence.

The digest is derived from recorded speech and can contain anything a
person said. Render it as PLAIN TEXT. It is never markup and never a
link target.

**`unknown` depends on an invariant:** nothing deletes from the brain's
`ambient_transcripts` table. If retention is ever added, an aged-out
recording would answer `unknown` and a phone that treats that as "lost"
would re-upload it forever. Add a distinct status then; do not widen
`unknown`.

`chat_request`:

```json
{
  "type": "chat_request",
  "hup_version": "1.3.0",
  "message_id": "uuid",
  "node_id": "phone-<id>",
  "ts": 1234567890.123,
  "payload": {
    "session_id": "phone-session-uuid",
    "text": "what is that object?",
    "reply_mode": "stream|final",
    "channel": "chat|vision_ask",
    "reply_to": "hup-msg-id|null"
  }
}
```

`chat_response`:

```json
{
  "type": "chat_response",
  "hup_version": "1.3.0",
  "message_id": "uuid",
  "node_id": "brain",
  "ts": 1234567890.456,
  "payload": {
    "session_id": "phone-session-uuid",
    "text": "I can help with that.",
    "reply_mode": "stream|final",
    "channel": "chat|vision_ask",
    "reply_to": "hup-msg-id|null"
  }
}
```

`voice_session_start`:

```json
{
  "type": "voice_session_start",
  "hup_version": "1.3.0",
  "payload": {
    "stream_id": "phone-voice-uuid",
    "sample_rate": 16000,
    "channels": 1,
    "language_hint": "en-US",
    "mode": "push_to_talk|hold_to_talk|vad",
    "interrupt_policy": "barge_in|strict_turn",
    "camera_linked": true
  }
}
```

A `voice_session_start` the brain could not honour is answered with an
`error` frame, code `1099`, name `voice_session_failed`, naming the
stream and the voice mode. There is no positive ack: silence means the
session opened. A daemon that renders a listening state on send MUST
leave it on this frame, because no audio will ever arrive on that
stream. The brain also emits `voice_status` with `state: "unavailable"`
for the failures it can name, carrying `cause` / `summary` /
`recommendation` (see `VoiceStatusPayload` in
`feral-core/models/protocol.py`), but that frame is best-effort
diagnosis while the error frame is the contract.

The brain used to record the start as allowed and send nothing when the
voice backend refused to open, because the only refusal it noticed was
an exception and the router reports every other failure by returning
nothing. The phone had no way to tell an open session from one that
never existed.

`voice_interrupt`:

```json
{
  "type": "voice_interrupt",
  "hup_version": "1.3.0",
  "payload": {
    "stream_id": "phone-voice-uuid",
    "reason": "user_interrupt"
  }
}
```

`genui_push`:

```json
{
  "type": "genui_push",
  "hup_version": "1.3.0",
  "payload": {
    "kind": "notification|interactive",
    "app_id": "feral.notes",
    "surface_id": "today",
    "title": "Door cam needs permission",
    "body": "Open live view?",
    "actions": [
      {"id": "approve", "label": "Approve", "value": {"action":"approve"}},
      {"id": "dismiss", "label": "Dismiss", "value": {"action":"dismiss"}}
    ],
    "sdui": {"...": "full SDUI tree if kind=interactive"}
  }
}
```

`genui_event`:

```json
{
  "type": "genui_event",
  "hup_version": "1.3.0",
  "payload": {
    "app_id": "feral.notes",
    "surface_id": "today",
    "event_type": "tap|toggle|submit|dismiss",
    "action_id": "approve",
    "value": {"action":"approve"}
  }
}
```

`peripheral_bridge_register`:

```json
{
  "type": "peripheral_bridge_register",
  "hup_version": "1.3.0",
  "payload": {
    "bridge_id": "phone-bridge-id",
    "platform": "ios|android",
    "devices": [
      {
        "device_id": "smart_glasses_01",
        "kind": "glasses|watch|band",
        "protocol": "web_bluetooth|native_bridge|none",
        "capabilities": ["imu","notifications","button"],
        "status": "connected|connecting|disconnected",
        "manifest": {"...": "full HUP DeviceManifest"}
      }
    ],
    "expires_at": "2026-04-30T12:00:00Z"
  }
}
```

`backchannel_request`:

```json
{
  "type": "backchannel_request",
  "hup_version": "1.3.0",
  "payload": {
    "request_id": "uuid",
    "device_id": "phone-<id>",
    "kind": "bug|feature|note",
    "payload": {"summary": "fix/add this"},
    "status": "pending"
  }
}
```

---

## 6. Capability Allowlist and Security

Per-device capability gating happens in the FERAL UI at
**Settings → Devices → <device> → Capabilities**. Each capability tier
(§5.1) has a per-device toggle. Brains:

- MUST NOT issue `hup_action_request` for a capability that is not in
  `granted_capabilities` from the `node_ack`.
- MUST issue an inline user confirmation (SDUI prompt) before sending any
  action whose declared tier is `motor`, or whose
  `requires_confirmation: true`.
- SHOULD rate-limit `active_actuator` actions to 10/min/device by default.
- MUST drop `camera_frame` and `microphone_chunk` events from nodes whose
  `camera`/`audio` tier is disabled, even if the daemon sends them.

Nodes:

- MUST refuse any `hup_action_request` whose `name` is not in their
  registered capabilities, replying with `success=false, error="capability_denied"`.
- MUST NOT send `device_event`s for capabilities they did not register.

---

## 7. Example Session — Wristband Registering, Streaming HR, Buzzing

```
# 1. TLS + WS upgrade
GET /v1/node HTTP/1.1
Host: feral.local:9090
Upgrade: websocket
Connection: Upgrade
Authorization: Bearer fkn_live_b58c2c34dd8e4c03b9e...
Sec-WebSocket-Key: ...
Sec-WebSocket-Version: 13

HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade

# 2. daemon → brain
{"hup_version":"1.0.0","type":"node_register","ts":1734369920.001,
 "payload":{"node_id":"acme-wb-001","node_type":"wearable",
            "name":"Acme Wristband","manufacturer":"Acme",
            "firmware_version":"1.2.3","platform":"zephyr",
            "capabilities":["heart_rate","buzzer","battery"],
            "sensors":["heart_rate","battery"],"actuators":["buzzer"]}}

# 3. brain → daemon
{"hup_version":"1.0.0","type":"node_ack","ts":1734369920.040,
 "payload":{"node_id":"acme-wb-001","session_token":"b58c2c34-...",
            "heartbeat_ms":10000,"server_time":1734369920.040,
            "granted_capabilities":["heart_rate","buzzer","battery"],
            "denied_capabilities":[]}}

# 4. daemon → brain (streaming)
{"hup_version":"1.0.0","type":"device_event","ts":1734369931.210,
 "payload":{"node_id":"acme-wb-001","event_type":"heart_rate",
            "data":{"bpm":72,"confidence":0.94},"ts":1734369931.210}}

# 5. daemon → brain (heartbeat)
{"hup_version":"1.0.0","type":"node_heartbeat","ts":1734369930.000,
 "payload":{"ts":1734369930.000,"battery_pct":87,"rssi":-54}}

# 6. brain → daemon (user: "buzz my wrist")
{"hup_version":"1.0.0","type":"hup_action_request","ts":1734369940.000,
 "payload":{"action_id":"f8c3e1a2","name":"buzz",
            "params":{"duration_ms":250,"pattern":"double"},"timeout_ms":5000}}

# 7. daemon → brain
{"hup_version":"1.0.0","type":"hup_action_response","ts":1734369940.180,
 "payload":{"action_id":"f8c3e1a2","success":true,
            "result":{"vibrated_ms":250},"error":null,"duration_ms":178}}

# 8. graceful shutdown (daemon → brain)
{"hup_version":"1.0.0","type":"node_bye","ts":1734369999.000,
 "payload":{"reason":"shutdown","restart_in_s":0}}
```

---

## 8. Errors

Whenever a brain rejects a frame or closes a socket for protocol reasons,
it uses the standard error envelope:

```json
{
  "hup_version": "1.0.0",
  "type": "error",
  "ts": 1734369921.000,
  "payload": {
    "code": 1002,
    "name": "bad_schema",
    "message": "node_register.capabilities must be an array of strings",
    "recoverable": false,
    "ref_action_id": null
  }
}
```

Reserved codes:

| Code | Name                  | Meaning                                                          |
|------|-----------------------|------------------------------------------------------------------|
| 1001 | `unauthorized`        | Missing/invalid API key, expired pairing token.                  |
| 1002 | `bad_schema`          | Frame failed JSON-Schema validation or unsupported `hup_version`.|
| 1003 | `capability_denied`   | Action or event references a capability the user disabled.       |
| 1004 | `rate_limited`        | Too many frames — back off per tier.                             |
| 1005 | `node_id_conflict`    | Another session holds this `node_id`; retry after 2 s.           |
| 1006 | `payload_too_large`   | Frame > 1 MiB or decoded base64 > 512 KiB.                       |
| 1007 | `timeout`             | Action deadline exceeded.                                        |
| 1099 | `internal`            | Brain-side bug. Daemon should retry with backoff.                |
| 4020 | `frame_too_large`     | v1.1+: `audio_frame.data_b64` > 64 KiB decoded, or `video_frame.data_b64` > 512 KiB decoded. Brain drops the frame and returns this error; the session stays open. Daemon SHOULD lower its encoder bitrate. |

> **4020 does not close the socket, by design.** This table previously said
> the brain closes it and the daemon must reconnect. The implementation has
> never done that, and the spec was amended to match rather than the other
> way round: a single oversized frame is a transient encoder problem, and
> tearing down the session would drop a live voice or vision stream along
> with it. Reconnecting also does nothing to make the next frame smaller,
> so the close bought no protection for either side. Dropping the frame and
> naming the cap lets the daemon lower its bitrate and keep talking.
>
> Amending prose is one edit. Changing the brain to close the socket would
> have been a wire-contract break across four SDK languages in exchange for
> worse behaviour. See AUDIT-FIXES F-03.

Codes `>= 2000` are reserved for vendor-private extensions.

WS close codes mirror a subset: `4001` unauthorized, `4002` bad_schema,
`4003` capability_denied, `4004` stale_heartbeat.

---

## 9. Reference Implementations

- **Python** — [`feral-nodes/python-node-sdk/`](./python-node-sdk/)
  (`pip install feral-node-sdk`).
- **TypeScript / Node.js** — [`feral-nodes/ts-node-sdk/`](./ts-node-sdk/)
  (`npm install @feral-ai/node-sdk`).
- **Vendor starter template** — [`feral-nodes/templates/hardware-daemon/`](./templates/hardware-daemon/)
  (cookiecutter-compatible; `cp -r` also works).

Both SDKs embed the schemas in §5 as runtime validators (Pydantic /
Zod) so daemons written with them are conformant by construction.

---

## 10. Compliance Statement

- HUP is published under **Apache-2.0**. Any vendor is free to implement
  it, fork it, or build atop it commercially.
- There is **no certification program**. Vendors self-declare conformance
  by shipping a daemon that passes the reference SDK test suites against
  a stock FERAL brain. A passing daemon MAY advertise "HUP v1 compatible"
  in marketing.
- Patent grant follows Apache-2.0 §3 — implementing HUP does not grant
  rights to any vendor's hardware patents, only to the protocol itself.
- There is no trademark on the string "HUP". The mark "FERAL" belongs to
  its owner; vendor daemons MUST NOT use it except to state compatibility.

---

## Appendix A — Mapping to `feral-core` Types

| HUP wire field                        | feral-core type                                    |
|---------------------------------------|----------------------------------------------------|
| `node_register.payload`               | `models.protocol.NodeRegisterPayload` (extended)   |
| `device_event.payload.event_type`     | `DeviceCapability.id` or sensor string             |
| `hup_action_request.payload`          | `hardware.protocol.HUPAction`                      |
| `hup_action_response.payload`         | `hardware.protocol.HUPResult`                      |
| Capability enum                       | Union of `NodeRegisterPayload.capabilities` strings and `DeviceCapability.category`/sensors/actuators seen in `FERAL_GLASSES_MANIFEST` |

Deltas from the current `/v1/node` handler are tracked in
`feral-nodes/README.md`.

---

## Appendix B — Version Changelog

### v1.3.1 (2026-05-19)

- **Patch** — strict Pydantic-v2 schema enforcement on the v1.3.0
  phone-as-peer envelopes. No new wire types, no new fields; existing
  fields are now `Literal`-typed where they previously accepted free
  strings:
  - `chat_request.reply_mode` ∈ `{"final", "stream"}`.
  - `chat_request.channel` ∈ `{"chat", "vision_ask"}`.
  - `voice_session_start.session_id` is now required (was implicitly
    optional; daemons that omitted it were silently accepted with an
    empty value, which surfaced downstream as a corrupted session
    binding).
  - `audio_chunk.stream_id` and `audio_chunk.channels` are now
    required.
- **Coherence** — the brain's hardcoded ``hup_version: "1.2.0"`` in
  the `node_ack` and `error` envelopes (`feral-core/api/server.py`)
  was replaced with the canonical `HUP_VERSION` constant from
  `models.protocol`. The brain now advertises exactly one version
  on every frame, sourced from one place.
- **Backward-compat:** strictly clarifying. v1.3.0 daemons that
  already populated these fields with valid values remain
  conformant. Daemons that relied on the loose-typed acceptance get
  a clean `error` frame (code `1002 bad_schema`) instead of silent
  downstream corruption.

### v1.3.0 (2026-04-29)

- **Added** phone-as-peer envelopes (§5.9): `chat_request`,
  `chat_response`, `voice_session_start`, `voice_interrupt`,
  `genui_push`, `genui_event`, `peripheral_bridge_register`,
  `backchannel_request`.
- **Added** explicit directionality and payload schemas for every
  phone-as-peer message type.
- **Backward-compat:** strictly additive to v1.2.0.

### v1.2.0 (2026-04-28)

- **Added** `node_ack` as REQUIRED brain response to `node_register`
  (§5.2). Payload: `node_id`, `session_token`, `heartbeat_ms`,
  `hup_version`, `capabilities`, `granted_capabilities`.
- **Canonical** `node_heartbeat` (§5.3) — brain handler now uses the
  canonical name. Legacy `heartbeat` alias deprecated, sunset 2026.7.0.
- **Canonical** `hup_action_request` (§5.5) / `hup_action_response`
  (§5.6) — brain emits and consumes the canonical names. Legacy
  `command`, `execute`, `hup_execute` aliases deprecated, sunset
  2026.7.0.
- **Added** `node_bye` handling in brain — graceful WS close with code
  1000 on receipt.
- **Added** `error` frame emission per §8 on protocol violations.
- **Added** §5.8 deprecation policy for type aliases.
- **iOS SDK** now sends `node_heartbeat` on the interval from
  `node_ack.heartbeat_ms` and `node_bye` on disconnect.
- **Backward-compat:** strictly additive. v1.1.0 daemons remain
  conformant via the alias window.

### v1.1.0 (2026-04-21)

- **Added** `audio_frame` event type (§5.4.1) — Opus/PCM16 frames with
  `sample_rate`, `channels`, `frame_ms`, `sequence`, `data_b64`. Cap:
  64 KiB decoded per frame.
- **Added** `video_frame` event type (§5.4.2) — JPEG/H.264 frames with
  `width`, `height`, `sequence`, `keyframe`, `data_b64`. Cap: 512 KiB
  decoded per frame.
- **Added** error code `4020 frame_too_large` for over-cap media frames.
- **Backward-compat:** strictly additive. v1.0.0 daemons remain
  conformant. v1.0.0 brains MUST ignore unknown event types per §1's
  forward-compat rule. Legacy `camera_frame` / `microphone_chunk` stay
  valid; new daemons SHOULD migrate to `video_frame` / `audio_frame`.

### v1.0.0

- Initial public release of the Hardware Unification Protocol.
