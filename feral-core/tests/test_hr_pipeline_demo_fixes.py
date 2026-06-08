"""Pin the heart-rate pipeline fixes shipped on 2026-06-08
(operator demo prep).

The brain previously stamped every unstamped biometric ``device_event``
with ``time.time()`` — including HealthKit pushes that legitimately
omit a timestamp because the bridge re-emits cached resting HR
samples taken hours earlier. That made the proactive layer fire
``hr_elevated`` on stale data, ran the ``scene.calming`` automation
on a non-event, and let two simultaneously-streaming wearables
(W300 glasses + Veepoo wristband) thrash the live HR slot with no
priority.

This module covers the seven scoped fixes:

#1 / #3 — ``_handle_biometric_device_event`` now resolves
            sample timestamps from the canonical HUP envelope
            (``ts``) and the legacy iOS HealthKit ``timestamp``;
            arrival-stamps only when the source is live (or
            unknown / inferred from node caps); leaves it 0.0
            for lagging sources without a ts.
#2        — ``proactive_engine`` adds a lagging-source guard on
            ``hr_elevated`` / ``spo2_low`` / ``baseline_hr``.
#4        — ``perception.fusion._wearable_priority`` makes W300 >
            Veepoo > polar/wahoo > garmin > w610 within the fresh
            window; freshness is the secondary tiebreak.
#5        — Baselines namespace per source
            (``hr_resting:jw_health_glasses``) AND keep writing
            the bare ``hr_resting`` row for back-compat.
#6        — ``/api/dashboard.latest_health`` shares the
            ``BrainState._latest_live_wearable_snapshot`` source
            of truth with ``/api/health/summary``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

import api.server as srv
from agents.proactive_engine import ProactiveEngine
from perception.fusion import (
    PerceptionEngine,
    PerceptionFrame,
    _wearable_priority,
)


# ---------------------------------------------------------------------------
# Fix #1 — sample_ts resolution
# ---------------------------------------------------------------------------


def _drive(monkeypatch, event_type: str, payload: dict) -> dict:
    """Run a single ``device_event`` through the brain dispatcher and
    return the ``sensors`` dict the perception engine would have
    received."""
    captured: dict = {}

    class _Perc:
        def update_sensors(self, sid, sensors):  # noqa: ARG002
            captured.update(sensors)

    monkeypatch.setattr(srv.state, "perception", _Perc(), raising=False)
    monkeypatch.setattr(
        srv.state, "get_sessions_for_daemon", lambda n: ["s1"], raising=False,
    )
    monkeypatch.setattr(srv.state, "somatic_engine", None, raising=False)
    monkeypatch.setattr(srv.state, "baseline_engine", None, raising=False)
    monkeypatch.setattr(srv.state, "daemons", {}, raising=False)
    srv._handle_biometric_device_event("node-1", event_type, payload)
    return captured


def test_lagging_source_without_ts_resolves_to_zero(monkeypatch):
    """A HealthKit push that forgot to stamp its sample MUST NOT be
    arrival-stamped — that's the exact stale-relabel bug fix #1
    is closing.  The brain now recognises the source as lagging
    and assigns ``sample_ts=0.0`` so the freshness gate treats it
    as "never seen"."""
    captured = _drive(
        monkeypatch,
        "heart_rate",
        {"bpm": 115, "source": "apple_healthkit"},
    )
    assert captured.get("ppg_heart_rate") == 115
    assert captured.get("heart_rate_source") == "apple_healthkit"
    assert captured.get("heart_rate_sample_ts") == 0.0, (
        "lagging source without ts must resolve to 0.0 so the "
        "freshness gate treats it as not-fresh"
    )


def test_live_wearable_without_ts_is_arrival_stamped(monkeypatch):
    """A genuinely-live BLE wearable push (wristband_daemon / iOS
    FeralSensorBridge) legitimately omits a timestamp; arrival
    time IS a valid sample time for those.  Fix #1 must keep that
    path arrival-stamped."""
    before = time.time()
    captured = _drive(
        monkeypatch,
        "heart_rate",
        {"bpm": 60, "source": "veepoo_wristband"},
    )
    ts = captured.get("heart_rate_sample_ts")
    assert isinstance(ts, float) and ts >= before, (
        "live wearable without ts must arrival-stamp with time.time()"
    )


def test_canonical_hup_envelope_ts_is_picked_up(monkeypatch):
    """``ts`` is the canonical HUP v1.1 envelope name. Adapters that
    set the envelope-level ``ts`` instead of the typed
    ``heart_rate_sample_ts`` must still get their sample time
    propagated."""
    sample = 1717260000.0
    captured = _drive(
        monkeypatch,
        "heart_rate",
        {"bpm": 72, "source": "veepoo_wristband", "ts": sample},
    )
    assert captured.get("heart_rate_sample_ts") == sample


def test_legacy_ios_timestamp_field_is_picked_up(monkeypatch):
    """Legacy iOS HealthKit clients ship the field as ``timestamp``
    rather than ``sample_ts``. Fix #1 expanded the lookup to cover
    that key too."""
    sample = 1717260000.0
    captured = _drive(
        monkeypatch,
        "heart_rate",
        {"bpm": 72, "source": "veepoo_wristband", "timestamp": sample},
    )
    assert captured.get("heart_rate_sample_ts") == sample


def test_spo2_lagging_source_without_ts_resolves_to_zero(monkeypatch):
    """Same lagging-source policy applies to spo2."""
    captured = _drive(
        monkeypatch,
        "spo2",
        {"current": 92, "source": "apple_healthkit"},
    )
    assert captured.get("spo2_pct") == 92
    assert captured.get("spo2_sample_ts") == 0.0


# ---------------------------------------------------------------------------
# Fix #3 — source inference from node capabilities
# ---------------------------------------------------------------------------


def test_source_inferred_from_node_capabilities(monkeypatch):
    """Daemons that omit ``heart_rate_source`` must inherit the
    canonical wearable source from the node's advertised
    capabilities so the freshness / priority rules don't demote
    them.  Without this, ``_is_live_wearable("")`` is False and
    a HealthKit push can clobber a fresh BLE read."""
    captured: dict = {}

    class _Perc:
        def update_sensors(self, sid, sensors):  # noqa: ARG002
            captured.update(sensors)

    fake_ws = MagicMock()
    fake_ws._feral_capabilities = ["jw_health_glasses"]
    monkeypatch.setattr(srv.state, "perception", _Perc(), raising=False)
    monkeypatch.setattr(
        srv.state, "get_sessions_for_daemon", lambda n: ["s1"], raising=False,
    )
    monkeypatch.setattr(srv.state, "somatic_engine", None, raising=False)
    monkeypatch.setattr(srv.state, "baseline_engine", None, raising=False)
    monkeypatch.setattr(
        srv.state, "daemons", {"theora-w300-1": fake_ws}, raising=False,
    )
    srv._handle_biometric_device_event(
        "theora-w300-1", "heart_rate", {"bpm": 72},
    )
    assert captured.get("heart_rate_source") == "jw_health_glasses", (
        "daemon advertising jw_health_glasses caps but omitting "
        "heart_rate_source must have it inferred from node caps"
    )


def test_source_inference_handles_legacy_alias(monkeypatch):
    """The legacy ``theora_w300`` capability id maps to the
    canonical ``jw_health_glasses`` source string."""
    captured: dict = {}

    class _Perc:
        def update_sensors(self, sid, sensors):  # noqa: ARG002
            captured.update(sensors)

    fake_ws = MagicMock()
    fake_ws._feral_capabilities = ["theora_w300"]
    monkeypatch.setattr(srv.state, "perception", _Perc(), raising=False)
    monkeypatch.setattr(
        srv.state, "get_sessions_for_daemon", lambda n: ["s1"], raising=False,
    )
    monkeypatch.setattr(srv.state, "somatic_engine", None, raising=False)
    monkeypatch.setattr(srv.state, "baseline_engine", None, raising=False)
    monkeypatch.setattr(
        srv.state, "daemons", {"glasses-1": fake_ws}, raising=False,
    )
    srv._handle_biometric_device_event(
        "glasses-1", "heart_rate", {"bpm": 72},
    )
    assert captured.get("heart_rate_source") == "jw_health_glasses"


def test_source_inference_for_veepoo(monkeypatch):
    """Veepoo wristband daemons advertise ``veepoo_wristband`` —
    the inference must surface that source."""
    captured: dict = {}

    class _Perc:
        def update_sensors(self, sid, sensors):  # noqa: ARG002
            captured.update(sensors)

    fake_ws = MagicMock()
    fake_ws._feral_capabilities = ["veepoo_wristband"]
    monkeypatch.setattr(srv.state, "perception", _Perc(), raising=False)
    monkeypatch.setattr(
        srv.state, "get_sessions_for_daemon", lambda n: ["s1"], raising=False,
    )
    monkeypatch.setattr(srv.state, "somatic_engine", None, raising=False)
    monkeypatch.setattr(srv.state, "baseline_engine", None, raising=False)
    monkeypatch.setattr(
        srv.state, "daemons", {"wristband-1": fake_ws}, raising=False,
    )
    srv._handle_biometric_device_event(
        "wristband-1", "heart_rate", {"bpm": 60},
    )
    assert captured.get("heart_rate_source") == "veepoo_wristband"


def test_explicit_source_wins_over_inference(monkeypatch):
    """If the daemon DID set ``heart_rate_source`` we keep it
    verbatim; the inference is only a fallback."""
    captured: dict = {}

    class _Perc:
        def update_sensors(self, sid, sensors):  # noqa: ARG002
            captured.update(sensors)

    fake_ws = MagicMock()
    fake_ws._feral_capabilities = ["jw_health_glasses"]
    monkeypatch.setattr(srv.state, "perception", _Perc(), raising=False)
    monkeypatch.setattr(
        srv.state, "get_sessions_for_daemon", lambda n: ["s1"], raising=False,
    )
    monkeypatch.setattr(srv.state, "somatic_engine", None, raising=False)
    monkeypatch.setattr(srv.state, "baseline_engine", None, raising=False)
    monkeypatch.setattr(
        srv.state, "daemons", {"node-1": fake_ws}, raising=False,
    )
    srv._handle_biometric_device_event(
        "node-1",
        "heart_rate",
        {"bpm": 72, "source": "apple_healthkit"},
    )
    assert captured.get("heart_rate_source") == "apple_healthkit"


# ---------------------------------------------------------------------------
# Fix #2 — proactive lagging-source guard
# ---------------------------------------------------------------------------


def _engine_with_frame(frame: PerceptionFrame):
    perception = MagicMock()
    perception._frames = {"sess-1": frame}
    perception.get_frame = lambda sid: frame  # noqa: ARG005
    eng = ProactiveEngine()
    eng._perception = perception
    eng._first_interaction_today = False
    captured: list = []

    async def _capture(msg):
        captured.append(msg)

    eng._deliver = _capture  # type: ignore[assignment]
    eng._can_fire = lambda trigger_id: True  # type: ignore[assignment]  # noqa: ARG005
    eng._record_fire = lambda trigger_id: None  # type: ignore[assignment]  # noqa: ARG005
    return eng, captured


@pytest.mark.asyncio
async def test_lagging_source_alert_blocked_even_when_fresh():
    """Demo-day pin: the exact 2026-06-08 incident.  Even with the
    sample_ts looking 5s old, an ``apple_healthkit`` source must
    NOT fire ``hr_elevated`` (or run ``scene.calming``).  Only
    a live wearable can drive that automation."""
    frame = PerceptionFrame(
        heart_rate=115,
        heart_rate_sample_ts=time.time() - 5.0,
        heart_rate_source="apple_healthkit",
    )
    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    assert not [m for m in captured if m.trigger_id == "hr_elevated"]


@pytest.mark.asyncio
async def test_live_wearable_elevated_hr_still_fires():
    frame = PerceptionFrame(
        heart_rate=120,
        heart_rate_sample_ts=time.time() - 5.0,
        heart_rate_source="jw_health_glasses",
    )
    eng, captured = _engine_with_frame(frame)
    await eng._evaluate()
    fired = [m for m in captured if m.trigger_id == "hr_elevated"]
    assert fired
    assert "jw_health_glasses" in fired[0].body


# ---------------------------------------------------------------------------
# Fix #4 — wearable-vs-wearable priority
# ---------------------------------------------------------------------------


def test_wearable_priority_lookup_orders_correctly():
    """Lower number = higher priority.  W300 / glasses are the
    canonical demo source so they must rank above the wristband."""
    assert _wearable_priority("jw_health_glasses") < _wearable_priority(
        "veepoo_wristband"
    )
    assert _wearable_priority("theora_w300") == _wearable_priority(
        "jw_health_glasses"
    ), "legacy alias must tie with canonical name"
    assert _wearable_priority("veepoo_wristband") < _wearable_priority(
        "polar"
    )
    assert _wearable_priority("garmin_ble") < _wearable_priority(
        "w610_glasses"
    )
    # Unknown sources fall to the lowest tier.
    assert _wearable_priority("apple_healthkit") > _wearable_priority(
        "w610_glasses"
    )


def test_w300_wins_over_freshly_arrived_veepoo_within_window():
    """Operator report 2026-06-08: with W300 + Veepoo both streaming,
    the per-tick "freshest wins" tiebreak made the HR slot thrash.
    Now W300 holds the slot for the whole 120s fresh window even
    if Veepoo's last push is 1s newer."""
    perc = PerceptionEngine()
    sid = "demo-sess"
    now = time.time()
    perc.update_sensors(sid, {
        "ppg_heart_rate": 78,
        "heart_rate_source": "jw_health_glasses",
        "heart_rate_sample_ts": now - 5.0,
    })
    perc.update_sensors(sid, {
        "ppg_heart_rate": 64,
        "heart_rate_source": "veepoo_wristband",
        "heart_rate_sample_ts": now - 1.0,
    })
    frame = perc.get_frame(sid)
    assert frame.heart_rate_source == "jw_health_glasses", (
        "higher-priority W300 must NOT be demoted by a slightly-fresher "
        "lower-priority Veepoo within the fresh window"
    )
    assert frame.heart_rate == 78


def test_veepoo_wins_when_w300_goes_stale():
    """Lower-priority source DOES take over once the high-priority
    one goes stale (>120s old).  Otherwise we'd be stuck on a
    silent W300 indefinitely."""
    perc = PerceptionEngine()
    sid = "demo-sess"
    now = time.time()
    perc.update_sensors(sid, {
        "ppg_heart_rate": 78,
        "heart_rate_source": "jw_health_glasses",
        "heart_rate_sample_ts": now - 200.0,  # stale (>120s)
    })
    perc.update_sensors(sid, {
        "ppg_heart_rate": 64,
        "heart_rate_source": "veepoo_wristband",
        "heart_rate_sample_ts": now - 5.0,
    })
    frame = perc.get_frame(sid)
    assert frame.heart_rate_source == "veepoo_wristband"
    assert frame.heart_rate == 64


def test_same_priority_tier_keeps_freshest():
    """Two reads from the same priority tier (e.g. polar + wahoo)
    fall back to the legacy "freshest wins" tiebreak."""
    perc = PerceptionEngine()
    sid = "demo-sess"
    now = time.time()
    perc.update_sensors(sid, {
        "ppg_heart_rate": 80,
        "heart_rate_source": "polar",
        "heart_rate_sample_ts": now - 10.0,
    })
    perc.update_sensors(sid, {
        "ppg_heart_rate": 75,
        "heart_rate_source": "wahoo",
        "heart_rate_sample_ts": now - 2.0,  # fresher
    })
    frame = perc.get_frame(sid)
    assert frame.heart_rate_source == "wahoo"
    assert frame.heart_rate == 75


def test_lagging_source_cannot_demote_live_wearable():
    """The pre-existing rule (operator report 2026-06-05) still
    holds: a HealthKit push (lagging) can NOT clobber a fresh
    live wearable, regardless of priority."""
    perc = PerceptionEngine()
    sid = "demo-sess"
    now = time.time()
    perc.update_sensors(sid, {
        "ppg_heart_rate": 64,
        "heart_rate_source": "veepoo_wristband",
        "heart_rate_sample_ts": now - 5.0,
    })
    perc.update_sensors(sid, {
        "ppg_heart_rate": 115,
        "heart_rate_source": "apple_healthkit",
        "heart_rate_sample_ts": now - 1.0,
    })
    frame = perc.get_frame(sid)
    assert frame.heart_rate_source == "veepoo_wristband"
    assert frame.heart_rate == 64


# ---------------------------------------------------------------------------
# Fix #5 — per-source baseline namespacing
# ---------------------------------------------------------------------------


def test_per_source_baseline_namespacing(monkeypatch):
    """Recording a known live-wearable HR must train BOTH the bare
    ``hr_resting`` row (back-compat) AND the per-source row
    ``hr_resting:<source>``.  Two simultaneous wearables therefore
    keep independent means."""
    records: list[tuple[str, float]] = []

    class FakeBaseline:
        def record(self, metric_id, value, category=None):  # noqa: ARG002
            records.append((metric_id, value))

    monkeypatch.setattr(
        srv.state, "baseline_engine", FakeBaseline(), raising=False,
    )

    srv._record_biometrics_to_baseline({
        "ppg_heart_rate": 60,
        "heart_rate_source": "jw_health_glasses",
        "heart_rate_sample_ts": time.time(),
    })
    metric_ids = {mid for mid, _ in records}
    assert "hr_resting" in metric_ids, "bare row preserved for back-compat"
    assert "hr_resting:jw_health_glasses" in metric_ids, (
        "per-source row must be trained for known live wearables"
    )

    # A second source trains its own namespaced row, leaving the
    # first untouched.
    records.clear()
    srv._record_biometrics_to_baseline({
        "ppg_heart_rate": 72,
        "heart_rate_source": "veepoo_wristband",
        "heart_rate_sample_ts": time.time(),
    })
    metric_ids = {mid for mid, _ in records}
    assert "hr_resting:veepoo_wristband" in metric_ids
    assert "hr_resting:jw_health_glasses" not in metric_ids


def test_unknown_source_does_not_get_namespaced_row(monkeypatch):
    """Unknown / blank sources only train the bare row — we don't
    fragment the baseline space on noise."""
    records: list[tuple[str, float]] = []

    class FakeBaseline:
        def record(self, metric_id, value, category=None):  # noqa: ARG002
            records.append((metric_id, value))

    monkeypatch.setattr(
        srv.state, "baseline_engine", FakeBaseline(), raising=False,
    )
    srv._record_biometrics_to_baseline({
        "ppg_heart_rate": 70,
        "heart_rate_source": "",
        "heart_rate_sample_ts": time.time(),
    })
    metric_ids = {mid for mid, _ in records}
    assert metric_ids == {"hr_resting"}


def test_lagging_source_trains_neither_row(monkeypatch):
    """HealthKit reads remain blocked entirely — neither the bare
    nor the namespaced row should be touched."""
    records: list[tuple[str, float]] = []

    class FakeBaseline:
        def record(self, metric_id, value, category=None):  # noqa: ARG002
            records.append((metric_id, value))

    monkeypatch.setattr(
        srv.state, "baseline_engine", FakeBaseline(), raising=False,
    )
    srv._record_biometrics_to_baseline({
        "ppg_heart_rate": 115,
        "heart_rate_source": "apple_healthkit",
        "heart_rate_sample_ts": time.time(),
    })
    assert records == [], (
        "HealthKit / lagging sources must never train the baseline"
    )


# ---------------------------------------------------------------------------
# Fix #5 (proactive query path) — namespaced lookup with fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_hr_queries_namespaced_row_first():
    """The proactive ``baseline_hr`` trigger must consult the active
    source's namespaced baseline first.  The bare row is the
    fallback only when the per-source row hasn't trained enough
    samples yet."""
    fake_baseline = MagicMock()

    namespaced = MagicMock()
    namespaced.values = [60.0, 61.0, 62.0, 60.0, 61.0]

    def _get_baseline(metric_id):
        if metric_id == "hr_resting:jw_health_glasses":
            return namespaced
        return None

    fake_baseline.get_baseline.side_effect = _get_baseline
    fake_baseline.check_anomaly.return_value = MagicMock(
        message="HR anomaly via namespaced row",
    )
    fake_baseline.check_trend.return_value = None

    frame = PerceptionFrame(
        heart_rate=110,
        heart_rate_sample_ts=time.time() - 5.0,
        heart_rate_source="jw_health_glasses",
    )
    eng, captured = _engine_with_frame(frame)
    eng._baseline = fake_baseline
    await eng._evaluate()

    fake_baseline.check_anomaly.assert_any_call(
        "hr_resting:jw_health_glasses", 110,
    )
    assert any(m.trigger_id == "baseline_hr" for m in captured)


@pytest.mark.asyncio
async def test_baseline_hr_falls_back_to_bare_when_namespaced_empty():
    """If the per-source row hasn't trained ≥3 values yet, fall
    back to bare ``hr_resting`` so the anomaly check still runs
    on day one."""
    fake_baseline = MagicMock()

    bare = MagicMock()
    bare.values = [60.0, 61.0, 62.0, 60.0, 61.0]

    def _get_baseline(metric_id):
        if metric_id == "hr_resting":
            return bare
        return None

    fake_baseline.get_baseline.side_effect = _get_baseline
    fake_baseline.check_anomaly.return_value = MagicMock(
        message="HR anomaly via bare row",
    )
    fake_baseline.check_trend.return_value = None

    frame = PerceptionFrame(
        heart_rate=110,
        heart_rate_sample_ts=time.time() - 5.0,
        heart_rate_source="jw_health_glasses",
    )
    eng, captured = _engine_with_frame(frame)
    eng._baseline = fake_baseline
    await eng._evaluate()

    fake_baseline.check_anomaly.assert_any_call("hr_resting", 110)
    assert any(m.trigger_id == "baseline_hr" for m in captured)


@pytest.mark.asyncio
async def test_baseline_hr_skipped_for_lagging_source():
    """Operator report 2026-06-08: ``baseline_hr`` must not anomaly-
    check stale HealthKit reads either; reuses the same lagging-
    source guard as ``hr_elevated``."""
    fake_baseline = MagicMock()
    bare = MagicMock()
    bare.values = [60.0, 61.0, 62.0, 60.0, 61.0]
    fake_baseline.get_baseline.return_value = bare
    fake_baseline.check_anomaly.return_value = MagicMock(
        message="should not be reached",
    )
    fake_baseline.check_trend.return_value = None

    frame = PerceptionFrame(
        heart_rate=115,
        heart_rate_sample_ts=time.time() - 5.0,
        heart_rate_source="apple_healthkit",
    )
    eng, captured = _engine_with_frame(frame)
    eng._baseline = fake_baseline
    await eng._evaluate()

    assert not any(m.trigger_id == "baseline_hr" for m in captured)
    fake_baseline.check_anomaly.assert_not_called()


# ---------------------------------------------------------------------------
# Fix #6 — latest_health == current_hr (canonical snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latest_health_matches_health_summary_current_hr(monkeypatch):
    """``/api/dashboard.latest_health.heart_rate`` and
    ``/api/health/summary.current_hr`` MUST agree on the live bpm
    + source.  Before fix #6, latest_health iterated sessions
    "last wins" with no live-wearable filter, so the two
    endpoints could disagree.  Now they share
    ``BrainState._latest_live_wearable_snapshot``."""
    from api.routes.dashboard import _get_dashboard_data
    from integrations.health_platforms import HealthAggregator

    snapshot = {
        "heart_rate": 72,
        "heart_rate_source": "jw_health_glasses",
    }

    state = srv.state
    monkeypatch.setattr(
        state,
        "_latest_live_wearable_snapshot",
        lambda: snapshot,
        raising=False,
    )
    # Empty / no-op stand-ins so the dashboard payload assembles.
    monkeypatch.setattr(state, "daemons", {}, raising=False)
    monkeypatch.setattr(state, "device_pairing_store", None, raising=False)
    monkeypatch.setattr(state, "node_subdevices", None, raising=False)
    monkeypatch.setattr(state, "sessions", set(), raising=False)
    monkeypatch.setattr(state, "perception", MagicMock(), raising=False)
    monkeypatch.setattr(state, "channel_manager", None, raising=False)
    monkeypatch.setattr(state, "somatic_engine", None, raising=False)
    monkeypatch.setattr(state, "_demo", None, raising=False)
    monkeypatch.setattr(state, "_boot_report", MagicMock(to_dict=lambda: {}), raising=False)
    monkeypatch.setattr(state, "memory", MagicMock(stats=MagicMock(return_value={})), raising=False)
    fake_memory = MagicMock()

    async def _stats():
        return {}

    fake_memory.stats = _stats
    monkeypatch.setattr(state, "memory", fake_memory, raising=False)
    monkeypatch.setattr(state, "skill_registry", MagicMock(skills={}), raising=False)
    monkeypatch.setattr(state, "audio", MagicMock(available=False), raising=False)
    monkeypatch.setattr(state, "sync_engine", None, raising=False)
    monkeypatch.setattr(state, "wasm_sandbox", None, raising=False)
    monkeypatch.setattr(state, "wake_word", None, raising=False)
    monkeypatch.setattr(state, "taskflows", None, raising=False)
    monkeypatch.setattr(state, "devices", {}, raising=False)
    monkeypatch.setattr(state, "vault", None, raising=False)
    monkeypatch.setattr(state, "sandbox", None, raising=False)
    monkeypatch.setattr(state, "policy", None, raising=False)
    monkeypatch.setattr(state, "realtime_proxy", None, raising=False)
    monkeypatch.setattr(state, "scene", None, raising=False)
    monkeypatch.setattr(state, "change_detector", None, raising=False)
    monkeypatch.setattr(state, "oauth", None, raising=False)
    monkeypatch.setattr(state, "spotify", None, raising=False)
    monkeypatch.setattr(state, "home_assistant", None, raising=False)
    monkeypatch.setattr(state, "notion", None, raising=False)
    monkeypatch.setattr(state, "event_bus", None, raising=False)
    monkeypatch.setattr(state, "marketplace", None, raising=False)
    monkeypatch.setattr(state, "orchestrator", None, raising=False)

    data = await _get_dashboard_data()
    latest_health = data["health"]
    assert latest_health.get("heart_rate") == 72
    assert latest_health.get("heart_rate_source") == "jw_health_glasses"
    assert latest_health.get("heart_rate_fresh") is True

    aggregator = HealthAggregator(live_wearable_provider=lambda: snapshot)
    summary = await aggregator.execute("health_summary", {}, vault={})
    assert summary["data"]["current_hr"] == latest_health["heart_rate"]
    assert (
        summary["data"]["current_hr_source"]
        == latest_health["heart_rate_source"]
    )
