"""
FERAL Microsoft 365 Integration — Microsoft Graph API
========================================================
Mail, Calendar, and OneDrive via the Microsoft Graph REST API.
OAuth2 with MSAL-style token flow via OAuthManager.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("feral.integrations.microsoft365")

GRAPH_API = "https://graph.microsoft.com/v1.0"


class Microsoft365Integration:
    """Microsoft Graph API integration for Mail, Calendar, and OneDrive."""

    def __init__(self, oauth_manager: Any = None):
        self._oauth = oauth_manager
        self._http = httpx.AsyncClient(base_url=GRAPH_API, timeout=20.0)

    async def _headers(self) -> Optional[dict[str, str]]:
        if not self._oauth:
            return None
        token = await self._oauth.get_token("microsoft")
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        token_present = (
            self._oauth is not None and self._oauth.is_connected("microsoft")
        )
        return is_connected_cached("microsoft", fallback=token_present)

    async def probe_connected(self) -> bool:
        """Force a live Microsoft Graph ``/me`` probe."""
        from integrations._probe_status import refresh

        result = await refresh("microsoft",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    async def execute(self, endpoint_id: str, args: dict[str, Any], vault: dict[str, str] | None = None) -> dict[str, Any]:
        dispatch = {
            "list_mail": self.list_mail,
            "send_mail": self.send_mail,
            "list_events": self.list_events,
            "create_event": self.create_event,
            "list_files": self.list_files,
            "search_files": self.search_files,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    # ── Mail ──────────────────────────────────────────────────────

    async def list_mail(self, max_results: int = 20, folder: str = "inbox", **_kw: Any) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Microsoft 365"}
        try:
            resp = await self._http.get(
                f"/me/mailFolders/{folder}/messages",
                params={
                    "$top": min(max_results, 50),
                    "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead",
                    "$orderby": "receivedDateTime desc",
                },
                headers=headers,
            )
            resp.raise_for_status()
            messages = resp.json().get("value", [])
            parsed = [
                {
                    "id": m.get("id", ""),
                    "subject": m.get("subject", ""),
                    "from": m.get("from", {}).get("emailAddress", {}).get("address", ""),
                    "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
                    "date": m.get("receivedDateTime", ""),
                    "preview": m.get("bodyPreview", ""),
                    "is_read": m.get("isRead", False),
                }
                for m in messages
            ]
            return {"success": True, "data": {"messages": parsed, "source": "microsoft"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def send_mail(
        self, to: str = "", subject: str = "", body: str = "", content_type: str = "Text", **_kw: Any
    ) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Microsoft 365"}
        try:
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": content_type, "content": body},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
                "saveToSentItems": True,
            }
            resp = await self._http.post("/me/sendMail", json=payload, headers=headers)
            resp.raise_for_status()
            return {"success": True, "data": {"sent_to": to, "subject": subject}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Calendar ──────────────────────────────────────────────────

    async def list_events(self, days_ahead: int = 7, max_results: int = 25, **_kw: Any) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Microsoft 365"}
        try:
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            end = now + timedelta(days=days_ahead)
            resp = await self._http.get(
                "/me/calendarView",
                params={
                    "startDateTime": now.isoformat(),
                    "endDateTime": end.isoformat(),
                    "$top": min(max_results, 50),
                    "$select": "id,subject,start,end,location,organizer,isAllDay",
                    "$orderby": "start/dateTime",
                },
                headers=headers,
            )
            resp.raise_for_status()
            events = resp.json().get("value", [])
            parsed = [
                {
                    "id": ev.get("id", ""),
                    "subject": ev.get("subject", ""),
                    "start": ev.get("start", {}).get("dateTime", ""),
                    "end": ev.get("end", {}).get("dateTime", ""),
                    "location": ev.get("location", {}).get("displayName", ""),
                    "is_all_day": ev.get("isAllDay", False),
                }
                for ev in events
            ]
            return {"success": True, "data": {"events": parsed, "source": "microsoft"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def create_event(
        self,
        subject: str = "",
        start: str = "",
        end: str = "",
        location: str = "",
        body: str = "",
        **_kw: Any,
    ) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Microsoft 365"}
        try:
            payload: dict[str, Any] = {
                "subject": subject,
                "start": {"dateTime": start, "timeZone": "UTC"},
                "end": {"dateTime": end, "timeZone": "UTC"},
            }
            if location:
                payload["location"] = {"displayName": location}
            if body:
                payload["body"] = {"contentType": "Text", "content": body}
            resp = await self._http.post("/me/events", json=payload, headers=headers)
            resp.raise_for_status()
            created = resp.json()
            return {
                "success": True,
                "data": {
                    "id": created.get("id", ""),
                    "subject": created.get("subject", ""),
                    "start": created.get("start", {}).get("dateTime", ""),
                    "end": created.get("end", {}).get("dateTime", ""),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── OneDrive ──────────────────────────────────────────────────

    async def list_files(self, folder_path: str = "", max_results: int = 25, **_kw: Any) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Microsoft 365"}
        try:
            if folder_path:
                url = f"/me/drive/root:/{folder_path}:/children"
            else:
                url = "/me/drive/root/children"
            resp = await self._http.get(
                url,
                params={
                    "$top": min(max_results, 100),
                    "$select": "id,name,size,lastModifiedDateTime,webUrl,file,folder",
                },
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json().get("value", [])
            parsed = [
                {
                    "id": f.get("id", ""),
                    "name": f.get("name", ""),
                    "size": f.get("size", 0),
                    "modified": f.get("lastModifiedDateTime", ""),
                    "web_url": f.get("webUrl", ""),
                    "is_folder": "folder" in f,
                }
                for f in items
            ]
            return {"success": True, "data": {"files": parsed, "source": "onedrive"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def search_files(self, query: str = "", max_results: int = 25, **_kw: Any) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Microsoft 365"}
        try:
            resp = await self._http.get(
                f"/me/drive/root/search(q='{query}')",
                params={
                    "$top": min(max_results, 50),
                    "$select": "id,name,size,lastModifiedDateTime,webUrl",
                },
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json().get("value", [])
            parsed = [
                {
                    "id": f.get("id", ""),
                    "name": f.get("name", ""),
                    "size": f.get("size", 0),
                    "modified": f.get("lastModifiedDateTime", ""),
                    "web_url": f.get("webUrl", ""),
                }
                for f in items
            ]
            return {"success": True, "data": {"files": parsed, "query": query, "source": "onedrive"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        await self._http.aclose()
