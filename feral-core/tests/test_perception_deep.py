"""
Deep unit tests for the perception subsystem (screen loop, location, fusion, change detection).

All I/O and platform hooks are mocked; tests are deterministic and fast.
"""
from __future__ import annotations

import asyncio
import base64
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from perception.change_detector import ChangeDetector, ChangeEvent
from perception.fusion import PerceptionEngine, PerceptionFrame
from perception.location import GeoPoint, LocationEngine
from perception.screen_loop import (
    ScreenLoop,
    ScreenTransitionDetector,
    TransitionEvent,
)

pytestmark = pytest.mark.no_auto_feral_home


def _fake_image_b64(seed: int, min_len: int = 400) -> str:
    """Deterministic pseudo-image bytes that satisfy ChangeDetector length checks."""
    raw = bytes((i + seed) % 256 for i in range(min_len))
    return base64.b64encode(raw).decode("ascii")


# ── ScreenLoop & ScreenTransitionDetector ─────────────────────────────


def test_screen_loop_init_clamps_interval_and_wires_dependencies():
    perception = MagicMock(spec=PerceptionEngine)
    memory = MagicMock()
    llm = MagicMock()
    loop = ScreenLoop(
        perception=perception,
        memory=memory,
        llm=llm,
        interval=0.5,
        session_id="test_sess",
    )
    assert loop._interval == 1.0  # max(1.0, interval)
    assert loop._session_id == "test_sess"
    assert loop._perception is perception
    assert loop._memory is memory
    assert loop._llm is llm
    assert not loop.is_running


def test_screen_loop_stats_reflect_running_and_counters():
    loop = ScreenLoop(interval=2.0)
    loop._capture_count = 3
    loop._error_count = 1
    loop._last_description = "idle"
    stats = loop.stats
    assert stats["running"] is False
    assert stats["interval"] == 2.0
    assert stats["captures"] == 3
    assert stats["errors"] == 1
    assert stats["last_description"] == "idle"


@pytest.mark.asyncio
async def test_screen_loop_start_stop_lifecycle():
    loop = ScreenLoop(interval=0.01)
    with patch.object(ScreenLoop, "_tick", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await loop.start()
            await asyncio.sleep(0)
            assert loop.is_running
            await loop.stop()
    assert not loop.is_running
    assert loop._task is None


@pytest.mark.asyncio
async def test_screen_loop_start_is_idempotent_when_already_running():
    loop = ScreenLoop(interval=0.01)
    with patch.object(ScreenLoop, "_tick", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await loop.start()
            first_task = loop._task
            await loop.start()
            assert loop._task is first_task
            await loop.stop()


@pytest.mark.asyncio
async def test_screen_loop_tick_llm_path_updates_perception():
    perception = MagicMock(spec=PerceptionEngine)
    frame = PerceptionFrame()
    perception.get_frame.return_value = frame
    llm = MagicMock()
    llm.available = True

    loop = ScreenLoop(perception=perception, llm=llm, interval=5.0, session_id="s1")
    fake_raw = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
    loop._tmp_path.write_bytes(fake_raw)

    with patch("perception.screen_loop._capture_screenshot", new_callable=AsyncMock, return_value=True):
        with patch(
            "perception.screen_loop._downscale_and_encode",
            return_value=("imgb64", "image/jpeg"),
        ):
            with patch(
                "perception.screen_loop._ask_vision_llm",
                new_callable=AsyncMock,
                return_value="Editing Swift code in Xcode with Safari open",
            ):
                await loop._tick()

    try:
        loop._tmp_path.unlink(missing_ok=True)
    except OSError:
        pass

    perception.get_frame.assert_called_with("s1")
    assert frame.has_vision is True
    assert frame.scene_description.startswith("Editing") or "Xcode" in frame.scene_description
    assert "Xcode" in frame.detected_objects
    assert loop._capture_count == 1


@pytest.mark.asyncio
async def test_screen_loop_tick_delegates_to_scene_analyzer():
    perception = MagicMock(spec=PerceptionEngine)
    frame = PerceptionFrame()
    perception.get_frame.return_value = frame

    scene = MagicMock()
    scene.available = True
    scene.analyze_frame = AsyncMock(
        return_value={
            "scene_description": "Spreadsheet in Numbers",
            "detected_objects": ["table", "chart"],
        }
    )

    loop = ScreenLoop(
        perception=perception,
        scene_analyzer=scene,
        interval=5.0,
        session_id="node-a",
    )
    fake_raw = b"\xff\xd8\xff\xe0" + bytes(300)
    loop._tmp_path.write_bytes(fake_raw)

    with patch("perception.screen_loop._capture_screenshot", new_callable=AsyncMock, return_value=True):
        with patch(
            "perception.screen_loop._downscale_and_encode",
            return_value=("bbb", "image/jpeg"),
        ):
            await loop._tick()

    try:
        loop._tmp_path.unlink(missing_ok=True)
    except OSError:
        pass

    scene.analyze_frame.assert_awaited_once()
    call_kw = scene.analyze_frame.await_args
    assert call_kw.kwargs.get("encoding") == "jpeg"
    assert call_kw.kwargs.get("node_id") == "node-a"
    assert frame.scene_description == "Spreadsheet in Numbers"
    assert frame.detected_objects == ["table", "chart"]


@pytest.mark.asyncio
async def test_screen_loop_records_episode_when_cost_accounting_raises():
    """Bookkeeping must not destroy an observation we already paid for.

    Regression: ``_tick`` awaited ``cost_guard.record()`` bare, and a
    CostBudget whose asyncio.Lock was bound to another event loop raised
    RuntimeError there on every tick. 1,548 ticks between 2026-07-07 and
    2026-07-26 captured a frame, called the VLM, then threw the
    description away before reaching the episode save.
    """
    perception = MagicMock(spec=PerceptionEngine)
    frame = PerceptionFrame()
    perception.get_frame.return_value = frame
    memory = MagicMock()
    memory.episode_save = AsyncMock()

    scene = MagicMock()
    scene.available = True
    scene.analyze_frame = AsyncMock(
        return_value={"scene_description": "Debugging a crash in the IDE"}
    )

    guard = MagicMock()
    guard.allow.return_value = True
    guard.is_paused = False
    guard.record = AsyncMock(
        side_effect=RuntimeError("Lock is bound to a different event loop")
    )

    loop = ScreenLoop(
        perception=perception,
        memory=memory,
        scene_analyzer=scene,
        cost_guard=guard,
        interval=5.0,
        session_id="screen_loop",
    )
    loop._detector._previous = "Reading a long email in a mail client"
    loop._tmp_path.write_bytes(b"\xff\xd8\xff\xe0" + bytes(300))

    with patch("perception.screen_loop._capture_screenshot", new_callable=AsyncMock, return_value=True):
        with patch(
            "perception.screen_loop._downscale_and_encode",
            return_value=("bbb", "image/jpeg"),
        ):
            await loop._tick()

    try:
        loop._tmp_path.unlink(missing_ok=True)
    except OSError:
        pass

    memory.episode_save.assert_awaited_once()
    saved = memory.episode_save.await_args.kwargs
    assert saved["summary"] == "Screen: Debugging a crash in the IDE"
    assert saved["event_type"].startswith("screen_")
    assert frame.scene_description == "Debugging a crash in the IDE"


@pytest.mark.asyncio
async def test_screen_loop_warns_when_capturing_but_seeing_nothing(caplog):
    """A loop that captures forever and describes nothing must say so.

    Regression: when the VLM produced no description ``_tick`` silently
    returned while ``stats["captures"]`` kept climbing, so the loop
    reported healthy for a week of recording zero episodes.
    """
    from perception.screen_loop import _BLIND_TICK_ALERT_EVERY

    perception = MagicMock(spec=PerceptionEngine)
    perception.get_frame.return_value = PerceptionFrame()

    scene = MagicMock()
    scene.available = True
    scene.analyze_frame = AsyncMock(return_value=None)

    loop = ScreenLoop(perception=perception, scene_analyzer=scene, interval=8.0)
    loop._tmp_path.write_bytes(b"\xff\xd8\xff\xe0" + bytes(300))

    with patch("perception.screen_loop._capture_screenshot", new_callable=AsyncMock, return_value=True):
        with patch(
            "perception.screen_loop._downscale_and_encode",
            return_value=("bbb", "image/jpeg"),
        ):
            with caplog.at_level("WARNING", logger="feral.perception.screen"):
                for _ in range(_BLIND_TICK_ALERT_EVERY):
                    await loop._tick()

    try:
        loop._tmp_path.unlink(missing_ok=True)
    except OSError:
        pass

    assert loop.stats["blind_ticks"] == _BLIND_TICK_ALERT_EVERY
    assert any("consecutive frames with no" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_screen_loop_blind_tick_counter_resets_on_a_real_description():
    perception = MagicMock(spec=PerceptionEngine)
    perception.get_frame.return_value = PerceptionFrame()

    scene = MagicMock()
    scene.available = True
    scene.analyze_frame = AsyncMock(return_value=None)

    loop = ScreenLoop(perception=perception, scene_analyzer=scene, interval=8.0)
    loop._tmp_path.write_bytes(b"\xff\xd8\xff\xe0" + bytes(300))

    with patch("perception.screen_loop._capture_screenshot", new_callable=AsyncMock, return_value=True):
        with patch(
            "perception.screen_loop._downscale_and_encode",
            return_value=("bbb", "image/jpeg"),
        ):
            await loop._tick()
            assert loop.stats["blind_ticks"] == 1

            scene.analyze_frame = AsyncMock(
                return_value={"scene_description": "Writing a document in Pages"}
            )
            await loop._tick()

    try:
        loop._tmp_path.unlink(missing_ok=True)
    except OSError:
        pass

    assert loop.stats["blind_ticks"] == 0


@pytest.mark.asyncio
async def test_screen_loop_transition_callback_async():
    perception = MagicMock(spec=PerceptionEngine)
    frame = PerceptionFrame()
    perception.get_frame.return_value = frame
    cb = AsyncMock()
    llm = MagicMock()
    llm.available = True

    loop = ScreenLoop(
        perception=perception,
        llm=llm,
        interval=5.0,
        on_transition=cb,
    )
    ev = TransitionEvent(
        timestamp=123.0,
        kind="app_switch",
        previous="old",
        current="new",
        confidence=0.91,
    )

    loop._tmp_path.write_bytes(b"x" * 400)

    with patch("perception.screen_loop._capture_screenshot", new_callable=AsyncMock, return_value=True):
        with patch(
            "perception.screen_loop._downscale_and_encode",
            return_value=("z", "image/jpeg"),
        ):
            with patch(
                "perception.screen_loop._ask_vision_llm",
                new_callable=AsyncMock,
                return_value="desc",
            ):
                with patch.object(loop._detector, "detect", return_value=ev):
                    with patch.object(loop, "_record_transition", new_callable=AsyncMock):
                        await loop._tick()

    cb.assert_awaited_once()
    assert cb.await_args.args[0] is ev


def test_screen_transition_detector_emits_on_dissimilar_descriptions():
    det = ScreenTransitionDetector(similarity_threshold=0.55)
    assert det.detect("hello world foo") is None
    out = det.detect("completely different text now zebra")
    assert out is not None
    assert out.previous == "hello world foo"
    assert out.current == "completely different text now zebra"
    assert out.kind in (
        "general",
        "app_switch",
        "error",
        "document",
        "browsing",
        "terminal",
        "ide",
    )


# ── LocationEngine ────────────────────────────────────────────────────


@pytest.fixture
def location_engine(tmp_path):
    with patch("perception.location.feral_data_home", return_value=tmp_path):
        eng = LocationEngine()
        yield eng
        eng.close()


def test_location_engine_add_and_list_geofence(location_engine):
    f = location_engine.add_geofence("office", 40.0, -74.0, 150.0, on_enter="enter", on_exit="exit")
    assert f.name == "office"
    assert f.radius_m == 150.0
    assert f.on_enter == "enter"
    all_f = location_engine.list_geofences()
    assert len(all_f) == 1
    assert all_f[0].center.lat == 40.0


def test_location_engine_haversine_zero_and_symmetric(location_engine):
    p = GeoPoint(10.0, 20.0)
    assert location_engine._haversine(p, p) == 0.0
    a = GeoPoint(52.0, 0.0)
    b = GeoPoint(52.009, 0.0)
    d_ab = location_engine._haversine(a, b)
    d_ba = location_engine._haversine(b, a)
    assert math.isclose(d_ab, d_ba, rel_tol=1e-9)
    assert 900 < d_ab < 1100


@pytest.mark.asyncio
async def test_location_engine_enter_exit_events(location_engine):
    location_engine.add_geofence("zone", 40.7128, -74.0060, 500.0)
    out = await location_engine.update_location(40.7200, -74.0060, source="test")
    assert not out
    ev_in = await location_engine.update_location(40.7130, -74.0060, source="test")
    assert len(ev_in) == 1
    assert ev_in[0]["event"] == "enter"
    assert ev_in[0]["fence"] == "zone"
    ev_out = await location_engine.update_location(40.7200, -74.0060, source="test")
    assert len(ev_out) == 1
    assert ev_out[0]["event"] == "exit"


@pytest.mark.asyncio
async def test_location_engine_distance_to_fence_center(location_engine):
    location_engine.add_geofence("p", 1.0, 2.0, 1000.0)
    await location_engine.update_location(1.0, 2.0, source="gps")
    d = location_engine.distance_to("p")
    assert d == 0.0


@pytest.mark.asyncio
async def test_location_engine_async_callback_on_trigger(location_engine):
    location_engine.add_geofence("c", 0.0, 0.0, 1_000_000.0)
    seen = []

    async def cb(event):
        seen.append(event)

    location_engine.on_trigger(cb)
    await location_engine.update_location(0.0, 0.0, source="t")
    assert len(seen) == 1
    assert seen[0]["event"] == "enter"


# ── PerceptionEngine (frame management) ───────────────────────────────


def test_perception_engine_get_frame_creates_and_reuses():
    eng = PerceptionEngine()
    a = eng.get_frame("s")
    b = eng.get_frame("s")
    assert a is b
    assert isinstance(a, PerceptionFrame)


def test_perception_engine_clear_removes_session():
    eng = PerceptionEngine()
    eng.get_frame("x").heart_rate = 99
    eng.clear("x")
    fresh = eng.get_frame("x")
    assert fresh.heart_rate == 0


def test_perception_engine_update_sensors_merges_into_frame():
    eng = PerceptionEngine()
    eng.update_sensors(
        "sess",
        {
            "vitals": {"ppg_heart_rate": 88},
            "gps": {"lat": 1.0, "lon": 2.0},
        },
    )
    fr = eng.get_frame("sess")
    assert fr.heart_rate == 88
    assert fr.location == {"lat": 1.0, "lon": 2.0}


def test_perception_engine_update_frame_via_mutable_get_frame():
    """PerceptionEngine exposes a mutable PerceptionFrame per session (no separate update_frame API)."""
    eng = PerceptionEngine()
    eng.get_frame("live").transcript = "user said hello"
    eng.get_frame("live").has_vision = True
    assert eng.get_frame("live").transcript == "user said hello"
    assert eng.get_frame("live").has_vision is True


# ── ChangeDetector ────────────────────────────────────────────────────


def test_change_detector_first_frame_triggers_scene_change():
    det = ChangeDetector(min_interval=0.0, change_threshold=0.99)
    b64 = _fake_image_b64(1)
    t0 = 1000.0
    with patch("perception.change_detector.time.time", return_value=t0):
        ev = det.should_analyze("n1", b64)
    assert ev is not None
    assert ev.trigger_reason == "scene_change"
    assert ev.change_score == 1.0


def test_change_detector_substantially_different_frame_triggers():
    det = ChangeDetector(min_interval=0.0, change_threshold=0.05)
    a = _fake_image_b64(0)
    b = _fake_image_b64(99)
    with patch.object(det, "_histogram_distance", return_value=0.5):
        with patch("perception.change_detector.time.time", side_effect=[100.0, 101.0]):
            first = det.should_analyze("n", a)
            assert first is not None
            second = det.should_analyze("n", b)
    assert second is not None
    assert second.trigger_reason in ("scene_change", "motion_start")


def test_change_detector_periodic_trigger_when_still():
    det = ChangeDetector(
        min_interval=0.0,
        max_interval=10.0,
        change_threshold=0.99,
        still_frame_count=100,
    )
    same = _fake_image_b64(3)
    times = [1000.0, 1020.0]
    with patch("perception.change_detector.time.time", side_effect=times):
        det.should_analyze("p", same)
        ev = det.should_analyze("p", same)
    assert ev is not None
    assert ev.trigger_reason == "periodic"


def test_change_detector_force_trigger():
    det = ChangeDetector()
    t0 = 500.0
    with patch("perception.change_detector.time.time", return_value=t0):
        ev = det.force_trigger("z", reason="user_request")
    assert isinstance(ev, ChangeEvent)
    assert ev.trigger_reason == "user_request"
    assert ev.node_id == "z"


def test_change_detector_clear_node_removes_state():
    det = ChangeDetector(min_interval=0.0)
    det.should_analyze("k", _fake_image_b64(7))
    det.clear_node("k")
    assert det.stats()["tracked_nodes"] == 0
