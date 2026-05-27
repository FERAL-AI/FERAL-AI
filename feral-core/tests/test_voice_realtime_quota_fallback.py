"""Pinning tests for the v2026.5.31 voice resilience contract.

Audit-r11 baseline:
  * OpenAI Realtime closes the WS with code 1013 + reason
    ``insufficient_quota`` when the loaded key runs out of credit.
    Prior behaviour: brain swallowed the close in
    ``RealtimeSession._receive_loop``, flipped ``_connected=False``,
    and emitted ZERO frames. Every voice surface (iOS, WebUI desktop,
    WebUI phone) went silent with no banner.
  * Fixed contract:
    1. ``_receive_loop`` forwards the exception to the per-session
       ``on_error`` callback.
    2. ``RealtimeProxy._handle_error`` classifies the error
       (``insufficient_quota``/``1013`` -> ``openai_realtime_quota``)
       and calls ``VoiceRouter.handle_realtime_failure``.
    3. ``VoiceRouter`` emits ONE ``voice_status state=degraded``
       frame, marks the session degraded, and routes all subsequent
       assistant turns through ``synthesize_assistant_speech`` (mp3
       ``tts_chunk`` frames).
    4. When the fallback TTS also fails (no key, network down) the
       router emits ``voice_status state=unavailable`` so the client
       can render a banner instead of silent failure.

These tests run without network access and without ``OPENAI_API_KEY``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from voice.gemini_realtime import GeminiRealtimeProxy
from voice.realtime_proxy import RealtimeProxy, RealtimeSession
from voice.router import VoiceRouter


# ── helpers ─────────────────────────────────────────────────────────


def _capture_sender():
    """Return ``(sender, list)`` where ``sender(session_id, msg)`` records
    each emitted ``FeralMessage`` (or raw dict for the node path) into
    ``list``. The whisper fallback exercises both paths so we capture
    both with the same recorder."""
    captured: list[tuple[str, object]] = []

    async def _send(session_id, msg):
        captured.append((session_id, msg))

    return _send, captured


@pytest.fixture()
def fallback_router(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    audio = MagicMock()
    audio.synthesize_speech = AsyncMock(return_value=[
        {"chunk_index": 0, "encoding": "mp3", "data_b64": "QQ==", "is_final": True},
    ])
    sender, captured = _capture_sender()
    router = VoiceRouter(
        realtime_proxy=MagicMock(available=True, _node_to_session={}),
        audio_pipeline=audio,
        send_to_session=sender,
    )
    return router, audio, captured


# ── voice_status emission ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_realtime_failure_emits_voice_status(fallback_router, monkeypatch):
    """quota error -> state=degraded + reason=openai_realtime_quota."""
    router, _audio, captured = fallback_router
    # Force the whisper fallback path so degraded (not unavailable) is emitted.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    await router.handle_realtime_failure(
        session_id="sess-1",
        reason="openai_realtime_quota",
        detail="received 1013 (insufficient_quota)",
    )

    assert router.is_session_degraded("sess-1")
    voice_status = [
        msg for (sid, msg) in captured
        if getattr(msg, "type", None) == "voice_status"
    ]
    assert len(voice_status) == 1, "expected exactly one voice_status frame"
    payload = voice_status[0].payload
    assert payload["state"] == "degraded"
    assert payload["reason"] == "openai_realtime_quota"
    assert payload["fallback_provider"] == "whisper"


@pytest.mark.asyncio
async def test_handle_realtime_failure_is_idempotent(fallback_router, monkeypatch):
    """Two quota errors on the same session emit one banner — clients render once."""
    router, _audio, captured = fallback_router
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    await router.handle_realtime_failure(session_id="s2", reason="openai_realtime_quota")
    await router.handle_realtime_failure(session_id="s2", reason="openai_realtime_quota")

    voice_status = [
        msg for (_, msg) in captured if getattr(msg, "type", None) == "voice_status"
    ]
    assert len(voice_status) == 1


@pytest.mark.asyncio
async def test_handle_realtime_failure_emits_unavailable_when_no_fallback(
    fallback_router, monkeypatch,
):
    """No OPENAI_API_KEY + ``audio.fallback_tts_providers=[]`` -> unavailable."""
    router, _audio, captured = fallback_router
    # The bundled default now includes ``whisper`` so explicitly null
    # out the chain to exercise the "nothing left" branch. Real users
    # land here when they have no OpenAI key AND no alt-TTS configured.
    monkeypatch.setattr(
        "config.loader.load_settings",
        lambda: {"audio": {"fallback_tts_providers": []}},
        raising=False,
    )

    await router.handle_realtime_failure(
        session_id="s3",
        reason="openai_realtime_quota",
    )

    payload = next(
        msg.payload for (_, msg) in captured
        if getattr(msg, "type", None) == "voice_status"
    )
    assert payload["state"] == "unavailable"
    assert payload["fallback_provider"] == ""


# ── whisper TTS fallback ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_assistant_speech_emits_tts_chunk(fallback_router, monkeypatch):
    """After degrade, assistant turns synthesise mp3 chunks on the session."""
    router, audio, captured = fallback_router
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    await router.handle_realtime_failure(session_id="s4", reason="openai_realtime_quota")

    delivered = await router.synthesize_assistant_speech("s4", "Hello there.")
    assert delivered is True

    audio.synthesize_speech.assert_awaited_once()
    tts_chunks = [
        msg for (_, msg) in captured if getattr(msg, "type", None) == "tts_chunk"
    ]
    assert len(tts_chunks) == 1
    payload = tts_chunks[0].payload
    assert payload["encoding"] == "mp3"
    assert payload["data_b64"]
    assert payload["is_final"] is True


@pytest.mark.asyncio
async def test_fallback_failure_emits_unavailable(monkeypatch):
    """Whisper synth returning None bumps the session to unavailable."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = MagicMock()
    audio.synthesize_speech = AsyncMock(return_value=None)
    sender, captured = _capture_sender()
    router = VoiceRouter(audio_pipeline=audio, send_to_session=sender)

    await router.handle_realtime_failure(session_id="s5", reason="openai_realtime_quota")
    delivered = await router.synthesize_assistant_speech("s5", "Hi")
    assert delivered is False

    voice_status = [
        msg.payload for (_, msg) in captured
        if getattr(msg, "type", None) == "voice_status"
    ]
    # First one is `degraded` from handle_realtime_failure, last is `unavailable`.
    assert voice_status[-1]["state"] == "unavailable"
    assert voice_status[-1]["reason"] == "fallback_tts_failed"


# ── RealtimeProxy classifier ────────────────────────────────────────


@pytest.mark.asyncio
async def test_realtime_proxy_classifies_quota_to_router(monkeypatch):
    """Proxy receives ``insufficient_quota`` text and routes to fallback."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    proxy = RealtimeProxy()
    router = MagicMock()
    router.handle_realtime_failure = AsyncMock()
    proxy.attach_fallback_router(router)

    await proxy._handle_error(
        "sess-quota",
        "received 1013 (insufficient_quota) ('You exceeded your current quota')",
    )

    router.handle_realtime_failure.assert_awaited_once()
    call = router.handle_realtime_failure.call_args
    assert call.kwargs["session_id"] == "sess-quota"
    assert call.kwargs["reason"] == "openai_realtime_quota"


@pytest.mark.asyncio
async def test_realtime_proxy_classifies_auth_error():
    proxy = RealtimeProxy()
    router = MagicMock()
    router.handle_realtime_failure = AsyncMock()
    proxy.attach_fallback_router(router)

    await proxy._handle_error("sess-auth", "401 Unauthorized: invalid_api_key")

    assert router.handle_realtime_failure.call_args.kwargs["reason"] == "openai_realtime_auth"


@pytest.mark.asyncio
async def test_realtime_proxy_handle_error_no_router_is_safe():
    """When no fallback router is attached, _handle_error must not raise."""
    proxy = RealtimeProxy()
    # Should not raise even without attach_fallback_router being called.
    await proxy._handle_error("sess-x", "some random failure")


# ── receive loop wiring ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_receive_loop_pipes_exception_to_on_error():
    """The receive loop must forward upstream WS errors to ``on_error``
    so the classifier can run. Pinned by the regression flow above."""

    class _DummyWS:
        def __init__(self):
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            raise RuntimeError("received 1013 (insufficient_quota) — closed")

        async def close(self):
            pass

    captured: list[str] = []

    async def on_error(sid, err):
        captured.append(err)

    rs = RealtimeSession(
        session_id="s",
        node_id="n",
        api_key="sk-test",
        on_error=on_error,
    )
    rs._ws = _DummyWS()
    rs._connected = True

    await rs._receive_loop()
    assert captured, "_receive_loop should have invoked on_error"
    assert "insufficient_quota" in captured[0]


# ── v2026.5.43 — S4 chained-morph contract ──────────────────────────
#
# When ``audio.fallback_mode == "chained"`` and both ``DEEPGRAM_API_KEY``
# and ``ELEVENLABS_API_KEY`` are in the vault, ``handle_realtime_failure``
# stops the dead realtime / gemini proxy session, retargets every bound
# node onto the chained pipeline, opens a chained session with the
# configured Deepgram + ElevenLabs pair, and emits one
# ``voice_status state=degraded fallback_provider=chained`` frame.


def _stub_audio_settings(monkeypatch, **audio):
    """Pin ``config.loader.load_settings`` to return the supplied
    ``audio`` block. Keeps tests deterministic across environments
    where ``~/.feral/settings.json`` may diverge."""
    monkeypatch.setattr(
        "config.loader.load_settings",
        lambda: {"audio": audio},
        raising=False,
    )


def _make_chained_router(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    sender, captured = _capture_sender()
    realtime = MagicMock(available=True, _node_to_session={})
    realtime.stop_session = AsyncMock()
    router = VoiceRouter(
        realtime_proxy=realtime,
        audio_pipeline=MagicMock(),
        send_to_session=sender,
    )
    router.set_chained_pipeline(MagicMock())
    router.open_chained_session = AsyncMock(return_value=MagicMock())
    return router, realtime, captured


@pytest.mark.asyncio
async def test_quota_morphs_to_chained_pipeline(monkeypatch):
    """S4 happy path — quota close becomes a Deepgram + ElevenLabs morph."""
    router, realtime, captured = _make_chained_router(monkeypatch)
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
        fallback_tts_providers=["whisper"],
    )
    router.bind_node_to_session("phone-1", "sess-1")
    router.register_voice_config("phone-1", {"sample_rate": 24000})

    proxy = RealtimeProxy()
    proxy.attach_fallback_router(router)

    await proxy._handle_error(
        "sess-1",
        "received 1013 (insufficient_quota) ('quota exceeded')",
    )

    router.open_chained_session.assert_awaited_once()
    call_kw = router.open_chained_session.call_args
    sid_arg = call_kw.args[0] if call_kw.args else call_kw.kwargs.get("session_id")
    opts = call_kw.args[1] if len(call_kw.args) > 1 else call_kw.kwargs.get("provider_opts", {})
    assert sid_arg == "sess-1"
    assert opts.get("stt_provider") == "deepgram"
    assert opts.get("tts_provider") == "elevenlabs"
    assert opts.get("node_id") == "phone-1"

    realtime.stop_session.assert_awaited_once_with("sess-1")
    # node config flipped to chained so subsequent audio_chunk
    # frames land on handle_chained_audio.
    assert router._node_voice_config["phone-1"]["mode"] == "chained"
    assert router._node_voice_config["phone-1"]["supports_realtime"] is False

    voice_status = [
        msg for (_, msg) in captured
        if getattr(msg, "type", None) == "voice_status"
    ]
    assert len(voice_status) == 1
    payload = voice_status[0].payload
    assert payload["state"] == "degraded"
    assert payload["fallback_provider"] == "chained"
    assert payload["reason"] == "openai_realtime_quota"
    # _session_degraded MUST NOT be populated on the chained branch —
    # otherwise response_delivery.send_text would also fire the whisper
    # synthesiser and the user hears every assistant turn twice.
    assert not router.is_session_degraded("sess-1")
    assert router._session_voice_mode.get("sess-1") == "chained"


@pytest.mark.asyncio
async def test_fallback_mode_whisper_preserves_legacy(monkeypatch):
    """When operators opt out by setting fallback_mode=whisper, the
    legacy mp3-chunk degrade is preserved unchanged."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sender, captured = _capture_sender()
    router = VoiceRouter(audio_pipeline=MagicMock(), send_to_session=sender)
    router.set_chained_pipeline(MagicMock())
    router.open_chained_session = AsyncMock()
    _stub_audio_settings(monkeypatch, fallback_mode="whisper", fallback_tts_providers=["whisper"])

    await router.handle_realtime_failure(
        session_id="s-whisper",
        reason="openai_realtime_quota",
    )

    router.open_chained_session.assert_not_awaited()
    voice_status = [m for (_, m) in captured if getattr(m, "type", None) == "voice_status"]
    assert len(voice_status) == 1
    assert voice_status[0].payload["fallback_provider"] == "whisper"
    assert router.is_session_degraded("s-whisper")


@pytest.mark.asyncio
async def test_missing_chained_keys_falls_back_to_whisper(monkeypatch):
    """fallback_mode=chained but DEEPGRAM_API_KEY unset → whisper degrade
    (so the user still hears something instead of total silence)."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sender, captured = _capture_sender()
    router = VoiceRouter(audio_pipeline=MagicMock(), send_to_session=sender)
    router.set_chained_pipeline(MagicMock())
    router.open_chained_session = AsyncMock()
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
        fallback_tts_providers=["whisper"],
    )

    await router.handle_realtime_failure(
        session_id="s-nokey",
        reason="openai_realtime_quota",
    )

    router.open_chained_session.assert_not_awaited()
    payload = next(
        msg.payload for (_, msg) in captured
        if getattr(msg, "type", None) == "voice_status"
    )
    assert payload["fallback_provider"] == "whisper"
    assert payload["state"] == "degraded"


@pytest.mark.asyncio
async def test_idempotent_double_quota_fires_one_morph(monkeypatch):
    """Two quota errors in a row emit one banner / open one chained
    session — the second call short-circuits on _session_voice_mode."""
    router, _realtime, captured = _make_chained_router(monkeypatch)
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
    )

    await router.handle_realtime_failure(session_id="s-idem", reason="openai_realtime_quota")
    await router.handle_realtime_failure(session_id="s-idem", reason="openai_realtime_quota")

    assert router.open_chained_session.await_count == 1
    voice_status = [m for (_, m) in captured if getattr(m, "type", None) == "voice_status"]
    assert len(voice_status) == 1


@pytest.mark.asyncio
async def test_send_text_no_double_tts_after_morph(monkeypatch):
    """response_delivery.send_text MUST skip the whisper synthesiser
    when the session is on the chained path — chained TTS already
    fans out tts_chunk frames itself."""
    from agents.response_delivery import send_text

    sender, _captured = _capture_sender()
    router = VoiceRouter(audio_pipeline=MagicMock(), send_to_session=sender)
    router._session_voice_mode["sess-chained"] = "chained"
    # Pre-arm the legacy degraded marker so the is_session_degraded
    # guard passes — the chained branch's job is to skip BEFORE the
    # whisper synth runs.
    router._session_degraded["sess-chained"] = {"state": "degraded"}
    router.synthesize_assistant_speech = AsyncMock()

    orchestrator = MagicMock()
    orchestrator.send = AsyncMock()
    orchestrator.voice_router = router
    orchestrator._text_response_suppressed = {}

    await send_text(orchestrator, "sess-chained", "Hello there.")

    router.synthesize_assistant_speech.assert_not_awaited()


@pytest.mark.asyncio
async def test_audio_routes_to_chained_after_morph(monkeypatch):
    """After the morph flips ``_node_voice_config[node]['mode']`` to
    ``chained``, the next ``audio_chunk`` from that node lands on
    ``handle_chained_audio`` instead of the dead realtime proxy."""
    router, _realtime, _captured = _make_chained_router(monkeypatch)
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
    )
    router.bind_node_to_session("phone-2", "sess-route")
    router.register_voice_config("phone-2", {"sample_rate": 24000, "supports_realtime": True})

    await router.handle_realtime_failure(
        session_id="sess-route",
        reason="openai_realtime_quota",
    )

    # Now an audio_chunk for phone-2 must take the chained branch.
    router.handle_chained_audio = AsyncMock()
    await router.handle_audio_from_node("phone-2", "sess-route", "AAAA==")
    router.handle_chained_audio.assert_awaited_once()


@pytest.mark.asyncio
async def test_gemini_quota_parity_morphs_to_chained(monkeypatch):
    """Gemini Live RESOURCE_EXHAUSTED triggers the same morph via
    GeminiRealtimeProxy._handle_error → handle_realtime_failure."""
    router, _realtime, captured = _make_chained_router(monkeypatch)
    gemini_proxy = MagicMock(available=True)
    gemini_proxy.stop_session = AsyncMock()
    router._gemini = gemini_proxy
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
    )

    proxy = GeminiRealtimeProxy()
    proxy.attach_fallback_router(router)
    await proxy._handle_error("sess-gem", "RESOURCE_EXHAUSTED: quota for your project")

    router.open_chained_session.assert_awaited_once()
    gemini_proxy.stop_session.assert_awaited_once_with("sess-gem")
    voice_status = [m for (_, m) in captured if getattr(m, "type", None) == "voice_status"]
    assert len(voice_status) == 1
    assert voice_status[0].payload["fallback_provider"] == "chained"
    assert voice_status[0].payload["provider"] == "gemini"
