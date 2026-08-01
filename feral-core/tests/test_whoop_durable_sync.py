"""Whoop durability + the brain-to-node health frame.

The defect
==========
Whoop was implemented and wired: an OAuth provider carrying the
``offline`` scope so a refresh token is actually issued
(``integrations/oauth_manager.py``), an API client hitting ``/recovery``,
``/activity/sleep``, ``/activity/workout`` and ``/cycle``
(``integrations/health_platforms.py``), construction in
``api/state.py``, and reachability through ``HealthAggregator`` and
``GET /api/health-summary``.

None of it was persisted. ``get_recovery`` / ``get_sleep`` /
``get_cycles`` were called on demand inside ``get_health_summary`` and
returned as a transient dict. No Whoop row existed in any table, so a
Whoop account connected for a year could still only be asked about the
last seven days, and only while the network was up.

Separately, health data had no way to reach the Theora iOS app as data.
That app's HUP parser decodes exactly ``node_ack``,
``hup_action_request``, ``error``, ``node_bye``, ``chat_response``,
``text_response``, ``transcript``, ``audio_response`` and
``voice_status``. There was no health frame at all, so Whoop numbers
could only arrive as English prose inside a chat reply, which an app
cannot render as a card or a chart.

What this pins
==============
* Whoop records land in the durable ``biometric_samples`` store.
* Syncing twice does not duplicate rows.
* Whoop never writes into the instantaneous ``hr`` / ``spo2`` /
  ``skin_temp`` series, so the glasses-derived ``vitals_trend`` stays
  uncorrupted.
* A cloud mirror survives past the 35-day prune while the live BLE
  series is still pruned at exactly 35 days.
* Six months of recovery is answerable from storage alone.
* The ``health_update`` frame decodes through ``parse_message`` and
  carries the exact documented payload.
* Source ids are carried verbatim, never relabelled.

No network
==========
Nothing here calls the real Whoop API. Every fetch is driven through
``httpx.MockTransport`` returning synthetic bodies shaped like the real
v1 responses ``health_platforms.WhoopClient`` parses, so the vendor
field names (``score.recovery_score``, ``total_in_bed_time_milli``, ...)
are exercised end to end rather than stubbed past.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.baseline_engine import BaselineEngine  # noqa: E402
from integrations import health_canonical as hc  # noqa: E402
from integrations.health_platforms import HealthAggregator, WhoopClient  # noqa: E402
from integrations.health_sync import (  # noqa: E402
    WhoopDurableSync,
    day_timestamp,
    parse_whoop_timestamp,
    readings_from_whoop_recovery,
)

DAY = 86400.0


# ─────────────────────────────────────────────
# Synthetic Whoop v1 bodies (shaped like the real API)
# ─────────────────────────────────────────────

def _recovery_body(created_at: str = "2026-07-30T06:12:44.235Z") -> dict:
    """Shape of ``GET /recovery`` per the Whoop v1 docs."""
    return {
        "records": [
            {
                "cycle_id": 93845,
                "sleep_id": "ecfc6a15-4661-442f-a9a4-f160dd7aff32",
                "user_id": 10129,
                "created_at": created_at,
                "updated_at": created_at,
                "score_state": "SCORED",
                "score": {
                    "user_calibrating": False,
                    "recovery_score": 66,
                    "resting_heart_rate": 54,
                    "hrv_rmssd_milli": 78.5,
                    "spo2_percentage": 97.2,
                    "skin_temp_celsius": 33.7,
                },
            }
        ],
        "next_token": None,
    }


def _sleep_body(days: int = 3) -> dict:
    """Shape of ``GET /activity/sleep``."""
    records = []
    for offset in range(days):
        stamp = time.strftime(
            "%Y-%m-%dT08:00:00.000Z", time.gmtime(time.time() - offset * DAY)
        )
        records.append({
            "id": 93845 + offset,
            "user_id": 10129,
            "created_at": stamp,
            "score_state": "SCORED",
            "score": {
                "stage_summary": {},
                "total_in_bed_time_milli": 28_800_000,       # 8.0 h
                "total_rem_sleep_time_milli": 6_120_000,     # 1.7 h
                "total_slow_wave_sleep_time_milli": 5_040_000,  # 1.4 h
                "sleep_efficiency_percentage": 91,
                "sleep_performance_percentage": 88,
                "respiratory_rate": 14.8,
            },
        })
    return {"records": records, "next_token": None}


def _cycle_body(days: int = 3) -> dict:
    """Shape of ``GET /cycle``."""
    records = []
    for offset in range(days):
        stamp = time.strftime(
            "%Y-%m-%dT04:00:00.000Z", time.gmtime(time.time() - offset * DAY)
        )
        records.append({
            "id": 93845 + offset,
            "user_id": 10129,
            "created_at": stamp,
            "score_state": "SCORED",
            "score": {
                "strain": 12.4 + offset,
                "kilojoule": 8_900.0,
                "average_heart_rate": 71,
                "max_heart_rate": 158,
            },
        })
    return {"records": records, "next_token": None}


def _whoop_client(
    recovery: dict | None = None,
    sleep: dict | None = None,
    cycles: dict | None = None,
    calls: list | None = None,
) -> WhoopClient:
    """A real ``WhoopClient`` whose transport is a mock, so the vendor
    JSON parsing in ``health_platforms.py`` is genuinely exercised and
    no request ever leaves the process."""
    bodies = {
        "/recovery": recovery if recovery is not None else _recovery_body(),
        "/activity/sleep": sleep if sleep is not None else _sleep_body(),
        "/cycle": cycles if cycles is not None else _cycle_body(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        for suffix, body in bodies.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"records": []})

    client = WhoopClient()
    client._token = "synthetic-access-token"
    client._http = httpx.AsyncClient(
        base_url="https://api.prod.whoop.com/developer/v1",
        transport=httpx.MockTransport(handler),
    )
    return client


def _sync(engine: BaselineEngine, whoop: WhoopClient, **kwargs) -> WhoopDurableSync:
    return WhoopDurableSync(
        whoop=whoop, store_provider=lambda: engine, **kwargs,
    )


@pytest.fixture
def engine():
    return BaselineEngine(db_path=":memory:")


@pytest.fixture(autouse=True)
def _no_probe_cache(monkeypatch):
    """``WhoopClient.connected`` consults the probe cache. Force the
    token-presence fallback so these tests never depend on whatever a
    previous probe left behind."""
    from integrations import _probe_status

    _probe_status.clear()
    yield
    _probe_status.clear()


# ─────────────────────────────────────────────
# 1. Whoop actually becomes durable
# ─────────────────────────────────────────────

async def test_sync_persists_whoop_recovery_into_biometric_samples(engine):
    """The core defect. Pre-fix nothing wrote a Whoop row anywhere, so
    every one of these lookups returned an empty list."""
    result = await _sync(engine, _whoop_client()).sync_once()

    assert result["synced"] is True
    assert result["written"] > 0

    recovery = engine.get_samples("recovery_score")
    assert len(recovery) == 1
    assert recovery[0]["value"] == 66
    assert recovery[0]["source"] == "whoop"

    assert engine.get_samples("resting_hr")[0]["value"] == 54
    assert engine.get_samples("hrv")[0]["value"] == 78.5
    assert engine.get_samples("spo2_avg")[0]["value"] == 97.2
    assert engine.get_samples("skin_temp_avg")[0]["value"] == 33.7


async def test_sync_persists_sleep_and_cycle_records(engine):
    await _sync(engine, _whoop_client()).sync_once()

    sleep_hours = engine.get_samples("sleep_hours")
    assert len(sleep_hours) == 3
    assert sleep_hours[0]["value"] == 8.0
    assert engine.get_samples("sleep_score")[0]["value"] == 88
    assert engine.get_samples("sleep_efficiency")[0]["value"] == 91
    assert engine.get_samples("rem_hours")[0]["value"] == 1.7
    assert engine.get_samples("deep_hours")[0]["value"] == 1.4
    assert engine.get_samples("respiratory_rate")[0]["value"] == 14.8

    strain = engine.get_samples("strain")
    assert len(strain) == 3
    assert engine.get_samples("calories_kj")[0]["value"] == 8900


async def test_sync_is_idempotent(engine):
    """Whoop revises recent records, so every sync re-reads a rolling
    window. Re-reading must not duplicate rows or the store grows
    without bound and every average is wrong."""
    whoop = _whoop_client()
    first = await _sync(engine, whoop).sync_once()
    before = len(engine.get_samples("sleep_hours"))

    second = await _sync(engine, whoop).sync_once()

    assert first["written"] > 0
    assert second["written"] == 0
    assert second["duplicates"] == first["written"]
    assert len(engine.get_samples("sleep_hours")) == before


async def test_sync_reports_not_connected_without_a_token(engine):
    """No credentials means an honest no-op, not a crash and not a
    fabricated empty record."""
    whoop = _whoop_client()
    whoop._token = ""
    result = await _sync(engine, whoop).sync_once()

    assert result["synced"] is False
    assert result["reason"] == "not_connected"
    assert engine.get_samples("recovery_score") == []


async def test_zero_scores_are_not_persisted_as_measurements(engine):
    """``WhoopClient`` defaults absent score fields to 0. A stored zero
    would read back as a real measurement of zero recovery, so absent
    must stay absent."""
    body = _recovery_body()
    body["records"][0]["score"] = {
        "recovery_score": 0,
        "resting_heart_rate": 0,
        "hrv_rmssd_milli": 0,
    }
    await _sync(engine, _whoop_client(recovery=body)).sync_once()

    assert engine.get_samples("recovery_score") == []
    assert engine.get_samples("resting_hr") == []
    assert engine.get_samples("hrv") == []


# ─────────────────────────────────────────────
# 2. No corruption of the live sensor series
# ─────────────────────────────────────────────

async def test_whoop_never_writes_the_instantaneous_sensor_metrics(engine):
    """Whoop's ``resting_heart_rate`` is one derived value a day; the
    glasses' ``hr`` is a PPG sample many times a minute. Sharing a
    metric name would corrupt every min/max/avg the vitals trend
    computes."""
    await _sync(engine, _whoop_client()).sync_once()

    assert engine.get_samples("hr") == []
    assert engine.get_samples("spo2") == []
    assert engine.get_samples("skin_temp") == []


async def test_glasses_vitals_trend_is_unaffected_by_a_whoop_sync(engine):
    """The regression this guards: a Whoop sync must not move the
    glasses-derived resting-HR estimate by a single beat."""
    now = time.time()
    for offset in range(7):
        engine.record_sample(
            "hr", 56 + offset, source="jw_health_glasses", ts=now - offset * DAY - 30,
        )
    aggregator = HealthAggregator(biometric_history_provider=lambda: engine)
    before = await aggregator.get_vitals_trend(days=7)

    await _sync(engine, _whoop_client()).sync_once()
    after = await aggregator.get_vitals_trend(days=7)

    assert before["resting_hr_estimate"] == after["resting_hr_estimate"]
    assert before["hr_sample_count"] == after["hr_sample_count"]
    assert after["sources"] == ["jw_health_glasses"]


async def test_cloud_source_cannot_write_an_instantaneous_metric():
    """The guard is in the vocabulary, not only in the mapping table, so
    a future vendor mapping typo cannot reintroduce the corruption."""
    assert hc.canonical_metric_for_source("hr", "whoop") is None
    assert hc.canonical_metric_for_source("spo2", "oura") is None
    assert hc.canonical_metric_for_source("resting_hr", "whoop") == "resting_hr"
    # A live wearable is still free to write the instantaneous series.
    assert hc.canonical_metric_for_source("hr", "jw_health_glasses") == "hr"


# ─────────────────────────────────────────────
# 3. Retention: 35 days does not fit a recovery score
# ─────────────────────────────────────────────

def test_cloud_samples_survive_the_35_day_prune(engine):
    """``biometric_samples`` is pruned at 35 days, which is right for a
    1 Hz sensor and would delete the entire reason to mirror Whoop."""
    now = time.time()
    engine.record_sample("recovery_score", 66, source="whoop", ts=now - 200 * DAY)
    engine.record_sample("hr", 60, source="jw_health_glasses", ts=now - 200 * DAY)

    engine.prune_samples()

    assert len(engine.get_samples("recovery_score", since=now - 400 * DAY)) == 1
    assert engine.get_samples("hr", since=now - 400 * DAY) == []


def test_live_sensor_retention_is_unchanged_at_exactly_35_days(engine):
    """The existing window must not move for existing data."""
    now = time.time()
    engine.record_sample("hr", 60, source="jw_health_glasses", ts=now - 36 * DAY)
    engine.record_sample("hr", 61, source="jw_health_glasses", ts=now - 34 * DAY)

    removed = engine.prune_samples()

    assert removed == 1
    remaining = engine.get_samples("hr", since=now - 100 * DAY)
    assert [r["value"] for r in remaining] == [61]


def test_explicit_retention_argument_still_applies_to_every_row(engine):
    """Back-compat: a caller passing an explicit horizon gets the old
    global sweep, cloud rows included."""
    now = time.time()
    engine.record_sample("recovery_score", 66, source="whoop", ts=now - 40 * DAY)
    engine.record_sample("hr", 60, source="jw_health_glasses", ts=now - 40 * DAY)

    assert engine.prune_samples(retention_days=10) == 2


def test_cloud_retention_is_env_overridable(engine, monkeypatch):
    monkeypatch.setenv("FERAL_HEALTH_CLOUD_RETENTION_DAYS", "90")
    assert engine.cloud_retention_days() == 90.0

    now = time.time()
    engine.record_sample("recovery_score", 66, source="whoop", ts=now - 120 * DAY)
    engine.record_sample("recovery_score", 70, source="whoop", ts=now - 60 * DAY)
    engine.prune_samples()

    assert len(engine.get_samples("recovery_score", since=now - 400 * DAY)) == 1


def test_cloud_retention_never_falls_below_the_live_window(engine, monkeypatch):
    """A hostile or fat-fingered override must not silently shorten the
    cloud mirror below the live series it is supposed to outlive."""
    monkeypatch.setenv("FERAL_HEALTH_CLOUD_RETENTION_DAYS", "1")
    assert engine.cloud_retention_days() == BaselineEngine.SAMPLE_RETENTION_DAYS

    monkeypatch.setenv("FERAL_HEALTH_CLOUD_RETENTION_DAYS", "not-a-number")
    assert engine.cloud_retention_days() == BaselineEngine.CLOUD_SAMPLE_RETENTION_DAYS


# ─────────────────────────────────────────────
# 4. Six months of recovery is answerable
# ─────────────────────────────────────────────

async def test_health_history_answers_a_six_month_question(engine):
    """The question the whole change exists for. Pre-fix this had no
    data source at all: the vendor trend endpoints only query the last N
    days and nothing was stored."""
    now = time.time()
    for week in range(26):
        engine.record_sample(
            "recovery_score", 60 + (week % 11), source="whoop",
            ts=now - week * 7 * DAY,
        )

    aggregator = HealthAggregator(biometric_history_provider=lambda: engine)
    history = await aggregator.get_health_history(days=180, metric="recovery_score")

    assert history["window_days"] == 180
    assert history["sources"] == ["whoop"]
    assert history["sample_count"] >= 25
    entries = history["series"]["recovery_score"]
    assert entries[0]["ts"] < entries[-1]["ts"]
    assert entries[0]["unit"] == "%"
    assert entries[0]["metric"] == "recovery_score"


async def test_health_history_is_honest_when_empty(engine):
    aggregator = HealthAggregator(biometric_history_provider=lambda: engine)
    history = await aggregator.get_health_history(days=180)

    assert history["sample_count"] == 0
    assert history["series"] == {}
    assert "No durable health readings" in history["note"]


async def test_health_history_is_reachable_through_the_skill_dispatch(engine):
    """Chat reaches health data only through ``execute``. An endpoint the
    dispatch does not know is dead regardless of the manifest."""
    now = time.time()
    engine.record_sample("recovery_score", 66, source="whoop", ts=now - 100 * DAY)

    aggregator = HealthAggregator(biometric_history_provider=lambda: engine)
    result = await aggregator.execute("health_history", {"days": 180}, vault={})

    assert result["success"] is True
    assert result["data"]["sample_count"] == 1
    assert "recovery_score" in result["data"]["metrics"]


async def test_health_history_reads_storage_not_the_vendor_api(engine):
    """It must answer with Whoop unreachable, which is most of the point
    of persisting in the first place."""
    calls: list[str] = []
    whoop = _whoop_client(calls=calls)
    await _sync(engine, whoop).sync_once()
    fetched = len(calls)

    aggregator = HealthAggregator(
        whoop=whoop, biometric_history_provider=lambda: engine,
    )
    history = await aggregator.get_health_history(days=180)

    assert history["sample_count"] > 0
    assert len(calls) == fetched, "health_history must not hit the vendor API"


# ─────────────────────────────────────────────
# 5. The health_update frame
# ─────────────────────────────────────────────

def test_health_update_is_a_registered_message_type():
    """Pre-fix there was no health frame at all in MESSAGE_TYPES, so the
    only way health data reached the app was English prose in a chat
    reply."""
    from models.protocol import MESSAGE_TYPES, HealthUpdatePayload

    assert MESSAGE_TYPES["health_update"] is HealthUpdatePayload


def test_health_update_frame_decodes_through_parse_message():
    from models.protocol import HealthUpdatePayload, parse_message

    frame = hc.build_health_update_frame(
        event_type=hc.HEALTH_EVENT_SUMMARY,
        readings=[hc.build_reading("recovery_score", 66, source="whoop", ts=1.0)],
        node_id="feral-iphone-1",
    )
    msg, payload = parse_message({
        "type": frame["type"], "payload": frame["payload"],
    })

    assert msg.type == "health_update"
    assert isinstance(payload, HealthUpdatePayload)
    assert payload.node_id == "feral-iphone-1"
    assert payload.event_type == "health_summary"
    assert payload.data.readings[0].metric == "recovery_score"
    assert payload.data.readings[0].unit == "%"


def test_health_update_envelope_mirrors_device_event():
    """The brief was to reuse the ``device_event`` convention rather than
    invent a vocabulary. ``device_event.payload`` is
    ``{node_id, event_type, data, ts}`` (HUP_SPEC 5.4); this must be the
    same four keys."""
    frame = hc.build_health_update_frame()

    assert set(frame) == {"hup_version", "type", "ts", "payload"}
    assert set(frame["payload"]) == {"node_id", "event_type", "data", "ts"}


def test_health_update_rejects_an_unknown_event_type():
    frame = hc.build_health_update_frame(event_type="something_else")
    assert frame["payload"]["event_type"] == "health_summary"


def test_reading_shape_is_the_durable_row_plus_render_metadata():
    """The canonical reading must be the ``biometric_samples`` row
    (ts, source, metric, value) with render fields that are pure
    functions of ``metric``. Anything else would be a sixth shape."""
    reading = hc.build_reading("resting_hr", 54.4, source="whoop", ts=1754006400.0)

    assert reading == {
        "metric": "resting_hr",
        "label": "Resting Heart Rate",
        # Stored at the source's own precision. ``precision`` below is a
        # DISPLAY hint; rounding on write would lose the value forever.
        "value": 54.4,
        "unit": "bpm",
        "precision": 0,
        "category": "vitals",
        "source": "whoop",
        "ts": 1754006400.0,
    }
    spec = hc.metric_spec("resting_hr")
    assert (reading["unit"], reading["label"], reading["category"]) == (
        spec.unit, spec.label, spec.category,
    )


def test_reading_refuses_unknown_metrics_and_non_numerics():
    assert hc.build_reading("made_up_metric", 5) is None
    assert hc.build_reading("resting_hr", None) is None
    assert hc.build_reading("resting_hr", "fifty-four") is None
    assert hc.build_reading("resting_hr", True) is None


async def test_summary_frame_carries_readings_the_app_can_render(engine):
    aggregator = HealthAggregator()
    whoop = MagicMock()
    whoop.connected = True
    whoop.get_recovery = AsyncMock(return_value={
        "success": True,
        "data": {"recovery_score": 66, "resting_hr": 54, "hrv_ms": 78},
    })
    whoop.get_sleep = AsyncMock(return_value={
        "success": True, "data": [{"total_sleep_hours": 8.0, "sleep_score": 88}],
    })
    whoop.get_cycles = AsyncMock(return_value={
        "success": True, "data": [{"strain": 12.4}],
    })
    aggregator._whoop = whoop

    frame = await aggregator.build_health_update(node_id="feral-iphone-1")
    readings = {r["metric"]: r for r in frame["payload"]["data"]["readings"]}

    assert frame["payload"]["data"]["sources"] == ["whoop"]
    assert readings["recovery_score"]["value"] == 66
    assert readings["recovery_score"]["unit"] == "%"
    assert readings["resting_hr"]["unit"] == "bpm"
    assert readings["sleep_hours"]["unit"] == "h"
    assert readings["hrv"]["unit"] == "ms"
    assert all(r["source"] == "whoop" for r in readings.values())


async def test_trend_frame_carries_dated_series(engine):
    now = time.time()
    for offset in range(10):
        engine.record_sample(
            "recovery_score", 60 + offset, source="whoop", ts=now - offset * DAY,
        )

    aggregator = HealthAggregator(biometric_history_provider=lambda: engine)
    frame = await aggregator.build_health_update(
        event_type=hc.HEALTH_EVENT_TREND, days=30,
    )
    payload = frame["payload"]

    assert payload["event_type"] == "vitals_trend"
    assert payload["data"]["window_days"] == 30
    series = {s["metric"]: s for s in payload["data"]["series"]}
    assert series["recovery_score"]["unit"] == "%"
    assert len(series["recovery_score"]["points"]) == 10
    assert series["recovery_score"]["points"][0]["ts"] > 0


def test_frame_carries_source_ids_verbatim():
    """The same glasses device is ``glasses`` to Theora and
    ``jw_health_glasses`` to FERAL, with nothing mapping them. This wire
    must not add a third spelling: whatever produced the sample is what
    the frame says."""
    frame = hc.build_health_update_frame(readings=[
        hc.build_reading("hr", 58, source="jw_health_glasses", ts=1.0),
        hc.build_reading("recovery_score", 66, source="whoop", ts=1.0),
    ])

    assert frame["payload"]["data"]["sources"] == ["jw_health_glasses", "whoop"]
    sources = {r["source"] for r in frame["payload"]["data"]["readings"]}
    assert sources == {"jw_health_glasses", "whoop"}


# ─────────────────────────────────────────────
# 6. Scheduling + timestamp handling
# ─────────────────────────────────────────────

async def test_health_summary_warms_the_durable_mirror(engine):
    """The sync also runs on the paths the user exercises (the chat
    tool, the manifest's 07:00 cron, ``GET /api/health-summary``), not
    only on the background loop."""
    whoop = _whoop_client()
    service = _sync(engine, whoop)
    aggregator = HealthAggregator(
        whoop=whoop,
        biometric_history_provider=lambda: engine,
        sync_provider=lambda: service,
    )

    assert engine.get_samples("recovery_score") == []
    await aggregator.get_health_summary()
    assert len(engine.get_samples("recovery_score")) == 1


async def test_maybe_sync_is_throttled(engine):
    clock = {"now": 1_000_000.0}
    service = _sync(
        engine, _whoop_client(), interval_s=900.0, clock=lambda: clock["now"],
    )

    first = await service.maybe_sync()
    clock["now"] += 60.0
    second = await service.maybe_sync()
    clock["now"] += 1000.0
    third = await service.maybe_sync()

    assert first["synced"] is True
    assert second["reason"] == "throttled"
    assert third["synced"] is True


async def test_a_broken_sync_never_breaks_the_summary(engine):
    """A mirror failure must not fail the question the user asked."""
    broken = MagicMock()
    broken.maybe_sync = AsyncMock(side_effect=RuntimeError("whoop is down"))
    aggregator = HealthAggregator(sync_provider=lambda: broken)

    summary = await aggregator.get_health_summary()

    assert summary["sources"] == []
    assert summary["resting_hr"] is None


async def test_sync_survives_a_partial_vendor_failure(engine):
    """A 401 on sleep must not lose an already-fetched recovery record."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recovery"):
            return httpx.Response(200, json=_recovery_body())
        return httpx.Response(401, json={"error": "unauthorized"})

    whoop = WhoopClient()
    whoop._token = "synthetic-access-token"
    whoop._http = httpx.AsyncClient(
        base_url="https://api.prod.whoop.com/developer/v1",
        transport=httpx.MockTransport(handler),
    )

    result = await _sync(engine, whoop).sync_once()

    assert result["written"] > 0
    assert engine.get_samples("recovery_score")[0]["value"] == 66
    assert engine.get_samples("sleep_hours") == []


def test_whoop_timestamp_parsing_handles_millisecond_zulu():
    """Whoop stamps UTC with a trailing ``Z`` and milliseconds, which
    ``datetime.fromisoformat`` rejects before Python 3.11."""
    parsed = parse_whoop_timestamp("2026-07-30T06:12:44.235Z")
    assert parsed is not None
    assert abs(parsed - 1785391964.235) < 0.001
    assert parse_whoop_timestamp("") is None
    assert parse_whoop_timestamp("not a date") is None


def test_day_timestamp_lands_in_its_own_local_date_bucket():
    """``get_daily_stats`` buckets by local date. A midnight-UTC anchor
    would file a record labelled 2026-07-30 under 2026-07-29 for every
    negative-offset timezone."""
    from datetime import datetime as dt

    ts = day_timestamp("2026-07-30")
    assert ts is not None
    assert dt.fromtimestamp(ts).strftime("%Y-%m-%d") == "2026-07-30"
    assert day_timestamp("") is None
    assert day_timestamp("30/07/2026") is None


def test_recovery_readings_carry_the_records_own_timestamp():
    """Not the sync's wall clock: a backfilled record must land on the
    day it happened."""
    readings = readings_from_whoop_recovery({
        "recovery_score": 66,
        "resting_hr": 54,
        "created_at": "2026-07-30T06:12:44.235Z",
    })
    expected = parse_whoop_timestamp("2026-07-30T06:12:44.235Z")

    assert readings
    assert all(r["ts"] == expected for r in readings)


# ─────────────────────────────────────────────
# 7. The route
# ─────────────────────────────────────────────

@pytest.fixture
def app_with_dashboard(monkeypatch, engine):
    from fastapi import FastAPI

    whoop = _whoop_client()
    service = _sync(engine, whoop)
    aggregator = HealthAggregator(
        whoop=whoop,
        biometric_history_provider=lambda: engine,
        sync_provider=lambda: service,
    )

    sent: list[dict] = []

    class _FakeWS:
        async def send_json(self, payload):
            sent.append(payload)

    mock_state = MagicMock()
    mock_state.health_aggregator = aggregator
    mock_state.daemons = {"feral-iphone-1": _FakeWS()}

    monkeypatch.setattr("api.state.state", mock_state)
    from api.routes import dashboard as dashboard_module
    monkeypatch.setattr(dashboard_module, "state", mock_state)

    app = FastAPI()
    app.include_router(dashboard_module.router)
    return app, sent, engine


def test_health_frame_route_returns_and_pushes_the_frame(app_with_dashboard):
    from fastapi.testclient import TestClient

    app, sent, _engine = app_with_dashboard
    response = TestClient(app, raise_server_exceptions=False).get("/api/health/frame")

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "health_update"
    assert body["payload"]["event_type"] == "health_summary"

    assert len(sent) == 1
    assert sent[0]["type"] == "health_update"
    assert sent[0]["payload"]["node_id"] == "feral-iphone-1"


def test_health_frame_route_rejects_an_unknown_event_type(app_with_dashboard):
    from fastapi.testclient import TestClient

    app, _sent, _engine = app_with_dashboard
    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/health/frame?event_type=nonsense"
    )

    assert response.status_code == 400


def test_health_frame_route_can_skip_the_broadcast(app_with_dashboard):
    from fastapi.testclient import TestClient

    app, sent, _engine = app_with_dashboard
    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/health/frame?push=0"
    )

    assert response.status_code == 200
    assert sent == []


# ─────────────────────────────────────────────
# 8. House style
# ─────────────────────────────────────────────

def test_new_health_modules_contain_no_em_dashes():
    """Checked as raw bytes, as the escaped literal, and after a JSON
    round-trip, because all three have slipped through before."""
    targets = [
        ROOT / "integrations" / "health_canonical.py",
        ROOT / "integrations" / "health_sync.py",
        ROOT / "skills" / "manifests" / "health_data.json",
    ]
    for path in targets:
        raw = path.read_bytes()
        assert "—".encode() not in raw, f"em dash in {path.name}"
        assert b"\\u2014" not in raw, f"escaped em dash in {path.name}"

    manifest = ROOT / "skills" / "manifests" / "health_data.json"
    round_tripped = json.dumps(json.loads(manifest.read_text()))
    assert "—" not in round_tripped
