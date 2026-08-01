"""Server-side VAD endpointing, and the timer it replaces.

The pipeline used to end an utterance by noticing that packets had
stopped arriving. That only works if the client stops sending, which
only the client's own 1.5s energy gate made it do, which is why a turn
cost about 2.3s of dead air before the STT request went out.

These tests pin the new contract:

* a continuously-streaming client gets endpointed (it never did before,
  the session just listened forever);
* the VAD, not the timer, is what fires;
* the timer is still armed as a backstop, because the VAD can only
  score frames that arrive;
* no VAD means the old behaviour, unchanged.

The VAD itself is stubbed here. Silero's real weights are not in the
test environment (``isolate_feral_home`` points ``FERAL_HOME`` at a
tmpdir on every test), and a unit test of the pipeline's reaction to
speech boundaries should not depend on a 2.2MB download either way.
``tests/test_voice_vad_model.py`` covers the real model when it is
present.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from voice.chained_pipeline import ChainedVoicePipeline, VoiceState
from voice.stt_providers import STTProvider, TranscriptFragment
from voice.tts_providers import TTSProvider
from voice.vad import VadEvent

SILENCE = base64.b64encode(b"\x00" * 480).decode()


@pytest.fixture(autouse=True)
async def _reap_pipeline_tasks():
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


class ScriptedEndpointer:
    """Emits a canned sequence of VAD events, one list per fed chunk."""

    def __init__(self, script: list[list[VadEvent]]):
        self._script = list(script)
        self.fed = 0

    def feed(self, _pcm: bytes) -> list[VadEvent]:
        self.fed += 1
        if not self._script:
            return []
        return self._script.pop(0)


class BufferedSTT(STTProvider):
    def __init__(self, transcript: str = "turn on the lights"):
        self._transcript = transcript
        self._result_queue: asyncio.Queue = asyncio.Queue()
        self.flush_calls = 0
        self._closed = False

    async def open_stream(self):
        while True:
            frag = await self._result_queue.get()
            if frag is None:
                break
            yield frag

    async def send_audio(self, audio_bytes: bytes) -> None:
        return None

    async def flush(self) -> None:
        self.flush_calls += 1
        await self._result_queue.put(
            TranscriptFragment(
                text=self._transcript, is_partial=False,
                is_final=True, speech_final=True,
            )
        )

    async def close(self) -> None:
        self._closed = True
        await self._result_queue.put(None)


class FakeTTS(TTSProvider):
    output_format = "pcm"
    sample_rate = 24000

    def __init__(self):
        self.calls: list[str] = []

    async def synthesize(self, text: str):
        self.calls.append(text)
        yield b"\x01\x02" * 64

    async def close(self) -> None:
        return None


class FakeLLM:
    def __init__(self, reply: str = "Done."):
        self.reply = reply
        self.calls: list[str] = []
        self.conversation_history: dict[str, list[dict]] = {}

    async def handle_command_stream(self, session_id: str, text: str, context=None):
        self.calls.append(text)
        self.conversation_history.setdefault(session_id, []).append(
            {"role": "assistant", "text": self.reply}
        )


async def _open(pipeline, stt, tts, llm, frames=None):
    return await pipeline.open_session(
        session_id="sess-vad",
        stt_provider=stt,
        tts_provider=tts,
        llm_handle=llm,
        send_frame=frames,
    )


@pytest.mark.asyncio
async def test_vad_speech_end_flushes_a_continuously_streaming_client():
    """The case that could not happen before.

    A client that never stops sending gives the packet-absence timer
    nothing to notice, so pre-VAD this session listened forever. Now
    the end of speech is read off the audio.
    """
    pipeline = ChainedVoicePipeline(silence_flush_seconds=30.0, vad_enabled=True)
    stt, tts, llm = BufferedSTT(), FakeTTS(), FakeLLM()
    session = await _open(pipeline, stt, tts, llm)
    session._endpointer = ScriptedEndpointer([
        [VadEvent.SPEECH_START],
        [],
        [VadEvent.SPEECH_END],
    ])

    for _ in range(3):
        await pipeline.handle_audio("sess-vad", SILENCE)
    # The turn runs off the ingest path, so give it the loop.
    for _ in range(20):
        await asyncio.sleep(0)
        if llm.calls:
            break

    assert llm.calls == ["turn on the lights"]
    assert session.last_endpoint_source == "vad"
    assert pipeline.endpoint_mode("sess-vad") == "vad"


@pytest.mark.asyncio
async def test_vad_does_not_flush_while_the_speaker_is_still_talking():
    pipeline = ChainedVoicePipeline(silence_flush_seconds=30.0, vad_enabled=True)
    stt, tts, llm = BufferedSTT(), FakeTTS(), FakeLLM()
    session = await _open(pipeline, stt, tts, llm)
    session._endpointer = ScriptedEndpointer([[VadEvent.SPEECH_START], [], []])

    for _ in range(3):
        await pipeline.handle_audio("sess-vad", SILENCE)
    await asyncio.sleep(0.05)

    assert llm.calls == []
    assert stt.flush_calls == 0


@pytest.mark.asyncio
async def test_silence_timer_still_backstops_a_client_that_stops_sending():
    """Regression pin.

    The first cut of the VAD work disarmed the silence timer whenever a
    VAD was loaded, on the theory that two endpointers would race. It
    is the wrong theory: the VAD only sees frames that arrive, so a
    client with an energy gate (every shipped one) or a stalled socket
    produced no end-of-speech event at all and the turn hung. The
    latency bench caught it as a turn that never completed.
    """
    pipeline = ChainedVoicePipeline(silence_flush_seconds=0.05, vad_enabled=True)
    stt, tts, llm = BufferedSTT(), FakeTTS(), FakeLLM()
    session = await _open(pipeline, stt, tts, llm)
    # A VAD that never reports anything: exactly the "client went quiet
    # before the VAD called it" case.
    session._endpointer = ScriptedEndpointer([])

    await pipeline.handle_audio("sess-vad", SILENCE)
    assert session._silence_task is not None, "timer must stay armed under VAD"
    await asyncio.sleep(0.25)

    assert llm.calls == ["turn on the lights"]
    assert session.last_endpoint_source == "silence_timer"


@pytest.mark.asyncio
async def test_no_vad_falls_back_to_the_timer_unchanged():
    pipeline = ChainedVoicePipeline(silence_flush_seconds=0.05, vad_enabled=False)
    stt, tts, llm = BufferedSTT(), FakeTTS(), FakeLLM()
    await _open(pipeline, stt, tts, llm)

    assert pipeline.endpoint_mode("sess-vad") == "timer"
    await pipeline.handle_audio("sess-vad", SILENCE)
    await asyncio.sleep(0.2)

    assert llm.calls == ["turn on the lights"]


@pytest.mark.asyncio
async def test_a_throwing_vad_degrades_to_the_timer_instead_of_killing_the_session():
    class Exploding:
        def feed(self, _pcm):
            raise RuntimeError("onnxruntime fell over")

    pipeline = ChainedVoicePipeline(silence_flush_seconds=0.05, vad_enabled=True)
    stt, tts, llm = BufferedSTT(), FakeTTS(), FakeLLM()
    session = await _open(pipeline, stt, tts, llm)
    session._endpointer = Exploding()

    await pipeline.handle_audio("sess-vad", SILENCE)
    assert session._endpointer is None, "a broken VAD must be dropped"
    await asyncio.sleep(0.2)
    assert llm.calls == ["turn on the lights"]


@pytest.mark.asyncio
async def test_speech_during_playback_cancels_the_turn():
    """Barge-in. The chained path had no cancel path at all."""
    frames: list[dict] = []

    async def capture(_sid, frame):
        frames.append(frame)

    slow_tts_started = asyncio.Event()

    class SlowTTS(FakeTTS):
        async def synthesize(self, text: str):
            self.calls.append(text)
            slow_tts_started.set()
            await asyncio.sleep(5)
            yield b"\x01\x02" * 64

    pipeline = ChainedVoicePipeline(silence_flush_seconds=30.0, vad_enabled=True)
    stt, tts, llm = BufferedSTT(), SlowTTS(), FakeLLM()
    session = await _open(pipeline, stt, tts, llm, frames=capture)
    session._endpointer = ScriptedEndpointer([[VadEvent.SPEECH_END]])

    await pipeline.handle_audio("sess-vad", SILENCE)
    await asyncio.wait_for(slow_tts_started.wait(), timeout=2.0)
    assert session.state == VoiceState.SPEAKING

    # The user starts talking over the reply.
    session._endpointer = ScriptedEndpointer([[VadEvent.SPEECH_START]])
    await pipeline.handle_audio("sess-vad", SILENCE)

    kinds = [f["type"] for f in frames]
    assert "voice_cancel" in kinds, kinds
    cancel = next(f for f in frames if f["type"] == "voice_cancel")
    assert cancel["payload"]["reason"] == "barge_in"
    assert cancel["payload"]["drop_pending_audio"] is True
    assert session.state == VoiceState.LISTENING


@pytest.mark.asyncio
async def test_cancel_is_safe_on_an_idle_or_unknown_session():
    pipeline = ChainedVoicePipeline(vad_enabled=False)
    stt, tts, llm = BufferedSTT(), FakeTTS(), FakeLLM()
    await _open(pipeline, stt, tts, llm)

    assert await pipeline.cancel("sess-vad") is False
    assert await pipeline.cancel("no-such-session") is False
    # Twice in a row must not raise either.
    assert await pipeline.cancel("sess-vad") is False


@pytest.mark.asyncio
async def test_barge_in_can_be_disabled():
    pipeline = ChainedVoicePipeline(vad_enabled=True, barge_in=False)
    stt, tts, llm = BufferedSTT(), FakeTTS(), FakeLLM()
    session = await _open(pipeline, stt, tts, llm)
    session.state = VoiceState.SPEAKING
    session._endpointer = ScriptedEndpointer([[VadEvent.SPEECH_START]])

    await pipeline.handle_audio("sess-vad", SILENCE)
    assert session.state == VoiceState.SPEAKING


# --- the surface api/server.py's voice_interrupt handler calls ----------


@pytest.mark.asyncio
async def test_router_cancel_chained_response_cuts_the_turn():
    """``api/server.py:2587`` cancels realtime and Gemini sessions only.

    This is the chained equivalent it needs to call. It is deliberately
    safe on an unknown session and on a router with no chained
    pipeline wired, so the server side stays a single unconditional
    line rather than a mode check.
    """
    from voice.router import VoiceRouter

    router = VoiceRouter()
    # No pipeline wired at all.
    assert await router.cancel_chained_response("nope") is False

    started = asyncio.Event()

    class SlowTTS(FakeTTS):
        async def synthesize(self, text: str):
            self.calls.append(text)
            started.set()
            await asyncio.sleep(5)
            yield b"\x00\x01"

    pipeline = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=30.0)
    router.set_chained_pipeline(pipeline)
    frames: list[dict] = []

    async def capture(_sid, frame):
        frames.append(frame)

    await pipeline.open_session(
        session_id="sess-vad",
        stt_provider=BufferedSTT(),
        tts_provider=SlowTTS(),
        llm_handle=FakeLLM(),
        send_frame=capture,
    )
    # An unknown session id must not raise.
    assert await router.cancel_chained_response("some-other-session") is False

    turn = asyncio.create_task(
        pipeline.handle_audio("sess-vad", SILENCE, is_final=True)
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)

    assert await router.cancel_chained_response("sess-vad") is True
    await asyncio.wait({turn})
    assert any(f["type"] == "voice_cancel" for f in frames)
