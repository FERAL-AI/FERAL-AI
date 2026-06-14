"""Operator report 2026-06-13 — glasses biometric history + week-over-week trend.

Live W300 smart-glasses readings (``jw_health_glasses``: HR + SpO2,
also skin-temp) streamed into the brain and fed the live snapshot and
the rolling baseline, but were NOT persisted as a queryable historical
time-series. So "over the last week how were my vitals?" returned
"no data" for every trend because those only came from Whoop/Oura.

This module pins the fix:

* Glasses HR/SpO2 ``device_event`` frames are appended to the durable
  ``BaselineEngine.biometric_samples`` time-series with their real
  sample timestamp + source (purely additive — runs AFTER the baseline
  record, never perturbs the freshness / per-source / priority logic).
* ``HealthAggregator.get_health_summary`` derives a real 7-day vitals
  trend from the glasses samples when NO third-party wearable is
  connected, instead of returning the all-null "no data" snapshot.
* When a cloud source (Whoop/Oura) IS connected, the legacy behaviour
  is unchanged — the glasses fallback block is not attached and the
  cloud resting_hr stays authoritative.
* The new ``vitals_trend`` endpoint surfaces the per-day breakdown.
* Cloud / lagging mirrors (HealthKit) are kept OUT of the wearable
  time-series.
* Retention keeps the series bounded.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.server as srv  # noqa: E402
from agents.baseline_engine import BaselineEngine  # noqa: E402
from integrations.health_platforms import HealthAggregator  # noqa: E402

DAY = 86400.0


def _setup_state(monkeypatch, engine: BaselineEngine) -> None:
    """Wire the brain's module-level ``state`` so a single biometric
    ``device_event`` can be driven through the real dispatcher with a
    real (in-memory) BaselineEngine, the same way
    ``test_hr_pipeline_demo_fixes._drive`` fakes the perception sink."""

    class _Perc:
        def update_sensors(self, sid, sensors):  # noqa: ARG002
            pass

    monkeypatch.setattr(srv.state, "perception", _Perc(), raising=False)
    monkeypatch.setattr(
        srv.state, "get_sessions_for_daemon", lambda n: [], raising=False,
    )
    monkeypatch.setattr(srv.state, "somatic_engine", None, raising=False)
    monkeypatch.setattr(srv.state, "baseline_engine", engine, raising=False)
    monkeypatch.setattr(srv.state, "daemons", {}, raising=False)


def _emit_hr(node: str, bpm: float, source: str, ts: float) -> None:
    srv._handle_biometric_device_event(
        node, "heart_rate", {"bpm": bpm, "source": source, "ts": ts},
    )


def _emit_spo2(node: str, pct: float, source: str, ts: float) -> None:
    srv._handle_biometric_device_event(
        node, "spo2", {"current": pct, "source": source, "ts": ts},
    )


# ---------------------------------------------------------------------------
# Persistence + weekly trend from the glasses alone
# ---------------------------------------------------------------------------


def test_glasses_samples_persist_and_build_weekly_trend(monkeypatch):
    eng = BaselineEngine(db_path=":memory:")
    _setup_state(monkeypatch, eng)
    now = time.time()

    # Seven simulated days, two HR + one SpO2 reading per day.
    for d in range(7):
        base = now - d * DAY
        _emit_hr("node-1", 58 + d, "jw_health_glasses", base - 100)
        _emit_hr("node-1", 70 + d, "jw_health_glasses", base - 50)
        _emit_spo2("node-1", 97, "jw_health_glasses", base - 100)

    hr_samples = eng.get_samples("hr", since=now - 8 * DAY)
    assert len(hr_samples) == 14, "every glasses HR frame must be persisted"
    assert all(s["source"] == "jw_health_glasses" for s in hr_samples)

    trend = eng.get_trend("hr", days=7)
    assert trend["sample_count"] == 14
    assert "jw_health_glasses" in trend["sources"]
    assert trend["min"] is not None and trend["max"] is not None
    assert len(trend["daily"]) >= 6  # ~one bucket per simulated day

    spo2_trend = eng.get_trend("spo2", days=7)
    assert spo2_trend["sample_count"] == 7
    assert spo2_trend["avg"] == 97


@pytest.mark.asyncio
async def test_health_summary_falls_back_to_glasses_trend(monkeypatch):
    """No Whoop/Oura connected — the summary must derive a real vitals
    trend from the glasses instead of returning the all-null snapshot
    that produced the "no data" complaint."""
    eng = BaselineEngine(db_path=":memory:")
    _setup_state(monkeypatch, eng)
    now = time.time()
    for d in range(5):
        _emit_hr("node-1", 55 + d, "jw_health_glasses", now - d * DAY - 60)
        _emit_spo2("node-1", 98, "jw_health_glasses", now - d * DAY - 60)

    agg = HealthAggregator(biometric_history_provider=lambda: eng)
    data = await agg.get_health_summary()

    vt = data.get("vitals_trend")
    assert vt is not None, "glasses-derived trend must be attached"
    assert vt["hr_sample_count"] == 5
    assert vt["spo2_avg"] == 98
    assert vt["resting_hr_estimate"] is not None
    # The "no data" symptom is gone: the resting-HR slot is filled
    # from the glasses week.
    assert data["resting_hr"] == vt["resting_hr_estimate"]
    assert data["resting_hr"] is not None
    assert "jw_health_glasses" in data["sources"]
    assert "glasses" in vt["note"] or "jw_health_glasses" in vt["note"]


@pytest.mark.asyncio
async def test_third_party_source_path_unchanged(monkeypatch):
    """When Whoop IS connected, the cloud resting_hr stays
    authoritative and the glasses fallback block is NOT attached —
    legacy behaviour is preserved."""
    eng = BaselineEngine(db_path=":memory:")
    now = time.time()
    for d in range(3):
        eng.record_sample("hr", 60 + d, source="jw_health_glasses", ts=now - d * DAY)

    fake_whoop = MagicMock()
    fake_whoop.connected = True
    fake_whoop.get_recovery = AsyncMock(return_value={
        "success": True,
        "data": {"recovery_score": 70, "resting_hr": 52, "hrv_ms": 60},
    })
    fake_whoop.get_sleep = AsyncMock(return_value={"success": True, "data": []})
    fake_whoop.get_cycles = AsyncMock(return_value={"success": True, "data": []})

    agg = HealthAggregator(
        whoop=fake_whoop, biometric_history_provider=lambda: eng,
    )
    data = await agg.get_health_summary()

    assert data["resting_hr"] == 52  # whoop wins, not the glasses min
    assert data["recovery_score"] == 70
    assert "vitals_trend" not in data, (
        "cloud source present → no glasses fallback block attached"
    )


def test_lagging_source_not_persisted_to_history(monkeypatch):
    """A HealthKit (lagging/cloud) push must NOT enter the wearable
    time-series — it already has its own trend via the cloud branches
    and would pollute the glasses-derived series."""
    eng = BaselineEngine(db_path=":memory:")
    _setup_state(monkeypatch, eng)
    srv._handle_biometric_device_event(
        "node-1", "heart_rate", {"bpm": 115, "source": "apple_healthkit"},
    )
    assert eng.get_samples("hr") == []


def test_retention_prunes_old_samples():
    """The series stays bounded: samples older than the retention
    horizon are deleted."""
    eng = BaselineEngine(db_path=":memory:")
    now = time.time()
    eng.record_sample("hr", 60, source="jw_health_glasses", ts=now - 40 * DAY)
    eng.record_sample("hr", 61, source="jw_health_glasses", ts=now - 1 * DAY)

    removed = eng.prune_samples()  # default 35-day horizon
    assert removed == 1

    remaining = eng.get_samples("hr", since=now - 100 * DAY)
    assert len(remaining) == 1
    assert remaining[0]["value"] == 61


@pytest.mark.asyncio
async def test_vitals_trend_endpoint(monkeypatch):
    eng = BaselineEngine(db_path=":memory:")
    now = time.time()
    for d in range(7):
        eng.record_sample("hr", 56 + d, source="jw_health_glasses", ts=now - d * DAY - 30)
        eng.record_sample("spo2", 96, source="jw_health_glasses", ts=now - d * DAY - 30)

    agg = HealthAggregator(biometric_history_provider=lambda: eng)
    result = await agg.execute("vitals_trend", {"days": 7}, vault={})

    assert result["success"] is True
    data = result["data"]
    assert data["hr_sample_count"] == 7
    assert data["spo2_avg"] == 96
    assert len(data["resting_hr_trend"]) >= 6
    assert data["resting_hr_estimate"] is not None
    assert "jw_health_glasses" in data["note"]


@pytest.mark.asyncio
async def test_vitals_trend_empty_when_no_history():
    """No persisted history → an explicit empty shape so the LLM can be
    honest, not a fabricated trend."""
    eng = BaselineEngine(db_path=":memory:")
    agg = HealthAggregator(biometric_history_provider=lambda: eng)
    result = await agg.execute("vitals_trend", {}, vault={})

    data = result["data"]
    assert data["resting_hr_trend"] == []
    assert data["resting_hr_estimate"] is None
    assert data["hr_range"] is None
    assert "No persisted" in data["note"]


@pytest.mark.asyncio
async def test_no_provider_keeps_legacy_summary_shape():
    """No history provider wired → the summary never grows a
    vitals_trend block and keeps the legacy null shape."""
    agg = HealthAggregator()
    data = await agg.get_health_summary()
    assert "vitals_trend" not in data
    assert data["resting_hr"] is None
    assert data["sources"] == []
