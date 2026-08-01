"""
FERAL Baseline Learning Engine v1
====================================
Analyzes user patterns over time and detects anomalies / drift from
normal behaviour.  Every recorded metric maintains a rolling window of
recent values; the engine computes running mean and standard-deviation
and can flag:

  * **anomaly**  — single value deviates > N sigma from the baseline
  * **trend**    — sustained upward/downward movement over a window
  * **milestone** — user hit a personal record or round-number goal

All baselines are persisted in a lightweight SQLite store so they
survive restarts.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

logger = logging.getLogger("feral.baseline")


@dataclass
class BaselineMetric:
    metric_id: str
    category: str = "general"
    values: list[float] = field(default_factory=list)
    window_size: int = 14
    mean: float = 0.0
    std_dev: float = 0.0
    last_updated: float = 0.0


@dataclass
class BaselineAlert:
    metric_id: str
    alert_type: Literal["anomaly", "trend", "milestone"]
    severity: Literal["info", "warning", "critical"]
    message: str
    current_value: float
    baseline_mean: float
    deviation_sigma: float
    timestamp: float = field(default_factory=time.time)


class BaselineEngine:
    """SQLite-backed baseline learning engine.

    Records metric observations, maintains rolling statistics, and
    exposes anomaly / trend detection for the proactive engine.
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS baselines (
        metric_id   TEXT PRIMARY KEY,
        category    TEXT NOT NULL DEFAULT 'general',
        values_json TEXT NOT NULL DEFAULT '[]',
        mean        REAL NOT NULL DEFAULT 0,
        std_dev     REAL NOT NULL DEFAULT 0,
        window_size INTEGER NOT NULL DEFAULT 14,
        updated_at  REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS baseline_alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_id   TEXT NOT NULL,
        alert_type  TEXT NOT NULL,
        severity    TEXT NOT NULL,
        message     TEXT NOT NULL,
        current_val REAL NOT NULL,
        baseline_mean REAL NOT NULL,
        deviation_sigma REAL NOT NULL,
        ts          REAL NOT NULL
    );
    -- Durable biometric time-series (operator report 2026-06-13).
    -- The rolling ``baselines`` table above keeps only the last
    -- ``window_size`` (14) values with no per-sample timestamp, so it
    -- can answer "is THIS reading anomalous" but NOT "over the last
    -- week how were my vitals". Live-wearable samples (W300 glasses,
    -- Veepoo wristband, any BLE PPG) are appended here verbatim —
    -- (ts, source, metric, value) — so the health summary can build
    -- real week-over-week trends from the glasses ALONE, with no
    -- third-party wearable connected. Bounded by ``prune_samples``.
    CREATE TABLE IF NOT EXISTS biometric_samples (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      REAL NOT NULL,
        source  TEXT NOT NULL DEFAULT '',
        metric  TEXT NOT NULL,
        value   REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_biosamples_metric_ts
        ON biometric_samples(metric, ts);
    CREATE INDEX IF NOT EXISTS idx_biosamples_ts
        ON biometric_samples(ts);
    """

    # Default retention for the raw biometric time-series. 35 days
    # comfortably covers the week-over-week ("last 7 days", "vs last
    # week") questions while keeping the table bounded. Mirrors the
    # retention philosophy of memory/decay.py:MemoryDecayService.
    SAMPLE_RETENTION_DAYS: float = 35.0

    # Cloud-wearable mirrors (Whoop, Oura) are a different workload from
    # a live BLE sensor and need a different horizon. A streaming PPG
    # source writes roughly one row a second, so 35 days is what keeps
    # the table bounded; a cloud wearable writes about ten rows a day of
    # daily-aggregate scores, so the same 35 days would throw away the
    # entire reason to persist it ("my recovery over six months") while
    # saving a few kilobytes. These sources therefore get their own,
    # longer horizon.
    #
    # This does NOT change retention for any existing data: rows whose
    # ``source`` is not in ``CLOUD_SAMPLE_SOURCES`` are still deleted at
    # exactly ``SAMPLE_RETENTION_DAYS``, and an explicit
    # ``prune_samples(retention_days=N)`` still applies N to every row
    # as before.
    CLOUD_SAMPLE_SOURCES: tuple[str, ...] = ("whoop", "oura")
    CLOUD_SAMPLE_RETENTION_DAYS: float = 400.0

    # Run the retention sweep at most once every N inserts so a busy
    # streaming wearable (≈1 sample/sec) doesn't issue a DELETE on
    # every frame. The sweep is also exposed via ``prune_samples`` so
    # callers / tests can force it.
    _SAMPLE_PRUNE_EVERY_N = 256

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = ":memory:"
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._DDL)
        self._alert_listeners: list = []
        self._sample_writes = 0

    def on_alert(self, callback) -> None:
        """Register a listener that fires every time an alert is persisted.

        Listeners receive the :class:`BaselineAlert` instance. Failures in
        individual callbacks are swallowed so one broken listener can't
        suppress subsequent ones or corrupt anomaly detection.
        """
        self._alert_listeners.append(callback)

    def record(
        self,
        metric_id: str,
        value: float,
        category: str = "general",
        window_size: int = 14,
    ) -> None:
        """Add a data point and recompute rolling statistics."""
        row = self._conn.execute(
            "SELECT values_json, window_size FROM baselines WHERE metric_id = ?",
            (metric_id,),
        ).fetchone()

        if row:
            values: list[float] = json.loads(row["values_json"])
            ws = row["window_size"]
        else:
            values = []
            ws = window_size

        values.append(value)
        if len(values) > ws:
            values = values[-ws:]

        mean = statistics.mean(values) if values else 0.0
        std = statistics.pstdev(values) if len(values) >= 2 else 0.0

        self._conn.execute(
            """INSERT INTO baselines (metric_id, category, values_json, mean, std_dev, window_size, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(metric_id) DO UPDATE SET
                   category    = excluded.category,
                   values_json = excluded.values_json,
                   mean        = excluded.mean,
                   std_dev     = excluded.std_dev,
                   updated_at  = excluded.updated_at
            """,
            (metric_id, category, json.dumps(values), mean, std, ws, time.time()),
        )
        self._conn.commit()

    def get_baseline(self, metric_id: str) -> Optional[BaselineMetric]:
        row = self._conn.execute(
            "SELECT * FROM baselines WHERE metric_id = ?", (metric_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_metric(row)

    def get_all_baselines(self) -> list[BaselineMetric]:
        rows = self._conn.execute("SELECT * FROM baselines ORDER BY updated_at DESC").fetchall()
        return [self._row_to_metric(r) for r in rows]

    def check_anomaly(
        self,
        metric_id: str,
        current_value: float,
        threshold_sigma: float = 2.0,
    ) -> Optional[BaselineAlert]:
        """Return an alert if *current_value* deviates beyond *threshold_sigma*."""
        metric = self.get_baseline(metric_id)
        if metric is None or len(metric.values) < 3 or metric.std_dev == 0:
            return None

        deviation = abs(current_value - metric.mean) / metric.std_dev
        if deviation < threshold_sigma:
            return None

        if deviation >= 3.0:
            severity: Literal["info", "warning", "critical"] = "critical"
        elif deviation >= 2.0:
            severity = "warning"
        else:
            severity = "info"

        direction = "above" if current_value > metric.mean else "below"
        alert = BaselineAlert(
            metric_id=metric_id,
            alert_type="anomaly",
            severity=severity,
            message=(
                f"{metric_id} is {current_value:.1f}, which is {deviation:.1f}σ "
                f"{direction} your baseline of {metric.mean:.1f}"
            ),
            current_value=current_value,
            baseline_mean=metric.mean,
            deviation_sigma=deviation,
        )
        self._persist_alert(alert)
        return alert

    def check_trend(
        self, metric_id: str, window: int = 7
    ) -> Optional[BaselineAlert]:
        """Detect a consistent upward or downward trend over the last *window* values."""
        metric = self.get_baseline(metric_id)
        if metric is None or len(metric.values) < window:
            return None

        tail = metric.values[-window:]
        diffs = [tail[i + 1] - tail[i] for i in range(len(tail) - 1)]

        if all(d > 0 for d in diffs):
            direction = "upward"
        elif all(d < 0 for d in diffs):
            direction = "downward"
        else:
            return None

        total_shift = tail[-1] - tail[0]
        sigma = abs(total_shift / metric.std_dev) if metric.std_dev else 0.0

        alert = BaselineAlert(
            metric_id=metric_id,
            alert_type="trend",
            severity="warning" if sigma >= 1.5 else "info",
            message=(
                f"{metric_id} shows a consistent {direction} trend over the "
                f"last {window} readings ({tail[0]:.1f} → {tail[-1]:.1f})"
            ),
            current_value=tail[-1],
            baseline_mean=metric.mean,
            deviation_sigma=sigma,
        )
        self._persist_alert(alert)
        return alert

    def get_alerts(self, since: float | None = None) -> list[BaselineAlert]:
        if since is None:
            since = time.time() - 86400  # last 24 h
        rows = self._conn.execute(
            "SELECT * FROM baseline_alerts WHERE ts >= ? ORDER BY ts DESC",
            (since,),
        ).fetchall()
        return [
            BaselineAlert(
                metric_id=r["metric_id"],
                alert_type=r["alert_type"],
                severity=r["severity"],
                message=r["message"],
                current_value=r["current_val"],
                baseline_mean=r["baseline_mean"],
                deviation_sigma=r["deviation_sigma"],
                timestamp=r["ts"],
            )
            for r in rows
        ]

    def summary(self) -> dict:
        metrics_count = self._conn.execute("SELECT count(*) c FROM baselines").fetchone()["c"]
        recent_alerts = self._conn.execute(
            "SELECT count(*) c FROM baseline_alerts WHERE ts >= ?",
            (time.time() - 86400,),
        ).fetchone()["c"]
        cats = self._conn.execute(
            "SELECT DISTINCT category FROM baselines"
        ).fetchall()
        return {
            "metrics_tracked": metrics_count,
            "recent_alerts": recent_alerts,
            "categories": [r["category"] for r in cats],
        }

    # ------------------------------------------------------------------
    # Durable biometric time-series (week-over-week trend source)
    # ------------------------------------------------------------------

    def record_sample(
        self,
        metric: str,
        value: float,
        source: str = "",
        ts: float | None = None,
        retention_days: float | None = None,
    ) -> None:
        """Append a raw biometric sample to the durable time-series.

        Unlike :meth:`record` (which keeps only a 14-value rolling
        window for anomaly detection) this preserves every reading with
        its own timestamp + source so the health summary can compute
        real daily / weekly statistics from the glasses stream alone.

        Args:
            metric: canonical metric name (``hr``, ``spo2``,
                ``skin_temp``, ``hrv``, ``steps`` ...).
            value: numeric reading.
            source: emitting wearable id (``jw_health_glasses`` ...).
            ts: sample epoch seconds. Defaults to ``time.time()``.
            retention_days: override the prune horizon for this write.
        """
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        sample_ts = float(ts) if isinstance(ts, (int, float)) and ts else time.time()
        self._conn.execute(
            "INSERT INTO biometric_samples (ts, source, metric, value) "
            "VALUES (?, ?, ?, ?)",
            (sample_ts, str(source or ""), str(metric), v),
        )
        self._sample_writes += 1
        if self._sample_writes % self._SAMPLE_PRUNE_EVERY_N == 0:
            self.prune_samples(retention_days)
        self._conn.commit()

    def cloud_retention_days(self) -> float:
        """Retention horizon for cloud-wearable samples, in days.

        Env override: ``FERAL_HEALTH_CLOUD_RETENTION_DAYS``. Default
        ``CLOUD_SAMPLE_RETENTION_DAYS`` (400 days, so "this month last
        year" is answerable). Read per call so an operator can widen or
        narrow it without a restart, and so tests can set it.
        """
        raw = os.environ.get("FERAL_HEALTH_CLOUD_RETENTION_DAYS")
        if raw is None or not str(raw).strip():
            return float(self.CLOUD_SAMPLE_RETENTION_DAYS)
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            logger.warning(
                "FERAL_HEALTH_CLOUD_RETENTION_DAYS=%r is not a number; "
                "using %s", raw, self.CLOUD_SAMPLE_RETENTION_DAYS,
            )
            return float(self.CLOUD_SAMPLE_RETENTION_DAYS)
        # Never shorter than the live-sensor horizon: a cloud mirror
        # outliving the raw stream is the whole point.
        return max(value, float(self.SAMPLE_RETENTION_DAYS))

    def prune_samples(self, retention_days: float | None = None) -> int:
        """Delete samples older than the retention horizon. Returns the
        number of rows removed. Keeps the time-series bounded so it can
        never grow without limit on a long-running brain.

        Passing *retention_days* applies that one horizon to every row,
        which is the pre-existing behaviour and what callers that want a
        hard sweep expect. With no argument, live-sensor sources are
        pruned at ``SAMPLE_RETENTION_DAYS`` (unchanged) and cloud
        wearable mirrors at the longer ``cloud_retention_days()``.
        """
        if retention_days is not None:
            cutoff = time.time() - (float(retention_days) * 86400.0)
            cur = self._conn.execute(
                "DELETE FROM biometric_samples WHERE ts < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount or 0

        cloud = tuple(self.CLOUD_SAMPLE_SOURCES)
        placeholders = ", ".join("?" for _ in cloud)
        now = time.time()
        removed = 0

        live_cutoff = now - (float(self.SAMPLE_RETENTION_DAYS) * 86400.0)
        cur = self._conn.execute(
            f"DELETE FROM biometric_samples WHERE ts < ? "
            f"AND lower(source) NOT IN ({placeholders})",
            (live_cutoff, *cloud),
        )
        removed += cur.rowcount or 0

        cloud_cutoff = now - (self.cloud_retention_days() * 86400.0)
        cur = self._conn.execute(
            f"DELETE FROM biometric_samples WHERE ts < ? "
            f"AND lower(source) IN ({placeholders})",
            (cloud_cutoff, *cloud),
        )
        removed += cur.rowcount or 0

        self._conn.commit()
        return removed

    def get_samples(
        self,
        metric: str,
        since: float | None = None,
        until: float | None = None,
        source: str | None = None,
    ) -> list[dict]:
        """Return raw samples for *metric*, ascending by time.

        Each row is ``{"ts": float, "source": str, "value": float}``.
        """
        query = "SELECT ts, source, value FROM biometric_samples WHERE metric = ?"
        params: list = [str(metric)]
        if since is not None:
            query += " AND ts >= ?"
            params.append(float(since))
        if until is not None:
            query += " AND ts <= ?"
            params.append(float(until))
        if source:
            query += " AND source = ?"
            params.append(str(source))
        query += " ORDER BY ts ASC"
        rows = self._conn.execute(query, params).fetchall()
        return [
            {"ts": r["ts"], "source": r["source"], "value": r["value"]}
            for r in rows
        ]

    def get_daily_stats(
        self,
        metric: str,
        days: int = 7,
        source: str | None = None,
    ) -> list[dict]:
        """Bucket the last *days* of samples by local calendar date.

        Returns a list (ascending by date) of::

            {"date": "YYYY-MM-DD", "count": N, "min": x, "max": y,
             "avg": z, "first": a, "last": b}

        ``min`` doubles as the daily resting-HR estimate for the HR
        metric (lowest sample of the day ≈ resting tone), which is the
        slot the health summary fills when no Whoop/Oura source exists.
        """
        since = time.time() - (float(days) * 86400.0)
        samples = self.get_samples(metric, since=since, source=source)
        buckets: dict[str, list[float]] = {}
        for s in samples:
            day = datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d")
            buckets.setdefault(day, []).append(s["value"])
        out: list[dict] = []
        for day in sorted(buckets):
            vals = buckets[day]
            out.append({
                "date": day,
                "count": len(vals),
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "avg": round(statistics.mean(vals), 2),
                "first": round(vals[0], 2),
                "last": round(vals[-1], 2),
            })
        return out

    def get_trend(
        self,
        metric: str,
        days: int = 7,
        source: str | None = None,
    ) -> dict:
        """Aggregate window stats + per-day breakdown for *metric*.

        Returns::

            {"metric": str, "days": int, "sample_count": N,
             "sources": [...], "min": x, "max": y, "avg": z,
             "first_ts": ts, "last_ts": ts, "daily": [ ...daily_stats ]}

        ``sample_count == 0`` means there is no persisted history for
        this metric/window — the caller should NOT fabricate a trend.
        """
        since = time.time() - (float(days) * 86400.0)
        samples = self.get_samples(metric, since=since, source=source)
        daily = self.get_daily_stats(metric, days=days, source=source)
        if not samples:
            return {
                "metric": metric,
                "days": int(days),
                "sample_count": 0,
                "sources": [],
                "min": None,
                "max": None,
                "avg": None,
                "first_ts": None,
                "last_ts": None,
                "daily": [],
            }
        vals = [s["value"] for s in samples]
        sources = sorted({s["source"] for s in samples if s["source"]})
        return {
            "metric": metric,
            "days": int(days),
            "sample_count": len(vals),
            "sources": sources,
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "avg": round(statistics.mean(vals), 2),
            "first_ts": samples[0]["ts"],
            "last_ts": samples[-1]["ts"],
            "daily": daily,
        }

    def sample_metrics(self, days: int = 35) -> list[str]:
        """Distinct metric names with at least one sample in the window."""
        since = time.time() - (float(days) * 86400.0)
        rows = self._conn.execute(
            "SELECT DISTINCT metric FROM biometric_samples WHERE ts >= ? "
            "ORDER BY metric",
            (since,),
        ).fetchall()
        return [r["metric"] for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_metric(row: sqlite3.Row) -> BaselineMetric:
        return BaselineMetric(
            metric_id=row["metric_id"],
            category=row["category"],
            values=json.loads(row["values_json"]),
            window_size=row["window_size"],
            mean=row["mean"],
            std_dev=row["std_dev"],
            last_updated=row["updated_at"],
        )

    def _persist_alert(self, alert: BaselineAlert) -> None:
        self._conn.execute(
            """INSERT INTO baseline_alerts
               (metric_id, alert_type, severity, message,
                current_val, baseline_mean, deviation_sigma, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                alert.metric_id,
                alert.alert_type,
                alert.severity,
                alert.message,
                alert.current_value,
                alert.baseline_mean,
                alert.deviation_sigma,
                alert.timestamp,
            ),
        )
        self._conn.commit()
        for listener in list(self._alert_listeners):
            try:
                listener(alert)
            except Exception as exc:
                logger.debug("Baseline alert listener failed: %s", exc)
