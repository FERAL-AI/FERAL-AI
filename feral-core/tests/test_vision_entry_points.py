"""Every route by which an image or a sensor reading enters the brain,
and proof that each one reaches a consumer.

Written for the SEES audit (2026-08-12). The defects these pin were all
of one shape: a producer wrote somewhere, and the reader looked
somewhere else, and the mismatch was invisible because the failure path
was a ``getattr`` default or a ``logger.debug``.

1. ``perception/glasses_buffer.py`` exported no ``get_glasses_buffer``,
   which is the exact symbol ``perception/context_attach.py`` probed for
   with ``getattr(..., lambda: None)``. Every ``glasses_frame`` a pair of
   glasses ever sent was written to ``state.glasses_buffer`` and read by
   nobody. Reproduced on a real brain: the frame landed
   (``device_ids_with_frames() == ['w610-PROBE']``) and the next voice
   turn attached no image.
2. ``device_event`` with ``event_type=uv`` passed the dispatcher filter,
   matched no extractor branch, and was discarded at debug. The same was
   true of ``gps``, ``battery``, ``ambient_light``, ``gyroscope`` and
   ``button_press``, and of the HUP v1.0 image type ``camera_frame``.
3. The ``frame`` envelope (what the shipped iOS bridge sends) never
   resolved a pending ``vision_request``, so "what do you see" against an
   iPhone could only ever time out.

These tests use REAL ``GlassesBuffer`` / ``PerceptionEngine`` /
``VisionBuffer`` objects inside the mocked brain state. A test double
for the object under test would reproduce the original bug (CLAUDE.md
trap 3): every existing vision-attach test injects a fake buffer, which
is why the missing accessor survived.
"""

from __future__ import annotations

import base64
import logging
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.no_auto_feral_home

_TEST_NODE_KEY = "vision-entry-node-key"


def _b64(n: int = 512) -> str:
    return base64.b64encode(b"\xff\xd8\xff" + b"\x00" * n).decode("ascii")


# ──────────────────────────────────────────────────────────────────
# 1. The glasses buffer read path
# ──────────────────────────────────────────────────────────────────


class TestGlassesBufferIsReadable:
    def test_module_exports_the_accessor_context_attach_probes_for(self):
        """The single symbol that made the whole feature dead."""
        from perception import glasses_buffer

        accessor = getattr(glasses_buffer, "get_glasses_buffer", None)
        assert callable(accessor), (
            "perception/context_attach.py resolves the buffer with "
            "getattr(glasses_buffer, 'get_glasses_buffer', lambda: None); "
            "without this export the default always wins and no frame is "
            "ever read"
        )

    def test_accessor_returns_the_registered_instance(self):
        from perception.glasses_buffer import (
            GlassesBuffer,
            get_glasses_buffer,
            set_glasses_buffer,
        )

        previous = get_glasses_buffer()
        try:
            buf = GlassesBuffer()
            set_glasses_buffer(buf)
            assert get_glasses_buffer() is buf
        finally:
            set_glasses_buffer(previous)

    def test_brainstate_registers_its_own_buffer(self):
        """The write path uses ``state.glasses_buffer``; the read path
        must resolve that same object, not a second empty one."""
        from api.state import state
        from perception.glasses_buffer import get_glasses_buffer

        assert get_glasses_buffer() is state.glasses_buffer

    def test_frame_written_by_the_write_path_is_attached_to_a_voice_turn(
        self, monkeypatch,
    ):
        """End to end through the real objects: ingest, then attach."""
        from perception import context_attach
        from perception.glasses_buffer import (
            GlassesBuffer,
            get_glasses_buffer,
            set_glasses_buffer,
        )

        previous = get_glasses_buffer()
        try:
            buf = GlassesBuffer()
            set_glasses_buffer(buf)
            # This is exactly what api.server._handle_glasses_frame calls.
            assert buf.ingest(
                {"device_id": "w610-TEST", "data_b64": _b64(), "encoding": "jpeg"},
                node_id="phone-1",
            ) is not None

            monkeypatch.setattr(
                context_attach, "load_settings", lambda: {"vision": {"enabled": True}}
            )
            out = context_attach.attach_vision_context(
                "what am I looking at?",
                context={"channel": "voice"},
                session_id="sess-1234",
            )
            assert isinstance(out, list), "no image block was attached"
            assert any(b.get("type") == "image_url" for b in out)
        finally:
            set_glasses_buffer(previous)

    def test_missing_accessor_is_reported_not_swallowed(self, monkeypatch, caplog):
        """If the export is ever removed again, the log says so."""
        from perception import context_attach, glasses_buffer

        monkeypatch.delattr(glasses_buffer, "get_glasses_buffer", raising=True)
        monkeypatch.setattr(context_attach, "_MISSING_ACCESSOR_WARNED", False)
        with caplog.at_level(logging.WARNING, logger="feral.perception.context_attach"):
            assert context_attach._get_glasses_buffer() is None
        assert any(
            "get_glasses_buffer" in r.message for r in caplog.records
        ), "a buffer that cannot be resolved must not fail silently"


# ──────────────────────────────────────────────────────────────────
# 2. device_event: every event_type against the extractor
# ──────────────────────────────────────────────────────────────────


# (event_type, payload, assertion on the real PerceptionFrame)
_SENSOR_CASES = [
    ("heart_rate", {"bpm": 72}, lambda f: f.heart_rate == 72),
    ("spo2", {"current": 97}, lambda f: f.spo2_pct == 97),
    ("skin_temperature", {"celsius": 33.4}, lambda f: f.skin_temperature_c == 33.4),
    ("temperature", {"celsius": 21.0}, lambda f: f.ambient_temperature_c == 21.0),
    ("steps", {"count": 4210}, lambda f: f.steps == 4210),
    ("uv", {"index": 6}, lambda f: f.uv_index == 6),
    ("accelerometer", {"x": 0.1, "y": 0.2, "z": 9.8}, lambda f: f.accel_xyz == [0.1, 0.2, 9.8]),
    ("gyroscope", {"x": 0.1, "y": 0.2, "z": 0.3}, lambda f: f.gyro_xyz == [0.1, 0.2, 0.3]),
    ("ambient_light", {"lux": 412}, lambda f: f.ambient_light_lux == 412),
    ("battery", {"percent": 88}, lambda f: f.battery_pct == 88),
    ("gps", {"lat": 37.7, "lon": -122.4}, lambda f: (f.location or {}).get("lat") == 37.7),
    ("gesture", {"gesture": "nod"}, lambda f: f.gesture == "nod"),
    ("button_press", {"button": "side", "pressed": True},
     lambda f: f.gesture == "button_side"),
]


@pytest.fixture
def biometric_state(monkeypatch):
    """A brain state with a REAL PerceptionEngine bound to one session."""
    import api.server as server
    from perception.fusion import PerceptionEngine

    st = MagicMock()
    st.perception = PerceptionEngine()
    st.get_sessions_for_daemon = MagicMock(return_value={"sess-1"})
    st.somatic_engine = None
    st.baseline_engine = None
    monkeypatch.setattr(server, "state", st)
    return st


@pytest.mark.parametrize("event_type,payload,check", _SENSOR_CASES,
                         ids=[c[0] for c in _SENSOR_CASES])
def test_device_event_reaches_the_perception_frame(
    biometric_state, event_type, payload, check,
):
    """Each event_type a HUP node can emit must land in the frame the
    LLM is shown. ``uv`` failed this for the life of the feature."""
    import api.server as server

    server._handle_biometric_device_event("glasses-1", event_type, dict(payload))
    frame = biometric_state.perception.get_frame("sess-1")
    assert check(frame), f"{event_type} did not reach PerceptionFrame: {frame}"


def test_dispatcher_filter_and_extractor_agree():
    """Guard against the ``uv`` shape returning: a type the dispatcher
    admits but the extractor cannot read is dropped silently."""
    import api.server as server

    admitted = {c[0] for c in _SENSOR_CASES}
    assert admitted <= set(server._EXTRACTABLE_EVENT_TYPES)


def test_unextractable_payload_warns_with_the_supported_list(
    biometric_state, caplog,
):
    import api.server as server

    with caplog.at_level(logging.WARNING, logger="feral.brain"):
        server._handle_biometric_device_event("glasses-1", "uv", {"nonsense": 1})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("Dropping device_event" in m for m in msgs), (
        "a reading the brain accepted and then discarded must be visible "
        "at warning; debug is off in every normal deployment"
    )
    assert any("Extractable types" in m for m in msgs)


def test_uv_index_and_steps_reach_the_llm_context():
    """A value on the frame that ``to_system_context`` omits is still
    invisible to the model."""
    from perception.fusion import PerceptionFrame

    frame = PerceptionFrame(uv_index=7, steps=8100, ambient_temperature_c=22.5)
    ctx = frame.to_system_context()
    assert "UV=7" in ctx
    assert "Steps=8100" in ctx
    assert "Ambient=22.5" in ctx


# ──────────────────────────────────────────────────────────────────
# 3. Image envelopes over a real /v1/node socket
# ──────────────────────────────────────────────────────────────────


def _make_ws_state() -> MagicMock:
    from collections import deque

    from api.state import VisionBuffer
    from perception.glasses_buffer import GlassesBuffer

    s = MagicMock()
    s.sessions = {}
    s.daemons = {}
    s.devices = {}
    s.init = AsyncMock(return_value=None)
    s.cron_service = None
    s.activity_log = deque()
    s.memory = MagicMock()
    s.memory.start_background_tasks = MagicMock()
    s.bind_session_to_daemon = MagicMock()
    s.get_sessions_for_daemon = MagicMock(return_value=set())
    s.perception = MagicMock()
    s.orchestrator = MagicMock()
    s.orchestrator.resolve_pending_frame = MagicMock()
    s.orchestrator.handle_command = AsyncMock()
    s.orchestrator.handle_daemon_result = AsyncMock()
    # Real buffers: the point of the test is where pixels land.
    s.vision_buffer = VisionBuffer()
    s.glasses_buffer = GlassesBuffer()
    s.change_detector = MagicMock()
    s.change_detector.should_analyze = MagicMock(return_value=None)
    s.scene = MagicMock(available=False)
    s.voice_router = None
    s.skill_executor = MagicMock()
    s.hardware_mesh = MagicMock()
    s.hardware_mesh.on_node_connected = AsyncMock()
    s.hardware_mesh.on_node_disconnected = MagicMock()
    s.capability_registry = MagicMock()
    s.supervisor = None
    s.somatic_engine = None
    s.baseline_engine = None
    return s


@contextmanager
def _node_client(mock: MagicMock):
    pairing_store = MagicMock()
    pairing_store.verify_device = MagicMock(return_value=None)
    mock.device_pairing_store = pairing_store
    from tests.test_server_websocket import _reimport_api_server_before_patching

    _reimport_api_server_before_patching()
    with ExitStack() as stack:
        stack.enter_context(patch("api.state.state", mock))
        from api.server import app

        stack.enter_context(patch("api.server.state", mock))
        stack.enter_context(patch("api.server.NODE_API_KEY", _TEST_NODE_KEY))
        stack.enter_context(patch("api.server._build_greeting", MagicMock(return_value="hi")))
        yield TestClient(app, raise_server_exceptions=False)


def _settle(ws) -> None:
    """Let the server drain the frames we just queued.

    The daemon socket answers only some envelopes, so a receive_json()
    round-trip is not a reliable barrier. ``node_bye`` is answered by a
    close, which IS observable, and it is the protocol's own "I am done"
    signal (HUP §5.3).
    """
    import time

    ws.send_json({"type": "node_bye", "payload": {}})
    time.sleep(0.4)


def _register(ws, node_id="glasses-1"):
    ws.send_json({
        "type": "node_register",
        "payload": {
            "node_id": node_id, "node_type": "glasses",
            "platform": "linux", "capabilities": ["camera"],
        },
    })
    return ws.receive_json()


class TestImageEnvelopes:
    def test_glasses_frame_lands_in_the_glasses_buffer(self):
        st = _make_ws_state()
        with _node_client(st) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register(ws)
                ws.send_json({
                    "type": "glasses_frame",
                    "payload": {
                        "device_id": "w610-1", "data_b64": _b64(),
                        "encoding": "jpeg", "source": "w610",
                    },
                })
                _settle(ws)
        assert st.glasses_buffer.device_ids_with_frames() == ["w610-1"]

    def test_camera_frame_device_event_is_not_dropped(self):
        """HUP v1.0's image type. It had no branch at all, so a v1.0
        daemon's every frame was discarded by the unknown-event path."""
        st = _make_ws_state()
        with _node_client(st) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register(ws)
                ws.send_json({
                    "type": "device_event",
                    "payload": {
                        "node_id": "glasses-1", "event_type": "camera_frame",
                        "data": {
                            "encoding": "jpeg", "resolution": [640, 480],
                            "data_b64": _b64(),
                        },
                    },
                })
                _settle(ws)
        assert st.vision_buffer.node_ids_with_frames() == ["glasses-1"]

    def test_frame_envelope_resolves_a_pending_vision_request(self):
        """``request_frame`` waits on a future that only
        ``resolve_pending_frame`` completes. The ``frame`` branch never
        called it, so perception_query against the shipped iOS bridge
        could only ever answer 504."""
        st = _make_ws_state()
        with _node_client(st) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register(ws)
                ws.send_json({
                    "type": "frame",
                    "msg_id": "req-42",
                    "payload": {"node_id": "glasses-1", "image_b64": _b64()},
                })
                _settle(ws)
        st.orchestrator.resolve_pending_frame.assert_called_once()
        assert st.orchestrator.resolve_pending_frame.call_args[0][0] == "req-42"

    def test_unknown_event_type_is_reported_once(self, caplog):
        import api.server  # noqa: F401 - ensure the module is importable first

        st = _make_ws_state()
        with caplog.at_level(logging.WARNING, logger="feral.brain"):
            with _node_client(st) as client:
                with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                    _register(ws)
                    for _ in range(3):
                        ws.send_json({
                            "type": "device_event",
                            "payload": {
                                "node_id": "glasses-1",
                                "event_type": "something_nobody_handles",
                                "data": {"v": 1},
                            },
                        })
                    _settle(ws)
        hits = [
            r for r in caplog.records
            if "something_nobody_handles" in r.getMessage()
        ]
        assert len(hits) == 1, (
            "an unhandled sensor must be reported exactly once per "
            "node+type: silent at debug is how camera_frame and gps hid, "
            "per-frame at warning would flood the log"
        )
