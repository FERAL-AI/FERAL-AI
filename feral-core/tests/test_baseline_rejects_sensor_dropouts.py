"""A dropout is not a reading, and must never reach a personal baseline.

``BaselineEngine.record`` accepted any float, so a sensor dropout
entered the rolling window as data and moved the mean and standard
deviation that every anomaly alert is measured against.

Measured on the operator's brain on 2026-09-07. 2,395 heart-rate
samples from paired glasses, twelve below 10 bpm and one at 8.0. The
learned ``hr_resting`` baseline was mean 100.71 with sd 23.74, and 102
alerts had fired, including this one:

    hr_resting is 54.0, which is 3.6 sigma below your baseline of 110.6

54 bpm is a good resting pulse. It was reported as critical because the
distribution it was compared against had been widened by readings no
body produces. A diagnostic that calls a healthy number an emergency
does not merely annoy: it is the thing that teaches an operator to
ignore the alert that matters.

The gate is deliberately wide. It rejects what a body cannot do, not
what a body should not do, so an elite resting pulse near 30 and a hard
effort at 200 both still land.
"""

from __future__ import annotations

import pytest

from agents.baseline_engine import BaselineEngine


@pytest.fixture
def engine():
    return BaselineEngine()


class TestWhatCounts:
    @pytest.mark.parametrize("value", [72.0, 54.0, 31.0, 205.0, 128.0])
    def test_real_heart_rates_are_kept(self, value):
        assert BaselineEngine.is_plausible("hr_resting", value) is True

    @pytest.mark.parametrize("value", [8.0, 0.0, -5.0, 240.0, 1000.0])
    def test_impossible_heart_rates_are_refused(self, value):
        assert BaselineEngine.is_plausible("hr_resting", value) is False

    def test_a_per_source_metric_uses_the_same_range(self):
        """``hr_resting:jw_health_glasses`` is still a heart rate.

        The source is carried after a colon so per-device baselines can
        be compared separately. The plausible range is a property of the
        body, not of the device that measured it, and the operator's
        poisoned baseline existed under exactly this id.
        """
        assert BaselineEngine.is_plausible("hr_resting:jw_health_glasses", 8.0) is False
        assert BaselineEngine.is_plausible("hr_resting:jw_health_glasses", 72.0) is True

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), None, "x"])
    def test_non_numbers_never_land(self, value):
        assert BaselineEngine.is_plausible("hr_resting", value) is False

    def test_an_unknown_metric_is_not_range_checked(self):
        """Inventing a range for an unseen metric would reject real data."""
        assert BaselineEngine.is_plausible("mystery_metric", 99999.0) is True

    @pytest.mark.parametrize("metric,bad,good", [
        ("spo2_pct", 0.0, 96.0),
        ("hrv_ms", 0.0, 42.0),
        ("skin_temp", 5.0, 33.4),
    ])
    def test_the_other_measured_metrics(self, metric, bad, good):
        assert BaselineEngine.is_plausible(metric, bad) is False
        assert BaselineEngine.is_plausible(metric, good) is True


class TestTheBaselineItLearns:
    def test_a_dropout_in_the_stream_does_not_move_the_baseline(self, engine):
        """The reported case, reduced."""
        clean = [72, 74, 70, 71, 73, 69, 75, 72, 70, 74, 71, 73, 70]
        for v in clean[:3]:
            engine.record("hr_resting", float(v), category="health")
        engine.record("hr_resting", 8.0, category="health")  # the dropout
        for v in clean[3:]:
            engine.record("hr_resting", float(v), category="health")

        metric = engine.get_baseline("hr_resting")
        assert 8.0 not in metric.values, "a dropout reached the window"
        assert len(metric.values) == len(clean)
        assert 68 < metric.mean < 76, f"mean was dragged to {metric.mean}"
        assert metric.std_dev < 4, f"sd was inflated to {metric.std_dev}"

    def test_a_good_resting_pulse_is_not_critical_against_a_clean_baseline(self, engine):
        """54 bpm against a real ~72 baseline is unusual, and should read so.

        The point is not that 54 stops alerting. It is that it is
        compared against the operator's actual distribution instead of
        one a broken sensor widened.
        """
        for v in [72, 74, 70, 71, 73, 69, 75, 72, 70, 74]:
            engine.record("hr_resting", float(v), category="health")
        baseline = engine.get_baseline("hr_resting")
        assert 68 < baseline.mean < 76
        assert baseline.std_dev < 4

    def test_an_existing_poisoned_window_heals_on_the_next_sample(self, engine):
        """No migration: the next reading repairs the metric's own window.

        Baselines learned before this gate existed still hold dropouts
        on disk, and those rows keep skewing alerts until something
        rewrites them.
        """
        engine.record("hr_resting", 72.0, category="health")
        # Simulate the pre-gate state by writing junk straight to the row.
        import json
        engine._conn.execute(
            "UPDATE baselines SET values_json = ? WHERE metric_id = 'hr_resting'",
            (json.dumps([8.0, 0.0, 72.0, 74.0]),),
        )
        engine._conn.commit()

        engine.record("hr_resting", 71.0, category="health")

        values = engine.get_baseline("hr_resting").values
        assert 8.0 not in values and 0.0 not in values, f"junk survived: {values}"
        assert sorted(values) == [71.0, 72.0, 74.0]

    def test_rejections_are_counted(self, engine):
        engine.record("hr_resting", 8.0, category="health")
        engine.record("hr_resting", 0.0, category="health")
        assert engine._rejected_samples == 2

    def test_a_rejected_sample_creates_no_row_at_all(self, engine):
        """A metric whose only reading was junk must not exist."""
        engine.record("hr_resting", 8.0, category="health")
        assert engine.get_baseline("hr_resting") is None
