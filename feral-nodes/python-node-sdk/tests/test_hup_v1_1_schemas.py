"""HUP v1.1 schema contract — audio_frame + video_frame validation.

These tests lock the wire shape declared in HUP_SPEC.md §5.4.1 and
§5.4.2. If the Markdown spec and these models drift, the systematic
sync is broken.
"""

from __future__ import annotations

import base64

import pytest

from feral_node_sdk.schemas import (
    AUDIO_FRAME_MAX_BYTES,
    VIDEO_FRAME_MAX_BYTES,
    AudioFramePayload,
    HUP_VERSION,
    VideoFramePayload,
    build_frame,
    DeviceEventPayload,
)


def _b64_of(size: int) -> str:
    return base64.b64encode(b"\x00" * size).decode("ascii")


def test_hup_version_is_a_semantic_version():
    # Not a literal. A pinned number here asserts nothing about behaviour
    # and fails on the next protocol bump, which is what happened at 1.4.0:
    # this file claimed the SDK was broken for weeks, unseen, because
    # feral-nodes has no CI job.
    parts = HUP_VERSION.split(".")
    assert len(parts) == 3, f"HUP_VERSION must be MAJOR.MINOR.PATCH, got {HUP_VERSION!r}"
    assert all(p.isdigit() for p in parts), f"non-numeric component in {HUP_VERSION!r}"


def test_audio_frame_valid_opus():
    p = AudioFramePayload(
        codec="opus",
        sample_rate=24000,
        channels=1,
        frame_ms=20,
        sequence=42,
        data_b64=_b64_of(1024),
    )
    assert p.event_type == "audio_frame"
    assert p.codec == "opus"


def test_audio_frame_rejects_bad_codec():
    with pytest.raises(ValueError):
        AudioFramePayload(
            codec="mp3",
            sample_rate=24000,
            channels=1,
            sequence=0,
            data_b64=_b64_of(128),
        )


def test_audio_frame_rejects_oversize_payload():
    with pytest.raises(ValueError, match="cap is"):
        AudioFramePayload(
            codec="opus",
            sample_rate=24000,
            channels=1,
            sequence=0,
            data_b64=_b64_of(AUDIO_FRAME_MAX_BYTES + 1),
        )


def test_video_frame_valid_jpeg():
    p = VideoFramePayload(
        codec="jpeg",
        width=1280,
        height=720,
        sequence=7,
        keyframe=True,
        data_b64=_b64_of(50_000),
    )
    assert p.codec == "jpeg"
    assert p.keyframe is True


def test_video_frame_rejects_oversize_payload():
    with pytest.raises(ValueError, match="cap is"):
        VideoFramePayload(
            codec="jpeg",
            width=1280,
            height=720,
            sequence=0,
            data_b64=_b64_of(VIDEO_FRAME_MAX_BYTES + 1),
        )


def test_build_frame_wraps_audio_payload_into_device_event():
    """An audio_frame rides inside the device_event envelope."""
    payload = DeviceEventPayload(
        node_id="feral-w300-0001",
        event_type="audio_frame",
        data={
            "codec": "opus",
            "sample_rate": 24000,
            "channels": 1,
            "sequence": 0,
            "data_b64": _b64_of(128),
        },
    )
    frame = build_frame("device_event", payload)
    assert frame["hup_version"] == HUP_VERSION
    assert frame["type"] == "device_event"
    assert frame["payload"]["event_type"] == "audio_frame"


def test_feral_node_emit_helpers_validate_locally():
    """The new FeralNode helpers validate before hitting the websocket.

    An oversized frame must raise a pydantic ValidationError locally,
    not silently ship a banned frame.
    """
    from pydantic import ValidationError

    from feral_node_sdk import FeralNode

    node = FeralNode(
        node_id="feral-w300-test",
        name="W300 Test",
        manufacturer="Theora",
        firmware_version="0.0.1",
        node_type="glasses",
        capabilities=["camera", "microphone"],
    )

    import asyncio
    with pytest.raises(ValidationError):
        asyncio.run(
            node.emit_video_frame(
                _b64_of(VIDEO_FRAME_MAX_BYTES + 1),
                codec="jpeg",
                width=640,
                height=480,
                sequence=0,
            )
        )
