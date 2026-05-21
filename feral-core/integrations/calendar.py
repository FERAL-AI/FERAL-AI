"""
FERAL Calendar Integration — Google Calendar API v3 + ICS Fallback
====================================================================
Manages events, scheduling, and ambient-strip "next event" awareness.
Falls back to raw ICS parsing when no Google OAuth is available.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("feral.integrations.calendar")

GCAL_API = "https://www.googleapis.com/calendar/v3"
CALENDAR_ID = "primary"


class CalendarIntegration:
    """
    Google Calendar API v3 integration with ICS URL fallback.
    Uses OAuthManager for Google token management.
    """

    def __init__(self, oauth_manager=None):
        self._oauth = oauth_manager
        self._http = httpx.AsyncClient(base_url=GCAL_API, timeout=15.0)
        self._ics_url: Optional[str] = os.environ.get("FERAL_CALENDAR_ICS")

    async def _headers(self) -> Optional[dict]:
        if not self._oauth:
            return None
        token = await self._oauth.get_token("google")
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    @property
    def connected(self) -> bool:
        """Live-verified Google Calendar connection.

        Pre-Lane-10 we returned ``True`` whenever an ICS URL was set,
        which lied about Google Calendar specifically — the ICS feed
        is a read-only side-channel for users without OAuth, not a
        valid Google Calendar credential. The truthful surface now:

        * If a Google probe has run recently → reflect that result.
        * Otherwise fall back to the OAuth ``is_connected`` check.

        ICS readiness is exposed separately via :attr:`ics_configured`
        so the WebUI can render "Read-only ICS feed" alongside (not
        as a substitute for) the Google connection.
        """
        from integrations._probe_status import is_connected_cached

        token_present = (
            self._oauth is not None and self._oauth.is_connected("google")
        )
        return is_connected_cached("google", fallback=token_present)

    @property
    def ics_configured(self) -> bool:
        return self._ics_url is not None

    async def probe_connected(self) -> bool:
        from integrations._probe_status import refresh

        result = await refresh("google",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    @property
    def _use_ics(self) -> bool:
        return self._ics_url is not None and (
            self._oauth is None or not self._oauth.is_connected("google")
        )

    async def execute(self, endpoint_id: str, args: dict, vault: dict = None) -> dict:
        """Skill executor interface — called by SkillExecutor."""
        dispatch = {
            "list_events": self.list_events,
            "get_today": self.get_today,
            "create_event": self.create_event,
            "next_event": self.next_event,
            "search_events": self.search_events,
            "delete_event": self.delete_event,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    # ── ICS helpers ────────────────────────────────────────────────

    async def _fetch_ics_events(self) -> list[dict[str, Any]]:
        """Parse VEVENT blocks from a remote ICS URL using regex only."""
        if not self._ics_url:
            return []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(self._ics_url)
                resp.raise_for_status()
                text = resp.text
        except Exception as e:
            logger.warning("ICS fetch failed: %s", e)
            return []

        events: list[dict[str, Any]] = []
        blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.DOTALL)
        for block in blocks:
            def _field(name: str) -> str:
                m = re.search(rf"^{name}[^:]*:(.+)$", block, re.MULTILINE)
                return m.group(1).strip() if m else ""

            dtstart_raw = _field("DTSTART")
            dtend_raw = _field("DTEND")
            events.append({
                "id": _field("UID"),
                "summary": _field("SUMMARY"),
                "description": _field("DESCRIPTION"),
                "start": dtstart_raw,
                "end": dtend_raw,
                "location": _field("LOCATION"),
            })
        return events

    @staticmethod
    def _ics_dt(raw: str) -> Optional[datetime]:
        """Best-effort parse of an ICS datetime string."""
        raw = raw.replace("Z", "+00:00")
        for fmt in ("%Y%m%dT%H%M%S%z", "%Y%m%dT%H%M%S", "%Y%m%d"):
            try:
                return datetime.strptime(raw[:len(fmt) + 4], fmt)
            except (ValueError, IndexError):
                continue
        return None

    # ── Google Calendar helpers ────────────────────────────────────

    @staticmethod
    def _parse_gcal_event(ev: dict) -> dict:
        start = ev.get("start", {})
        end = ev.get("end", {})
        return {
            "id": ev.get("id", ""),
            "summary": ev.get("summary", "(No title)"),
            "description": ev.get("description", ""),
            "start": start.get("dateTime", start.get("date", "")),
            "end": end.get("dateTime", end.get("date", "")),
            "location": ev.get("location", ""),
            "html_link": ev.get("htmlLink", ""),
        }

    # ── Endpoints ──────────────────────────────────────────────────

    async def list_events(self, days_ahead: int = 7, **kwargs) -> dict:
        now = datetime.now(timezone.utc)
        time_max = now + timedelta(days=days_ahead)

        if self._use_ics:
            try:
                all_events = await self._fetch_ics_events()
                filtered = []
                for ev in all_events:
                    dt = self._ics_dt(ev["start"])
                    if dt and now <= dt.replace(tzinfo=timezone.utc) <= time_max:
                        filtered.append(ev)
                filtered.sort(key=lambda e: e["start"])
                return {"success": True, "data": {"events": filtered, "source": "ics"}}
            except Exception as e:
                return {"success": False, "error": str(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Calendar"}
        try:
            resp = await self._http.get(
                f"/calendars/{CALENDAR_ID}/events",
                params={
                    "timeMin": now.isoformat(),
                    "timeMax": time_max.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 50,
                },
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            events = [self._parse_gcal_event(e) for e in items]
            return {"success": True, "data": {"events": events, "source": "google"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_today(self, **kwargs) -> dict:
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        if self._use_ics:
            try:
                all_events = await self._fetch_ics_events()
                today: list[dict] = []
                for ev in all_events:
                    dt = self._ics_dt(ev["start"])
                    if dt and start_of_day <= dt.replace(tzinfo=timezone.utc) < end_of_day:
                        today.append(ev)
                today.sort(key=lambda e: e["start"])
                return {"success": True, "data": {"events": today, "date": now.strftime("%Y-%m-%d"), "source": "ics"}}
            except Exception as e:
                return {"success": False, "error": str(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Calendar"}
        try:
            resp = await self._http.get(
                f"/calendars/{CALENDAR_ID}/events",
                params={
                    "timeMin": start_of_day.isoformat(),
                    "timeMax": end_of_day.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            events = [self._parse_gcal_event(e) for e in items]
            return {"success": True, "data": {"events": events, "date": now.strftime("%Y-%m-%d"), "source": "google"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def create_event(self, title: str = "", start: str = "", end: str = "", description: str = "", **kwargs) -> dict:
        if self._use_ics:
            return {"success": False, "error": "Cannot create events via ICS — connect Google Calendar"}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Calendar"}
        try:
            body: dict[str, Any] = {
                "summary": title,
                "start": {"dateTime": start, "timeZone": "UTC"},
                "end": {"dateTime": end, "timeZone": "UTC"},
            }
            if description:
                body["description"] = description
            resp = await self._http.post(
                f"/calendars/{CALENDAR_ID}/events",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            created = resp.json()
            return {"success": True, "data": self._parse_gcal_event(created)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def next_event(self, **kwargs) -> dict:
        """Nearest upcoming event — optimised for ambient strip display."""
        now = datetime.now(timezone.utc)

        if self._use_ics:
            try:
                all_events = await self._fetch_ics_events()
                nearest: Optional[dict] = None
                nearest_dt: Optional[datetime] = None
                for ev in all_events:
                    dt = self._ics_dt(ev["start"])
                    if dt and dt.replace(tzinfo=timezone.utc) >= now:
                        if nearest_dt is None or dt.replace(tzinfo=timezone.utc) < nearest_dt:
                            nearest = ev
                            nearest_dt = dt.replace(tzinfo=timezone.utc)
                if nearest:
                    return {"success": True, "data": nearest}
                return {"success": True, "data": {"message": "No upcoming events"}}
            except Exception as e:
                return {"success": False, "error": str(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Calendar"}
        try:
            resp = await self._http.get(
                f"/calendars/{CALENDAR_ID}/events",
                params={
                    "timeMin": now.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 1,
                },
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if items:
                return {"success": True, "data": self._parse_gcal_event(items[0])}
            return {"success": True, "data": {"message": "No upcoming events"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def search_events(self, query: str = "", days_back: int = 30, **kwargs) -> dict:
        if self._use_ics:
            try:
                all_events = await self._fetch_ics_events()
                q = query.lower()
                matched = [e for e in all_events if q in e.get("summary", "").lower() or q in e.get("description", "").lower()]
                return {"success": True, "data": {"events": matched, "query": query, "source": "ics"}}
            except Exception as e:
                return {"success": False, "error": str(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Calendar"}
        try:
            now = datetime.now(timezone.utc)
            resp = await self._http.get(
                f"/calendars/{CALENDAR_ID}/events",
                params={
                    "q": query,
                    "timeMin": (now - timedelta(days=days_back)).isoformat(),
                    "timeMax": (now + timedelta(days=days_back)).isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 25,
                },
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            events = [self._parse_gcal_event(e) for e in items]
            return {"success": True, "data": {"events": events, "query": query, "source": "google"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def delete_event(self, event_id: str = "", **kwargs) -> dict:
        if self._use_ics:
            return {"success": False, "error": "Cannot delete events via ICS — connect Google Calendar"}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Calendar"}
        try:
            resp = await self._http.delete(
                f"/calendars/{CALENDAR_ID}/events/{event_id}",
                headers=headers,
            )
            resp.raise_for_status()
            return {"success": True, "data": {"deleted": event_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        await self._http.aclose()
