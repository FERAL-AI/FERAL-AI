"""Actual realtime receive loops stay responsive while inert tools are delayed."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice.gemini_realtime import GeminiRealtimeProxy, GeminiRealtimeSession
from voice.realtime_proxy import RealtimeProxy, RealtimeSession
from voice.realtime_tool_dispatch import MAX_PENDING_TOOLS, RealtimeToolLane


class Wire:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.frames = []
        self.sent = asyncio.Event()
        self.block_send = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return json.dumps(frame)

    async def send(self, raw):
        if self.block_send is not None:
            await self.block_send.wait()
        self.frames.append(json.loads(raw))
        self.sent.set()

    async def close(self):
        self.incoming.put_nowait(None)


def tool_event(provider, call_id="c1", response_id="r1"):
    if provider == "openai":
        return {"type": "response.function_call_arguments.done", "response_id": response_id,
                "call_id": call_id, "name": "fixture__read", "arguments": "{}"}
    return {"toolCall": {"functionCalls": [{"id": call_id, "name": "fixture__read", "args": {}}]}}


def interrupt_event(provider):
    return ({"type": "input_audio_buffer.speech_started"} if provider == "openai"
            else {"serverContent": {"interrupted": True}})


def outputs(provider, frames):
    if provider == "openai":
        return [(frame["item"]["call_id"], json.loads(frame["item"]["output"]))
                for frame in frames if frame.get("item", {}).get("type") == "function_call_output"]
    return [(result["id"], result["response"]) for frame in frames
            for result in frame.get("toolResponse", {}).get("functionResponses", [])]


async def harness(provider, execute):
    session_cls, proxy_cls = ((RealtimeSession, RealtimeProxy) if provider == "openai"
                              else (GeminiRealtimeSession, GeminiRealtimeProxy))
    owner = session_cls("responsive-sid", "fixture-node", api_key="fixture")
    owner._connected = True
    owner._ws = Wire()
    proxy = proxy_cls.__new__(proxy_cls)
    proxy._memory = proxy._orchestrator = None
    proxy._sessions = {owner.session_id: owner}
    proxy._skill_registry = SimpleNamespace(skills={"fixture": SimpleNamespace(
        endpoints=[SimpleNamespace(id="read")],
    )})
    proxy._skill_executor = SimpleNamespace(execute=execute)
    proxy._send_tool_feedback = AsyncMock()
    proxy._record_voice_tool_episode = AsyncMock()
    owner._callback_guard = lambda: proxy._sessions.get(owner.session_id) is owner
    owner._on_tool_call = proxy._bind_callback(owner, proxy._handle_tool_call, response_scoped=True)
    interruption = asyncio.Event()

    async def speech_started(sid):
        interruption.set()
        if provider == "openai":
            await owner.cancel_response()

    owner._on_speech_started = speech_started
    owner._recv_task = asyncio.create_task(owner._receive_loop())
    if provider == "openai":
        owner._ws.incoming.put_nowait({"type": "response.created", "response": {"id": "r1"}})
    return owner, proxy, interruption


async def settle(owner, release=None):
    if release is not None:
        release.set()
    await owner.disconnect()
    if owner._recv_task is not None:
        await asyncio.wait_for(owner._recv_task, 1)
    for _ in range(10):
        if not owner._callback_tasks:
            break
        await asyncio.sleep(0)
    assert not owner._callback_tasks
    assert owner._tool_lane._task is None
    assert owner._tool_failure_lane._task is None


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("delay", [1, 3, 5])
async def test_same_wire_interruption_precedes_delayed_tool_release(provider, delay):
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def execute(*args, **kwargs):
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), delay)
        except asyncio.CancelledError:
            # A dispatched boundary can resist cancellation; keep its lane.
            await release.wait()
        completed.append(True)
        return {"success": True, "data": {"nonce": "old"}}

    owner, _, interruption = await harness(provider, execute)
    try:
        owner._ws.incoming.put_nowait(tool_event(provider))
        await asyncio.wait_for(entered.wait(), 1)
        start = asyncio.get_running_loop().time()
        owner._ws.incoming.put_nowait(interrupt_event(provider))
        await asyncio.wait_for(interruption.wait(), 0.1)
        assert asyncio.get_running_loop().time() - start < 0.1
        assert not completed and not release.is_set()
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert outputs(provider, owner._ws.frames) == []
    finally:
        await settle(owner, release)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_receive_lane_serializes_effects_and_preserves_falsy_results(provider):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        else:
            finished.set()
        return {"success": True, "data": False, "error": None}

    owner, _, _ = await harness(provider, execute)
    try:
        owner._ws.incoming.put_nowait(tool_event(provider, "c1"))
        await asyncio.wait_for(entered.wait(), 1)
        owner._ws.incoming.put_nowait(tool_event(provider, "c2"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        for _ in range(10):
            if len(outputs(provider, owner._ws.frames)) == 2:
                break
            await asyncio.sleep(0)
        assert [identifier for identifier, _ in outputs(provider, owner._ws.frames)] == ["c1", "c2"]
        assert all(result["data"] is False for _, result in outputs(provider, owner._ws.frames))
    finally:
        await settle(owner, release)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("invalidate", ["replacement", "disconnect", "receive_eof"])
async def test_stale_owner_or_disconnected_wire_cannot_publish_or_dispatch_pending(provider, invalidate):
    entered, release = asyncio.Event(), asyncio.Event()
    execute = AsyncMock()

    async def delayed(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"success": True, "data": "old"}

    execute.side_effect = delayed
    owner, proxy, _ = await harness(provider, execute)
    try:
        owner._ws.incoming.put_nowait(tool_event(provider))
        await asyncio.wait_for(entered.wait(), 1)
        owner._ws.incoming.put_nowait(tool_event(provider, "pending"))
        await asyncio.sleep(0)
        if invalidate == "replacement":
            proxy._sessions[owner.session_id] = object()
            owner._tool_lane.discard_obsolete()
        elif invalidate == "disconnect":
            await owner.disconnect()
        else:
            owner._ws.incoming.put_nowait(None)
            await asyncio.wait_for(owner._recv_task, 1)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        execute.assert_awaited_once()
        assert outputs(provider, owner._ws.frames) == []
    finally:
        await settle(owner, release)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_duplicate_tool_id_does_not_repeat_effect(provider):
    entered, release = asyncio.Event(), asyncio.Event()
    execute = AsyncMock()

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"success": True, "data": 0}

    execute.side_effect = delayed
    owner, _, _ = await harness(provider, execute)
    try:
        owner._ws.incoming.put_nowait(tool_event(provider))
        await asyncio.wait_for(entered.wait(), 1)
        owner._ws.incoming.put_nowait(tool_event(provider))
        await asyncio.sleep(0)
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        execute.assert_awaited_once()
        assert len(outputs(provider, owner._ws.frames)) == 1
    finally:
        await settle(owner, release)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_queue_full_refuses_without_dispatch_and_intake_still_interrupts(provider):
    entered, release = asyncio.Event(), asyncio.Event()
    execute = AsyncMock()

    async def delayed(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"success": True, "data": "old"}

    execute.side_effect = delayed
    owner, _, interruption = await harness(provider, execute)
    try:
        owner._ws.incoming.put_nowait(tool_event(provider))
        await asyncio.wait_for(entered.wait(), 1)
        for index in range(1, MAX_PENDING_TOOLS + 1):
            owner._ws.incoming.put_nowait(tool_event(provider, f"c{index + 1}"))
        await asyncio.wait_for(owner._ws.sent.wait(), 0.1)
        owner._ws.incoming.put_nowait(interrupt_event(provider))
        await asyncio.wait_for(interruption.wait(), 0.2)
        execute.assert_awaited_once()
        refused = outputs(provider, owner._ws.frames)
        assert len(refused) == 1
        assert refused[0][1]["success"] is False
        assert refused[0][1]["error_code"] == "REALTIME_TOOL_QUEUE_FULL"
        assert refused[0][1]["outcome_unknown"] is False
        assert not owner._tool_lane._pending
    finally:
        await settle(owner, release)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_exception_truthfully_reports_unknown_without_replaying_and_next_tool_runs(provider):
    calls = []

    async def execute(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("private fixture failure must not reach wire")
        return {"success": True, "data": []}

    owner, _, _ = await harness(provider, execute)
    try:
        owner._ws.incoming.put_nowait(tool_event(provider, "bad"))
        owner._ws.incoming.put_nowait(tool_event(provider, "good"))
        for _ in range(30):
            if len(outputs(provider, owner._ws.frames)) == 2:
                break
            await asyncio.sleep(0)
        wire_results = outputs(provider, owner._ws.frames)
        assert len(calls) == 2
        assert wire_results[0][1]["success"] is False
        assert wire_results[0][1]["outcome_unknown"] is True
        assert wire_results[1][1]["data"] == []
        assert "private fixture" not in json.dumps(owner._ws.frames)
    finally:
        await settle(owner)


async def test_openai_completed_response_keeps_admitted_tool_work_but_replacement_fences_it():
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"success": True, "data": {}}

    owner, _, _ = await harness("openai", execute)
    try:
        owner._ws.incoming.put_nowait(tool_event("openai"))
        await asyncio.wait_for(entered.wait(), 1)
        owner._ws.incoming.put_nowait({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
        await asyncio.sleep(0)
        release.set()
        for _ in range(15):
            if outputs("openai", owner._ws.frames):
                break
            await asyncio.sleep(0)
        assert len(outputs("openai", owner._ws.frames)) == 1
    finally:
        await settle(owner, release)


async def test_gemini_specific_tool_cancellation_suppresses_bound_callback_and_pending_effect():
    entered, release = asyncio.Event(), asyncio.Event()
    execute = AsyncMock()

    async def delayed(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"success": True, "data": "cancelled-call"}

    execute.side_effect = delayed
    owner, _, _ = await harness("gemini", execute)
    try:
        owner._ws.incoming.put_nowait(tool_event("gemini", "c1"))
        await asyncio.wait_for(entered.wait(), 1)
        owner._ws.incoming.put_nowait(tool_event("gemini", "c2"))
        owner._ws.incoming.put_nowait({"toolCallCancellation": {"ids": ["c1", "c2"]}})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        execute.assert_awaited_once()
        assert outputs("gemini", owner._ws.frames) == []
    finally:
        await settle(owner, release)


async def test_resistant_old_tool_keeps_serial_lane_until_exit():
    retained, release, entered = set(), asyncio.Event(), asyncio.Event()
    old_current = True
    ran = []
    lane = RealtimeToolLane(retained)

    async def old():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        ran.append("old exited")

    async def new():
        ran.append("new")

    lane.submit((1, "old"), lambda: old_current, old)
    await entered.wait()
    old_current = False
    lane.discard_obsolete()
    lane.submit((2, "new"), lambda: True, new)
    await asyncio.sleep(0)
    assert not ran
    assert len(retained) == 1
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert ran == ["old exited", "new"]
    assert not retained
    lane.close()


def test_new_background_dispatch_tasks_have_retained_references():
    path = Path(__file__).parents[1] / "voice" / "realtime_tool_dispatch.py"
    tree = ast.parse(path.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "create_task"]
    assert len(calls) == 1
    assert any(isinstance(node, ast.Assign) and node.value is calls[0]
               and any(isinstance(target, ast.Attribute) and target.attr == "_task" for target in node.targets)
               for node in ast.walk(tree))
    assert "self._retained.add(self._task)" in path.read_text()
    assert "task.exception()" in path.read_text()


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_blocked_refusal_wire_does_not_stall_same_wire_interrupt(provider):
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"success": True, "data": "old"}

    owner, _, interruption = await harness(provider, execute)
    try:
        owner._ws.incoming.put_nowait(tool_event(provider))
        await asyncio.wait_for(entered.wait(), 1)
        owner._ws.block_send = asyncio.Event()
        for index in range(1, MAX_PENDING_TOOLS + 8):
            owner._ws.incoming.put_nowait(tool_event(provider, f"overflow-{index}"))
        owner._ws.incoming.put_nowait(interrupt_event(provider))
        await asyncio.wait_for(interruption.wait(), 0.1)
        assert outputs(provider, owner._ws.frames) == []
        assert len(owner._callback_tasks) <= 3  # two lanes plus receive callback
    finally:
        owner._ws.block_send.set()
        await settle(owner, release)


async def test_openai_continues_once_after_all_same_response_tools_and_done():
    second_entered, release_second = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            second_entered.set()
            await release_second.wait()
        return {"success": True, "data": len(calls)}

    owner, _, _ = await harness("openai", execute)
    original_send = owner._ws.send

    async def provider_send(raw):
        await original_send(raw)
        if json.loads(raw).get("type") == "response.create":
            owner._ws.incoming.put_nowait({"type": "response.created", "response": {"id": "r2"}})

    owner._ws.send = provider_send
    try:
        owner._ws.incoming.put_nowait(tool_event("openai", "c1"))
        owner._ws.incoming.put_nowait(tool_event("openai", "c2"))
        owner._ws.incoming.put_nowait({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
        await asyncio.wait_for(second_entered.wait(), 1)
        assert [identifier for identifier, _ in outputs("openai", owner._ws.frames)] == ["c1"]
        assert not any(frame.get("type") == "response.create" for frame in owner._ws.frames)
        release_second.set()
        for _ in range(30):
            if owner._active_response_id == "r2":
                break
            await asyncio.sleep(0)
        assert len(calls) == 2
        assert [identifier for identifier, _ in outputs("openai", owner._ws.frames)] == ["c1", "c2"]
        assert [frame.get("type") for frame in owner._ws.frames] == [
            "conversation.item.create", "conversation.item.create", "response.create",
        ]
        assert owner._active_response_id == "r2"
    finally:
        await settle(owner, release_second)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_oversized_tool_arguments_are_refused_without_executor_dispatch(provider):
    from voice.realtime_tool_dispatch import MAX_TOOL_EVENT_CHARS
    execute = AsyncMock()
    owner, _, _ = await harness(provider, execute)
    try:
        event = tool_event(provider)
        if provider == "openai":
            event["arguments"] = json.dumps({"large": "x" * MAX_TOOL_EVENT_CHARS})
        else:
            event["toolCall"]["functionCalls"][0]["args"] = {"large": "x" * MAX_TOOL_EVENT_CHARS}
        owner._ws.incoming.put_nowait(event)
        await asyncio.wait_for(owner._ws.sent.wait(), 1)
        execute.assert_not_awaited()
        results = outputs(provider, owner._ws.frames)
        assert results[0][1]["error_code"] == "REALTIME_TOOL_INPUT_TOO_LARGE"
        assert results[0][1]["outcome_unknown"] is False
        assert len(json.dumps(owner._ws.frames)) < 2048
    finally:
        await settle(owner)
