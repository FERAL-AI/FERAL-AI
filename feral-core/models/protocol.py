"""
FERAL Protocol — The Wire Format
==================================
Every component in FERAL speaks this protocol.
Brain, Phone, Daemon, Robot — all use the same message envelope.
This is the single source of truth for all message types.
"""

from __future__ import annotations
from pydantic import AliasChoices, BaseModel, Field, field_validator
from typing import Optional, Literal, Any
from uuid import uuid4
from time import time

HUP_VERSION = "1.4.0"


# ─────────────────────────────────────────────
# The brain -> node envelope (HUP_SPEC.md section 5)
# ─────────────────────────────────────────────
# "Every HUP frame is a JSON object with hup_version, type, ts, payload."
# Five brain-to-node sends spelled that out by hand and twelve did not
# (counted by the AST gate in tests/test_hup_version_unified.py against
# the pre-change tree), so the wire carried two shapes of the same
# protocol -- and the twelve included every hup_action_request builder,
# which is the actuator command frame. Nothing broke
# because no shipping SDK validates the envelope on inbound -- the Swift
# decoder documents the omission and tolerates it by name -- but a
# third-party daemon written against the published spec is entitled to
# reject a frame with no version on it, and it would have been right to.
#
# One builder, used by every brain-to-node send. ``hup_frame`` is for a
# frame being constructed here; ``stamp_hup_envelope`` is for a dict that
# already exists (a ``FeralMessage.model_dump()``, a frame assembled by a
# helper) and only needs the two missing fields. Neither overwrites a
# value the caller set, so a replayed or forwarded frame keeps its own
# ``ts``.
#
# ``tests/test_hup_version_unified.py`` AST-scans the brain for a
# node-bound send that bypasses both.

#: Envelope keys HUP_SPEC.md section 5 requires on every frame.
HUP_ENVELOPE_KEYS: tuple = ("hup_version", "type", "ts", "payload")


def stamp_hup_envelope(frame: dict) -> dict:
    """Fill in ``hup_version`` / ``ts`` / ``payload`` when absent, in place.

    Returns the same dict so it can wrap a send argument directly. A key
    the caller already set is never overwritten: forwarding a frame must
    not restamp it with the brain's clock.
    """
    if not isinstance(frame, dict):
        return frame
    frame.setdefault("hup_version", HUP_VERSION)
    frame.setdefault("ts", time())
    frame.setdefault("payload", {})
    return frame


def hup_frame(msg_type: str, payload: Optional[dict] = None, **extra) -> dict:
    """Build a spec-complete brain -> node HUP frame.

    ``extra`` carries the non-envelope keys some routes add (``hop``,
    ``session_id``, ``msg_id``); they sit alongside the envelope rather
    than inside the payload, which is where they already were on the
    wire.
    """
    frame = {
        "hup_version": HUP_VERSION,
        "type": msg_type,
        "ts": time(),
        "payload": payload if payload is not None else {},
    }
    frame.update(extra)
    return frame


# ─────────────────────────────────────────────
# Structural field bounds (AUDIT-FIXES F-02)
# ─────────────────────────────────────────────
# Every model below used to declare its fields bare, so the brain accepted
# ``device_id=""``, ``width=-5``, ``height=1000000000``, ``sequence=-42`` and a
# 900,000-byte decoded frame against a documented 512 KiB cap. Meanwhile the
# Python node SDK bounded all of them, so every constraint lived in the
# component an attacker controls. See AUDIT-FIXES.md F-02.
#
# The bounds are STRUCTURAL only: identifier lengths, non-negative counters,
# pixel ranges, coordinate domains and decoded blob caps. No semantic ceiling
# (heart rate, skin temperature, battery percent) is invented here. Real
# hardware reports surprising values, and this file already documents two
# out-of-range sentinels in its own defaults (``GlassesStatusPayload.
# battery_level = -1``), so a wrong semantic ceiling would turn a working
# device into a rejected one.
#
# A violation is REJECTED, never clamped: ``parse_message`` raises
# ``ValidationError`` and ``api/server.py`` converts it into a HUP section 8
# error frame (code 1003) that keeps the socket alive and names the field.

#: Hardware / session / correlation identifiers. 128 matches the node SDK's
#: ``device_id`` bound; real paired devices on this install carry 36-char UUIDs.
MAX_ID_LEN = 128
#: Session identifiers get their own, far looser bound. They are NOT opaque
#: ids: the brain composes them by concatenation, and one of the segments is
#: a user-supplied ``branch_name`` (``api/routes/conversations.py`` builds
#: ``f"{session_id}:{branch_name}:{uuid[:6]}"``, and sub-agents append
#: ``:sub:<n>:<uuid>`` per nesting level). A 128-char cap here would refuse
#: every frame belonging to a conversation branched under a long name.
MAX_SESSION_ID_LEN = 1024
#: Short human-facing labels: names, models, tool names, provider tags.
MAX_NAME_LEN = 256
#: Opaque credentials (session tokens). Generous; they are not free text.
MAX_TOKEN_LEN = 4096
#: Capability / sensor / tag style lists. Generous by design: the point is to
#: refuse a million-element list, not to guess how many sensors a node has.
MAX_LIST_ITEMS = 512
#: How many transcript ids one ``ambient_digest_request`` may carry.
#:
#: Deliberately NOT MAX_LIST_ITEMS. The reply is one ``ambient_digest``
#: frame per id, and each can carry a ``detail`` of up to 20,000 chars
#: (``agents/ambient_transcript.py`` caps it there), so 512 ids is a ~10MB
#: burst across 512 frames at the exact moment a phone reconnects, which
#: is when it is most likely to be on cellular. 64 keeps the worst case
#: near 1.3MB and a phone with more than that simply asks again; the
#: reply carries ``remaining`` so it knows to.
MAX_DIGEST_REQUEST_ITEMS = 64
#: Filesystem path fields. Linux PATH_MAX is 4096.
MAX_PATH_LEN = 4096
#: Pixel dimension ceiling, mirroring the node SDK's ``width`` / ``height``.
MAX_PIXELS = 8192
#: Decoded (not base64-character) size cap for video-class frames.
#: HUP_SPEC.md section 5.4.2 / 5.4.3. This is the only declaration: F-03 made
#: ``api/server.py`` import it instead of keeping a second copy, because two
#: copies is how the model layer and the handler came to measure different
#: quantities against the same number.
VIDEO_FRAME_MAX_BYTES = 512 * 1024


def decoded_b64_size(value: str) -> int:
    """Return the DECODED byte length of a base64 string.

    Measuring ``len(value)`` instead is the F-03 defect: base64 inflates 4/3,
    so a character count turned the 512 KiB cap into a 384 KiB one and a legal
    400 KiB JPEG was dropped with a log-only warning.

    Computed arithmetically rather than by ``b64decode`` because the frame
    handlers in ``api/server.py`` call this once per frame at camera frame
    rate, and decoding would allocate a full copy of every frame purely to
    measure it. Embedded whitespace (MIME-wrapped base64) is stripped first so
    the arithmetic stays exact for that shape too.
    """
    if not value:
        return 0
    if "\n" in value or "\r" in value or " " in value:
        value = "".join(value.split())
        if not value:
            return 0
    padding = 2 if value.endswith("==") else 1 if value.endswith("=") else 0
    return (len(value) * 3) // 4 - padding


def _decoded_size_guard(value: str, cap: int, label: str) -> str:
    """Reject a base64 blob whose DECODED size exceeds ``cap``.

    Measuring the encoded string instead is the F-03 defect: base64 inflates
    4/3, so a character count turns a 512 KiB cap into a 384 KiB one and
    silently drops legal 400 KiB JPEGs.

    This decodes rather than calling :func:`decoded_b64_size` because it is a
    validator and must also reject a blob that is not base64 at all. The two
    agree on every well-formed input, which is the only input that reaches
    the handlers.
    """
    import base64

    try:
        decoded = base64.b64decode(value, validate=False)
    except Exception as exc:
        raise ValueError(f"data_b64 is not valid base64: {exc}") from exc
    if len(decoded) > cap:
        raise ValueError(
            f"{label} data_b64 decoded to {len(decoded)} bytes; cap is {cap}"
        )
    return value


# ─────────────────────────────────────────────
# The Universal Message Envelope
# ─────────────────────────────────────────────

class FeralMessage(BaseModel):
    """Every message in the system uses this envelope."""
    msg_id: str = Field(default_factory=lambda: str(uuid4()), max_length=MAX_ID_LEN)
    session_id: str = Field(default="", max_length=MAX_SESSION_ID_LEN)
    # ge=0: a unix epoch in milliseconds is never negative. A device with a
    # broken clock sending a negative timestamp used to be accepted and then
    # ordered ahead of every real message in the timeline.
    timestamp_ms: int = Field(default_factory=lambda: int(time() * 1000), ge=0)
    hop: Literal["client", "brain", "daemon", "skill"] = "client"
    # Discriminator (see payload types below). Bounded because it is looked up
    # in MESSAGE_TYPES and logged verbatim on a miss.
    type: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    payload: dict = Field(default_factory=dict)


# ─────────────────────────────────────────────
# Payload Models — Client → Brain
# ─────────────────────────────────────────────

class AudioChunkPayload(BaseModel):
    """Streaming audio from client to brain.

    ``data_b64`` carries no decoded-size cap on purpose. The only documented
    audio cap in this system is ``AUDIO_FRAME_MAX_BYTES`` (64 KiB), and it
    governs the HUP ``audio_frame`` envelope, which is a different message
    type that is not registered in ``MESSAGE_TYPES``. Applying it here would
    be inventing a bound: a two-second 24 kHz pcm16 chunk is 96,000 bytes and
    would start being rejected. See the F-02 note in AUDIT-FIXES.md.
    """
    encoding: str = Field(default="opus", max_length=32)
    # ge=1 rather than a range: a zero or negative sample rate is nonsense,
    # but no upper bound is invented here because the SDK's 8k-96k range
    # belongs to the HUP ``audio_frame`` envelope, not to this one.
    sample_rate: int = Field(default=24000, ge=1)
    channels: int = Field(default=1, ge=1)
    chunk_index: int = Field(default=0, ge=0)
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
    # ``upload_id`` is joined onto a filesystem path under $FERAL_HOME/uploads,
    # so an empty or unbounded value is worth refusing at the wire.
    upload_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    filename: str = Field(default="", max_length=MAX_NAME_LEN)
    content_type: str = Field(default="", max_length=MAX_NAME_LEN)
    size_bytes: int = Field(default=0, ge=0)
    # A hex sha256 digest is exactly 64 characters.
    sha256: str = Field(default="", max_length=64)


class TextCommandPayload(BaseModel):
    """Text input (for web/CLI clients that type instead of speak).

    PR 10: an optional ``attachments`` list lets the composer ship
    file references alongside the prompt without inlining bytes.

    ``text`` is deliberately unbounded. It is a user's prompt, and pasting a
    long document into the composer is a supported thing to do; any character
    ceiling here would be a guess that silently truncates real work.
    """
    text: str
    context: Optional[dict] = None
    attachments: Optional[list[AttachmentRef]] = Field(
        default=None, max_length=MAX_LIST_ITEMS
    )


class BiometricPayload(BaseModel):
    """Sensor data from glasses or phone.

    Every numeric field here is deliberately left unbounded (F-02). A ceiling
    on heart rate, SpO2, skin temperature or UV index would be a SEMANTIC
    guess: 220 bpm during a sprint and 43 C in a sauna are both real, and this
    protocol already uses out-of-range sentinels for "unknown" elsewhere
    (``GlassesStatusPayload.battery_level = -1``). A wrong ceiling would turn
    a working device into a rejected one, which is strictly worse than the
    unbounded int it replaces. Only the structural bounds are applied.
    """
    heart_rate_bpm: Optional[int] = None
    spo2_pct: Optional[int] = None
    # Three axes. The field name is the contract and every consumer indexes
    # [0]/[1]/[2]; an unbounded float list here is a free allocation vector.
    accel_xyz: Optional[list[float]] = Field(default=None, max_length=3)
    temperature_c: Optional[float] = None
    uv_index: Optional[int] = None
    gps: Optional[dict] = None  # {"lat": float, "lon": float}
    inferred_state: Optional[str] = Field(
        default=None, max_length=MAX_NAME_LEN
    )  # "resting", "walking", "running", "stressed"


class UIEventPayload(BaseModel):
    """User interacted with a generated UI element.

    ``app_id`` is optional and backward-compatible: legacy SDUI /
    proactive events still work without it. When present, the brain
    routes the event through ``AppRegistry.validate_action`` first so
    third-party apps can't dispatch to skill endpoints they didn't
    declare in their surface's ``action_contract``.
    """
    screen_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    event: Literal["tap", "toggle", "slider", "text_input", "dismiss"]
    # Routed through AppRegistry.validate_action, so it is an identifier the
    # brain looks up, never free text.
    action_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    value: Optional[Any] = None
    app_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)


# ─────────────────────────────────────────────
# Payload Models — Phone-as-peer Envelopes (HUP v1.3)
# ─────────────────────────────────────────────

class ChatRequestPayload(BaseModel):
    """Phone text/vision query request routed through the orchestrator.

    ``text`` is unbounded for the same reason as ``TextCommandPayload.text``.
    """
    session_id: str = Field(..., min_length=1, max_length=MAX_SESSION_ID_LEN)
    text: str
    reply_mode: Literal["stream", "final"] = "final"
    channel: Literal["chat", "vision_ask"] = "chat"
    reply_to: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
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

    ``session_id`` carries a length cap but no ``min_length``: the brain
    builds this frame as a hand-written dict in ``api/server.py`` rather than
    through this model, so there is no way to prove from here that it never
    emits an empty session id, and refusing one would drop a real reply.
    ``error`` is the operator-visible failure text and stays unbounded.
    """
    session_id: str = Field(..., max_length=MAX_SESSION_ID_LEN)
    text: str
    reply_mode: Literal["stream", "final"] = "final"
    channel: Literal["chat", "vision_ask"] = "chat"
    reply_to: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    error: Optional[str] = None
    somatic: Optional["SomaticStatePayload"] = None
    """The behavioural policy in force for THIS reply, or None.

    Present so a client can attribute the shape of an answer to the
    state that produced it, on the same frame as the answer. The
    unsolicited ``somatic_state`` frame reports the policy when it
    changes; this reports the policy that was actually applied to a
    given turn, which is the thing you can point at on camera.

    None when no somatic engine is attached or no biometric reading has
    ever landed. Deliberately not an empty object: "the agent is not
    adapting" and "the agent is adapting to a neutral state" are
    different claims and a UI should be able to tell them apart.
    """


class VoiceSessionStartPayload(BaseModel):
    """Phone voice session bootstrap metadata."""
    stream_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    sample_rate: int = Field(..., ge=1)
    channels: int = Field(..., ge=1)
    language_hint: str = Field(default="en-US", max_length=64)
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
    stream_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    reason: str = Field(default="user_interrupt", max_length=MAX_NAME_LEN)


class GenUIPushActionPayload(BaseModel):
    """Action button attached to a GenUI push card."""
    id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    label: str = Field(..., max_length=MAX_NAME_LEN)
    value: dict = Field(default_factory=dict)


class GenUIPushPayload(BaseModel):
    """Brain-originated mobile GenUI push payload."""
    kind: Literal["notification", "interactive"]
    app_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    surface_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    push_id: str = Field(default="", max_length=MAX_ID_LEN)
    screen_id: str = Field(default="", max_length=MAX_ID_LEN)
    title: str = Field(..., max_length=MAX_NAME_LEN)
    # ``body`` is notification prose and stays unbounded.
    body: str = ""
    actions: list[GenUIPushActionPayload] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    sdui: Optional[dict] = None


class GenUIEventPayload(BaseModel):
    """Phone-originated GenUI interaction routed to app action handlers."""
    app_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    surface_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    event_type: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    action_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    value: Optional[Any] = None
    screen_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)


class LocationUpdatePayload(BaseModel):
    """Phone-originated geolocation update streamed over the same HUP
    WebSocket as other peer envelopes.

    Replaces the legacy ``POST /api/location/update`` HTTP path that
    relied on dashboard API key auth — phones authenticate with
    ``phone_bearer`` over WS subprotocol, so the HTTP path returned
    401 for them. Sending location as a HUP envelope gets free auth
    + lifecycle alignment with the rest of the peer streams.

    HUP v1.3.1 addition.

    ``accuracy_m`` / ``heading_deg`` / ``speed_mps`` are deliberately left
    unbounded. CoreLocation reports **-1** for each of them when the value is
    unavailable (indoors, no motion, no compass), and the iOS companion
    forwards the fix verbatim. A ``ge=0`` bound would look obviously correct
    and would reject a large share of real iPhone fixes. ``altitude_m`` is
    genuinely negative below sea level. Only ``lat`` / ``lon`` are bounded,
    to the coordinate system's own domain rather than to a guess.
    """
    node_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    accuracy_m: Optional[float] = None
    altitude_m: Optional[float] = None
    heading_deg: Optional[float] = None
    speed_mps: Optional[float] = None
    source: str = Field(default="browser_node", max_length=MAX_NAME_LEN)
    ts: Optional[float] = None


# Legacy / alternate spellings → canonical literal for bridged
# peripherals. BLE peripherals are bridged *through* the phone node and
# relayed over its WS (see hardware/adapters/bridge.py), so the canonical
# protocol for a raw "ble" device is ``native_bridge`` (NOT
# ``web_bluetooth``, which is the browser Web Bluetooth transport).
PERIPHERAL_PROTOCOL_ALIASES = {
    "ble": "native_bridge",
    "bluetooth": "native_bridge",
    "bluetooth_le": "native_bridge",
    "ble_bridge": "native_bridge",
    "bridge": "native_bridge",
    "webble": "web_bluetooth",
    "web_ble": "web_bluetooth",
}
PERIPHERAL_KIND_ALIASES = {
    "wristband": "band",
    "bracelet": "band",
    "wearable": "band",
}


class PeripheralBridgeDevicePayload(BaseModel):
    """One bridged peripheral exposed by the phone peer.

    Tolerant-by-design: phone builds (and older app installs) send
    alternate spellings for ``protocol`` and ``kind`` — most notably
    ``protocol="ble"`` (raw transport name) and ``kind="wristband"``.
    The brain treats these as aliases of its canonical literals so a
    single bad spelling never rejects the whole registration batch and
    leaves the companion app stuck "reconnecting…". New canonical
    values are still accepted unchanged; only legacy aliases are
    rewritten (see ``PERIPHERAL_PROTOCOL_ALIASES`` /
    ``PERIPHERAL_KIND_ALIASES``).
    """

    device_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    kind: Literal["glasses", "watch", "band"]
    protocol: Literal["web_bluetooth", "native_bridge", "none"]
    capabilities: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    status: Literal["connected", "connecting", "disconnected"] = "connecting"
    manifest: dict = Field(default_factory=dict)

    @field_validator("protocol", mode="before")
    @classmethod
    def _normalize_protocol(cls, value: Any) -> Any:
        if isinstance(value, str):
            return PERIPHERAL_PROTOCOL_ALIASES.get(value.strip().lower(), value)
        return value

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> Any:
        if isinstance(value, str):
            return PERIPHERAL_KIND_ALIASES.get(value.strip().lower(), value)
        return value


class PeripheralBridgeRegisterPayload(BaseModel):
    """Phone bridge registration/update payload."""
    bridge_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    platform: Literal["ios", "android"]
    devices: list[PeripheralBridgeDevicePayload] = Field(..., max_length=MAX_LIST_ITEMS)
    # ISO-8601 timestamp string.
    expires_at: str = Field(..., max_length=64)


class BackchannelRequestPayload(BaseModel):
    """Structured operator-review request from phone."""
    device_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    kind: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    payload: dict = Field(default_factory=dict)
    request_id: str = Field(default_factory=lambda: str(uuid4()), max_length=MAX_ID_LEN)
    status: str = Field(default="pending", max_length=64)


class AmbientTranscriptAckPayload(BaseModel):
    """Brain-to-client confirmation that a transcript is durably stored.

    Required, not optional. The phone queues transcripts while the brain
    is off and has no other signal to stop resending; today only
    chat_request gets a response, and location_update, backchannel_request
    and every media frame are answered with silence.

    The ack is sent once the raw text is on disk, NOT once summarization
    has finished. Summarization runs in the background and can fail or be
    interrupted; acking on its completion would mean a brain restarted
    mid-drain loses transcripts the phone has already discarded.
    ``duplicate`` tells the phone the brain had already seen this id, so a
    lost ack costs a resend and never a second episode.
    """

    transcript_id: str = Field(..., max_length=MAX_ID_LEN)
    duplicate: bool = False
    accepted: bool = True
    detail: str = Field(default="", max_length=MAX_NAME_LEN)


class AmbientDigestRequestPayload(BaseModel):
    """Phone asking for the summaries of transcripts it has already sent.

    Summarization is a background task that finishes seconds to minutes
    after the ack, by which time the phone is usually gone, so it cannot
    learn the outcome by staying connected. This is the pull leg: on
    connect the phone names the ids it has synced but holds no digest
    for. The push leg (an unsolicited ``ambient_digest`` at the end of
    processing) covers the case where it is still here.

    ``include_detail`` is off by default and that default is the whole
    point. The bulk case is a reconnect after days away, where the phone
    wants enough to render a list; ``detail`` is up to 20,000 characters
    of the conversation and is what makes that burst expensive. It asks
    for detail when a card is opened, one id at a time.
    """

    transcript_ids: list[str] = Field(
        default_factory=list, max_length=MAX_DIGEST_REQUEST_ITEMS,
    )
    include_detail: bool = False


class AmbientDigestPayload(BaseModel):
    """What the brain made of one recorded conversation.

    Sent two ways, deliberately as ONE type so the phone has a single
    inbound handler: unsolicited when processing finishes with the node
    still connected, and as the reply to ``ambient_digest_request``.

    This is the stored ``TranscriptOutcome``, not the episode fields.
    The episode is shaped for FTS and for the model's context block,
    which forces names and dates into prose and caps ``summary`` at 500;
    on a phone card that renders as a duplicated date and a truncated
    sentence.

    ``injection_flags`` is deliberately NOT here. It is stored with the
    digest because it is a useful signal in the brain's own logs, but
    shipping it to a UI invites rendering a scare banner over something
    a colleague happened to say in a meeting.

    The three statuses each carry information the phone acts on:

    ``ready``    summarized, fields populated.
    ``pending``  the brain HAS the transcript but has not finished with
                 it. Either the background task is still running or it
                 failed and the boot sweep will retry from our copy. The
                 phone shows the transcript with no summary and asks
                 again on the next connect.
    ``unknown``  no row the requester owns. This closes a real hole: the
                 phone drops its copy on ``accepted: true``, so if the
                 brain's database were restored from an older backup
                 both sides would silently believe the conversation was
                 safe. ``unknown`` is how the phone finds out, and its
                 response is to resend the transcript.

    THE INVARIANT ``unknown`` DEPENDS ON: nothing deletes from
    ``ambient_transcripts``. It is verified today and there is a test
    pinning it. If retention is ever added, ``unknown`` starts also
    meaning "expired", and a phone that treats it as "lost" will resend
    every aged-out recording forever. Add a distinct status then; do not
    widen this one.
    """

    transcript_id: str = Field(..., max_length=MAX_ID_LEN)
    status: Literal["ready", "pending", "unknown"] = "ready"
    summary: str = ""
    detail: str = ""
    people: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    topics: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    commitments: list[dict] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    degraded: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    episode_id: str = Field(default="", max_length=MAX_ID_LEN)
    processed_at: Optional[float] = Field(default=None, ge=0.0)
    #: How many more digests answer the request this one belongs to.
    #:
    #: A phone reconnecting after a week has something to wait for, and
    #: without this it cannot tell "your last digest" from "the first of
    #: forty" until the frames stop arriving. It lets the phone say it is
    #: fetching and show progress instead of appearing to hang. Zero on
    #: an unsolicited push, which is always a single digest.
    remaining: int = Field(default=0, ge=0)

    physiological_note: str = Field(default="", max_length=1000)
    """What the body did during this conversation, or "".

    A separate field rather than a sentence folded into ``summary`` so a
    client can render it distinctly, or suppress it entirely, and so a
    reader can always tell which part of the record is what people said
    and which part is what a heart rate did.

    Guaranteed never to describe an emotional state, and never derived
    from a movement-confounded moment: confounded moments are dropped
    before the model sees them, and the sentence it returns is checked
    again afterwards. See agents/ambient_transcript.usable_moments and
    sanitise_physiological_note.
    """

    moments_considered: int = Field(default=0, ge=0)
    """Moments that survived the confound and confidence filters.

    Lets a client distinguish "no physiological signal was measured"
    from "signal was measured and said nothing worth reporting", which
    an empty note alone cannot.
    """


class AmbientMomentPayload(BaseModel):
    """One point in a recorded conversation where the body reacted.

    Detected on the phone, which holds the raw heart-rate series aligned
    to the audio. The brain never computes these; it reasons over them,
    and the summary is the only place they surface.

    ``confounded`` IS THE MOST IMPORTANT FIELD IN THIS MODEL. It means
    movement explains the rise: the wearer stood up, walked, climbed
    stairs. A confounded moment is a fact about physics, not about
    feeling, and a summary that narrates it as an emotional response
    ("his heart rate spiked when the investor update came up") is
    fabricating an inner state from a flight of stairs. That is the
    difference between a health product and a liability, so the
    prohibition is enforced in the prompt AND independently after the
    model returns, rather than trusted to either alone.

    ``segment_index`` INDEXES THE PHONE'S OWN SEGMENTATION, not the
    brain's. ``agents/ambient_transcript.py`` chunks the transcript into
    6000-character map segments and labels them ``[segment N]``; those
    are a different partition of the same conversation and the two
    numberings do not correspond. Nothing may join on the bare index.
    ``quote`` and ``t_offset_s`` are how a moment is actually placed in
    the text, and a moment carrying neither is reported to the model as
    an unanchored session-level observation.
    """

    segment_index: int = Field(default=0, ge=0)
    """Index into the PHONE's segmentation. See the class docstring."""

    delta_bpm: float = Field(default=0.0, ge=-300.0, le=300.0)
    """Heart-rate deviation from ``baseline_hr``, signed."""

    score: float = Field(default=0.0, ge=0.0, le=1.0)
    """The phone's confidence that this is a real reaction, 0-1."""

    confounded: bool = False
    """Movement explains the rise. NEVER describe as an emotional response."""

    quote: str = Field(default="", max_length=1000)
    """Optional. The spoken words at this moment, for anchoring."""

    t_offset_s: Optional[float] = Field(default=None, ge=0.0)
    """Optional. Seconds from ``started_at``, for anchoring."""


class AmbientTranscriptPayload(BaseModel):
    """A finished ambient conversation transcribed on the phone.

    The glasses are the microphone, the phone is the recorder, and the
    phone is the only thing that talks to the brain. ``source`` is
    provenance, never a transport and never somewhere to route an action.

    Named ``ambient_transcript`` and not ``transcript`` because that key
    is already bound to the brain-to-client TranscriptPayload; reusing it
    would silently reinterpret every outbound frame.

    ``started_at`` is the REAL capture time, not ingestion time. The
    phone queues while the brain is off, so a transcript normally lands
    hours or days after the conversation, and timeline recall filters on
    created_at alone.

    ``text`` is unbounded per the convention above: prose fields carry
    meaning that a structural cap would silently destroy.
    """

    transcript_id: str = Field(default_factory=lambda: str(uuid4()), max_length=MAX_ID_LEN)
    text: str
    session_id: str = Field(default="", max_length=MAX_SESSION_ID_LEN)
    node_id: str = Field(default="", max_length=MAX_ID_LEN)
    device_id: str = Field(default="", max_length=MAX_ID_LEN)
    started_at: Optional[float] = Field(default=None, ge=0.0)
    ended_at: Optional[float] = Field(default=None, ge=0.0)
    source: Literal["phone_mic", "glasses_mic", "theora_eye", "unknown"] = "phone_mic"
    language: str = Field(default="en-US", max_length=64)
    speakers: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)

    # ── Physiology alongside the words ──────────────────────────────
    #
    # All optional. A phone that computes none of this sends none of it
    # and the transcript is summarized exactly as before.
    moments: list["AmbientMomentPayload"] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS,
    )
    """Points in the conversation where the body reacted.

    Computed on the phone, which is the only side that has the raw
    heart-rate series aligned to the audio. The brain does not detect
    these; it reasons over them.
    """

    baseline_hr: Optional[float] = Field(default=None, ge=0.0, le=300.0)
    """Session-level resting heart rate the deltas are measured against.

    Without it a delta is uninterpretable: +12 bpm off a baseline of 55
    is a different event from +12 off 95.
    """

    respiratory_bpm: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    """Session-level respiration rate, breaths per minute."""


# ─────────────────────────────────────────────
# Payload Models — Brain → Client
# ─────────────────────────────────────────────

class SomaticStatePayload(BaseModel):
    """The body state the agent is currently acting on, and what it did
    about it.

    This exists because the behavioural policy was applied invisibly.
    The agent read the somatic vector, shortened its answers, went quiet
    and restricted its own tools, and none of that was observable from
    outside: a shorter reply is indistinguishable from a reply that
    happened to be short. Anything a system changes about itself in
    response to the user's body has to be inspectable, both to prove it
    is working and to let the user disagree with it.

    Two halves, deliberately in one frame. ``cognitive_load`` and the
    vitals are the INPUT; ``tone``, ``suppress_non_urgent`` and
    ``tool_restrictions`` are the OUTPUT the policy derived from it.
    Shipping only the second half gives a UI no way to explain itself,
    and only the first gives it nothing to show.

    ``stale`` is not decoration. A somatic vector persists in memory
    after the wearable disconnects, so a policy can go on being applied
    from a reading taken hours ago. A client must not present a stale
    frame as the wearer's current state.
    """

    session_id: str = Field(default="", max_length=MAX_SESSION_ID_LEN)

    # Input: what the body is doing.
    cognitive_load: float = Field(default=0.0, ge=0.0, le=1.0)
    stress_level: float = Field(default=0.0, ge=0.0, le=1.0)
    fatigue_level: float = Field(default=0.0, ge=0.0, le=1.0)
    heart_rate: float = Field(default=0.0, ge=0.0)
    hrv_ms: float = Field(default=0.0, ge=0.0)
    spo2_pct: float = Field(default=0.0, ge=0.0)
    activity_level: float = Field(default=0.0, ge=0.0, le=1.0)

    # Output: what the agent is doing about it.
    tone: str = Field(default="normal", max_length=MAX_NAME_LEN)
    proactive_level: str = Field(default="normal", max_length=MAX_NAME_LEN)
    suppress_non_urgent: bool = False
    max_response_tokens: Optional[int] = Field(default=None, ge=0)
    tool_restrictions: list[str] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS,
    )

    # Provenance.
    updated_at: float = Field(default=0.0, ge=0.0)
    age_s: float = Field(default=0.0, ge=0.0)
    stale: bool = False
    has_biometrics: bool = False
    reason: str = Field(default="", max_length=MAX_NAME_LEN)
    """Why this frame was sent: "biometrics", "poll" or "chat_turn"."""


# ChatResponsePayload.somatic is annotated with this class by name,
# hundreds of lines before it exists. `from __future__ import
# annotations` makes every annotation in this module a string, so
# pydantic cannot resolve that reference at class-creation time and
# leaves the model incomplete: constructing one raises
# PydanticUserError about an undefined annotation. Rebuilding here, at
# the first point where the name is bound, completes it.
ChatResponsePayload.model_rebuild()


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

    Ordering fields (``item_id`` / ``previous_item_id`` / ``seq``)
    exist because transcript frames do NOT arrive in conversation
    order. OpenAI's Realtime docs say
    ``conversation.item.input_audio_transcription.completed`` "runs
    asynchronously with Response creation, so this event may come
    before or after the Response events" — the user's own words can
    land after the assistant reply that answered them. A client that
    appends by arrival time renders the turn inverted (operator
    report 2026-07-28). Clients must order by this metadata, not by
    arrival:

      * ``item_id`` — provider-stable identity for the conversation
        item. Also the replace key: a late final with the same
        ``item_id`` as an earlier partial supersedes it in place
        rather than appending a duplicate bubble.
      * ``previous_item_id`` — the item this one follows. OpenAI
        supplies it on ``conversation.item.added`` and
        ``input_audio_buffer.committed``; chained together the links
        form the provider's canonical order.
      * ``seq`` — brain-assigned per-session monotonic counter, the
        provider-agnostic fallback for Gemini Live and the chained
        whisper path, which supply no item identity at all.

    All three are optional: older brains omit them and older clients
    ignore them, so the wire stays backward compatible in both
    directions.

    ``confidence`` is deliberately unbounded. It is a provider-reported
    score and this brain routes 16 of them; Deepgram normalises to [0, 1] but
    nothing verifies that the other STT backends do, and this frame is
    brain-to-client, so a bound buys no security while a wrong one drops the
    transcript entirely.
    """
    text: str
    is_partial: bool = False
    confidence: float = 1.0
    role: Optional[str] = Field(default="assistant", max_length=32)
    item_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    previous_item_id: Optional[str] = Field(default=None, max_length=MAX_ID_LEN)
    # Brain-assigned per-session monotonic counter, so never negative.
    seq: Optional[int] = Field(default=None, ge=0)


class SDUIPayload(BaseModel):
    """Server-Driven UI — the generated interface."""
    screen_id: str = Field(default_factory=lambda: str(uuid4()), max_length=MAX_ID_LEN)
    ttl_seconds: int = Field(default=300, ge=0)
    root: dict  # The SDUI tree (see genui/schema/)


class SDUIPatchPayload(BaseModel):
    """Partial update to an existing generated screen.

    ``patches`` is unbounded: it is brain-generated and its length is a
    function of how much of the screen changed, which nothing here can
    predict without risking a dropped update.
    """
    screen_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    patches: list[dict]  # [{"path": "children.0.value", "op": "replace", "value": "new text"}]


class TTSChunkPayload(BaseModel):
    """Streaming audio from brain to client (text-to-speech).

    ``data_b64`` is brain-generated TTS output with no documented cap, so
    none is invented here.
    """
    chunk_index: int = Field(default=0, ge=0)
    encoding: str = Field(default="mp3", max_length=32)
    data_b64: str = ""
    is_final: bool = False


class TextResponsePayload(BaseModel):
    """Plain text response (for CLI/chat clients)."""
    text: str
    tool_calls: Optional[list[dict]] = Field(default=None, max_length=MAX_LIST_ITEMS)
    # Per-turn attribution, same contract as ``StreamDeltaPayload`` below.
    # This is the path a DEFAULT install actually uses for chat, because
    # ``features.streaming`` defaults to False, so it needs the fields at
    # least as much as the streaming one does. ``usage`` here is summed
    # across every LLM round the turn made, not just the final one.
    model: str = Field(default="", max_length=MAX_NAME_LEN)
    usage: dict = Field(default_factory=dict)


class StreamDeltaPayload(BaseModel):
    """Streaming text token from brain to client (real-time LLM output)."""
    delta: str
    stream_id: str = ""
    is_final: bool = False
    # Per-turn attribution, set only on the terminal frame (is_final=True).
    #
    # ``model`` is the model that ACTUALLY ANSWERED, which is not always the
    # configured one: the failover chain can hop providers mid-turn, so a UI
    # that shows ``llm.model`` from settings can be wrong without any way for
    # the user to tell. Empty when the provider did not report it.
    #
    # ``usage`` is ``{input_tokens, output_tokens, total_tokens}``. Empty when
    # the provider reported none. The chat-completions streaming path only
    # emits usage when the request sets ``stream_options.include_usage``,
    # which FERAL now does (an endpoint that rejects the key is retried once
    # without it, and then reports no usage). The Responses API reports it on
    # the terminal event with no opt-in. Anthropic splits it across
    # ``message_start`` (input) and ``message_delta`` (output).
    model: str = Field(default="", max_length=MAX_NAME_LEN)
    usage: dict = Field(default_factory=dict)


class ToolStartPayload(BaseModel):
    """Brain notifies client that a tool call has begun.

    Renders as a chip or equivalent affordance in the UI so the user
    sees what the agent is doing without the model having to narrate
    it in prose. ``args_preview`` is a short, redacted JSON string
    suitable for a one-line display — not the full argument blob.
    """
    tool: str = Field(..., max_length=MAX_NAME_LEN)
    call_id: str = Field(default="", max_length=MAX_ID_LEN)
    skill_id: str = Field(default="", max_length=MAX_ID_LEN)
    endpoint_id: str = Field(default="", max_length=MAX_ID_LEN)
    # The orchestrator truncates this to 160 characters before sending
    # (agents/orchestrator.py). 4096 is a generous structural ceiling that
    # cannot clip anything the brain actually emits.
    args_preview: str = Field(default="", max_length=4096)
    display_name: str = Field(default="", max_length=MAX_NAME_LEN)


class ToolResultPayload(BaseModel):
    """Brain notifies client that a tool call finished.

    Paired with ``tool_start`` by ``call_id`` when present. The client
    uses this to clear the active-tool chip and (optionally) record a
    per-turn activity row.
    """
    tool: str = Field(..., max_length=MAX_NAME_LEN)
    call_id: str = Field(default="", max_length=MAX_ID_LEN)
    success: bool = True
    error: str = ""
    # Machine-readable reason the call did not run, when it was DECLINED
    # rather than attempted: "plan_mode_blocked", "policy_denied",
    # "pending_approval". Empty for a normal success or a genuine failure.
    #
    # A refusal and a crash both arrive with success=False, so without this
    # the client rendered "FERAL declined this on purpose" and "the tool
    # threw" identically, as a red failure. The client keys its refused
    # state off this code and never off the error prose, which is
    # user-facing copy that changes.
    error_code: str = Field(default="", max_length=64)
    latency_ms: float = Field(default=0.0, ge=0.0)
    # Human-readable excerpt of what the tool returned, for the chat UI's
    # result renderer. OPT-IN per endpoint via ``emit_result_preview`` in the
    # skill manifest, and default OFF: tool results routinely carry vault
    # reads, API responses holding tokens, file contents and mail bodies,
    # and this codebase has no redaction pass to lean on. Empty string means
    # "not offered for this endpoint", which the client renders as an
    # explicit note rather than implying the tool returned nothing.
    result_preview: str = ""
    # True when ``result_preview`` was cut to fit, so the client can say so
    # instead of presenting a fragment as the whole result.
    result_preview_truncated: bool = False


class GesturePayload(BaseModel):
    """Gesture detected by a hardware daemon (glasses IMU, camera, etc.).

    ``confidence`` is left unbounded for the same reason as
    ``TranscriptPayload.confidence``: it is a detector-reported score whose
    normalisation this file cannot verify.
    """
    gesture: str = Field(..., min_length=1, max_length=64)  # "nod", "shake", "look_up", ...
    confidence: float = 1.0
    source: str = Field(default="imu", max_length=64)  # "imu", "camera", "touch"


class RefusalPayload(BaseModel):
    """Structured refusal from the supervisor / orchestrator.

    Emitted when the brain explicitly declines to act — supervisor
    paused, policy gate denied, autonomy mode rejected the request.
    The consumer renders this as a yellow chip in chat plus an
    actionable ``retry_hint`` (e.g. "resume supervisor in Settings →
    Oversight"). Pinned by Lane 08 WS6.
    """
    # ``reason`` and ``retry_hint`` are user-facing prose and stay unbounded;
    # truncating a refusal explanation removes the only thing the user gets.
    reason: str
    retry_hint: str = ""
    source: str = Field(default="supervisor", max_length=64)  # supervisor | policy | ...
    kind: str = Field(default="", max_length=64)  # command | command_stream | ui_event | ...


class BudgetExceededPayload(BaseModel):
    """Structured cost-budget refusal.

    Emitted when a CostBudget.check_and_reserve / record_usage call
    flagged the request as breaching a per-call-site or global cap.
    Lane 12 renders this as a yellow banner: "Chat budget reached
    ($X.XX / hour). Resets at HH:MM." Pinned by Lane 08 WS8.
    """
    # Length cap only, no ``min_length``. The orchestrator builds this frame
    # as ``str(budget.get("call_site") or call_site)`` (agents/orchestrator.py),
    # so an empty label is reachable, and a ValidationError there would turn a
    # budget banner into an exception on the refusal path.
    call_site: str = Field(..., max_length=MAX_NAME_LEN)
    cap_dollars: float = Field(default=0.0, ge=0.0)
    current_dollars: float = Field(default=0.0, ge=0.0)
    window: str = Field(default="hour", max_length=32)  # hour | day
    reset_at: float = Field(default=0.0, ge=0.0)  # unix epoch seconds when the cap resets


class ErrorPayload(BaseModel):
    """Something went wrong.

    ``message`` is operator-facing prose and stays unbounded.
    """
    code: str = Field(..., max_length=64)
    message: str
    recoverable: bool = True


class TimelinePayload(BaseModel):
    """Fused-timeline frame for S1 ("what did I do yesterday?").

    Emitted by the orchestrator in parallel with the streaming chat
    response when the LLM (or heuristic router) dispatches
    ``notes_memory__fused_timeline``. Lets the WebUI mount a
    TimelineCard immediately instead of waiting for the model to
    finish narrating.

    ``window`` is the parsed temporal range: ``{"from": iso,
    "to": iso, "label": "yesterday"}``. ``entries`` is the flat,
    chronologically sorted list of typed entries (episode | note |
    knowledge | event | health | …); the client groups by
    ``source`` for the collapsible-sections view. ``degraded_sources``
    parallels the timeout-degraded pattern from
    ``memory/store.py::stats`` — every source that the fusion tried
    to query but failed (no token, no client, exception) lands here
    as ``{"source": ..., "reason": ...}`` so the UI can render an
    honest chip instead of a silently missing section.

    ``entries`` is unbounded on purpose: it is the brain's own fusion result
    and its length is the answer to the user's question ("what did I do last
    month?"), so any cap here would silently truncate a correct reply.
    """
    session_id: str = Field(default="", max_length=MAX_SESSION_ID_LEN)
    query: str = ""
    window: dict = Field(default_factory=dict)
    entries: list[dict] = Field(default_factory=list)
    summary: str = ""
    sources_queried: list[str] = Field(default_factory=list)
    degraded_sources: list[dict] = Field(default_factory=list)


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

    The node SDK additionally pins ``node_id`` to
    ``^[A-Za-z0-9._:-]{1,128}$``. That character class is NOT mirrored here:
    it is a stronger claim than a length bound, and non-SDK nodes (the Swift
    and Kotlin bridges) are not known to honour it, so adopting it would
    disconnect already-paired hardware over a character this brain has never
    objected to. The length half of the SDK's bound is adopted.
    """
    node_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    node_type: Literal[
        "desktop", "server", "rpi", "robot", "glasses", "phone",
        "tablet", "actuator", "sensor", "wearable", "camera",
        "vehicle", "appliance", "browser_camera", "browser_node",
    ]
    os: str = Field(default="", max_length=MAX_NAME_LEN)
    platform: str = Field(default="", max_length=MAX_NAME_LEN)  # "ios", "android", ...
    manufacturer: str = Field(default="", max_length=MAX_NAME_LEN)
    model: str = Field(default="", max_length=MAX_NAME_LEN)
    firmware_version: str = Field(default="", max_length=MAX_NAME_LEN)
    capabilities: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
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
    skills: list[dict] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class ExecuteCommandPayload(BaseModel):
    """Brain tells daemon to do something.

    ``action`` is the script body and is deliberately unbounded: an
    AppleScript or shell payload has no natural length.
    """
    command_id: str = Field(default_factory=lambda: str(uuid4()), max_length=MAX_ID_LEN)
    executor: str = Field(..., min_length=1, max_length=64)  # "applescript", "shell", ...
    action: str  # The actual command/script
    args: dict = Field(default_factory=dict)
    # ge=0 only. The node SDK's ``le=120_000`` on the equivalent HUP field is
    # NOT adopted: this brain already dispatches a 30,000 ms tool timeout and
    # computes others from arbitrary seconds (agents/tool_runner.py,
    # hardware/mesh.py), so an upper bound risks refusing the brain's own
    # commands. Recorded under F-02 rather than guessed at.
    timeout_ms: int = Field(default=5000, ge=0)
    requires_confirmation: bool = False


class ExecuteResultPayload(BaseModel):
    """Daemon reports back the result.

    ``exit_code`` is unbounded in both directions: POSIX reports a
    signal-terminated process as a negative code, and stdout/stderr are
    program output with no natural ceiling.
    """
    command_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    status: Literal["success", "failure", "denied", "timeout"]
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


# ─────────────────────────────────────────────
# Payload Models — Vision Pipeline (Daemon ↔ Brain)
# ─────────────────────────────────────────────

class VisionFramePayload(BaseModel):
    """Daemon pushes a captured camera frame to the brain.

    ``data_b64`` carries NO decoded-size cap here, unlike
    ``GlassesFramePayload``. The cap that governs this frame is
    ``VISION_MAX_FRAME_KB`` (``api/state.py``), which is operator-tunable via
    ``FERAL_VISION_MAX_FRAME_KB``. Hard-coding 512 KiB into the model would
    override that setting and reject frames on any install that raised it, so
    the runtime check stays the single source of truth. Recorded under F-02.
    """
    node_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    frame_id: str = Field(default_factory=lambda: str(uuid4()), max_length=MAX_ID_LEN)
    encoding: Literal["jpeg", "png", "webp"] = "jpeg"
    # Exactly [width, height]; every consumer indexes [0] and [1].
    resolution: list[int] = Field(
        default_factory=lambda: [640, 480], min_length=2, max_length=2
    )
    data_b64: str = ""
    timestamp: float = Field(default_factory=time, ge=0.0)
    metadata: dict = Field(default_factory=dict)  # scene_brightness, faces_detected, etc.


class VisionRequestPayload(BaseModel):
    """Brain requests a frame capture from a daemon's camera."""
    resolution: str = Field(default="640x480", max_length=32)
    # 1-100 is the JPEG quality scale itself, already documented on this line
    # before F-02; it is a format constant, not an invented ceiling.
    quality: int = Field(default=80, ge=1, le=100)  # JPEG quality 1-100
    reason: str = ""


# ─────────────────────────────────────────────
# Payload Models — Device Registration
# ─────────────────────────────────────────────

class DeviceRegisterPayload(BaseModel):
    """Hardware device (glasses, robot, etc.) registers with the brain.

    ``battery_pct`` is left unbounded here even though the node SDK bounds
    the same-named field on ``node_heartbeat``. That bound is only adopted
    where the SDK already enforces it; on this envelope nothing does, and
    this file's own ``GlassesStatusPayload.battery_level = -1`` shows the
    protocol uses out-of-range battery sentinels for "unknown".
    """
    device_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    device_type: Literal["glasses", "phone", "watch", "robot", "camera", "sensor_hub"]
    name: str = Field(default="", max_length=MAX_NAME_LEN)
    # ["heart_rate", "spo2", "accelerometer", "uv", "temperature", "camera"]
    sensors: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    firmware_version: str = Field(default="", max_length=MAX_NAME_LEN)
    battery_pct: Optional[int] = None


# ─────────────────────────────────────────────
# Payload Models — Phone Bridge (iOS/Android → Brain)
# ─────────────────────────────────────────────

class SensorTelemetryPayload(BaseModel):
    """Single sensor reading from FERAL glasses via phone bridge.

    ``sensor`` is the canonical field name ("heart_rate", "spo2",
    "temperature", "uv", "steps", ...). Legacy iOS clients that still
    ship the pre-v2026.5.43 ``FeralBrainClient.sendSensorData(type:
    String, ...)`` overload emit the field as ``sensor_type``; the
    Pydantic alias below auto-maps that legacy key onto ``sensor`` so
    ``parse_message`` succeeds against both wire shapes until the
    iOS-side fix rolls out via the App Store. See
    ``AUDIT-r14/round3/findings/lane8-daemon-shell-and-healthkit.md``
    §B3 for the migration plan.
    """
    node_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    sensor: str = Field(
        ...,
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("sensor", "sensor_type"),
    )
    data: dict  # Sensor-specific values
    timestamp: str = Field(default="", max_length=64)
    source: str = Field(default="feral_glasses", max_length=MAX_NAME_LEN)


class SensorBatchPayload(BaseModel):
    """Multiple sensor readings in one message."""
    node_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    readings: dict  # {"heart_rate": {...}, "spo2": {...}, ...}
    timestamp: str = Field(default="", max_length=64)
    source: str = Field(default="feral_glasses", max_length=MAX_NAME_LEN)


class GlassesStatusPayload(BaseModel):
    """Phone reports glasses connection status.

    ``battery_level`` stays unbounded: **-1 is this model's own default** and
    means "unknown". A ``ge=0`` bound would reject the value the brain itself
    declares, which is the exact failure mode F-02 warns about.
    """
    node_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    glasses_connected: bool = False
    battery_level: int = -1
    glasses_model: str = Field(default="FERAL", max_length=MAX_NAME_LEN)


class SkillApprovalPayload(BaseModel):
    """User approved/rejected a proposed skill."""
    skill_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    approved: bool = False


class ConfirmationResponsePayload(BaseModel):
    """User responded to a permission confirmation."""
    action: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    approved: bool = False


class PermissionRequestPayload(BaseModel):
    """Agent requests folder access from the user."""
    request_id: str = Field(
        default_factory=lambda: str(uuid4())[:8], max_length=MAX_ID_LEN
    )
    # This is a filesystem path the brain will ask permission to open.
    path: str = Field(..., min_length=1, max_length=MAX_PATH_LEN)
    operation: Literal["read", "write", "readwrite"] = "read"
    reason: str = ""


class PermissionResponsePayload(BaseModel):
    """User grants or denies folder access."""
    request_id: str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    granted: bool = False
    mode: str = Field(default="read", max_length=32)


# ─────────────────────────────────────────────
# Payload Models — Voice Pipeline
# ─────────────────────────────────────────────

class VoiceConfigPayload(BaseModel):
    """Client/node declares voice capabilities and selected mode."""
    node_id: str = Field(default="", max_length=MAX_ID_LEN)
    supports_realtime: bool = False
    mode: Literal["realtime", "whisper", "auto", "disabled"] = "auto"
    preferred_model: str = Field(default="", max_length=MAX_NAME_LEN)
    sample_rate: int = Field(default=24000, ge=1)
    encoding: str = Field(default="pcm16", max_length=32)

class AudioResponsePayload(BaseModel):
    """Brain sends audio back to a node (realtime TTS or Whisper TTS).

    ``data_b64`` is brain-generated audio with no documented cap, so none is
    invented (see ``AudioChunkPayload``).
    """
    data_b64: str = ""
    encoding: str = Field(default="pcm16", max_length=32)
    sample_rate: int = Field(default=24000, ge=1)
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
    # ``reason`` / ``cause`` are machine tags; ``detail`` / ``summary`` /
    # ``recommendation`` are operator-facing prose and stay unbounded.
    reason: str = Field(default="", max_length=MAX_NAME_LEN)
    provider: str = Field(default="", max_length=MAX_NAME_LEN)
    fallback_provider: str = Field(default="", max_length=MAX_NAME_LEN)
    detail: str = ""
    #: Live microphone mute state for the session, stamped onto EVERY
    #: voice_status frame by ``VoiceRouter._emit_voice_status``. Clients
    #: reconcile their mic indicator from the newest frame, so a status
    #: frame that omitted this would flip the UI back to "listening"
    #: over a microphone the brain is still refusing to read.
    muted: bool = False
    #: Human-facing failure diagnosis (``voice/diagnostics.py``).
    #: ``cause`` is a machine tag, ``summary`` says what is wrong and
    #: ``recommendation`` says what to do about it. All three are empty
    #: when there is nothing to diagnose, and ``cause="unknown"`` when
    #: the cause genuinely could not be determined -- never a guess.
    cause: str = Field(default="", max_length=MAX_NAME_LEN)
    summary: str = ""
    recommendation: str = ""
    #: True when a user who chose local/private voice would be served
    #: by a cloud provider instead. Never reported as success.
    privacy_downgrade: bool = False

class VisionQueryPayload(BaseModel):
    """User explicitly asks about what the camera sees."""
    query: str = "What do you see?"
    node_id: str = Field(default="", max_length=MAX_ID_LEN)
    force: bool = True


class HandoffRequestPayload(BaseModel):
    """Client asks to move working-memory context to another device class."""
    to_node_type: str = Field(default="desktop", max_length=64)
    history_depth: int = Field(default=20, ge=1, le=500)


# ─────────────────────────────────────────────
# Message Type Registry — Maps type strings to payload models
# ─────────────────────────────────────────────

class NodeAckPayload(BaseModel):
    """Brain acknowledges a node_register (HUP_SPEC §5.2)."""
    node_id: str = Field(default="", max_length=MAX_ID_LEN)
    session_token: str = Field(default="", max_length=MAX_TOKEN_LEN)
    hup_version: str = Field(default=HUP_VERSION, max_length=32)
    heartbeat_ms: int = Field(default=10000, ge=0)
    server_time: float = Field(default_factory=time, ge=0.0)
    capabilities: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    granted_capabilities: list[str] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    denied_capabilities: list[str] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )


class HUPActionRequestPayload(BaseModel):
    """Brain dispatches an action to a daemon (HUP_SPEC §5.5)."""
    action_id: str = Field(
        default_factory=lambda: str(uuid4())[:8], max_length=MAX_ID_LEN
    )
    name: str = Field(default="", max_length=MAX_NAME_LEN)
    params: dict = Field(default_factory=dict)
    # See ExecuteCommandPayload.timeout_ms for why the SDK's le=120_000 is
    # not adopted.
    timeout_ms: int = Field(default=5000, ge=0)
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
    action_id: str = Field(default="", max_length=MAX_ID_LEN)
    request_id: str = Field(default="", max_length=MAX_ID_LEN)
    success: bool = True
    result: dict = Field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = Field(default=0, ge=0)


class NodeHeartbeatPayload(BaseModel):
    """Daemon heartbeat (HUP_SPEC §5.3).

    ``battery_pct`` mirrors the node SDK's own ``ge=0, le=100`` on this exact
    field, so an SDK node can never be rejected by it. ``rssi`` is left bare
    because the SDK leaves it bare too; the bounded radio field on this wire
    is ``DeviceAnnouncePayload.rssi_dbm``.
    """
    ts: float = Field(default_factory=time, ge=0.0)
    battery_pct: Optional[int] = Field(default=None, ge=0, le=100)
    rssi: Optional[int] = None


class NodeByePayload(BaseModel):
    """Graceful disconnect (HUP_SPEC §5.7)."""
    reason: str = Field(default="shutdown", max_length=MAX_NAME_LEN)
    restart_in_s: int = Field(default=0, ge=0)


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

    **Bounds mirror the Python node SDK exactly** (F-02). The SDK at
    ``feral-nodes/python-node-sdk/src/feral_node_sdk/schemas.py`` already
    declared ``device_id`` 1-128, ``width`` / ``height`` 1-8192,
    ``sequence >= 0`` and a decoded 512 KiB cap, while claiming in its
    docstring to "mirror the brain". It did not: the brain accepted
    ``device_id=""``, ``width=-5``, ``height=1000000000``, ``sequence=-42``
    and a 900,000-byte frame. Mirroring is exact and no stricter, so a frame
    the SDK builds can never be refused here;
    ``tests/test_protocol_field_constraints.py`` enforces the equality.
    """
    device_id: str = Field(..., min_length=1, max_length=128)
    timestamp: float = Field(default_factory=time)
    encoding: Literal["jpeg", "png", "webp"] = "jpeg"
    data_b64: str
    width: Optional[int] = Field(default=None, ge=1, le=MAX_PIXELS)
    height: Optional[int] = Field(default=None, ge=1, le=MAX_PIXELS)
    source: str = "glasses"
    sequence: Optional[int] = Field(default=None, ge=0)

    @field_validator("data_b64")
    @classmethod
    def _data_b64_decoded_size(cls, v: str) -> str:
        # Measured on DECODED bytes. api/server.py:3672 measures base64
        # CHARACTERS against the same constant, so its effective ceiling is
        # 384 KiB and the two now disagree for frames between 384 and 512
        # KiB. That is AUDIT-FIXES F-03 and is fixed there, not here.
        return _decoded_size_guard(v, VIDEO_FRAME_MAX_BYTES, "glasses_frame")


class DeviceAnnouncePayload(BaseModel):
    """Peripheral-discovery envelope (HUP_SPEC §5.4.4 — v1.3.0+).

    Closes the "what BLE devices are around my phone?" loop without
    exposing per-vendor BLE APIs. The brain upserts a knowledge-graph
    entity keyed by ``device_id`` with ``category=device`` so device
    queries land via the same memory tool path as everything else.

    **Bounds mirror the Python node SDK exactly** (F-02): ``device_id``
    1-128 and ``rssi_dbm`` in [-127, 20], the physical dBm range a BLE
    scanner can report. The brain previously accepted ``rssi_dbm=-9999``.
    Every other field is left exactly as bare as the SDK leaves it, so
    neither side can reject a frame the other builds.
    """
    scanner_node_id: str = ""
    device_id: str = Field(..., min_length=1, max_length=128)
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
    rssi_dbm: Optional[int] = Field(default=None, ge=-127, le=20)
    advertised_services: list[str] = Field(default_factory=list)
    first_seen: Optional[float] = None
    last_seen: Optional[float] = None
    metadata: dict = Field(default_factory=dict)


class HealthReadingModel(BaseModel):
    """One canonical health reading.

    This is ``BaselineEngine.biometric_samples``' durable row
    ``(ts, source, metric, value)`` plus render metadata that is a pure
    function of ``metric`` (see
    ``integrations/health_canonical.py::CANONICAL_METRICS``). It is not
    a new reading shape: it is the existing durable one, made
    renderable.

    ``source`` is carried verbatim from whatever produced the sample
    (``whoop``, ``oura``, ``jw_health_glasses``). No source id is
    translated on this wire.
    """
    metric: str = Field(..., min_length=1, max_length=64)
    # ``value`` is unbounded: it holds every canonical metric, from a resting
    # heart rate to a step count, so no single range is meaningful.
    value: float
    unit: str = Field(default="", max_length=32)
    label: str = Field(default="", max_length=MAX_NAME_LEN)
    # Decimal places used by the renderer.
    precision: int = Field(default=0, ge=0, le=10)
    category: str = Field(default="vitals", max_length=64)
    source: str = Field(default="", max_length=64)
    ts: float = Field(default_factory=time, ge=0.0)


class HealthSeriesModel(BaseModel):
    """A dated series of one metric, for a chart.

    ``points`` is unbounded: its length is the requested window, which the
    caller chooses.
    """
    metric: str = Field(..., min_length=1, max_length=64)
    label: str = Field(default="", max_length=MAX_NAME_LEN)
    unit: str = Field(default="", max_length=32)
    precision: int = Field(default=0, ge=0, le=10)
    category: str = Field(default="vitals", max_length=64)
    source: str = Field(default="", max_length=64)
    points: list[dict] = Field(default_factory=list)


class HealthUpdateDataModel(BaseModel):
    """``health_update.payload.data``.

    ``readings`` / ``series`` are unbounded: both are brain-assembled answers
    whose length is set by the query window.
    """
    sources: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    window_days: int = Field(default=0, ge=0)
    note: str = ""
    readings: list[HealthReadingModel] = Field(default_factory=list)
    series: list[HealthSeriesModel] = Field(default_factory=list)


class HealthUpdatePayload(BaseModel):
    """Brain → node health frame (HUP v1.3.0).

    Theora's iOS HUP client decoded exactly ``node_ack``,
    ``hup_action_request``, ``error``, ``node_bye``, ``chat_response``,
    ``text_response``, ``transcript``, ``audio_response`` and
    ``voice_status``. There was no health frame at all, so the only way
    Whoop / glasses data reached the app was as English prose inside a
    chat reply, which an app cannot render as a card or a chart.

    The envelope is the exact mirror of the daemon → brain
    ``device_event`` (HUP_SPEC 5.4): ``{node_id, event_type, data, ts}``.
    Same vocabulary, opposite direction. ``event_type`` is
    ``health_summary`` (current values) or ``vitals_trend`` (dated
    series); both carry the same canonical reading shape so one renderer
    handles both.
    """
    node_id: str = Field(default="", max_length=MAX_ID_LEN)
    event_type: Literal["health_summary", "vitals_trend"] = "health_summary"
    ts: float = Field(default_factory=time, ge=0.0)
    data: HealthUpdateDataModel = Field(default_factory=HealthUpdateDataModel)


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
    # Ambient conversation captured by the glasses mic, transcribed on
    # the phone, queued there while the brain is off. NOT "transcript":
    # that key is the brain-to-client TranscriptPayload below.
    "ambient_transcript": AmbientTranscriptPayload,
    # The pull leg of the digest return path. The phone names the
    # transcripts it has synced but holds no summary for; the brain
    # answers one ambient_digest per id. See AmbientDigestRequestPayload.
    "ambient_digest_request": AmbientDigestRequestPayload,

    # Brain → Client
    "transcript": TranscriptPayload,
    # The behavioural policy the agent is currently applying, plus the
    # body state it derived from. Unsolicited: pushed when biometrics
    # move the policy, so a client can render what the agent is doing
    # about the wearer's state instead of inferring it from reply
    # length. See SomaticStatePayload.
    "somatic_state": SomaticStatePayload,
    "ambient_transcript_ack": AmbientTranscriptAckPayload,
    # Brain → Client, both unsolicited on completion and as the reply to
    # ambient_digest_request. One type for both so the phone has one
    # inbound handler. First frame pair in this feature where one goes
    # each way; HUP_SPEC 5.9 notes the direction of each.
    "ambient_digest": AmbientDigestPayload,
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
    "timeline": TimelinePayload,

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

    # Health (brain → node; mirrors device_event conventions)
    "health_update": HealthUpdatePayload,

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
