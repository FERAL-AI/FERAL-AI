"""Spotify player commands must not claim success they did not achieve.

``pause``, ``next_track``, ``previous_track``, ``play_playlist`` and
``set_volume`` fired their request and returned ``{"success": True}``
without ever reading the response. Spotify answers a command it cannot
carry out with an HTTP error and a machine-readable reason, most often
404 NO_ACTIVE_DEVICE when nothing is playing anywhere.

So asking the brain to pause your music with no active device produced
"paused" and silence about the fact that nothing happened. ``play_pause``
and ``queue_track`` in the same class always checked, which is how the
gap survived review: the class looked like it handled this.
"""

from __future__ import annotations

import asyncio

import pytest

from integrations.spotify import SpotifyIntegration


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


NO_DEVICE = _Resp(404, {"error": {"status": 404, "reason": "NO_ACTIVE_DEVICE",
                                  "message": "Player command failed: No active device found"}})
PREMIUM = _Resp(403, {"error": {"status": 403, "reason": "PREMIUM_REQUIRED",
                                "message": "Player command failed: Premium required"}})
EXPIRED = _Resp(401, {"error": {"status": 401, "message": "The access token expired"}})
OK = _Resp(204)


class _HTTP:
    """Records calls and returns a canned response for every verb."""

    def __init__(self, resp):
        self._resp = resp
        self.calls: list[tuple[str, str]] = []

    async def put(self, path, **kw):
        self.calls.append(("PUT", path))
        return self._resp

    async def post(self, path, **kw):
        self.calls.append(("POST", path))
        return self._resp

    async def get(self, path, **kw):
        self.calls.append(("GET", path))
        return self._resp


def _skill(resp):
    s = SpotifyIntegration.__new__(SpotifyIntegration)
    s._http = _HTTP(resp)

    async def _headers():
        return {"Authorization": "Bearer test"}

    s._headers = _headers
    return s


COMMANDS = [
    ("pause", {}),
    ("next_track", {}),
    ("previous_track", {}),
    ("play_playlist", {"uri": "spotify:playlist:test"}),
    ("set_volume", {"volume_percent": 40}),
]


class TestAFailedCommandIsNotASuccess:
    @pytest.mark.parametrize("method,kwargs", COMMANDS)
    def test_no_active_device_is_reported(self, method, kwargs):
        """The realistic case: the user has Spotify open nowhere."""
        skill = _skill(NO_DEVICE)
        result = asyncio.run(getattr(skill, method)(**kwargs))
        assert result["success"] is False, f"{method} claimed success with no active device"
        assert "no active device" in result["error"].lower()

    @pytest.mark.parametrize("method,kwargs", COMMANDS)
    def test_premium_required_is_reported(self, method, kwargs):
        skill = _skill(PREMIUM)
        result = asyncio.run(getattr(skill, method)(**kwargs))
        assert result["success"] is False
        assert "premium" in result["error"].lower()

    @pytest.mark.parametrize("method,kwargs", COMMANDS)
    def test_an_expired_token_is_reported(self, method, kwargs):
        skill = _skill(EXPIRED)
        result = asyncio.run(getattr(skill, method)(**kwargs))
        assert result["success"] is False
        assert "expired" in result["error"].lower() or "invalid" in result["error"].lower()

    @pytest.mark.parametrize("method,kwargs", COMMANDS)
    def test_the_error_names_a_remedy_not_just_a_code(self, method, kwargs):
        """A raw status code tells the user nothing they can act on."""
        result = asyncio.run(getattr(_skill(NO_DEVICE), method)(**kwargs))
        assert "HTTP 404" not in result["error"]
        assert len(result["error"]) > 30


class TestASucceedingCommandStillSucceeds:
    """Guards the fix against over-correction.

    Spotify answers these with 204 No Content, so a checker that only
    accepted 200 would break every working call.
    """

    @pytest.mark.parametrize("method,kwargs", COMMANDS)
    def test_204_is_success(self, method, kwargs):
        result = asyncio.run(getattr(_skill(OK), method)(**kwargs))
        assert result["success"] is True, result

    @pytest.mark.parametrize("method,kwargs", COMMANDS)
    def test_the_request_is_still_actually_sent(self, method, kwargs):
        skill = _skill(OK)
        asyncio.run(getattr(skill, method)(**kwargs))
        assert skill._http.calls, f"{method} sent no request at all"


def test_every_player_method_inspects_its_response():
    """Structural guard so a sixth fire-and-forget command cannot be added.

    This is the property that was violated. Naming the five methods would
    not stop a new one from repeating the mistake, so assert over the
    class instead.
    """
    import ast
    import inspect

    src = inspect.getsource(SpotifyIntegration)
    tree = ast.parse("class _S:\n" + "\n".join("    " + ln for ln in src.splitlines()[1:]))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name in ("close", "_player_error"):
            continue
        seg = ast.get_source_segment("class _S:\n" + "\n".join("    " + ln for ln in src.splitlines()[1:]), node) or ""
        if "self._http." not in seg:
            continue
        if not any(t in seg for t in ("_player_error", "raise_for_status", "status_code")):
            offenders.append(node.name)
    assert offenders == [], (
        f"these methods call Spotify and never inspect the response: {offenders}. "
        "They will report success for a command that did not happen."
    )
