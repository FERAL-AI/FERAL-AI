"""
FERAL Google Contacts Integration — People API
=================================================
Resolve names to email addresses and phone numbers.
Enables "email Sarah" or "call John" by looking up contacts.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from integrations._http_errors import http_error_detail

logger = logging.getLogger("feral.integrations.google_contacts")

PEOPLE_API = "https://people.googleapis.com/v1"


def _parse_person(person: dict) -> dict[str, Any]:
    """Extract a flat contact record from a People API person resource."""
    names = person.get("names", [])
    name = names[0].get("displayName", "") if names else ""

    emails = [e.get("value", "") for e in person.get("emailAddresses", [])]
    phones = [p.get("value", "") for p in person.get("phoneNumbers", [])]

    orgs = person.get("organizations", [])
    company = orgs[0].get("name", "") if orgs else ""

    photos = person.get("photos", [])
    photo_url = photos[0].get("url", "") if photos else ""

    return {
        "resource_name": person.get("resourceName", ""),
        "name": name,
        "emails": emails,
        "phones": phones,
        "company": company,
        "photo_url": photo_url,
    }


class GoogleContactsIntegration:
    """Google People API integration for contact resolution."""

    def __init__(self, oauth_manager: Any = None):
        self._oauth = oauth_manager
        self._http = httpx.AsyncClient(timeout=15.0)

    async def _headers(self) -> Optional[dict[str, str]]:
        if not self._oauth:
            return None
        token = await self._oauth.get_token("google")
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        token_present = (
            self._oauth is not None and self._oauth.is_connected("google")
        )
        return is_connected_cached("google", fallback=token_present)

    async def probe_connected(self) -> bool:
        """Force a live Google ``/oauth2/v3/userinfo`` probe."""
        from integrations._probe_status import refresh

        result = await refresh("google",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    async def execute(self, endpoint_id: str, args: dict[str, Any], vault: dict[str, str] | None = None) -> dict[str, Any]:
        dispatch = {
            "search_contacts": self.search_contacts,
            "get_contact": self.get_contact,
            "list_contacts": self.list_contacts,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    async def search_contacts(self, query: str = "", max_results: int = 10, **_kw: Any) -> dict[str, Any]:
        """Search contacts by name, email, or phone number."""
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Contacts"}
        try:
            resp = await self._http.get(
                f"{PEOPLE_API}/people:searchContacts",
                params={
                    "query": query,
                    "readMask": "names,emailAddresses,phoneNumbers,organizations,photos",
                    "pageSize": min(max_results, 30),
                },
                headers=headers,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            contacts = [_parse_person(r.get("person", {})) for r in results]
            return {"success": True, "data": {"contacts": contacts, "query": query}}
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def get_contact(self, resource_name: str = "", **_kw: Any) -> dict[str, Any]:
        """Get a single contact by resource name (e.g., 'people/c1234')."""
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Contacts"}
        try:
            resp = await self._http.get(
                f"{PEOPLE_API}/{resource_name}",
                params={"personFields": "names,emailAddresses,phoneNumbers,organizations,photos,addresses,birthdays"},
                headers=headers,
            )
            resp.raise_for_status()
            contact = _parse_person(resp.json())
            return {"success": True, "data": contact}
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def list_contacts(self, page_size: int = 20, page_token: str = "", **_kw: Any) -> dict[str, Any]:
        """List the user's contacts (connections)."""
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Contacts"}
        try:
            params: dict[str, Any] = {
                "personFields": "names,emailAddresses,phoneNumbers,organizations,photos",
                "pageSize": min(page_size, 100),
                "sortOrder": "LAST_MODIFIED_DESCENDING",
            }
            if page_token:
                params["pageToken"] = page_token
            resp = await self._http.get(
                f"{PEOPLE_API}/people/me/connections",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            connections = data.get("connections", [])
            contacts = [_parse_person(c) for c in connections]
            return {
                "success": True,
                "data": {
                    "contacts": contacts,
                    "total": data.get("totalPeople", len(contacts)),
                    "next_page_token": data.get("nextPageToken"),
                },
            }
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def close(self):
        await self._http.aclose()
