"""Brain-side HUP v1.1 dispatch — audio_frame, video_frame, and
biometric device_event routing.

Asserts the branches in the ``/v1/node`` WebSocket handler exist and
route correctly:

* ``video_frame`` (flat + nested-``data``) → ``state.vision_buffer``.
* ``audio_frame`` (flat + nested-``data``) →
  ``state.voice_router.handle_audio_from_node``. It used to assert
  ``state.audio.ingest_frame``, a method ``AudioPipeline`` has never
  defined; the ``FakeAudio`` double below defined it, so this file
  proved a call that could not happen in production. See
  ``tests/test_audio_frame_reaches_transcription.py``.
* ``device_event(event_type=heart_rate|spo2|skin_temperature|steps|
  accelerometer|gesture)`` → ``state.perception.update_sensors`` +
  baseline recording.

The nested-``data`` shape matters: the Python SDK's
``emit_video_frame`` / ``emit_audio_frame`` serialise frame fields
inside ``DeviceEventPayload.data`` (so the wire carries
``payload.data.data_b64``), not flat. A handler that only reads the
top level silently drops every frame — exactly the bug the
``_unwrap_hup_frame`` helper fixes.

Per HUP_SPEC.md §1's forward-compat rule, unknown device_event
event_types must NOT raise.
"""

from __future__ import annotations

import base64
import importlib
import time

import pytest


pytestmark = pytest.mark.no_auto_feral_home


def _b64(size: int) -> str:
    return base64.b64encode(b"\x00" * size).decode("ascii")


@pytest.fixture()
def server_module(monkeypatch):
    """Import the live server module with a stub state.

    The handlers we exercise are module-level helpers, so we patch
    ``state`` after import and call them directly.
    """
    server = importlib.import_module("api.server")

    pushed = []
    ingested = []
    sensor_updates: list[tuple[str, dict]] = []
    baseline_records: list[tuple[str, float, str]] = []
    sessions: list[str] = ["sid-1"]

    class FakeVisionBuffer:
        # Mirrors VisionBuffer.push(node_id, frame). See F-01.
        def push(self, node_id, frame):
            pushed.append((node_id, frame))

    class FakePerception:
        def update_vision(self, *_a, **_k):
            pass

        def update_sensors(self, sid, sensors):
            sensor_updates.append((sid, sensors))

        def update_gesture(self, sid, gesture):
            sensor_updates.append((sid, {"gesture": gesture}))

    class FakeChangeDetector:
        def should_analyze(self, *_a, **_k):
            return None

    class FakeVoiceRouter:
        # Mirrors VoiceRouter.handle_audio_from_node, the real consumer.
        async def handle_audio_from_node(self, **kwargs):
            ingested.append((kwargs["node_id"], kwargs))

    class FakeBaseline:
        def record(self, metric_id, value, category=None):
            baseline_records.append((metric_id, value, category))

    class FakeState:
        vision_buffer = FakeVisionBuffer()
        perception = FakePerception()
        change_detector = FakeChangeDetector()
        audio = object()
        voice_router = FakeVoiceRouter()
        scene = None
        orchestrator = None
        somatic_engine = None
        baseline_engine = FakeBaseline()

        def get_sessions_for_daemon(self, _node):
            return sessions

    monkeypatch.setattr(server, "state", FakeState())
    return server, pushed, ingested, sensor_updates, baseline_records


def test_video_frame_lands_in_vision_buffer(server_module):
    server, pushed, *_ = server_module
    payload = {
        "event_type": "video_frame",
        "codec": "jpeg",
        "width": 640,
        "height": 480,
        "sequence": 1,
        "data_b64": _b64(2048),
    }
    server._handle_video_frame("feral-glasses-test", payload, msg_id="m1")
    assert len(pushed) == 1
    node_id, frame = pushed[0]
    assert node_id == "feral-glasses-test"
    assert frame["codec"] == "jpeg"


def test_video_frame_nested_payload_unwraps(server_module):
    """HUP v1.1 Python SDK wraps frame fields inside `payload.data`.

    A handler that only reads top-level would silently drop every
    SDK-shipped frame. This test exercises the nested shape to keep
    the unwrap helper honest.
    """
    server, pushed, *_ = server_module
    payload = {
        "node_id": "feral-glasses-test",
        "event_type": "video_frame",
        "data": {
            "codec": "jpeg",
            "width": 640,
            "height": 480,
            "sequence": 1,
            "data_b64": _b64(2048),
        },
    }
    server._handle_video_frame(None, payload, msg_id="m-nested")
    assert len(pushed) == 1
    node_id, frame = pushed[0]
    assert node_id == "feral-glasses-test"
    assert frame["codec"] == "jpeg"
    assert frame["data_b64"]


def test_video_frame_over_cap_is_dropped(server_module):
    # F-03: the cap is DECODED bytes, so the payload has to decode to more
    # than the cap. This used to be ``"x" * (cap + 8)``, which is only
    # ~384 KiB decoded and is now legal, as it always should have been.
    server, pushed, *_ = server_module
    payload = {
        "event_type": "video_frame",
        "codec": "jpeg",
        "width": 640,
        "height": 480,
        "sequence": 2,
        "data_b64": _b64(server.VIDEO_FRAME_MAX_BYTES + 8),
    }
    reason = server._handle_video_frame("feral-glasses-test", payload, msg_id="m2")
    assert pushed == []
    # The reason is what daemon_session turns into the HUP 4020 error frame.
    assert reason and "video_frame" in reason


async def test_audio_frame_lands_in_the_voice_router(server_module):
    server, _pushed, ingested, *_ = server_module
    payload = {
        "event_type": "audio_frame",
        "codec": "opus",
        "sample_rate": 24000,
        "channels": 1,
        "sequence": 5,
        "data_b64": _b64(512),
    }
    await server._handle_audio_frame("feral-band-test", payload)
    assert len(ingested) == 1
    node_id, call = ingested[0]
    assert node_id == "feral-band-test"
    assert call["encoding"] == "opus"
    assert call["audio_b64"] == payload["data_b64"]


async def test_audio_frame_nested_payload_unwraps(server_module):
    server, _pushed, ingested, *_ = server_module
    payload = {
        "node_id": "feral-band-test",
        "event_type": "audio_frame",
        "data": {
            "codec": "opus",
            "sample_rate": 24000,
            "channels": 1,
            "sequence": 9,
            "data_b64": _b64(512),
        },
    }
    await server._handle_audio_frame(None, payload)
    assert len(ingested) == 1
    node_id, call = ingested[0]
    assert node_id == "feral-band-test"
    assert call["encoding"] == "opus"


async def test_audio_frame_over_cap_is_dropped(server_module):
    # F-03: decoded bytes, see the video_frame case above.
    server, _pushed, ingested, *_ = server_module
    payload = {
        "event_type": "audio_frame",
        "codec": "opus",
        "sample_rate": 24000,
        "channels": 1,
        "sequence": 6,
        "data_b64": _b64(server.AUDIO_FRAME_MAX_BYTES + 4),
    }
    reason = await server._handle_audio_frame("feral-band-test", payload)
    assert ingested == []
    assert reason and "audio_frame" in reason


async def test_audio_frame_no_voice_router_does_not_raise(server_module, monkeypatch):
    server, _pushed, _ingested, *_ = server_module
    # Mimic an early-boot brain: the socket is up, the router is not.
    server.state.voice_router = None
    payload = {
        "event_type": "audio_frame",
        "codec": "opus",
        "sample_rate": 24000,
        "channels": 1,
        "sequence": 0,
        "data_b64": _b64(64),
    }
    # Must NOT raise — daemon should not be punished for the brain
    # not being ready.
    await server._handle_audio_frame("any", payload)


# ---------------------------------------------------------------------------
# Biometric device_event dispatch — the wristband_daemon ships heart_rate
# and spo2 as device_event envelopes. Before the fix, the brain dropped
# them with a debug log. Now they land in the same sinks as the legacy
# `telemetry` branch.
# ---------------------------------------------------------------------------


def test_heart_rate_device_event_hits_perception_and_baseline(server_module):
    server, _pushed, _ingested, sensor_updates, baseline_records = server_module
    payload = {
        "node_id": "feral-band-test",
        "event_type": "heart_rate",
        "data": {"bpm": 82, "confidence": 0.9},
    }
    server._handle_biometric_device_event("feral-band-test", "heart_rate", payload)
    assert any(s[1].get("ppg_heart_rate") == 82 for s in sensor_updates)
    # server._BIOMETRIC_KEY_MAP routes "ppg_heart_rate" → "hr_resting".
    assert any(mid == "hr_resting" for mid, *_ in baseline_records)


def test_lagging_source_hr_does_not_train_baseline(server_module):
    """Stale cloud/HealthKit reads must not pollute the resting baseline.

    Operator report 2026-06-07: ``apple_healthkit`` HR=115 (a workout read
    hours old, resampled to "now") was being averaged into ``hr_resting``,
    dragging the learned mean to ~100 bpm. Lagging sources are now excluded
    from baseline training even when their sample_ts looks fresh.
    """
    server, _pushed, _ingested, _sensor_updates, baseline_records = server_module
    payload = {
        "node_id": "feral-band-test",
        "event_type": "heart_rate",
        "bpm": 115,
        "source": "apple_healthkit",
        "heart_rate_sample_ts": time.time(),
    }
    server._handle_biometric_device_event("feral-band-test", "heart_rate", payload)
    assert not any(mid == "hr_resting" for mid, *_ in baseline_records)


def test_live_wearable_hr_trains_baseline(server_module):
    """A fresh wearable read (with source) still trains the baseline."""
    server, _pushed, _ingested, _sensor_updates, baseline_records = server_module
    payload = {
        "node_id": "feral-band-test",
        "event_type": "heart_rate",
        "bpm": 51,
        "source": "veepoo_wristband",
        "heart_rate_sample_ts": time.time(),
    }
    server._handle_biometric_device_event("feral-band-test", "heart_rate", payload)
    assert any(mid == "hr_resting" and val == 51 for mid, val, *_ in baseline_records)


def test_stale_wearable_hr_does_not_train_baseline(server_module):
    """Even a wearable sample that is old (sample_ts hours ago) is skipped."""
    server, _pushed, _ingested, _sensor_updates, baseline_records = server_module
    payload = {
        "node_id": "feral-band-test",
        "event_type": "heart_rate",
        "bpm": 60,
        "source": "veepoo_wristband",
        "heart_rate_sample_ts": time.time() - 3600,
    }
    server._handle_biometric_device_event("feral-band-test", "heart_rate", payload)
    assert not any(mid == "hr_resting" for mid, *_ in baseline_records)


def test_spo2_device_event_records_to_sensors(server_module):
    server, _pushed, _ingested, sensor_updates, _baseline = server_module
    payload = {
        "node_id": "feral-band-test",
        "event_type": "spo2",
        "data": {"current": 97},
    }
    server._handle_biometric_device_event("feral-band-test", "spo2", payload)
    assert any(s[1].get("spo2_pct") == 97 for s in sensor_updates)


def test_gesture_device_event_hits_gesture_pipeline(server_module):
    server, _pushed, _ingested, sensor_updates, _baseline = server_module
    payload = {
        "node_id": "feral-glasses-test",
        "event_type": "gesture",
        "data": {"gesture": "nod", "confidence": 0.85},
    }
    server._handle_biometric_device_event("feral-glasses-test", "gesture", payload)
    assert any(s[1].get("gesture") == "nod" for s in sensor_updates)


def test_unknown_event_type_does_not_raise(server_module):
    """HUP §1 forward-compat rule — unknown event_types are ignored."""
    server, *_ = server_module
    # Never reaches the biometric dispatcher, but the handler must
    # tolerate being called with an unrecognised key anyway.
    server._handle_biometric_device_event(
        "feral-band-test", "something_invented_tomorrow", {"data": {"x": 1}}
    )
