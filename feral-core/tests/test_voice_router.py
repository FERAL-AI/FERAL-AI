"""Tests for voice.router — VoiceRouter triple-path audio routing."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice.router import VoiceRouter


@pytest.fixture()
def mock_realtime():
    rt = MagicMock()
    rt.available = True
    rt.get_session = MagicMock(return_value=None)
    rt.start_session = AsyncMock()
    # Zombie eviction: the audio hot path asks the proxy to reap a
    # disconnected session before every lookup, so a proxy double has
    # to model it. Default "nothing to evict".
    rt.evict_dead_session = AsyncMock(return_value=False)
    rt._node_to_session = {}
    rt.shutdown = AsyncMock()
    return rt


@pytest.fixture()
def mock_gemini():
    gm = MagicMock()
    gm.available = True
    gm.get_session = MagicMock(return_value=None)
    gm.start_session = AsyncMock()
    gm._node_to_session = {}
    gm.shutdown = AsyncMock()
    return gm


@pytest.fixture()
def router(mock_realtime, mock_gemini):
    r = VoiceRouter(
        realtime_proxy=mock_realtime,
        audio_pipeline=MagicMock(),
        orchestrator=MagicMock(),
    )
    r.set_gemini_proxy(mock_gemini)
    return r


# ── Provider selection ───────────────────────────────────────────

def test_default_provider_openai_when_supports_realtime(router):
    router.register_voice_config("n1", {"supports_realtime": True})
    assert router._resolve_provider("n1") == "openai"


def test_gemini_via_env(router, monkeypatch):
    monkeypatch.setenv("FERAL_VOICE_PROVIDER", "gemini")
    router.register_voice_config("n1", {"supports_realtime": True})
    assert router._resolve_provider("n1") == "gemini"


def test_node_specific_provider_config(router):
    router.register_voice_config("n1", {"voice_provider": "gemini"})
    assert router._resolve_provider("n1") == "gemini"


def test_whisper_fallback_no_proxy():
    r = VoiceRouter()
    assert r._resolve_provider("any") == "whisper"


# ── Session voice mode ───────────────────────────────────────────

def test_session_voice_mode_switching(router):
    router.set_session_voice_mode("s1", "realtime")
    assert router._resolve_session_provider("s1") == "openai"

    router.set_session_voice_mode("s1", "whisper")
    assert router._resolve_session_provider("s1") == "whisper"


def test_session_uses_realtime(router):
    router.set_session_voice_mode("s1", "realtime")
    assert router.session_uses_realtime("s1") is True
    assert router.session_uses_realtime("unknown") is False


# ── Wake word gating ─────────────────────────────────────────────

async def test_wake_word_blocks_audio():
    wake = MagicMock()
    wake.enabled = True
    wake.process_frame = AsyncMock(return_value=False)

    r = VoiceRouter(wake_word_detector=wake)
    r.register_voice_config("n1", {"supports_realtime": True})

    await r.handle_audio_from_node("n1", "s1", base64.b64encode(b"\x00" * 100).decode())
    wake.process_frame.assert_awaited_once()


# ── handle_audio dispatching ─────────────────────────────────────

async def test_handle_audio_dispatches_openai(router, mock_realtime):
    sess = MagicMock(connected=True, send_audio=AsyncMock())
    mock_realtime.get_session.return_value = sess
    router.register_voice_config("n1", {"supports_realtime": True})

    await router.handle_audio_from_node("n1", "s1", "AAAA==")
    sess.send_audio.assert_awaited_once_with("AAAA==")


async def test_handle_audio_dispatches_whisper(mock_realtime):
    pipeline = MagicMock()
    pipeline.process_audio_chunk = AsyncMock(return_value=None)

    r = VoiceRouter(realtime_proxy=mock_realtime, audio_pipeline=pipeline)
    await r.handle_audio_from_node("n1", "s1", "AAAA==")
    pipeline.process_audio_chunk.assert_awaited_once()


# ── Graceful no-proxy ────────────────────────────────────────────

async def test_graceful_no_audio_pipeline():
    r = VoiceRouter()
    await r.handle_audio_from_node("n1", "s1", "AAAA==")


async def test_shutdown_delegates(router, mock_realtime, mock_gemini):
    await router.shutdown()
    mock_realtime.shutdown.assert_awaited_once()
    mock_gemini.shutdown.assert_awaited_once()


# ── Provider-key resolution (Bug 4) ──────────────────────────────
#
# The voice router must resolve provider API keys via
# ``security.vault_keys.get_active_key`` so a labeled-only key (set
# via ``feral key add --provider openai --label prod --set-active``,
# which does NOT also write the legacy default-namespace entry) is
# visible to the realtime/chained voice path. The chat path already
# honours this (``agents.llm_provider``); these tests pin that the
# voice path now does too — strictly additive (env-only setups keep
# working).


@pytest.fixture
def _vault_isolated(tmp_path, monkeypatch):
    """In-memory keychain + isolated FERAL_HOME so labeled-key writes
    land in this test's tmp_path. Mirrors ``fake_keychain`` from
    ``test_llm_vault_hot_path.py`` but kept local so this test file
    stays self-contained (the router suite has no conftest dep on the
    LLM hot-path fixtures)."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)
    for _envvar in (
        "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY",
        "GROQ_API_KEY", "CARTESIA_API_KEY",
    ):
        monkeypatch.delenv(_envvar, raising=False)

    store: dict[tuple[str, str], str] = {}
    import security.vault as vault_mod
    monkeypatch.setattr(
        vault_mod, "_keyring_get_password",
        lambda service, username: store.get((service, username)),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_set_password",
        lambda service, username, password: store.__setitem__(
            (service, username), password,
        ),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_delete_password",
        lambda service, username: store.pop((service, username), None),
    )
    vault_mod.reset_vault()
    yield store
    vault_mod.reset_vault()


def test_voice_router_resolves_labeled_only_openai_key(_vault_isolated, monkeypatch):
    """Labeled-only OpenAI key (no env var, no legacy default-namespace
    entry) must be visible to the realtime/chained voice path.

    This is the operator-mental-model gap closed by Bug 4: today
    ``feral key add --provider openai --label prod --set-active``
    stores the key in the labeled-vault namespace only. Pre-fix the
    voice router read ``os.getenv("OPENAI_API_KEY")`` directly and
    saw nothing, so chained-pipeline preflight + the whisper-fallback
    picker both reported "no key" even though chat was happily
    talking to OpenAI.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from security.vault_keys import add_provider_key
    from voice.router import _resolve_provider_key

    add_provider_key("openai", "prod", "sk-labeled-prod", set_active=True)

    assert _resolve_provider_key("openai", "OPENAI_API_KEY") == "sk-labeled-prod"

    r = VoiceRouter(audio_pipeline=MagicMock())
    assert r._pick_fallback_provider() == "whisper"


def test_voice_router_falls_back_to_env_when_no_labeled_key(
    _vault_isolated, monkeypatch,
):
    """Regression guard: env-only setups (no labeled key, no legacy
    default-namespace entry) must keep working. The resolver must
    return the env value untouched so existing operators who only
    ever exported ``OPENAI_API_KEY`` see no behaviour change."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-only")
    from voice.router import _resolve_provider_key

    assert _resolve_provider_key("openai", "OPENAI_API_KEY") == "sk-env-only"


def test_voice_router_deepgram_elevenlabs_keys_resolve_independently(
    _vault_isolated, monkeypatch,
):
    """Labeled keys for deepgram + elevenlabs must each resolve to
    their own provider's key — no cross-contamination through the
    shared resolver. Pins the per-call-site provider plumbing in
    ``_try_chained_morph`` and ``open_chained_session`` so a
    future refactor can't accidentally map deepgram → openai or
    vice versa."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    from security.vault_keys import add_provider_key
    from voice.router import _resolve_provider_key

    add_provider_key("deepgram", "default", "dg-secret", set_active=True)
    add_provider_key("elevenlabs", "default", "el-secret", set_active=True)

    assert _resolve_provider_key("deepgram", "DEEPGRAM_API_KEY") == "dg-secret"
    assert _resolve_provider_key("elevenlabs", "ELEVENLABS_API_KEY") == "el-secret"
    assert _resolve_provider_key("openai", "OPENAI_API_KEY") == ""


# ── Chained STT sample-rate plumbing (bug 2 regression) ─────────────


@pytest.mark.asyncio
async def test_open_chained_session_threads_sample_rate_to_stt(monkeypatch):
    """The chained pipeline's STT provider MUST be constructed with the
    same sample rate the audio source is sending. The iOS HFP / glasses
    path streams 24 kHz mono PCM16; before the fix the router built the
    STT provider with no explicit rate, so providers fell through to
    their 16 kHz default and Deepgram interpreted the 24 kHz buffer at
    16 kHz — slurring phonemes and producing the live "weird words then
    degrades" symptom. This test pins the per-session opt as the source
    of truth and the 24 kHz default when opts is silent.
    """
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    captured: dict = {}

    def _spy_get_stt_provider(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs
        provider = MagicMock()
        provider.close = AsyncMock()
        return provider

    def _spy_get_tts_provider(name, **kwargs):
        provider = MagicMock()
        provider.close = AsyncMock()
        return provider

    r = VoiceRouter(audio_pipeline=MagicMock(), orchestrator=MagicMock())
    chained = MagicMock()
    chained.open_session = AsyncMock(return_value=MagicMock())
    r.set_chained_pipeline(chained)

    # Stub both settings blocks the provider resolver consults so this
    # test pins the sample rate and nothing else. ``voice.chained`` is
    # the phone Settings panel's block and wins when populated; empty
    # here means the ``chained_fallback`` pair below is what applies.
    r._load_voice_settings = MagicMock(return_value={})
    r._load_audio_settings = MagicMock(return_value={
        "chained_fallback": {
            "stt_provider": "deepgram",
            "tts_provider": "elevenlabs",
        },
    })

    # ``open_chained_session`` imports both providers lazily inside the
    # function, so the patch target is the source module, not router.
    with patch("voice.stt_providers.get_stt_provider", side_effect=_spy_get_stt_provider), \
         patch("voice.tts_providers.get_tts_provider", side_effect=_spy_get_tts_provider):
        # Explicit per-session opt — overrides the 24 kHz default.
        await r.open_chained_session("sess-1", {"sample_rate": 24000})
        assert captured["name"] == "deepgram"
        assert captured["kwargs"]["sample_rate"] == 24000

        # No opt → falls back to the 24 kHz default the iOS path uses.
        captured.clear()
        await r.open_chained_session("sess-2", {})
        assert captured["kwargs"]["sample_rate"] == 24000

        # A different opt (e.g. a wristband at 16 kHz) is honoured.
        captured.clear()
        await r.open_chained_session("sess-3", {"sample_rate": 16000})
        assert captured["kwargs"]["sample_rate"] == 16000


def test_deepgram_provider_honours_sample_rate_in_url():
    """Belt-and-braces: the Deepgram URL must encode the configured
    sample_rate so the server-side STT decodes at the right rate. If
    this regresses, the live transcript "degraded" symptom returns
    even when ``open_chained_session`` is wired correctly."""
    from voice.stt_providers.deepgram import DEEPGRAM_WS_URL, DeepgramSTTProvider

    prov = DeepgramSTTProvider(api_key="dg-key", sample_rate=24000)
    url = DEEPGRAM_WS_URL.format(
        model=prov._model, sample_rate=prov._sample_rate, language=prov._language,
    )
    assert "sample_rate=24000" in url
