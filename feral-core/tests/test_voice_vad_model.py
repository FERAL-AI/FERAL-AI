"""The Silero VAD wrapper itself: framing, resampling, hysteresis.

Split from ``test_voice_vad_endpointing.py`` (which stubs the VAD and
tests the pipeline's reaction to boundaries) because these test the
signal path: does a 24kHz stream get resampled to the 16kHz the model
demands, are frames exactly 512 samples, does one quiet frame mid-word
end the utterance.

Most of it runs against a scripted probability function, so it needs
no weights. The two tests that exercise the real ONNX model are opt-in
via ``FERAL_TEST_VAD_MODEL=/path/to/silero_vad.onnx``, because the
autouse ``isolate_feral_home`` fixture points ``FERAL_HOME`` at a
tmpdir on every test and a unit suite must not download 2.2MB to run.
"""

from __future__ import annotations

import os
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from voice.vad import (
    VAD_FRAME_SAMPLES,
    VAD_SAMPLE_RATE,
    SileroVAD,
    VadConfig,
    VadEndpointer,
    VadEvent,
    load_endpointer,
    vad_available,
)

REAL_MODEL = os.environ.get("FERAL_TEST_VAD_MODEL", "")
needs_model = pytest.mark.skipif(
    not REAL_MODEL or not Path(REAL_MODEL).is_file(),
    reason="set FERAL_TEST_VAD_MODEL to a silero_vad.onnx to run",
)


class ScriptedVAD:
    """Stands in for the ONNX session. Scores by frame index."""

    def __init__(self, scores: list[float]):
        self.scores = list(scores)
        self.calls = 0
        self.frame_sizes: list[int] = []

    def reset(self) -> None:
        self.calls = 0

    def probability(self, frame) -> float:
        self.frame_sizes.append(len(frame))
        value = self.scores[self.calls] if self.calls < len(self.scores) else 0.0
        self.calls += 1
        return value


def _pcm(samples: int, value: int = 8000) -> bytes:
    return struct.pack(f"<{samples}h", *([value] * samples))


def test_frames_handed_to_the_model_are_exactly_the_size_it_expects():
    """Silero v5 is fixed-frame. Anything else returns garbage rather
    than raising, so a wrong frame size fails silently and forever."""
    vad = ScriptedVAD([0.0] * 100)
    endpointer = VadEndpointer(vad, sample_rate=VAD_SAMPLE_RATE)
    # Deliberately not a frame multiple.
    endpointer.feed(_pcm(VAD_FRAME_SAMPLES * 3 + 137))
    assert vad.calls == 3
    assert set(vad.frame_sizes) == {VAD_FRAME_SAMPLES}


def test_a_trailing_odd_byte_does_not_shift_every_later_sample():
    vad = ScriptedVAD([0.0] * 100)
    endpointer = VadEndpointer(vad, sample_rate=VAD_SAMPLE_RATE)
    endpointer.feed(_pcm(VAD_FRAME_SAMPLES) + b"\x7f")
    assert vad.calls == 1
    assert vad.frame_sizes == [VAD_FRAME_SAMPLES]


def test_resampling_is_continuous_across_chunk_boundaries():
    """Resampling each chunk independently drops a fraction of a sample
    at every boundary. At 10 chunks a second that drift reads to the
    VAD as a periodic click, i.e. as speech onset."""
    vad = ScriptedVAD([0.0] * 1000)
    endpointer = VadEndpointer(vad, sample_rate=24000)
    # 24kHz -> 16kHz is 2:3. One second of audio in 100ms chunks must
    # produce very close to 16000/512 = 31 frames.
    for _ in range(10):
        endpointer.feed(_pcm(2400))
    assert vad.calls in (30, 31), vad.calls


def test_speech_start_needs_sustained_speech_not_one_loud_frame():
    config = VadConfig(min_speech_ms=96, min_silence_ms=300)
    vad = ScriptedVAD([0.9, 0.0, 0.0, 0.9, 0.9, 0.9])
    endpointer = VadEndpointer(vad, sample_rate=VAD_SAMPLE_RATE, config=config)

    events = endpointer.feed(_pcm(VAD_FRAME_SAMPLES * 3))
    assert events == [], "a single loud frame must not open an utterance"

    events = endpointer.feed(_pcm(VAD_FRAME_SAMPLES * 3))
    assert events == [VadEvent.SPEECH_START]


def test_one_quiet_frame_mid_word_does_not_end_the_utterance():
    config = VadConfig(min_speech_ms=32, min_silence_ms=300)
    # start, then a dip, then speech again, then real silence.
    scores = [0.9] + [0.1] + [0.9] * 3 + [0.0] * 12
    vad = ScriptedVAD(scores)
    endpointer = VadEndpointer(vad, sample_rate=VAD_SAMPLE_RATE, config=config)

    events = endpointer.feed(_pcm(VAD_FRAME_SAMPLES * 5))
    assert events == [VadEvent.SPEECH_START]
    assert endpointer.speaking is True

    events = endpointer.feed(_pcm(VAD_FRAME_SAMPLES * 12))
    assert events == [VadEvent.SPEECH_END]
    assert endpointer.speaking is False


def test_min_silence_ms_controls_how_long_the_pause_must_be():
    config = VadConfig(min_speech_ms=32, min_silence_ms=300)
    # 300ms / 32ms per frame = 9 frames of silence needed.
    vad = ScriptedVAD([0.9] + [0.0] * 8)
    endpointer = VadEndpointer(vad, sample_rate=VAD_SAMPLE_RATE, config=config)
    events = endpointer.feed(_pcm(VAD_FRAME_SAMPLES * 9))
    assert VadEvent.SPEECH_START in events
    assert VadEvent.SPEECH_END not in events, "ended one frame too early"


def test_config_reads_from_settings_and_survives_junk():
    config = VadConfig.from_settings({
        "threshold": 0.7, "min_silence_ms": 250, "neg_threshold": "nonsense",
    })
    assert config.threshold == 0.7
    assert config.min_silence_ms == 250
    assert config.neg_threshold == 0.35, "a bad value must fall back, not crash"


def test_vad_reports_unavailable_without_weights(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    ready, reason = vad_available()
    assert ready is False
    assert "weights not downloaded" in reason
    assert load_endpointer() is None, "must degrade, never raise"


# --- against the real model --------------------------------------------


@needs_model
def test_real_model_loads_and_scores_a_frame():
    vad = SileroVAD(REAL_MODEL)
    silence = [0.0] * VAD_FRAME_SAMPLES
    prob = vad.probability(silence)
    assert 0.0 <= prob <= 1.0
    assert prob < 0.5, "silence must not read as speech"


@needs_model
@pytest.mark.skipif(
    not Path("/usr/bin/say").exists(), reason="needs macOS `say` for real speech",
)
def test_real_model_endpoints_real_speech_within_the_target_window(tmp_path):
    """The number the whole latency change rests on.

    Target is 250-400ms from the speaker stopping to end-of-speech. A
    tone will not do as a stimulus: Silero is trained on speech and
    scores a square wave near zero, which is how an earlier version of
    the latency bench concluded the VAD was a no-op.
    """
    wav_path = tmp_path / "speech.wav"
    subprocess.run(
        [
            "say", "--file-format=WAVE", "--data-format=LEI16@24000",
            "-o", str(wav_path), "Move my two o'clock to Thursday please",
        ],
        check=True, capture_output=True, timeout=60,
    )
    with wave.open(str(wav_path)) as handle:
        rate = handle.getframerate()
        speech = handle.readframes(handle.getnframes())

    endpointer = VadEndpointer(SileroVAD(REAL_MODEL), sample_rate=rate)
    stream = speech + b"\x00" * (rate * 2 * 2)
    frame_bytes = rate * 2 * 100 // 1000

    started = None
    ended = None
    for offset in range(0, len(stream), frame_bytes):
        position = offset / (rate * 2)
        for event in endpointer.feed(stream[offset: offset + frame_bytes]):
            if event == VadEvent.SPEECH_START and started is None:
                started = position
            elif event == VadEvent.SPEECH_END and ended is None:
                ended = position

    assert started is not None and started < 0.5, started
    assert ended is not None, "never endpointed real speech"
    lag_ms = (ended - len(speech) / (rate * 2)) * 1000.0
    assert 100.0 <= lag_ms <= 700.0, f"endpoint lag {lag_ms:.0f}ms out of band"
    # Sub-millisecond per 32ms frame is what lets this sit in the audio
    # hot path without a thread pool.
    assert endpointer.mean_infer_ms < 2.0, endpointer.mean_infer_ms
