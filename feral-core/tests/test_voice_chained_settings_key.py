"""Regression: the phone Settings panel's chained pick must reach the
chained pipeline.

The panel (``feral-client-v2/src/pages/phone/SettingsPanel.jsx``)
persists ``voice.chained.stt_provider`` / ``.stt_model`` /
``.tts_provider`` / ``.tts_voice``. Nothing read those keys: the router
only ever looked at ``audio.chained_fallback.*``, so a user who picked
Groq Whisper plus ElevenLabs in the phone UI still got Deepgram plus
ElevenLabs on every chained session.

These tests drive settings through the same call the phone UI's write
lands on (``POST /api/config/update`` -> ``ConfigLoader.update_settings``)
and assert the chained session constructs the providers that were picked.
``FERAL_HOME`` is redirected to a tmp dir by the autouse
``isolate_feral_home`` fixture in ``conftest.py``, so these writes never
touch a real install.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from voice.router import VoiceRouter  # noqa: E402


def _write_settings(section: str, key: str, value) -> None:
    """Persist a setting exactly the way ``POST /api/config/update`` does."""
    from config.loader import ConfigLoader

    loader = ConfigLoader()
    loader.discover()
    loader.update_settings(section, key, value)


def _router_with_spies():
    """Return ``(router, captured)`` where ``captured`` records the
    provider name + kwargs each registry factory was asked for."""
    captured: dict = {"stt": None, "tts": None}

    def _spy_stt(name, **kwargs):
        captured["stt"] = (name, kwargs)
        provider = MagicMock()
        provider.close = AsyncMock()
        return provider

    def _spy_tts(name, **kwargs):
        captured["tts"] = (name, kwargs)
        provider = MagicMock()
        provider.close = AsyncMock()
        return provider

    router = VoiceRouter(audio_pipeline=MagicMock(), orchestrator=MagicMock())
    chained = MagicMock()
    chained.open_session = AsyncMock(return_value=MagicMock())
    router.set_chained_pipeline(chained)
    return router, captured, _spy_stt, _spy_tts


@pytest.mark.asyncio
async def test_phone_settings_chained_pick_reaches_chained_session(monkeypatch):
    """The exact defect: pick Groq Whisper + ElevenLabs in the phone
    Settings panel, get Groq Whisper + ElevenLabs in the session.

    Before the fix this built ``deepgram`` + ``elevenlabs`` because the
    router read ``audio.chained_fallback`` and never looked at
    ``voice.chained``.
    """
    monkeypatch.setenv("GROQ_API_KEY", "gq-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")

    _write_settings("voice", "chained", {
        "stt_provider": "groq_whisper",
        "stt_model": "whisper-large-v3",
        "tts_provider": "elevenlabs",
        "tts_voice_id": "21m00Tcm4TlvDq8ikWAM",
    })

    router, captured, spy_stt, spy_tts = _router_with_spies()
    with patch("voice.stt_providers.get_stt_provider", side_effect=spy_stt), \
         patch("voice.tts_providers.get_tts_provider", side_effect=spy_tts):
        session = await router.open_chained_session("sess-ui", {})

    assert session is not None
    assert captured["stt"][0] == "groq_whisper"
    assert captured["tts"][0] == "elevenlabs"
    # The picked STT model must ride along too. Deepgram's ``nova-3``
    # used to be hardcoded here, which is not a valid Groq model id.
    assert captured["stt"][1]["model"] == "whisper-large-v3"
    assert captured["tts"][1]["voice_id"] == "21m00Tcm4TlvDq8ikWAM"


@pytest.mark.asyncio
async def test_setup_wizard_chained_fallback_still_honoured(monkeypatch):
    """``cli/setup/steps/voice_preflight.py`` mirrors the operator's pick
    into ``audio.chained_fallback``. That write must keep working when
    the phone UI never wrote a ``voice.chained`` block."""
    monkeypatch.setenv("OPENAI_API_KEY", "oa-test")
    monkeypatch.setenv("CARTESIA_API_KEY", "ct-test")

    _write_settings("audio", "chained_fallback", {
        "stt_provider": "openai_whisper",
        "tts_provider": "cartesia",
    })

    router, captured, spy_stt, spy_tts = _router_with_spies()
    with patch("voice.stt_providers.get_stt_provider", side_effect=spy_stt), \
         patch("voice.tts_providers.get_tts_provider", side_effect=spy_tts):
        await router.open_chained_session("sess-wizard", {})

    assert captured["stt"][0] == "openai_whisper"
    assert captured["tts"][0] == "cartesia"
    # No model configured anywhere, so the provider's own default wins
    # instead of Deepgram's ``nova-3`` being forced onto Whisper.
    assert "model" not in captured["stt"][1]


@pytest.mark.asyncio
async def test_voice_chained_wins_over_audio_chained_fallback(monkeypatch):
    """Both blocks populated: the UI block is the one the user last
    touched by hand, so it wins."""
    monkeypatch.setenv("GROQ_API_KEY", "gq-test")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-test")

    _write_settings("audio", "chained_fallback", {
        "stt_provider": "deepgram",
        "tts_provider": "elevenlabs",
    })
    _write_settings("voice", "chained", {
        "stt_provider": "groq_whisper",
        "tts_provider": "openai",
        "tts_voice": "shimmer",
    })

    router, captured, spy_stt, spy_tts = _router_with_spies()
    with patch("voice.stt_providers.get_stt_provider", side_effect=spy_stt), \
         patch("voice.tts_providers.get_tts_provider", side_effect=spy_tts):
        await router.open_chained_session("sess-both", {})

    assert captured["stt"][0] == "groq_whisper"
    assert captured["tts"][0] == "openai"
    assert captured["tts"][1]["voice"] == "shimmer"


@pytest.mark.asyncio
async def test_per_session_opts_still_beat_settings(monkeypatch):
    """A ``voice_session_start`` envelope that names providers keeps
    overriding both settings blocks."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")

    _write_settings("voice", "chained", {
        "stt_provider": "groq_whisper",
        "tts_provider": "openai",
    })

    router, captured, spy_stt, spy_tts = _router_with_spies()
    with patch("voice.stt_providers.get_stt_provider", side_effect=spy_stt), \
         patch("voice.tts_providers.get_tts_provider", side_effect=spy_tts):
        await router.open_chained_session("sess-opts", {
            "stt_provider": "deepgram",
            "tts_provider": "elevenlabs",
        })

    assert captured["stt"][0] == "deepgram"
    assert captured["tts"][0] == "elevenlabs"


@pytest.mark.asyncio
async def test_morph_substitution_drops_the_other_providers_stt_model(monkeypatch):
    """The realtime-failure morph substitutes OpenAI Whisper when the
    configured STT provider has no key. A Deepgram ``nova-*`` model id
    must not ride along to Whisper, which would reject it."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "oa-test")

    _write_settings("voice", "chained", {
        "stt_provider": "deepgram",
        "stt_model": "nova-3",
        "tts_provider": "elevenlabs",
    })

    router, captured, spy_stt, spy_tts = _router_with_spies()
    router._realtime = MagicMock()
    router._realtime.stop_session = AsyncMock()
    router._emit_voice_status = AsyncMock()

    with patch("voice.stt_providers.get_stt_provider", side_effect=spy_stt), \
         patch("voice.tts_providers.get_tts_provider", side_effect=spy_tts):
        await router.handle_realtime_failure(
            "sess-morph", reason="server_error", detail="", provider="openai",
        )

    assert captured["stt"][0] == "openai_whisper"
    assert "model" not in captured["stt"][1]


def test_resolve_chained_config_defaults_when_settings_empty():
    """No settings anywhere: the shipped Deepgram + ElevenLabs pair."""
    router = VoiceRouter(audio_pipeline=MagicMock(), orchestrator=MagicMock())
    router._load_voice_settings = MagicMock(return_value={})
    resolved = router._resolve_chained_config({})
    assert resolved["stt_provider"] == "deepgram"
    assert resolved["tts_provider"] == "elevenlabs"
    assert resolved["stt_model"] == ""
