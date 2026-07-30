"""The chained fallback must actually speak — with the frames clients send.

Voice-collapse audit (2026-07), finding 4. Two independent blockers,
either one fatal, both fixed here:

(a) ``handle_audio`` only flushed on ``is_final=True``, and
    ``_flush_pipeline`` had no other caller. No client ever sets that
    flag — ``feral-client-v2/src/lib/voiceRealtime.js`` hardcodes
    ``is_final: false`` and the brain forwards what it received — so
    after a fallback morph the session accepted audio forever and
    emitted nothing at all. The utterance now ends on the STT
    provider's own end-of-speech signal, or on a server-side silence
    timer for buffered providers that have none.

(b) ``open_session`` never called ``stt_provider.open_stream()``. For
    Deepgram that is the call that opens the WebSocket and starts the
    receive loop, so ``send_audio`` no-op'd on a None ``_ws`` and not
    one byte of audio was ever transcribed.

Every test here drives the pipeline with realistic ``is_final: False``
frames only.
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import MagicMock

import pytest

from voice.chained_pipeline import ChainedVoicePipeline, VoiceState
from voice.stt_providers import STTProvider, TranscriptFragment
from voice.tts_providers import TTSProvider

CHUNK = base64.b64encode(b"\x00" * 320).decode()


@pytest.fixture(autouse=True)
async def _reap_pipeline_tasks():
    """Cancel per-session tasks tests leave behind by not closing.

    The pipeline owns a long-lived STT consumer per session; without
    this the loop teardown prints "Task was destroyed but it is
    pending" for every test that skips ``close_session``.
    """
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


class StreamingSTT(STTProvider):
    """Deepgram-shaped provider: fragments arrive on the open stream."""

    def __init__(self):
        self.opened = False
        self.audio_chunks: list[bytes] = []
        self.flush_calls = 0
        self._queue: asyncio.Queue = asyncio.Queue()

    async def open_stream(self):
        self.opened = True
        while True:
            frag = await self._queue.get()
            if frag is None:
                break
            yield frag

    async def send_audio(self, audio_bytes: bytes) -> None:
        self.audio_chunks.append(audio_bytes)

    async def flush(self) -> None:
        self.flush_calls += 1

    async def close(self) -> None:
        await self._queue.put(None)

    def emit(self, text: str, *, speech_final: bool):
        """Push a fragment the way Deepgram's receive loop does."""
        self._queue.put_nowait(TranscriptFragment(
            text=text,
            is_partial=not speech_final,
            is_final=speech_final,
            speech_final=speech_final,
        ))


class BufferedSTT(STTProvider):
    """Whisper-shaped provider: silent until someone calls ``flush()``."""

    def __init__(self, transcript: str = "turn on the lights"):
        self.opened = False
        self._transcript = transcript
        self._buffer = bytearray()
        self._result_queue: asyncio.Queue = asyncio.Queue()

    async def open_stream(self):
        self.opened = True
        while True:
            frag = await self._result_queue.get()
            if frag is None:
                break
            yield frag

    async def send_audio(self, audio_bytes: bytes) -> None:
        self._buffer.extend(audio_bytes)

    async def flush(self) -> None:
        if not self._buffer:
            return
        self._buffer.clear()
        await self._result_queue.put(TranscriptFragment(
            text=self._transcript, is_partial=False, is_final=True, speech_final=True,
        ))

    async def close(self) -> None:
        await self._result_queue.put(None)


class FakeTTS(TTSProvider):
    def __init__(self, chunk: bytes = b"mp3-bytes"):
        self._chunk = chunk

    async def synthesize(self, text: str):
        if text:
            yield self._chunk


class FakeLLM:
    def __init__(self, response: str = "Lights are on."):
        self._response = response
        self.conversation_history: dict[str, list[dict]] = {}
        self.calls: list[str] = []

    async def handle_command_stream(self, session_id: str, text: str, context=None):
        self.calls.append(text)
        self.conversation_history.setdefault(session_id, []).extend([
            {"role": "user", "text": text},
            {"role": "assistant", "text": self._response},
        ])


def _frame_types(frames: list[dict], kind: str) -> list[dict]:
    return [f for f in frames if f["type"] == kind]


# ── (b) the recognition stream is actually opened ───────────────────


@pytest.mark.asyncio
async def test_open_session_starts_the_stt_stream():
    pipeline = ChainedVoicePipeline()
    stt = StreamingSTT()

    session = await pipeline.open_session("sess-1", stt, FakeTTS(), FakeLLM())
    # Let the consumer task reach its first await.
    await asyncio.sleep(0)

    assert stt.opened is True, "open_stream is what connects a streaming provider"
    assert session._stt_task is not None and not session._stt_task.done()


@pytest.mark.asyncio
async def test_audio_reaches_the_provider_without_any_final_flag():
    pipeline = ChainedVoicePipeline()
    stt = StreamingSTT()
    await pipeline.open_session("sess-1", stt, FakeTTS(), FakeLLM())

    for i in range(3):
        await pipeline.handle_audio("sess-1", CHUNK, chunk_index=i, is_final=False)

    assert len(stt.audio_chunks) == 3


# ── (a) end-of-speech drives the flush ──────────────────────────────


@pytest.mark.asyncio
async def test_streaming_speech_final_runs_the_whole_chain():
    """Deepgram's own end-of-speech signal answers the user."""
    frames: list[dict] = []

    async def capture(_sid, frame):
        frames.append(frame)

    pipeline = ChainedVoicePipeline()
    stt = StreamingSTT()
    llm = FakeLLM("Lights are on.")
    await pipeline.open_session("sess-1", stt, FakeTTS(), llm, send_frame=capture)

    await pipeline.handle_audio("sess-1", CHUNK, chunk_index=0, is_final=False)
    stt.emit("turn on", speech_final=False)
    stt.emit("turn on the lights", speech_final=True)
    await asyncio.sleep(0.05)

    assert llm.calls == ["turn on the lights"]
    audio = [f for f in _frame_types(frames, "audio_chunk") if f["payload"]["data_b64"]]
    assert audio, "the fallback path must emit TTS audio"
    assert base64.b64decode(audio[0]["payload"]["data_b64"]) == b"mp3-bytes"
    assert [f["payload"]["state"] for f in _frame_types(frames, "voice_state")][-1] == "idle"


@pytest.mark.asyncio
async def test_partial_fragments_do_not_trigger_the_llm():
    pipeline = ChainedVoicePipeline()
    stt = StreamingSTT()
    llm = FakeLLM()
    await pipeline.open_session("sess-1", stt, FakeTTS(), llm)

    await pipeline.handle_audio("sess-1", CHUNK, chunk_index=0, is_final=False)
    stt.emit("turn", speech_final=False)
    stt.emit("turn on", speech_final=False)
    await asyncio.sleep(0.05)

    assert llm.calls == []


@pytest.mark.asyncio
async def test_silence_timer_flushes_a_buffered_provider():
    """Whisper/Groq emit nothing until flushed — the timer does it."""
    pipeline = ChainedVoicePipeline(silence_flush_seconds=0.05)
    stt = BufferedSTT("turn on the lights")
    llm = FakeLLM("Done.")
    await pipeline.open_session("sess-1", stt, FakeTTS(), llm)

    await pipeline.handle_audio("sess-1", CHUNK, chunk_index=0, is_final=False)
    await pipeline.handle_audio("sess-1", CHUNK, chunk_index=1, is_final=False)
    await asyncio.sleep(0.2)

    assert llm.calls == ["turn on the lights"]


@pytest.mark.asyncio
async def test_silence_timer_extends_while_the_user_keeps_talking():
    """Mid-utterance gaps between chunks must not cut the user off."""
    pipeline = ChainedVoicePipeline(silence_flush_seconds=0.12)
    stt = BufferedSTT()
    llm = FakeLLM()
    await pipeline.open_session("sess-1", stt, FakeTTS(), llm)

    for i in range(4):
        await pipeline.handle_audio("sess-1", CHUNK, chunk_index=i, is_final=False)
        await asyncio.sleep(0.05)
    assert llm.calls == [], "flushed while the user was still speaking"

    await asyncio.sleep(0.25)
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_one_utterance_produces_exactly_one_llm_turn():
    """speech_final + silence timer must not both fire the same words."""
    pipeline = ChainedVoicePipeline(silence_flush_seconds=0.05)
    stt = StreamingSTT()
    llm = FakeLLM()
    await pipeline.open_session("sess-1", stt, FakeTTS(), llm)

    await pipeline.handle_audio("sess-1", CHUNK, chunk_index=0, is_final=False)
    stt.emit("turn on the lights", speech_final=True)
    await asyncio.sleep(0.3)

    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_second_utterance_still_works():
    """The session keeps serving turns after the first one completes."""
    pipeline = ChainedVoicePipeline()
    stt = StreamingSTT()
    llm = FakeLLM()
    await pipeline.open_session("sess-1", stt, FakeTTS(), llm)

    await pipeline.handle_audio("sess-1", CHUNK, chunk_index=0, is_final=False)
    stt.emit("first question", speech_final=True)
    await asyncio.sleep(0.05)

    await pipeline.handle_audio("sess-1", CHUNK, chunk_index=1, is_final=False)
    stt.emit("second question", speech_final=True)
    await asyncio.sleep(0.05)

    assert llm.calls == ["first question", "second question"]


@pytest.mark.asyncio
async def test_transcripts_are_emitted_for_partials_and_finals():
    frames: list[dict] = []

    async def capture(_sid, frame):
        frames.append(frame)

    pipeline = ChainedVoicePipeline()
    stt = StreamingSTT()
    await pipeline.open_session("sess-1", stt, FakeTTS(), FakeLLM(), send_frame=capture)

    await pipeline.handle_audio("sess-1", CHUNK, chunk_index=0, is_final=False)
    stt.emit("turn on", speech_final=False)
    stt.emit("turn on the lights", speech_final=True)
    await asyncio.sleep(0.05)

    transcripts = _frame_types(frames, "transcript")
    assert any(f["payload"]["is_partial"] for f in transcripts)
    assert any(
        f["payload"]["text"] == "turn on the lights" and not f["payload"]["is_partial"]
        for f in transcripts
    )


@pytest.mark.asyncio
async def test_close_session_reaps_the_stt_task():
    pipeline = ChainedVoicePipeline()
    stt = StreamingSTT()
    session = await pipeline.open_session("sess-1", stt, FakeTTS(), FakeLLM())
    task = session._stt_task

    await pipeline.close_session("sess-1")

    assert task.done()


@pytest.mark.asyncio
async def test_stt_stream_failure_surfaces_an_error_state():
    """A dead recognition stream must not fail silently."""
    frames: list[dict] = []

    async def capture(_sid, frame):
        frames.append(frame)

    class ExplodingSTT(StreamingSTT):
        async def open_stream(self):
            raise RuntimeError("deepgram handshake refused")
            yield  # pragma: no cover - generator marker

    pipeline = ChainedVoicePipeline()
    await pipeline.open_session(
        "sess-1", ExplodingSTT(), FakeTTS(), FakeLLM(), send_frame=capture,
    )
    await asyncio.sleep(0.05)

    errors = [
        f for f in _frame_types(frames, "voice_state")
        if f["payload"]["state"] == VoiceState.ERROR.value
    ]
    assert errors
    assert "deepgram handshake refused" in errors[0]["payload"]["error"]


# ── router wiring: a morphed session opens a real chained session ───


@pytest.mark.asyncio
async def test_router_opens_chained_session_with_a_started_stream(monkeypatch):
    """End-to-end through the router: morph -> audio -> spoken answer."""
    from voice.router import VoiceRouter

    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    stt = StreamingSTT()
    tts = FakeTTS()
    monkeypatch.setattr("voice.stt_providers.get_stt_provider", lambda *a, **k: stt)
    monkeypatch.setattr("voice.tts_providers.get_tts_provider", lambda *a, **k: tts)

    sent: list = []

    async def send(_sid, msg):
        sent.append(msg)

    llm = FakeLLM("All set.")
    pipeline = ChainedVoicePipeline()
    router = VoiceRouter(
        audio_pipeline=MagicMock(), orchestrator=llm, send_to_session=send,
    )
    router.set_chained_pipeline(pipeline)

    await router.open_chained_session("sess-1", {"stt_provider": "deepgram"})
    await asyncio.sleep(0)
    assert stt.opened is True

    await router.handle_chained_audio("sess-1", CHUNK, chunk_index=0, is_final=False)
    stt.emit("turn on the lights", speech_final=True)
    await asyncio.sleep(0.05)

    assert llm.calls == ["turn on the lights"]
    audio = [
        m for m in sent
        if getattr(m, "type", None) == "audio_chunk" and m.payload.get("data_b64")
    ]
    assert audio, "morphed session produced no audio"

    await pipeline.close_session("sess-1")
