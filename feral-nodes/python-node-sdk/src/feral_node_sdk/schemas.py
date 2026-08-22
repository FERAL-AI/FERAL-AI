"""Pydantic mirrors of the HUP v1 wire schemas in `HUP_SPEC.md` §5.

These models are the canonical *runtime* validators for the Python SDK; the
Markdown spec is the canonical *normative* source. Keep them in lockstep.
"""

from __future__ import annotations

import time as _time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

HUP_VERSION = "1.3.0"
# Per-frame decoded-size caps from HUP_SPEC.md §5.4.1 / §5.4.2.
AUDIO_FRAME_MAX_BYTES = 64 * 1024
VIDEO_FRAME_MAX_BYTES = 512 * 1024

NodeType = Literal[
    "desktop",
    "server",
    "rpi",
    "robot",
    "glasses",
    "phone",
    "actuator",
    "sensor",
    "wearable",
    "camera",
    "vehicle",
    "appliance",
]


class HUPFrame(BaseModel):
    """Outer envelope shared by every HUP message."""

    hup_version: str = HUP_VERSION
    type: str
    ts: float = Field(default_factory=_time.time)
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeRegisterPayload(BaseModel):
    node_id: str = Field(..., pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    node_type: NodeType = "sensor"
    name: str = ""
    manufacturer: str = ""
    model: str = ""
    firmware_version: str = ""
    platform: str = ""
    os: str = ""
    capabilities: list[str] = Field(default_factory=list)
    sensors: list[str] = Field(default_factory=list)
    actuators: list[str] = Field(default_factory=list)
    location: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("capabilities", "sensors", "actuators", "tags", mode="before")
    @classmethod
    def _to_str_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        return [str(x) for x in v]


class NodeAckPayload(BaseModel):
    node_id: str
    session_token: str
    primary_session_id: str = ""
    heartbeat_ms: int = 10000
    server_time: float = Field(default_factory=_time.time)
    granted_capabilities: list[str] = Field(default_factory=list)
    denied_capabilities: list[str] = Field(default_factory=list)


class TextResponsePayload(BaseModel):
    text: str
    tool_calls: Optional[list[dict[str, Any]]] = None
    model: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    session_id: str = ""
    reply_mode: str = ""
    channel: str = ""
    source: str = ""
    trigger_id: str = ""
    priority: str = ""
    title: str = ""
    voice_text: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class NodeHeartbeatPayload(BaseModel):
    ts: float = Field(default_factory=_time.time)
    battery_pct: Optional[int] = Field(default=None, ge=0, le=100)
    rssi: Optional[int] = None


class DeviceEventPayload(BaseModel):
    node_id: str
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=_time.time)


class AudioFramePayload(BaseModel):
    """HUP v1.1 `audio_frame` payload (per HUP_SPEC.md §5.4.1)."""

    event_type: Literal["audio_frame"] = "audio_frame"
    codec: Literal["opus", "pcm16"]
    sample_rate: int = Field(..., ge=8000, le=96000)
    channels: int = Field(..., ge=1, le=2)
    frame_ms: int = Field(default=20, ge=1, le=120)
    sequence: int = Field(..., ge=0)
    data_b64: str

    @field_validator("data_b64")
    @classmethod
    def _data_b64_decoded_size(cls, v: str) -> str:
        import base64
        try:
            decoded = base64.b64decode(v, validate=False)
        except Exception as exc:
            raise ValueError(f"data_b64 is not valid base64: {exc}") from exc
        if len(decoded) > AUDIO_FRAME_MAX_BYTES:
            raise ValueError(
                f"audio_frame data_b64 decoded to {len(decoded)} bytes; cap is {AUDIO_FRAME_MAX_BYTES}"
            )
        return v


class VideoFramePayload(BaseModel):
    """HUP v1.1 `video_frame` payload (per HUP_SPEC.md §5.4.2)."""

    event_type: Literal["video_frame"] = "video_frame"
    codec: Literal["jpeg", "h264"]
    width: int = Field(..., ge=1, le=8192)
    height: int = Field(..., ge=1, le=8192)
    sequence: int = Field(..., ge=0)
    keyframe: bool = True
    data_b64: str

    @field_validator("data_b64")
    @classmethod
    def _data_b64_decoded_size(cls, v: str) -> str:
        import base64
        try:
            decoded = base64.b64decode(v, validate=False)
        except Exception as exc:
            raise ValueError(f"data_b64 is not valid base64: {exc}") from exc
        if len(decoded) > VIDEO_FRAME_MAX_BYTES:
            raise ValueError(
                f"video_frame data_b64 decoded to {len(decoded)} bytes; cap is {VIDEO_FRAME_MAX_BYTES}"
            )
        return v


class GlassesFramePayload(BaseModel):
    """HUP v1.3.0 ``glasses_frame`` payload (per HUP_SPEC.md §5.4.3).

    The Python SDK validator mirrors the brain's ``GlassesFramePayload``
    in ``feral-core/models/protocol.py``. ``device_id`` and ``data_b64``
    are required; everything else has a wire-friendly default so a
    minimal daemon can emit a frame with just three fields.
    """

    device_id: str = Field(..., min_length=1, max_length=128)
    timestamp: float = Field(default_factory=_time.time)
    encoding: Literal["jpeg", "png", "webp"] = "jpeg"
    data_b64: str
    width: Optional[int] = Field(default=None, ge=1, le=8192)
    height: Optional[int] = Field(default=None, ge=1, le=8192)
    source: str = "glasses"
    sequence: Optional[int] = Field(default=None, ge=0)

    @field_validator("data_b64")
    @classmethod
    def _data_b64_decoded_size(cls, v: str) -> str:
        import base64
        try:
            decoded = base64.b64decode(v, validate=False)
        except Exception as exc:
            raise ValueError(f"data_b64 is not valid base64: {exc}") from exc
        if len(decoded) > VIDEO_FRAME_MAX_BYTES:
            raise ValueError(
                f"glasses_frame data_b64 decoded to {len(decoded)} bytes; "
                f"cap is {VIDEO_FRAME_MAX_BYTES}"
            )
        return v


class DeviceAnnouncePayload(BaseModel):
    """HUP v1.3.0 ``device_announce`` payload (per HUP_SPEC.md §5.4.4)."""

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
    metadata: dict[str, Any] = Field(default_factory=dict)


class HUPActionRequestPayload(BaseModel):
    action_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=5000, ge=1, le=120_000)
    requires_confirmation: bool = False


class HUPActionResponsePayload(BaseModel):
    action_id: str
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = Field(default=0, ge=0)


class NodeByePayload(BaseModel):
    reason: str = "shutdown"
    restart_in_s: int = 0


class ErrorPayload(BaseModel):
    code: int
    name: str
    message: str
    recoverable: bool = True
    ref_action_id: Optional[str] = None


MESSAGE_TYPES: dict[str, type[BaseModel]] = {
    "node_register": NodeRegisterPayload,
    "node_ack": NodeAckPayload,
    "node_heartbeat": NodeHeartbeatPayload,
    "device_event": DeviceEventPayload,
    "hup_action_request": HUPActionRequestPayload,
    "hup_action_response": HUPActionResponsePayload,
    "node_bye": NodeByePayload,
    "text_response": TextResponsePayload,
    "error": ErrorPayload,
    # HUP v1.3.0
    "glasses_frame": GlassesFramePayload,
    "device_announce": DeviceAnnouncePayload,
}


def build_frame(type_: str, payload: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """Serialize a validated payload into a wire-ready HUP envelope."""
    model_cls = MESSAGE_TYPES.get(type_)
    if model_cls is None:
        raise ValueError(f"unknown HUP message type: {type_!r}")
    if isinstance(payload, BaseModel):
        validated = payload
    else:
        validated = model_cls(**payload)
    return {
        "hup_version": HUP_VERSION,
        "type": type_,
        "ts": _time.time(),
        "payload": validated.model_dump(exclude_none=False),
    }
