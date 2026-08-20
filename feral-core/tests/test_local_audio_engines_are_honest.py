"""``AudioPipeline``'s local engines: never worked, said they were ready,
and quietly sent the audio to OpenAI instead.

Verified on the audit machine, 2026-08-12, both under
``HF_HUB_OFFLINE=1``::

    _LocalSTT._ensure_model  -> LocalEntryNotFoundError (no cached snapshot)
    _LocalTTS._ensure_voice  -> FileNotFoundError: 'en_US-lessac-medium.json'

while the same process logged::

    Audio pipeline ready - STT: local/faster-whisper (base),
                           TTS: local/piper (en_US-lessac-medium)

and ``feral voice providers`` on the same machine at the same time
reported both as "not configured - model not downloaded". Three
subsystems, three different answers.

The TTS one is the worse half. ``PiperVoice.load`` takes a path;
``_LocalTTS`` passed a bare voice name, so the call raised
``FileNotFoundError`` on every machine whether the voice was installed
or not. Local TTS through this pipeline has never emitted a byte. The
handler caught it and called ``_synthesize_cloud``, so selecting "local,
for privacy" silently meant "OpenAI", with an ``error`` log as the only
trace.

``voice/local_models.py`` states the rule this file was breaking, in its
own module docstring: "an operator who chose local engines for privacy
must not be silently rerouted to a cloud provider."
"""

from __future__ import annotations

import logging

import pytest

from perception.audio_pipeline import AudioPipeline, _LocalSTT, _LocalTTS
from voice.local_models import ModelUnavailable


@pytest.fixture()
def local_audio(monkeypatch, tmp_path):
    """Select local STT + TTS against an empty model store.

    ``HF_HOME`` is redirected too, not just ``FERAL_HOME``.
    ``local_models.faster_whisper_model_present`` deliberately also
    accepts an existing HuggingFace hub snapshot (so an operator who
    already has the weights is not made to download them twice), so a
    test that only isolates ``FERAL_HOME`` passes or fails depending on
    what is in the developer's ``~/.cache/huggingface``.

    That is not hypothetical. Running this file against the unfixed
    source made ``WhisperModel("base", compute_type="int8")`` pull
    141MB into that cache mid-test - which is the defect under test,
    demonstrating itself, and it then made the next run of the same
    test pass for the wrong reason.
    """
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hf" / "hub"))
    monkeypatch.setenv("FERAL_STT_PROVIDER", "local")
    monkeypatch.setenv("FERAL_TTS_PROVIDER", "local")
    monkeypatch.setenv("FERAL_STT_MODEL", "base")
    monkeypatch.setenv("FERAL_TTS_VOICE", "en_US-lessac-medium")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.delenv("FERAL_LOCAL_AUDIO_CLOUD_FALLBACK", raising=False)


# ---------------------------------------------------------------------------
# 1. A missing model is a typed error with a remedy, not a network call
# ---------------------------------------------------------------------------


def test_local_stt_refuses_to_download_mid_session(local_audio):
    pytest.importorskip("faster_whisper")
    with pytest.raises(ModelUnavailable) as excinfo:
        _LocalSTT()._ensure_model()
    assert "fetch-faster-whisper" in str(excinfo.value)


def test_local_tts_resolves_a_path_not_a_bare_name(local_audio):
    pytest.importorskip("piper")
    with pytest.raises(ModelUnavailable) as excinfo:
        _LocalTTS()._ensure_voice()
    # Not FileNotFoundError('en_US-lessac-medium.json'), which is what a
    # bare voice name produced whether or not the voice was installed.
    assert "fetch-piper" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 2. Boot says what is actually runnable
# ---------------------------------------------------------------------------


def test_boot_does_not_claim_a_missing_local_engine_is_ready(local_audio, caplog):
    with caplog.at_level(logging.INFO, logger="feral.audio"):
        pipeline = AudioPipeline()

    assert pipeline.local_stt_ready is False
    assert pipeline.local_tts_ready is False

    # There are two honest reasons an engine cannot run, and they need
    # different remedies: the PACKAGE is absent (pip install), or the
    # package is present and the MODEL is absent (fetch it). Asserting
    # the literal "not downloaded" accepted only the second, so this
    # test failed on any machine without piper-tts installed even though
    # the pipeline was reporting the situation correctly.
    #
    # The invariant is not a wording. It is that the detail names an
    # actionable cause instead of a vague "not configured".
    for engine, detail in (
        ("stt", pipeline.local_stt_detail),
        ("tts", pipeline.local_tts_detail),
    ):
        assert detail, f"local {engine} is not ready and gave no reason"
        assert ("not installed" in detail) or ("not downloaded" in detail), (
            f"local {engine} detail does not name an actionable cause: {detail!r}"
        )
        assert "pip install" in detail or "expected at" in detail or "fetch" in detail, (
            f"local {engine} detail names no remedy: {detail!r}"
        )

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "NOT READY" in text
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a local engine that cannot run was reported at INFO only"


# ---------------------------------------------------------------------------
# 3. No silent reroute to the cloud
# ---------------------------------------------------------------------------


async def test_failed_local_stt_does_not_reach_the_cloud(local_audio, caplog):
    pipeline = AudioPipeline()
    cloud_calls = []

    async def _cloud(*args, **kwargs):
        cloud_calls.append(args)
        return "transcript from openai"

    pipeline._transcribe_cloud = _cloud

    with caplog.at_level(logging.ERROR, logger="feral.audio"):
        out = await pipeline._transcribe_local(b"\x00" * 4000, "pcm16", 16000)

    assert out is None
    assert cloud_calls == [], (
        "audio from an operator who selected LOCAL STT was sent to OpenAI"
    )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "FERAL_LOCAL_AUDIO_CLOUD_FALLBACK" in text


async def test_failed_local_tts_does_not_reach_the_cloud(local_audio, caplog):
    pipeline = AudioPipeline()
    cloud_calls = []

    async def _cloud(*args, **kwargs):
        cloud_calls.append(args)
        return [{"chunk_index": 0, "encoding": "mp3", "data_b64": "", "is_final": True}]

    pipeline._synthesize_cloud = _cloud

    with caplog.at_level(logging.ERROR, logger="feral.audio"):
        out = await pipeline._synthesize_local("hello")

    assert out is None
    assert cloud_calls == []
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "FERAL_LOCAL_AUDIO_CLOUD_FALLBACK" in text


async def test_cloud_fallback_is_available_when_explicitly_asked_for(
    local_audio, monkeypatch
):
    """The capability is preserved, just no longer silent or default."""
    monkeypatch.setenv("FERAL_LOCAL_AUDIO_CLOUD_FALLBACK", "1")
    pipeline = AudioPipeline()

    async def _cloud(*_args, **_kwargs):
        return "transcript from openai"

    pipeline._transcribe_cloud = _cloud
    assert await pipeline._transcribe_local(b"\x00" * 4000, "pcm16", 16000) == (
        "transcript from openai"
    )
