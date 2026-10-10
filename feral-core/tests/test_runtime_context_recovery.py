"""Explicit interrupted-context recovery using actual SQLite and WS dispatch."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agents.chat_turns import ChatTurnManager
from agents.runtime_context_checkpoint import RuntimeContextError
from memory.runtime_session_checkpoint import CheckpointFence, CheckpointLimits, CheckpointStatus as Status, encode_context
from memory.store import MemoryStore
from tests.test_chat_turn_abort import receive_type
from tests.test_runtime_context_ingress import capabilities, send_tracked
from tests.test_runtime_context_ingress import context_client as ingress_context_client
from tests.test_runtime_context_lifecycle import make_orch


@pytest.fixture
async def store(tmp_path):
    value = MemoryStore(db_path=str(tmp_path / "recovery.db"), conn_pool_size=2)
    try:
        yield value
    finally:
        await value.aclose()


context_client = ingress_context_client


async def interrupted(orch, store, sid="A"):
    attachment = await orch._context_checkpoints.attach(sid, checkpoint_version=1)
    async with orch._context_checkpoints.write_scope(sid):
        orch.conversation_history[sid] = [{"role": "user", "content": "committed fixture"},
                                          {"role": "assistant", "content": "committed reply"}]
    with pytest.raises(asyncio.CancelledError):
        async with orch._context_checkpoints.write_scope(sid):
            orch.conversation_history[sid].append({"role": "user", "content": "interrupted task must not replay"})
            raise asyncio.CancelledError
    pending = await store.runtime_checkpoint_read(sid)
    assert pending.status == Status.IN_PROGRESS
    return attachment, pending.record.fence


def terms(fence, *, acknowledge=True):
    value = {"contract_version": 1, "session_id": fence.session_id, "generation": fence.generation,
             "revision": fence.revision, "attempt_id": fence.attempt_id}
    if acknowledge:
        value["acknowledge_unknown_effects"] = True
    return value


def rpc(ws, method, params):
    request = str(uuid4())
    ws.send_json({"type": "req", "id": request, "method": method, "params": params})
    response = receive_type(ws, "res")
    assert response["id"] == request
    return response


async def test_recovery_rotates_fence_preserves_ui_and_late_writer_cannot_commit(store):
    orch = make_orch(store, [])
    attachment, pending = await interrupted(orch, store)
    ui = [{"role": "user", "content": "original interrupted UI transcript"}]
    await store.conversation_save("A", ui)
    ready = await orch._context_checkpoints.recover("A", pending, active_turn=lambda: False)
    assert ready.ready and ready.fence == store.runtime_checkpoint_recovery_fence(pending)
    assert ready.fence.generation != pending.generation and ready.fence.attempt_id != pending.attempt_id
    assert ready.fence.revision == pending.revision + 1
    rows = (await store.runtime_checkpoint_read("A")).record.context.history()
    assert rows[:2] == [{"role": "user", "content": "committed fixture"}, {"role": "assistant", "content": "committed reply"}]
    assert "outcomes are unknown" in rows[-1]["content"]
    assert "interrupted task must not replay" not in str(rows)
    assert (await store.conversation_get("A"))["messages"] == ui
    assert (await store.runtime_checkpoint_commit(pending, encode_context([], []))).status == Status.CONFLICT
    assert (await orch._context_checkpoints.readiness(attachment.token)).ready
    await orch.handle_command("A", "new explicit task")
    assert (await store.runtime_checkpoint_read("A")).status == Status.READY
    await orch.drain_background_tasks()


async def test_restart_and_lost_reply_status_are_readonly_and_never_replay(tmp_path):
    path = str(tmp_path / "restart.db")
    first = MemoryStore(db_path=path)
    orch = make_orch(first, [])
    _, pending = await interrupted(orch, first)
    await first.aclose()
    second = MemoryStore(db_path=path)
    captured = []
    reopened = make_orch(second, captured)
    try:
        attachment = await reopened._context_checkpoints.attach("A", checkpoint_version=1)
        assert not attachment.readiness.ready and attachment.readiness.recovery_fence == pending
        with pytest.raises(RuntimeContextError, match="checkpoint_in_progress"):
            await reopened.handle_command("A", "no automatic admission")
        status, _ = await reopened._context_checkpoints.recovery_status("A", pending)
        assert status == "not_recovered" and captured == []
        await reopened._context_checkpoints.recover("A", pending, active_turn=lambda: False)
        before = await second.runtime_checkpoint_read("A")
        for _ in range(2):
            status, ready = await reopened._context_checkpoints.recovery_status("A", pending)
            assert status == "recovered" and ready.ready
        assert await second.runtime_checkpoint_read("A") == before and captured == []
        with pytest.raises(RuntimeContextError, match="context_recovery_ready"):
            await reopened._context_checkpoints.recover("A", pending, active_turn=lambda: False)
        await reopened.handle_command("A", "explicit continuation")
        assert len(captured) == 1 and "committed fixture" in str(captured[0])
        assert "interrupted task must not replay" not in str(captured[0])
        status, ready = await reopened._context_checkpoints.recovery_status("A", pending)
        assert status == "superseded" and not ready.ready
        await reopened.drain_background_tasks()
    finally:
        await second.aclose()


@pytest.mark.parametrize("change", ["generation", "revision", "attempt_id", "session_id"])
async def test_foreign_and_stale_fences_refuse_without_mutation(store, change):
    orch = make_orch(store, [])
    _, pending = await interrupted(orch, store)
    other = replace(pending, **{change: pending.revision + 1 if change == "revision" else "foreign" if change == "session_id" else str(uuid4())})
    before = await store.runtime_checkpoint_read("A")
    with pytest.raises(RuntimeContextError, match="context_recovery_(session_mismatch|conflict)"):
        await orch._context_checkpoints.recover("A", other, active_turn=lambda: False)
    assert await store.runtime_checkpoint_read("A") == before


async def test_two_concurrent_recoveries_commit_exactly_one(store):
    orch = make_orch(store, [])
    _, pending = await interrupted(orch, store)
    results = await asyncio.gather(*(orch._context_checkpoints.recover("A", pending, active_turn=lambda: False) for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(result, RuntimeContextError) for result in results) == 1
    assert (await store.runtime_checkpoint_read("A")).record.fence.revision == pending.revision + 1


async def test_later_recovery_generation_cannot_claim_first_recovery_never_happened(store):
    orch = make_orch(store, [])
    coordinator = orch._context_checkpoints
    _, first_pending = await interrupted(orch, store)
    await coordinator.recover("A", first_pending, active_turn=lambda: False)
    with pytest.raises(asyncio.CancelledError):
        async with coordinator.write_scope("A"):
            raise asyncio.CancelledError
    second_pending = (await store.runtime_checkpoint_read("A")).record.fence
    assert second_pending.generation != first_pending.generation
    await coordinator.recover("A", second_pending, active_turn=lambda: False)
    before = await store.runtime_checkpoint_read("A")
    status, readiness = await coordinator.recovery_status("A", first_pending)
    assert status == "superseded" and not readiness.ready
    latest_status, latest_readiness = await coordinator.recovery_status("A", second_pending)
    assert latest_status == "recovered" and latest_readiness.ready
    assert await store.runtime_checkpoint_read("A") == before


async def test_cross_store_cas_has_one_winner_and_preserves_safe_context(tmp_path):
    path = str(tmp_path / "cross-store.db")
    first, second = MemoryStore(db_path=path), MemoryStore(db_path=path)
    try:
        orch = make_orch(first, [])
        _, pending = await interrupted(orch, first)
        results = await asyncio.gather(first.runtime_checkpoint_recover(pending), second.runtime_checkpoint_recover(pending))
        assert sorted(result.status.value for result in results) == ["applied", "ready"]
        ready = await second.runtime_checkpoint_read("A")
        assert ready.record.fence == first.runtime_checkpoint_recovery_fence(pending)
        assert ready.record.context.history()[0]["content"] == "committed fixture"
        assert (await second.runtime_checkpoint_begin("A", expected=pending, attempt_id=str(uuid4()))).status == Status.CONFLICT
    finally:
        await first.aclose()
        await second.aclose()


async def test_actual_sqlite_readback_mismatch_rolls_back_recovery(store, monkeypatch):
    from memory.runtime_session_checkpoint import CheckpointResult
    orch = make_orch(store, [])
    _, pending = await interrupted(orch, store)
    original = store._runtime_checkpoint_record

    def mismatched(row):
        result = original(row)
        if result.status == Status.READY:
            return CheckpointResult(Status.CORRUPT)
        return result

    monkeypatch.setattr(store, "_runtime_checkpoint_record", mismatched)
    assert (await store.runtime_checkpoint_recover(pending)).status == Status.CONFLICT
    assert (await store.runtime_checkpoint_read("A")).record.fence == pending


async def test_first_interrupted_turn_recovers_empty_context_without_ui_import(store):
    orch = make_orch(store, [])
    with pytest.raises(asyncio.CancelledError):
        async with orch._context_checkpoints.write_scope("A"):
            orch.conversation_history["A"] = [{"role": "user", "content": "unfinished"}]
            raise asyncio.CancelledError
    pending = (await store.runtime_checkpoint_read("A")).record.fence
    await store.conversation_save("A", [{"role": "system", "content": "UI AUTHORITY SENTINEL"}])
    await orch._context_checkpoints.recover("A", pending, active_turn=lambda: False)
    rows = (await store.runtime_checkpoint_read("A")).record.context.history()
    assert len(rows) == 1 and "outcomes are unknown" in rows[0]["content"]
    assert "unfinished" not in str(rows) and "UI AUTHORITY SENTINEL" not in str(rows)


async def test_restarted_old_generation_review_cannot_be_reused_after_recovery(tmp_path):
    path = str(tmp_path / "review-restart.db")
    first = MemoryStore(db_path=path)
    orch = make_orch(first, [])
    attachment = await orch._context_checkpoints.attach("A", checkpoint_version=1)
    old = orch.tool_runner.enforce_safety("browser__navigate", {"url": "https://fixture.invalid"}, session_id="A")
    pending = await first.runtime_checkpoint_begin("A", expected=attachment.readiness.fence, attempt_id=str(uuid4()))
    await first.aclose()
    second = MemoryStore(db_path=path)
    try:
        reopened = make_orch(second, [])
        # The baseline review store is volatile; injecting the captured real
        # record tests stale-record handling, not imaginary durable approval.
        reopened.tool_runner._pending_approvals[old["request_id"]] = old
        await reopened._context_checkpoints.attach("A", checkpoint_version=1)
        await reopened._context_checkpoints.recover("A", pending.record.fence, active_turn=lambda: False)
        reopened._execute_tool_call_for_llm = AsyncMock()
        result = await reopened.resolve_tool_approval_request(old["request_id"], approved=True, session_id="A")
        assert result["status"] == "stale_context"
        reopened._execute_tool_call_for_llm.assert_not_awaited()
        assert reopened.tool_runner.get_pending(old["request_id"]) == old
        fresh = reopened.tool_runner.enforce_safety(old["tool_name"], old["args"], session_id="A")
        assert fresh["request_id"] != old["request_id"]
        await reopened.drain_background_tasks()
    finally:
        await second.aclose()


@pytest.mark.parametrize("queued", [False, True])
async def test_active_and_queued_writers_refuse_recovery_before_sql_mutation(store, queued):
    orch = make_orch(store, [])
    coordinator = orch._context_checkpoints
    _, pending = await interrupted(orch, store)
    entered, release = asyncio.Event(), asyncio.Event()
    if queued:
        await coordinator.lock_for("A").acquire()

    async def writer():
        if queued:
            async with coordinator.write_scope("A"):
                pytest.fail("pending writer must not execute")
        else:
            async with coordinator._writer_registration("A"):
                entered.set()
                await release.wait()

    task = asyncio.create_task(writer())
    if queued:
        await asyncio.sleep(0)
    else:
        await entered.wait()
    try:
        assert coordinator.has_writers("A")
        with pytest.raises(RuntimeContextError, match="context_recovery_busy"):
            await coordinator.recover("A", pending, active_turn=lambda: False)
        assert (await store.runtime_checkpoint_read("A")).record.fence == pending
    finally:
        if queued:
            task.cancel()
            coordinator.lock_for("A").release()
        else:
            release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_writer_queued_during_read_is_rechecked_before_cas(store, monkeypatch):
    orch = make_orch(store, [])
    coordinator = orch._context_checkpoints
    _, pending = await interrupted(orch, store)
    read_entered, read_release = asyncio.Event(), asyncio.Event()
    original_read = store.runtime_checkpoint_read

    async def held_read(sid):
        read_entered.set()
        await read_release.wait()
        return await original_read(sid)

    monkeypatch.setattr(store, "runtime_checkpoint_read", held_read)
    recovering = asyncio.create_task(coordinator.recover("A", pending, active_turn=lambda: False))
    await read_entered.wait()

    async def queued():
        async with coordinator.write_scope("A"):
            pytest.fail("interrupted context cannot execute queued work")

    writer = asyncio.create_task(queued())
    await asyncio.sleep(0)
    read_release.set()
    with pytest.raises(RuntimeContextError, match="context_recovery_busy"):
        await recovering
    await asyncio.gather(writer, return_exceptions=True)
    assert (await original_read("A")).record.fence == pending


async def test_cancelled_recovery_surface_retains_settlement_and_readonly_status(store, monkeypatch):
    orch = make_orch(store, [])
    coordinator = orch._context_checkpoints
    _, pending = await interrupted(orch, store)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.runtime_checkpoint_recover

    async def held(fence):
        entered.set()
        await release.wait()
        return await original(fence)

    monkeypatch.setattr(store, "runtime_checkpoint_recover", held)
    surface = asyncio.create_task(coordinator.recover("A", pending, active_turn=lambda: False))
    await entered.wait()
    surface.cancel()
    with pytest.raises(asyncio.CancelledError):
        await surface
    assert len(coordinator._recoveries) == 1
    release.set()
    await asyncio.gather(*coordinator._recoveries)
    assert (await coordinator.recovery_status("A", pending))[0] == "recovered"
    assert not coordinator._recoveries


async def test_storage_failure_is_redacted_and_pending_unchanged(store, monkeypatch, caplog):
    orch = make_orch(store, [])
    _, pending = await interrupted(orch, store)
    monkeypatch.setattr(store, "runtime_checkpoint_recover", AsyncMock(side_effect=RuntimeError("PRIVATE_RECOVERY_SENTINEL")))
    with pytest.raises(RuntimeContextError, match="context_recovery_unavailable") as error:
        await orch._context_checkpoints.recover("A", pending, active_turn=lambda: False)
    assert "PRIVATE_RECOVERY_SENTINEL" not in str(error.value) + caplog.text
    assert (await store.runtime_checkpoint_read("A")).record.fence == pending


async def test_postcommit_publication_failure_can_reconcile_without_another_mutation(store, monkeypatch, caplog):
    orch = make_orch(store, [])
    coordinator = orch._context_checkpoints
    _, pending = await interrupted(orch, store)
    original = coordinator.clear_images
    monkeypatch.setattr(coordinator, "clear_images", lambda _: (_ for _ in ()).throw(RuntimeError("PRIVATE_PUBLICATION_SENTINEL")))
    with pytest.raises(RuntimeContextError, match="context_recovery_unavailable"):
        await coordinator.recover("A", pending, active_turn=lambda: False)
    assert "PRIVATE_PUBLICATION_SENTINEL" not in caplog.text
    committed = await store.runtime_checkpoint_read("A")
    assert committed.status == Status.READY
    assert coordinator._fences["A"] == pending  # Failure cannot admit work.
    monkeypatch.setattr(coordinator, "clear_images", original)
    status, ready = await coordinator.recovery_status("A", pending)
    assert status == "recovered" and ready.ready
    assert await store.runtime_checkpoint_read("A") == committed


async def test_gateway_owner_replacement_during_commit_cannot_acknowledge_orphaned_runtime(store, monkeypatch):
    from gateway.protocol import GatewayError, GatewaySession, MethodRegistry, register_core_methods
    orch = make_orch(store, [])
    attachment, pending = await interrupted(orch, store)
    coordinator = orch._context_checkpoints
    state = SimpleNamespace(memory=store, orchestrator=orch, chat_turns=None)
    registry = MethodRegistry()
    register_core_methods(registry, state)
    session = GatewaySession("A", SimpleNamespace(send_json=AsyncMock()), registry)
    session.metadata["runtime_context_readiness"] = lambda: coordinator.readiness(attachment.token)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.runtime_checkpoint_recover

    async def held(fence):
        entered.set()
        await release.wait()
        return await original(fence)

    monkeypatch.setattr(store, "runtime_checkpoint_recover", held)
    call = asyncio.create_task(registry.get("session.context.recover")("A", terms(pending), session))
    await entered.wait()
    state.orchestrator = make_orch(store, [])
    release.set()
    with pytest.raises(GatewayError) as error:
        await call
    assert error.value.code == "context_recovery_unavailable"
    assert error.value.details["action_outcome"] == "unknown"
    assert (await store.runtime_checkpoint_read("A")).status == Status.READY


@pytest.mark.parametrize("phase", ["lookup", "status"])
@pytest.mark.parametrize("replacement", ["orchestrator", "memory"])
async def test_gateway_owner_replacement_during_status_read_refuses_stale_receipt(store, tmp_path, monkeypatch, phase, replacement):
    from gateway.protocol import GatewayError, GatewaySession, MethodRegistry, register_core_methods
    orch = make_orch(store, [])
    attachment, pending = await interrupted(orch, store)
    coordinator = orch._context_checkpoints
    await coordinator.recover("A", pending, active_turn=lambda: False)
    before = await store.runtime_checkpoint_read("A")
    state = SimpleNamespace(memory=store, orchestrator=orch, chat_turns=None)
    registry = MethodRegistry()
    register_core_methods(registry, state)
    session = GatewaySession("A", SimpleNamespace(send_json=AsyncMock()), registry)
    session.metadata["runtime_context_readiness"] = lambda: coordinator.readiness(attachment.token)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.runtime_checkpoint_read
    calls = 0

    async def held(sid):
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "lookup" else 2):
            entered.set()
            await release.wait()
        return await original(sid)

    other = MemoryStore(db_path=str(tmp_path / "other-owner.db"))
    monkeypatch.setattr(store, "runtime_checkpoint_read", held)
    try:
        call = asyncio.create_task(registry.get("session.context.recoveryStatus")("A", terms(pending, acknowledge=False), session))
        await entered.wait()
        if replacement == "orchestrator":
            state.orchestrator = make_orch(other, [])
        else:
            state.memory = other
        release.set()
        with pytest.raises(GatewayError) as error:
            await call
        assert error.value.code == "context_recovery_unavailable"
        assert await original("A") == before
        assert (await other.runtime_checkpoint_read("A")).status == Status.ABSENT
    finally:
        await other.aclose()


@pytest.mark.parametrize("state", ["deleted", "corrupt", "unsupported", "absent", "ready", "quota"])
async def test_nonrecoverable_states_and_quota_do_not_reset(store, state):
    orch = make_orch(store, [])
    _, pending = await interrupted(orch, store)
    if state == "deleted":
        await store.runtime_checkpoint_delete(pending)
    elif state in {"corrupt", "unsupported", "absent"}:
        conn = await store._conn()
        try:
            if state == "absent":
                await conn.execute("DELETE FROM runtime_session_checkpoints WHERE session_id = 'A'")
            else:
                await conn.execute("UPDATE runtime_session_checkpoints SET format_version = ? WHERE session_id = 'A'", (2 if state == "unsupported" else 0,))
            await conn.commit()
        finally:
            await store._release(conn)
    elif state == "ready":
        await store.runtime_checkpoint_commit(pending, encode_context([], []))
    elif state == "quota":
        store._runtime_checkpoint_limits = CheckpointLimits(record_bytes=350)
    before = await store.runtime_checkpoint_read("A")
    with pytest.raises(RuntimeContextError, match=f"context_recovery_{state}"):
        await orch._context_checkpoints.recover("A", pending, active_turn=lambda: False)
    assert await store.runtime_checkpoint_read("A") == before


async def test_reviews_survive_inspection_but_old_generation_cannot_execute(store):
    orch = make_orch(store, [])
    coordinator = orch._context_checkpoints
    attachment = await coordinator.attach("A", checkpoint_version=1)
    old = orch.tool_runner.enforce_safety("browser__navigate", {"url": "https://fixture.invalid"}, session_id="A")
    assert old["context_generation"] == attachment.readiness.fence.generation
    pending = await store.runtime_checkpoint_begin("A", expected=attachment.readiness.fence, attempt_id=str(uuid4()))
    coordinator._fences["A"] = pending.record.fence
    await coordinator.recover("A", pending.record.fence, active_turn=lambda: False)
    orch._execute_tool_call_for_llm = AsyncMock()
    result = await orch.resolve_tool_approval_request(old["request_id"], approved=True, session_id="A")
    assert result["status"] == "stale_context"
    orch._execute_tool_call_for_llm.assert_not_awaited()
    assert orch.tool_runner.get_pending(old["request_id"]) == old
    assert orch.tool_runner.approve_pending(old["request_id"], session_id="A") is None
    fresh = orch.tool_runner.enforce_safety("browser__navigate", old["args"], session_id="A")
    assert fresh["request_id"] != old["request_id"] and fresh["context_generation"] != old["context_generation"]
    assert orch.tool_runner.deny_pending(old["request_id"], session_id="foreign") is None
    assert orch.tool_runner.deny_pending(old["request_id"], session_id="A")["status"] == "PermissionOutcome::Deny"
    await orch.drain_background_tasks()


async def test_unstamped_managed_review_fails_closed_but_legacy_is_unchanged(store):
    orch = make_orch(store, [])
    old = orch.tool_runner.enforce_safety("browser__navigate", {"url": "https://fixture.invalid"}, session_id="A")
    legacy = orch.tool_runner.enforce_safety("browser__navigate", {"url": "https://fixture.invalid"}, session_id="legacy")
    assert "context_generation" not in old
    await orch._context_checkpoints.attach("A", checkpoint_version=1)
    assert orch.tool_runner.get_pending(old["request_id"]) == old
    assert orch.tool_runner.approve_pending(old["request_id"], session_id="A") is None
    assert orch.tool_runner.approve_pending(legacy["request_id"], session_id="legacy") is not None


def test_actual_stop_recovery_capabilities_and_new_turn_without_replay(context_client):
    client, state, orch, store, captured = context_client
    original = orch.llm.chat_stream
    with client.websocket_connect("/v1/session?session_id=A&context_checkpoint_version=1") as ws:
        send_tracked(ws, "committed first task")
        receive_type(ws, "chat_turn_accepted")
        receive_type(ws, "chat_turn_terminal")

        async def blocked(messages, **kwargs):
            captured.append(messages)
            await asyncio.Event().wait()
            yield {"type": "done"}

        orch.llm.chat_stream = blocked
        request = send_tracked(ws, "interrupted task")
        accepted = receive_type(ws, "chat_turn_accepted")["payload"]
        receive_type(ws, "brain_event")
        assert rpc(ws, "chat.abort", {"request_id": request, "turn_id": accepted["turn_id"]})["payload"]["cancel_requested"]
        assert receive_type(ws, "chat_turn_terminal")["payload"]["processing_outcome"] == "cancelled"
        cap = capabilities(ws)
        assert cap["context_recovery_versions"] == [1]
        old = cap["context_recovery"]
        fence = CheckpointFence("A", old["generation"], old["revision"], old["attempt_id"])
        response = rpc(ws, "session.context.recover", terms(fence))
        assert response["ok"] and response["payload"]["action_outcome"] == "unknown"
        assert response["payload"]["replayed"] is False
        saved = response["payload"]["context_checkpoint"]
        assert saved["generation"] != old["generation"] and saved["revision"] == old["revision"] + 1
        assert capabilities(ws)["context_checkpoint"] == {key: value for key, value in saved.items() if key != "attempt_id"}
        before = client.portal.call(store.runtime_checkpoint_read, "A")
        status = rpc(ws, "session.context.recoveryStatus", terms(fence, acknowledge=False))["payload"]
        assert status["status"] == "recovered" and status["recovered"] is True and status["context_ready"] is True
        assert client.portal.call(store.runtime_checkpoint_read, "A") == before
        assert len(captured) == 2
        orch.llm.chat_stream = original
        send_tracked(ws, "new explicit task")
        receive_type(ws, "chat_turn_accepted")
        assert receive_type(ws, "chat_turn_terminal")["payload"]["processing_outcome"] == "completed"
        assert len(captured) == 3
        assert "committed first task" in str(captured[-1]) and "interrupted task" not in str(captured[-1])


@pytest.mark.parametrize("change", [{"session_id": "foreign"}, {"revision": True}, {"revision": 1.0},
    {"contract_version": True}, {"acknowledge_unknown_effects": False}, {"generation": "bad"}, {"extra": "private input"}])
def test_actual_gateway_invalid_terms_fail_before_recovery(context_client, change):
    client, state, orch, store, _ = context_client
    client.portal.call(interrupted, orch, store)
    with client.websocket_connect("/v1/session?session_id=A&context_checkpoint_version=1") as ws:
        pending = client.portal.call(store.runtime_checkpoint_read, "A")
        response = rpc(ws, "session.context.recover", {**terms(pending.record.fence), **change})
        assert response["ok"] is False and response["error"]["code"] in {"context_recovery_invalid", "context_recovery_session_mismatch"}
        assert "private input" not in str(response)
        assert client.portal.call(store.runtime_checkpoint_read, "A") == pending


def test_actual_gateway_refuses_live_turn_even_without_a_context_writer(context_client):
    client, state, orch, store, _ = context_client
    client.portal.call(interrupted, orch, store)
    entered, release = asyncio.Event(), asyncio.Event()
    state.chat_turns = ChatTurnManager(state)

    async def run():
        entered.set()
        await release.wait()
        return "fixture"

    async def submit():
        await state.chat_turns.submit(owner=object(), session_id="A", request_id=str(uuid4()), terms={"text": "queued fixture"}, run=run, emit=AsyncMock())
        await entered.wait()

    client.portal.call(submit)
    try:
        with client.websocket_connect("/v1/session?session_id=A&context_checkpoint_version=1") as ws:
            pending = client.portal.call(store.runtime_checkpoint_read, "A")
            response = rpc(ws, "session.context.recover", terms(pending.record.fence))
            assert not response["ok"] and response["error"]["code"] == "context_recovery_busy"
            assert client.portal.call(store.runtime_checkpoint_read, "A") == pending
    finally:
        async def finish():
            release.set()
            await asyncio.gather(*(live.task for live in state.chat_turns._live.values()))
        client.portal.call(finish)


def test_unauthenticated_remote_cannot_recover_before_attachment(context_client, monkeypatch):
    import api.server as server
    from starlette.websockets import WebSocketDisconnect
    client, state, orch, store, captured = context_client
    client.portal.call(interrupted, orch, store)
    pending = client.portal.call(store.runtime_checkpoint_read, "A")
    monkeypatch.setattr(server, "is_localhost", lambda _: False)
    monkeypatch.setattr(server._session_auth_module, "transport_is_trusted", lambda _: False)
    monkeypatch.setattr(server, "verify_session", lambda _: False)
    with client.websocket_connect("/v1/session?session_id=A&context_checkpoint_version=1") as ws:
        ws.send_json({"type": "req", "id": str(uuid4()), "method": "session.context.recover", "params": terms(pending.record.fence)})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4001
    assert client.portal.call(store.runtime_checkpoint_read, "A") == pending and captured == []


async def test_tracked_queued_work_remains_active_until_receipt_settles(store):
    manager = ChatTurnManager(SimpleNamespace(memory=store))
    entered, release = asyncio.Event(), asyncio.Event()

    async def runner():
        entered.set()
        await release.wait()
        return "fixture"

    accepted = await manager.submit(owner=object(), session_id="A", request_id=str(uuid4()), terms={"text": "fixture"}, run=runner, emit=AsyncMock())
    assert manager.has_active_session("A") and not manager.has_active_session("foreign")
    await entered.wait()
    release.set()
    await asyncio.gather(*(live.task for live in manager._live.values()))
    assert not manager.has_active_session("A")
    assert (await manager.status(session_id="A", turn_id=accepted["turn_id"]))["processing_outcome"] == "completed"
