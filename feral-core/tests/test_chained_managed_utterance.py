"""Explicit managed utterance admission and committed-only speech."""
import asyncio
import base64
import uuid
import pytest
from bridges.client_voice_attempt import VoiceAttemptError
from voice.chained_pipeline import ChainedVoicePipeline, MANAGED_MAX_AUDIO_BYTES
from voice.stt_providers import STTProvider, TranscriptFragment
from voice.tts_providers import TTSProvider

class BufferedSTT(STTProvider):
    def __init__(self, text="do the task"):
        self.text, self.audio = text, bytearray()
        self._result_queue = asyncio.Queue()
        self.flushes, self.closed = 0, False
    async def open_stream(self):
        raise AssertionError("managed must not start background recognition")
        yield
    async def send_audio(self, data):
        self.audio.extend(data)
    async def flush(self):
        self.flushes += 1
        if self.audio:
            self._result_queue.put_nowait(TranscriptFragment(self.text, False, True, speech_final=True))
            self.audio.clear()
    async def close(self):
        self.closed = True

class Speech(TTSProvider):
    output_format, sample_rate = "pcm16", 24000
    def __init__(self):
        self.texts, self.closed = [], False
    async def synthesize(self, text):
        self.texts.append(text)
        yield b"\0\0" * 2400
    async def close(self):
        self.closed = True

def terminal(request, **changes):
    value = dict(contract_version=1, request_id=request, turn_id=str(uuid.uuid4()), session_id="managed",
        processing_outcome="completed", final_text="Exact committed answer", durable=True, replayed=False,
        context_checkpoint=dict(contract_version=1, session_id="managed", generation=str(uuid.uuid4()),
            revision=1, attempt_id=str(uuid.uuid4()), durable=True))
    value.update(changes)
    return value

async def opened(submit=None, guard=None, speaker=None, provider="openai_whisper"):
    p = ChainedVoicePipeline(vad_enabled=False, silence_flush_seconds=0.01)
    stt, tts, frames, calls, aborts = BufferedSTT(), speaker or Speech(), [], [], []
    async def default_submit(request, transcript):
        calls.append((request, transcript))
        return terminal(request)
    async def abort(request, turn):
        aborts.append((request, turn))
        return {"processing_outcome": "outcome_unknown"}
    async def send(sid, frame):
        frames.append(frame)
    s = await p.open_session("managed", stt, tts, None, send, submit_tracked_utterance=submit or default_submit,
        abort_tracked_utterance=abort, assert_admission_current=guard or (lambda: None), managed_stt_provider=provider)
    return p, s, stt, tts, frames, calls, aborts

async def capture(p, s):
    request = str(uuid.uuid4())
    await p.begin_utterance(s, request)
    await p.handle_audio_for_session(s, base64.b64encode(b"\0\0" * 100).decode(), request_id=request)
    return request

@pytest.mark.asyncio
async def test_explicit_finish_only_and_exact_committed_speech():
    p, s, stt, tts, frames, calls, _ = await opened()
    try:
        request = await capture(p, s)
        await asyncio.sleep(0.04)
        assert not calls and stt.flushes == 0 and s._silence_task is None and s._stt_task is None
        await (await p.finish_utterance(s, request))
        assert calls == [(request, "do the task")] and tts.texts == ["Exact committed answer"]
        audio = [f["payload"] for f in frames if f["type"] == "audio_chunk"]
        assert audio and audio[-1]["is_final"]
        assert all(f["request_id"] == request and f["context_checkpoint"]["durable"] is True for f in audio)
        assert all(f["turn_id"] == audio[0]["turn_id"] for f in audio)
        with pytest.raises(VoiceAttemptError):
            await p.finish_utterance(s, request)
        with pytest.raises(VoiceAttemptError):
            await p.begin_utterance(s, request)
    finally:
        await p.shutdown()

@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"replayed": True}, {"request_id": str(uuid.uuid4())}, {"session_id": "foreign"}, {"context_checkpoint": None},
    {"durable": False}, {"processing_outcome": "cancelled", "context_checkpoint": None},
    {"processing_outcome": "failed", "context_checkpoint": None},
    {"processing_outcome": "outcome_unknown", "context_checkpoint": None},
    {"context_checkpoint": {"contract_version": 1, "session_id": "managed", "durable": True}},
])
async def test_uncertified_or_historical_terminal_never_speaks(changes):
    async def submit(request, text):
        return terminal(request, **changes)
    p, s, _, tts, frames, _, _ = await opened(submit=submit)
    try:
        await (await p.finish_utterance(s, await capture(p, s)))
        assert not tts.texts and not any(f["type"] == "audio_chunk" for f in frames)
    finally:
        await p.shutdown()

@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["awaiting_approval", "refused"])
async def test_committed_noncompleted_terminal_can_speak(outcome):
    async def submit(request, text):
        return terminal(request, processing_outcome=outcome)
    p, s, _, tts, _, _, _ = await opened(submit=submit)
    try:
        await (await p.finish_utterance(s, await capture(p, s)))
        assert tts.texts == ["Exact committed answer"]
    finally:
        await p.shutdown()

@pytest.mark.asyncio
async def test_invalid_identity_busy_audio_and_automatic_flush_refused():
    p, s, stt, _, _, _, _ = await opened()
    try:
        for value in ["", "not-a-uuid", str(uuid.uuid4()).upper(), 1]:
            with pytest.raises(VoiceAttemptError):
                await p.begin_utterance(s, value)
        request = await capture(p, s)
        with pytest.raises(VoiceAttemptError):
            await p.begin_utterance(s, str(uuid.uuid4()))
        for params in [{}, {"request_id": str(uuid.uuid4())}, {"request_id": request, "is_final": True}]:
            with pytest.raises(VoiceAttemptError):
                await p.handle_audio_for_session(s, "AAA=", **params)
        with pytest.raises(VoiceAttemptError):
            await p._drive_turn(s, wait=False, source="timer")
        with pytest.raises(VoiceAttemptError):
            await p._on_stt_fragment(s, TranscriptFragment("unexpected", False, True))
        s._managed_audio_bytes = MANAGED_MAX_AUDIO_BYTES
        with pytest.raises(VoiceAttemptError):
            await p.handle_audio_for_session(s, "AAA=", request_id=request)
        assert stt.flushes == 0
    finally:
        await p.shutdown()

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["deepgram", "unknown", None])
async def test_streaming_or_undeclared_managed_provider_refused(provider):
    with pytest.raises(VoiceAttemptError):
        await opened(provider=provider)

@pytest.mark.asyncio
async def test_callback_trio_required():
    stt, tts = BufferedSTT(), Speech()
    with pytest.raises(VoiceAttemptError):
        await ChainedVoicePipeline().open_session("managed", stt, tts, None,
            assert_admission_current=lambda: None, managed_stt_provider="openai_whisper")
    assert stt.closed and tts.closed

@pytest.mark.asyncio
async def test_speech_interrupt_keeps_tracked_task_and_no_second_admission():
    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def submit(request, text):
        started.set()
        await release.wait()
        completed.set()
        return terminal(request)
    p, s, _, tts, _, _, aborts = await opened(submit=submit)
    try:
        task = await p.finish_utterance(s, await capture(p, s))
        await started.wait()
        assert await p.interrupt_output("managed", expected_session=s)
        assert not task.done() and aborts == []
        with pytest.raises(VoiceAttemptError):
            await p.begin_utterance(s, str(uuid.uuid4()))
        release.set()
        await task
        assert completed.is_set() and not tts.texts
    finally:
        release.set()
        await p.shutdown()

@pytest.mark.asyncio
async def test_disconnect_cancels_waiter_not_independent_manager_child():
    started, release = asyncio.Event(), asyncio.Event()
    async def manager_child():
        started.set()
        await release.wait()
        return "done"
    child = asyncio.create_task(manager_child())
    async def submit(request, text):
        await asyncio.shield(child)
        return terminal(request)
    p, s, _, tts, _, _, aborts = await opened(submit=submit)
    task = await p.finish_utterance(s, await capture(p, s))
    await started.wait()
    await asyncio.sleep(0)
    await p.close_session("managed")
    assert task.cancelled() and not child.done() and not aborts and not tts.texts
    release.set()
    assert await child == "done"

@pytest.mark.asyncio
async def test_stop_uses_exact_request_unknown_turn_receipt():
    started = asyncio.Event()
    async def submit(request, text):
        started.set()
        await asyncio.Event().wait()
    p, s, _, _, frames, _, aborts = await opened(submit=submit)
    try:
        request = await capture(p, s)
        task = await p.finish_utterance(s, request)
        await started.wait()
        assert await p.cancel("managed")
        assert aborts == [(request, None)] and task.cancelled()
        stop = next(f["payload"] for f in frames if f["type"] == "voice_cancel")
        assert stop["abort_receipt"]["processing_outcome"] == "outcome_unknown"
        assert stop["agent_task_cancel_requested"] is False
    finally:
        await p.shutdown()

@pytest.mark.asyncio
async def test_attachment_loss_after_terminal_wait_suppresses_speech():
    current = True
    def guard():
        if not current:
            raise VoiceAttemptError("detached")
    async def submit(request, text):
        nonlocal current
        current = False
        return terminal(request)
    p, s, _, tts, frames, _, _ = await opened(submit=submit, guard=guard)
    try:
        await (await p.finish_utterance(s, await capture(p, s)))
        assert not tts.texts and not any(f["type"] == "audio_chunk" for f in frames)
    finally:
        await p.shutdown()

@pytest.mark.asyncio
async def test_late_tts_after_interrupt_has_no_audio_or_final_sentinel():
    speaking, release = asyncio.Event(), asyncio.Event()
    class LateSpeech(Speech):
        async def synthesize(self, text):
            self.texts.append(text)
            speaking.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
            yield b"\0\0" * 2400
    p, s, _, tts, frames, _, aborts = await opened(speaker=LateSpeech())
    try:
        task = await p.finish_utterance(s, await capture(p, s))
        await speaking.wait()
        assert await p.interrupt_output("managed", expected_session=s)
        release.set()
        await task
        assert tts.texts == ["Exact committed answer"] and not aborts
        assert not any(f["type"] == "audio_chunk" for f in frames)
    finally:
        release.set()
        await p.shutdown()

@pytest.mark.asyncio
async def test_detach_during_tts_blocks_late_audio():
    current, speaking, release = True, asyncio.Event(), asyncio.Event()
    def guard():
        if not current:
            raise VoiceAttemptError("detached")
    class HeldSpeech(Speech):
        async def synthesize(self, text):
            speaking.set()
            await release.wait()
            yield b"\0\0" * 2400
    p, s, _, _, frames, _, _ = await opened(guard=guard, speaker=HeldSpeech())
    try:
        task = await p.finish_utterance(s, await capture(p, s))
        await speaking.wait()
        current = False
        release.set()
        await task
        assert not any(f["type"] == "audio_chunk" for f in frames)
    finally:
        release.set()
        await p.shutdown()

@pytest.mark.asyncio
async def test_failed_stt_and_partial_only_result_never_submit():
    for failed in (False, True):
        p, s, stt, tts, _, calls, _ = await opened()
        async def flush():
            if failed:
                raise RuntimeError("synthetic recognition failure")
            stt._result_queue.put_nowait(TranscriptFragment("unfinalized", True, False))
        stt.flush = flush
        try:
            await (await p.finish_utterance(s, await capture(p, s)))
            assert not calls and not tts.texts
        finally:
            await p.shutdown()

@pytest.mark.asyncio
async def test_stale_exact_producer_cannot_capture_finish_or_stop_replacement():
    p, old, _, _, _, _, _ = await opened()
    request = await capture(p, old)
    replacement = object()
    p._sessions["managed"] = replacement
    try:
        with pytest.raises(VoiceAttemptError):
            await p.handle_audio_for_session(old, "AAA=", request_id=request)
        with pytest.raises(VoiceAttemptError):
            await p.finish_utterance(old, request)
        with pytest.raises(VoiceAttemptError):
            await p.interrupt_output("managed", expected_session=old)
        assert p.get_session("managed") is replacement
    finally:
        p._sessions["managed"] = old
        await p.shutdown()

@pytest.mark.asyncio
async def test_startup_guard_loss_cleans_exact_unpublished_providers():
    p, stt, tts = ChainedVoicePipeline(vad_enabled=False), BufferedSTT(), Speech()
    current = True
    def guard():
        if not current:
            raise VoiceAttemptError("detached")
    async def send(sid, frame):
        nonlocal current
        current = False
    async def submit(request, text):
        return terminal(request)
    async def abort(request, turn):
        return {}
    with pytest.raises(VoiceAttemptError):
        await p.open_session("managed", stt, tts, None, send,
            submit_tracked_utterance=submit, abort_tracked_utterance=abort,
            assert_admission_current=guard, managed_stt_provider="openai_whisper")
    assert p.get_session("managed") is None and stt.closed and tts.closed

@pytest.mark.asyncio
async def test_router_uses_trusted_captured_sender_and_pure_buffered_capability(monkeypatch):
    from voice.router import VoiceRouter
    p, frames = ChainedVoicePipeline(vad_enabled=False), []
    router = VoiceRouter(audio_pipeline=None, orchestrator=None)
    router.set_chained_pipeline(p)
    cfg = dict(stt_provider="openai_whisper", tts_provider="openai", stt_model="", tts_model="", tts_voice="", tts_voice_id="")
    monkeypatch.setattr(router, "_resolve_chained_config", lambda: cfg)
    constructions = []
    def stt_factory(name, **kwargs):
        constructions.append(name)
        return BufferedSTT()
    monkeypatch.setattr("voice.stt_providers.get_stt_provider", stt_factory)
    monkeypatch.setattr("voice.tts_providers.get_tts_provider", lambda name, **kwargs: Speech())
    async def latest_sender(*args):
        raise AssertionError("managed events cannot use latest SID sender")
    router._send_to_session = latest_sender
    async def captured_sender(sid, frame):
        frames.append(frame)
    async def submit(request, text):
        return terminal(request)
    async def abort(request, turn):
        return {}
    assert router.supports_managed_chained_voice() and not constructions
    assert not router.supports_managed_chained_voice({"stt_provider": "deepgram"})
    with pytest.raises(VoiceAttemptError):
        await router.open_chained_session("managed", submit_tracked_utterance=submit)
    s = await router.open_chained_session("managed", submit_tracked_utterance=submit,
        abort_tracked_utterance=abort, assert_admission_current=lambda: None, managed_send_frame=captured_sender)
    try:
        await (await p.finish_utterance(s, await capture(p, s)))
        assert constructions == ["openai_whisper"] and any(f["type"] == "audio_chunk" for f in frames)
    finally:
        await p.shutdown()

@pytest.mark.asyncio
async def test_stop_during_capture_seals_audio_and_finish_without_fake_cancel():
    p, s, _, tts, frames, calls, aborts = await opened()
    try:
        request = await capture(p, s)
        assert await p.cancel("managed")
        assert aborts == [(request, None)] and not calls and not tts.texts
        with pytest.raises(VoiceAttemptError):
            await p.handle_audio_for_session(s, "AAA=", request_id=request)
        with pytest.raises(VoiceAttemptError):
            await p.finish_utterance(s, request)
        with pytest.raises(VoiceAttemptError):
            await p.begin_utterance(s, str(uuid.uuid4()))
        assert next(f["payload"] for f in frames if f["type"] == "voice_cancel")["agent_task_cancel_requested"] is False
    finally:
        await p.shutdown()

@pytest.mark.asyncio
async def test_managed_private_legacy_fallbacks_are_refused():
    p, s, _, _, _, calls, _ = await opened()
    try:
        with pytest.raises(VoiceAttemptError):
            await p._flush_pipeline(s)
        with pytest.raises(VoiceAttemptError):
            await p._run_turn(s, "never execute")
        assert not calls and p._taps == {}
    finally:
        await p.shutdown()
