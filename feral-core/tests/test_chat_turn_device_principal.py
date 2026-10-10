"""Authenticated source isolation across real SQLite receipt boundaries."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from agents.chat_turns import ChatTurnError, ChatTurnManager
from memory.store import MemoryStore
from security.approval_ingress import (
    PairedDevicePrincipal, begin_node_approval_ingress,
    device_approval_authority_unavailable, end_node_approval_ingress,
)


@pytest_asyncio.fixture
async def tracked_device(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    state = SimpleNamespace(memory=store, orchestrator=object())
    manager = ChatTurnManager(state)
    yield manager, state
    tasks = []
    for live in list(manager._live.values()):
        if live.task:
            live.task.cancel()
            tasks.append(live.task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if manager._settlements:
        await asyncio.gather(*manager._settlements)
    await state.memory.aclose()


async def finish(manager, accepted, source=None):
    for _ in range(200):
        receipt = await manager.status(session_id=accepted["session_id"], request_id=accepted["request_id"],
                                       source_principal=source)
        if receipt and "processing_outcome" in receipt:
            return receipt
        await asyncio.sleep(0.005)
    raise AssertionError("Tracked processing did not settle")


async def submit(manager, source, request=None, *, emit=None, run=None):
    return await manager.submit(owner=object(), session_id="shared-conversation",
        request_id=request or str(uuid4()), terms={"text": "Read the fixture", "device_target": "brain"},
        source_principal=source, emit=emit or AsyncMock(), run=run or AsyncMock(return_value="fixture result"))


async def test_exact_device_replay_and_foreign_receipt_privacy(tracked_device):
    manager, _ = tracked_device
    own = PairedDevicePrincipal(str(uuid4()), lambda: True)
    foreign = PairedDevicePrincipal(str(uuid4()), lambda: True)
    run, emit = AsyncMock(return_value="private fixture result"), AsyncMock()
    accepted = await submit(manager, own, run=run, emit=emit)
    final = await finish(manager, accepted, own)
    replacement = PairedDevicePrincipal(own.device_id, lambda: True)
    duplicate_run = AsyncMock()
    replay = await submit(manager, replacement, accepted["request_id"], run=duplicate_run)
    assert replay["turn_id"] == accepted["turn_id"] and replay["replayed"] is True
    assert run.await_count == 1 and duplicate_run.await_count == 0
    foreign_emit, foreign_run = AsyncMock(), AsyncMock()
    with pytest.raises(ChatTurnError, match="request_conflict"):
        await submit(manager, foreign, accepted["request_id"], emit=foreign_emit, run=foreign_run)
    foreign_emit.assert_not_awaited()
    foreign_run.assert_not_awaited()
    assert await manager.status(session_id="shared-conversation", request_id=accepted["request_id"],
                                source_principal=foreign) is None
    assert await manager.status(session_id="shared-conversation", request_id=accepted["request_id"]) == final
    public = json.dumps([accepted, final, [call.args for call in emit.await_args_list]])
    assert own.device_id not in public and "source_principal" not in public


async def test_node_admission_is_inherited_and_does_not_enable_approvals(tracked_device):
    manager, _ = tracked_device
    principal = PairedDevicePrincipal(str(uuid4()), lambda: True)
    token = begin_node_approval_ingress(principal)
    try:
        assert device_approval_authority_unavailable() is True
        accepted = await submit(manager, None)
        assert (await finish(manager, accepted))["processing_outcome"] == "completed"
        with pytest.raises(ChatTurnError, match="device_authority_unavailable"):
            await submit(manager, PairedDevicePrincipal(principal.device_id, lambda: True))
    finally:
        end_node_approval_ingress(token)
    token = begin_node_approval_ingress()
    try:
        with pytest.raises(ChatTurnError, match="device_authority_unavailable"):
            await submit(manager, None)
    finally:
        end_node_approval_ingress(token)


@pytest.mark.parametrize("timing", ["before_claim", "during_acceptance", "before_run"])
async def test_revocation_fences_effect_admission(tracked_device, timing):
    manager, state = tracked_device
    current = timing != "before_claim"
    source = PairedDevicePrincipal(str(uuid4()), lambda: current)
    run = AsyncMock(return_value="must not execute")
    request = str(uuid4())

    async def emit(_kind, _payload):
        nonlocal current
        if timing == "during_acceptance":
            current = False
        elif timing == "before_run":
            asyncio.get_running_loop().call_soon(revoke)

    def revoke():
        nonlocal current
        current = False

    if timing in {"before_claim", "during_acceptance"}:
        with pytest.raises(asyncio.CancelledError):
            await submit(manager, source, request, run=run, emit=emit)
    else:
        accepted = await submit(manager, source, request, run=run, emit=emit)
        assert (await finish(manager, accepted))["processing_outcome"] == "cancelled"
    run.assert_not_awaited()
    row = await state.memory.chat_turn_get(session_id="shared-conversation", request_id=request)
    assert row is None if timing == "before_claim" else row["processing_outcome"] == "cancelled"


async def test_reopen_retains_private_source_without_replay(tracked_device):
    manager, state = tracked_device
    source = PairedDevicePrincipal(str(uuid4()), lambda: True)
    accepted = await submit(manager, source)
    await finish(manager, accepted, source)
    path = state.memory.db_path
    await state.memory.aclose()
    state.memory = MemoryStore(db_path=path)
    restarted = ChatTurnManager(state)
    run = AsyncMock()
    replay = await submit(restarted, source, accepted["request_id"], run=run)
    assert replay["replayed"] and replay["turn_id"] == accepted["turn_id"]
    run.assert_not_awaited()
    assert await restarted.status(session_id="shared-conversation", request_id=accepted["request_id"],
        source_principal=PairedDevicePrincipal(str(uuid4()), lambda: True)) is None


async def test_revoked_subscriber_does_not_block_current_terminal_delivery(tracked_device):
    manager, _ = tracked_device
    release, entered = asyncio.Event(), asyncio.Event()
    current = True
    original = PairedDevicePrincipal(str(uuid4()), lambda: current)
    first_events = []

    async def first_emit(kind, payload):
        original.require_current()
        first_events.append(kind)

    async def run():
        entered.set()
        await release.wait()
        return "committed fixture"

    accepted = await submit(manager, original, run=run, emit=first_emit)
    await entered.wait()
    replacement = PairedDevicePrincipal(original.device_id, lambda: True)
    emit = AsyncMock()
    await submit(manager, replacement, accepted["request_id"], emit=emit)
    current = False
    release.set()
    await finish(manager, accepted, replacement)
    await asyncio.sleep(0)
    assert first_events == ["chat_turn_accepted"]
    assert [call.args[0] for call in emit.await_args_list] == ["chat_turn_accepted", "chat_turn_terminal"]


async def test_old_receipt_schema_migrates_without_assigning_device(tmp_path):
    path = str(tmp_path / "old.db")
    request, turn = str(uuid4()), str(uuid4())
    receipt = {"contract_version": 1, "session_id": "shared-conversation", "request_id": request,
               "turn_id": turn, "durable": True, "processing_outcome": "completed", "final_text": "old"}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE chat_turn_receipts (turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_id TEXT NOT NULL, input_digest TEXT NOT NULL, status TEXT NOT NULL, receipt_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(session_id, request_id))")
        db.execute("INSERT INTO chat_turn_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (turn, "shared-conversation", request, "old-digest", "terminal", json.dumps(receipt), 1, 1))
    store = MemoryStore(db_path=path)
    try:
        assert await store.chat_turn_get(session_id="shared-conversation", request_id=request) == receipt
        source = PairedDevicePrincipal(str(uuid4()), lambda: True).storage_binding()
        assert await store.chat_turn_get(session_id="shared-conversation", request_id=request, source_principal=source) is None
        conflict = await store.chat_turn_claim(session_id="shared-conversation", request_id=request,
            turn_id=str(uuid4()), input_digest="old-digest", receipt={}, source_principal=source)
        assert conflict == {"created": False, "conflict": True}
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT source_principal_json FROM chat_turn_receipts").fetchone() == (None,)
        # Named inserts remain compatible after the additive migration.
        fresh = await store.chat_turn_claim(session_id="other", request_id=str(uuid4()), turn_id=str(uuid4()),
            input_digest="new", receipt={"durable": True})
        assert fresh["created"] is True
    finally:
        await store.aclose()


@pytest.mark.parametrize("identity", ["", "not-a-uuid", str(uuid4()).upper()])
def test_principal_requires_canonical_server_identity(identity):
    with pytest.raises(ValueError):
        PairedDevicePrincipal(identity, lambda: True)
