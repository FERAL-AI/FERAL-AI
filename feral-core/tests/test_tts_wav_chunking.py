"""Every TTS chunk must decode on its own.

A WAV is a RIFF header followed by raw samples. `_synthesize_local`
sliced the file at 32KB byte offsets and labelled every piece
`encoding: "wav"`, so only the first carried a header. Clients decode a
`tts_chunk` independently (`RealtimeVoiceEngine.handleTtsChunk` calls
`decodeAudioData` per frame), so the rest did not merely fall silent,
they raised.

Measured in Chrome on a 3 second reply cut at 32KB, before the fix:

    chunk 0  32768 bytes  ok, 0.742s
    chunk 1  32768 bytes  EncodingError
    chunk 2  32768 bytes  EncodingError
    chunk 3  32768 bytes  EncodingError
    chunk 4   1272 bytes  EncodingError

    decoded 1, failed 4, playable 0.742s of 3.0s

and after:

    decoded 5, failed 0, playable 3.000s

Cloud TTS returns MP3, whose frames are self-describing and survive an
arbitrary cut, which is why only the local Piper path was affected and
why nobody noticed.
"""

from __future__ import annotations

import io
import math
import struct
import wave

import pytest

from perception.audio_pipeline import (
    _pcm16_to_wav,
    _split_wav_into_playable_chunks,
)

RATE = 22050


def tone(seconds: float = 3.0, rate: int = RATE) -> bytes:
    n = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * 180 * i / rate)))
        for i in range(n)
    )


def parse(chunk: bytes) -> tuple[int, int, int, bytes]:
    """Decode a chunk the way a player would, or raise."""
    with wave.open(io.BytesIO(chunk), "rb") as w:
        return (
            w.getnchannels(), w.getsampwidth(), w.getframerate(),
            w.readframes(w.getnframes()),
        )


class TestEveryChunkStandsAlone:
    def test_all_chunks_parse_as_wav(self):
        wav = _pcm16_to_wav(tone(), RATE)
        chunks = _split_wav_into_playable_chunks(wav, 32 * 1024)
        assert len(chunks) > 1, "test needs a payload big enough to split"
        for i, c in enumerate(chunks):
            # Raises wave.Error on a headerless slice, which is the bug.
            parse(c)

    def test_every_chunk_starts_with_a_riff_header(self):
        wav = _pcm16_to_wav(tone(), RATE)
        for i, c in enumerate(_split_wav_into_playable_chunks(wav, 32 * 1024)):
            assert c[:4] == b"RIFF", f"chunk {i} has no RIFF header"
            assert c[8:12] == b"WAVE", f"chunk {i} is not a WAVE file"

    def test_no_audio_is_lost_or_duplicated(self):
        pcm = tone()
        wav = _pcm16_to_wav(pcm, RATE)
        rebuilt = b"".join(
            parse(c)[3] for c in _split_wav_into_playable_chunks(wav, 32 * 1024)
        )
        assert rebuilt == pcm

    def test_the_chunks_carry_the_original_sample_rate(self):
        """A wrong rate plays the reply at the wrong pitch and speed."""
        for rate in (16000, 22050, 24000):
            wav = _pcm16_to_wav(tone(1.0, rate), rate)
            for c in _split_wav_into_playable_chunks(wav, 8 * 1024):
                assert parse(c)[2] == rate

    def test_chunks_are_cut_on_sample_boundaries(self):
        """An odd offset byte-swaps every following sample into noise."""
        wav = _pcm16_to_wav(tone(), RATE)
        for c in _split_wav_into_playable_chunks(wav, 32 * 1024 + 1):
            assert len(parse(c)[3]) % 2 == 0


class TestEdges:
    def test_audio_smaller_than_one_chunk_is_one_chunk(self):
        wav = _pcm16_to_wav(tone(0.1), RATE)
        chunks = _split_wav_into_playable_chunks(wav, 32 * 1024)
        assert len(chunks) == 1
        parse(chunks[0])

    def test_empty_input_produces_no_chunks(self):
        assert _split_wav_into_playable_chunks(b"", 32 * 1024) == []

    def test_unparseable_audio_is_passed_through_whole(self):
        """Better one oversized playable blob than five broken ones."""
        junk = b"not a wav at all" * 100
        assert _split_wav_into_playable_chunks(junk, 32 * 1024) == [junk]

    def test_stereo_is_not_relabelled_as_mono(self):
        """Re-wrapping stereo as mono would play it at double speed."""
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(tone(1.0) * 2)
        stereo = out.getvalue()
        chunks = _split_wav_into_playable_chunks(stereo, 32 * 1024)
        assert chunks == [stereo]
        assert parse(chunks[0])[0] == 2

    def test_a_tiny_chunk_size_still_cuts_whole_frames(self):
        wav = _pcm16_to_wav(tone(0.2), RATE)
        for c in _split_wav_into_playable_chunks(wav, 1):
            assert len(parse(c)[3]) % 2 == 0


class TestTheSynthesisPathUsesIt:
    @pytest.mark.asyncio
    async def test_local_tts_emits_independently_playable_chunks(self):
        """Drive the real method, not just the helper."""
        from perception.audio_pipeline import AudioPipeline

        pipeline = AudioPipeline()
        wav = _pcm16_to_wav(tone(), RATE)

        class _FakePiper:
            def synthesize(self, text):
                return wav

        pipeline._local_tts = _FakePiper()
        chunks = await pipeline._synthesize_local("hello there")

        assert chunks and len(chunks) > 1
        import base64
        for i, c in enumerate(chunks):
            assert c["encoding"] == "wav"
            assert c["chunk_index"] == i
            parse(base64.b64decode(c["data_b64"]))
        assert chunks[-1]["is_final"] is True
        assert not any(c["is_final"] for c in chunks[:-1])
