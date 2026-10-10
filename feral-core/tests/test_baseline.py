"""Tests for the Baseline Learning Engine."""

import pytest

from agents.baseline_engine import BaselineEngine


@pytest.fixture
def engine(tmp_path):
    db = str(tmp_path / "baseline_test.db")
    return BaselineEngine(db_path=db)


class TestRecord:
    def test_record_creates_metric(self, engine: BaselineEngine):
        engine.record("hr_resting", 72.0, category="health")
        metric = engine.get_baseline("hr_resting")
        assert metric is not None
        assert metric.metric_id == "hr_resting"
        assert metric.category == "health"
        assert metric.values == [72.0]
        assert metric.mean == 72.0

    def test_record_rolling_window(self, engine: BaselineEngine):
        for v in range(20):
            engine.record("steps", float(v), category="activity", window_size=10)
        metric = engine.get_baseline("steps")
        assert len(metric.values) == 10
        assert metric.values[0] == 10.0
        assert metric.values[-1] == 19.0

    def test_record_updates_stats(self, engine: BaselineEngine):
        for v in [70.0, 72.0, 68.0, 74.0, 71.0]:
            engine.record("hr_resting", v, category="health")
        metric = engine.get_baseline("hr_resting")
        assert abs(metric.mean - 71.0) < 0.01
        assert metric.std_dev > 0


class TestAnomaly:
    def _seed(self, engine: BaselineEngine, metric_id="hr_resting", values=None):
        if values is None:
            values = [70, 71, 72, 69, 70, 71, 72, 70, 71, 69]
        for v in values:
            engine.record(metric_id, float(v), category="health")

    def test_no_anomaly_within_range(self, engine: BaselineEngine):
        self._seed(engine)
        alert = engine.check_anomaly("hr_resting", 71.0)
        assert alert is None

    def test_anomaly_above(self, engine: BaselineEngine):
        self._seed(engine)
        alert = engine.check_anomaly("hr_resting", 90.0)
        assert alert is not None
        assert alert.alert_type == "anomaly"
        assert alert.severity in ("warning", "critical")
        assert alert.deviation_sigma >= 2.0
        assert "above" in alert.message

    def test_anomaly_below(self, engine: BaselineEngine):
        self._seed(engine)
        alert = engine.check_anomaly("hr_resting", 50.0)
        assert alert is not None
        assert "below" in alert.message

    def test_anomaly_persisted(self, engine: BaselineEngine):
        self._seed(engine)
        engine.check_anomaly("hr_resting", 90.0)
        alerts = engine.get_alerts(since=0)
        assert len(alerts) >= 1
        assert alerts[0].metric_id == "hr_resting"

    def test_no_anomaly_insufficient_data(self, engine: BaselineEngine):
        engine.record("new_metric", 10.0)
        engine.record("new_metric", 11.0)
        alert = engine.check_anomaly("new_metric", 100.0)
        assert alert is None

    def test_critical_severity_at_3sigma(self, engine: BaselineEngine):
        self._seed(engine)
        metric = engine.get_baseline("hr_resting")
        # Measured against the metric's noise floor, not the window's own
        # spread: this seed is tight (~1 bpm), and a deviation smaller
        # than a heart-rate sensor's own noise is not a signal.
        floor = BaselineEngine._effective_sigma("hr_resting", metric.std_dev)
        extreme = metric.mean + floor * 4
        alert = engine.check_anomaly("hr_resting", extreme)
        assert alert is not None
        assert alert.severity == "critical"

    def test_a_tight_window_cannot_manufacture_an_impossible_sigma(
        self, engine: BaselineEngine,
    ):
        """The bug this floor exists for.

        A continuously-sampled vital can fill the whole 14-value window
        from one steady minute. The operator's install did exactly that
        while walking: mean 103.5, std_dev 1.12. A normal 64 bpm then
        scored 35 sigma and persisted a CRITICAL alert, which the health
        page rendered.
        """
        self._seed(engine, values=[105, 105, 105, 104, 104, 102, 102,
                                   102, 102, 103, 103, 104, 104, 104])
        alert = engine.check_anomaly("hr_resting", 64.0)
        # It may still alert -- 64 really is far from 103 -- but the
        # number attached to it has to be physically meaningful.
        if alert is not None:
            assert alert.deviation_sigma < 10.0, (
                f"{alert.deviation_sigma:.1f}σ is not a real deviation"
            )


class TestTrend:
    def test_upward_trend(self, engine: BaselineEngine):
        for v in [60, 61, 62, 63, 64, 65, 66]:
            engine.record("sleep_score", float(v), category="health")
        alert = engine.check_trend("sleep_score", window=5)
        assert alert is not None
        assert alert.alert_type == "trend"
        assert "upward" in alert.message

    def test_downward_trend(self, engine: BaselineEngine):
        for v in [8.0, 7.5, 7.0, 6.5, 6.0, 5.5, 5.0]:
            engine.record("sleep_hours", float(v), category="health")
        alert = engine.check_trend("sleep_hours", window=5)
        assert alert is not None
        assert "downward" in alert.message

    def test_no_trend_when_mixed(self, engine: BaselineEngine):
        for v in [70, 72, 68, 74, 69, 73, 71]:
            engine.record("mixed", float(v))
        alert = engine.check_trend("mixed", window=5)
        assert alert is None

    def test_no_trend_insufficient_data(self, engine: BaselineEngine):
        engine.record("short", 1.0)
        engine.record("short", 2.0)
        alert = engine.check_trend("short", window=5)
        assert alert is None


class TestSummaryAndGetAll:
    def test_summary(self, engine: BaselineEngine):
        engine.record("hr_resting", 72, category="health")
        engine.record("steps", 8000, category="activity")
        s = engine.summary()
        assert s["metrics_tracked"] == 2
        assert "health" in s["categories"]
        assert "activity" in s["categories"]

    def test_get_all_baselines(self, engine: BaselineEngine):
        engine.record("a", 1.0, category="x")
        engine.record("b", 2.0, category="y")
        all_metrics = engine.get_all_baselines()
        assert len(all_metrics) == 2
        ids = {m.metric_id for m in all_metrics}
        assert ids == {"a", "b"}

    def test_get_alerts_empty(self, engine: BaselineEngine):
        assert engine.get_alerts(since=0) == []
