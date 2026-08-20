"""
FERAL Spotify Integration — Real Spotify Web API
===================================================
Full playback control, search, and queue management via OAuth2 PKCE.
Registers as a native skill implementation so the LLM can control
music through the standard tool-calling pipeline.
"""

from __future__ import annotations
import logging
from typing import Optional

import httpx

logger = logging.getLogger("feral.integrations.spotify")

SPOTIFY_API = "https://api.spotify.com/v1"


class SpotifyIntegration:
    """
    Native Spotify Web API integration.
    Uses OAuthManager for token management.
    """

    def __init__(self, oauth_manager=None):
        self._oauth = oauth_manager
        self._http = httpx.AsyncClient(base_url=SPOTIFY_API, timeout=10.0)

    async def _headers(self) -> Optional[dict]:
        if not self._oauth:
            return None
        token = await self._oauth.get_token("spotify")
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        token_present = (
            self._oauth is not None and self._oauth.is_connected("spotify")
        )
        return is_connected_cached("spotify", fallback=token_present)

    async def probe_connected(self) -> bool:
        """Force a live API round-trip to verify Spotify connectivity.

        Updates the shared probe-status cache so subsequent
        ``connected`` reads reflect the live result.
        """
        from integrations._probe_status import refresh

        result = await refresh("spotify",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    async def execute(self, endpoint_id: str, args: dict, vault: dict = None) -> dict:
        """Skill executor interface — called by SkillExecutor when matching spotify_music."""
        dispatch = {
            "now_playing": self.now_playing,
            "play_pause": self.play_pause,
            "pause": self.pause,
            "next_track": self.next_track,
            "previous_track": self.previous_track,
            "search": self.search,
            "queue_track": self.queue_track,
            "get_playlists": self.get_playlists,
            "play_playlist": self.play_playlist,
            "set_volume": self.set_volume,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    async def now_playing(self, **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Spotify"}
        try:
            resp = await self._http.get("/me/player/currently-playing", headers=headers)
            if resp.status_code == 204:
                return {"success": True, "data": {"playing": False, "message": "Nothing playing"}}
            resp.raise_for_status()
            data = resp.json()
            item = data.get("item", {})
            return {
                "success": True,
                "data": {
                    "playing": data.get("is_playing", False),
                    "track": item.get("name", "Unknown"),
                    "artist": ", ".join(a["name"] for a in item.get("artists", [])),
                    "album": item.get("album", {}).get("name", ""),
                    "progress_ms": data.get("progress_ms", 0),
                    "duration_ms": item.get("duration_ms", 0),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def play_pause(self, **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            state = await self._http.get("/me/player", headers=headers)
            if state.status_code == 200:
                is_playing = state.json().get("is_playing", False)
                if is_playing:
                    await self._http.put("/me/player/pause", headers=headers)
                    return {"success": True, "data": {"action": "paused"}}
                else:
                    await self._http.put("/me/player/play", headers=headers)
                    return {"success": True, "data": {"action": "playing"}}
            return {"success": False, "error": "No active device"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Spotify answers a player command that cannot be carried out with an
    # HTTP error and a machine-readable reason, most often 404
    # NO_ACTIVE_DEVICE (nothing is playing anywhere) or 403
    # PREMIUM_REQUIRED. ``pause``, ``next_track``, ``previous_track``,
    # ``play_playlist`` and ``set_volume`` used to fire the request and
    # return {"success": True} without ever reading the response, so the
    # brain told the user it had paused their music when nothing had
    # happened. ``play_pause`` and ``queue_track`` in this same class
    # always checked; this is the missing half.
    @staticmethod
    def _player_error(resp, action: str) -> Optional[dict]:
        """``None`` when the command was carried out, else a failure envelope."""
        if resp is None:
            return {"success": False, "error": f"No response from Spotify for {action}."}
        code = getattr(resp, "status_code", None)
        if code is not None and 200 <= code < 300:
            return None
        reason = ""
        message = ""
        try:
            err = (resp.json() or {}).get("error") or {}
            reason = str(err.get("reason") or "")
            message = str(err.get("message") or "")
        except Exception:
            pass
        if reason == "NO_ACTIVE_DEVICE" or code == 404:
            return {
                "success": False,
                "error": (
                    f"Cannot {action}: Spotify has no active device. Start playback "
                    "on a phone, desktop app or speaker first, then try again."
                ),
            }
        if reason == "PREMIUM_REQUIRED" or code == 403:
            return {"success": False, "error": f"Cannot {action}: Spotify Premium is required."}
        if code == 401:
            return {"success": False, "error": f"Cannot {action}: the Spotify token is expired or invalid."}
        detail = message or reason or f"HTTP {code}"
        return {"success": False, "error": f"Cannot {action}: {detail}"}

    async def pause(self, **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            resp = await self._http.put("/me/player/pause", headers=headers)
            failure = self._player_error(resp, "pause")
            if failure:
                return failure
            return {"success": True, "data": {"action": "paused"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def next_track(self, **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            resp = await self._http.post("/me/player/next", headers=headers)
            failure = self._player_error(resp, "skip to the next track")
            if failure:
                return failure
            return {"success": True, "data": {"action": "skipped_to_next"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def previous_track(self, **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            resp = await self._http.post("/me/player/previous", headers=headers)
            failure = self._player_error(resp, "skip to the previous track")
            if failure:
                return failure
            return {"success": True, "data": {"action": "skipped_to_previous"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def search(self, query: str = "", type: str = "track", limit: int = 5, **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            resp = await self._http.get(
                "/search",
                params={"q": query, "type": type, "limit": limit},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get(f"{type}s", {}).get("items", []):
                entry = {"name": item.get("name", ""), "uri": item.get("uri", "")}
                if type == "track":
                    entry["artist"] = ", ".join(a["name"] for a in item.get("artists", []))
                    entry["album"] = item.get("album", {}).get("name", "")
                results.append(entry)
            return {"success": True, "data": {"results": results, "query": query}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def queue_track(self, uri: str = "", **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            resp = await self._http.post(
                "/me/player/queue", params={"uri": uri}, headers=headers,
            )
            resp.raise_for_status()
            return {"success": True, "data": {"queued": uri}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_playlists(self, limit: int = 10, **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            resp = await self._http.get(
                "/me/playlists", params={"limit": limit}, headers=headers,
            )
            resp.raise_for_status()
            playlists = [
                {"name": p["name"], "uri": p["uri"], "tracks": p.get("tracks", {}).get("total", 0)}
                for p in resp.json().get("items", [])
            ]
            return {"success": True, "data": {"playlists": playlists}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def play_playlist(self, uri: str = "", **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            resp = await self._http.put(
                "/me/player/play",
                json={"context_uri": uri},
                headers=headers,
            )
            failure = self._player_error(resp, "play that playlist")
            if failure:
                return failure
            return {"success": True, "data": {"playing_playlist": uri}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_volume(self, volume_percent: int = 50, **kwargs) -> dict:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected"}
        try:
            resp = await self._http.put(
                "/me/player/volume",
                params={"volume_percent": max(0, min(100, volume_percent))},
                headers=headers,
            )
            failure = self._player_error(resp, "set the volume")
            if failure:
                return failure
            return {"success": True, "data": {"volume": volume_percent}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        await self._http.aclose()
