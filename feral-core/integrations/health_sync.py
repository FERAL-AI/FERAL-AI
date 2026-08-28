"""
FERAL Whoop Durable Sync
==========================
Whoop was implemented and wired (OAuth provider with the ``offline``
scope, an API client, a ``HealthAggregator`` fan-in, and
``GET /api/health-summary``) but nothing was ever persisted.
``get_recovery`` / ``get_sleep`` / ``get_cycles`` were called on demand
inside ``get_health_summary`` and returned as a transient dict, so
"my recovery over six months" had no data to answer from. A Whoop
account that has been connected for a year could still only be asked
about the last seven days, and only while the network was up.

This module closes that. It pulls Whoop's daily records on an interval
and writes them into the existing durable
``BaselineEngine.biometric_samples`` table as canonical readings.

Why ``biometric_samples`` and not a new table
---------------------------------------------
Every Whoop field this syncs is a scalar with a timestamp and a source,
which is exactly ``(ts, source, metric, value)``. A Whoop-specific table
would have been a sixth health-reading shape carrying the same facts,
plus a second set of query helpers, plus a second retention policy. The
one field of Whoop's that genuinely is not a scalar sample is the
workout record (``sport_id`` is categorical, and a workout is an
interval event rather than a point reading), so workouts are
deliberately not synced here.

Two things had to be true first, and both are handled:

* **Metric namespacing.** Whoop's ``resting_heart_rate`` is a
  once-a-day derived value; the glasses' ``hr`` is an instantaneous PPG
  sample many times a minute. Writing both under ``hr`` would corrupt
  every min/max/avg the vitals trend computes. Whoop writes only the
  ``daily`` metric family (``resting_hr``, ``spo2_avg``, ...), enforced
  by ``health_canonical.canonical_metric_for_source``.
* **Retention.** ``biometric_samples`` is pruned at 35 days, which is
  right for a 1 Hz sensor stream and wrong for a 10-rows-a-day cloud
  mirror. ``BaselineEngine.prune_samples`` now applies a longer horizon
  to cloud sources only; the live BLE series keeps its exact 35-day
  behaviour.

Sync is idempotent: every write is checked against what is already
stored for that (metric, source, day), so re-running it never
duplicates a row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from integrations.health_canonical import (
    SOURCE_WHOOP,
    build_reading,
    canonical_metric_for_source,
)

logger = logging.getLogger("feral.integrations.health.sync")

#: How often the background loop pulls Whoop. Whoop's rate limit is
#: generous and its data only changes a few times a day, so the default
#: is deliberately unhurried. Override with
#: ``FERAL_WHOOP_SYNC_INTERVAL_S`` (seconds, minimum 60).
DEFAULT_SYNC_INTERVAL_S = 900.0
_MIN_SYNC_INTERVAL_S = 60.0

#: How far back each sync pulls. Whoop backfills and revises recent
#: records (a sleep score can change hours after the fact), so a rolling
#: window is re-read every time and de-duplicated on write rather than
#: only fetching since the last cursor. Override with
#: ``FERAL_WHOOP_SYNC_LOOKBACK_DAYS`` (minimum 1).
DEFAULT_LOOKBACK_DAYS = 14

#: Ceiling on how long a user-facing health read may spend waiting for a
#: mirror refresh. "How did I sleep" runs inside the user's turn, which
#: holds ``Orchestrator._get_session_lock``, so every second spent here
#: also delays the user's NEXT message. Past this budget the refresh
#: continues in the background and the question is answered from what is
#: already in durable storage, which is the whole point of mirroring.
USER_WAIT_BUDGET_S = 2.0

#: Whoop recovery ``score`` field -> canonical metric.
_RECOVERY_FIELD_MAP: dict[str, str] = {
    "recovery_score": "recovery_score",
    "resting_hr": "resting_hr",
    "hrv_ms": "hrv",
    "spo2_pct": "spo2_avg",
    "skin_temp_celsius": "skin_temp_avg",
}

#: Whoop sleep entry field -> canonical metric.
_SLEEP_FIELD_MAP: dict[str, str] = {
    "total_sleep_hours": "sleep_hours",
    "sleep_score": "sleep_score",
    "sleep_efficiency": "sleep_efficiency",
    "rem_hours": "rem_hours",
    "deep_hours": "deep_hours",
    "respiratory_rate": "respiratory_rate",
}

#: Whoop cycle entry field -> canonical metric.
_CYCLE_FIELD_MAP: dict[str, str] = {
    "strain": "strain",
    "avg_hr": "avg_hr",
    "max_hr": "max_hr",
    "calories": "calories_kj",
}


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return max(value, minimum)


def sync_interval_s() -> float:
    """Background sync interval in seconds (env-overridable)."""
    return _env_float(
        "FERAL_WHOOP_SYNC_INTERVAL_S", DEFAULT_SYNC_INTERVAL_S, _MIN_SYNC_INTERVAL_S,
    )


def sync_lookback_days() -> int:
    """How many days each sync re-reads (env-overridable)."""
    return int(_env_float("FERAL_WHOOP_SYNC_LOOKBACK_DAYS", float(DEFAULT_LOOKBACK_DAYS), 1.0))


def parse_whoop_timestamp(raw: Any) -> Optional[float]:
    """Parse a Whoop ``created_at`` ISO-8601 string to epoch seconds.

    Whoop stamps UTC with a trailing ``Z`` and millisecond precision
    (``2026-07-30T06:12:44.235Z``), which ``datetime.fromisoformat``
    rejects before Python 3.11 and accepts after. Normalise the suffix
    so both behave the same.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def day_timestamp(date_str: Any) -> Optional[float]:
    """Turn a ``YYYY-MM-DD`` day label into a stable epoch seconds value.

    Anchored at 12:00 **local** time, not midnight UTC, on purpose:
    ``BaselineEngine.get_daily_stats`` buckets by
    ``datetime.fromtimestamp(ts)`` (local), so a midnight-UTC anchor
    would land a Whoop record labelled 2026-07-30 into the 2026-07-29
    bucket for every negative-offset timezone. Noon local can only
    collide with a bucket boundary for offsets beyond 12 hours from the
    brain's own clock, which cannot happen for a locally stamped day.
    """
    text = str(date_str or "").strip()[:10]
    if not text:
        return None
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return day.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def readings_from_whoop_recovery(data: Any) -> list[dict[str, Any]]:
    """Canonical readings from a ``WhoopClient.get_recovery`` payload."""
    if not isinstance(data, dict):
        return []
    ts = parse_whoop_timestamp(data.get("created_at")) or time.time()
    out: list[dict[str, Any]] = []
    for field, metric in _RECOVERY_FIELD_MAP.items():
        reading = _reading(metric, data.get(field), ts)
        if reading:
            out.append(reading)
    return out


def readings_from_whoop_entries(
    entries: Any,
    field_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Canonical readings from a list of dated Whoop entries."""
    out: list[dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        ts = day_timestamp(entry.get("date"))
        if ts is None:
            continue
        for field, metric in field_map.items():
            reading = _reading(metric, entry.get(field), ts)
            if reading:
                out.append(reading)
    return out


def _reading(metric: str, value: Any, ts: float) -> Optional[dict[str, Any]]:
    """Build one canonical Whoop reading, dropping zeros and non-numerics.

    Whoop returns ``0`` for absent optional score fields (the client's
    ``score.get("recovery_score", 0)`` default), and a stored zero would
    read as a real measurement of zero. A genuine zero recovery score or
    zero resting heart rate is not a thing, so zero is treated as
    missing rather than persisted as a fact.
    """
    allowed = canonical_metric_for_source(metric, SOURCE_WHOOP)
    if allowed is None:
        logger.debug("Whoop metric %r is not writable by a cloud source", metric)
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric == 0.0:
        return None
    return build_reading(allowed, numeric, source=SOURCE_WHOOP, ts=ts)


class WhoopDurableSync:
    """Periodically mirror Whoop's daily records into durable storage.

    Args:
        whoop: a ``WhoopClient`` (or anything with ``connected``,
            ``get_recovery``, ``get_sleep``, ``get_cycles``).
        store_provider: zero-arg callable returning the
            ``BaselineEngine`` that owns ``biometric_samples``. Lazy so
            construction order does not matter and the hot path never
            imports ``api.state``.
        frame_sink: optional async callable invoked with the
            ``health_update`` frame after a sync that wrote something.
            Used to push fresh health data to connected nodes.
        interval_s / lookback_days: overrides for the env defaults.
        clock: injectable time source for tests.
        user_wait_budget_s: the longest a user-facing read is allowed to
            wait for a refresh in :meth:`maybe_sync`. Past it the refresh
            keeps running in the background and the caller is answered
            from what is already stored.
    """

    def __init__(
        self,
        whoop: Any = None,
        store_provider: Optional[Callable[[], Any]] = None,
        frame_sink: Optional[Callable[[dict], Any]] = None,
        interval_s: Optional[float] = None,
        lookback_days: Optional[int] = None,
        clock: Callable[[], float] = time.time,
        user_wait_budget_s: float = USER_WAIT_BUDGET_S,
    ):
        self._whoop = whoop
        self._store_provider = store_provider
        self._frame_sink = frame_sink
        self._interval_s = interval_s
        self._lookback_days = lookback_days
        self._clock = clock
        self._user_wait_budget_s = float(user_wait_budget_s)
        self._last_sync_at: float = 0.0
        self._last_result: dict[str, Any] = {}
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        # How many syncs are running right now. Read by ``maybe_sync`` so a
        # user query can decline to queue behind one instead of parking on
        # ``_lock`` for the length of three vendor round-trips.
        self._inflight: int = 0
        # Strong reference to a refresh a user query kicked off and then
        # stopped waiting for. Without it the loop's weak reference lets
        # the task be collected mid-flight.
        self._pending_refresh: Optional[asyncio.Future] = None

    # -- configuration -------------------------------------------------

    @property
    def interval_s(self) -> float:
        return self._interval_s if self._interval_s else sync_interval_s()

    @property
    def lookback_days(self) -> int:
        return self._lookback_days if self._lookback_days else sync_lookback_days()

    @property
    def last_sync_at(self) -> float:
        return self._last_sync_at

    @property
    def last_result(self) -> dict[str, Any]:
        return dict(self._last_result)

    # -- store ---------------------------------------------------------

    def _store(self) -> Optional[Any]:
        if self._store_provider is None:
            return None
        try:
            store = self._store_provider()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("WhoopDurableSync store_provider raised: %s", exc)
            return None
        if store is None or not hasattr(store, "record_sample"):
            return None
        return store

    # -- fetch ---------------------------------------------------------

    async def _collect_readings(self) -> list[dict[str, Any]]:
        """Pull the lookback window from Whoop as canonical readings.

        Every vendor call is individually guarded: a 401 on sleep must
        not lose an already-fetched recovery record.
        """
        days = self.lookback_days
        readings: list[dict[str, Any]] = []

        try:
            recovery = await self._whoop.get_recovery()
            if isinstance(recovery, dict) and recovery.get("success"):
                readings.extend(readings_from_whoop_recovery(recovery.get("data")))
        except Exception as exc:
            logger.warning("Whoop sync: recovery fetch failed: %s", exc)

        try:
            sleep = await self._whoop.get_sleep(days=days)
            if isinstance(sleep, dict) and sleep.get("success"):
                readings.extend(
                    readings_from_whoop_entries(sleep.get("data"), _SLEEP_FIELD_MAP)
                )
        except Exception as exc:
            logger.warning("Whoop sync: sleep fetch failed: %s", exc)

        try:
            cycles = await self._whoop.get_cycles(days=days)
            if isinstance(cycles, dict) and cycles.get("success"):
                readings.extend(
                    readings_from_whoop_entries(cycles.get("data"), _CYCLE_FIELD_MAP)
                )
        except Exception as exc:
            logger.warning("Whoop sync: cycle fetch failed: %s", exc)

        return readings

    # -- persist -------------------------------------------------------

    @staticmethod
    def _dedupe_key(metric: str, ts: float) -> tuple[str, int]:
        """Identity of a stored Whoop sample.

        Bucketed to the whole second: Whoop timestamps are stable
        between polls, and a float round-trip through SQLite must not
        make the same record look new.
        """
        return (str(metric), int(round(float(ts))))

    def _existing_keys(
        self, store: Any, metrics: set[str], since: float,
    ) -> set[tuple[str, int]]:
        keys: set[tuple[str, int]] = set()
        for metric in metrics:
            try:
                rows = store.get_samples(metric, since=since, source=SOURCE_WHOOP)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Whoop sync: get_samples(%s) failed: %s", metric, exc)
                continue
            for row in rows or []:
                keys.add(self._dedupe_key(metric, row.get("ts") or 0.0))
        return keys

    def _persist(self, readings: list[dict[str, Any]]) -> dict[str, Any]:
        store = self._store()
        if store is None:
            return {"written": 0, "duplicates": 0, "reason": "no_store"}
        if not readings:
            return {"written": 0, "duplicates": 0, "reason": "no_records"}

        metrics = {r["metric"] for r in readings}
        oldest = min(r["ts"] for r in readings)
        # Widen the dedupe lookup by a day so a sample written just
        # outside the window edge is still recognised.
        existing = self._existing_keys(store, metrics, since=oldest - 86400.0)

        written = 0
        duplicates = 0
        seen: set[tuple[str, int]] = set()
        for reading in sorted(readings, key=lambda r: r["ts"]):
            key = self._dedupe_key(reading["metric"], reading["ts"])
            if key in existing or key in seen:
                duplicates += 1
                continue
            try:
                store.record_sample(
                    reading["metric"],
                    reading["value"],
                    source=SOURCE_WHOOP,
                    ts=reading["ts"],
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Whoop sync: record_sample(%s) failed: %s",
                    reading["metric"], exc,
                )
                continue
            seen.add(key)
            written += 1
        return {"written": written, "duplicates": duplicates, "reason": ""}

    # -- public API ----------------------------------------------------

    @property
    def sync_in_flight(self) -> bool:
        """True while a sync is between its first and last vendor call."""
        return self._inflight > 0

    async def sync_once(self) -> dict[str, Any]:
        """Run one sync. Never raises; reports what happened."""
        readings: list[dict[str, Any]] = []
        written = 0
        self._inflight += 1
        try:
            async with self._lock:
                now = float(self._clock())
                if self._whoop is None:
                    result = {"synced": False, "written": 0, "reason": "no_client"}
                    self._last_result = result
                    return result
                try:
                    connected = bool(getattr(self._whoop, "connected", False))
                except Exception:  # pragma: no cover - defensive
                    connected = False
                if not connected:
                    result = {"synced": False, "written": 0, "reason": "not_connected"}
                    self._last_result = result
                    self._last_sync_at = now
                    return result

                # Claim the window BEFORE the vendor calls, not after.
                # ``maybe_sync`` reads ``_last_sync_at`` without the lock,
                # so writing it only on the way out left the timestamp
                # stale for the whole length of the sync: anyone arriving
                # mid-flight passed the throttle and ran a second full
                # round-trip on top of the one already running. The
                # docstring on ``maybe_sync`` has always said "since the
                # last attempt", and this is what makes that true.
                self._last_sync_at = now

                readings = await self._collect_readings()
                outcome = self._persist(readings)
                written = int(outcome["written"])
                result = {
                    "synced": True,
                    "source": SOURCE_WHOOP,
                    "fetched": len(readings),
                    "written": written,
                    "duplicates": outcome["duplicates"],
                    "reason": outcome["reason"],
                    "at": now,
                }
                self._last_result = result
        finally:
            self._inflight -= 1

        if written and self._frame_sink is not None:
            await self._emit_frame(readings)
        return result

    async def _emit_frame(self, readings: list[dict[str, Any]]) -> None:
        """Push the freshest reading per metric as a ``health_update``."""
        from integrations.health_canonical import (
            HEALTH_EVENT_SUMMARY,
            build_health_update_frame,
        )

        latest: dict[str, dict[str, Any]] = {}
        for reading in readings:
            current = latest.get(reading["metric"])
            if current is None or reading["ts"] >= current["ts"]:
                latest[reading["metric"]] = reading
        frame = build_health_update_frame(
            event_type=HEALTH_EVENT_SUMMARY,
            readings=[latest[k] for k in sorted(latest)],
            sources=[SOURCE_WHOOP],
            note="Whoop sync",
        )
        try:
            outcome = self._frame_sink(frame)
            if asyncio.iscoroutine(outcome):
                await outcome
        except Exception as exc:
            logger.warning("Whoop sync: frame_sink failed: %s", exc)

    async def maybe_sync(self) -> dict[str, Any]:
        """Give the mirror a chance to refresh, without holding up the
        caller.

        This is on the user's read path: ``get_health_summary`` calls it
        as its first statement, so "how did I sleep" runs through here,
        inside a turn that holds the session lock. It must therefore never
        park on ``_lock``, and never spend an unbounded amount of time in
        the vendor API.

        Three outcomes:

        * a sync is already running       -> return at once, ``in_flight``
        * inside ``interval_s``           -> return at once, ``throttled``
        * otherwise                       -> start one, wait up to
          ``user_wait_budget_s`` for it, and if it outlives that budget
          leave it running and answer ``still_running``. The refresh is
          not lost, it just stops being the caller's problem.
        """
        if self.sync_in_flight:
            # Somebody else is already doing exactly this work. Waiting
            # for them buys the caller nothing: the store is written at
            # the end either way, and the old code then went on to run a
            # SECOND full sync because the throttle timestamp was stale.
            return {"synced": False, "written": 0, "reason": "in_flight"}

        now = float(self._clock())
        if self._last_sync_at and (now - self._last_sync_at) < self.interval_s:
            return {"synced": False, "written": 0, "reason": "throttled"}

        task = asyncio.ensure_future(self.sync_once())
        # Strong reference, so a refresh that outlives the caller's budget
        # cannot be garbage-collected mid-flight (AUDIT-FIXES F-06).
        self._pending_refresh = task
        task.add_done_callback(self._clear_pending_refresh)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self._user_wait_budget_s,
            )
        except asyncio.TimeoutError:
            logger.info(
                "Whoop refresh outlived the %.1fs user budget; continuing in "
                "the background", self._user_wait_budget_s,
            )
            return {"synced": False, "written": 0, "reason": "still_running"}

    def _clear_pending_refresh(self, task: asyncio.Future) -> None:
        if self._pending_refresh is task:
            self._pending_refresh = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Whoop sync loop iteration failed: %s", exc)
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        """Start the background sync loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("WhoopDurableSync.start called with no running loop")
            return
        self._task = loop.create_task(self._loop())
        logger.info(
            "Whoop durable sync started (every %.0fs, %dd lookback)",
            self.interval_s, self.lookback_days,
        )

    async def stop(self) -> None:
        """Stop the background sync loop."""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
