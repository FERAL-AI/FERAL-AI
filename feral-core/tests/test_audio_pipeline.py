"""Tests for perception.audio_pipeline — AudioPipeline STT/TTS/VAD."""
from __future__ import annotations

import base64
import struct
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from perception.audio_pipeline import (
    AudioBuffer,
    AudioPipeline,
    _pcm16_to_wav,
    detect_local_audio_capabilities,
)


# ── AudioBuffer / VAD ────────────────────────────────────────────

def test_buffer_accumulates_chunks():
    buf = AudioBuffer("sess-1")
    buf.append(b"\x00" * 1000, "pcm16", 16000)
    buf.append(b"\x00" * 1000, "pcm16", 16000)
    assert buf._total_bytes == 2000
    data = buf.flush()
    assert len(data) == 2000
    assert buf._total_bytes == 0


def test_vad_not_triggered_without_silence():
    buf = AudioBuffer("sess-1")
    buf.append(b"\x00" * 3000, "pcm16", 16000)
    assert buf.vad_triggered() is False


def test_vad_triggered_after_silence():
    buf = AudioBuffer("sess-1")
    buf.append(b"\x00" * 3000, "pcm16", 16000)
    buf._last_chunk_time = time.time() - 2.0
    assert buf.vad_triggered() is True


def test_flush_empty_buffer():
    buf = AudioBuffer("sess-1")
    assert buf.flush() == b""


# ── STT provider routing ─────────────────────────────────────────

def test_stt_defaults_to_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("FERAL_STT_PROVIDER", raising=False)
    pipeline = AudioPipeline()
    assert pipeline._use_local_stt is False
    assert pipeline.available is True


def test_stt_local_when_env_set(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FERAL_STT_PROVIDER", "faster-whisper")
    pipeline = AudioPipeline()
    assert pipeline._use_local_stt is True


# ── TTS provider routing ─────────────────────────────────────────

def test_tts_local_when_env_set(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FERAL_TTS_PROVIDER", "piper")
    pipeline = AudioPipeline()
    assert pipeline._use_local_tts is True


# ── PCM to WAV conversion ────────────────────────────────────────

def test_pcm16_to_wav_produces_valid_header():
    pcm = struct.pack("<h", 0) * 100
    wav = _pcm16_to_wav(pcm, sample_rate=16000)
    assert wav[:4] == b"RIFF"
    assert b"WAVE" in wav[:12]


def test_pcm16_to_wav_clamps_low_sample_rate():
    wav = _pcm16_to_wav(b"\x00\x00" * 50, sample_rate=100)
    assert wav[:4] == b"RIFF"


# ── detect_local_audio_capabilities ──────────────────────────────

def test_capabilities_when_packages_missing(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _block_audio(name, *a, **kw):
        if name in ("faster_whisper", "piper"):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _block_audio)
    caps = detect_local_audio_capabilities()
    assert caps["local_stt"] is False
    assert caps["local_tts"] is False
    assert caps["stt_models"] == []


# ── process_audio_chunk end-to-end ───────────────────────────────

async def test_process_audio_chunk_transcribes_on_final(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("FERAL_STT_PROVIDER", raising=False)
    pipeline = AudioPipeline()
    pipeline._transcribe = AsyncMock(return_value="hello world")

    audio = base64.b64encode(b"\x00" * 2000).decode()
    result = await pipeline.process_audio_chunk("s1", audio, 0, is_final=True)
    assert result == "hello world"


async def test_process_audio_chunk_returns_none_when_short(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    pipeline = AudioPipeline()

    audio = base64.b64encode(b"\x00" * 10).decode()
    result = await pipeline.process_audio_chunk("s1", audio, 0, is_final=True)
    assert result is None


# ── local capability probe must not claim what it cannot do ──────

class TestCapabilitiesReportPresenceNotImports:
    """`local_stt` was True on `import faster_whisper` alone.

    `pip install 'feral-ai[stt]'` installs the package and downloads no
    weights, which is the normal state of a fresh install. In that
    state the probe reported local STT as available and `stt_models`
    returned the list of *valid model names* rather than the ones on
    disk, so `feral doctor` printed
    "models: tiny, base, small, medium, large" over a machine that had
    none of them. Transcription then failed at first use with "model
    not downloaded", having been told twice that it would work.
    """

    def _caps(self, monkeypatch, *, importable=True, present=()):
        import perception.audio_pipeline as ap
        import voice.local_models as lm

        monkeypatch.setattr(
            lm, "faster_whisper_model_present", lambda m: m in present,
        )
        monkeypatch.setattr(lm, "piper_voice_present", lambda v: False)
        if not importable:
            import builtins
            real = builtins.__import__

            def fake(name, *a, **k):
                if name in ("faster_whisper", "piper"):
                    raise ImportError(name)
                return real(name, *a, **k)

            monkeypatch.setattr(builtins, "__import__", fake)
        return ap.detect_local_audio_capabilities()

    def test_installed_with_no_weights_is_not_available(self, monkeypatch):
        caps = self._caps(monkeypatch, present=())
        assert caps["stt_importable"] is True
        assert caps["local_stt"] is False, (
            "reported local STT as working with no model on disk"
        )
        assert caps["stt_models_present"] == []

    def test_installed_with_weights_is_available(self, monkeypatch):
        caps = self._caps(monkeypatch, present=("small",))
        assert caps["local_stt"] is True
        assert caps["stt_models_present"] == ["small"]

    def test_present_never_claims_a_model_that_is_absent(self, monkeypatch):
        caps = self._caps(monkeypatch, present=("small",))
        assert "base" not in caps["stt_models_present"]
        assert "large" not in caps["stt_models_present"]

    def test_the_picker_list_stays_complete(self, monkeypatch):
        """`stt_models` is what you may CHOOSE, not what you have.

        /api/audio/providers/stt/faster-whisper/models is a picker. If
        it listed only downloaded models a fresh install would show an
        empty dropdown and no way to pick something to fetch.
        """
        caps = self._caps(monkeypatch, present=())
        assert "base" in caps["stt_models"]
        assert "large" in caps["stt_models"]

    def test_tts_follows_the_same_rule(self, monkeypatch):
        caps = self._caps(monkeypatch, present=())
        assert caps["local_tts"] is False
        assert caps["tts_voices_present"] == []

    def test_a_probe_failure_never_raises(self, monkeypatch):
        """doctor calls this to describe a broken machine."""
        import voice.local_models as lm

        def boom(*a, **k):
            raise OSError("filesystem is on fire")

        monkeypatch.setattr(lm, "faster_whisper_model_present", boom)
        monkeypatch.setattr(lm, "piper_voice_present", boom)
        from perception.audio_pipeline import detect_local_audio_capabilities

        caps = detect_local_audio_capabilities()
        assert caps["local_stt"] is False
        assert caps["local_tts"] is False
