# FERAL Android Bridge

Android SDK and sample app for connecting to the FERAL Brain.

## Requirements

- Android Studio Hedgehog (2023.1.1) or later
- JDK 17
- Android SDK 34

## Build

```bash
# Library AAR
./gradlew :bridge:assembleRelease

# Sample APK (debug)
./gradlew :sample:assembleDebug
```

The AAR is output to `bridge/build/outputs/aar/`.
The APK is output to `sample/build/outputs/apk/debug/`.

## Usage

```kotlin
val client = FeralBrainClient(
    host = "192.168.1.100",
    port = 9090,
    apiKey = "your-api-key",
    delegate = this,
)
client.connect()

// Send text
client.sendTextCommand("What's the weather?")

// Send audio
client.sendAudioChunk(pcmBytes, chunkIndex = 0, isFinal = false)

// Send sensor data
client.sendSensorTelemetry(heartRate = 72, spo2 = 98)
```

## Architecture

- `bridge/` — Library module (AAR) with `FeralBrainClient` WebSocket client
- `sample/` — Demo app implementing `FeralBrainDelegate`

As of 2026.5.8 the bridge authenticates via the `Authorization: Bearer
<token>` header (matching `FeralBrainClient.kt`; class previously
named `TheoraBrainClient` — renamed in v2026.5.28 for iOS/Android
parity). The legacy `?api_key=` query auth is accepted by brains
during the deprecation window (sunset `2026.7.0`). The bridge
connects to `wss://host[:port]/v1/node` (or `ws://` for Mode A LAN
with explicit opt-in) and handles the full HUP surface that iOS
`feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift` exposes:

Outbound message types:
- `register` — node identity + capabilities
- `voice_config` — realtime / batch mode + sample rate
- `audio_chunk` — PCM16 audio chunks (base64)
- `text_command` — text prompts
- `sensor_telemetry` — single-tick health/sensor reading
- `sensor_batch` — batched per-sensor readings (new in v2026.5.28)
- `frame` — base64 camera frame (new in v2026.5.28)
- `glasses_status` — connected glasses summary
- `skill_approval` — accept/reject brain-proposed skill (new in v2026.5.28)
- `confirmation_response` — user verdict on a `confirmation_required` prompt (new in v2026.5.28)

Inbound message types:
- `registered` — brain confirms registration + assigns session_id (new in v2026.5.28)
- `text_response` — assistant text
- `sdui` — server-driven UI tree
- `audio_response` / `tts_chunk` — audio output
- `speech_started` — barge-in signal (stop local playback)
- `transcript` — STT partial/final transcript
- `execute` — node-side action invocation
- `skill_proposal` — brain wants to install a skill (new in v2026.5.28)
- `confirmation_required` — brain needs user approval for a tool action (new in v2026.5.28)

Platform-specific helpers in the bridge (no iOS counterpart, by design):
- `AudioManager` / `WakeWordDetector` — onnxruntime-based wake-word
- `CameraManager` — CameraX wrappers
- `LocationManager` — FusedLocationProvider wrappers
- `PairingManager` — bridge-side pairing state machine

Design tokens shared with iOS (`FeralV2Tokens.swift`) and web
(`feral-client-v2/src/styles/tokens.css`) live at
`bridge/src/main/java/ai/feral/bridge/FeralV2Tokens.kt`.
