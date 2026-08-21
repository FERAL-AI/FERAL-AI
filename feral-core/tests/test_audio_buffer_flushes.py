"""A continuously streaming client must actually get transcribed.

`AudioBuffer` ended an utterance on one signal: a gap of more than 1.5s
between arriving packets. Every real client streams back to back at
100ms (BrowserNode, voiceRealtime and usePerceptionShare all send
`pcm16` continuously), so the gap never elapsed while someone was
speaking and the only thing that ever flushed the buffer was
`is_final`. A HUP `audio_frame` has no `is_final` field at all, so on
the device path nothing was transcribed, ever.

Measured on the code before this change, with 100ms PCM16 chunks fed in
back to back and no `is_final`:

    chunks sent      : 50 (160000 bytes = 5.0s of audio)
    _transcribe calls: 0
    still buffered   : 160000 bytes

The class docstring called the VAD "energy-based" the whole time. No
energy was computed anywhere in the file.

These tests drive the real pipeline with real PCM16 and assert on how
many times STT was actually asked to run.
"""

from __future__ import annotations

import asyncio
import base64
import math
import struct

import pytest

from perception.audio_pipeline import (
    MAX_UTTERANCE_SEC,
    MIN_VOICED_BYTES,
    SILENCE_END_SEC,
    AudioBuffer,
    AudioPipeline,
    _chunk_duration_sec,
    _is_silent,
)

RATE = 16000


def speech(ms: int = 100, amp: int = 8000) -> bytes:
    """A 220Hz tone, which is in the range a voice occupies."""
    n = int(RATE * ms / 1000)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * 220 * i / RATE)))
        for i in range(n)
    )


def silence(ms: int = 100) -> bytes:
    return b"\x00\x00" * int(RATE * ms / 1000)


class _Recorder:
    """A pipeline whose STT call is recorded rather than performed."""

    def __init__(self):
        self.pipeline = AudioPipeline()
        self.calls: list[int] = []

        async def _fake(audio, *a, **k):
            self.calls.append(len(audio))
            return "transcribed"

        self.pipeline._transcribe = _fake
        self._i = 0

    async def send(self, chunk: bytes, session: str = "s", is_final: bool = False):
        await self.pipeline.process_audio_chunk(
            session, base64.b64encode(chunk).decode(), self._i, is_final,
            encoding="pcm16", sample_rate=RATE,
        )
        self._i += 1


def _run(coro):
    return asyncio.run(coro)


class TestTheOriginalFailure:
    def test_continuous_speech_then_a_pause_is_transcribed(self):
        """The exact case that produced zero transcriptions."""
        r = _Recorder()

        async def scenario():
            for _ in range(20):      # 2.0s of speech
                await r.send(speech())
            for _ in range(10):      # 1.0s of quiet, still streaming
                await r.send(silence())

        _run(scenario())
        assert len(r.calls) == 1, (
            f"expected one utterance, got {len(r.calls)}; "
            "a continuously streaming client is never transcribed"
        )
        assert r.calls[0] > MIN_VOICED_BYTES

    def test_nothing_is_left_stranded_in_the_buffer(self):
        r = _Recorder()

        async def scenario():
            for _ in range(20):
                await r.send(speech())
            for _ in range(10):
                await r.send(silence())

        _run(scenario())
        # Only the tail after the flush should remain, not 5s of audio.
        assert r.pipeline.get_buffer("s").pending_bytes < 10_000


class TestTheBackstop:
    def test_a_speaker_who_never_pauses_is_still_transcribed(self):
        """15s of unbroken speech must not buffer forever."""
        r = _Recorder()

        async def scenario():
            for _ in range(150):     # 15.0s, no pause anywhere
                await r.send(speech())

        _run(scenario())
        assert len(r.calls) >= 1, "an unbroken speaker is never transcribed"

    def test_the_cut_happens_at_the_declared_ceiling(self):
        """It must fire at the ceiling, within one chunk, and not before.

        Summing 0.1 repeatedly does not land exactly on 12.0, so this
        asserts the property that matters (the cut happens promptly at
        the ceiling) rather than an exact chunk count.
        """
        buf = AudioBuffer("s")
        assert not buf.overflowing()
        chunks = 0
        while not buf.overflowing() and chunks < 1000:
            buf.append(speech(), "pcm16", RATE)
            chunks += 1
        assert buf.overflowing(), "the ceiling never fired"
        # Fired at the ceiling, not a chunk early and not seconds late.
        assert MAX_UTTERANCE_SEC <= buf.duration_sec < MAX_UTTERANCE_SEC + 0.2

    def test_compressed_audio_still_gets_a_ceiling(self):
        """Opus bytes are not samples, so only the ceiling can save it."""
        buf = AudioBuffer("s")
        for _ in range(200):
            buf.append(b"\x00" * 4000, "opus", RATE)
        assert buf.overflowing()
        # And its silence is never guessed at from compressed bytes.
        assert not _is_silent(b"\x00" * 4000, "opus")


class TestItDoesNotOverfire:
    def test_a_silent_room_never_reaches_stt(self):
        """Flushing silence would bill an STT call per second of nothing."""
        r = _Recorder()

        async def scenario():
            for _ in range(60):      # 6s of pure silence
                await r.send(silence())

        _run(scenario())
        assert r.calls == [], f"silence was sent to STT {len(r.calls)} times"

    def test_a_short_blip_is_not_an_utterance(self):
        buf = AudioBuffer("s")
        buf.append(speech(ms=20), "pcm16", RATE)   # under MIN_VOICED_BYTES
        for _ in range(20):
            buf.append(silence(), "pcm16", RATE)
        assert not buf.speech_ended()

    def test_a_pause_shorter_than_the_threshold_does_not_split_a_sentence(self):
        buf = AudioBuffer("s")
        for _ in range(20):
            buf.append(speech(), "pcm16", RATE)
        # Half the threshold: a breath between words, not an ending.
        for _ in range(int(SILENCE_END_SEC * 10 / 2)):
            buf.append(silence(), "pcm16", RATE)
        assert not buf.speech_ended()


class TestTheEnergyMeasurement:
    def test_it_tells_speech_from_silence(self):
        assert _is_silent(silence(), "pcm16")
        assert not _is_silent(speech(), "pcm16")

    def test_it_accepts_the_encoding_aliases_clients_send(self):
        for name in ("pcm16", "pcm", "linear16", "PCM16"):
            assert _is_silent(silence(), name), name

    def test_an_odd_trailing_byte_does_not_raise(self):
        """A truncated frame is a bad frame, not a crash."""
        assert _is_silent(silence()[:-1], "pcm16") in (True, False)
        assert _is_silent(b"\x01", "pcm16") is False
        assert _is_silent(b"", "pcm16") is False

    def test_pcm16_duration_is_exact(self):
        # 100ms at 16kHz mono 16-bit is 3200 bytes.
        assert _chunk_duration_sec(b"\x00" * 3200, "pcm16", RATE) == pytest.approx(0.1)
        assert _chunk_duration_sec(b"\x00" * 3200, "pcm16", 8000) == pytest.approx(0.2)

    def test_a_zero_sample_rate_does_not_divide_by_zero(self):
        assert _chunk_duration_sec(b"\x00" * 3200, "pcm16", 0) == pytest.approx(0.1)


class TestFlushResetsEverything:
    def test_a_flushed_buffer_starts_clean(self):
        """A stale silent-run would end the next utterance instantly."""
        buf = AudioBuffer("s")
        for _ in range(20):
            buf.append(speech(), "pcm16", RATE)
        for _ in range(10):
            buf.append(silence(), "pcm16", RATE)
        assert buf.speech_ended()
        buf.flush()
        assert not buf.speech_ended()
        assert not buf.overflowing()
        assert buf.duration_sec == 0.0
        assert buf.voiced_bytes == 0
        assert buf.pending_bytes == 0
