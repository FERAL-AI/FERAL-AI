"""AUDIT-FIXES F-03: frame size caps measure DECODED bytes, not base64 characters.

The caps are documented in decoded bytes: HUP_SPEC.md section 5.4.1 (64 KiB
audio), 5.4.2 / 5.4.3 (512 KiB video / glasses), and the log lines say "B".
Every check measured ``len(data_b64)`` instead, and base64 inflates 4/3, so a
512 KiB cap admitted only 384 KiB of image. A legal 400 KiB JPEG passed both
SDK validators, reported a successful send, and was dropped by the brain with
a log-only warning.

Two defects, both covered here:

1. **Measurement.** A 400 KiB decoded frame must be accepted; only a frame
   whose DECODED size exceeds the cap may be rejected.
2. **Silence.** The rejection path names "HUP error 4020" in its log message
   and docstring, but 4020 was never sent to anyone: the string appears only
   in logs. The daemon believed the frame landed. An over-cap frame must now
   produce an HUP section 8 error frame with code 4020 / ``frame_too_large``.

The six sites, all in ``api/server.py``:

    :1823  client_session  vision_frame   (webclient), measurement only, see below
    :2304  daemon_session  vision_frame
    :3283  daemon_session  frame
    :3598  _handle_video_frame
    :3636  _handle_audio_frame
    :3672  _handle_glasses_frame

``client_session`` speaks the ``FeralMessage`` error shape, not HUP error
codes, so the webclient site is fixed for measurement only. See the F-03
record in AUDIT-FIXES.md.
"""

from __future__ import annotations

import ast
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.protocol import VIDEO_FRAME_MAX_BYTES as CANONICAL_VIDEO_CAP
from tests.test_hup_protocol import (  # reuse, do not duplicate
    _TEST_NODE_KEY,
    _make_mock_state,
    _node_client,
    _register_node,
)
from tests.test_server_websocket import ws_client, ws_mock_state  # noqa: F401 (pytest fixtures)

pytestmark = pytest.mark.no_auto_feral_home

#: The audit's number. 400 KiB of JPEG is legal under a 512 KiB decoded cap
#: but is ~533 KiB of base64 characters, so the old check dropped it.
FOUR_HUNDRED_KIB = 400 * 1024


def _b64(n: int) -> str:
    """Base64 of ``n`` zero bytes. Decodes to exactly ``n`` bytes."""
    return base64.b64encode(b"\x00" * n).decode("ascii")


# ─────────────────────────────────────────────
# The constant has one home (interaction with F-02)
# ─────────────────────────────────────────────


def test_server_imports_the_canonical_video_cap_instead_of_redeclaring_it():
    """``models/protocol.py`` is canonical (CLAUDE.md).

    F-02 put the decoded cap on ``GlassesFramePayload`` using
    ``models.protocol.VIDEO_FRAME_MAX_BYTES``. ``api/server.py`` kept its own
    literal with the same value, which is how the model and the handler came
    to measure different quantities against the same number. One home only.
    """
    from api import server as srv

    assert srv.VIDEO_FRAME_MAX_BYTES == CANONICAL_VIDEO_CAP

    source = Path(srv.__file__).read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Name)
                    and target.id == "VIDEO_FRAME_MAX_BYTES"
                ), "api/server.py redeclares VIDEO_FRAME_MAX_BYTES; import it from models.protocol"

    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "models.protocol"
        and any(a.name == "VIDEO_FRAME_MAX_BYTES" for a in node.names)
        for node in ast.walk(tree)
    )
    assert imported, "api/server.py must import VIDEO_FRAME_MAX_BYTES from models.protocol"


# ─────────────────────────────────────────────
# Handlers: measurement + a returned rejection reason
# ─────────────────────────────────────────────


@pytest.fixture
def srv_with_mock_state(monkeypatch):
    from api import server as srv

    fake_state = MagicMock()
    fake_state.orchestrator = None
    fake_state.scene = MagicMock(available=False)
    fake_state.change_detector.should_analyze.return_value = None
    fake_state.get_sessions_for_daemon.return_value = []
    # A plain MagicMock attribute is not awaitable, and audio_frame now
    # awaits its consumer.
    fake_state.voice_router.handle_audio_from_node = AsyncMock()
    monkeypatch.setattr(srv, "state", fake_state)
    return srv, fake_state


def test_video_frame_of_400_kib_decoded_is_accepted(srv_with_mock_state):
    """533 KiB of base64, 400 KiB decoded. Dropped before F-03, legal after."""
    srv, st = srv_with_mock_state
    reason = srv._handle_video_frame(
        "glasses-1", {"codec": "jpeg", "data_b64": _b64(FOUR_HUNDRED_KIB)}
    )
    assert reason is None
    st.vision_buffer.push.assert_called_once()


def test_video_frame_over_the_decoded_cap_returns_a_reason(srv_with_mock_state):
    srv, st = srv_with_mock_state
    reason = srv._handle_video_frame(
        "glasses-1", {"codec": "jpeg", "data_b64": _b64(CANONICAL_VIDEO_CAP + 1024)}
    )
    st.vision_buffer.push.assert_not_called()
    assert reason and "video_frame" in reason
    # The reason is what the daemon reads. It must carry the decoded size,
    # not the base64 character count, or it is telling the operator to fix
    # the wrong number.
    assert str(CANONICAL_VIDEO_CAP + 1024) in reason


async def test_audio_frame_of_60_kib_decoded_is_accepted(srv_with_mock_state):
    """80 KiB of base64, 60 KiB decoded, against the 64 KiB decoded cap.

    Asserts against ``voice_router.handle_audio_from_node``: the old
    ``state.audio.ingest_frame`` sink was a method AudioPipeline has
    never defined, so on a MagicMock state it recorded a call that could
    not happen against the real object.
    """
    srv, st = srv_with_mock_state
    st.get_sessions_for_daemon.return_value = ["sid-1"]
    reason = await srv._handle_audio_frame("band-1", {"data_b64": _b64(60 * 1024)})
    assert reason is None
    st.voice_router.handle_audio_from_node.assert_awaited_once()


async def test_audio_frame_over_the_decoded_cap_returns_a_reason(srv_with_mock_state):
    srv, st = srv_with_mock_state
    st.get_sessions_for_daemon.return_value = ["sid-1"]
    reason = await srv._handle_audio_frame(
        "band-1", {"data_b64": _b64(srv.AUDIO_FRAME_MAX_BYTES + 512)}
    )
    st.voice_router.handle_audio_from_node.assert_not_awaited()
    assert reason and "audio_frame" in reason
    assert str(srv.AUDIO_FRAME_MAX_BYTES + 512) in reason


def test_glasses_frame_of_400_kib_decoded_is_accepted(srv_with_mock_state):
    srv, st = srv_with_mock_state
    reason = srv._handle_glasses_frame(
        "phone-1", {"device_id": "w610-D344", "data_b64": _b64(FOUR_HUNDRED_KIB)}
    )
    assert reason is None
    st.glasses_buffer.ingest.assert_called_once()


def test_glasses_frame_over_the_decoded_cap_returns_a_reason(srv_with_mock_state):
    srv, st = srv_with_mock_state
    reason = srv._handle_glasses_frame(
        "phone-1",
        {"device_id": "w610-D344", "data_b64": _b64(CANONICAL_VIDEO_CAP + 1024)},
    )
    st.glasses_buffer.ingest.assert_not_called()
    assert reason and "glasses_frame" in reason


def test_a_frame_exactly_at_the_cap_is_accepted(srv_with_mock_state):
    """Only strictly-over is rejected, matching the model layer's boundary."""
    srv, st = srv_with_mock_state
    reason = srv._handle_video_frame(
        "glasses-1", {"data_b64": _b64(CANONICAL_VIDEO_CAP)}
    )
    assert reason is None
    st.vision_buffer.push.assert_called_once()


# ─────────────────────────────────────────────
# The daemon socket actually receives 4020
# ─────────────────────────────────────────────


def _first_frame_after(ws, sent: dict) -> dict:
    """Send ``sent``, then a message guaranteed to answer, return frame one.

    The trailing unknown-type message exists so this cannot hang against an
    unfixed tree: before F-03 an over-cap frame produced nothing at all, so
    a bare ``receive_json()`` would block until the test timed out. With the
    probe, the unfixed tree answers with the 1002 unknown-type error and the
    assertion fails on content rather than on a hang.
    """
    ws.send_json(sent)
    ws.send_json({"type": "definitely-not-a-hup-type", "payload": {}})
    return ws.receive_json()


def test_oversized_video_frame_gets_a_4020_error_frame():
    mock = _make_mock_state()
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="cam-node")
            frame = _first_frame_after(ws, {
                "type": "video_frame",
                "payload": {
                    "codec": "jpeg",
                    "data_b64": _b64(CANONICAL_VIDEO_CAP + 1024),
                },
            })

    assert frame["type"] == "error"
    assert frame["payload"]["code"] == 4020
    assert frame["payload"]["name"] == "frame_too_large"


def test_oversized_audio_frame_gets_a_4020_error_frame():
    mock = _make_mock_state()
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="band-node")
            frame = _first_frame_after(ws, {
                "type": "audio_frame",
                "payload": {"codec": "opus", "data_b64": _b64(128 * 1024)},
            })

    assert frame["type"] == "error"
    assert frame["payload"]["code"] == 4020


def test_oversized_legacy_vision_frame_gets_a_4020_error_frame():
    """server.py:2304, the ``vision_frame`` branch of the daemon socket."""
    mock = _make_mock_state()
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="cam-node")
            frame = _first_frame_after(ws, {
                "type": "vision_frame",
                "payload": {
                    "node_id": "cam-node",
                    "data_b64": _b64(CANONICAL_VIDEO_CAP + 1024),
                },
            })

    assert frame["type"] == "error"
    assert frame["payload"]["code"] == 4020


def test_oversized_legacy_frame_gets_a_4020_error_frame():
    """server.py:3283, the bare ``frame`` branch of the daemon socket."""
    mock = _make_mock_state()
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="cam-node")
            frame = _first_frame_after(ws, {
                "type": "frame",
                "payload": {"image_b64": _b64(CANONICAL_VIDEO_CAP + 1024)},
            })

    assert frame["type"] == "error"
    assert frame["payload"]["code"] == 4020


def test_a_400_kib_video_frame_is_buffered_and_draws_no_error():
    """The behaviour change, stated: this frame used to be dropped."""
    mock = _make_mock_state()
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="cam-node")
            frame = _first_frame_after(ws, {
                "type": "video_frame",
                "payload": {"codec": "jpeg", "data_b64": _b64(FOUR_HUNDRED_KIB)},
            })

    # First frame back is the unknown-type probe, not a 4020.
    assert frame["payload"]["code"] == 1002
    mock.vision_buffer.push.assert_called_once()


def test_a_400_kib_vision_frame_from_the_webclient_is_buffered(ws_mock_state, ws_client):  # noqa: F811
    """server.py:1823, measurement only.

    ``client_session`` has no HUP error-code channel: it speaks
    ``FeralMessage(type="error", payload={"text": ...})``, which the web
    client renders as a chat notice and a toast. Emitting one per over-cap
    frame would fire at camera frame rate, so this site is fixed for
    measurement and left silent. Recorded in AUDIT-FIXES.md under F-03.
    """
    with ws_client.websocket_connect("/v1/session") as ws:
        ws.receive_json()  # greeting
        ws.send_json({
            "type": "vision_frame",
            "payload": {
                "node_id": "webcam",
                "data_b64": _b64(FOUR_HUNDRED_KIB),
            },
        })

    ws_mock_state.vision_buffer.push.assert_called_once()
