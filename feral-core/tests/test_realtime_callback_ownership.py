"""Provider callbacks belong to the instance and response that produced them.

All sockets and credentials are inert fixtures; no live audio or API calls.
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice.gemini_realtime import GeminiRealtimeProxy, GeminiRealtimeSession
from voice.realtime_proxy import RealtimeProxy, RealtimeSession


class Wire:
    def __init__(self):
        self.frames = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def inert_runtime(monkeypatch):
    monkeypatch.setattr("voice.realtime_proxy._resolve_openai_key", lambda: "fixture")
    monkeypatch.setitem(sys.modules, "api.state", SimpleNamespace(state=SimpleNamespace(orchestrator=None)))

    async def connect(session):
        session._connected = True

    monkeypatch.setattr(RealtimeSession, "connect", connect)
    monkeypatch.setattr(GeminiRealtimeSession, "connect", connect)


async def restarted(proxy_class):
    delivered = []

    async def send(node, frame):
        delivered.append((node, frame))

    proxy = proxy_class(send_to_node=send)
    proxy._build_system_prompt = AsyncMock(return_value="inert fixture")
    old = await proxy.start_session("same-sid", "old-node")
    await proxy.stop_session("same-sid")
    replacement = await proxy.start_session("same-sid", "new-node")
    return proxy, old, replacement, delivered


@pytest.mark.asyncio
async def test_openai_old_instance_cannot_forward_to_replacement():
    proxy, old, replacement, delivered = await restarted(RealtimeProxy)
    await old._handle_event({"type": "response.output_audio.delta", "response_id": "old-response", "delta": "AA=="})
    assert delivered == []
    assert proxy._sessions["same-sid"] is replacement
    await proxy.stop_session("same-sid")


@pytest.mark.asyncio
async def test_gemini_old_instance_cannot_forward_to_replacement():
    proxy, old, replacement, delivered = await restarted(GeminiRealtimeProxy)
    await old._handle_event({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm", "data": "AA=="}}]}}})
    assert delivered == []
    assert proxy._sessions["same-sid"] is replacement
    await proxy.stop_session("same-sid")


@pytest.mark.asyncio
async def test_openai_old_instance_cannot_cancel_replacement():
    proxy, old, replacement, delivered = await restarted(RealtimeProxy)
    replacement._ws = Wire()
    replacement._response_in_progress = True
    await old._handle_event({"type": "input_audio_buffer.speech_started"})
    assert replacement._ws.frames == []
    assert delivered == []
    await proxy.stop_session("same-sid")


async def responses():
    emitted = AsyncMock()
    session = RealtimeSession("response-sid", "fixture-node", api_key="fixture", on_audio_delta=emitted)
    session._connected = True
    session._ws = Wire()
    await session._handle_event({"type": "response.created", "response": {"id": "r1"}})
    await session._handle_event({"type": "response.created", "response": {"id": "r2"}})
    return session, emitted


@pytest.mark.asyncio
async def test_old_response_delta_cannot_leak_after_new_response():
    session, emitted = await responses()
    await session._handle_event({"type": "response.output_audio.delta", "response_id": "r1", "delta": "AA=="})
    emitted.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_response_done_cannot_clear_current_cancel_authority():
    session, _ = await responses()
    await session._handle_event({"type": "response.done", "response": {"id": "r1", "status": "cancelled"}})
    await session.cancel_response()
    assert session._response_in_progress is True
    assert session._ws.frames == [{"type": "response.cancel", "response_id": "r2"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "gemini"])
async def test_every_registered_callback_is_bound_to_the_constructed_instance(kind):
    proxy, old, replacement, delivered = await restarted(RealtimeProxy if kind == "openai" else GeminiRealtimeProxy)
    calls = {
        "_on_audio_delta": ("same-sid", "AA==", False),
        "_on_transcript": ("same-sid", "stale", True),
        "_on_tool_call": ("same-sid", "call", "fixture__read", "{}"),
        "_on_speech_started": ("same-sid",),
        "_on_error": ("same-sid", "fixture failure"),
    }
    if kind == "openai":
        calls.update({
            "_on_conversation_item": ("same-sid", {"action": "done", "item": {}}),
            "_on_notice": ("same-sid", "fixture", "notice"),
            "_on_response_created": ("same-sid",),
            "_on_response_done": ("same-sid", "completed", ""),
        })
    else:
        calls["_on_input_transcript"] = ("same-sid", "stale input")
    for name, args in calls.items():
        with pytest.raises(asyncio.CancelledError):
            await getattr(old, name)(*args)
    assert delivered == []
    assert proxy._sessions["same-sid"] is replacement
    assert proxy._bg_tasks == set()
    await proxy.stop_session("same-sid")


@pytest.mark.asyncio
async def test_supplied_gemini_callbacks_obey_instance_ownership():
    proxy = GeminiRealtimeProxy()
    proxy._build_system_prompt = AsyncMock(return_value="fixture")
    callbacks = {name: AsyncMock(return_value="{}") for name in (
        "on_audio_delta", "on_transcript", "on_tool_call", "on_speech_started", "on_error",
    )}
    old = await proxy.start_session("same-sid", "old", **callbacks)
    await old._handle_event({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm", "data": "AA=="}}]}}})
    callbacks["on_audio_delta"].assert_awaited_once()
    for callback in callbacks.values():
        callback.reset_mock()
    await proxy.stop_session("same-sid")
    replacement = await proxy.start_session("same-sid", "replacement", **callbacks)
    with pytest.raises(asyncio.CancelledError):
        await old._on_tool_call("same-sid", "call", "fixture__read", "{}")
    await old._handle_event({"serverContent": {"interrupted": True}})
    await old._handle_event({"error": {"message": "old"}})
    for callback in callbacks.values():
        callback.assert_not_awaited()
    assert proxy._sessions["same-sid"] is replacement
    await proxy.stop_session("same-sid")


@pytest.mark.asyncio
async def test_missing_response_ids_are_not_assigned_after_response_replacement():
    session, audio = await responses()
    transcript = AsyncMock()
    done = AsyncMock()
    session._on_transcript = transcript
    session._on_response_done = done
    await session._handle_event({"type": "response.output_audio.delta", "delta": "AA=="})
    await session._handle_event({"type": "response.output_text.done", "text": "unattributed"})
    await session._handle_event({"type": "response.done", "response": {"status": "completed"}})
    audio.assert_not_awaited()
    transcript.assert_not_awaited()
    done.assert_not_awaited()
    await session._handle_event({"type": "response.output_audio.delta", "response_id": "r2", "delta": "AA=="})
    audio.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_response_output_is_suppressed_but_its_done_is_delivered():
    session, audio = await responses()
    done = AsyncMock()
    session._on_response_done = done
    await session.cancel_response()
    await session._handle_event({"type": "response.output_audio.delta", "response_id": "r2", "delta": "AA=="})
    await session._handle_event({"type": "response.done", "response": {"id": "r2", "status": "cancelled"}})
    audio.assert_not_awaited()
    done.assert_awaited_once_with("response-sid", "cancelled", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "gemini"])
@pytest.mark.parametrize("restart", [False, True])
async def test_dispatched_tool_cannot_publish_after_owner_or_response_changed(kind, restart):
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # An external effect can already be running and need not roll back.
            await release.wait()
        return {"success": True, "data": {"ok": True}}

    endpoint = SimpleNamespace(id="read")
    registry = SimpleNamespace(skills={"fixture": SimpleNamespace(endpoints=[endpoint])}, get_all_tools=lambda: [])
    orchestrator = SimpleNamespace(_emit_tool_start=AsyncMock(), _emit_tool_result=AsyncMock())
    cls = RealtimeProxy if kind == "openai" else GeminiRealtimeProxy
    proxy = cls(skill_registry=registry, skill_executor=SimpleNamespace(execute=execute), orchestrator=orchestrator)
    proxy._build_system_prompt = AsyncMock(return_value="fixture")
    old = await proxy.start_session("same-sid", "old")
    old._ws = Wire()
    if kind == "openai":
        await old._handle_event({"type": "response.created", "response": {"id": "r1"}})
        event = {"type": "response.function_call_arguments.done", "response_id": "r1", "call_id": "c1", "name": "fixture__read", "arguments": "{}"}
    else:
        event = {"toolCall": {"functionCalls": [{"id": "c1", "name": "fixture__read", "args": {}}]}}
    task = asyncio.create_task(old._handle_event(event))
    await asyncio.wait_for(entered.wait(), 2)
    if restart:
        await proxy.stop_session("same-sid")
        current = await proxy.start_session("same-sid", "replacement")
    else:
        current = old
        if kind == "openai":
            await old._handle_event({"type": "response.created", "response": {"id": "r2"}})
        else:
            await old._handle_event({"serverContent": {"interrupted": True}})
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    orchestrator._emit_tool_start.assert_awaited_once()
    orchestrator._emit_tool_result.assert_not_awaited()
    assert old._ws.frames == []
    assert proxy._sessions["same-sid"] is current
    await proxy.stop_session("same-sid")


@pytest.mark.asyncio
async def test_gemini_interruption_invalidates_suspended_content_callbacks():
    entered, release = asyncio.Event(), asyncio.Event()
    audio = AsyncMock()

    async def transcript(*args):
        entered.set()
        await release.wait()

    session = GeminiRealtimeSession("fixture-sid", "fixture-node", on_transcript=transcript, on_audio_delta=audio)
    task = asyncio.create_task(session._handle_event({"serverContent": {"outputTranscription": {"text": "old"}, "turnComplete": True}}))
    await asyncio.wait_for(entered.wait(), 2)
    await session._handle_event({"serverContent": {"interrupted": True}})
    release.set()
    await asyncio.wait_for(task, 2)
    audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_gemini_interrupted_envelope_preserves_independent_user_transcript():
    audio, transcript, input_transcript, speech = (AsyncMock() for _ in range(4))
    session = GeminiRealtimeSession("fixture-sid", "fixture-node", on_audio_delta=audio, on_transcript=transcript, on_input_transcript=input_transcript, on_speech_started=speech)
    await session._handle_event({"serverContent": {
        "interrupted": True, "turnComplete": True,
        "inputTranscription": {"text": "stop please"},
        "outputTranscription": {"text": "discard"},
        "modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm", "data": "AA=="}}]},
    }})
    speech.assert_awaited_once_with("fixture-sid")
    input_transcript.assert_awaited_once_with("fixture-sid", "stop please")
    audio.assert_not_awaited()
    transcript.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_failure_can_stop_its_session_without_self_await_or_lost_cleanup():
    proxy = RealtimeProxy()
    proxy._build_system_prompt = AsyncMock(return_value="fixture")
    session = await proxy.start_session("same-sid", "node")

    async def fallback(**kwargs):
        await proxy.stop_session(kwargs["session_id"])

    proxy.attach_fallback_router(SimpleNamespace(handle_realtime_failure=fallback))
    session._recv_task = asyncio.current_task()
    with pytest.raises(asyncio.CancelledError):
        await session._handle_event({"type": "error", "error": {"code": "insufficient_quota", "message": "fixture"}})
    assert proxy._sessions == {}
    assert proxy._node_to_session == {}
    assert session._retired
    assert session._callback_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "gemini"])
async def test_replaced_connect_failure_does_not_degrade_current_session(kind, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    session_class = RealtimeSession if kind == "openai" else GeminiRealtimeSession
    cls = RealtimeProxy if kind == "openai" else GeminiRealtimeProxy

    async def connect(session):
        if session.node_id == "old":
            entered.set()
            await release.wait()
        else:
            session._connected = True

    monkeypatch.setattr(session_class, "connect", connect)
    proxy = cls()
    proxy._build_system_prompt = AsyncMock(return_value="fixture")
    fallback = AsyncMock()
    proxy.attach_fallback_router(SimpleNamespace(handle_realtime_failure=fallback))
    task = asyncio.create_task(proxy.start_session("same-sid", "old"))
    await asyncio.wait_for(entered.wait(), 2)
    replacement = await proxy.start_session("same-sid", "replacement")
    release.set()
    assert await asyncio.wait_for(task, 2) is None
    fallback.assert_not_awaited()
    assert proxy._sessions["same-sid"] is replacement
    await proxy.stop_session("same-sid")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "gemini"])
@pytest.mark.parametrize("replacement", [False, True])
async def test_connect_exception_cleans_only_its_captured_partial_session(kind, replacement, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    partial = []
    session_class = RealtimeSession if kind == "openai" else GeminiRealtimeSession
    cls = RealtimeProxy if kind == "openai" else GeminiRealtimeProxy

    async def connect(session):
        session._ws = Wire()
        if session.node_id == "old":
            partial.append(session)
            entered.set()
            await release.wait()
            raise RuntimeError("inert callback/connect failure")
        session._connected = True

    monkeypatch.setattr(session_class, "connect", connect)
    proxy = cls()
    proxy._build_system_prompt = AsyncMock(return_value="fixture")
    fallback = AsyncMock()
    proxy.attach_fallback_router(SimpleNamespace(handle_realtime_failure=fallback))
    task = asyncio.create_task(proxy.start_session("same-sid", "old"))
    await asyncio.wait_for(entered.wait(), 2)
    current = await proxy.start_session("same-sid", "replacement") if replacement else None
    release.set()
    with pytest.raises(RuntimeError, match="inert callback/connect failure"):
        await asyncio.wait_for(task, 2)
    assert partial[0]._retired
    assert proxy._sessions.get("same-sid") is current
    assert "old" not in proxy._node_to_session
    fallback.assert_not_awaited()
    if replacement:
        await proxy.stop_session("same-sid")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "gemini"])
async def test_receive_teardown_is_bounded_when_dispatched_work_resists_cancel(kind):
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    session_class = RealtimeSession if kind == "openai" else GeminiRealtimeSession
    session = session_class("fixture-sid", "fixture-node")
    task = asyncio.create_task(work())
    session._recv_task = task
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await asyncio.wait_for(session.disconnect(), 2)
        assert not task.done()
        assert session._retired
    finally:
        release.set()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_response_replacement_during_tool_output_send_prevents_old_response_create():
    entered, release = asyncio.Event(), asyncio.Event()
    session, _ = await responses()

    class SlowWire(Wire):
        async def send(self, raw):
            self.frames.append(json.loads(raw))
            entered.set()
            await release.wait()

    session._ws = SlowWire()
    task = asyncio.create_task(session.send_tool_result("call", "{}"))
    await asyncio.wait_for(entered.wait(), 2)
    await session._handle_event({"type": "response.created", "response": {"id": "r3"}})
    release.set()
    await asyncio.wait_for(task, 2)
    assert [frame["type"] for frame in session._ws.frames] == ["conversation.item.create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "gemini"])
async def test_receive_loop_survives_response_epoch_cancellation(kind):
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def execute(*args):
        entered.set()
        await release.wait()
        return {"success": True, "data": {"ok": True}}

    async def send(node, frame):
        delivered.append(frame)

    registry = SimpleNamespace(skills={"fixture": SimpleNamespace(endpoints=[SimpleNamespace(id="read")])}, get_all_tools=lambda: [])
    cls = RealtimeProxy if kind == "openai" else GeminiRealtimeProxy
    proxy = cls(skill_registry=registry, skill_executor=SimpleNamespace(execute=execute), send_to_node=send)
    proxy._build_system_prompt = AsyncMock(return_value="fixture")
    session = await proxy.start_session("same-sid", "node")
    if kind == "openai":
        events = [
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.function_call_arguments.done", "response_id": "r1", "call_id": "c1", "name": "fixture__read", "arguments": "{}"},
            {"type": "response.created", "response": {"id": "r2"}},
            {"type": "response.output_audio.delta", "response_id": "r2", "delta": "NEW=="},
        ]
    else:
        events = [
            {"toolCall": {"functionCalls": [{"id": "c1", "name": "fixture__read", "args": {}}]}},
            {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm", "data": "NEW=="}}]}}},
        ]

    class ScriptedWire(Wire):
        async def __aiter__(self):
            for event in events:
                yield json.dumps(event)

    session._ws = ScriptedWire()
    task = asyncio.create_task(session._receive_loop())
    session._recv_task = task
    await asyncio.wait_for(entered.wait(), 2)
    if kind == "openai":
        await session.cancel_response()
    else:
        await session._handle_event({"serverContent": {"interrupted": True}})
    release.set()
    await asyncio.wait_for(task, 2)
    audio = [frame["payload"]["data_b64"] for frame in delivered if frame["type"] == "audio_response"]
    assert audio == ["NEW=="]
    await proxy.stop_session("same-sid")
