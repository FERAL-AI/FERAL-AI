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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("feral.integrations.health")

WHOOP_API = "https://api.prod.whoop.com/developer/v1"
OURA_API = "https://api.ouraring.com/v2/usercollection"


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

    def __init__(self, whoop: Optional[WhoopClient] = None,
                 oura: Optional[OuraClient] = None):
        self._whoop = whoop
        self._oura = oura

    @property
    def sources(self) -> list[str]:
        s = []
        if self._whoop and self._whoop.connected:
            s.append("whoop")
        if self._oura and self._oura.connected:
            s.append("oura")
        return s

    async def get_health_summary(self) -> dict[str, Any]:
        """Merge data from all connected platforms into a unified dict."""
        summary: dict[str, Any] = {
            "sleep_hours": None,
            "sleep_quality": None,
            "recovery_score": None,
            "resting_hr": None,
            "hrv": None,
            "readiness": None,
            "activity_score": None,
            "strain": None,
            "sources": self.sources,
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

        Lane 05 W5: signature gained the third ``vault`` arg so the
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
