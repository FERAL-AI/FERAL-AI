"""Tests for the FERAL protocol message parsing."""
import pytest
from models.protocol import (
    FeralMessage,
    TextCommandPayload,
    AudioChunkPayload,
    VisionFramePayload,
    VisionRequestPayload,
    SDUIPayload,
    ExecuteCommandPayload,
    VoiceConfigPayload,
    parse_message,
    MESSAGE_TYPES,
)


def test_feral_message_defaults():
    msg = FeralMessage(type="text_command", payload={"text": "hello"})
    assert msg.msg_id
    assert msg.session_id == ""
    assert msg.hop == "client"
    assert msg.type == "text_command"
    assert msg.timestamp_ms > 0


def test_parse_text_command():
    raw = {"type": "text_command", "hop": "client", "payload": {"text": "search the web"}}
    msg, payload = parse_message(raw)
    assert isinstance(payload, TextCommandPayload)
    assert payload.text == "search the web"


def test_parse_audio_chunk():
    raw = {
        "type": "audio_chunk", "hop": "client",
        "payload": {"encoding": "opus", "sample_rate": 16000, "chunk_index": 0, "is_final": False, "data_b64": "dGVzdA=="},
    }
    msg, payload = parse_message(raw)
    assert isinstance(payload, AudioChunkPayload)
    assert payload.encoding == "opus"
    assert payload.data_b64 == "dGVzdA=="


def test_parse_configured_chained_voice_selection():
    _, payload = parse_message({"type": "voice_config", "hop": "client", "payload": {
        "mode": "chained", "provider": "configured", "supports_realtime": False,
    }})
    assert isinstance(payload, VoiceConfigPayload)
    assert payload.mode == "chained" and payload.provider == "configured"
    assert VoiceConfigPayload().provider == "openai"


@pytest.mark.parametrize("selection", [
    {"mode": "unknown-mode"}, {"mode": "chained", "provider": "x" * 257},
])
def test_invalid_voice_selection_does_not_parse(selection):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        parse_message({"type": "voice_config", "hop": "client", "payload": selection})


def test_parse_vision_frame():
    raw = {
        "type": "vision_frame", "hop": "daemon",
        "payload": {"node_id": "glasses-1", "encoding": "jpeg", "resolution": [640, 480], "data_b64": "abc123"},
    }
    msg, payload = parse_message(raw)
    assert isinstance(payload, VisionFramePayload)
    assert payload.node_id == "glasses-1"
    assert payload.encoding == "jpeg"


def test_parse_vision_request():
    raw = {
        "type": "vision_request", "hop": "brain",
        "payload": {"resolution": "1280x720", "quality": 90, "reason": "user asked"},
    }
    msg, payload = parse_message(raw)
    assert isinstance(payload, VisionRequestPayload)
    assert payload.resolution == "1280x720"
    assert payload.quality == 90


def test_parse_unknown_type():
    raw = {"type": "unknown_future_type", "hop": "client", "payload": {"foo": "bar"}}
    msg, payload = parse_message(raw)
    assert msg.type == "unknown_future_type"
    assert payload is None


def test_sdui_payload_defaults():
    sdui = SDUIPayload(root={"type": "VStack", "children": []})
    assert sdui.screen_id
    assert sdui.ttl_seconds == 300


def test_execute_command_defaults():
    cmd = ExecuteCommandPayload(executor="shell", action="ls -la")
    assert cmd.command_id
    assert cmd.timeout_ms == 5000
    assert cmd.requires_confirmation is False


def test_all_message_types_registered():
    expected = [
        "audio_chunk", "text_command", "biometric", "ui_event", "device_register",
        "transcript", "sdui", "sdui_patch", "tts_chunk", "text_response", "error",
        "node_register", "execute", "execute_result",
        "vision_frame", "vision_request",
    ]
    for t in expected:
        assert t in MESSAGE_TYPES, f"Missing type: {t}"
