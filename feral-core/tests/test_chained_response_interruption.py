"""Response interruption uses synthetic streams and performs no external actions."""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from voice.chained_pipeline import ChainedVoicePipeline
from voice.router import VoiceRouter
from voice.vad import VadEvent
from voice.stt_providers import TranscriptFragment


class SyntheticSTT:
    async def open_stream(self):
        await asyncio.Future()
        yield None

    async def flush(self):
        pass

    async def close(self):
        pass


class BufferedSyntheticSTT(SyntheticSTT):
    def __init__(self):
        self.buffer = bytearray()
        self._result_queue = asyncio.Queue()

    async def send_audio(self, audio_bytes):
        self.buffer.extend(audio_bytes)

    async def flush(self):
        if self.buffer:
            self.buffer.clear()
            self._result_queue.put_nowait(TranscriptFragment(text="second overlapping instruction", is_partial=False, is_final=True))


class SyntheticTTS:
    output_format = "pcm16"
    sample_rate = 24000

    def __init__(self):
        self.started = asyncio.Event()

    async def synthesize(self, text):
        self.started.set()
        yield bytes(4800)
        await asyncio.Future()

    async def close(self):
        pass


class LateTTS(SyntheticTTS):
    """Provider that yields one final chunk despite receiving cancellation."""
    async def synthesize(self, text):
        self.started.set()
        yield bytes(4800)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            yield bytes(4800)


class FiniteTTS(SyntheticTTS):
    async def synthesize(self, text):
        self.started.set()
        yield bytes(4800)


class SyntheticCommand:
    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = False
        self.completed = 0
        self.calls = 0
        self.texts = []
        self.active = 0
        self.max_active = 0
        self.conversation_history = {}

    async def send(self, session_id, message):
        pass

    async def handle_command_stream(self, session_id, text, context=None):
        self.calls += 1
        self.texts.append(text)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.send(session_id, {"type": "stream_delta", "payload": {"delta": "The synthetic task is still working. "}})
            await self.release.wait()
            await self.send(session_id, {"type": "stream_delta", "payload": {"delta": "Second sentence. "}})
            self.completed += 1
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active -= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tts_type", [SyntheticTTS, LateTTS])
@pytest.mark.parametrize("interrupt_path", ["router", "vad"])
async def test_response_interrupt_preserves_running_command(tts_type, interrupt_path):
    pipeline = ChainedVoicePipeline(vad_enabled=False, barge_in=True)
    router = VoiceRouter()
    router.set_chained_pipeline(pipeline)
    command, tts, frames = SyntheticCommand(), tts_type(), []

    async def send(_sid, frame):
        frames.append(frame)

    session = await pipeline.open_session("owned", SyntheticSTT(), tts, command, send_frame=send)
    try:
        session._pending_finals.append("synthetic command")
        await pipeline._drive_turn(session, wait=False)
        await asyncio.wait_for(tts.started.wait(), 1)
        assert await router.cancel_chained_response("foreign") is False
        assert not command.cancelled and not session._turn_task.done()
        if interrupt_path == "router":
            assert await router.cancel_chained_response("owned") is True
        else:
            session._endpointer = SimpleNamespace(feed=lambda _pcm: [VadEvent.SPEECH_START])
            assert await pipeline._feed_vad(session, bytes(4800)) is False
            assert session._output_interrupted
        assert await router.cancel_chained_response("owned") is False
        await asyncio.sleep(0)
        assert not command.cancelled, "response interruption cancelled the running command child"
        command.release.set()
        await asyncio.wait_for(asyncio.shield(session._turn_task), 1)
        assert command.calls == 1 and command.completed == 1
        marker = next(i for i, frame in enumerate(frames) if frame["type"] == "voice_cancel")
        assert frames[marker]["payload"]["agent_task_cancel_requested"] is False
        assert not any(frame["type"] == "audio_chunk" for frame in frames[marker + 1:])
        assert session._speech_task is None
        assert await router.cancel_chained_response("owned") is False
        # Another utterance gets its own output; the old response is not replayed.
        session.tts_provider = FiniteTTS()
        session._pending_finals.append("a different synthetic command")
        await pipeline._drive_turn(session, wait=True)
        assert command.calls == 2 and command.completed == 2
        assert any(frame["type"] == "audio_chunk" for frame in frames[marker + 1:])
    finally:
        command.release.set()
        await pipeline.close_session("owned")


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "close"])
async def test_explicit_task_cancellation_still_cancels_command(stop):
    pipeline = ChainedVoicePipeline(vad_enabled=False)
    command, tts = SyntheticCommand(), SyntheticTTS()
    session = await pipeline.open_session("owned", SyntheticSTT(), tts, command)
    try:
        session._pending_finals.append("synthetic command")
        await pipeline._drive_turn(session, wait=False)
        await asyncio.wait_for(tts.started.wait(), 1)
        assert await pipeline.interrupt_output("owned") is True
        if stop == "cancel":
            assert await pipeline.cancel("owned", reason="explicit_task_abort") is True
        else:
            await pipeline.close_session("owned")
        assert command.cancelled and command.completed == 0 and command.calls == 1
        assert session._speech_task is None
    finally:
        await pipeline.close_session("owned")


@pytest.mark.asyncio
async def test_overlapping_utterance_flushes_after_running_command_finishes():
    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=0.01)
    command, tts, stt = SyntheticCommand(), SyntheticTTS(), BufferedSyntheticSTT()
    session = await pipeline.open_session("owned", stt, tts, command)
    try:
        session._pending_finals.append("first held instruction")
        await pipeline._drive_turn(session, wait=False)
        await asyncio.wait_for(tts.started.wait(), 1)
        assert await pipeline.interrupt_output("owned") is True
        session.tts_provider = FiniteTTS()
        session._endpointer = SimpleNamespace(feed=lambda _pcm: [VadEvent.SPEECH_END])
        await pipeline.handle_audio("owned", base64.b64encode(bytes(4800)).decode())
        # Both the actual VAD EOU and silence backstop occur while the
        # first command remains held; no third packet/flush is supplied.
        await asyncio.sleep(0.02)
        assert command.calls == 1 and stt.buffer
        command.release.set()
        for _ in range(100):
            if command.completed == 2:
                break
            await asyncio.sleep(0.001)
        assert command.texts == ["first held instruction", "second overlapping instruction"]
        assert command.completed == 2 and not command.cancelled
        await asyncio.sleep(0.02)
        assert command.calls == 2 and command.max_active == 1
        assert not session._queued_flush and not stt.buffer
    finally:
        command.release.set()
        await pipeline.close_session("owned")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "close", "failure", "replacement"])
async def test_queued_endpoint_is_not_replayed_after_failed_or_cancelled_turn(ending):
    class FailingCommand(SyntheticCommand):
        async def handle_command_stream(self, session_id, text, context=None):
            await super().handle_command_stream(session_id, text, context)
            raise RuntimeError("synthetic command failure")

    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=0.01)
    command = FailingCommand() if ending == "failure" else SyntheticCommand()
    tts, stt = SyntheticTTS(), BufferedSyntheticSTT()
    session = await pipeline.open_session("owned", stt, tts, command)
    try:
        session._pending_finals.append("first held instruction")
        await pipeline._drive_turn(session, wait=False)
        await asyncio.wait_for(tts.started.wait(), 1)
        assert await pipeline.interrupt_output("owned") is True
        session._endpointer = SimpleNamespace(feed=lambda _pcm: [VadEvent.SPEECH_END])
        await pipeline.handle_audio("owned", base64.b64encode(bytes(4800)).decode())
        assert session._queued_flush
        if ending == "cancel":
            assert await pipeline.cancel("owned", reason="explicit_task_abort") is True
        elif ending == "close":
            await pipeline.close_session("owned")
        elif ending == "replacement":
            await pipeline.open_session("owned", SyntheticSTT(), FiniteTTS(), command)
        else:
            command.release.set()
            await asyncio.wait_for(asyncio.shield(session._turn_task), 1)
        await asyncio.sleep(0.02)
        assert command.calls == 1 and not session._queued_flush
        assert command.max_active == 1
    finally:
        command.release.set()
        await pipeline.close_session("owned")


@pytest.mark.asyncio
async def test_replaced_session_refuses_late_old_output():
    pipeline = ChainedVoicePipeline(vad_enabled=False)
    command, tts, frames = SyntheticCommand(), SyntheticTTS(), []

    async def send(_sid, frame):
        frames.append(frame)

    old = await pipeline.open_session("owned", SyntheticSTT(), tts, command, send_frame=send)
    try:
        old._pending_finals.append("synthetic command")
        await pipeline._drive_turn(old, wait=False)
        await asyncio.wait_for(tts.started.wait(), 1)
        replacement = await pipeline.open_session("owned", SyntheticSTT(), FiniteTTS(), command, send_frame=send)
        before = len(frames)
        await pipeline._emit_audio_chunk(old, "AAAA", 99, False, owner=old._turn_task)
        await pipeline._emit_frame(old, {"type": "voice_cancel", "payload": {}})
        old._pending_finals.append("stale synthetic command")
        await pipeline._drive_turn(old, wait=True)
        old._endpointer = SimpleNamespace(feed=lambda _pcm: [VadEvent.SPEECH_START])
        assert await pipeline._feed_vad(old, bytes(4800)) is False
        assert len(frames) == before
        assert await pipeline.interrupt_output("owned") is False
        assert replacement is pipeline.get_session("owned")
        assert command.cancelled and command.calls == 1
    finally:
        await pipeline.close_session("owned")


@pytest.mark.asyncio
async def test_command_failure_stops_speech_without_replaying():
    class FailingCommand(SyntheticCommand):
        async def handle_command_stream(self, session_id, text, context=None):
            await super().handle_command_stream(session_id, text, context)
            raise RuntimeError("synthetic command failure")

    pipeline = ChainedVoicePipeline(vad_enabled=False)
    command, tts = FailingCommand(), SyntheticTTS()
    session = await pipeline.open_session("owned", SyntheticSTT(), tts, command)
    try:
        session._pending_finals.append("synthetic command")
        await pipeline._drive_turn(session, wait=False)
        await asyncio.wait_for(tts.started.wait(), 1)
        command.release.set()
        await asyncio.wait_for(asyncio.shield(session._turn_task), 1)
        assert command.calls == 1 and session._speech_task is None
        assert await pipeline.interrupt_output("owned") is False
    finally:
        await pipeline.close_session("owned")
