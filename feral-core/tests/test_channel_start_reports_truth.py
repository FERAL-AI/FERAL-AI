"""Starting a channel must report what happened, not always "ok".

``POST /api/channels/start`` returned ``{"ok": True, "channel": <type>}``
on every path. ``ChannelManager.start_channel`` returned None whether it
started a channel, could not find the type, found it degraded, or found
it never connected.

That matters most for the five channel classes that ship in
``channels/`` and are absent from ``ChannelManager.CHANNEL_TYPES``:
feishu, matrix, signal, voice_call, zalo. ``pyproject.toml`` declares
``channel-matrix``, ``channel-signal``, ``channel-voice-call``,
``channel-feishu`` and ``channel-zalo`` extras for them, so an operator
has every reason to try. ``SignalChannel.send`` documents itself as a
stub and logs "dropping message".
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from channels.base import Channel, ChannelManager  # noqa: E402


class _Fake(Channel):
    """Channel whose start outcome the test dictates."""

    outcome = "ok"

    @property
    def channel_type(self) -> str:
        return "fake"

    async def start(self):
        if self.outcome == "ok":
            self._running = True
            self._connected = True
        elif self.outcome == "degraded":
            self._degraded = True
            self._degraded_reason = "auth rejected by upstream"
        # "dead": leave _running and _connected False

    async def stop(self):
        self._running = False

    async def send(self, channel_id, response):
        return None


def _manager_with(outcome: str) -> ChannelManager:
    mgr = ChannelManager()

    class _C(_Fake):
        pass

    _C.outcome = outcome
    mgr.CHANNEL_TYPES = dict(ChannelManager.CHANNEL_TYPES)
    mgr.CHANNEL_TYPES["fake"] = _C
    return mgr


# ---------------------------------------------------------------------------
# The manager tells the truth
# ---------------------------------------------------------------------------

def test_an_unknown_channel_type_is_not_a_success():
    mgr = ChannelManager()
    out = asyncio.run(mgr.start_channel("signal", {}))
    assert out["started"] is False
    assert out["reason"] == "unknown_channel_type"
    assert "signal" in out["detail"]


@pytest.mark.parametrize("orphan", ["signal", "matrix", "feishu", "zalo", "voice_call"])
def test_every_orphaned_channel_class_reports_unknown(orphan):
    """Five classes ship, none are wired. The API must say so."""
    mgr = ChannelManager()
    assert orphan not in mgr.CHANNEL_TYPES
    assert asyncio.run(mgr.start_channel(orphan, {}))["started"] is False


def test_a_degraded_channel_is_not_a_success():
    out = asyncio.run(_manager_with("degraded").start_channel("fake", {}))
    assert out["started"] is False
    assert out["reason"] == "degraded"
    assert "auth rejected" in out["detail"]


def test_a_channel_that_never_connects_is_not_a_success():
    out = asyncio.run(_manager_with("dead").start_channel("fake", {}))
    assert out["started"] is False
    assert out["reason"] == "did_not_start"


def test_a_channel_that_starts_is_a_success():
    mgr = _manager_with("ok")
    out = asyncio.run(mgr.start_channel("fake", {}))
    assert out["started"] is True
    assert "fake" in mgr._channels


# ---------------------------------------------------------------------------
# The route surfaces it
# ---------------------------------------------------------------------------

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.routes.channels as channels_route

    app = FastAPI()
    app.include_router(channels_route.router)
    return TestClient(app), channels_route


def test_route_404s_on_an_unknown_channel_type(monkeypatch):
    client, mod = _client()
    monkeypatch.setattr(mod.state, "channel_manager", ChannelManager(), raising=False)
    resp = client.post("/api/channels/start", json={"type": "signal", "config": {}})
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "unknown_channel_type"


def test_route_502s_on_a_degraded_channel(monkeypatch):
    client, mod = _client()
    monkeypatch.setattr(mod.state, "channel_manager", _manager_with("degraded"), raising=False)
    resp = client.post("/api/channels/start", json={"type": "fake", "config": {}})
    assert resp.status_code == 502
    assert resp.json()["reason"] == "degraded"


def test_route_reports_ok_when_the_channel_really_started(monkeypatch):
    client, mod = _client()
    monkeypatch.setattr(mod.state, "channel_manager", _manager_with("ok"), raising=False)
    resp = client.post("/api/channels/start", json={"type": "fake", "config": {}})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "channel": "fake"}
