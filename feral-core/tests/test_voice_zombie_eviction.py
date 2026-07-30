"""Zombie realtime sessions must be evicted, never reused.

Voice-collapse audit (2026-07), finding 2.

``RealtimeProxy`` kept sessions in ``_sessions`` / ``_node_to_session``
after ``_connected`` flipped False. Both router audio entry points did a
bare ``get_session``, found a non-None handle, skipped the re-open, and
then dropped the chunk on their own ``rs.connected`` gate. The user
talked into a socket that had been closed minutes earlier, forever —
nothing in the system ever removed the corpse, and the next
``voice_session_start`` was handed the same dead handle.

Pinned contract:
  1. ``get_session`` never returns a disconnected session, and prunes
     the map entries so the next lookup re-opens.
  2. ``evict_dead_session`` closes the leaked socket and reports
     whether it reaped anything.
  3. Both router entry points (node + web client) evict and re-open
     rather than dropping the chunk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from voice.realtime_proxy import RealtimeProxy, RealtimeSession
from voice.router import VoiceRouter, _ENV_VOICE_PROVIDER


def _dead_session(proxy: RealtimeProxy, session_id: str, node_id: str) -> RealtimeSession:
    """Register a session that reports disconnected, as a dropped WS leaves it."""
    rs = RealtimeSession(session_id=session_id, node_id=node_id, api_key="sk-test")
    rs._connected = False
    proxy._sessions[session_id] = rs
    proxy._node_to_session[node_id] = session_id
    return rs


# ── RealtimeProxy ───────────────────────────────────────────────────


def test_get_session_never_returns_a_disconnected_session():
    proxy = RealtimeProxy()
    _dead_session(proxy, "sess-zombie", "phone-1")

    assert proxy.get_session("phone-1") is None


def test_get_session_prunes_the_zombie_from_both_maps():
    proxy = RealtimeProxy()
    _dead_session(proxy, "sess-zombie", "phone-1")

    proxy.get_session("phone-1")

    assert "sess-zombie" not in proxy._sessions
    assert "phone-1" not in proxy._node_to_session


def test_get_session_still_returns_a_live_session():
    proxy = RealtimeProxy()
    rs = _dead_session(proxy, "sess-live", "phone-1")
    rs._connected = True

    assert proxy.get_session("phone-1") is rs


@pytest.mark.asyncio
async def test_evict_dead_session_closes_the_leaked_socket():
    proxy = RealtimeProxy()
    rs = _dead_session(proxy, "sess-zombie", "phone-1")
    ws = AsyncMock()
    rs._ws = ws

    reaped = await proxy.evict_dead_session("phone-1")

    assert reaped is True
    ws.close.assert_awaited_once()
    assert "sess-zombie" not in proxy._sessions
    assert "phone-1" not in proxy._node_to_session


@pytest.mark.asyncio
async def test_evict_dead_session_leaves_a_live_session_alone():
    proxy = RealtimeProxy()
    rs = _dead_session(proxy, "sess-live", "phone-1")
    rs._connected = True
    rs._ws = AsyncMock()

    assert await proxy.evict_dead_session("phone-1") is False
    assert proxy.get_session("phone-1") is rs


@pytest.mark.asyncio
async def test_evict_dead_session_on_unknown_node_is_a_noop():
    proxy = RealtimeProxy()
    assert await proxy.evict_dead_session("never-seen") is False


# ── VoiceRouter audio entry points ──────────────────────────────────


def _router_with_zombie(monkeypatch, node_id: str):
    """Router over a real proxy holding one dead session for ``node_id``."""
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    proxy = RealtimeProxy()
    dead = _dead_session(proxy, "sess-old", node_id)
    dead._ws = AsyncMock()

    fresh = MagicMock(connected=True)
    fresh.send_audio = AsyncMock()
    proxy.start_session = AsyncMock(return_value=fresh)

    router = VoiceRouter(realtime_proxy=proxy, audio_pipeline=MagicMock())
    return router, proxy, dead, fresh


@pytest.mark.asyncio
async def test_node_audio_reopens_instead_of_dropping_the_chunk(monkeypatch):
    router, proxy, dead, fresh = _router_with_zombie(monkeypatch, "phone-1")
    router.register_voice_config("phone-1", {"voice_provider": "openai"})

    await router.handle_audio_from_node("phone-1", "sess-new", "QUJD")

    proxy.start_session.assert_awaited_once()
    fresh.send_audio.assert_awaited_once_with("QUJD")
    # The zombie's socket was closed rather than left billing.
    dead._ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_audio_reopens_instead_of_dropping_the_chunk(monkeypatch):
    router, proxy, dead, fresh = _router_with_zombie(monkeypatch, "webclient_12345678")
    router.set_session_voice_mode("12345678-abcd", "realtime")

    await router.handle_audio_from_client("12345678-abcd", "QUJD")

    proxy.start_session.assert_awaited_once()
    fresh.send_audio.assert_awaited_once_with("QUJD")
    dead._ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_session_is_not_evicted_or_reopened(monkeypatch):
    """The hot path must not disturb a healthy session."""
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    proxy = RealtimeProxy()
    live = _dead_session(proxy, "sess-live", "phone-1")
    live._connected = True
    live.send_audio = AsyncMock()
    proxy.start_session = AsyncMock()

    router = VoiceRouter(realtime_proxy=proxy, audio_pipeline=MagicMock())
    router.register_voice_config("phone-1", {"voice_provider": "openai"})

    await router.handle_audio_from_node("phone-1", "sess-live", "QUJD")

    proxy.start_session.assert_not_awaited()
    live.send_audio.assert_awaited_once_with("QUJD")
