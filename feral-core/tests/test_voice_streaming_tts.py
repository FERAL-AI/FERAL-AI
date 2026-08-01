"""Overlapping the LLM and TTS stages, and streaming the audio out.

Before this the chained turn ran strictly in series: await the whole
LLM answer, scrape it out of ``conversation_history``, synthesise all
of it, emit one audio frame. Time-to-first-audio was therefore
generation time plus synthesis time, every turn.

Three behaviours are pinned here:

* the LLM's tokens are observed as they are published, not after the
  call returns, and each finished sentence is synthesised immediately;
* PCM providers get their audio emitted incrementally, in frames the
  client can play without decoding;
* encoded (MP3) providers still get whole, independently decodable
  files per chunk, because a slice of an MP3 is not an MP3 and
  ``decodeAudioData`` rejects it.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from voice.chained_pipeline import PCM_CHUNK_BYTES, ChainedVoicePipeline
from voice.sentence_stream import SentenceAccumulator, split_sentences
from voice.stt_providers import STTProvider, TranscriptFragment
from voice.tts_providers import TTSProvider

SILENCE = base64.b64encode(b"\x00" * 480).decode()


@pytest.fixture(autouse=True)
async def _reap_pipeline_tasks():
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


class BufferedSTT(STTProvider):
    def __init__(self, transcript: str = "what is on today"):
        self._transcript = transcript
        self._result_queue: asyncio.Queue = asyncio.Queue()

    async def open_stream(self):
        while True:
            frag = await self._result_queue.get()
            if frag is None:
                break
            yield frag

    async def send_audio(self, audio_bytes: bytes) -> None:
        return None

    async def flush(self) -> None:
        await self._result_queue.put(
            TranscriptFragment(
                text=self._transcript, is_partial=False,
                is_final=True, speech_final=True,
            )
        )

    async def close(self) -> None:
        await self._result_queue.put(None)


class RecordingTTS(TTSProvider):
    def __init__(self, output_format="pcm", chunk=b"\x01\x02" * 16, repeats=1):
        self.output_format = output_format
        self.sample_rate = 24000
        self.calls: list[str] = []
        self._chunk = chunk
        self._repeats = repeats

    async def synthesize(self, text: str):
        self.calls.append(text)
        for _ in range(self._repeats):
            yield self._chunk

    async def close(self) -> None:
        return None


class StreamingLLM:
    """Publishes tokens through ``send``, the way the orchestrator does."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[str] = []
        self.conversation_history: dict[str, list[dict]] = {}
        self.sent: list = []
        self.tts_calls_at_half = None

    async def send(self, session_id, message):
        self.sent.append(message)

    async def handle_command_stream(self, session_id: str, text: str, context=None):
        self.calls.append(text)
        tokens = self.reply.split(" ")
        for index, token in enumerate(tokens):
            piece = token if index == 0 else " " + token
            await self.send(session_id, {
                "type": "stream_delta",
                "payload": {"delta": piece, "is_final": False},
            })
            await asyncio.sleep(0)
        self.conversation_history.setdefault(session_id, []).append(
            {"role": "assistant", "text": self.reply}
        )


class NonStreamingLLM:
    """No ``send``, so no tap: the buffered fallback has to carry it."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[str] = []
        self.conversation_history: dict[str, list[dict]] = {}

    async def handle_command_stream(self, session_id: str, text: str, context=None):
        self.calls.append(text)
        self.conversation_history.setdefault(session_id, []).append(
            {"role": "assistant", "text": self.reply}
        )


async def _run_turn(pipeline, stt, tts, llm, frames):
    async def capture(_sid, frame):
        frames.append(frame)

    await pipeline.open_session(
        session_id="sess-tts",
        stt_provider=stt,
        tts_provider=tts,
        llm_handle=llm,
        send_frame=capture,
    )
    await pipeline.handle_audio("sess-tts", SILENCE, is_final=True)


REPLY = (
    "The kettle is already on. "
    "I moved your two o'clock to Thursday so the afternoon is clear. "
    "Nothing else needs you before then."
)


@pytest.mark.asyncio
async def test_llm_stream_is_synthesised_sentence_by_sentence():
    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=30.0)
    tts = RecordingTTS()
    frames: list[dict] = []
    await _run_turn(pipeline, BufferedSTT(), tts, StreamingLLM(REPLY), frames)

    assert len(tts.calls) == 3, tts.calls
    assert tts.calls[0] == "The kettle is already on."
    assert tts.calls[1].startswith("I moved your two o'clock")
    assert tts.calls[2] == "Nothing else needs you before then."


@pytest.mark.asyncio
async def test_a_non_streaming_handle_still_gets_audio():
    """No tap available means slower, never silent."""
    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=30.0)
    tts = RecordingTTS()
    frames: list[dict] = []
    llm = NonStreamingLLM(REPLY)
    await _run_turn(pipeline, BufferedSTT(), tts, llm, frames)

    assert llm.calls == ["what is on today"]
    assert len(tts.calls) == 3
    assert any(f["type"] == "audio_chunk" and f["payload"]["data_b64"] for f in frames)


@pytest.mark.asyncio
async def test_the_tap_is_removed_after_the_turn():
    """A subscriber left behind would leak deltas into the next turn."""
    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=30.0)
    llm = StreamingLLM(REPLY)
    frames: list[dict] = []
    await _run_turn(pipeline, BufferedSTT(), RecordingTTS(), llm, frames)

    tap = pipeline._tap_for(llm)
    assert tap is not None
    assert tap.subscriber_count == 0


@pytest.mark.asyncio
async def test_the_tap_still_delivers_the_frame_to_the_client():
    """Observation only. The orchestrator's own send must not be eaten."""
    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=30.0)
    llm = StreamingLLM(REPLY)
    frames: list[dict] = []
    await _run_turn(pipeline, BufferedSTT(), RecordingTTS(), llm, frames)

    assert len(llm.sent) == len(REPLY.split(" "))


@pytest.mark.asyncio
async def test_pcm_is_emitted_incrementally_in_playable_frames():
    # Three provider chunks of one and a half frames each, so the
    # pipeline has to coalesce and split rather than pass through.
    chunk = b"\x01\x02" * (PCM_CHUNK_BYTES // 2)
    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=30.0)
    tts = RecordingTTS(output_format="pcm", chunk=chunk, repeats=3)
    frames: list[dict] = []
    await _run_turn(pipeline, BufferedSTT(), tts, StreamingLLM(REPLY), frames)

    audio = [f["payload"] for f in frames if f["type"] == "audio_chunk"]
    carrying = [p for p in audio if p["data_b64"]]
    assert len(carrying) > 3, "PCM must stream, not arrive as one frame"
    for payload in carrying:
        assert payload["encoding"] == "pcm16"
        assert payload["sample_rate"] == 24000
        raw = base64.b64decode(payload["data_b64"])
        assert len(raw) <= PCM_CHUNK_BYTES
        assert len(raw) % 2 == 0, "a frame must not split a sample"

    # Indices strictly increase so the client can spot a dropped frame.
    indices = [p["chunk_index"] for p in carrying]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)

    final = [p for p in audio if p["is_final"]]
    assert len(final) == 1 and final[0]["data_b64"] == ""


@pytest.mark.asyncio
async def test_mp3_frames_stay_whole_files_one_per_chunk():
    """The v2026.5.28 invariant. One frame must be one decodable file."""
    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=30.0)
    tts = RecordingTTS(output_format="mp3", chunk=b"\xff\xfb" + b"\x00" * 200, repeats=4)
    frames: list[dict] = []
    await _run_turn(pipeline, BufferedSTT(), tts, StreamingLLM(REPLY), frames)

    audio = [f["payload"] for f in frames if f["type"] == "audio_chunk"]
    carrying = [p for p in audio if p["data_b64"]]
    # One frame per sentence: the four transport slices of each
    # synthesis are joined back into one complete file.
    assert len(carrying) == 3
    for payload in carrying:
        assert payload["encoding"] == "mp3"
        assert len(base64.b64decode(payload["data_b64"])) == 4 * 202


@pytest.mark.asyncio
async def test_provider_chunk_floor_is_honoured():
    """macOS ``say`` pays ~1s per invocation; tiny chunks underrun."""

    class BigChunkTTS(RecordingTTS):
        min_chunk_chars = 500

    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=30.0)
    tts = BigChunkTTS()
    frames: list[dict] = []
    await _run_turn(pipeline, BufferedSTT(), tts, StreamingLLM(REPLY), frames)

    assert len(tts.calls) == 1, tts.calls
    assert tts.calls[0].startswith("The kettle")
    assert tts.calls[0].endswith("before then.")


# --- sentence splitting -------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hello there my old friend. How are you doing today?",
         ["Hello there my old friend.", "How are you doing today?"]),
        # ...but a sentence under ``min_chars`` rides along with the
        # next one rather than costing its own synthesis round trip.
        ("Hi there. How are you doing today?",
         ["Hi there. How are you doing today?"]),
        # Abbreviations are not sentence ends.
        ("Dr. Smith will see you now, and that is the whole plan.",
         ["Dr. Smith will see you now, and that is the whole plan."]),
        # Decimals are not sentence ends.
        ("The total came to 3.5 kilos of flour for the week.",
         ["The total came to 3.5 kilos of flour for the week."]),
        # A short fragment rides along with the next one.
        ("Yes. That is booked for Thursday afternoon.",
         ["Yes. That is booked for Thursday afternoon."]),
        # Domains must not split.
        ("Everything is on feral.io under the docs tab for now.",
         ["Everything is on feral.io under the docs tab for now."]),
    ],
)
def test_split_sentences(text, expected):
    assert split_sentences(text) == expected


def test_accumulator_emits_as_soon_as_a_sentence_completes():
    acc = SentenceAccumulator()
    out: list[str] = []
    for token in "The kettle is already on. And ".split(" "):
        out.extend(acc.push(token + " "))
    assert out == ["The kettle is already on."]
    assert acc.buffer.strip() == "And"


def test_accumulator_cuts_unpunctuated_text_at_max_chars():
    acc = SentenceAccumulator(min_chars=10, max_chars=40)
    out = acc.push("word " * 30)
    assert out, "an unpunctuated wall of text must still stream"
    assert all(len(chunk) <= 40 for chunk in out)


def test_accumulator_full_text_round_trips():
    acc = SentenceAccumulator()
    acc.push(REPLY)
    acc.flush()
    joined = acc.full_text()
    for sentence in ("kettle", "Thursday", "before then"):
        assert sentence in joined
