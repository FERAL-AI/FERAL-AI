"""The shared desktop/phone turn runner must never report failure as prose."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_unexpected_turn_failure_is_error_frame(monkeypatch):
    import api.server as server

    orchestrator = SimpleNamespace(handle_command_stream=AsyncMock(side_effect=RuntimeError("private diagnostic")))
    monkeypatch.setattr(server, "state", SimpleNamespace(orchestrator=orchestrator, skill_gen=None))
    ws = SimpleNamespace(send_json=AsyncMock())
    await server._build_chat_turn_runner(ws=ws, session_id="test", refined_text="hello", ctx={})

    ws.send_json.assert_awaited_once()
    frame = ws.send_json.await_args.args[0]
    assert frame["type"] == "error"
    assert frame["session_id"] == "test"
    assert frame["payload"] == {
        "code": "chat_turn_failed",
        "message": "The chat turn failed. Please try again.",
        "recoverable": True,
    }
    assert "private diagnostic" not in str(frame)


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_emit_success_or_failure(monkeypatch):
    import api.server as server

    orchestrator = SimpleNamespace(handle_command_stream=AsyncMock(side_effect=asyncio.CancelledError()))
    monkeypatch.setattr(server, "state", SimpleNamespace(orchestrator=orchestrator, skill_gen=None))
    ws = SimpleNamespace(send_json=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await server._build_chat_turn_runner(ws=ws, session_id="test", refined_text="hello", ctx={})
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_tracked_owner_progress_is_correlated_and_failure_never_suggests_retry(tmp_path, monkeypatch, failed):
    import api.server as server
    from agents.chat_turns import ChatTurnManager, turn_audit
    from memory.store import MemoryStore

    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    ws = SimpleNamespace(send_json=AsyncMock())

    async def command(**kwargs):
        audit = turn_audit("exact-A")
        assert audit is not None
        audit.began = True
        if failed:
            raise RuntimeError("PRIVATE_DIAGNOSTIC_SENTINEL")
        return "Actual processing reply"

    skill_gen = SimpleNamespace(
        detect_unmet_need=AsyncMock(return_value={"capability": "fixture", "service": "local"}),
        generate_skill=AsyncMock(return_value={"id": "unapproved-fixture"}),
    )
    state = SimpleNamespace(memory=store, orchestrator=SimpleNamespace(handle_command_stream=command), skill_gen=skill_gen)
    monkeypatch.setattr(server, "state", state)
    manager = ChatTurnManager(state)
    request_id = str(uuid4())
    emit = AsyncMock()

    async def run():
        return await server._build_chat_turn_runner(ws=ws, session_id="exact-A", refined_text="fixture", ctx={}, tracked=True)

    try:
        accepted = await manager.submit(owner=ws, session_id="exact-A", request_id=request_id, terms={"text": "fixture"}, run=run, emit=emit)
        tasks = [live.task for live in manager._live.values() if live.task is not None]
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        ws.send_json.assert_awaited_once()
        frame = ws.send_json.await_args.args[0]
        assert frame["session_id"] == "exact-A"
        assert frame["payload"]["chat_turn"] == {"contract_version": 1, "request_id": request_id, "turn_id": accepted["turn_id"]}
        receipt = await manager.status(session_id="exact-A", request_id=request_id)
        if failed:
            assert frame["type"] == "error" and frame["payload"]["recoverable"] is False
            assert "Inspect its status" in frame["payload"]["message"]
            assert "try again" not in frame["payload"]["message"]
            assert "PRIVATE_DIAGNOSTIC_SENTINEL" not in str(frame)
            assert receipt["processing_outcome"] == "outcome_unknown" and receipt["action_outcome"] == "unknown"
            skill_gen.generate_skill.assert_not_awaited()
        else:
            assert frame["type"] == "skill_proposal" and frame["payload"]["manifest"] == {"id": "unapproved-fixture"}
            assert receipt["processing_outcome"] == "completed" and receipt["action_outcome"] == "not_asserted"
    finally:
        await store.aclose()
