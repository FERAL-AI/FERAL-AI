"""
Tests for ChainedVoicePipeline — state machine transitions, LLM integration
(mocked), flush semantics, error propagation.
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice.chained_pipeline import ChainedVoicePipeline, ChainedSession, VoiceState
from voice.stt_providers import STTProvider, TranscriptFragment
from voice.tts_providers import TTSProvider


# ── Helpers ──────────────────────────────────────────────────────────

class FakeSTTProvider(STTProvider):
    """Buffered STT that stores audio and returns a canned transcript."""

    def __init__(self, transcript: str = "hello world"):
        self._transcript = transcript
        self._buffer = bytearray()
        self._result_queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def open_stream(self):
        while True:
            frag = await self._result_queue.get()
            if frag is None:
                break
            yield frag

    async def send_audio(self, audio_bytes: bytes) -> None:
        self._buffer.extend(audio_bytes)

    async def flush(self) -> None:
        if self._buffer:
            await self._result_queue.put(
                TranscriptFragment(
                    text=self._transcript,
                    is_partial=False,
                    is_final=True,
                    speech_final=True,
                )
            )
            self._buffer.clear()

    async def close(self) -> None:
        self._closed = True
        await self._result_queue.put(None)


class FakeTTSProvider(TTSProvider):
    """TTS that yields a fixed audio chunk."""

    def __init__(self, chunk: bytes = b"audio-data-123"):
        self._chunk = chunk

    async def synthesize(self, text: str):
        if text:
            yield self._chunk


class FakeLLMHandle:
    """Mimics the brain orchestrator with conversation history."""

    def __init__(self, response: str = "I can help with that."):
        self._response = response
        self.conversation_history: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []

    async def handle_command_stream(self, session_id: str, text: str, context=None):
        self.calls.append((session_id, text))
        if session_id not in self.conversation_history:
            self.conversation_history[session_id] = []
        self.conversation_history[session_id].append(
            {"role": "user", "text": text}
        )
        self.conversation_history[session_id].append(
            {"role": "assistant", "text": self._response}
        )


@pytest.fixture(autouse=True)
async def _reap_pipeline_tasks():
    """Cancel per-session tasks tests leave behind by not closing.

    ``open_session`` now owns a long-lived STT consumer task (plus a
    silence timer while audio is arriving). Tests that skip
    ``close_session`` would otherwise leave them pending at loop
    teardown, which asyncio reports as "Task was destroyed but it is
    pending" noise on every run.
    """
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


# ── Tests ────────────────────────────────────────────────────────────

class TestChainedPipelineInit:
    def test_creates_empty_sessions(self):
        pipeline = ChainedVoicePipeline()
        assert pipeline._sessions == {}


class TestOpenSession:
    @pytest.mark.asyncio
    async def test_open_session_returns_session(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider()
        tts = FakeTTSProvider()
        llm = FakeLLMHandle()

        session = await pipeline.open_session("sess-1", stt, tts, llm)

        assert isinstance(session, ChainedSession)
        assert session.session_id == "sess-1"
        assert session.state == VoiceState.IDLE

    @pytest.mark.asyncio
    async def test_open_session_replaces_existing(self):
        pipeline = ChainedVoicePipeline()
        stt1 = FakeSTTProvider()
        stt2 = FakeSTTProvider()
        tts = FakeTTSProvider()
        llm = FakeLLMHandle()

        await pipeline.open_session("sess-1", stt1, tts, llm)
        session2 = await pipeline.open_session("sess-1", stt2, tts, llm)

        assert session2.stt_provider is stt2
        assert stt1._closed


class TestStateMachine:
    @pytest.mark.asyncio
    async def test_idle_to_listening_on_audio(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider()
        tts = FakeTTSProvider()
        llm = FakeLLMHandle()
        frames = []

        async def capture_frame(sid, frame):
            frames.append(frame)

        session = await pipeline.open_session("sess-1", stt, tts, llm, send_frame=capture_frame)
        assert session.state == VoiceState.IDLE

        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=False)

        assert session.state == VoiceState.LISTENING

    @pytest.mark.asyncio
    async def test_full_state_cycle(self):
        """idle → listening → processing → speaking → idle"""
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider("test input")
        tts = FakeTTSProvider(b"\xff\xfb\x90\x00")
        llm = FakeLLMHandle("test response")
        states_seen = []

        async def capture_frame(sid, frame):
            if frame["type"] == "voice_state":
                states_seen.append(frame["payload"]["state"])

        await pipeline.open_session("sess-1", stt, tts, llm, send_frame=capture_frame)

        audio_b64 = base64.b64encode(b"\x00" * 320).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=False)
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=1, is_final=True)

        assert "listening" in states_seen
        assert "processing" in states_seen
        assert "speaking" in states_seen
        assert states_seen[-1] == "idle"

    @pytest.mark.asyncio
    async def test_error_state_on_llm_failure(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider("hello")
        tts = FakeTTSProvider()
        llm = FakeLLMHandle()
        llm.handle_command_stream = AsyncMock(side_effect=RuntimeError("LLM down"))

        states_seen = []

        async def capture_frame(sid, frame):
            if frame["type"] == "voice_state":
                states_seen.append(frame["payload"]["state"])

        await pipeline.open_session("sess-1", stt, tts, llm, send_frame=capture_frame)

        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=True)

        assert "error" in states_seen
        assert states_seen[-1] == "idle"


class TestFlushSemantics:
    @pytest.mark.asyncio
    async def test_empty_transcript_returns_to_idle(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider("")
        tts = FakeTTSProvider()
        llm = FakeLLMHandle()
        states = []

        async def capture_frame(sid, frame):
            if frame["type"] == "voice_state":
                states.append(frame["payload"]["state"])

        await pipeline.open_session("sess-1", stt, tts, llm, send_frame=capture_frame)

        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=True)

        assert states[-1] == "idle"
        assert len(llm.calls) == 0

    @pytest.mark.asyncio
    async def test_transcript_forwarded_to_llm(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider("turn on the lights")
        tts = FakeTTSProvider()
        llm = FakeLLMHandle("Done!")

        await pipeline.open_session("sess-1", stt, tts, llm)

        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=True)

        assert len(llm.calls) == 1
        assert llm.calls[0] == ("sess-1", "turn on the lights")


class TestLLMIntegration:
    @pytest.mark.asyncio
    async def test_llm_response_triggers_tts(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider("say hi")
        tts = FakeTTSProvider(b"mp3-audio")
        llm = FakeLLMHandle("Hi there!")
        audio_chunks = []

        async def capture_frame(sid, frame):
            if frame["type"] == "audio_chunk":
                audio_chunks.append(frame)

        await pipeline.open_session("sess-1", stt, tts, llm, send_frame=capture_frame)

        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=True)

        data_chunks = [c for c in audio_chunks if c["payload"]["data_b64"]]
        assert len(data_chunks) >= 1
        decoded = base64.b64decode(data_chunks[0]["payload"]["data_b64"])
        assert decoded == b"mp3-audio"

    @pytest.mark.asyncio
    async def test_tts_final_marker_emitted(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider("test")
        tts = FakeTTSProvider(b"data")
        llm = FakeLLMHandle("response")
        audio_chunks = []

        async def capture_frame(sid, frame):
            if frame["type"] == "audio_chunk":
                audio_chunks.append(frame)

        await pipeline.open_session("sess-1", stt, tts, llm, send_frame=capture_frame)

        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=True)

        final_chunks = [c for c in audio_chunks if c["payload"]["is_final"]]
        assert len(final_chunks) == 1


class TestTranscriptEmission:
    @pytest.mark.asyncio
    async def test_transcript_frames_emitted(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider("hello world")
        tts = FakeTTSProvider()
        llm = FakeLLMHandle("yo")
        transcript_frames = []

        async def capture_frame(sid, frame):
            if frame["type"] == "transcript":
                transcript_frames.append(frame)

        await pipeline.open_session("sess-1", stt, tts, llm, send_frame=capture_frame)

        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=True)

        assert len(transcript_frames) >= 1
        assert any(f["payload"]["text"] == "hello world" for f in transcript_frames)


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_tts_error_surfaces_error_state(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider("hello")
        llm = FakeLLMHandle("response text")
        states = []

        class FailingTTS(TTSProvider):
            async def synthesize(self, text):
                if False:
                    yield b""
                raise RuntimeError("TTS service down")

        async def capture_frame(sid, frame):
            if frame["type"] == "voice_state":
                states.append(frame["payload"])

        await pipeline.open_session("sess-1", stt, FailingTTS(), llm, send_frame=capture_frame)

        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("sess-1", audio_b64, chunk_index=0, is_final=True)

        error_states = [s for s in states if s["state"] == "error"]
        assert len(error_states) >= 1
        assert "TTS service down" in error_states[0].get("error", "")

    @pytest.mark.asyncio
    async def test_no_session_handle_audio_is_noop(self):
        pipeline = ChainedVoicePipeline()
        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        await pipeline.handle_audio("nonexistent", audio_b64, chunk_index=0, is_final=True)


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_close_session_removes_and_cleans(self):
        pipeline = ChainedVoicePipeline()
        stt = FakeSTTProvider()
        tts = FakeTTSProvider()
        llm = FakeLLMHandle()

        await pipeline.open_session("sess-1", stt, tts, llm)
        assert "sess-1" in pipeline._sessions

        await pipeline.close_session("sess-1")
        assert "sess-1" not in pipeline._sessions
        assert stt._closed

    @pytest.mark.asyncio
    async def test_shutdown_closes_all(self):
        pipeline = ChainedVoicePipeline()
        stt1 = FakeSTTProvider()
        stt2 = FakeSTTProvider()
        tts = FakeTTSProvider()
        llm = FakeLLMHandle()

        await pipeline.open_session("sess-1", stt1, tts, llm)
        await pipeline.open_session("sess-2", stt2, tts, llm)

        await pipeline.shutdown()
        assert len(pipeline._sessions) == 0

    @pytest.mark.asyncio
    async def test_close_nonexistent_is_noop(self):
        pipeline = ChainedVoicePipeline()
        await pipeline.close_session("nope")
