"""A voice session that never opened must not be acked as opened.

``VoiceRouter.open_session`` reports every failure by returning
``None``: an unavailable OpenAI Realtime proxy, an unavailable Gemini
proxy, an unrecognised mode, and a chained pipeline whose STT or TTS
provider could not be constructed all take that path. Only a genuine
crash raises.

``api/server.py``'s ``voice_session_start`` handler wrapped the call in
``except Exception`` and did nothing else, so the ``None`` fell straight
through to ``_record_phone_envelope("allowed", ...)``. Measured before
the fix, with ``open_session`` returning ``None``:

  * the audit row said ``decision="allowed"`` for a session that does
    not exist,
  * no ``error`` frame reached the node,
  * no ``voice_status`` frame reached the node,

so the phone had nothing to react to and its orb stayed on "listening"
for the rest of the connection.

These tests pin both halves of the fix: the brain records the failure
truthfully, and the node is told.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_hup_protocol import (
    _TEST_NODE_KEY,
    _make_mock_state,
    _node_client,
    _register_node,
)

pytestmark = pytest.mark.no_auto_feral_home


def _mock_state_with_supervisor() -> MagicMock:
    mock = _make_mock_state()
    mock.supervisor = MagicMock()
    mock.supervisor.record = MagicMock()
    return mock


def _phone_rows(mock_state: MagicMock, message_type: str) -> list[dict]:
    rows = []
    for c in mock_state.supervisor.record.call_args_list:
        if not c.kwargs or c.kwargs.get("kind") != "phone_envelope":
            continue
        detail = c.kwargs.get("detail", {}) or {}
        if detail.get("message_type") != message_type:
            continue
        rows.append({"decision": c.kwargs.get("decision"), "detail": detail})
    return rows


def _start_voice(mock, *, node_id: str = "phone-voice-fail") -> list[dict]:
    """Drive one voice_session_start and return the frames the node saw."""
    frames: list[dict] = []
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id=node_id, node_type="phone")
            ws.send_json(
                {
                    "type": "voice_session_start",
                    "hup_version": "1.3.0",
                    "ts": 1734369923.0,
                    "payload": {
                        "stream_id": "voice-stream-fail",
                        "sample_rate": 16000,
                        "channels": 1,
                        "language_hint": "en-US",
                        "mode": "push_to_talk",
                        "interrupt_policy": "barge_in",
                    },
                }
            )
            # A sentinel round-trip: whatever the handler emitted for the
            # start arrives before the error frame for this bogus type.
            ws.send_json({"type": "totally_bogus_type", "payload": {}})
            while True:
                frame = ws.receive_json()
                if (
                    frame.get("type") == "error"
                    and frame.get("payload", {}).get("code") == 1002
                ):
                    break
                frames.append(frame)
    return frames


def test_open_session_returning_none_is_not_recorded_as_allowed():
    mock = _mock_state_with_supervisor()
    mock.voice_router = MagicMock()
    mock.voice_router.open_session = AsyncMock(return_value=None)

    _start_voice(mock)

    rows = _phone_rows(mock, "voice_session_start")
    assert rows, "voice_session_start produced no audit row at all"
    decisions = {r["decision"] for r in rows}
    assert "allowed" not in decisions, (
        "the brain acked a voice session that open_session refused to open; "
        f"rows={rows}"
    )
    assert decisions == {"error"}
    assert rows[-1]["detail"].get("reason") == "open_session_failed"


def test_open_session_returning_none_tells_the_node():
    mock = _mock_state_with_supervisor()
    mock.voice_router = MagicMock()
    mock.voice_router.open_session = AsyncMock(return_value=None)

    frames = _start_voice(mock)

    errors = [f for f in frames if f.get("type") == "error"]
    assert errors, (
        "no error frame reached the node, so the phone had nothing to "
        f"leave the listening state on; frames={frames}"
    )
    assert errors[0]["payload"]["name"] == "voice_session_failed"
    assert "voice-stream-fail" in errors[0]["payload"]["message"]


def test_open_session_raising_also_tells_the_node():
    """The crash path recorded an ``error`` row but told the node nothing."""
    mock = _mock_state_with_supervisor()
    mock.voice_router = MagicMock()
    mock.voice_router.open_session = AsyncMock(
        side_effect=RuntimeError("realtime handshake exploded"),
    )

    frames = _start_voice(mock, node_id="phone-voice-boom")

    rows = _phone_rows(mock, "voice_session_start")
    assert {r["decision"] for r in rows} == {"error"}

    errors = [f for f in frames if f.get("type") == "error"]
    assert errors, f"a crashed open told the node nothing; frames={frames}"
    assert errors[0]["payload"]["name"] == "voice_session_failed"
    assert "realtime handshake exploded" in errors[0]["payload"]["message"]


def test_successful_open_is_still_allowed():
    """The success path must keep its ``allowed`` row and stay quiet."""
    mock = _mock_state_with_supervisor()
    mock.voice_router = MagicMock()
    mock.voice_router.open_session = AsyncMock(return_value=MagicMock())

    frames = _start_voice(mock, node_id="phone-voice-ok")

    rows = _phone_rows(mock, "voice_session_start")
    assert [r["decision"] for r in rows] == ["allowed"]
    assert not [f for f in frames if f.get("type") == "error"]
