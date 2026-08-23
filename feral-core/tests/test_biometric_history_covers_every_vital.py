"""Every vital the brain ingests must also reach the durable history.

`_HISTORY_METRIC_MAP` in `api/server.py` decides which sensor readings
are written to `BaselineEngine.biometric_samples`, which is what
answers "how was my HRV last week". It is a hand-maintained dict, and
`_EXTRACTABLE_EVENT_TYPES` is a separate hand-maintained tuple.

They drifted. `hrv` was added to the event vocabulary and to the
somatic bridge in 2026.8.23 and never added here, so an HRV reading
moved the behavioural policy in the moment and left no trace behind it.
That is the same writer-reader gap that dropped `skin_temperature` and
`steps` on their way into the somatic vector, one layer further out,
and it is invisible: nothing errors, the reading simply is not there
when somebody asks for a trend.

This test exists so the next vital added to the ingest path cannot
quietly skip the history.
"""
from __future__ import annotations

import time

import pytest

import api.server as srv


#: Event types that produce a point-in-time vital worth trending.
#:
#: Deliberately NOT every extractable type. `accelerometer`,
#: `gyroscope`, `gps`, `gesture`, `button_press`, `ambient_light`,
#: `battery` and `activity` are either vectors, device state, or
#: interactions, none of which belong in a biometric time series. If a
#: new type here needs trending, add it to `_HISTORY_METRIC_MAP` and
#: this list together.
TRENDABLE_EVENTS = {
    "heart_rate": ("bpm", 70, "hr"),
    "hrv": ("rmssd_ms", 42, "hrv"),
    "spo2": ("current", 97, "spo2"),
    "skin_temperature": ("celsius", 33.4, "skin_temp"),
    "steps": ("count", 4200, "steps"),
}


class _RecordingEngine:
    def __init__(self):
        self.samples = []

    def record_sample(self, metric, value, source="", ts=0.0):
        self.samples.append((metric, float(value), source))


@pytest.fixture()
def brain(monkeypatch, tmp_path):
    from api.state import state

    engine = _RecordingEngine()
    monkeypatch.setattr(state, "baseline_engine", engine, raising=False)
    monkeypatch.setitem(state._daemon_session_bindings, "glasses-1", {"s1"})
    return engine


@pytest.mark.parametrize(
    "event_type, payload_key, value, metric",
    [(e, k, v, m) for e, (k, v, m) in TRENDABLE_EVENTS.items()],
)
def test_every_trendable_vital_reaches_the_history(
    brain, event_type, payload_key, value, metric,
):
    srv._handle_biometric_device_event("glasses-1", event_type, {
        payload_key: value,
        "source": "jw_health_glasses",
        "ts": time.time(),
    })
    recorded = {m for m, _, _ in brain.samples}
    assert metric in recorded, (
        f"{event_type} was ingested but never written to the biometric "
        f"history; add it to _HISTORY_METRIC_MAP. Recorded: {recorded}"
    )


def test_hrv_specifically(brain):
    """Regression pin. This is the one that was missing."""
    srv._handle_biometric_device_event("glasses-1", "hrv", {
        "rmssd_ms": 42, "source": "jw_health_glasses", "ts": time.time(),
    })
    assert ("hrv", 42.0, "jw_health_glasses") in brain.samples


def test_a_lagging_source_is_still_excluded(brain):
    """The history is the wearable-derived trend. A cloud mirror
    re-stamps stale reads and would pollute it, which is why the
    exclusion exists; adding hrv must not have widened the door.
    """
    srv._handle_biometric_device_event("glasses-1", "hrv", {
        "rmssd_ms": 42, "source": "apple_healthkit", "ts": time.time(),
    })
    assert not any(m == "hrv" for m, _, _ in brain.samples)


def test_the_map_and_the_vocabulary_have_not_drifted_again():
    """Both lists are hand-maintained. This is the check that they agree
    about the vitals, which is the drift that caused the bug."""
    for event_type in TRENDABLE_EVENTS:
        assert event_type in srv._EXTRACTABLE_EVENT_TYPES, (
            f"{event_type} is trendable but the dispatcher will not accept it"
        )
    mapped_metrics = {m for m, _, _ in srv._HISTORY_METRIC_MAP.values()}
    for _event, (_k, _v, metric) in TRENDABLE_EVENTS.items():
        assert metric in mapped_metrics, (
            f"{metric} has no _HISTORY_METRIC_MAP entry"
        )
