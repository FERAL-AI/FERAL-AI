"""Create-only draft invariants; controlled providers and disposable SQLite only."""
import asyncio
import hashlib
import sqlite3
import threading
from unittest.mock import AsyncMock, patch

import pytest

from agents.tool_genesis import GeneratedTool, ToolGenesisEngine, ToolSequence


CODE = "async def main(args):\n    raise RuntimeError('fixture must never execute')"


class Provider:
    def __init__(self, code=CODE):
        self.code = code
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = None

    async def chat(self, messages):
        self.calls += 1
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return {"fixture": self.code}

    def extract_response(self, response):
        return response["fixture"], []


def intent_sig(intent):
    return hashlib.md5(("intent::" + intent[:200]).encode()).hexdigest()[:12]


def engine(tmp_path, provider, *, persisted=True):
    return ToolGenesisEngine(llm=provider, db_path=str(tmp_path / "genesis.sqlite") if persisted else None)


def rows(tmp_path):
    with sqlite3.connect(tmp_path / "genesis.sqlite") as con:
        return con.execute("SELECT sig,tool_id,python_code,use_count FROM generated_tools ORDER BY sig").fetchall()


def sequence(subject, sig):
    subject._sequences[sig] = ToolSequence(tools=["notes__read", "notes__list"], args_signature=sig, count=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_same_prefix_does_not_replace_approved_live_record(tmp_path, persisted):
    provider = Provider()
    subject = engine(tmp_path, provider, persisted=persisted)
    first = "x" * 199 + "e\u0301 first"
    second = "x" * 199 + "e\u0301 changed"
    target = await subject.propose_from_intent(first)
    assert target == "genesis_intent_" + intent_sig(first)
    original = subject.get_generated(target)
    assert subject.approve_tool(target) is True
    assert await subject.propose_from_intent(second) is None
    assert provider.calls == 1
    assert subject.get_generated(target) is original
    assert original.approved is True and original.python_code == CODE
    assert len(subject.list_generated()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first_kind", ["intent", "sequence"])
async def test_shared_signature_namespace_never_replaces_record(tmp_path, first_kind):
    provider = Provider()
    subject = engine(tmp_path, provider)
    intent = "Synthetic shared namespace"
    sig = intent_sig(intent)
    sequence(subject, sig)
    if first_kind == "intent":
        target = await subject.propose_from_intent(intent)
        assert await subject.generate_tool(sig) is None
    else:
        target = (await subject.generate_tool(sig)).tool_id
        assert await subject.propose_from_intent(intent) is None
    assert provider.calls == 1
    assert list(subject._generated) == [sig]
    assert rows(tmp_path) == [(sig, target, CODE, 0)]


@pytest.mark.asyncio
async def test_existing_database_target_is_refused_by_stale_engine_before_provider(tmp_path):
    first_provider, stale_provider = Provider(), Provider()
    first, stale = engine(tmp_path, first_provider), engine(tmp_path, stale_provider)
    intent = "Synthetic persisted target"
    target = await first.propose_from_intent(intent)
    assert stale.get_generated(target) is None
    assert await stale.propose_from_intent(intent) is None
    assert stale_provider.calls == 0 and stale._generated == {}
    assert rows(tmp_path) == [(intent_sig(intent), target, CODE, 0)]


@pytest.mark.asyncio
async def test_live_partial_record_and_foreign_signature_tool_id_refuse_creation(tmp_path):
    provider = Provider()
    subject = engine(tmp_path, provider)
    intent = "Synthetic partial target"
    sig = intent_sig(intent)
    partial = GeneratedTool("genesis_intent_" + sig, "existing", "partial", [], "existing code", approved=True)
    subject._generated[sig] = partial
    assert await subject.propose_from_intent(intent) is None
    assert provider.calls == 0 and subject._generated[sig] is partial and partial.approved
    subject._generated.clear()
    assert subject._insert_new_generated("foreign-signature", partial)
    assert await subject.propose_from_intent(intent) is None
    assert provider.calls == 0 and subject._generated == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["intent", "sequence"])
async def test_same_engine_waiters_generate_once_and_keep_live_lock(tmp_path, kind):
    provider = Provider()
    provider.release = asyncio.Event()
    subject = engine(tmp_path, provider)
    intent = "Synthetic concurrent target"
    sig = intent_sig(intent)
    sequence(subject, sig)
    call = (lambda: subject.propose_from_intent(intent)) if kind == "intent" else (lambda: subject.generate_tool(sig))
    first = asyncio.create_task(call())
    await asyncio.wait_for(provider.entered.wait(), 1)
    waiting = asyncio.create_task(call())
    for _ in range(10):
        await asyncio.sleep(0)
    assert provider.calls == 1 and subject._generation_lock.locked()
    held = subject._generation_lock
    provider.release.set()
    results = await asyncio.gather(first, waiting)
    assert sum(result is not None for result in results) == 1
    assert provider.calls == 1 and len(rows(tmp_path)) == 1
    assert subject._generation_lock is held and not held.locked()


@pytest.mark.asyncio
async def test_queued_waiter_cancellation_does_not_evict_held_target_lock(tmp_path):
    provider = Provider()
    provider.release = asyncio.Event()
    subject = engine(tmp_path, provider)
    intent = "Synthetic cancelled waiter"
    first = asyncio.create_task(subject.propose_from_intent(intent))
    await asyncio.wait_for(provider.entered.wait(), 1)
    waiter = asyncio.create_task(subject.propose_from_intent(intent))
    for _ in range(10):
        await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert subject._generation_lock.locked() and provider.calls == 1
    last = asyncio.create_task(subject.propose_from_intent(intent))
    provider.release.set()
    assert await first is not None
    assert await last is None
    assert not subject._generation_lock.locked() and provider.calls == 1


@pytest.mark.asyncio
async def test_independent_engine_race_preserves_one_commit_but_can_call_provider_twice(tmp_path):
    left_provider, right_provider = Provider(), Provider("async def main(args):\n    return {'different': True}")
    barrier = asyncio.Event()
    left_provider.release = right_provider.release = barrier
    left, right = engine(tmp_path, left_provider), engine(tmp_path, right_provider)
    intent = "Synthetic cross-instance target"
    tasks = [asyncio.create_task(left.propose_from_intent(intent)), asyncio.create_task(right.propose_from_intent(intent))]
    await asyncio.wait_for(asyncio.gather(left_provider.entered.wait(), right_provider.entered.wait()), 1)
    barrier.set()
    results = await asyncio.gather(*tasks)
    assert sum(result is not None for result in results) == 1
    assert left_provider.calls == right_provider.calls == 1  # Explicit remaining provider-call limitation.
    winner = left if results[0] is not None else right
    loser = right if results[0] is not None else left
    stored = rows(tmp_path)
    assert len(stored) == 1 and stored[0][2] == winner.get_generated(stored[0][1]).python_code
    assert loser._generated == {}
    assert await loser.propose_from_intent(intent) is None
    assert left_provider.calls == right_provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lookup", "insert", "readback", "commit_ack"])
@pytest.mark.parametrize("kind", ["intent", "sequence"])
async def test_storage_failures_never_publish_unconfirmed_draft(tmp_path, monkeypatch, failure, kind):
    provider = Provider()
    subject = engine(tmp_path, provider)
    intent = "Synthetic storage failure"
    sig = intent_sig(intent)
    sequence(subject, sig)
    insert = subject._insert_new_generated

    def failing(*args):
        raise sqlite3.OperationalError("fixture storage failure")

    def ambiguous(*args):
        assert insert(*args)
        if failure == "readback":
            return False
        raise sqlite3.OperationalError("fixture commit reply lost")

    if failure == "lookup":
        monkeypatch.setattr(subject, "_stored_target_exists", failing)
    else:
        monkeypatch.setattr(subject, "_insert_new_generated", ambiguous if failure in {"readback", "commit_ack"} else failing)
    result = await (subject.propose_from_intent(intent) if kind == "intent" else subject.generate_tool(sig))
    assert result is None and subject._generated == {}
    assert provider.calls == (0 if failure == "lookup" else 1)
    assert len(rows(tmp_path)) == (1 if failure in {"readback", "commit_ack"} else 0)
    if failure in {"readback", "commit_ack"}:
        result = await (subject.propose_from_intent(intent) if kind == "intent" else subject.generate_tool(sig))
        assert result is None and provider.calls == 1  # Committed row fences another call despite lost publication.


@pytest.mark.asyncio
async def test_verified_commit_precedes_live_publication_and_offloads_io(tmp_path, monkeypatch):
    provider = Provider()
    subject = engine(tmp_path, provider)
    intent = "Synthetic commit visibility"
    insert = subject._insert_new_generated
    event_thread = threading.get_ident()

    def inspect_commit(sig, tool):
        assert threading.get_ident() != event_thread
        assert subject._generated == {}
        committed = insert(sig, tool)
        assert subject._generated == {} and rows(tmp_path)[0][2] == CODE
        return committed

    monkeypatch.setattr(subject, "_insert_new_generated", inspect_commit)
    target = await subject.propose_from_intent(intent)
    assert subject.get_generated(target).python_code == CODE


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mismatched_readback", "lost_commit_ack"])
async def test_actual_sqlite_commit_and_readback_boundaries(tmp_path, monkeypatch, failure):
    provider = Provider()
    subject = engine(tmp_path, provider)
    connect = sqlite3.connect

    class FaultConnection(sqlite3.Connection):
        def commit(self):
            super().commit()
            if failure == "lost_commit_ack":
                raise sqlite3.OperationalError("fixture acknowledgement lost after actual commit")

        def execute(self, sql, *args):
            cursor = super().execute(sql, *args)
            if failure == "mismatched_readback" and sql.startswith("SELECT sig, tool_id, name, description"):
                actual = cursor.fetchone()

                class Mismatch:
                    def fetchone(self):
                        return (*actual[:5], "different exposed code", *actual[6:])

                return Mismatch()
            return cursor

    # Affect only the insertion helper; existence checks retain real connections.
    insert = subject._insert_new_generated

    def faulty_insert(*args):
        with patch("agents.tool_genesis.sqlite3.connect", side_effect=lambda *a, **kw: connect(*a, **kw, factory=FaultConnection)):
            return insert(*args)

    monkeypatch.setattr(subject, "_insert_new_generated", faulty_insert)
    intent = "Synthetic actual SQL failure"
    assert await subject.propose_from_intent(intent) is None
    assert subject._generated == {} and provider.calls == 1
    assert rows(tmp_path) == [(intent_sig(intent), "genesis_intent_" + intent_sig(intent), CODE, 0)]
    assert await subject.propose_from_intent(intent) is None
    assert provider.calls == 1 and not subject._generation_lock.locked()


@pytest.mark.asyncio
async def test_drafting_does_not_approve_promote_or_execute(tmp_path):
    provider = Provider()
    subject = engine(tmp_path, provider)
    sequence(subject, "synthetic-sequence")
    with patch.object(subject, "approve_tool", side_effect=AssertionError("no approval")), patch.object(subject, "promote", side_effect=AssertionError("no promotion")), patch.object(subject, "execute_tool", new=AsyncMock(side_effect=AssertionError("no execution"))):
        intent_target = await subject.propose_from_intent("Synthetic unapproved draft")
        sequence_target = await subject.generate_tool("synthetic-sequence")
    assert subject.get_generated(intent_target).approved is False
    assert sequence_target.approved is False
    assert all(tool.requires_approval and tool.use_count == 0 for tool in subject._generated.values())


@pytest.mark.asyncio
async def test_provider_cancellation_propagates_without_draft_or_retained_lock(tmp_path):
    provider = Provider()
    provider.release = asyncio.Event()
    subject = engine(tmp_path, provider)
    task = asyncio.create_task(subject.propose_from_intent("Synthetic cancelled generation"))
    await asyncio.wait_for(provider.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert subject._generated == {} and rows(tmp_path) == []
    assert not subject._generation_lock.locked()


@pytest.mark.asyncio
async def test_cancellation_during_sqlite_worker_is_not_rollback_or_live_success(tmp_path, monkeypatch):
    provider = Provider()
    subject = engine(tmp_path, provider)
    intent = "Synthetic commit cancellation"
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    insert = subject._insert_new_generated

    def controlled_insert(*args):
        entered.set()
        assert release.wait(2), "fixture did not release SQLite worker"
        try:
            assert insert(*args)
        finally:
            committed.set()
        return True

    monkeypatch.setattr(subject, "_insert_new_generated", controlled_insert)
    task = asyncio.create_task(subject.propose_from_intent(intent))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert subject._generated == {} and not subject._generation_lock.locked()
        release.set()
        assert await asyncio.to_thread(committed.wait, 1)
        assert rows(tmp_path) == [(intent_sig(intent), "genesis_intent_" + intent_sig(intent), CODE, 0)]
        assert subject._generated == {}  # Background commit is not a success notification.
        assert await subject.propose_from_intent(intent) is None
        assert provider.calls == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_distinct_targets_share_one_bounded_generation_lock(tmp_path):
    provider = Provider()
    provider.release = asyncio.Event()
    subject = engine(tmp_path, provider)
    first = asyncio.create_task(subject.propose_from_intent("Synthetic first distinct draft"))
    await asyncio.wait_for(provider.entered.wait(), 1)
    second = asyncio.create_task(subject.propose_from_intent("Synthetic second distinct draft"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert provider.calls == 1
    provider.release.set()
    assert all(await asyncio.gather(first, second))
    assert provider.calls == 2 and len(rows(tmp_path)) == 2
    assert not subject._generation_lock.locked()
