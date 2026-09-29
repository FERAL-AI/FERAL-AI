"""Places lookup backed by Google Maps Grounding Lite.

"Find a café near me" had no answer path: the only geo code was the
weather skill's city-name geocoder, and browsing a map site with the
browser tools is slow and breaks on every layout change.

Grounding Lite is an MCP server (``https://mapstools.googleapis.com/mcp``)
rather than a REST API, so this reaches it through the MCP client the
brain already ships. The connection is held privately by this skill
instead of being registered with ``MCPClientManager`` on purpose: a
registered server publishes its own tools into the model's tool list,
and the model should see one stable ``places__find_places`` contract
rather than three Google tools whose shape we do not control. The
operator's sandbox policy is still consulted, because a remote MCP
endpoint is exactly what ``mcp.allowed_servers`` exists to gate.

Attribution is not optional. The Maps Platform terms allow grounded
output to reach an end user only when the Google Maps source links travel
with it and are viewable within one user interaction. This product speaks
results through glasses that have no screen, so every result carries its
sources verbatim for the phone to render while the glasses talk. A result
with no sources is unusable rather than merely unattributed, and says so.
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Dict, Optional

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.places")

MCP_SERVER_NAME = "google_maps_grounding"
MCP_URL = "https://mapstools.googleapis.com/mcp"
DEFAULT_RADIUS_M = 1500
MAX_RESULTS = 10


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle metres between two fixes.

    Computed here rather than asked of the API: Grounding Lite documents
    no distance field, and the distance that matters is from where the
    wearer actually is, which only this brain knows.
    """
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _first(mapping: dict, *paths, default=None):
    """First present value among dotted paths, or ``default``."""
    for path in paths:
        cur: Any = mapping
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if cur is not None:
            return cur
    return default


def _sources_from(place: dict) -> list[dict]:
    """Every Google Maps link this place carries, as {title, url}.

    Shape-tolerant on purpose: the published docs do not fix the
    response schema, so anything that looks like a Maps URL is kept
    rather than dropped, and the title is passed through exactly as
    received because the terms require it displayed unaltered.
    """
    out: list[dict] = []
    seen: set[str] = set()

    def _add(url: Any, title: Any = None) -> None:
        if not isinstance(url, str) or not url.startswith("http") or url in seen:
            return
        seen.add(url)
        out.append({"title": str(title) if title else "Google Maps", "url": url})

    attribution = place.get("attribution")
    if isinstance(attribution, dict):
        _add(attribution.get("url") or attribution.get("uri"), attribution.get("title"))
    elif isinstance(attribution, list):
        for item in attribution:
            if isinstance(item, dict):
                _add(item.get("url") or item.get("uri"), item.get("title"))

    links = place.get("googleMapsLinks")
    if isinstance(links, dict):
        for key, value in links.items():
            _add(value, place.get("name") or key)
    _add(place.get("googleMapsUri"), place.get("name"))
    return out


@register_skill
class PlacesSkill(BaseSkill):
    """Nearby places, with the sources the terms require."""

    skill_id = "places"

    def __init__(self) -> None:
        self._conn = None

    # ── connection ───────────────────────────────────────────────

    async def _connection(self, api_key: str):
        if self._conn is not None and getattr(self._conn, "is_connected", False):
            return self._conn
        from mcp.client import MCPServerConfig, MCPServerConnection

        config = MCPServerConfig(
            name=MCP_SERVER_NAME,
            transport="http",
            url=MCP_URL,
            headers={"X-Goog-Api-Key": api_key},
        )
        conn = MCPServerConnection(MCP_SERVER_NAME, config)
        if not await conn.connect():
            return None
        self._conn = conn
        return conn

    @staticmethod
    def _policy_allows() -> bool:
        """Respect ``mcp.allowed_servers`` even though we bypass the manager."""
        try:
            from api.state import state
            policy = getattr(state, "sandbox_policy", None)
            check = getattr(policy, "can_use_mcp_server", None) if policy else None
            return check(MCP_SERVER_NAME) if callable(check) else True
        except Exception:
            return True

    @staticmethod
    def _origin(args: Dict[str, Any]) -> Optional[dict]:
        """Where to search from: explicit coordinates, else last known fix."""
        lat, lon = args.get("lat"), args.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return {"lat": float(lat), "lon": float(lon), "source": "explicit"}
        try:
            from api.state import state
            fix = state.perception.last_known_location()
        except Exception:
            fix = None
        if isinstance(fix, dict) and fix.get("lat") is not None:
            return {
                "lat": float(fix["lat"]), "lon": float(fix["lon"]),
                "accuracy_m": fix.get("accuracy_m"), "ts": fix.get("ts"),
                "source": "device",
            }
        return None

    # ── entry point ──────────────────────────────────────────────

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str],
    ) -> Dict[str, Any]:
        dispatch = {
            "find_places": self._find_places,
        }
        handler = dispatch.get(endpoint_id)
        if handler is None:
            return {"ok": False, "error": f"unknown endpoint {endpoint_id!r}"}
        return await handler(args, vault)

    async def _find_places(
        self, args: Dict[str, Any], vault: Dict[str, str],
    ) -> Dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query is required, e.g. 'café'"}

        api_key = self.get_api_key(vault, fallback_env="GOOGLE_MAPS_API_KEY")
        if not api_key:
            return {
                "ok": False, "reason": "not_configured",
                "error": (
                    "Places lookup needs a Google Maps Platform API key with "
                    "Maps Grounding Lite enabled. Set GOOGLE_MAPS_API_KEY."
                ),
            }
        if not self._policy_allows():
            return {
                "ok": False, "reason": "blocked_by_policy",
                "error": (
                    f"the sandbox policy does not permit the MCP server "
                    f"{MCP_SERVER_NAME!r} (mcp.allowed_servers)"
                ),
            }

        origin = self._origin(args)
        if origin is None:
            # Not an error. Location is opt-in and off by default, so the
            # honest answer is to ask, not to fail.
            return {
                "ok": False, "reason": "location_unknown",
                "message": (
                    "I don't have your location. Share it from the phone, or "
                    "tell me the area to search in."
                ),
            }

        radius = int(args.get("radius_m") or DEFAULT_RADIUS_M)
        limit = max(1, min(int(args.get("limit") or 5), MAX_RESULTS))

        conn = await self._connection(api_key)
        if conn is None:
            return {"ok": False, "reason": "unavailable",
                    "error": "could not reach Google Maps Grounding Lite"}

        result = await conn.call_tool("search_places", {
            "text_query": query,
            "location_bias": {"circle": {
                "center": {"latitude": origin["lat"], "longitude": origin["lon"]},
                "radius_meters": radius,
            }},
        })
        # MCP reports a refused call as a 200 carrying `isError: true`,
        # not as a transport error. Checking only the transport made a
        # permission failure look like a successful search that happened
        # to find nothing, which is the most dangerous shape a failure
        # can take: it reads as "there are no cafes near you".
        if isinstance(result, dict) and result.get("isError"):
            text = ""
            for block in result.get("content") or []:
                if isinstance(block, dict) and block.get("text"):
                    text = str(block["text"])
                    break
            return {
                "ok": False, "reason": "upstream_error",
                "error": text or "the places provider refused the request",
                "detail": json.dumps(result, default=str)[:512],
            }

        if isinstance(result, dict) and result.get("error"):
            upstream = {"ok": False, "reason": "upstream_error",
                        "error": str(result["error"])}
            if result.get("detail"):
                upstream["detail"] = str(result["detail"])[:512]
            return upstream

        if os.environ.get("FERAL_PLACES_DEBUG"):
            # The response schema is not published, so when a call comes
            # back empty the only way to tell "no results" from "parsed
            # the wrong shape" is to look at what actually arrived.
            logger.info("places raw result: %s", json.dumps(result, default=str)[:4000])

        places = self._normalise(result, origin, limit)
        unattributed = [p["name"] for p in places if not p["sources"]]
        return {
            "ok": True,
            "query": query,
            "origin": origin,
            "radius_m": radius,
            "count": len(places),
            "places": places,
            # The phone must render these for the same interaction in which
            # the glasses speak the answer, or we are outside the terms.
            "attribution_required": True,
            "unattributed": unattributed,
        }

    # ── response shaping ─────────────────────────────────────────

    @staticmethod
    def _payload(result: Any) -> list[dict]:
        """Pull the places list out of an MCP tool result.

        Written defensively because Grounding Lite's response schema is
        not published: an MCP result may carry JSON directly or a text
        block holding JSON, and the places may sit under any of a few
        plausible keys.
        """
        candidates: list[Any] = []
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        try:
                            candidates.append(json.loads(block["text"]))
                        except (ValueError, TypeError):
                            continue
            candidates.append(result)
        for candidate in candidates:
            if isinstance(candidate, dict):
                for key in ("places", "results", "searchResults"):
                    value = candidate.get(key)
                    if isinstance(value, list):
                        return [p for p in value if isinstance(p, dict)]
            if isinstance(candidate, list):
                return [p for p in candidate if isinstance(p, dict)]
        return []

    def _normalise(self, result: Any, origin: dict, limit: int) -> list[dict]:
        out: list[dict] = []
        for place in self._payload(result)[:limit]:
            lat = _first(place, "location.latitude", "location.lat", "latitude")
            lon = _first(place, "location.longitude", "location.lng", "longitude")
            row: dict = {
                "name": _first(place, "displayName.text", "name", "title", default=""),
                "address": _first(
                    place, "formattedAddress", "shortFormattedAddress", "address",
                    default="",
                ),
                # Present only when the API returned them. Never defaulted:
                # a café with no rating must not read as a café rated 0.
                "rating": _first(place, "rating"),
                "open_now": _first(
                    place, "currentOpeningHours.openNow", "regularOpeningHours.openNow",
                    "openNow",
                ),
                "sources": _sources_from(place),
            }
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                row["lat"], row["lon"] = float(lat), float(lon)
                row["distance_m"] = round(
                    _haversine_m(origin["lat"], origin["lon"], float(lat), float(lon))
                )
            out.append(row)
        out.sort(key=lambda r: r.get("distance_m", float("inf")))
        return out
