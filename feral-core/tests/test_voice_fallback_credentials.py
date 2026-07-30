"""Fallback must not recover onto the credential that just failed.

Voice-collapse audit (2026-07), findings 5 and 6.

Finding 5: ``_try_chained_morph`` auto-substitutes OpenAI Whisper STT +
OpenAI TTS when the configured Deepgram/ElevenLabs keys are missing.
That is right for a chat-key-only operator — and exactly wrong when the
trigger was ``openai_realtime_quota`` / ``openai_realtime_auth``, where
the OpenAI key IS the thing that failed. The "recovered" session then
401s on every request behind a green banner. It now refuses, and the
caller emits ``unavailable`` rather than promising the whisper fallback
(which runs on the same dead key).

Finding 6: ``handle_realtime_failure`` defaulted ``fallback_mode`` to
``"whisper"`` while ``config/loader.py`` defaults it to ``"chained"``.
Any test stubbing ``load_settings`` without the key silently exercised
the legacy branch — which is how the chained pipeline's dead ends went
unnoticed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from voice.router import VoiceRouter


def _capture_router(monkeypatch, *, with_chained: bool = True):
    captured: list = []

    async def send(_sid, msg):
        captured.append(msg)

    router = VoiceRouter(
        realtime_proxy=MagicMock(available=True, _node_to_session={}),
        audio_pipeline=MagicMock(),
        send_to_session=send,
    )
    if with_chained:
        router.set_chained_pipeline(MagicMock())
        router.open_chained_session = AsyncMock(return_value=MagicMock())
    return router, captured


def _stub_audio_settings(monkeypatch, **audio):
    monkeypatch.setattr(
        "config.loader.load_settings", lambda: {"audio": audio}, raising=False,
    )


def _statuses(captured):
    return [m.payload for m in captured if getattr(m, "type", None) == "voice_status"]


# ── finding 5 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", ["openai_realtime_quota", "openai_realtime_auth"])
@pytest.mark.asyncio
async def test_quota_or_auth_never_substitutes_the_openai_pair(monkeypatch, reason):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dead")
    router, captured = _capture_router(monkeypatch)
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
        fallback_tts_providers=["whisper"],
    )

    await router.handle_realtime_failure(session_id="s-dead", reason=reason)

    router.open_chained_session.assert_not_awaited()
    payload = _statuses(captured)[-1]
    assert payload["state"] == "unavailable", "a dead key must not promise audio"
    assert payload["reason"] == reason
    assert payload["fallback_provider"] == ""


@pytest.mark.asyncio
async def test_non_credential_failure_still_substitutes_the_openai_pair(monkeypatch):
    """The chat-key-only operator keeps their working fallback."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    router, captured = _capture_router(monkeypatch)
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
    )

    await router.handle_realtime_failure(
        session_id="s-net", reason="openai_realtime_connect",
    )

    router.open_chained_session.assert_awaited_once()
    opts = router.open_chained_session.call_args.args[1]
    assert opts["stt_provider"] == "openai_whisper"
    assert opts["tts_provider"] == "openai"


@pytest.mark.asyncio
async def test_quota_with_working_deepgram_keys_still_morphs(monkeypatch):
    """The refusal is about substitution, not about quota generally."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dead")
    router, captured = _capture_router(monkeypatch)
    router._realtime.stop_session = AsyncMock()
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
    )

    await router.handle_realtime_failure(
        session_id="s-ok", reason="openai_realtime_quota",
    )

    router.open_chained_session.assert_awaited_once()
    assert _statuses(captured)[-1]["fallback_provider"] == "chained"


@pytest.mark.asyncio
async def test_no_openai_key_at_all_keeps_the_legacy_whisper_degrade(monkeypatch):
    """Nothing was substituted, so nothing dishonest was promised."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    router, captured = _capture_router(monkeypatch)
    _stub_audio_settings(
        monkeypatch,
        fallback_mode="chained",
        chained_fallback={"stt_provider": "deepgram", "tts_provider": "elevenlabs"},
        fallback_tts_providers=["whisper"],
    )

    await router.handle_realtime_failure(
        session_id="s-nokey", reason="openai_realtime_quota",
    )

    payload = _statuses(captured)[-1]
    assert payload["state"] == "degraded"
    assert payload["fallback_provider"] == "whisper"


# ── finding 6 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_mode_defaults_to_chained_like_the_loader(monkeypatch):
    """Settings with no ``fallback_mode`` must take the chained branch."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    router, _captured = _capture_router(monkeypatch)
    router._realtime.stop_session = AsyncMock()
    _stub_audio_settings(monkeypatch, fallback_tts_providers=["whisper"])

    await router.handle_realtime_failure(
        session_id="s-default", reason="openai_realtime_connect",
    )

    router.open_chained_session.assert_awaited_once()


def test_router_default_matches_the_settings_loader_default():
    """The two defaults are the same value, not just the same today."""
    from config.loader import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["audio"]["fallback_mode"] == "chained"
