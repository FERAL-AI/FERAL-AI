"""
FERAL Health Platform Aggregation
====================================
Multi-platform health data aggregation from Whoop and Oura Ring.
Provides a unified HealthAggregator that merges data from all
connected platforms with graceful degradation.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger("feral.integrations.health")

WHOOP_API = "https://api.prod.whoop.com/developer/v1"
OURA_API = "https://api.ouraring.com/v2/usercollection"

# ``get_health_summary``'s flat snapshot field -> canonical metric name
# (``integrations/health_canonical.py``). The snapshot dict is one of
# the five pre-existing health-reading shapes; this map is how it
# projects onto the canonical one for the wire, so the frame never
# invents a field name the summary does not already have.
_SUMMARY_TO_CANONICAL: dict[str, str] = {
    "sleep_hours": "sleep_hours",
    "sleep_quality": "sleep_score",
    "recovery_score": "recovery_score",
    "resting_hr": "resting_hr",
    "hrv": "hrv",
    "readiness": "readiness",
    "activity_score": "activity_score",
    "strain": "strain",
    "current_hr": "hr",
    "current_spo2": "spo2",
}


class WhoopClient:
    """Whoop API v1 client for recovery, sleep, workouts, cycles, and body data."""

    def __init__(self, oauth_manager=None):
        self._oauth = oauth_manager
        self._token: str = os.environ.get("FERAL_WHOOP_TOKEN", "")
        self._http = httpx.AsyncClient(base_url=WHOOP_API, timeout=15.0)

    async def _headers(self) -> Optional[dict[str, str]]:
        token = self._token
        if not token and self._oauth:
            token = await self._oauth.get_token("whoop") or ""
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        token_present = bool(self._token) or (
            self._oauth is not None and self._oauth.is_connected("whoop")
        )
        return is_connected_cached("whoop", fallback=token_present)

    async def probe_connected(self) -> bool:
        from integrations._probe_status import refresh

        result = await refresh("whoop",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    async def get_recovery(self) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Whoop not connected"}
        try:
            resp = await self._http.get("/recovery", headers=headers,
                                        params={"limit": 1})
            resp.raise_for_status()
            records = resp.json().get("records", [])
            if not records:
                return {"success": True, "data": None}
            rec = records[0]
            score = rec.get("score", {})
            return {
                "success": True,
                "data": {
                    "recovery_score": score.get("recovery_score", 0),
                    "resting_hr": score.get("resting_heart_rate", 0),
                    "hrv_ms": score.get("hrv_rmssd_milli", 0),
                    "spo2_pct": score.get("spo2_percentage"),
                    "skin_temp_celsius": score.get("skin_temp_celsius"),
                    "created_at": rec.get("created_at", ""),
                },
            }
        except Exception as e:
            logger.warning("Whoop recovery fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def get_sleep(self, days: int = 7) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Whoop not connected"}
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%dT00:00:00.000Z"
            )
            resp = await self._http.get(
                "/activity/sleep",
                headers=headers,
                params={"start": start, "limit": days},
            )
            resp.raise_for_status()
            records = resp.json().get("records", [])
            entries = []
            for rec in records:
                score = rec.get("score", {})
                entries.append({
                    "date": rec.get("created_at", "")[:10],
                    "total_sleep_hours": round(
                        score.get("total_in_bed_time_milli", 0) / 3_600_000, 2
                    ),
                    "sleep_efficiency": score.get("sleep_efficiency_percentage", 0),
                    "rem_hours": round(
                        score.get("total_rem_sleep_time_milli", 0) / 3_600_000, 2
                    ),
                    "deep_hours": round(
                        score.get("total_slow_wave_sleep_time_milli", 0) / 3_600_000, 2
                    ),
                    "sleep_score": score.get("sleep_performance_percentage", 0),
                    "respiratory_rate": score.get("respiratory_rate"),
                })
            return {"success": True, "data": entries, "source": "whoop"}
        except Exception as e:
            logger.warning("Whoop sleep fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def get_workouts(self, days: int = 7) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Whoop not connected"}
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%dT00:00:00.000Z"
            )
            resp = await self._http.get(
                "/activity/workout",
                headers=headers,
                params={"start": start, "limit": 25},
            )
            resp.raise_for_status()
            records = resp.json().get("records", [])
            workouts = []
            for rec in records:
                score = rec.get("score", {})
                workouts.append({
                    "date": rec.get("created_at", "")[:10],
                    "sport_id": rec.get("sport_id"),
                    "strain": score.get("strain", 0),
                    "avg_hr": score.get("average_heart_rate", 0),
                    "max_hr": score.get("max_heart_rate", 0),
                    "calories": score.get("kilojoule", 0),
                    "duration_min": round(
                        score.get("duration_milli", 0) / 60_000, 1
                    ),
                })
            return {"success": True, "data": workouts, "source": "whoop"}
        except Exception as e:
            logger.warning("Whoop workouts fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def get_cycles(self, days: int = 7) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Whoop not connected"}
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%dT00:00:00.000Z"
            )
            resp = await self._http.get(
                "/cycle",
                headers=headers,
                params={"start": start, "limit": days},
            )
            resp.raise_for_status()
            records = resp.json().get("records", [])
            cycles = []
            for rec in records:
                score = rec.get("score", {})
                cycles.append({
                    "date": rec.get("created_at", "")[:10],
                    "strain": score.get("strain", 0),
                    "calories": score.get("kilojoule", 0),
                    "avg_hr": score.get("average_heart_rate", 0),
                    "max_hr": score.get("max_heart_rate", 0),
                })
            return {"success": True, "data": cycles, "source": "whoop"}
        except Exception as e:
            logger.warning("Whoop cycles fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def get_body_measurements(self) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Whoop not connected"}
        try:
            resp = await self._http.get("/body_measurement", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return {
                "success": True,
                "data": {
                    "height_m": data.get("height_meter"),
                    "weight_kg": data.get("weight_kilogram"),
                    "max_hr": data.get("max_heart_rate"),
                },
            }
        except Exception as e:
            logger.warning("Whoop body measurements fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def close(self):
        await self._http.aclose()


class OuraClient:
    """Oura Ring API v2 client for sleep, readiness, activity, and heart rate."""

    def __init__(self, oauth_manager=None):
        self._oauth = oauth_manager
        self._token: str = os.environ.get("FERAL_OURA_TOKEN", "")
        self._http = httpx.AsyncClient(base_url=OURA_API, timeout=15.0)

    async def _headers(self) -> Optional[dict[str, str]]:
        token = self._token
        if not token and self._oauth:
            token = await self._oauth.get_token("oura") or ""
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        token_present = bool(self._token) or (
            self._oauth is not None and self._oauth.is_connected("oura")
        )
        return is_connected_cached("oura", fallback=token_present)

    async def probe_connected(self) -> bool:
        from integrations._probe_status import refresh

        result = await refresh("oura",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    async def get_sleep(self, days: int = 7) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Oura not connected"}
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%d"
            )
            resp = await self._http.get(
                "/daily_sleep", headers=headers, params={"start_date": start}
            )
            resp.raise_for_status()
            items = resp.json().get("data", [])
            entries = []
            for item in items:
                contrib = item.get("contributors", {})
                entries.append({
                    "date": item.get("day", ""),
                    "sleep_score": item.get("score"),
                    "total_sleep_hours": round(
                        item.get("timestamp_end", 0) / 3600, 2
                    ) if isinstance(item.get("timestamp_end"), (int, float)) else None,
                    "deep_sleep_contrib": contrib.get("deep_sleep"),
                    "rem_sleep_contrib": contrib.get("rem_sleep"),
                    "efficiency": contrib.get("efficiency"),
                    "restfulness": contrib.get("restfulness"),
                })
            return {"success": True, "data": entries, "source": "oura"}
        except Exception as e:
            logger.warning("Oura sleep fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def get_readiness(self, days: int = 7) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Oura not connected"}
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%d"
            )
            resp = await self._http.get(
                "/daily_readiness", headers=headers, params={"start_date": start}
            )
            resp.raise_for_status()
            items = resp.json().get("data", [])
            entries = []
            for item in items:
                contrib = item.get("contributors", {})
                entries.append({
                    "date": item.get("day", ""),
                    "readiness_score": item.get("score"),
                    "temperature_deviation": item.get("temperature_deviation"),
                    "hrv_balance": contrib.get("hrv_balance"),
                    "body_temperature": contrib.get("body_temperature"),
                    "resting_hr": contrib.get("resting_heart_rate"),
                    "recovery_index": contrib.get("recovery_index"),
                })
            return {"success": True, "data": entries, "source": "oura"}
        except Exception as e:
            logger.warning("Oura readiness fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def get_activity(self, days: int = 7) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Oura not connected"}
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%d"
            )
            resp = await self._http.get(
                "/daily_activity", headers=headers, params={"start_date": start}
            )
            resp.raise_for_status()
            items = resp.json().get("data", [])
            entries = []
            for item in items:
                entries.append({
                    "date": item.get("day", ""),
                    "activity_score": item.get("score"),
                    "active_calories": item.get("active_calories"),
                    "total_calories": item.get("total_calories"),
                    "steps": item.get("steps"),
                    "equivalent_walking_distance": item.get(
                        "equivalent_walking_distance"
                    ),
                    "high_activity_min": item.get("high_activity_met_minutes"),
                    "medium_activity_min": item.get("medium_activity_met_minutes"),
                })
            return {"success": True, "data": entries, "source": "oura"}
        except Exception as e:
            logger.warning("Oura activity fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def get_heart_rate(self) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Oura not connected"}
        try:
            start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
                "%Y-%m-%dT%H:%M:%S+00:00"
            )
            resp = await self._http.get(
                "/heartrate", headers=headers, params={"start_datetime": start}
            )
            resp.raise_for_status()
            items = resp.json().get("data", [])
            readings = [
                {"bpm": item.get("bpm"), "source": item.get("source"),
                 "timestamp": item.get("timestamp")}
                for item in items[-50:]  # cap to latest 50
            ]
            return {"success": True, "data": readings, "source": "oura"}
        except Exception as e:
            logger.warning("Oura heart rate fetch failed: %s", e)
            return {"success": False, "error": str(e)}

    async def close(self):
        await self._http.aclose()


class HealthAggregator:
    """Unified health interface that merges data from all connected platforms."""

    def __init__(
        self,
        whoop: Optional[WhoopClient] = None,
        oura: Optional[OuraClient] = None,
        live_wearable_provider: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
        biometric_history_provider: Optional[Callable[[], Any]] = None,
        sync_provider: Optional[Callable[[], Any]] = None,
    ):
        self._whoop = whoop
        self._oura = oura
        # Pluggable accessor for the brain's perception state. When
        # set, ``get_health_summary`` consults it for the most recent
        # FRESH live-wearable HR/SpO2 sample (Theora W300 glasses,
        # Veepoo wristband, etc.) so the chat path matches what the
        # WebUI's ``/api/dashboard.latest_health`` already surfaces.
        # Operator report 2026-06-07: the iOS chat assistant answered
        # "no current data coming in from your health sources right
        # now" when asked about heart rate even though the W300 was
        # streaming bpm into the perception frame — the previous
        # implementation only looked at Whoop/Oura, which were both
        # disconnected. The provider is a callable so the hot-path
        # never imports ``api.state`` (would be a circular import) and
        # tests can inject a stub.
        self._live_wearable_provider = live_wearable_provider
        # Pluggable accessor for the durable biometric time-series
        # (``BaselineEngine.biometric_samples``). Operator report
        # 2026-06-13: glasses HR/SpO2 streamed into the live snapshot +
        # rolling baseline but were never persisted as a queryable
        # series, so "over the last week how were my vitals?" returned
        # "no data" for every trend (sleep/recovery/HRV/resting-HR) —
        # those only came from Whoop/Oura. When NO third-party wearable
        # is connected, ``get_health_summary`` / ``get_vitals_trend``
        # now derive real week-over-week stats from this store so the
        # glasses alone answer the question. Callable to keep the
        # hot-path free of an ``api.state`` import and let tests inject
        # an in-memory ``BaselineEngine``.
        self._biometric_history_provider = biometric_history_provider
        # Pluggable accessor for the durable Whoop mirror
        # (``integrations.health_sync.WhoopDurableSync``). Pre-fix,
        # Whoop was fetched on demand inside ``get_health_summary`` and
        # thrown away, so nothing about Whoop survived the request and
        # "my recovery over six months" was unanswerable. The sync
        # service writes Whoop's daily records into the same durable
        # ``biometric_samples`` store the glasses use. Consulted here so
        # the mirror stays warm on the paths the user actually
        # exercises (chat's ``health_summary`` tool, the manifest's
        # 07:00 ``morning_briefing`` cron, ``GET /api/health-summary``)
        # in addition to the service's own background loop. Callable and
        # rate-limited, so with no Whoop connected it costs nothing.
        self._sync_provider = sync_provider

    @property
    def sources(self) -> list[str]:
        s = []
        if self._whoop and self._whoop.connected:
            s.append("whoop")
        if self._oura and self._oura.connected:
            s.append("oura")
        return s

    def _live_wearable_snapshot(self) -> Optional[dict[str, Any]]:
        """Pull the freshest live-wearable HR/SpO2 reading from the
        perception state, if any. Returns ``None`` when the provider
        is missing, raises, or has no fresh sample.

        Shape (all fields optional except ``source`` when present):
        ``{
            "heart_rate": int|None,
            "heart_rate_source": str|None,
            "spo2": int|None,
            "spo2_source": str|None,
            "skin_temperature_c": float|None,
        }``
        """
        if self._live_wearable_provider is None:
            return None
        try:
            snap = self._live_wearable_provider()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "HealthAggregator live_wearable_provider raised: %s", exc,
            )
            return None
        if not isinstance(snap, dict) or not snap:
            return None
        return snap

    def _biometric_history(self) -> Optional[Any]:
        """Resolve the durable biometric time-series store (a
        :class:`~agents.baseline_engine.BaselineEngine`), or ``None``
        when no provider is wired. Defensive: a raising provider must
        never crash the summary."""
        if self._biometric_history_provider is None:
            return None
        try:
            store = self._biometric_history_provider()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "HealthAggregator biometric_history_provider raised: %s", exc,
            )
            return None
        if store is None or not hasattr(store, "get_trend"):
            return None
        return store

    @staticmethod
    def _has_third_party_trend_source(summary: dict[str, Any]) -> bool:
        """True when a cloud wearable (Whoop/Oura) contributed the
        resting/recovery/HRV trend fields. When False and glasses
        history exists, the summary falls back to the glasses-derived
        trend instead of leaving every field null ("no data")."""
        return any(
            summary.get(k) is not None
            for k in ("recovery_score", "hrv", "readiness", "strain")
        ) or ("whoop" in summary.get("sources", []) or "oura" in summary.get("sources", []))

    def _build_glasses_vitals_trend(
        self, days: int = 7,
    ) -> Optional[dict[str, Any]]:
        """Compute a week-over-week vitals trend from the persisted
        wearable samples (glasses/wristband). Returns ``None`` when no
        history store is wired or no samples exist in the window."""
        store = self._biometric_history()
        if store is None:
            return None
        try:
            hr = store.get_trend("hr", days=days)
            spo2 = store.get_trend("spo2", days=days)
            skin = store.get_trend("skin_temp", days=days)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("biometric history get_trend failed: %s", exc)
            return None
        if not hr["sample_count"] and not spo2["sample_count"] and not skin["sample_count"]:
            return None

        # Daily resting-HR estimate = the day's minimum sample (lowest
        # tone of the day ≈ resting); avg gives the day's overall level.
        resting_hr_trend = [
            {
                "date": d["date"],
                "resting_hr": d["min"],
                "avg_hr": d["avg"],
                "max_hr": d["max"],
                "samples": d["count"],
            }
            for d in hr["daily"]
        ]
        sources = sorted(set(hr["sources"]) | set(spo2["sources"]) | set(skin["sources"]))
        primary_source = sources[0] if sources else "wearable sensor"

        trend: dict[str, Any] = {
            "window_days": days,
            "sources": sources,
            "primary_source": primary_source,
            "hr_sample_count": hr["sample_count"],
            "spo2_sample_count": spo2["sample_count"],
            "resting_hr_trend": resting_hr_trend,
            "hr_range": (
                {"min": hr["min"], "max": hr["max"], "avg": hr["avg"]}
                if hr["sample_count"]
                else None
            ),
            "spo2_avg": spo2["avg"] if spo2["sample_count"] else None,
            "spo2_min": spo2["min"] if spo2["sample_count"] else None,
            "skin_temp_avg": skin["avg"] if skin["sample_count"] else None,
        }
        # The lowest daily-min across the window is the most honest
        # single-number "resting HR" answer derived from the glasses.
        resting_candidates = [d["min"] for d in hr["daily"] if d.get("min")]
        trend["resting_hr_estimate"] = (
            round(min(resting_candidates), 1) if resting_candidates else None
        )

        total = hr["sample_count"] + spo2["sample_count"]
        trend["note"] = (
            f"Derived from {primary_source} ({total} samples over {days}d); "
            "no third-party wearable connected."
        )
        return trend

    async def get_vitals_trend(self, days: int = 7) -> dict[str, Any]:
        """Week-over-week vitals trend built from the persisted
        glasses / wearable samples. Endpoint behind the ``health_data``
        skill so chat can answer "how were my vitals this week?" with
        real stats even when no Whoop/Oura is connected.

        Returns the trend dict (see :meth:`_build_glasses_vitals_trend`)
        or, when nothing has been persisted yet, an explicit empty
        shape so the LLM can say so honestly instead of hallucinating.
        """
        trend = self._build_glasses_vitals_trend(days=days)
        if trend is None:
            return {
                "window_days": days,
                "sources": [],
                "resting_hr_trend": [],
                "hr_range": None,
                "spo2_avg": None,
                "spo2_min": None,
                "resting_hr_estimate": None,
                "note": (
                    "No persisted wearable biometric history for the last "
                    f"{days} days. Connect/stream the W300 glasses (or another "
                    "wearable) to build a vitals trend."
                ),
            }
        return trend

    async def _maybe_sync_durable(self) -> None:
        """Give the Whoop mirror a chance to refresh before answering.

        Rate-limited inside the sync service, guarded here, and a no-op
        when no service is wired or Whoop is disconnected. A failure to
        mirror must never fail the summary the user asked for.
        """
        if self._sync_provider is None:
            return
        try:
            service = self._sync_provider()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("HealthAggregator sync_provider raised: %s", exc)
            return
        if service is None or not hasattr(service, "maybe_sync"):
            return
        try:
            await service.maybe_sync()
        except Exception as exc:
            logger.warning("Whoop durable sync failed: %s", exc)

    async def get_health_history(
        self,
        days: int = 180,
        metric: str | None = None,
    ) -> dict[str, Any]:
        """Long-window history straight out of durable storage.

        This is the endpoint that answers "how has my recovery been over
        the last six months". It reads ``biometric_samples`` only, never
        the vendor APIs, so it works offline, covers windows far longer
        than any vendor query in this file, and returns whatever was
        mirrored regardless of whether Whoop is reachable right now.

        Every entry is a canonical reading, the same shape the
        ``health_update`` frame carries.
        """
        from integrations.health_canonical import (
            CANONICAL_METRICS, build_reading,
        )

        window = max(int(days or 0), 1)
        store = self._biometric_history()
        if store is None:
            return {
                "window_days": window,
                "metrics": [],
                "sources": [],
                "series": {},
                "note": (
                    "No durable biometric store is wired, so there is no "
                    "health history to read."
                ),
            }

        if metric:
            wanted = [str(metric)] if str(metric) in CANONICAL_METRICS else []
        else:
            try:
                wanted = [
                    m for m in store.sample_metrics(days=window)
                    if m in CANONICAL_METRICS
                ]
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("health history sample_metrics failed: %s", exc)
                wanted = []

        since = time.time() - (float(window) * 86400.0)
        series: dict[str, list[dict[str, Any]]] = {}
        sources: set[str] = set()
        for name in wanted:
            try:
                rows = store.get_samples(name, since=since)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("health history get_samples(%s) failed: %s", name, exc)
                continue
            entries = []
            for row in rows or []:
                reading = build_reading(
                    name, row.get("value"),
                    source=row.get("source", ""), ts=row.get("ts"),
                )
                if reading is None:
                    continue
                entries.append(reading)
                if reading["source"]:
                    sources.add(reading["source"])
            if entries:
                series[name] = entries

        total = sum(len(v) for v in series.values())
        return {
            "window_days": window,
            "metrics": sorted(series),
            "sources": sorted(sources),
            "sample_count": total,
            "series": series,
            "note": (
                f"{total} durable readings across {len(series)} metrics over "
                f"{window}d, from {', '.join(sorted(sources)) or 'no source'}."
                if total
                else (
                    f"No durable health readings in the last {window} days. "
                    "Connect Whoop or stream a wearable to build history."
                )
            ),
        }

    async def build_health_update(
        self,
        event_type: str = "health_summary",
        days: int = 7,
        node_id: str = "",
    ) -> dict[str, Any]:
        """Build the brain-to-node ``health_update`` frame.

        ``health_summary`` carries current values as canonical readings;
        ``vitals_trend`` carries dated per-metric series. Both use the
        same reading shape so one iOS renderer handles both.
        """
        from integrations.health_canonical import (
            HEALTH_EVENT_SUMMARY,
            HEALTH_EVENT_TREND,
            build_health_update_frame,
            build_reading,
            build_series,
        )

        if str(event_type) == HEALTH_EVENT_TREND:
            history = await self.get_health_history(days=days)
            series = []
            for name in history.get("metrics", []):
                entries = history["series"].get(name) or []
                built = build_series(
                    name,
                    [{"ts": e["ts"], "value": e["value"]} for e in entries],
                    source=entries[0]["source"] if entries else "",
                )
                if built:
                    series.append(built)
            return build_health_update_frame(
                event_type=HEALTH_EVENT_TREND,
                series=series,
                sources=history.get("sources") or [],
                node_id=node_id,
                window_days=int(history.get("window_days") or days),
                note=str(history.get("note") or ""),
            )

        summary = await self.get_health_summary()
        now = time.time()
        readings: list[dict[str, Any]] = []
        for field, canonical in _SUMMARY_TO_CANONICAL.items():
            value = summary.get(field)
            if value is None:
                continue
            source = ""
            if field in ("current_hr", "current_spo2"):
                source = str(summary.get(f"{field}_source") or "")
            if not source:
                srcs = summary.get("sources") or []
                source = str(srcs[0]) if srcs else ""
            reading = build_reading(canonical, value, source=source, ts=now)
            if reading:
                readings.append(reading)
        return build_health_update_frame(
            event_type=HEALTH_EVENT_SUMMARY,
            readings=readings,
            sources=[str(s) for s in (summary.get("sources") or [])],
            node_id=node_id,
            window_days=0,
        )

    async def get_health_summary(self) -> dict[str, Any]:
        """Merge data from all connected platforms into a unified dict."""
        await self._maybe_sync_durable()
        summary: dict[str, Any] = {
            "sleep_hours": None,
            "sleep_quality": None,
            "recovery_score": None,
            "resting_hr": None,
            "hrv": None,
            "readiness": None,
            "activity_score": None,
            "strain": None,
            "current_hr": None,
            "current_hr_source": None,
            "current_spo2": None,
            "current_spo2_source": None,
            "sources": list(self.sources),
        }

        # Whoop data
        if self._whoop and self._whoop.connected:
            try:
                recovery = await self._whoop.get_recovery()
                if recovery.get("success") and recovery.get("data"):
                    rd = recovery["data"]
                    summary["recovery_score"] = rd.get("recovery_score")
                    summary["resting_hr"] = rd.get("resting_hr")
                    summary["hrv"] = rd.get("hrv_ms")
            except Exception as e:
                logger.warning("Whoop summary aggregation error: %s", e)

            try:
                sleep = await self._whoop.get_sleep(days=1)
                if sleep.get("success") and sleep.get("data"):
                    latest = sleep["data"][0] if sleep["data"] else None
                    if latest:
                        summary["sleep_hours"] = latest.get("total_sleep_hours")
                        summary["sleep_quality"] = latest.get("sleep_score")
            except Exception as e:
                logger.warning("Whoop sleep aggregation error: %s", e)

            try:
                cycles = await self._whoop.get_cycles(days=1)
                if cycles.get("success") and cycles.get("data"):
                    latest = cycles["data"][0] if cycles["data"] else None
                    if latest:
                        summary["strain"] = latest.get("strain")
            except Exception as e:
                logger.warning("Whoop cycle aggregation error: %s", e)

        # Oura data (fills gaps left by Whoop or overrides where Oura is primary)
        if self._oura and self._oura.connected:
            try:
                readiness = await self._oura.get_readiness(days=1)
                if readiness.get("success") and readiness.get("data"):
                    latest = readiness["data"][-1] if readiness["data"] else None
                    if latest:
                        summary["readiness"] = latest.get("readiness_score")
                        if summary["resting_hr"] is None:
                            summary["resting_hr"] = latest.get("resting_hr")
                        if summary["recovery_score"] is None:
                            summary["recovery_score"] = latest.get("readiness_score")
            except Exception as e:
                logger.warning("Oura readiness aggregation error: %s", e)

            try:
                sleep = await self._oura.get_sleep(days=1)
                if sleep.get("success") and sleep.get("data"):
                    latest = sleep["data"][-1] if sleep["data"] else None
                    if latest:
                        if summary["sleep_hours"] is None:
                            summary["sleep_hours"] = latest.get("total_sleep_hours")
                        if summary["sleep_quality"] is None:
                            summary["sleep_quality"] = latest.get("sleep_score")
            except Exception as e:
                logger.warning("Oura sleep aggregation error: %s", e)

            try:
                activity = await self._oura.get_activity(days=1)
                if activity.get("success") and activity.get("data"):
                    latest = activity["data"][-1] if activity["data"] else None
                    if latest:
                        summary["activity_score"] = latest.get("activity_score")
            except Exception as e:
                logger.warning("Oura activity aggregation error: %s", e)

        # Live wearable fan-in (Theora W300 glasses, Veepoo wristband,
        # any BLE PPG source flowing into perception). Operator report
        # 2026-06-07: the chat tool used to answer "no current data"
        # when neither Whoop nor Oura was connected even though the
        # W300 was actively streaming HR. Surfacing the perception
        # frame's fresh wearable reading here closes the gap so chat
        # and WebUI agree on what "current heart rate" means.
        snap = self._live_wearable_snapshot()
        if snap:
            hr = snap.get("heart_rate")
            hr_source = snap.get("heart_rate_source")
            if hr:
                summary["current_hr"] = int(hr)
                if hr_source:
                    summary["current_hr_source"] = str(hr_source)
                # Treat the fresh wearable HR as the authoritative
                # "resting"-slot fallback when Whoop/Oura haven't
                # contributed one. The UI label for ``resting_hr``
                # is "current resting estimate" — a fresh PPG sample
                # is a better answer than "null" for that slot.
                if summary["resting_hr"] is None:
                    summary["resting_hr"] = int(hr)
                if hr_source and str(hr_source) not in summary["sources"]:
                    summary["sources"].append(str(hr_source))
            spo2 = snap.get("spo2")
            spo2_source = snap.get("spo2_source")
            if spo2:
                summary["current_spo2"] = int(spo2)
                if spo2_source:
                    summary["current_spo2_source"] = str(spo2_source)
                if spo2_source and str(spo2_source) not in summary["sources"]:
                    summary["sources"].append(str(spo2_source))

        # Glasses-derived week-over-week fallback (operator report
        # 2026-06-13). When NO third-party wearable (Whoop/Oura)
        # contributed the trend fields, derive a real 7-day vitals
        # trend from the persisted glasses/wristband samples so
        # "how were my vitals this week?" stops returning "no data".
        # When a cloud source IS connected, behaviour is unchanged —
        # we only attach the additive glasses trend block, never
        # overwrite cloud-mirror values.
        if not self._has_third_party_trend_source(summary):
            trend = self._build_glasses_vitals_trend(days=7)
            if trend is not None:
                summary["vitals_trend"] = trend
                # Fill the resting-HR slot from the glasses week when
                # neither a cloud source nor a fresh live sample did.
                if summary["resting_hr"] is None and trend.get("resting_hr_estimate"):
                    summary["resting_hr"] = trend["resting_hr_estimate"]
                for src in trend.get("sources", []):
                    if src and src not in summary["sources"]:
                        summary["sources"].append(src)

        return summary

    async def get_sleep_trend(self, days: int = 7) -> list[dict[str, Any]]:
        """Daily sleep entries from any connected source."""
        entries: list[dict[str, Any]] = []

        if self._whoop and self._whoop.connected:
            try:
                result = await self._whoop.get_sleep(days=days)
                if result.get("success") and result.get("data"):
                    for entry in result["data"]:
                        entries.append({**entry, "source": "whoop"})
            except Exception as e:
                logger.warning("Whoop sleep trend error: %s", e)

        if self._oura and self._oura.connected:
            try:
                result = await self._oura.get_sleep(days=days)
                if result.get("success") and result.get("data"):
                    for entry in result["data"]:
                        entries.append({**entry, "source": "oura"})
            except Exception as e:
                logger.warning("Oura sleep trend error: %s", e)

        entries.sort(key=lambda e: e.get("date", ""))
        return entries

    async def get_recovery_trend(self, days: int = 7) -> list[dict[str, Any]]:
        """Daily recovery/readiness scores from any connected source."""
        entries: list[dict[str, Any]] = []

        if self._whoop and self._whoop.connected:
            try:
                result = await self._whoop.get_cycles(days=days)
                if result.get("success") and result.get("data"):
                    for entry in result["data"]:
                        entries.append({
                            "date": entry.get("date"),
                            "score": entry.get("strain"),
                            "type": "strain",
                            "source": "whoop",
                        })
            except Exception as e:
                logger.warning("Whoop recovery trend error: %s", e)

        if self._oura and self._oura.connected:
            try:
                result = await self._oura.get_readiness(days=days)
                if result.get("success") and result.get("data"):
                    for entry in result["data"]:
                        entries.append({
                            "date": entry.get("date"),
                            "score": entry.get("readiness_score"),
                            "type": "readiness",
                            "source": "oura",
                        })
            except Exception as e:
                logger.warning("Oura recovery trend error: %s", e)

        entries.sort(key=lambda e: e.get("date", ""))
        return entries

    async def execute(
        self,
        endpoint_id: str,
        args: dict,
        vault: dict | None = None,
    ) -> dict[str, Any]:
        """Skill executor interface — called by SkillExecutor.

        Lane 05 : signature gained the third ``vault`` arg so the
        skill executor's standard ``await impl.execute(endpoint_id,
        args, self._vault)`` call site works against this aggregator.
        Pre-fix the signature only accepted two positional args and
        every dispatch raised TypeError — health_data was effectively
        dead in chat regardless of manifest.
        """
        del vault  # No vault credential needed; per-platform clients
        # carry their own OAuth tokens via OAuthManager.
        dispatch = {
            "health_summary": self.get_health_summary,
            "sleep_trend": self.get_sleep_trend,
            "recovery_trend": self.get_recovery_trend,
            "vitals_trend": self.get_vitals_trend,
            "health_history": self.get_health_history,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {
                "success": False,
                "status_code": 404,
                "data": None,
                "error": f"Unknown endpoint: {endpoint_id}",
            }
        # The aggregator's helper kwargs are limited to ``days`` (int).
        # Drop the executor's hidden ``_feral_require_sandbox`` flag and
        # any other unknown kwargs so the helper signature is clean.
        clean_args = {k: v for k, v in (args or {}).items() if not k.startswith("_")}
        result = await fn(**clean_args)
        if isinstance(result, dict):
            # health_summary returns a flat dict (not the {success,
            # data} envelope); wrap it.
            if "success" in result:
                return result
            return {"success": True, "status_code": 200, "data": result, "error": None}
        return {"success": True, "status_code": 200, "data": result, "error": None}

    async def close(self):
        if self._whoop:
            await self._whoop.close()
        if self._oura:
            await self._oura.close()
