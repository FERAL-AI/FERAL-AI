"""
FERAL Canonical Health Reading
================================
FERAL had five different in-tree shapes for "a health reading" and no
canonical one:

1. ``models.protocol.BiometricPayload`` (``heart_rate_bpm``, ``spo2_pct``,
   ``temperature_c`` ...) on the client-to-brain envelope.
2. HUP ``device_event.data`` (``{"bpm": 72}`` / ``{"current": 97}``),
   keyed off ``event_type`` (HUP_SPEC 5.4).
3. ``BaselineEngine.biometric_samples`` rows: ``(ts, source, metric,
   value)``.
4. ``HealthAggregator.get_health_summary()``'s flat snapshot dict
   (``sleep_hours``, ``resting_hr``, ``hrv``, ``strain`` ...).
5. ``POST /api/health/ingest`` HealthKit samples (``{event_type, bpm,
   source, sample_source, sampled_at_ms}``).

Plus each vendor client's own per-endpoint dict in
``health_platforms.py``. This module does NOT add a sixth. It promotes
shape 3, the durable ``(ts, source, metric, value)`` long form, to the
canonical reading, because it is the only one of the five that is
already persisted, already source-tagged, already timestamped per
sample, and already queryable over an arbitrary window. The other four
are all lossy projections of it.

A canonical reading is that row plus render metadata resolved from the
metric vocabulary below (``unit``, ``label``, ``precision``,
``category``). Nothing new is stored: ``unit`` and friends are pure
functions of ``metric``, so the DB row and the wire frame stay the same
fact.

Source labelling
----------------
``source`` is carried verbatim from whatever produced the sample. The
W300 glasses are ``jw_health_glasses`` end to end (the iOS client's
``FeralHUP.glassesSource`` and the brain's ``_HISTORY_METRIC_MAP``
writers agree), cloud wearables are ``whoop`` / ``oura``. This module
deliberately does not translate source ids: a rename here would be a
sixth vocabulary. See ``docs/handoff/THEORA_IOS_TASKS.md`` for the
open ``glasses`` vs ``jw_health_glasses`` labelling question.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

# Wire constants for the brain-to-node health frame. Kept next to the
# reading model so the frame can never drift away from the shape it
# carries.
HEALTH_UPDATE_TYPE = "health_update"
HEALTH_EVENT_SUMMARY = "health_summary"
HEALTH_EVENT_TREND = "vitals_trend"
HEALTH_EVENT_TYPES = (HEALTH_EVENT_SUMMARY, HEALTH_EVENT_TREND)

#: Canonical source id for the Whoop cloud API.
SOURCE_WHOOP = "whoop"
#: Canonical source id for the Oura cloud API.
SOURCE_OURA = "oura"
#: Cloud wearable sources. Their samples are daily aggregates (a handful
#: of rows per day), not a streaming sensor, so they get a much longer
#: retention horizon than the live BLE series. See
#: ``BaselineEngine.prune_samples``.
CLOUD_SOURCES = (SOURCE_WHOOP, SOURCE_OURA)


class MetricSpec:
    """Render metadata for one canonical metric name.

    ``kind`` separates the two physically different families that share
    this table:

    * ``instant`` -- a raw sensor sample (BLE PPG heart rate, SpO2 spot
      read). Many per minute, aggregated by the caller.
    * ``daily`` -- a once-per-day derived score from a cloud wearable
      (Whoop recovery, sleep efficiency). One row per day.

    Mixing them under one metric name would corrupt every min/max/avg
    the vitals trend computes, which is why the daily family gets its
    own names (``resting_hr`` not ``hr``, ``spo2_avg`` not ``spo2``).
    """

    __slots__ = ("key", "label", "unit", "precision", "category", "kind")

    def __init__(
        self,
        key: str,
        label: str,
        unit: str,
        precision: int,
        category: str,
        kind: str,
    ):
        self.key = key
        self.label = label
        self.unit = unit
        self.precision = precision
        self.category = category
        self.kind = kind


def _spec(key, label, unit, precision, category, kind) -> MetricSpec:
    return MetricSpec(key, label, unit, precision, category, kind)


#: The canonical metric vocabulary. Every writer into
#: ``biometric_samples`` and every reader out of it uses these names.
CANONICAL_METRICS: dict[str, MetricSpec] = {
    # ---- instantaneous sensor samples (live BLE wearables) ----------
    "hr": _spec("hr", "Heart Rate", "bpm", 0, "vitals", "instant"),
    "spo2": _spec("spo2", "Blood Oxygen", "%", 0, "vitals", "instant"),
    "skin_temp": _spec(
        "skin_temp", "Skin Temperature", "°C", 1, "vitals", "instant",
    ),
    "body_temp": _spec(
        "body_temp", "Body Temperature", "°C", 1, "vitals", "instant",
    ),
    # ---- daily derived scores (cloud wearables) ---------------------
    "resting_hr": _spec(
        "resting_hr", "Resting Heart Rate", "bpm", 0, "vitals", "daily",
    ),
    "hrv": _spec("hrv", "HRV", "ms", 0, "vitals", "daily"),
    "spo2_avg": _spec(
        "spo2_avg", "Blood Oxygen (avg)", "%", 1, "vitals", "daily",
    ),
    "skin_temp_avg": _spec(
        "skin_temp_avg", "Skin Temperature (avg)", "°C", 1,
        "vitals", "daily",
    ),
    "recovery_score": _spec(
        "recovery_score", "Recovery", "%", 0, "recovery", "daily",
    ),
    "readiness": _spec("readiness", "Readiness", "%", 0, "recovery", "daily"),
    "sleep_hours": _spec("sleep_hours", "Sleep", "h", 2, "sleep", "daily"),
    "sleep_score": _spec(
        "sleep_score", "Sleep Performance", "%", 0, "sleep", "daily",
    ),
    "sleep_efficiency": _spec(
        "sleep_efficiency", "Sleep Efficiency", "%", 0, "sleep", "daily",
    ),
    "rem_hours": _spec("rem_hours", "REM Sleep", "h", 2, "sleep", "daily"),
    "deep_hours": _spec("deep_hours", "Deep Sleep", "h", 2, "sleep", "daily"),
    "respiratory_rate": _spec(
        "respiratory_rate", "Respiratory Rate", "rpm", 1, "sleep", "daily",
    ),
    "strain": _spec("strain", "Strain", "", 1, "activity", "daily"),
    "activity_score": _spec(
        "activity_score", "Activity", "%", 0, "activity", "daily",
    ),
    "avg_hr": _spec("avg_hr", "Average Heart Rate", "bpm", 0, "activity", "daily"),
    "max_hr": _spec("max_hr", "Max Heart Rate", "bpm", 0, "activity", "daily"),
    "calories_kj": _spec("calories_kj", "Energy", "kJ", 0, "activity", "daily"),
    "steps": _spec("steps", "Steps", "steps", 0, "activity", "daily"),
}

#: Metric names a cloud wearable is allowed to write. Enforced by
#: ``canonical_metric_for_source`` so a vendor mapping typo can never
#: drop daily aggregates into the instantaneous sensor series and
#: silently corrupt ``vitals_trend``'s min/max/avg.
CLOUD_WRITABLE_METRICS = frozenset(
    k for k, spec in CANONICAL_METRICS.items() if spec.kind == "daily"
)


def metric_spec(metric: str) -> Optional[MetricSpec]:
    """Return the spec for *metric*, or ``None`` when it is not part of
    the canonical vocabulary. Callers must not invent metric names."""
    return CANONICAL_METRICS.get(str(metric or "").strip())


def canonical_metric_for_source(metric: str, source: str) -> Optional[str]:
    """Validate that *source* may write *metric*.

    Returns the metric name when the write is allowed, ``None`` when it
    is not. Cloud sources are restricted to the ``daily`` family.
    """
    spec = metric_spec(metric)
    if spec is None:
        return None
    if str(source or "").strip().lower() in CLOUD_SOURCES:
        return spec.key if spec.key in CLOUD_WRITABLE_METRICS else None
    return spec.key


def build_reading(
    metric: str,
    value: Any,
    source: str = "",
    ts: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Build one canonical reading.

    This is the single reading shape used by the durable store, the
    ``health_update`` frame, and the health history skill endpoint::

        {"metric": "resting_hr", "label": "Resting Heart Rate",
         "value": 54.0, "unit": "bpm", "precision": 0,
         "category": "vitals", "source": "whoop", "ts": 1754006400.0}

    Returns ``None`` for an unknown metric or a non-numeric value so a
    bad vendor field can never become a fabricated reading.
    """
    spec = metric_spec(metric)
    if spec is None:
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return {
        "metric": spec.key,
        "label": spec.label,
        "value": round(numeric, spec.precision) if spec.precision else round(numeric),
        "unit": spec.unit,
        "precision": spec.precision,
        "category": spec.category,
        "source": str(source or ""),
        "ts": float(ts) if isinstance(ts, (int, float)) and ts else time.time(),
    }


def build_series(
    metric: str,
    points: Iterable[dict[str, Any]],
    source: str = "",
) -> Optional[dict[str, Any]]:
    """Build one renderable series for the ``vitals_trend`` frame.

    *points* is an iterable of ``{"ts": float, "value": float}`` (an
    optional ``"date"`` is passed through when present). The series
    carries the same render metadata as a reading so a chart and a stat
    tile label the same metric identically.
    """
    spec = metric_spec(metric)
    if spec is None:
        return None
    out_points: list[dict[str, Any]] = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        raw_value = point.get("value")
        if raw_value is None or isinstance(raw_value, bool):
            continue
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            continue
        entry: dict[str, Any] = {
            "ts": float(point.get("ts") or 0.0),
            "value": (
                round(numeric, spec.precision) if spec.precision else round(numeric)
            ),
        }
        if point.get("date"):
            entry["date"] = str(point["date"])
        out_points.append(entry)
    if not out_points:
        return None
    return {
        "metric": spec.key,
        "label": spec.label,
        "unit": spec.unit,
        "precision": spec.precision,
        "category": spec.category,
        "source": str(source or ""),
        "points": out_points,
    }


def build_health_update_frame(
    event_type: str = HEALTH_EVENT_SUMMARY,
    readings: Optional[list[dict[str, Any]]] = None,
    series: Optional[list[dict[str, Any]]] = None,
    sources: Optional[list[str]] = None,
    node_id: str = "",
    window_days: int = 0,
    note: str = "",
    ts: Optional[float] = None,
) -> dict[str, Any]:
    """Build a complete brain-to-node ``health_update`` HUP frame.

    The envelope mirrors the daemon-to-brain ``device_event`` (HUP_SPEC
    5.4) exactly: ``{node_id, event_type, data, ts}`` inside the standard
    ``{hup_version, type, ts, payload}`` frame. No new vocabulary, just
    the same convention running the other direction.
    """
    from models.protocol import HUP_VERSION

    stamp = float(ts) if isinstance(ts, (int, float)) and ts else time.time()
    evt = str(event_type or HEALTH_EVENT_SUMMARY)
    if evt not in HEALTH_EVENT_TYPES:
        evt = HEALTH_EVENT_SUMMARY
    clean_readings = [r for r in (readings or []) if isinstance(r, dict)]
    clean_series = [s for s in (series or []) if isinstance(s, dict)]
    if sources is None:
        collected = {r.get("source") for r in clean_readings if r.get("source")}
        collected |= {s.get("source") for s in clean_series if s.get("source")}
        resolved_sources = sorted(str(s) for s in collected)
    else:
        resolved_sources = [str(s) for s in sources if s]
    return {
        "hup_version": HUP_VERSION,
        "type": HEALTH_UPDATE_TYPE,
        "ts": stamp,
        "payload": {
            "node_id": str(node_id or ""),
            "event_type": evt,
            "ts": stamp,
            "data": {
                "sources": resolved_sources,
                "window_days": int(window_days or 0),
                "note": str(note or ""),
                "readings": clean_readings,
                "series": clean_series,
            },
        },
    }
