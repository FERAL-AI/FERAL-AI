"""
Unit tests for Gemini realtime voice proxy (`voice.gemini_realtime`)
and VoiceRouter Gemini integration (`voice.router`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice.gemini_realtime import GeminiRealtimeProxy, GeminiRealtimeSession


class TestGeminiRealtimeProxy:
    """Tests for `GeminiRealtimeProxy`."""

    def test_init_initializes_session_dict(self) -> None:
        """Constructor allocates empty session and node maps."""
        proxy = GeminiRealtimeProxy()
        assert proxy._sessions == {}
        assert proxy._node_to_session == {}

    def test_has_session_unknown_false_after_manual_add_true(self) -> None:
        """`has_session` tracks `_sessions` membership."""
        proxy = GeminiRealtimeProxy()
        assert proxy.has_session("missing") is False
        proxy._sessions["sid-1"] = MagicMock()
        assert proxy.has_session("sid-1") is True

    @pytest.mark.asyncio
    async def test_start_session_creates_entry_mock_websocket(self) -> None:
        """`start_session` registers the session after connect (websocket mocked)."""
        with patch("voice.gemini_realtime.os.getenv", return_value="fake-key"):
            proxy = GeminiRealtimeProxy()
        async def fake_connect(self_inner):
            self_inner._ws = MagicMock()
            self_inner._connected = True
        with patch.object(GeminiRealtimeSession, "connect", fake_connect):
            sess = await proxy.start_session("sess-a", "node-a")
        assert isinstance(sess, GeminiRealtimeSession)
        assert sess.connected
        assert proxy.has_session("sess-a")
        assert proxy._node_to_session.get("node-a") == "sess-a"

    @pytest.mark.asyncio
    async def test_relay_audio_no_session_no_error(self) -> None:
        """Relaying audio without a matching session is a silent no-op."""
        proxy = GeminiRealtimeProxy()
        await proxy.relay_audio("nonexistent-session", "AAA=")

    @pytest.mark.asyncio
    async def test_relay_audio_forwards_when_session_connected(self) -> None:
        """Audio is passed to `GeminiRealtimeSession.send_audio` when connected."""
        proxy = GeminiRealtimeProxy()
        gs = MagicMock(spec=GeminiRealtimeSession)
        gs.connected = True
        gs.send_audio = AsyncMock()
        proxy._sessions["live-sid"] = gs
        await proxy.relay_audio("live-sid", "PCM64=")
        gs.send_audio.assert_awaited_once_with("PCM64=")

    def test_get_session_by_node_id(self) -> None:
        """get_session resolves node_id → session_id → session object."""
        proxy = GeminiRealtimeProxy()
        gs = MagicMock(spec=GeminiRealtimeSession)
        proxy._sessions["sid-x"] = gs
        proxy._node_to_session["node-x"] = "sid-x"
        assert proxy.get_session("node-x") is gs
        assert proxy.get_session("unknown") is None


class TestVoiceRouterGemini:
    """Tests for Gemini routing in VoiceRouter."""

    def _make_router(self, *, gemini_available=True, openai_available=False):
        from voice.router import VoiceRouter

        router = VoiceRouter()
        if gemini_available:
            gemini = MagicMock()
            gemini.available = True
            gemini.get_session = MagicMock(return_value=None)
            gemini.start_session = AsyncMock()
            gemini._node_to_session = {}
            router.set_gemini_proxy(gemini)
        if openai_available:
            rt = MagicMock()
            rt.available = True
            router._realtime = rt
        return router

    def test_resolve_provider_gemini_via_node_config(self) -> None:
        router = self._make_router()
        router.register_voice_config("n1", {"voice_provider": "gemini", "supports_realtime": True})
        assert router._resolve_provider("n1") == "gemini"

    def test_resolve_provider_falls_back_to_whisper(self) -> None:
        router = self._make_router(gemini_available=False)
        assert router._resolve_provider("n2") == "whisper"

    def test_resolve_provider_env_override(self) -> None:
        router = self._make_router()
        router.register_voice_config("n3", {"supports_realtime": True})
        with patch.dict("os.environ", {"FERAL_VOICE_PROVIDER": "gemini"}):
            assert router._resolve_provider("n3") == "gemini"

    def test_session_provider_gemini_via_env(self) -> None:
        router = self._make_router()
        router.set_session_voice_mode("s1", "realtime")
        with patch.dict("os.environ", {"FERAL_VOICE_PROVIDER": "gemini"}):
            assert router._resolve_session_provider("s1") == "gemini"

    def test_set_gemini_proxy(self) -> None:
        from voice.router import VoiceRouter
        router = VoiceRouter()
        assert router._gemini is None
        proxy = MagicMock()
        router.set_gemini_proxy(proxy)
        assert router._gemini is proxy

    @pytest.mark.asyncio
    async def test_handle_audio_for_gemini_creates_session(self) -> None:
        router = self._make_router()
        gs_mock = MagicMock()
        gs_mock.connected = True
        gs_mock.send_audio = AsyncMock()
        router._gemini.start_session = AsyncMock(return_value=gs_mock)
        await router.handle_audio_for_gemini("sess-1", "AAAA==")
        router._gemini.start_session.assert_awaited_once()
        gs_mock.send_audio.assert_awaited_once_with("AAAA==")
