"""Captured attachment ownership with genuine checkpoint SQLite and SID locks."""
import asyncio
from types import SimpleNamespace

import pytest

from agents.runtime_context_checkpoint import (
    ContextReadinessState, RuntimeContextCoordinator, RuntimeContextError, RuntimeContextReadiness,
)
from api.runtime_context import prepared_scope
from memory.runtime_session_checkpoint import CheckpointStatus
from memory.store import MemoryStore


@pytest.fixture
async def attached(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "attached.db"))
    locks, history = {}, {}
    coordinator = RuntimeContextCoordinator(store, history=history,
        lock_for=lambda sid: locks.setdefault(sid, asyncio.Lock()),
        image_call_ids=lambda sid: frozenset(), clear_images=lambda sid: None)
    attachment = await coordinator.attach("thread-A", checkpoint_version=1)
    owner = object()
    state = SimpleNamespace(memory=store, orchestrator=SimpleNamespace(_context_checkpoints=coordinator),
                            sessions={"thread-A": owner})
    try:
        yield state, coordinator, attachment, owner, store
    finally:
        await store.aclose()


def scope(state, attachment, owner):
    return prepared_scope(state, "thread-A", attachment=attachment,
                          current_owner=lambda: state.sessions.get("thread-A") is owner)


async def test_reviewed_fence_can_commit_its_own_next_revision(attached):
    state, coordinator, attachment, owner, store = attached
    expected = (await store.runtime_checkpoint_read("thread-A")).record.fence
    async with prepared_scope(state, "thread-A", attachment=attachment,
            current_owner=lambda: state.sessions.get("thread-A") is owner,
            expected_fence=expected) as receipt:
        coordinator.history["thread-A"] = [{"role": "user", "content": "reviewed utterance"}]
    assert receipt.committed_fence.generation == expected.generation
    assert receipt.committed_fence.revision == expected.revision + 2


async def test_stale_reviewed_fence_refuses_without_starting_another_attempt(attached):
    state, _coordinator, attachment, owner, store = attached
    expected = (await store.runtime_checkpoint_read("thread-A")).record.fence
    async with scope(state, attachment, owner):
        pass
    current = (await store.runtime_checkpoint_read("thread-A")).record.fence
    with pytest.raises(RuntimeContextError, match="context_review_superseded"):
        async with prepared_scope(state, "thread-A", attachment=attachment,
                current_owner=lambda: state.sessions.get("thread-A") is owner,
                expected_fence=expected):
            pytest.fail("Stale reviewed context entered its command")
    assert (await store.runtime_checkpoint_read("thread-A")).record.fence == current


async def test_queued_review_rechecks_durable_fence_after_lock_admission(attached):
    from uuid import uuid4
    from memory.runtime_session_checkpoint import encode_context
    state, coordinator, attachment, owner, store = attached
    expected = (await store.runtime_checkpoint_read("thread-A")).record.fence
    lock = coordinator.lock_for("thread-A")
    await lock.acquire()
    async def queued():
        async with prepared_scope(state, "thread-A", attachment=attachment,
                current_owner=lambda: state.sessions.get("thread-A") is owner,
                expected_fence=expected):
            pytest.fail("Queued stale review entered its command")
    task = asyncio.create_task(queued())
    try:
        await asyncio.sleep(0)
        assert not task.done()
        begun = await store.runtime_checkpoint_begin("thread-A", attempt_id=str(uuid4()), expected=expected)
        assert begun.status == CheckpointStatus.APPLIED
        committed = await store.runtime_checkpoint_commit(begun.record.fence, encode_context([], []))
        assert committed.status == CheckpointStatus.APPLIED
    finally:
        lock.release()
    with pytest.raises(RuntimeContextError, match="checkpoint_conflict"):
        await task
    assert (await store.runtime_checkpoint_read("thread-A")).record.fence == committed.record.fence


async def test_queued_review_refuses_after_another_coordinated_turn_commits(attached):
    state, _coordinator, attachment, owner, store = attached
    expected = (await store.runtime_checkpoint_read("thread-A")).record.fence
    entered, release = asyncio.Event(), asyncio.Event()
    async def first():
        async with scope(state, attachment, owner):
            entered.set()
            await release.wait()
    first_task = asyncio.create_task(first())
    await asyncio.wait_for(entered.wait(), 2)
    async def queued():
        async with prepared_scope(state, "thread-A", attachment=attachment,
                current_owner=lambda: state.sessions.get("thread-A") is owner,
                expected_fence=expected):
            pytest.fail("Review from before the other turn entered a command")
    task = asyncio.create_task(queued())
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await first_task
    current = (await store.runtime_checkpoint_read("thread-A")).record.fence
    with pytest.raises(RuntimeContextError, match="context_review_superseded"):
        await task
    assert (await store.runtime_checkpoint_read("thread-A")).record.fence == current


async def test_exact_scope_captures_its_commit_not_latest_sid(attached):
    state, coordinator, attachment, owner, store = attached
    async with scope(state, attachment, owner) as receipt:
        assert receipt.committed_fence is None
        async with coordinator.command_scope("thread-A"):
            coordinator.history["thread-A"] = [{"role": "user", "content": "first"}]
        with pytest.raises(RuntimeContextError, match="nested_context_command"):
            async with coordinator.command_scope("thread-A"):
                pass
    first = receipt.committed_fence
    assert first == (await store.runtime_checkpoint_read("thread-A")).record.fence
    async with scope(state, attachment, owner) as next_receipt:
        coordinator.history["thread-A"].append({"role": "assistant", "content": "second"})
    assert next_receipt.committed_fence != first
    assert receipt.committed_fence == first


@pytest.mark.parametrize("mutation", ["owner", "detach", "refusal", "store", "coordinator"])
async def test_admission_rechecks_after_waiting_for_exact_lock(attached, mutation):
    state, coordinator, attachment, owner, store = attached
    lock = coordinator.lock_for("thread-A")
    entered = False
    async def run():
        nonlocal entered
        async with scope(state, attachment, owner):
            entered = True
    await lock.acquire()
    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    detach = None
    if mutation == "owner":
        state.sessions["thread-A"] = object()
    elif mutation == "detach":
        detach = asyncio.create_task(coordinator.detach(attachment.token))
        await asyncio.sleep(0)
        assert attachment.token not in coordinator._attachments
    elif mutation == "refusal":
        coordinator._attachment_refusals[attachment.token] = RuntimeContextReadiness(
            "thread-A", ContextReadinessState.CONFLICT, True)
    elif mutation == "store":
        state.memory = object()
    else:
        state.orchestrator._context_checkpoints = object()
    lock.release()
    with pytest.raises(RuntimeContextError):
        await task
    if detach is not None:
        await detach
    assert not entered
    assert (await store.runtime_checkpoint_read("thread-A")).record.fence.revision == 1


@pytest.mark.parametrize("operation", ["runtime_checkpoint_read", "runtime_checkpoint_begin", "runtime_checkpoint_commit"])
@pytest.mark.parametrize("mutation", ["owner", "store", "refusal"])
async def test_await_completion_cannot_certify_replaced_attachment(attached, monkeypatch, operation, mutation):
    state, coordinator, attachment, owner, store = attached
    original = getattr(store, operation)
    suspended, release = asyncio.Event(), asyncio.Event()
    async def held(*args, **kwargs):
        result = await original(*args, **kwargs)
        suspended.set()
        await release.wait()
        return result
    monkeypatch.setattr(store, operation, held)
    captured = []
    async def run():
        async with scope(state, attachment, owner) as receipt:
            captured.append(receipt)
            coordinator.history["thread-A"] = [{"role": "user", "content": "fixture"}]
    task = asyncio.create_task(run())
    await asyncio.wait_for(suspended.wait(), 2)
    if mutation == "owner":
        state.sessions["thread-A"] = object()
    elif mutation == "store":
        coordinator.store = object()
    else:
        coordinator._attachment_refusals[attachment.token] = RuntimeContextReadiness(
            "thread-A", ContextReadinessState.UNAVAILABLE, True)
    release.set()
    with pytest.raises(RuntimeContextError):
        await task
    assert all(receipt._committed is None for receipt in captured)
    assert not coordinator.has_writers("thread-A")


async def test_child_task_cannot_inherit_receipt_or_command_handoff(attached):
    state, coordinator, attachment, owner, _ = attached
    async with scope(state, attachment, owner) as receipt:
        async def steal_receipt():
            return receipt.committed_fence
        with pytest.raises(RuntimeContextError, match="scope_owner_invalid"):
            await asyncio.create_task(steal_receipt())
        entered = asyncio.Event()
        async def command():
            async with coordinator.command_scope("thread-A"):
                entered.set()
        child = asyncio.create_task(command())
        await asyncio.sleep(0)
        assert not entered.is_set()
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await child


@pytest.mark.parametrize("failure", ["exception", "cancel", "false_commit"])
async def test_failed_scope_never_exposes_certified_fence(attached, monkeypatch, failure):
    state, _coordinator, attachment, owner, store = attached
    from memory.runtime_session_checkpoint import CheckpointResult
    original = store.runtime_checkpoint_commit
    async def commit(*args, **kwargs):
        if failure == "exception":
            raise OSError("synthetic disk failure")
        if failure == "cancel":
            raise asyncio.CancelledError()
        return CheckpointResult(CheckpointStatus.CONFLICT)
    monkeypatch.setattr(store, "runtime_checkpoint_commit", commit)
    receipt = None
    with pytest.raises((RuntimeContextError, asyncio.CancelledError)):
        async with scope(state, attachment, owner) as receipt:
            pass
    assert receipt._committed is None
    monkeypatch.setattr(store, "runtime_checkpoint_commit", original)
    assert (await store.runtime_checkpoint_read("thread-A")).status == CheckpointStatus.IN_PROGRESS


async def test_exception_and_cancellation_in_preparation_leave_uncertified(attached):
    state, _coordinator, attachment, owner, store = attached
    receipt = None
    with pytest.raises(RuntimeError):
        async with scope(state, attachment, owner) as receipt:
            raise RuntimeError("synthetic preparation failure")
    assert receipt._committed is None
    assert (await store.runtime_checkpoint_read("thread-A")).status == CheckpointStatus.IN_PROGRESS


@pytest.mark.parametrize("legacy_passthrough", [True, False])
@pytest.mark.parametrize("retained_cache", [True, False])
async def test_managed_attachment_cannot_recreate_disappeared_checkpoint(attached, legacy_passthrough, retained_cache):
    state, coordinator, attachment, owner, store = attached
    coordinator.legacy_passthrough = legacy_passthrough
    if not retained_cache:
        # Deliberate volatile-cache fault injection, not normal cache eviction.
        coordinator._fences.pop("thread-A")
    connection = await store._conn()
    try:
        await connection.execute("DELETE FROM runtime_session_checkpoints WHERE session_id = ?", ("thread-A",))
        await connection.commit()
    finally:
        await store._release(connection)
    assert (await store.runtime_checkpoint_read("thread-A")).status == CheckpointStatus.ABSENT
    with pytest.raises(RuntimeContextError, match="unmanaged_runtime_context|context_unavailable"):
        async with scope(state, attachment, owner):
            pytest.fail("Missing managed state must not become a new empty context")
    assert (await store.runtime_checkpoint_read("thread-A")).status == CheckpointStatus.ABSENT
