"""Registered v1 controls with real coordinator/pipeline, isolated SQLite only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from agents.runtime_context_checkpoint import RuntimeContextCoordinator
from gateway.protocol import GatewaySession
from memory.store import MemoryStore
from tests.test_client_voice_configuration import SyntheticOrchestrator, configured_router


class HeldCommand(SyntheticOrchestrator):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.conversation_history = {}

    async def handle_command_stream(self, session_id, text, context):
        try:
            async with self._context_checkpoints.command_scope(session_id):
                self.entered.set()
                await self.release.wait()
                self.conversation_history[session_id] = [{"role": "assistant", "content": "synthetic answer"}]
                return "synthetic answer"
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest_asyncio.fixture
async def voice_live(monkeypatch, tmp_path):
    owner = SimpleNamespace(send_json=AsyncMock())
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    command = HeldCommand()
    store = MemoryStore(db_path=str(tmp_path / "voice.db"))
    coordinator = RuntimeContextCoordinator(store, history=command.conversation_history,
        lock_for=command._get_session_lock, image_call_ids=lambda sid: frozenset(),
        clear_images=lambda sid: None, legacy_passthrough=True)
    command._context_checkpoints = coordinator
    state.memory = store
    state.orchestrator = router._orchestrator = command
    attachment = await coordinator.attach("owned")
    gateway = GatewaySession("owned", owner, state.gateway_registry)
    async def readiness():
        return await coordinator.readiness(attachment.token)
    gateway.metadata["runtime_context_readiness"] = readiness
    async def media_readiness():
        from api.runtime_context import established_legacy_media_readiness
        return await established_legacy_media_readiness(state, coordinator, attachment)
    gateway.metadata["runtime_context_voice_readiness"] = media_readiness
    identity = {"voice_attempt_version": 1, "voice_attempt_id": str(uuid4())}
    async def request(method, extra=None, pair=None):
        request_id = str(uuid4())
        await gateway.handle_message({"type": "req", "id": request_id, "method": method,
            "params": {**(identity if pair is None else pair), **(extra or {})}})
        return next(call.args[0] for call in reversed(owner.send_json.await_args_list)
                    if call.args[0].get("id") == request_id)
    await request("voice.config", {"mode": "chained", "provider": "configured"})
    selected = router._chained.get_session("owned")
    selected.stt_provider.send_audio = AsyncMock()
    selected.stt_provider.flush = AsyncMock()
    selected._pending_finals.append("synthetic task")
    async def empty_speech(text):
        if False:
            yield b""
    selected.tts_provider.synthesize = empty_speech
    # Endpointing runs the existing retained turn task after admission returns.
    router._chained._silence_flush_seconds = 0.01
    try:
        yield state, router, command, selected, request, identity
    finally:
        command.release.set()
        await router._chained.close_session("owned")
        await coordinator.detach(attachment.token)
        await store.aclose()


@pytest.mark.asyncio
async def test_final_audio_admission_releases_writer_lock(voice_live):
    state, router, command, selected, request, identity = voice_live
    reply = await asyncio.wait_for(request("voice.audio", {"data_b64": "AAAA", "is_final": True}), 0.2)
    assert reply["ok"] and reply["payload"]["received"]
    await asyncio.wait_for(command.entered.wait(), 0.2)
    assert not selected._turn_task.done()
    command.release.set()
    await asyncio.wait_for(asyncio.shield(selected._turn_task), 0.2)
    assert not command.cancelled.is_set()
    assert selected.state.value == "idle"


@pytest.mark.asyncio
async def test_speech_controls_remain_prompt_while_command_owns_writer(voice_live):
    state, router, command, selected, request, identity = voice_live
    await request("voice.audio", {"data_b64": "AAAA", "is_final": False})
    await asyncio.wait_for(command.entered.wait(), 0.2)
    mute = await asyncio.wait_for(request("voice.mute", {"muted": True}), 0.2)
    interrupted = await asyncio.wait_for(request("voice.interrupt", {"voice_request_id": str(uuid4())}), 0.2)
    assert mute["ok"] and interrupted["ok"] and interrupted["payload"]["cancel_requested"]
    assert selected._output_interrupted and not command.cancelled.is_set()
    unmuted = await asyncio.wait_for(request("voice.mute", {"muted": False}), 0.2)
    media = await asyncio.wait_for(request("voice.audio", {"data_b64": "AAAA", "is_final": False}), 0.2)
    assert unmuted["ok"] and media["ok"] and not command.cancelled.is_set()
    command.release.set()
    await asyncio.wait_for(asyncio.shield(selected._turn_task), 0.2)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "chained"])
async def test_lifecycle_cancels_only_owned_voice_turn_before_writer_lock(voice_live, mode):
    state, router, command, selected, request, identity = voice_live
    await request("voice.audio", {"data_b64": "AAAA", "is_final": False})
    await asyncio.wait_for(command.entered.wait(), 0.2)
    pair = identity if mode == "disabled" else {"voice_attempt_version": 1, "voice_attempt_id": str(uuid4())}
    reply = await asyncio.wait_for(request("voice.config", {"mode": mode, "provider": "configured"}, pair), 0.3)
    assert reply["ok"] and command.cancelled.is_set()
    assert router._chained.get_session("owned") is not selected
    assert (router._chained.get_session("owned") is None) is (mode == "disabled")


@pytest.mark.asyncio
async def test_late_audio_never_admits_replacement_turn(voice_live):
    state, router, command, selected, request, identity = voice_live
    from voice.chained_pipeline import ChainedSession
    foreign = ChainedSession("owned", SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock()), command)
    async def replaced(data):
        router._chained._sessions["owned"] = foreign
    selected.stt_provider.send_audio = AsyncMock(side_effect=replaced)
    reply = await request("voice.audio", {"data_b64": "AAAA", "is_final": True})
    assert not reply["ok"] and reply["error"]["code"] == "voice_producer_superseded"
    assert selected._turn_task is None and foreign._turn_task is None
    assert router._chained.get_session("owned") is foreign
    foreign.stt_provider.close.assert_not_awaited()
    await selected.stt_provider.close()
    selected._stt_task.cancel()
    await asyncio.gather(selected._stt_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "chained"])
async def test_lifecycle_cancellation_cannot_teardown_foreign_replacement(voice_live, mode):
    state, router, command, selected, request, identity = voice_live
    from voice.chained_pipeline import ChainedSession
    foreign = ChainedSession("owned", SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock()), command)
    original = command.handle_command_stream
    async def replaced(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except asyncio.CancelledError:
            router._chained._sessions["owned"] = foreign
            router._session_voice_mode["owned"] = "foreign-marker"
            raise
    command.handle_command_stream = replaced
    await request("voice.audio", {"data_b64": "AAAA", "is_final": False})
    await asyncio.wait_for(command.entered.wait(), 0.2)
    pair = identity if mode == "disabled" else {"voice_attempt_version": 1, "voice_attempt_id": str(uuid4())}
    reply = await request("voice.config", {"mode": mode, "provider": "configured"}, pair)
    assert not reply["ok"] and reply["error"]["code"] == "voice_producer_superseded"
    assert router._chained.get_session("owned") is foreign
    assert router._session_voice_mode["owned"] == "foreign-marker"
    foreign.stt_provider.close.assert_not_awaited()
    await selected.stt_provider.close()
    selected._stt_task.cancel()
    await asyncio.gather(selected._stt_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_lifecycle_pending_refuses_old_media_before_cancel_finishes(voice_live):
    state, router, command, selected, request, identity = voice_live
    original = command.handle_command_stream
    cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()
    async def delayed_cleanup(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except asyncio.CancelledError:
            cleaning.set()
            await finish_cleanup.wait()
            raise
    command.handle_command_stream = delayed_cleanup
    await request("voice.audio", {"data_b64": "AAAA", "is_final": False})
    await asyncio.wait_for(command.entered.wait(), 0.2)
    pair = {"voice_attempt_version": 1, "voice_attempt_id": str(uuid4())}
    lifecycle = asyncio.create_task(request("voice.config", {"mode": "chained", "provider": "configured"}, pair))
    try:
        await asyncio.wait_for(cleaning.wait(), 0.2)
        assert selected._voice_attempt.lifecycle_pending
        reply = await asyncio.wait_for(request("voice.audio", {"data_b64": "AAAA", "is_final": True}), 0.2)
        assert not reply["ok"] and reply["error"]["code"] == "voice_attempt_superseded"
        assert not lifecycle.done()
        finish_cleanup.set()
        assert (await asyncio.wait_for(lifecycle, 0.3))["ok"]
        assert router._chained.get_session("owned") is not selected
    finally:
        finish_cleanup.set()
        await asyncio.gather(lifecycle, return_exceptions=True)
