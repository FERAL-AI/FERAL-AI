"""Trusted progress attribution through the real session sender and turn manager."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agents.chat_turns import ChatTurnManager
from api.state import BrainState
from memory.store import MemoryStore
from models.protocol import FeralMessage


@pytest.mark.asyncio
async def test_progress_uses_current_exact_turn_without_overwriting_permission_identity(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    ws = SimpleNamespace(send_json=AsyncMock())
    state = SimpleNamespace(memory=store, orchestrator=object(), sessions={"A": ws}, _channel_collectors={})
    state.send_to_session = BrainState.send_to_session.__get__(state)
    manager = ChatTurnManager(state)
    request = str(uuid4())
    original = FeralMessage(type="permission_request", session_id="A", payload={"request_id": "folder-review", "path": "/disposable/project", "chat_turn": {"turn_id": "forged"}})
    done = asyncio.Event()

    async def run():
        assert await state.send_to_session("A", original)
        done.set()
        return "finished"

    try:
        accepted = await manager.submit(owner=ws, session_id="A", request_id=request, terms={"text": "fixture"}, run=run, emit=AsyncMock())
        await asyncio.wait_for(done.wait(), 5)
        await asyncio.gather(*(live.task for live in list(manager._live.values()) if live.task))
        sent = ws.send_json.await_args.args[0]
        assert sent["payload"]["request_id"] == "folder-review"
        assert sent["payload"]["chat_turn"] == {"contract_version": 1, "request_id": request, "turn_id": accepted["turn_id"]}
        assert original.payload["chat_turn"] == {"turn_id": "forged"}
        assert (await manager.status(session_id="A", request_id=request))["processing_outcome"] == "completed"
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_concurrent_progress_cannot_inherit_another_session_or_closed_turn(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    sinks = {sid: SimpleNamespace(send_json=AsyncMock()) for sid in ("A", "B", "other")}
    state = SimpleNamespace(memory=store, orchestrator=object(), sessions=sinks, _channel_collectors={})
    state.send_to_session = BrainState.send_to_session.__get__(state)
    manager = ChatTurnManager(state)
    arrived = {sid: asyncio.Event() for sid in ("A", "B")}
    release = asyncio.Event()

    async def run(sid):
        arrived[sid].set()
        await release.wait()
        await state.send_to_session(sid, FeralMessage(type="stream_delta", session_id=sid, payload={"delta": sid}))
        await state.send_to_session("other", FeralMessage(type="text_response", session_id="other", payload={"text": "unrelated"}))
        return sid

    try:
        accepted = {}
        for sid in ("A", "B"):
            async def execute(sid=sid):
                return await run(sid)
            accepted[sid] = await manager.submit(owner=sinks[sid], session_id=sid, request_id=str(uuid4()), terms={"text": sid}, run=execute, emit=AsyncMock())
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in arrived.values())), 5)
        tasks = [live.task for live in manager._live.values() if live.task]
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        for sid in ("A", "B"):
            identity = sinks[sid].send_json.await_args.args[0]["payload"]["chat_turn"]
            assert identity["request_id"] == accepted[sid]["request_id"]
            assert identity["turn_id"] == accepted[sid]["turn_id"]
        assert all("chat_turn" not in call.args[0]["payload"] for call in sinks["other"].send_json.await_args_list)
        await state.send_to_session("A", FeralMessage(type="text_response", session_id="A", payload={"text": "outside turn"}))
        assert "chat_turn" not in sinks["A"].send_json.await_args.args[0]["payload"]
    finally:
        await store.aclose()
