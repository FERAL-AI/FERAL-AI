"""
FERAL Protocol — The Wire Format
==================================
Every component in FERAL speaks this protocol.
Brain, Phone, Daemon, Robot — all use the same message envelope.
This is the single source of truth for all message types.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Literal, Any
from uuid import uuid4
from time import time

HUP_VERSION = "1.3.0"


# ─────────────────────────────────────────────
# The Universal Message Envelope
# ─────────────────────────────────────────────

class FeralMessage(BaseModel):
    """Every message in the system uses this envelope."""
    msg_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    timestamp_ms: int = Field(default_factory=lambda: int(time() * 1000))
    hop: Literal["client", "brain", "daemon", "skill"] = "client"
    type: str  # Discriminator — see payload types below
    payload: dict = Field(default_factory=dict)


# ─────────────────────────────────────────────
# Payload Models — Client → Brain
# ─────────────────────────────────────────────

class AudioChunkPayload(BaseModel):
    """Streaming audio from client to brain."""
    encoding: str = "opus"
    sample_rate: int = 24000
    channels: int = 1
    chunk_index: int = 0
    is_final: bool = False
    data_b64: str = ""


class AttachmentRef(BaseModel):
    """Reference to a previously-uploaded file (PR 10).

    The actual bytes live under ``$FERAL_HOME/uploads/<upload_id>`` and
    are never embedded in the payload — keeping the LLM's prompt
    bounded and avoiding base64 bloat on the WS. The orchestrator
    resolves the ref through :class:`memory.uploads.UploadStore`
    when a tool needs the on-disk path.
    """
    upload_id: str
    filename: str = ""
    content_type: str = ""
    size_bytes: int = 0
    sha256: str = ""


class TextCommandPayload(BaseModel):
    """Text input (for web/CLI clients that type instead of speak).

    PR 10: an optional ``attachments`` list lets the composer ship
    file references alongside the prompt without inlining bytes."""
    text: str
    context: Optional[dict] = None
    attachments: Optional[list[AttachmentRef]] = None


class BiometricPayload(BaseModel):
    """Sensor data from glasses or phone."""
    heart_rate_bpm: Optional[int] = None
    spo2_pct: Optional[int] = None
    accel_xyz: Optional[list[float]] = None
    temperature_c: Optional[float] = None
    uv_index: Optional[int] = None
    gps: Optional[dict] = None  # {"lat": float, "lon": float}
    inferred_state: Optional[str] = None  # "resting", "walking", "running", "stressed"


class UIEventPayload(BaseModel):
    """User interacted with a generated UI element.

    ``app_id`` is optional and backward-compatible: legacy SDUI /
    proactive events still work without it. When present, the brain
    routes the event through ``AppRegistry.validate_action`` first so
    third-party apps can't dispatch to skill endpoints they didn't
    declare in their surface's ``action_contract``.
    """
    screen_id: str
    event: Literal["tap", "toggle", "slider", "text_input", "dismiss"]
    action_id: str
    value: Optional[Any] = None
    app_id: Optional[str] = None


# ─────────────────────────────────────────────
# Payload Models — Phone-as-peer Envelopes (HUP v1.3)
# ─────────────────────────────────────────────

class ChatRequestPayload(BaseModel):
    """Phone text/vision query request routed through the orchestrator."""
    session_id: str
    text: str
    reply_mode: Literal["stream", "final"] = "final"
    channel: Literal["chat", "vision_ask"] = "chat"
    reply_to: Optional[str] = None
    # Phase 1 (audit-r10 overhaul plan) — device_target tells the brain
    # WHERE the requested action should run. The orchestrator's
    # ExecutionSurfacePolicy dispatches Mac-side skills when
    # `device_target == "brain"`, phone-native skills when
    # `device_target == "phone"`, glasses bridged via phone when
    # `device_target == "glasses"`, and falls back to the conservative
    # `http_api` surface when `auto` / None so existing behavior is
    # preserved until the PromptRefiner (Phase 2) starts populating
    # this field deterministically.
    device_target: Optional[Literal["brain", "phone", "glasses", "auto"]] = None


class ChatResponsePayload(BaseModel):
    """Brain response envelope for phone chat requests.

    ``error`` carries the orchestrator failure text on the failure
    branch and is ``None`` on success. Phase-1.5 truthfulness sweep
    added it so a chat-only client (one that doesn't track the
    parallel HUP ``error`` frame) can still surface a real failure
    string instead of rendering an empty assistant bubble. The
    daemon_session ``chat_request`` branch sets it to ``None`` on
    success, the orchestrator's exception text on failure.
    """
    session_id: str
    text: str
    reply_mode: Literal["stream", "final"] = "final"
    channel: Literal["chat", "vision_ask"] = "chat"
    reply_to: Optional[str] = None
    error: Optional[str] = None


class VoiceSessionStartPayload(BaseModel):
    """Phone voice session bootstrap metadata."""
    stream_id: str
    sample_rate: int
    channels: int
    language_hint: str = "en-US"
    mode: Literal["push_to_talk", "hold_to_talk", "vad"] = "push_to_talk"
    interrupt_policy: Literal["barge_in", "strict_turn"] = "barge_in"
    camera_linked: bool = False


class VoiceInterruptPayload(BaseModel):
    """Signal from phone to cut in-flight TTS on the active stream.

    ``stream_id`` used to be required, but in practice the phone UI
    emits a bare ``voice_interrupt`` (tap-to-interrupt on the orb)
    without knowing the session's stream id — the brain looks up the
    active voice session via the node_id on the WS. Making this
    optional stops live-test pydantic validation errors like:
      VoiceInterruptPayload.stream_id: Field required
    from dropping the interrupt frame entirely.
    """
    stream_id: Optional[str] = None
    reason: str = "user_interrupt"


class GenUIPushActionPayload(BaseModel):
    """Action button attached to a GenUI push card."""
    id: str
    label: str
    value: dict = Field(default_factory=dict)


class GenUIPushPayload(BaseModel):
    """Brain-originated mobile GenUI push payload."""
    kind: Literal["notification", "interactive"]
    app_id: str
    surface_id: str
    push_id: str = ""
    screen_id: str = ""
    title: str
    body: str = ""
    actions: list[GenUIPushActionPayload] = Field(default_factory=list)
    sdui: Optional[dict] = None


class GenUIEventPayload(BaseModel):
    """Phone-originated GenUI interaction routed to app action handlers."""
    app_id: str
    surface_id: str
    event_type: str
    action_id: str
    value: Optional[Any] = None
    screen_id: Optional[str] = None


class LocationUpdatePayload(BaseModel):
    """Phone-originated geolocation update streamed over the same HUP
    WebSocket as other peer envelopes.

    Replaces the legacy ``POST /api/location/update`` HTTP path that
    relied on dashboard API key auth — phones authenticate with
    ``phone_bearer`` over WS subprotocol, so the HTTP path returned
    401 for them. Sending location as a HUP envelope gets free auth
    + lifecycle alignment with the rest of the peer streams.

    HUP v1.3.1 addition.
    """
    node_id: str
    lat: float
    lon: float
    accuracy_m: Optional[float] = None
    altitude_m: Optional[float] = None
    heading_deg: Optional[float] = None
    speed_mps: Optional[float] = None
    source: str = "browser_node"
    ts: Optional[float] = None


class PeripheralBridgeDevicePayload(BaseModel):
    """One bridged peripheral exposed by the phone peer."""
    device_id: str
    kind: Literal["glasses", "watch", "band"]
    protocol: Literal["web_bluetooth", "native_bridge", "none"]
    capabilities: list[str] = Field(default_factory=list)
    status: Literal["connected", "connecting", "disconnected"] = "connecting"
    manifest: dict = Field(default_factory=dict)


class PeripheralBridgeRegisterPayload(BaseModel):
    """Phone bridge registration/update payload."""
    bridge_id: str
    platform: Literal["ios", "android"]
    devices: list[PeripheralBridgeDevicePayload]
    expires_at: str


class BackchannelRequestPayload(BaseModel):
    """Structured operator-review request from phone."""
    device_id: str
    kind: str
    payload: dict = Field(default_factory=dict)
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    status: str = "pending"


# ─────────────────────────────────────────────
# Payload Models — Brain → Client
# ─────────────────────────────────────────────

class TranscriptPayload(BaseModel):
    """Speech-to-text result.

    The ``role`` field disambiguates user-spoken text from
    assistant-spoken text (OpenAI Realtime + Gemini Live both fan
    speaker and listener transcripts through the same event family).
    Wire consumers must respect it — iOS used to hardcode every
    transcript as ``user`` which surfaced as "all chat bubbles look
    identical" (operator report 2026-05-08, fixed in companion-ios
    PR #1 commit-batch + brain realtime_proxy.py companion fix).
    Defaults to ``"assistant"`` because in practice the brain emits
    role-tagged frames everywhere; an unset role on the wire is
    almost always an assistant transcript.
    """
    text: str
    is_partial: bool = False
    confidence: float = 1.0
    role: Optional[str] = "assistant"


class SDUIPayload(BaseModel):
    """Server-Driven UI — the generated interface."""
    screen_id: str = Field(default_factory=lambda: str(uuid4()))
    ttl_seconds: int = 300
    root: dict  # The SDUI tree (see genui/schema/)


class SDUIPatchPayload(BaseModel):
    """Partial update to an existing generated screen."""
    screen_id: str
    patches: list[dict]  # [{"path": "children.0.value", "op": "replace", "value": "new text"}]


class TTSChunkPayload(BaseModel):
    """Streaming audio from brain to client (text-to-speech)."""
    chunk_index: int = 0
    encoding: str = "mp3"
    data_b64: str = ""
    is_final: bool = False


class TextResponsePayload(BaseModel):
    """Plain text response (for CLI/chat clients)."""
    text: str
    tool_calls: Optional[list[dict]] = None


class StreamDeltaPayload(BaseModel):
    """Streaming text token from brain to client (real-time LLM output)."""
    delta: str
    stream_id: str = ""
    is_final: bool = False


class ToolStartPayload(BaseModel):
    """Brain notifies client that a tool call has begun.

    Renders as a chip or equivalent affordance in the UI so the user
    sees what the agent is doing without the model having to narrate
    it in prose. ``args_preview`` is a short, redacted JSON string
    suitable for a one-line display — not the full argument blob.
    """
    tool: str
    call_id: str = ""
    skill_id: str = ""
    endpoint_id: str = ""
    args_preview: str = ""
    display_name: str = ""


class ToolResultPayload(BaseModel):
    """Brain notifies client that a tool call finished.

    Paired with ``tool_start`` by ``call_id`` when present. The client
    uses this to clear the active-tool chip and (optionally) record a
    per-turn activity row.
    """
    tool: str
    call_id: str = ""
    success: bool = True
    error: str = ""
    latency_ms: float = 0.0


class GesturePayload(BaseModel):
    """Gesture detected by a hardware daemon (glasses IMU, camera, etc.)."""
    gesture: str  # "nod", "shake", "look_up", "look_down", "double_tap"
    confidence: float = 1.0
    source: str = "imu"  # "imu", "camera", "touch"


class RefusalPayload(BaseModel):
    """Structured refusal from the supervisor / orchestrator.

    Emitted when the brain explicitly declines to act — supervisor
    paused, policy gate denied, autonomy mode rejected the request.
    The consumer renders this as a yellow chip in chat plus an
    actionable ``retry_hint`` (e.g. "resume supervisor in Settings →
    Oversight"). Pinned by Lane 08 WS6.
    """
    reason: str
    retry_hint: str = ""
    source: str = "supervisor"  # supervisor | policy | autonomy | ...
    kind: str = ""  # command | command_stream | ui_event | ...


class BudgetExceededPayload(BaseModel):
    """Structured cost-budget refusal.

    Emitted when a CostBudget.check_and_reserve / record_usage call
    flagged the request as breaching a per-call-site or global cap.
    Lane 12 renders this as a yellow banner: "Chat budget reached
    ($X.XX / hour). Resets at HH:MM." Pinned by Lane 08 WS8.
    """
    call_site: str
    cap_dollars: float = 0.0
    current_dollars: float = 0.0
    window: str = "hour"  # hour | day
    reset_at: float = 0.0  # unix epoch seconds when the cap resets


class ErrorPayload(BaseModel):
    """Something went wrong."""
    code: str
    message: str
    recoverable: bool = True


# ─────────────────────────────────────────────
# Payload Models — Brain ↔ Daemon
# ─────────────────────────────────────────────

class NodeRegisterPayload(BaseModel):
    """Daemon announces itself to the brain.

    Mirrors HUP v1.1's node_register envelope
    (feral-nodes/python-node-sdk/src/feral_node_sdk/schemas.py). The
    ``node_type`` Literal widened to cover every type the HUP spec
    declares so a wristband daemon announcing ``node_type="wearable"``
    isn't rejected by pydantic before the /v1/node handler even sees
    it. `manufacturer` and `model` are optional v1.1 fields the
    Devices UI surfaces.
    """
    node_id: str
    node_type: Literal[
        "desktop", "server", "rpi", "robot", "glasses", "phone",
        "tablet", "actuator", "sensor", "wearable", "camera",
        "vehicle", "appliance", "browser_camera", "browser_node",
    ]
    os: str = ""
    platform: str = ""  # "ios", "android", "linux", "macos"
    manufacturer: str = ""
    model: str = ""
    firmware_version: str = ""
    capabilities: list[str] = []  # ["applescript", "keyboard", "filesystem", "camera", "gpio"]
    # Phase 4 (audit-r10 overhaul) — structured skill manifests the
    # node publishes alongside its flat capability list. Each entry
    # is `{"id", "name", "description", "actions": [{"name",
    # "summary", "requires_permission"?}]}`. Phase 5's capability
    # registry consumes these to drive `GET /api/capabilities` and
    # to teach the orchestrator which `phone.*` / `glasses.*` action
    # names actually exist on the currently connected nodes.
    #
    # Typed as `list[dict]` rather than a nested model because the
    # node SDK is the source of truth for the manifest shape — the
    # brain is a passive consumer that re-emits whatever the node
    # published. Validation lives in the registry, not here.
    skills: list[dict] = []


class ExecuteCommandPayload(BaseModel):
    """Brain tells daemon to do something."""
    command_id: str = Field(default_factory=lambda: str(uuid4()))
    executor: str  # "applescript", "shell", "keyboard", "gpio"
    action: str  # The actual command/script
    args: dict = Field(default_factory=dict)
    timeout_ms: int = 5000
    requires_confirmation: bool = False


class ExecuteResultPayload(BaseModel):
    """Daemon reports back the result."""
    command_id: str
    status: Literal["success", "failure", "denied", "timeout"]
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


# ─────────────────────────────────────────────
# Payload Models — Vision Pipeline (Daemon ↔ Brain)
# ─────────────────────────────────────────────

class VisionFramePayload(BaseModel):
    """Daemon pushes a captured camera frame to the brain."""
    node_id: str
    frame_id: str = Field(default_factory=lambda: str(uuid4()))
    encoding: Literal["jpeg", "png", "webp"] = "jpeg"
    resolution: list[int] = Field(default_factory=lambda: [640, 480])  # [width, height]
    data_b64: str = ""
    timestamp: float = Field(default_factory=time)
    metadata: dict = Field(default_factory=dict)  # scene_brightness, faces_detected, etc.


class VisionRequestPayload(BaseModel):
    """Brain requests a frame capture from a daemon's camera."""
    resolution: str = "640x480"
    quality: int = 80  # JPEG quality 1-100
    reason: str = ""


# ─────────────────────────────────────────────
# Payload Models — Device Registration
# ─────────────────────────────────────────────

class DeviceRegisterPayload(BaseModel):
    """Hardware device (glasses, robot, etc.) registers with the brain."""
    device_id: str
    device_type: Literal["glasses", "phone", "watch", "robot", "camera", "sensor_hub"]
    name: str = ""
    sensors: list[str] = []  # ["heart_rate", "spo2", "accelerometer", "uv", "temperature", "camera"]
    firmware_version: str = ""
    battery_pct: Optional[int] = None


# ─────────────────────────────────────────────
# Payload Models — Phone Bridge (iOS/Android → Brain)
# ─────────────────────────────────────────────

class SensorTelemetryPayload(BaseModel):
    """Single sensor reading from FERAL glasses via phone bridge."""
    node_id: str
    sensor: str  # "heart_rate", "spo2", "temperature", "uv", "steps"
    data: dict  # Sensor-specific values
    timestamp: str = ""
    source: str = "feral_glasses"


class SensorBatchPayload(BaseModel):
    """Multiple sensor readings in one message."""
    node_id: str
    readings: dict  # {"heart_rate": {...}, "spo2": {...}, ...}
    timestamp: str = ""
    source: str = "feral_glasses"


class GlassesStatusPayload(BaseModel):
    """Phone reports glasses connection status."""
    node_id: str
    glasses_connected: bool = False
    battery_level: int = -1
    glasses_model: str = "FERAL"


class SkillApprovalPayload(BaseModel):
    """User approved/rejected a proposed skill."""
    skill_id: str
    approved: bool = False


class ConfirmationResponsePayload(BaseModel):
    """User responded to a permission confirmation."""
    action: str
    approved: bool = False


class PermissionRequestPayload(BaseModel):
    """Agent requests folder access from the user."""
    request_id: str = Field(default_factory=lambda: str(uuid4())[:8])
    path: str
    operation: Literal["read", "write", "readwrite"] = "read"
    reason: str = ""


class PermissionResponsePayload(BaseModel):
    """User grants or denies folder access."""
    request_id: str
    granted: bool = False
    mode: str = "read"


# ─────────────────────────────────────────────
# Payload Models — Voice Pipeline
# ─────────────────────────────────────────────

class VoiceConfigPayload(BaseModel):
    """Client/node declares voice capabilities and selected mode."""
    node_id: str = ""
    supports_realtime: bool = False
    mode: Literal["realtime", "whisper", "auto", "disabled"] = "auto"
    preferred_model: str = ""
    sample_rate: int = 24000
    encoding: str = "pcm16"

class AudioResponsePayload(BaseModel):
    """Brain sends audio back to a node (realtime TTS or Whisper TTS)."""
    data_b64: str = ""
    encoding: str = "pcm16"
    sample_rate: int = 24000
    is_final: bool = False

class VoiceStatusPayload(BaseModel):
    """Brain -> client voice subsystem health update.

    Emitted by the voice router when a realtime provider fails (e.g.
    OpenAI Realtime closes WS 1013 ``insufficient_quota``) so the
    client can render a banner instead of going silent. ``state`` is
    the tri-state: ``available`` (normal), ``degraded`` (realtime
    down, falling back to chunked TTS), ``unavailable`` (no audio
    path at all). ``reason`` is a short machine-friendly tag the
    client renders into a human banner (e.g.
    ``openai_realtime_quota``, ``no_tts_provider``).
    """
    state: Literal["available", "degraded", "unavailable"] = "available"
    reason: str = ""
    provider: str = ""
    fallback_provider: str = ""
    detail: str = ""

class VisionQueryPayload(BaseModel):
    """User explicitly asks about what the camera sees."""
    query: str = "What do you see?"
    node_id: str = ""
    force: bool = True


class HandoffRequestPayload(BaseModel):
    """Client asks to move working-memory context to another device class."""
    to_node_type: str = "desktop"
    history_depth: int = Field(default=20, ge=1, le=500)


# ─────────────────────────────────────────────
# Message Type Registry — Maps type strings to payload models
# ─────────────────────────────────────────────

class NodeAckPayload(BaseModel):
    """Brain acknowledges a node_register (HUP_SPEC §5.2)."""
    node_id: str = ""
    session_token: str = ""
    hup_version: str = HUP_VERSION
    heartbeat_ms: int = 10000
    server_time: float = Field(default_factory=time)
    capabilities: list[str] = []
    granted_capabilities: list[str] = []
    denied_capabilities: list[str] = []


class HUPActionRequestPayload(BaseModel):
    """Brain dispatches an action to a daemon (HUP_SPEC §5.5)."""
    action_id: str = Field(default_factory=lambda: str(uuid4())[:8])
    name: str = ""
    params: dict = Field(default_factory=dict)
    timeout_ms: int = 5000
    requires_confirmation: bool = False
    # Phase 1 — device_target lets the orchestrator address a specific
    # node-type when fanning out actions (e.g. "phone" for native
    # iOS/Android skills, "glasses" for BLE-bridged peripherals). The
    # daemon ignores this field when it owns the action regardless;
    # carried on the wire for symmetry with ChatRequestPayload + future
    # multi-node fan-out where the brain must pick which daemon runs
    # the same action name.
    device_target: Optional[Literal["brain", "phone", "glasses", "auto"]] = None


class HUPActionResponsePayload(BaseModel):
    """Daemon responds to an hup_action_request (HUP_SPEC §5.6)."""
    action_id: str = ""
    request_id: str = ""
    success: bool = True
    result: dict = Field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0


class NodeHeartbeatPayload(BaseModel):
    """Daemon heartbeat (HUP_SPEC §5.3)."""
    ts: float = Field(default_factory=time)
    battery_pct: Optional[int] = None
    rssi: Optional[int] = None


class NodeByePayload(BaseModel):
    """Graceful disconnect (HUP_SPEC §5.7)."""
    reason: str = "shutdown"
    restart_in_s: int = 0


class GlassesFramePayload(BaseModel):
    """Smart-glasses (or glasses-equivalent) vision frame envelope.

    HUP_SPEC §5.4.3 (v1.3.0+). The brain stores accepted frames into the
    per-device circular buffer at ``feral-core/perception/glasses_buffer.py``
    which the orchestrator's vision-context-attach reads.

    ``device_id`` is the stable hardware id (e.g. ``w610-D344`` for a
    real W610 unit, or the iPhone node id when the phone camera is the
    fallback source). It can differ from the HUP-level ``node_id`` that
    forwarded the frame — a phone forwarding W610 frames carries the
    phone as ``node_id`` and the glasses id as ``device_id``.

    ``source`` is a free-form provenance label. The brain forwards it
    verbatim into the buffer; the orchestrator may use it for cost
    accounting (e.g. cheaper vision tier for ``camera_fallback`` than
    ``w610``) but the wire itself doesn't gate on it.
    """
    device_id: str
    timestamp: float = Field(default_factory=time)
    encoding: Literal["jpeg", "png", "webp"] = "jpeg"
    data_b64: str
    width: Optional[int] = None
    height: Optional[int] = None
    source: str = "glasses"
    sequence: Optional[int] = None


class DeviceAnnouncePayload(BaseModel):
    """Peripheral-discovery envelope (HUP_SPEC §5.4.4 — v1.3.0+).

    Closes the "what BLE devices are around my phone?" loop without
    exposing per-vendor BLE APIs. The brain upserts a knowledge-graph
    entity keyed by ``device_id`` with ``category=device`` so device
    queries land via the same memory tool path as everything else.
    """
    scanner_node_id: str = ""
    device_id: str
    device_kind: Literal[
        "bluetooth_le",
        "bluetooth_classic",
        "mdns",
        "usb",
        "airplay",
        "homekit",
        "unknown",
    ] = "unknown"
    name: str = ""
    manufacturer: str = ""
    rssi_dbm: Optional[int] = None
    advertised_services: list[str] = Field(default_factory=list)
    first_seen: Optional[float] = None
    last_seen: Optional[float] = None
    metadata: dict = Field(default_factory=dict)


MESSAGE_TYPES = {
    # Client → Brain
    "audio_chunk": AudioChunkPayload,
    "text_command": TextCommandPayload,
    "biometric": BiometricPayload,
    "ui_event": UIEventPayload,
    "device_register": DeviceRegisterPayload,
    "handoff_request": HandoffRequestPayload,
    "chat_request": ChatRequestPayload,
    "voice_session_start": VoiceSessionStartPayload,
    "voice_interrupt": VoiceInterruptPayload,
    "genui_event": GenUIEventPayload,
    "location_update": LocationUpdatePayload,
    "peripheral_bridge_register": PeripheralBridgeRegisterPayload,
    "backchannel_request": BackchannelRequestPayload,

    # Brain → Client
    "transcript": TranscriptPayload,
    "sdui": SDUIPayload,
    "sdui_patch": SDUIPatchPayload,
    "tts_chunk": TTSChunkPayload,
    "text_response": TextResponsePayload,
    "stream_delta": StreamDeltaPayload,
    "tool_start": ToolStartPayload,
    "tool_result": ToolResultPayload,
    "gesture": GesturePayload,
    "error": ErrorPayload,
    "refusal": RefusalPayload,
    "budget_exceeded": BudgetExceededPayload,
    "chat_response": ChatResponsePayload,
    "genui_push": GenUIPushPayload,

    # Brain ↔ Daemon (HUP canonical)
    "register": NodeRegisterPayload,
    "node_register": NodeRegisterPayload,
    "node_ack": NodeAckPayload,
    "node_heartbeat": NodeHeartbeatPayload,
    "hup_action_request": HUPActionRequestPayload,
    "hup_action_response": HUPActionResponsePayload,
    "node_bye": NodeByePayload,
    "execute": ExecuteCommandPayload,
    "execute_result": ExecuteResultPayload,

    # Vision Pipeline
    "vision_frame": VisionFramePayload,
    "vision_request": VisionRequestPayload,
    "glasses_frame": GlassesFramePayload,

    # Hardware mesh (peripheral discovery — HUP v1.3.0)
    "device_announce": DeviceAnnouncePayload,

    # Phone Bridge
    "sensor_telemetry": SensorTelemetryPayload,
    "sensor_batch": SensorBatchPayload,
    "glasses_status": GlassesStatusPayload,
    "skill_approval": SkillApprovalPayload,
    "confirmation_response": ConfirmationResponsePayload,
    "permission_request": PermissionRequestPayload,
    "permission_response": PermissionResponsePayload,

    # Voice Pipeline
    "voice_config": VoiceConfigPayload,
    "audio_response": AudioResponsePayload,
    "voice_status": VoiceStatusPayload,
    "vision_query": VisionQueryPayload,
}

DEPRECATED_TYPE_ALIASES: dict[str, str] = {
    "command": "hup_action_request",
    "execute": "hup_action_request",
    "hup_execute": "hup_action_request",
    "heartbeat": "node_heartbeat",
}
DEPRECATED_ALIAS_SUNSET = "2026.7.0"


def parse_message(raw: dict) -> tuple[FeralMessage, BaseModel | None]:
    """Parse a raw dict into a FeralMessage + typed payload."""
    msg = FeralMessage(**raw)
    payload_cls = MESSAGE_TYPES.get(msg.type)
    if payload_cls:
        return msg, payload_cls(**msg.payload)
    return msg, None
