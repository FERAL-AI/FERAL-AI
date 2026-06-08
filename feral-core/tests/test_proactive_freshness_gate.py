"""Pin the freshness gate on proactive HR / SpO2 alerts.

Operator report (2026-05-09): the FERAL web-UI showed
``Heart Rate Alert: 115 bpm`` while the W300 glasses were
disconnected. The number was NOT fake — it came from
``Apple HealthKit`` returning the most-recent recorded HR sample
(possibly hours old, e.g. from a workout that morning). The
proactive engine fired on the stale value as if it were a
real-time reading.

Fix: ``PerceptionFrame`` now tracks per-metric sample timestamps
(``heart_rate_sample_ts``, ``spo2_sample_ts``) and source labels
(``heart_rate_source``, ``spo2_source``). The proactive engine's
``_evaluate`` requires the sample to be within
``FRESH_WINDOW_S`` (120s) of "now" before firing. The notification
body surfaces the source + sample age so the user can audit.

This test pins the freshness gate end-to-end through ``_evaluate``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agents.proactive_engine import ProactiveEngine
from perception.fusion import PerceptionFrame


def _engine_with_frame(frame: PerceptionFrame) -> tuple[ProactiveEngine, list]:
    """Returns (engine, captured_messages_list).

    ``_deliver`` is patched to append to the list so the test can
    inspect what would have been sent. ``_can_fire`` is forced True
    so the cooldown logic doesn't suppress a legitimate alert.
    """
    perception = MagicMock()
    perception._frames = {"sess-1": frame}
    perception.get_frame = lambda sid: frame
    eng = ProactiveEngine()
    eng._perception = perception
    eng._first_interaction_today = False  # skip morning-briefing branch
    captured: list = []

    async def _capture(msg):
        captured.append(msg)

    eng._deliver = _capture  # type: ignore[assignment]
    eng._can_fire = lambda trigger_id: True  # type: ignore[assignment]
    eng._record_fire = lambda trigger_id: None  # type: ignore[assignment]
    return eng, captured


@pytest.mark.asyncio
async def test_fresh_elevated_hr_does_fire() -> None:
    """A real elevated reading taken just now from a LIVE wearable
    must still fire.

    The source matters (operator report 2026-06-08 demo prep, fix
    #2): cloud-mirror sources like ``apple_healthkit`` resample
    ``sample_ts`` to "now" even when the underlying read is hours
    old, so freshness alone can't tell live from stale. The proactive
    engine therefore now requires a non-lagging source AND a fresh
    sample. The W300 glasses (``jw_health_glasses``) is the
    canonical demo source for elevated HR alerts.
    """
    frame = PerceptionFrame(
        heart_rate=115,
        heart_rate_sample_ts=time.time() - 5.0,  # 5s old, fresh
        heart_rate_source="jw_health_glasses",
        activity_state="resting",
    )
    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    fired = [m for m in captured if m.trigger_id == "hr_elevated"]
    assert fired, (
        "fresh elevated HR (115 bpm, 5s old) from a live wearable "
        "must fire the elevated alert"
    )
    body = fired[0].body
    assert "115" in body
    assert "jw_health_glasses" in body, "source must surface in body"


@pytest.mark.asyncio
async def test_fresh_elevated_hr_from_lagging_source_does_not_fire() -> None:
    """Operator report 2026-06-08 — even a "fresh" HealthKit reading
    (HealthKit relabels ``sample_ts`` to "now" for cached resting
    HR pulled from a workout hours earlier) MUST NOT trip
    ``hr_elevated`` → ``scene.calming``.  The lagging-source guard
    is the only thing that catches this; the freshness window can't.
    """
    frame = PerceptionFrame(
        heart_rate=115,
        heart_rate_sample_ts=time.time() - 5.0,  # looks fresh
        heart_rate_source="apple_healthkit",
        activity_state="resting",
    )
    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    fired = [m for m in captured if m.trigger_id == "hr_elevated"]
    assert not fired, (
        "fresh-looking apple_healthkit elevated HR must NOT fire — "
        "the proactive engine's lagging-source guard (fix #2) was "
        "introduced precisely to block this stale-relabel pattern."
    )


@pytest.mark.asyncio
async def test_stale_elevated_hr_does_not_fire() -> None:
    """The exact operator-reported case: HR=115 with a >2-minute-old sample."""
    frame = PerceptionFrame(
        heart_rate=115,
        heart_rate_sample_ts=time.time() - 3600.0,  # 1h old
        heart_rate_source="apple_healthkit",
        activity_state="resting",
    )
    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    fired = [m for m in captured if m.trigger_id == "hr_elevated"]
    assert not fired, (
        "STALE elevated HR (sample 1h old) must NOT fire — that's the "
        "exact 2026-05-09 operator-reported regression. The freshness "
        "gate in agents/proactive_engine.py likely got loosened."
    )


@pytest.mark.asyncio
async def test_hr_with_no_sample_ts_does_not_fire() -> None:
    """If a sender never set sample_ts (legacy frame), don't fire.

    Conservative: missing freshness data => treat as stale. Old
    senders that don't yet plumb ``sample_ts`` will need to be
    upgraded to opt back into proactive alerts.
    """
    frame = PerceptionFrame(
        heart_rate=115,
        heart_rate_sample_ts=0.0,  # explicit "never seen"
        activity_state="resting",
    )
    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    fired = [m for m in captured if m.trigger_id == "hr_elevated"]
    assert not fired, (
        "Frame with sample_ts=0.0 must NOT fire — missing freshness "
        "data is treated as stale by design (defensive default)."
    )


@pytest.mark.asyncio
async def test_legacy_sender_omitting_sample_ts_does_not_fake_fresh() -> None:
    """End-to-end pin for the 2026-05-09 round 2 regression: an old
    iOS build that emits ``device_event`` payloads WITHOUT a
    ``heart_rate_sample_ts`` field must NOT cause the brain to fire
    Heart Rate Alert. The fix is in
    ``perception/fusion.update_sensors`` — when ``*_sample_ts`` is
    missing the frame field defaults to ``0.0`` (= "never seen"), NOT
    ``time.time()`` (= fresh).

    Without this pin, an old companion build that sends a stale
    HealthKit reading (HR=115 from a workout 4 hours ago) would land
    at the brain with no ``sample_ts``, fusion would stamp
    ``time.time()`` as the freshness, and the proactive engine would
    fire ``hr_elevated`` on a 4-hour-old reading — the exact bug the
    operator caught at 15:49:37 in the 2026-05-09 logs.
    """
    from perception.fusion import PerceptionEngine

    perception = PerceptionEngine()
    perception.update_sensors("legacy-sess", {
        "vitals": {"ppg_heart_rate": 115},
        # explicitly NO ppg_heart_rate_sample_ts / heart_rate_sample_ts
    })
    frame = perception.get_frame("legacy-sess")
    assert frame.heart_rate == 115
    assert frame.heart_rate_sample_ts == 0.0, (
        f"legacy payload (no sample_ts) must default to 0.0 not "
        f"time.time(). Got {frame.heart_rate_sample_ts!r}. The fix in "
        "perception/fusion.update_sensors regressed."
    )

    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    fired = [m for m in captured if m.trigger_id == "hr_elevated"]
    assert not fired, (
        "Old-build sender supplying HR=115 without sample_ts MUST NOT "
        "trigger an alert — that's the 2026-05-09 phantom-alert bug."
    )


@pytest.mark.asyncio
async def test_fresh_low_spo2_does_fire_with_source() -> None:
    frame = PerceptionFrame(
        spo2_pct=88,
        spo2_sample_ts=time.time() - 10.0,
        spo2_source="theora_w300",
    )
    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    fired = [m for m in captured if m.trigger_id == "spo2_low"]
    assert fired
    assert "88" in fired[0].body
    assert "theora_w300" in fired[0].body


@pytest.mark.asyncio
async def test_stale_low_spo2_does_not_fire() -> None:
    frame = PerceptionFrame(
        spo2_pct=88,
        spo2_sample_ts=time.time() - 7200.0,  # 2h old
        spo2_source="apple_healthkit",
    )
    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    fired = [m for m in captured if m.trigger_id == "spo2_low"]
    assert not fired


@pytest.mark.asyncio
async def test_freshness_window_boundary_120s() -> None:
    """Boundary check: 119s ago fires, 121s ago does not.

    Uses a live wearable source — ``apple_healthkit`` is now blocked
    by the lagging-source guard (fix #2) regardless of the freshness
    window, so the boundary test must use a non-lagging source to
    exercise the freshness gate cleanly.
    """
    frame_just_inside = PerceptionFrame(
        heart_rate=115,
        heart_rate_sample_ts=time.time() - 119.0,
        heart_rate_source="jw_health_glasses",
    )
    eng_inside, captured_inside = _engine_with_frame(frame_just_inside)
    await eng_inside._evaluate()
    inside = [m for m in captured_inside if m.trigger_id == "hr_elevated"]
    assert inside, "119s old (inside 120s window) should fire"

    frame_just_outside = PerceptionFrame(
        heart_rate=115,
        heart_rate_sample_ts=time.time() - 121.0,
        heart_rate_source="jw_health_glasses",
    )
    eng_outside, captured_outside = _engine_with_frame(frame_just_outside)
    await eng_outside._evaluate()
    outside = [m for m in captured_outside if m.trigger_id == "hr_elevated"]
    assert not outside, "121s old (outside 120s window) should NOT fire"
