"""Real SQLite chat receipts, with controlled execution and no external effects."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from agents.chat_turns import ChatTurnManager, ChatTurnError, turn_audit
from memory.store import MemoryStore


@pytest_asyncio.fixture
async def tracked(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    state = SimpleNamespace(memory=store, orchestrator=object())
    manager = ChatTurnManager(state)
    yield manager, state
    for live in list(manager._live.values()):
        if live.task:
            live.task.cancel()
    tasks = [live.task for live in list(manager._live.values()) if live.task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if manager._settlements:
        await asyncio.gather(*manager._settlements)
    await state.memory.aclose()
    if state.memory is not store:
        await store.aclose()


async def submitted(manager, *, run=None, owner=None, session="thread-A", request_id=None, text="fixture", emit=None):
    owner = owner or object()
    emit = emit or AsyncMock()
    run = run or AsyncMock(return_value="final fixture response")
    receipt = await manager.submit(owner=owner, session_id=session, request_id=request_id or str(uuid4()),
                                   terms={"text": text}, run=run, emit=emit)
    return receipt, owner, emit, run


async def terminal(manager, receipt):
    for _ in range(200):
        row = await manager.status(session_id=receipt["session_id"], turn_id=receipt["turn_id"])
        if "processing_outcome" in row:
            return row
        await asyncio.sleep(0.005)
    raise AssertionError("No bounded terminal receipt")


async def test_committed_identity_deduplicates_without_execution(tracked):
    manager, state = tracked
    receipt, owner, emit, run = await submitted(manager)
    final = await terminal(manager, receipt)
    assert final["processing_outcome"] == "completed"
    assert final["final_text"] == "final fixture response" and final["action_outcome"] == "not_asserted"
    duplicate = await submitted(manager, request_id=receipt["request_id"], owner=owner)
    assert duplicate[0]["turn_id"] == receipt["turn_id"] and duplicate[0]["replayed"] is True
    assert duplicate[3].await_count == 0 and run.await_count == 1
    with pytest.raises(ChatTurnError, match="request_conflict"):
        await submitted(manager, request_id=receipt["request_id"], text="changed action")
    assert await manager.status(session_id="foreign-thread", turn_id=receipt["turn_id"]) is None
    assert (await state.memory.chat_turn_get(session_id="thread-A", request_id=receipt["request_id"]))["durable"] is True


async def test_duplicate_live_request_subscribes_reconnected_reader_once(tracked):
    manager, _state = tracked
    entered, release = asyncio.Event(), asyncio.Event()

    async def run():
        entered.set()
        await release.wait()
        return "actual final"

    receipt, old_owner, old_emit, _ = await submitted(manager, run=run)
    await entered.wait()
    replacement = await submitted(manager, request_id=receipt["request_id"])
    assert replacement[0]["turn_id"] == receipt["turn_id"] and replacement[3].await_count == 0
    refused = await manager.abort(owner=replacement[1], session_id="thread-A", turn_id=receipt["turn_id"], request_id=receipt["request_id"])
    assert refused["cancel_requested"] is False
    release.set()
    await terminal(manager, receipt)
    await asyncio.sleep(0)
    assert [call.args[0] for call in replacement[2].await_args_list] == ["chat_turn_accepted", "chat_turn_terminal"]
    assert [call.args[0] for call in old_emit.await_args_list] == ["chat_turn_accepted", "chat_turn_terminal"]


@pytest.mark.parametrize("outcome", ["pending", "error", "budget", "refused", "empty", "exception"])
async def test_truthful_processing_outcomes_never_assert_effect_success(tracked, outcome):
    manager, _state = tracked

    async def run():
        audit = turn_audit("thread-A")
        if outcome == "pending":
            audit.approval_request_ids.append("review-1")
        elif outcome == "error":
            audit.error = True
        elif outcome == "budget":
            audit.budget_exceeded = True
        elif outcome == "refused":
            audit.refused = True
        elif outcome == "empty":
            return None
        else:
            audit.began = True
            raise RuntimeError("private effect diagnostic")
        return "fixture response"

    receipt, _owner, _emit, _ = await submitted(manager, run=run)
    final = await terminal(manager, receipt)
    expected = dict(pending="awaiting_approval", error="failed", budget="budget_exceeded", refused="refused", empty="unavailable", exception="outcome_unknown")
    assert final["processing_outcome"] == expected[outcome]
    assert final["action_outcome"] != "success" and "private effect" not in str(final)


async def test_acceptance_storage_failure_has_no_ack_and_no_execution(tracked, monkeypatch):
    manager, state = tracked
    await manager.start()
    monkeypatch.setattr(state.memory, "chat_turn_claim", AsyncMock(side_effect=RuntimeError("disk full")))
    emit, run = AsyncMock(), AsyncMock()
    with pytest.raises(RuntimeError):
        await submitted(manager, emit=emit, run=run)
    emit.assert_not_awaited()
    run.assert_not_awaited()


async def test_terminal_storage_failure_never_sends_durable_terminal(tracked, monkeypatch):
    manager, state = tracked
    original = state.memory.chat_turn_update

    async def update(**kwargs):
        if kwargs["status"] == "terminal":
            raise RuntimeError("disk full")
        return await original(**kwargs)

    monkeypatch.setattr(state.memory, "chat_turn_update", update)
    receipt, _owner, emit, _run = await submitted(manager)
    for _ in range(200):
        if len(emit.await_args_list) >= 2:
            break
        await asyncio.sleep(0.005)
    assert [call.args[0] for call in emit.await_args_list] == ["chat_turn_accepted", "error"]
    assert "processing_outcome" not in await manager.status(session_id="thread-A", turn_id=receipt["turn_id"])


async def test_reopen_fences_interrupted_receipts_without_replay(tracked):
    manager, state = tracked
    await manager.start()
    request, turn = str(uuid4()), str(uuid4())
    accepted = dict(contract_version=1, request_id=request, turn_id=turn, session_id="thread-A", status="accepted", durable=True, replayed=False)
    await state.memory.chat_turn_claim(session_id="thread-A", request_id=request, turn_id=turn, input_digest="fixture-digest", receipt=accepted)
    path = state.memory.db_path
    await state.memory.aclose()
    state.memory = MemoryStore(db_path=path)
    restarted = ChatTurnManager(state)
    await restarted.start()
    row = await restarted.status(session_id="thread-A", request_id=request)
    assert row["processing_outcome"] == "outcome_unknown" and restarted._live == {}


async def test_configured_capacity_and_input_bounds_fail_before_execution(tracked, monkeypatch):
    manager, _state = tracked
    monkeypatch.setenv("FERAL_CHAT_TURN_RECEIPT_LIMIT", "1")
    receipt, *_ = await submitted(manager)
    await terminal(manager, receipt)
    with pytest.raises(ChatTurnError, match="quota"):
        await submitted(manager)
    monkeypatch.setenv("FERAL_CHAT_TURN_RECEIPT_LIMIT", "2")
    next_receipt, *_ = await submitted(manager)
    await terminal(manager, next_receipt)
    with pytest.raises(ChatTurnError, match="input_too_large"):
        await submitted(manager, text="x" * (8 * 1024 * 1024 + 1))
    for session in (" leading", "trailing ", "bad\nthread", "bad\x7fthread", "x" * 1025):
        with pytest.raises(ChatTurnError, match="invalid_request"):
            await submitted(manager, session=session)


async def test_active_session_quota_and_atomic_duplicate_claims(tracked):
    manager, _state = tracked
    request = str(uuid4())
    release = asyncio.Event()

    async def waiting():
        await release.wait()
        return "done"

    paired = await asyncio.gather(submitted(manager, request_id=request, run=waiting),
                                   submitted(manager, request_id=request, run=waiting))
    assert paired[0][0]["turn_id"] == paired[1][0]["turn_id"] and len(manager._live) == 1
    for _ in range(7):
        await submitted(manager, run=waiting)
    with pytest.raises(ChatTurnError, match="quota"):
        await submitted(manager, run=waiting)
    release.set()
    for receipt, *_rest in paired:
        await terminal(manager, receipt)
