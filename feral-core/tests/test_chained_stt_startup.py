"""Recognition startup with inert transports, never credentials or real audio."""
import asyncio
from contextlib import suppress

import pytest

from voice.chained_pipeline import ChainedVoicePipeline
from voice.stt_providers import STTProvider
from voice.stt_providers.deepgram import DeepgramSTTProvider


class PreparedSTT(STTProvider):
    def __init__(self, *, fail=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.fail = fail

    async def prepare(self):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("synthetic recognition preparation failed")

    async def open_stream(self):
        await asyncio.Event().wait()
        yield None

    async def send_audio(self, audio_bytes):
        pass

    async def close(self):
        self.closed = True


class InertTTS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class InertSocket:
    def __init__(self):
        self.sent = []
        self.closed = asyncio.Event()

    async def send(self, value):
        self.sent.append(value)

    async def close(self):
        self.closed.set()

    async def __aiter__(self):
        await self.closed.wait()
        return
        yield


@pytest.mark.asyncio
async def test_pipeline_does_not_publish_before_recognition_prepare():
    pipeline = ChainedVoicePipeline(vad_enabled=False)
    stt, tts = PreparedSTT(), InertTTS()
    opening = asyncio.create_task(pipeline.open_session("owned", stt, tts, object()))
    try:
        await asyncio.wait_for(stt.entered.wait(), .5)
        assert pipeline.get_session("owned") is None and not opening.done()
        stt.release.set()
        session = await asyncio.wait_for(opening, .5)
        assert pipeline.get_session("owned") is session
    finally:
        opening.cancel()
        with suppress(asyncio.CancelledError):
            await opening
        await pipeline.close_session("owned")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["failure", "cancel", "timeout"])
async def test_unpublished_prepare_failure_closes_only_new_resources(monkeypatch, ending):
    import voice.chained_pipeline as module
    pipeline = ChainedVoicePipeline(vad_enabled=False)
    old_stt, old_tts = PreparedSTT(), InertTTS()
    old_stt.release.set()
    old = await pipeline.open_session("owned", old_stt, old_tts, object())
    stt, tts = PreparedSTT(fail=ending == "failure"), InertTTS()
    if ending == "timeout":
        monkeypatch.setattr(module, "STT_PREPARE_TIMEOUT_SECONDS", .02)
    opening = asyncio.create_task(pipeline.open_session("owned", stt, tts, object()))
    try:
        await asyncio.wait_for(stt.entered.wait(), .5)
        if ending == "failure":
            stt.release.set()
            with pytest.raises(RuntimeError, match="synthetic"):
                await opening
        elif ending == "cancel":
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
        else:
            with pytest.raises(TimeoutError):
                await opening
        assert pipeline.get_session("owned") is old
        assert stt.closed and tts.closed and not old_stt.closed and not old_tts.closed
    finally:
        opening.cancel()
        with suppress(asyncio.CancelledError, RuntimeError, TimeoutError):
            await opening
        await pipeline.close_session("owned")


@pytest.mark.asyncio
async def test_replacement_during_prepare_survives_old_open():
    pipeline = ChainedVoicePipeline(vad_enabled=False)
    stt, tts = PreparedSTT(), InertTTS()
    opening = asyncio.create_task(pipeline.open_session("owned", stt, tts, object()))
    try:
        await asyncio.wait_for(stt.entered.wait(), .5)
        new_stt, new_tts = PreparedSTT(), InertTTS()
        new_stt.release.set()
        replacement = await pipeline.open_session("owned", new_stt, new_tts, object())
        stt.release.set()
        with pytest.raises(RuntimeError, match="superseded"):
            await opening
        assert pipeline.get_session("owned") is replacement
        assert stt.closed and tts.closed and not new_stt.closed and not new_tts.closed
    finally:
        opening.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await opening
        await pipeline.close_session("owned")


@pytest.mark.asyncio
async def test_deepgram_first_audio_waits_for_one_connection(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    socket = InertSocket()
    calls = []

    async def connect(url, **kwargs):
        calls.append(url)
        entered.set()
        await release.wait()
        return socket

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    provider = DeepgramSTTProvider(api_key="synthetic-never-transmitted")
    sending = asyncio.create_task(provider.send_audio(b"\x00\x00"))
    try:
        await asyncio.wait_for(entered.wait(), .5)
        assert not sending.done() and not socket.sent
        release.set()
        await asyncio.wait_for(sending, .5)
        await provider.prepare()
        assert len(calls) == 1 and socket.sent == [b"\x00\x00"]
    finally:
        sending.cancel()
        with suppress(asyncio.CancelledError):
            await sending
        await provider.close()


@pytest.mark.asyncio
async def test_preparation_without_transport_refuses_audio(monkeypatch):
    provider = DeepgramSTTProvider(api_key="synthetic-never-transmitted")

    async def incomplete_prepare():
        return

    monkeypatch.setattr(provider, "prepare", incomplete_prepare)
    with pytest.raises(RuntimeError, match="transport is unavailable"):
        await provider.send_audio(b"\x00\x00")
    await provider.close()


@pytest.mark.asyncio
async def test_deepgram_late_connect_after_close_cannot_publish(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    socket = InertSocket()

    async def connect(url, **kwargs):
        entered.set()
        await release.wait()
        return socket

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    provider = DeepgramSTTProvider(api_key="synthetic-never-transmitted")
    preparing = asyncio.create_task(provider.prepare())
    try:
        await asyncio.wait_for(entered.wait(), .5)
        await provider.close()
        release.set()
        with pytest.raises(RuntimeError, match="closed"):
            await preparing
        assert provider._ws is None and socket.closed.is_set()
        with pytest.raises(RuntimeError, match="closed"):
            await provider.send_audio(b"\x00\x00")
    finally:
        preparing.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await preparing
        await provider.close()
