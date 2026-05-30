"""Lane 05 (Wave 2) — Voice provider probes + Cartesia TTS module.

Closes the THESIS_SCENARIOS S4 prerequisite "every voice provider
gated on vault key + probe":

  * Each STT / TTS / Realtime provider has a registered probe.
  * Probes return ``no_key`` (configured=False) when no key, or
    ``ok``/``unauthorized``/``http_error`` once a key is set.
  * Cartesia TTS module is importable, registers itself with the
    chained pipeline registry, builds a valid Sonic-2 payload.
  * voice_provider_catalogue() returns the structured list the
    /api/voice/providers REST surface (Lane 05 ) consumes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import voice.tts_providers.cartesia  # noqa: F401  (registers cartesia provider)
import voice.tts_providers.elevenlabs  # noqa: F401
import voice.tts_providers.openai_tts  # noqa: F401
import voice.stt_providers.deepgram  # noqa: F401
import voice.stt_providers.groq_whisper  # noqa: F401
import voice.stt_providers.openai_whisper  # noqa: F401
from security.probe import (  # noqa: E402
    clear_probe_cache,
    probe,
    registered_probe_ids,
    voice_provider_catalogue,
    VOICE_PROVIDER_CATALOGUE,
)
from voice.tts_providers import _PROVIDER_REGISTRY as _TTS_REGISTRY  # noqa: E402
from voice.tts_providers.cartesia import CartesiaTTSProvider  # noqa: E402

VOICE_PROVIDER_IDS = (
    "openai_realtime",
    "gemini_live",
    "deepgram",
    "groq_whisper",
    "openai_whisper",
    "elevenlabs",
    "cartesia",
    "openai_tts",
)


# ── Probe registration ────────────────────────────────────────────


@pytest.mark.parametrize("provider_id", VOICE_PROVIDER_IDS)
def test_voice_provider_probe_is_registered(provider_id):
    assert provider_id in registered_probe_ids(), (
        f"missing probe for voice provider {provider_id!r}"
    )


@pytest.mark.parametrize("provider_id", VOICE_PROVIDER_IDS)
@pytest.mark.asyncio
async def test_voice_provider_probe_returns_no_key_when_unset(
    provider_id, monkeypatch
):
    """Without an env var or vault entry, every voice provider probe
    returns ``configured=False`` with reason='no_key' — the doctor
    surface needs that signal to render Settings → Voice correctly."""
    clear_probe_cache()
    for env_key in (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPGRAM_API_KEY",
        "GROQ_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        monkeypatch.delenv(env_key, raising=False)

    result = await probe(provider_id, vault=None, force=True)
    assert result.ok is False
    assert result.reason == "no_key"
    assert result.provider == provider_id


# ── Cartesia provider sanity ──────────────────────────────────────


def test_cartesia_provider_is_registered():
    assert "cartesia" in _TTS_REGISTRY
    assert _TTS_REGISTRY["cartesia"] is CartesiaTTSProvider


def test_cartesia_requires_api_key():
    with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
        CartesiaTTSProvider(api_key="")


def test_cartesia_default_construction():
    provider = CartesiaTTSProvider(api_key="ck-test")
    assert provider._model_id == "sonic-2"
    assert provider._output_container == "mp3"
    assert provider._sample_rate == 22050
    assert provider.use_websocket is False


def test_cartesia_rejects_bad_output_container():
    with pytest.raises(ValueError, match="output_container"):
        CartesiaTTSProvider(api_key="ck-test", output_container="ogg")


@pytest.mark.asyncio
async def test_cartesia_synthesize_no_op_on_empty_text():
    provider = CartesiaTTSProvider(api_key="ck-test")
    chunks = []
    async for chunk in provider.synthesize(""):
        chunks.append(chunk)
    assert chunks == []


@pytest.mark.asyncio
async def test_cartesia_rest_payload_shape(monkeypatch):
    """The REST path must send a Sonic-2 shaped payload + correct
    headers. We intercept httpx.AsyncClient.stream so the test runs
    offline yet exercises the real payload-build logic."""
    captured: dict = {}

    class _FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.request = None

        async def aread(self):
            return b""

        async def aiter_bytes(self, chunk_size):  # noqa: D401
            yield b"audio-chunk-1"
            yield b"audio-chunk-2"

    class _FakeStream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            captured.update(kwargs)

        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, *args):
            return False

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            return _FakeStream(method=method, url=url, **kwargs)

    monkeypatch.setattr("voice.tts_providers.cartesia.httpx.AsyncClient", _FakeClient)

    provider = CartesiaTTSProvider(api_key="ck-test", voice_id="abc-123")
    chunks = []
    async for chunk in provider.synthesize("hello world"):
        chunks.append(chunk)

    assert chunks == [b"audio-chunk-1", b"audio-chunk-2"]
    assert captured["url"] == "https://api.cartesia.ai/tts/bytes"
    headers = captured["headers"]
    assert headers["X-API-Key"] == "ck-test"
    assert headers["Cartesia-Version"] == "2024-11-13"
    payload = captured["json"]
    assert payload["model_id"] == "sonic-2"
    assert payload["transcript"] == "hello world"
    assert payload["voice"] == {"mode": "id", "id": "abc-123"}
    assert payload["output_format"]["container"] == "mp3"
    assert payload["language"] == "en"


# ── Voice catalogue surface ( hook) ─────────────────────────────


def test_voice_provider_catalogue_lists_all_eight():
    catalogue = voice_provider_catalogue()
    assert len(catalogue) == 8
    ids = {entry["id"] for entry in catalogue}
    assert ids == set(VOICE_PROVIDER_IDS)
    kinds = {entry["kind"] for entry in catalogue}
    assert kinds == {"realtime", "stt", "tts"}


def test_voice_provider_catalogue_kinds_are_consistent():
    by_id = {pid: kind for pid, kind, _ in VOICE_PROVIDER_CATALOGUE}
    assert by_id["openai_realtime"] == "realtime"
    assert by_id["gemini_live"] == "realtime"
    assert by_id["deepgram"] == "stt"
    assert by_id["elevenlabs"] == "tts"
    assert by_id["cartesia"] == "tts"
