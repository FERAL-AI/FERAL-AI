"""Trend frame provenance must remain faithful to the durable sample rows."""
from unittest.mock import AsyncMock

import pytest

from integrations.health_canonical import build_reading
from integrations.health_platforms import HealthAggregator


@pytest.mark.asyncio
async def test_trend_separates_metric_source_unit_and_keeps_recorded_times():
    aggregator = HealthAggregator()
    rows = [
        build_reading("hr", 71.25, source="jw_health_glasses", ts=1000),
        build_reading("hr", 80.5, source="veepoo_wristband", ts=2000),
        build_reading("hr", 72.75, source="jw_health_glasses", ts=9000),
        build_reading("hr", 69.5, source="", ts=4000),
    ]
    aggregator.get_health_history = AsyncMock(return_value={
        "metrics": ["hr"], "series": {"hr": rows}, "source_ids": ["jw_health_glasses", "veepoo_wristband"], "window_days": 7,
    })
    aggregator.get_health_summary = AsyncMock(side_effect=AssertionError("must not query current sensors or cloud providers"))
    aggregator._maybe_sync_durable = AsyncMock(side_effect=AssertionError("offline trend must not sync vendors"))
    frame = await aggregator.build_health_update(event_type="vitals_trend", days=7)
    data = frame["payload"]["data"]
    assert data["source_grouping"] == "metric_source_unit"
    grouped = {s["source"]: s for s in data["series"]}
    assert set(grouped) == {"", "jw_health_glasses", "veepoo_wristband"}
    assert [p["ts"] for p in grouped["jw_health_glasses"]["points"]] == [1000, 9000]
    assert [p["value"] for p in grouped["jw_health_glasses"]["points"]] == [71.25, 72.75]
    assert all(s["unit"] == "bpm" for s in data["series"])
    assert all(p["source"] == s["source"] for s in data["series"] for p in s["points"])
    aggregator.get_health_history.assert_awaited_once_with(days=7, include_ids=True)
    aggregator.get_health_summary.assert_not_awaited()
    aggregator._maybe_sync_durable.assert_not_awaited()


@pytest.mark.asyncio
async def test_trend_rejects_invalid_or_mislabeled_rows_without_inventing_samples():
    aggregator = HealthAggregator()
    valid = build_reading("hr", 75, source="unknown-future-device", ts=1200)
    rows = [valid, {**valid, "ts": 0}, {**valid, "ts": float("nan")}, {**valid, "value": float("inf")}, {**valid, "unit": "wrong"}, {**valid, "value": True}]
    aggregator.get_health_history = AsyncMock(return_value={"metrics": ["hr"], "series": {"hr": rows}, "window_days": 30})
    result = await aggregator.build_health_update(event_type="vitals_trend", days=30)
    series = result["payload"]["data"]["series"]
    assert len(series) == 1
    assert series[0]["source"] == "unknown-future-device"
    assert series[0]["points"] == [{"ts": 1200.0, "value": 75.0, "source": "unknown-future-device"}]


@pytest.mark.asyncio
async def test_empty_history_has_provenance_contract_without_fake_zero_series():
    aggregator = HealthAggregator()
    aggregator.get_health_history = AsyncMock(return_value={"metrics": [], "series": {}, "window_days": 7})
    result = await aggregator.build_health_update(event_type="vitals_trend", days=7)
    assert result["payload"]["data"]["source_grouping"] == "metric_source_unit"
    assert result["payload"]["data"]["series"] == []


@pytest.mark.asyncio
async def test_typed_protocol_roundtrip_keeps_grouping_names_and_point_sources():
    from models.protocol import HealthUpdatePayload

    aggregator = HealthAggregator()
    row = build_reading("hr", 75, source="jw_health_glasses", ts=1200)
    aggregator.get_health_history = AsyncMock(return_value={"metrics": ["hr"], "series": {"hr": [row]}, "window_days": 7})
    frame = await aggregator.build_health_update(event_type="vitals_trend", days=7)
    restored = HealthUpdatePayload.model_validate(frame["payload"]).model_dump()
    assert restored["data"]["source_grouping"] == "metric_source_unit"
    assert restored["data"]["series"][0]["source_name"] == frame["payload"]["data"]["series"][0]["source_name"]
    assert restored["data"]["series"][0]["points"][0]["source"] == "jw_health_glasses"
    assert HealthUpdatePayload.model_validate({"event_type": "health_summary", "data": {}}).data.source_grouping is None
