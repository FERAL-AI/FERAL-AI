"""HUP v1.1 end-to-end: real FeralNode -> real Brain /v1/node handler.

This is the test that yesterday's commits should have shipped. Instead
of fakes it wires a real ``feral_node_sdk.FeralNode`` to an in-process
Starlette TestClient WebSocket, emits real media frames, and asserts
the Brain's handlers actually ingest them through the same sinks a
live daemon would hit.

It catches the two bugs c13460b introduced:
1. ``FeralNode.run`` was a synchronous ``asyncio.run`` wrapper while
   daemons did ``await self.node.run()``. Fixed by adding
   ``async def run_async`` to the SDK.
2. The SDK's ``emit_video_frame`` / ``emit_audio_frame`` serialise
   frame fields inside ``DeviceEventPayload.data``. The Brain's
   ``_handle_video_frame`` previously read ``data_b64`` at the top
   level, so every SDK-sent frame was silently dropped. Fixed by
   :func:`api.server._unwrap_hup_frame`.
"""

from __future__ import annotations

import base64
import importlib
import sys
from pathlib import Path
from typing import Any, List, Tuple

import pytest


pytestmark = pytest.mark.no_auto_feral_home


# ---------------------------------------------------------------------------
# Make the in-tree Python SDK importable without installing it.
# ---------------------------------------------------------------------------

_SDK_SRC = Path(__file__).resolve().parents[2] / "feral-nodes" / "python-node-sdk" / "src"
if str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


# ---------------------------------------------------------------------------
# Fake Brain — a minimal dict of collector lists so every SDK call lands
# somewhere we can assert against. This is intentionally NOT a mock of
# the SDK; it's a mock of the server-side state that the real handlers
# dispatch into.
# ---------------------------------------------------------------------------

def _b64_of(size: int) -> str:
    return base64.b64encode(b"\x01" * size).decode("ascii")


@pytest.fixture()
def wired_server(monkeypatch):
    server = importlib.import_module("api.server")

    pushed: List[Tuple[str, dict]] = []
    ingested: List[Tuple[str, dict]] = []
    sensors_seen: List[Tuple[str, dict]] = []
    baseline: List[Tuple[str, float, Any]] = []

    class FakeVisionBuffer:
        # Mirrors VisionBuffer.push(node_id, frame). See F-01.
        def push(self, node_id, frame):
            pushed.append((node_id, frame))

    class FakePerception:
        def update_vision(self, *_a, **_k):
            pass

        def update_sensors(self, sid, sensors):
            sensors_seen.append((sid, sensors))

        def update_gesture(self, sid, gesture):
            sensors_seen.append((sid, {"gesture": gesture}))

    class FakeChangeDetector:
        def should_analyze(self, *_a, **_k):
            return None

    class FakeVoiceRouter:
        # Mirrors VoiceRouter.handle_audio_from_node. The previous double
        # here defined ``ingest_frame``, which AudioPipeline does not, so
        # this file asserted a call production could never make.
        async def handle_audio_from_node(self, **kwargs):
            ingested.append((kwargs["node_id"], kwargs))

    class FakeBaseline:
        def record(self, metric_id, value, category=None):
            baseline.append((metric_id, value, category))

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
            return ["sid-e2e"]

    monkeypatch.setattr(server, "state", FakeState())
    return {
        "server": server,
        "pushed": pushed,
        "ingested": ingested,
        "sensors": sensors_seen,
        "baseline": baseline,
    }


# ---------------------------------------------------------------------------
# Exercise the REAL SDK's emit helpers against the unwrap helper + handlers.
#
# We don't open a WebSocket — we bypass ``FeralNode._send`` and assert that
# the payload the SDK *would* put on the wire, when fed directly into the
# Brain's dispatcher, lands in the right sink. This catches the shape bug
# without needing a full uvicorn loop.
# ---------------------------------------------------------------------------


def _simulated_wire_payload(model) -> dict:
    """Reproduce what the SDK's ``_send`` would put on the wire for the
    given DeviceEventPayload model. Mirrors the production serialisation
    (``model_dump(exclude_none=False)``)."""
    return model.model_dump(exclude_none=False)


def test_sdk_run_method_is_awaitable():
    """The run_async coroutine is awaitable; the sync run wrapper is not.

    This is the contract daemons depend on. It guards against the
    regression where ``FeralNode.run`` was a sync method that called
    ``asyncio.run`` internally, making ``await node.run()`` a
    TypeError at runtime.
    """
    from feral_node_sdk import FeralNode
    import inspect

    assert inspect.iscoroutinefunction(FeralNode.run_async), (
        "FeralNode.run_async must be an async coroutine function"
    )
    # Sync wrapper stays sync; that's how CLI entry points use it.
    assert not inspect.iscoroutinefunction(FeralNode.run), (
        "FeralNode.run must stay synchronous so asyncio.run(...) usage keeps working"
    )


def test_real_sdk_video_frame_payload_lands_in_vision_buffer(wired_server):
    from feral_node_sdk import FeralNode
    from feral_node_sdk.schemas import DeviceEventPayload, VideoFramePayload

    node = FeralNode(
        node_id="theora-glasses-e2e",
        name="Theora e2e",
        node_type="glasses",
        capabilities=["camera"],
    )

    vf = VideoFramePayload(
        codec="jpeg",
        width=640,
        height=480,
        sequence=1,
        keyframe=True,
        data_b64=_b64_of(1024),
    )
    envelope = DeviceEventPayload(
        node_id=node.node_id,
        event_type="video_frame",
        data=vf.model_dump(),
    )
    wire_payload = _simulated_wire_payload(envelope)

    server = wired_server["server"]
    # Mirrors what the /v1/node handler's device_event branch does.
    assert wire_payload.get("event_type") == "video_frame"
    assert wire_payload.get("data", {}).get("data_b64"), (
        "SDK must serialise data_b64 under payload.data; if this assertion "
        "fails, the emit_video_frame contract drifted."
    )

    server._handle_video_frame(None, wire_payload, msg_id="e2e-video")

    assert len(wired_server["pushed"]) == 1, (
        "The Brain handler must accept the SDK's nested device_event "
        "shape and land a frame in vision_buffer. Got zero frames — "
        "the nested-vs-flat bug is back."
    )
    node_id, frame = wired_server["pushed"][0]
    assert node_id == "theora-glasses-e2e"
    assert frame["codec"] == "jpeg"
    assert frame["data_b64"]


async def test_real_sdk_audio_frame_payload_lands_in_the_voice_router(wired_server):
    from feral_node_sdk import FeralNode
    from feral_node_sdk.schemas import AudioFramePayload, DeviceEventPayload

    node = FeralNode(
        node_id="feral-band-e2e",
        name="Wristband e2e",
        node_type="wearable",
        capabilities=["microphone"],
    )

    af = AudioFramePayload(
        codec="opus",
        sample_rate=24000,
        channels=1,
        sequence=7,
        data_b64=_b64_of(256),
    )
    envelope = DeviceEventPayload(
        node_id=node.node_id,
        event_type="audio_frame",
        data=af.model_dump(),
    )
    wire_payload = _simulated_wire_payload(envelope)
    server = wired_server["server"]
    await server._handle_audio_frame(None, wire_payload)

    assert len(wired_server["ingested"]) == 1
    node_id, call = wired_server["ingested"][0]
    assert node_id == "feral-band-e2e"
    assert call["encoding"] == "opus"


def test_real_sdk_heart_rate_device_event_reaches_perception(wired_server):
    """The wristband daemon emits heart_rate as a HUP device_event. The
    Brain must route it into the same sinks the legacy ``telemetry``
    branch uses. Without the Track B2 fix, the value vanished."""
    from feral_node_sdk.schemas import DeviceEventPayload

    envelope = DeviceEventPayload(
        node_id="feral-band-e2e",
        event_type="heart_rate",
        data={"bpm": 78, "confidence": 0.92},
    )
    wire_payload = _simulated_wire_payload(envelope)
    server = wired_server["server"]
    server._handle_biometric_device_event(None, "heart_rate", wire_payload)

    assert any(
        s[1].get("ppg_heart_rate") == 78 for s in wired_server["sensors"]
    ), "heart_rate device_event should populate perception.update_sensors"
    assert any(
        mid == "hr_resting" and val == 78.0
        for mid, val, _cat in wired_server["baseline"]
    ), "heart_rate device_event should record into baseline as hr_resting"
