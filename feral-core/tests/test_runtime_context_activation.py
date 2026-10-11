"""Activation-order regressions before production attachment integration.

These cases exercise the existing coordinator and SQLite boundary. They do not
claim that production sockets negotiate or activate durable context yet.
"""
from uuid import uuid4
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.runtime_context_checkpoint import (
    ContextReadinessState, RuntimeContextAttachmentToken, RuntimeContextCoordinator,
    RuntimeContextError, legacy_context_mutation,
)
from memory.runtime_session_checkpoint import CheckpointLimits, CheckpointStatus
from memory.store import MemoryStore
from tests.test_runtime_context_lifecycle import make_orch


@pytest.fixture
async def activation_store(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "activation.db"), conn_pool_size=2)
    try:
        yield store
    finally:
        await store.aclose()


async def test_presaved_request_marker_is_not_trusted_empty_context(activation_store):
    """Reproduce why attachment must initialize before native presend save."""
    sid = "fresh-native-thread"
    request_id = str(uuid4())
    await activation_store.conversation_save(sid, [{
        "role": "user", "content": "fixture draft", "request_id": request_id,
    }])
    captured = []
    orch = make_orch(activation_store, captured)
    try:
        with pytest.raises(RuntimeContextError, match="legacy_context_unavailable"):
            await orch.handle_command(sid, "fixture draft")
        assert captured == []
        assert (await activation_store.runtime_checkpoint_read(sid)).status == CheckpointStatus.ABSENT
        assert not orch.conversation_history.get(sid)
    finally:
        await orch.drain_background_tasks()


async def test_ready_read_is_read_only_and_never_imports_ui(activation_store):
    sid = "checkpoint-thread"
    captured = []
    orch = make_orch(activation_store, captured)
    try:
        await orch.handle_command(sid, "trusted fixture prompt")
        before = await activation_store.runtime_checkpoint_read(sid)
        await activation_store.conversation_save(sid, [{
            "role": "system", "content": "untrusted UI fixture must never become policy",
        }])
        after = await activation_store.runtime_checkpoint_read(sid)
        assert after == before
        assert after.status == CheckpointStatus.READY
        assert after.record.context.history()[0]["content"] == "trusted fixture prompt"
        assert all("untrusted UI" not in str(row) for row in after.record.context.history())
        assert len(captured) == 1
    finally:
        await orch.drain_background_tasks()


def isolated_coordinator(store, *, limits=None):
    locks, history, images_cleared = {}, {}, []
    coordinator = RuntimeContextCoordinator(
        store, history=history, lock_for=lambda sid: locks.setdefault(sid, asyncio.Lock()),
        image_call_ids=lambda sid: frozenset(), clear_images=images_cleared.append,
        legacy_passthrough=True,
    )
    return coordinator, locks, history, images_cleared


async def test_atomic_empty_attach_precedes_ui_presave_and_restart_provider(tmp_path):
    path = str(tmp_path / "restart.db")
    store = MemoryStore(db_path=path)
    first = make_orch(store, [])
    first._context_checkpoints.legacy_passthrough = True
    try:
        attachment = await first._context_checkpoints.attach("A", checkpoint_version=1)
        assert attachment.readiness.ready and attachment.readiness.initialized
        empty = await store.runtime_checkpoint_read("A")
        assert empty.status == CheckpointStatus.READY and empty.record.context.history() == []
        await store.conversation_save("A", [{"role": "user", "content": "first", "request_id": str(uuid4())}])
        await first.handle_command("A", "first")
        assert await first._context_checkpoints.detach(attachment.token)
        assert "A" not in first.conversation_history
        await first.drain_background_tasks()
    finally:
        await store.aclose()
    reopened = MemoryStore(db_path=path)
    captured = []
    second = make_orch(reopened, captured)
    second._context_checkpoints.legacy_passthrough = True
    try:
        attachment = await second._context_checkpoints.attach("A")  # Known SID cannot opt out.
        assert attachment.readiness.ready and attachment.readiness.managed
        assert not attachment.readiness.initialized
        await second.handle_command("A", "second")
        assert any(row.get("content") == "first" for row in captured[0])
        assert any(row.get("content") == "second" for row in captured[0])
        await second._context_checkpoints.detach(attachment.token)
        await second.drain_background_tasks()
    finally:
        await reopened.aclose()


async def test_legacy_unknown_command_remains_live_without_checkpoint(activation_store):
    captured = []
    orch = make_orch(activation_store, captured)
    orch._context_checkpoints.legacy_passthrough = True
    attachment = await orch._context_checkpoints.attach("legacy")
    try:
        assert attachment.readiness.state == ContextReadinessState.LEGACY
        assert not attachment.readiness.ready
        await orch.handle_command("legacy", "legacy fixture")
        assert len(captured) == 1
        assert (await activation_store.runtime_checkpoint_read("legacy")).status == CheckpointStatus.ABSENT
        assert not await orch._context_checkpoints.detach(attachment.token)
        assert orch.conversation_history["legacy"]
    finally:
        await orch.drain_background_tasks()


async def test_nonempty_legacy_ui_never_initialized(activation_store):
    await activation_store.conversation_save("old", [{"role": "user", "content": "legacy fixture"}])
    coordinator, _, history, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("old", checkpoint_version=1)
    assert attachment.readiness.state == ContextReadinessState.LEGACY_UNAVAILABLE
    repeated = await coordinator.readiness(attachment.token)
    assert repeated.state == ContextReadinessState.LEGACY_UNAVAILABLE and repeated.managed
    assert not repeated.ready and repeated.fence is None
    assert attachment.readiness.fence is None and history == {}
    assert (await activation_store.runtime_checkpoint_read("old")).status == CheckpointStatus.ABSENT
    await coordinator.detach(attachment.token)


@pytest.mark.parametrize("state", ["in_progress", "deleted", "corrupt", "unsupported"])
async def test_existing_unready_records_fail_closed_without_empty_overwrite(activation_store, state):
    created = await activation_store.runtime_checkpoint_initialize_empty("A")
    conn = await activation_store._conn()
    try:
        if state == "in_progress":
            await activation_store.runtime_checkpoint_begin("A", expected=created.record.fence, attempt_id=str(uuid4()))
        elif state == "deleted":
            await activation_store.runtime_checkpoint_delete(created.record.fence)
        elif state == "corrupt":
            await conn.execute("UPDATE runtime_session_checkpoints SET payload_json='bad', payload_bytes=3 WHERE session_id='A'")
            await conn.commit()
        else:
            await conn.execute("UPDATE runtime_session_checkpoints SET format_version=999 WHERE session_id='A'")
            await conn.commit()
    finally:
        await activation_store._release(conn)
    before = await activation_store.runtime_checkpoint_read("A")
    coordinator, _, history, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("A", checkpoint_version=1)
    assert attachment.readiness.state.value == state
    assert attachment.readiness.managed and not attachment.readiness.ready
    assert attachment.readiness.fence is None and history == {}
    assert await activation_store.runtime_checkpoint_read("A") == before
    with pytest.raises(RuntimeContextError):
        async with coordinator.write_scope("A"):
            pytest.fail("unready writer entered")
    await coordinator.detach(attachment.token)


async def test_unavailable_attachment_is_sticky_and_no_legacy_failure_downgrade(activation_store, monkeypatch):
    coordinator, _, _, _ = isolated_coordinator(activation_store)
    original = activation_store.runtime_checkpoint_read
    monkeypatch.setattr(activation_store, "runtime_checkpoint_read", AsyncMock(side_effect=RuntimeError("fixture secret")))
    attachment = await coordinator.attach("A")
    assert attachment.readiness.managed and attachment.readiness.state == ContextReadinessState.UNAVAILABLE
    monkeypatch.setattr(activation_store, "runtime_checkpoint_read", original)
    later = await coordinator.readiness(attachment.token)
    assert later.managed and later.state == ContextReadinessState.UNAVAILABLE and later.fence is None
    assert not await coordinator.detach(attachment.token)


async def test_cancellation_while_waiting_cleans_exact_reservation_no_pending(activation_store):
    coordinator, _, _, _ = isolated_coordinator(activation_store)
    lock = coordinator.lock_for("A")
    await lock.acquire()
    task = asyncio.create_task(coordinator.attach("A", checkpoint_version=1))
    await asyncio.sleep(0)
    assert coordinator.has_attachments("A")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator.has_attachments("A")
    lock.release()
    assert (await activation_store.runtime_checkpoint_read("A")).status == CheckpointStatus.ABSENT


async def test_cancel_after_empty_commit_leaves_ready_not_pending(activation_store, monkeypatch):
    coordinator, _, _, _ = isolated_coordinator(activation_store)
    original = activation_store.runtime_checkpoint_initialize_empty
    committed = asyncio.Event()
    async def held(sid):
        result = await original(sid)
        committed.set()
        await asyncio.Event().wait()
        return result
    monkeypatch.setattr(activation_store, "runtime_checkpoint_initialize_empty", held)
    task = asyncio.create_task(coordinator.attach("A", checkpoint_version=1))
    await asyncio.wait_for(committed.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator.has_attachments("A")
    assert (await activation_store.runtime_checkpoint_read("A")).status == CheckpointStatus.READY
    monkeypatch.setattr(activation_store, "runtime_checkpoint_initialize_empty", original)
    attachment = await coordinator.attach("A", checkpoint_version=1)
    assert attachment.readiness.ready
    await coordinator.detach(attachment.token)


async def test_exact_tokens_and_new_attachment_fence_queued_cleanup(activation_store):
    coordinator, locks, history, cleared = isolated_coordinator(activation_store)
    old = await coordinator.attach("A", checkpoint_version=1)
    history["A"] = [{"role": "assistant", "content": "live fixture"}]
    forged = RuntimeContextAttachmentToken("A")
    assert not await coordinator.detach(forged)
    with pytest.raises(RuntimeContextError, match="context_attachment_invalid"):
        await coordinator.readiness(forged)
    await locks["A"].acquire()
    cleanup = asyncio.create_task(coordinator.detach(old.token))
    await asyncio.sleep(0)
    new_task = asyncio.create_task(coordinator.attach("A", checkpoint_version=1))
    await asyncio.sleep(0)
    before = len(cleared)
    locks["A"].release()
    assert not await cleanup
    new = await new_task
    assert new.readiness.ready and history["A"][0]["content"] == "live fixture"
    assert len(cleared) == before
    assert await coordinator.detach(new.token)
    assert "A" not in history
    assert not await coordinator.detach(new.token)


async def test_live_attachment_limits_cancelled_waiter_releases_slot(activation_store):
    coordinator, _, _, _ = isolated_coordinator(activation_store)
    tokens = [(await coordinator.attach("same")).token for _ in range(8)]
    with pytest.raises(RuntimeContextError, match="context_attachment_quota"):
        await coordinator.attach("same")
    for index in range(120):
        tokens.append((await coordinator.attach(f"legacy-{index}")).token)
    with pytest.raises(RuntimeContextError, match="context_attachment_quota"):
        await coordinator.attach("new")
    await coordinator.detach(tokens.pop())
    fresh = await coordinator.attach("new")
    tokens.append(fresh.token)
    for token in tokens:
        await coordinator.detach(token)
    assert not coordinator._attachments


async def test_readiness_is_immutable_read_only_and_rechecks_current_record(activation_store):
    coordinator, _, _, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("A", checkpoint_version=1)
    before = await activation_store.runtime_checkpoint_read("A")
    readiness = await coordinator.readiness(attachment.token)
    assert readiness.ready and readiness.fence == before.record.fence
    with pytest.raises(TypeError):
        readiness.omissions["images"] = 50
    assert await activation_store.runtime_checkpoint_read("A") == before
    pending = await activation_store.runtime_checkpoint_begin("A", expected=before.record.fence, attempt_id=str(uuid4()))
    later = await coordinator.readiness(attachment.token)
    assert not later.ready and later.state == ContextReadinessState.IN_PROGRESS and later.fence is None
    assert await activation_store.runtime_checkpoint_read("A") == pending.__class__(CheckpointStatus.IN_PROGRESS, pending.record)
    await coordinator.detach(attachment.token)


@pytest.mark.parametrize("limits", [CheckpointLimits(record_bytes=1), CheckpointLimits(total_bytes=1)])
async def test_empty_initializer_quota_returns_typed_no_pending(tmp_path, limits):
    store = MemoryStore(db_path=str(tmp_path / "quota.db"), runtime_checkpoint_limits=limits)
    try:
        result = await store.runtime_checkpoint_initialize_empty("A")
        assert result.status == CheckpointStatus.QUOTA
        assert (await store.runtime_checkpoint_read("A")).status == CheckpointStatus.ABSENT
    finally:
        await store.aclose()


async def test_managed_mutators_refuse_before_body_and_lock_legacy(activation_store):
    coordinator, locks, _, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("A", checkpoint_version=1)
    holder = SimpleNamespace(_context_checkpoints=coordinator)
    for operation in ("reset", "compact", "snapshot", "branch", "restore", "delete", "voice", "handoff"):
        with pytest.raises(RuntimeContextError, match=f"managed_{operation}_unsupported"):
            async with legacy_context_mutation(holder, activation_store, ("legacy", "A"), operation):
                pytest.fail("managed mutation entered")
    async with legacy_context_mutation(holder, activation_store, ("legacy",), "reset"):
        assert locks["legacy"].locked()
    await coordinator.detach(attachment.token)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_real_realtime_tool_entry_refuses_before_executor(activation_store, provider):
    from voice.realtime_proxy import RealtimeProxy
    from voice.gemini_realtime import GeminiRealtimeProxy
    cls = RealtimeProxy if provider == "openai" else GeminiRealtimeProxy
    coordinator, _, _, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("A", checkpoint_version=1)
    proxy = cls.__new__(cls)
    proxy._memory = activation_store
    proxy._orchestrator = SimpleNamespace(_context_checkpoints=coordinator)
    proxy._checkpoint_legacy_handle_tool_call = AsyncMock(return_value="must not execute")
    result = json.loads(await proxy._handle_tool_call("A", "fixture-call", "fixture_write", "{}"))
    assert result["success"] is False and result["code"] == "managed_voice_unsupported"
    proxy._checkpoint_legacy_handle_tool_call.assert_not_awaited()
    assert (await activation_store.runtime_checkpoint_read("A")).status == CheckpointStatus.READY
    await coordinator.detach(attachment.token)


async def test_handoff_managed_target_and_pending_source_are_refused(activation_store):
    from agents.session_handoff import SessionHandoffManager
    coordinator, _, _, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("target", checkpoint_version=1)
    activation_store.working_push("source", {"role": "user", "text": "source fixture"})
    notify = AsyncMock()
    manager = SessionHandoffManager(memory=activation_store, orchestrator=SimpleNamespace(_context_checkpoints=coordinator),
                                    sessions={"source": object(), "target": object()}, send_to_session=notify)
    result = await manager._apply_transfer("source", "target", "desktop", 20)
    assert not result["success"] and result["code"] == "managed_handoff_unsupported"
    assert activation_store.working_get("target") == []
    notify.assert_not_awaited()
    result = await manager.handoff("target", "glasses")
    assert not result["success"] and not result["pending"] and not manager._pending
    await coordinator.detach(attachment.token)


async def test_empty_initialization_parallel_store_cas_and_row_quota(tmp_path):
    path = str(tmp_path / "parallel.db")
    first = MemoryStore(db_path=path, runtime_checkpoint_limits=CheckpointLimits(sessions=1))
    second = MemoryStore(db_path=path, runtime_checkpoint_limits=CheckpointLimits(sessions=1))
    try:
        results = await asyncio.gather(first.runtime_checkpoint_initialize_empty("A"), second.runtime_checkpoint_initialize_empty("A"))
        assert {result.status for result in results} == {CheckpointStatus.APPLIED, CheckpointStatus.READY}
        assert results[0].record.fence == results[1].record.fence
        assert (await second.runtime_checkpoint_initialize_empty("B")).status == CheckpointStatus.QUOTA
        assert (await second.runtime_checkpoint_read("B")).status == CheckpointStatus.ABSENT
    finally:
        await first.aclose()
        await second.aclose()


async def test_legacy_cleanup_explicit_only_rechecks_new_attachment_after_learner(activation_store):
    coordinator, _, history, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("legacy")
    history["legacy"] = [{"role": "user", "content": "keep fixture"}]
    learning, release = asyncio.Event(), asyncio.Event()
    async def learner(sid):
        learning.set()
        await release.wait()
    coordinator.finalize_legacy = learner
    cleanup = asyncio.create_task(coordinator.detach(attachment.token, clear_legacy=True))
    await asyncio.wait_for(learning.wait(), 2)
    attaching = asyncio.create_task(coordinator.attach("legacy"))
    await asyncio.sleep(0)
    release.set()
    assert not await cleanup
    new = await attaching
    assert history["legacy"]
    assert await coordinator.detach(new.token, clear_legacy=True)
    assert "legacy" not in history


async def test_gateway_ready_capability_is_read_only_and_serializes_no_attempt(activation_store):
    from gateway.protocol import GatewaySession, MethodRegistry, register_core_methods
    coordinator, _, _, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("A", checkpoint_version=1)
    state = SimpleNamespace(memory=activation_store, orchestrator=SimpleNamespace(_context_checkpoints=coordinator))
    registry = MethodRegistry()
    register_core_methods(registry, state)
    session = GatewaySession("A", SimpleNamespace(send_json=AsyncMock()), registry)
    session.metadata["tracked_chat_send"] = AsyncMock()
    session.metadata["runtime_context_readiness"] = lambda: coordinator.readiness(attachment.token)
    before = await activation_store.runtime_checkpoint_read("A")
    result = await registry.get("chat.capabilities")("A", {}, session)
    assert result["context_ready"] and result["context_checkpoint_versions"] == [1]
    assert result["context_managed"] is True
    assert result["context_checkpoint"]["session_id"] == "A"
    assert "attempt_id" not in json.dumps(result) and before.record.fence.attempt_id not in json.dumps(result)
    assert result["managed_unsupported_paths"]
    assert await activation_store.runtime_checkpoint_read("A") == before
    session.metadata["tracked_chat_send"].assert_not_awaited()
    for operation in ("reset", "compact", "snapshot"):
        with pytest.raises(Exception) as raised:
            await registry.get(f"session.{operation}")("A", {}, session)
        assert raised.value.code == f"managed_{operation}_unsupported"
    await coordinator.detach(attachment.token)


async def test_registered_http_mutators_refuse_known_managed_target(activation_store, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from api.routes import conversations, memory
    coordinator, _, history, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("managed", checkpoint_version=1)
    orch = SimpleNamespace(_context_checkpoints=coordinator, conversation_history=history, llm=None)
    fixture_state = SimpleNamespace(memory=activation_store, orchestrator=orch)
    monkeypatch.setattr(conversations, "state", fixture_state)
    monkeypatch.setattr(memory, "state", fixture_state)
    snapshot = await activation_store.snapshot_session(session_id="legacy", history=[{"role": "user", "content": "legacy fixture"}])
    await activation_store.conversation_save("managed", [{"role": "user", "content": "UI fixture"}])
    app = FastAPI()
    app.include_router(conversations.router)
    app.include_router(memory.router)
    requests = (
        ("DELETE", "/api/conversations/managed", None, "delete"),
        ("POST", "/api/session/snapshot", {"session_id": "managed"}, "snapshot"),
        ("POST", "/api/session/branch", {"snapshot_id": snapshot["snapshot_id"], "target_session_id": "managed"}, "branch"),
        ("POST", "/api/session/branch", {"session_id": "managed", "target_session_id": "new-target"}, "branch"),
        ("POST", "/api/session/restore", {"snapshot_id": snapshot["snapshot_id"], "target_session_id": "managed"}, "restore"),
        ("POST", "/api/memory/compact?session_id=managed", None, "compact"),
    )
    before = await activation_store.runtime_checkpoint_read("managed")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        for method, path, body, operation in requests:
            response = await client.request(method, path, json=body)
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == f"managed_{operation}_unsupported"
            assert await activation_store.runtime_checkpoint_read("managed") == before
            assert history["managed"] == []
    assert (await activation_store.conversation_get("managed"))["messages"][0]["content"] == "UI fixture"
    assert len(await activation_store.list_snapshots()) == 1
    await coordinator.detach(attachment.token)


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_actual_approval_effect_is_fenced_and_commit_failure_never_retry_safe(activation_store, monkeypatch, commit_fails):
    orch = make_orch(activation_store, [])
    orch._context_checkpoints.legacy_passthrough = True
    attachment = await orch._context_checkpoints.attach("A", checkpoint_version=1)
    orch.memory = activation_store
    orch._try_genui_for_result = AsyncMock()
    async def effect(*args, **kwargs):
        assert (await activation_store.runtime_checkpoint_read("A")).status == CheckpointStatus.IN_PROGRESS
        assert orch._get_session_lock("A").locked()
        return {"success": True, "data": {"note": "fixture effect"}}
    orch._execute_tool_call_for_llm = AsyncMock(side_effect=effect)
    pending = orch.tool_runner.enforce_safety("browser__navigate", {"url": "https://fixture.invalid"}, session_id="A")
    assert pending["status"] == "pending_approval"
    if commit_fails:
        monkeypatch.setattr(activation_store, "runtime_checkpoint_commit", AsyncMock(side_effect=RuntimeError("fixture commit uncertain")))
    try:
        if commit_fails:
            with pytest.raises(RuntimeContextError) as raised:
                await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="A")
            assert raised.value.effects_may_have_occurred and not raised.value.retry_safe
            assert (await activation_store.runtime_checkpoint_read("A")).status == CheckpointStatus.IN_PROGRESS
        else:
            result = await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="A")
            assert result["status"] == "approved"
            ready = await activation_store.runtime_checkpoint_read("A")
            assert ready.status == CheckpointStatus.READY
            assert any("fixture effect" in row["text"] for row in ready.record.context.working())
        orch._execute_tool_call_for_llm.assert_awaited_once()
    finally:
        await orch._context_checkpoints.detach(attachment.token)
        await orch.drain_background_tasks()


async def test_existing_pending_checkpoint_refuses_approval_before_grant_or_effect(activation_store):
    orch = make_orch(activation_store, [])
    attachment = await orch._context_checkpoints.attach("A", checkpoint_version=1)
    pending = orch.tool_runner.enforce_safety("browser__navigate", {"url": "https://fixture.invalid"}, session_id="A")
    orch._execute_tool_call_for_llm = AsyncMock()
    await activation_store.runtime_checkpoint_begin("A", expected=attachment.readiness.fence, attempt_id=str(uuid4()))
    with pytest.raises(RuntimeContextError, match="checkpoint_in_progress"):
        await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="A")
    orch._execute_tool_call_for_llm.assert_not_awaited()
    assert orch.tool_runner.get_pending(pending["request_id"]) is not None
    await orch._context_checkpoints.detach(attachment.token)
    await orch.drain_background_tasks()


@pytest.mark.parametrize("after_effect", [False, True])
async def test_registered_approval_api_reports_context_refusal_vs_unknown_effect(activation_store, monkeypatch, after_effect):
    import httpx
    from fastapi import FastAPI
    from api.routes import approvals
    orch = make_orch(activation_store, [])
    attachment = await orch._context_checkpoints.attach("A", checkpoint_version=1)
    orch._try_genui_for_result = AsyncMock()
    orch._execute_tool_call_for_llm = AsyncMock(return_value={"success": True, "data": {"note": "fixture effect"}})
    pending = orch.tool_runner.enforce_safety("browser__navigate", {"url": "https://fixture.invalid"}, session_id="A")
    if after_effect:
        monkeypatch.setattr(activation_store, "runtime_checkpoint_commit", AsyncMock(side_effect=RuntimeError("fixture private diagnostic")))
    else:
        await activation_store.runtime_checkpoint_begin("A", expected=attachment.readiness.fence, attempt_id=str(uuid4()))
    monkeypatch.setattr(approvals, "state", SimpleNamespace(orchestrator=orch))
    app = FastAPI()
    app.include_router(approvals.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            malformed = await client.post(f"/api/approvals/{pending['request_id']}/approve", json={"session_id": " A "})
            assert malformed.status_code == 422
            orch._execute_tool_call_for_llm.assert_not_awaited()
            response = await client.post(f"/api/approvals/{pending['request_id']}/approve", json={"session_id": "A"})
        assert response.status_code == (503 if after_effect else 409)
        detail = response.json()["detail"]
        assert detail["retry_safe"] is False and detail["effects_may_have_occurred"] is after_effect
        assert detail["processing_outcome"] == ("outcome_unknown" if after_effect else "unavailable")
        assert detail["action_outcome"] == ("unknown" if after_effect else "not_asserted")
        assert "fixture private diagnostic" not in response.text
        assert orch._execute_tool_call_for_llm.await_count == int(after_effect)
        assert (await activation_store.runtime_checkpoint_read("A")).status == CheckpointStatus.IN_PROGRESS
    finally:
        await orch._context_checkpoints.detach(attachment.token)
        await orch.drain_background_tasks()


async def test_atomic_initializer_ui_save_race_never_imports_request_marker(tmp_path):
    path = str(tmp_path / "ui-race.db")
    first, second = MemoryStore(db_path=path), MemoryStore(db_path=path)
    try:
        for index in range(10):
            sid = f"race-{index}"
            initialize = first.runtime_checkpoint_initialize_empty(sid)
            save = second.conversation_save(sid, [{"role": "user", "content": "untrusted marker", "request_id": str(uuid4())}])
            operations = [initialize, save] if index % 2 == 0 else [save, initialize]
            results = await asyncio.gather(*operations)
            result = results[0 if index % 2 == 0 else 1]
            assert result.status in {CheckpointStatus.APPLIED, CheckpointStatus.CONFLICT}
            read = await first.runtime_checkpoint_read(sid)
            if result.status == CheckpointStatus.APPLIED:
                assert read.status == CheckpointStatus.READY and read.record.context.history() == []
            else:
                assert read.status == CheckpointStatus.ABSENT
    finally:
        await first.aclose()
        await second.aclose()


async def test_gateway_tracked_context_refusal_typed_status_remains_available(activation_store):
    from gateway.protocol import GatewaySession, MethodRegistry, register_core_methods
    state = SimpleNamespace(memory=activation_store, orchestrator=None)
    registry = MethodRegistry()
    register_core_methods(registry, state)
    send = AsyncMock()
    session = GatewaySession("A", SimpleNamespace(send_json=send), registry)
    submit = AsyncMock(side_effect=RuntimeContextError("checkpoint_in_progress"))
    session.metadata["tracked_chat_send"] = submit
    request_id = str(uuid4())
    await session.handle_message({"type": "req", "id": request_id, "method": "chat.send",
                                  "params": {"text": "fixture", "turn_contract_version": 1}})
    error = send.await_args_list[-1].args[0]
    assert not error["ok"] and error["id"] == request_id
    assert error["error"]["code"] == "checkpoint_in_progress"
    assert error["error"]["details"]["retry_safe"] is False
    assert error["error"]["details"]["action_outcome"] == "not_asserted"
    await session.handle_message({"type": "req", "id": str(uuid4()), "method": "chat.status",
                                  "params": {"request_id": request_id}})
    status = send.await_args_list[-1].args[0]
    assert status["ok"] and not status["payload"]["found"]
    submit.assert_awaited_once()
    assert (await activation_store.runtime_checkpoint_read("A")).status == CheckpointStatus.ABSENT


async def test_legacy_queued_writer_prevents_eviction_during_unlocked_wakeup(activation_store):
    orch = make_orch(activation_store, [])
    coordinator = orch._context_checkpoints
    coordinator.legacy_passthrough = True
    orch._conversation_max_sessions = 1
    orch.conversation_history.update({"A": [{"role": "user", "content": "queued fixture"}], "B": []})
    orch._session_last_active.update({"A": 0.0, "B": 1.0})
    lock = coordinator.lock_for("A")
    await lock.acquire()
    async def writer():
        async with coordinator.write_scope("A"):
            assert orch.conversation_history["A"][0]["content"] == "queued fixture"
    task = asyncio.create_task(writer())
    await asyncio.sleep(0)
    assert coordinator.has_writers("A")
    lock.release()
    assert not lock.locked()  # Queued waiter has not resumed yet.
    orch._evict_stale_sessions()
    assert "A" in orch.conversation_history and orch._get_session_lock("A") is lock
    await task
    assert not coordinator.has_writers("A")
    await orch.drain_background_tasks()


async def test_legacy_cleanup_rechecks_writer_queued_during_learner(activation_store):
    coordinator, _, history, _ = isolated_coordinator(activation_store)
    attachment = await coordinator.attach("legacy")
    history["legacy"] = [{"role": "user", "content": "queued during learner"}]
    learning, release = asyncio.Event(), asyncio.Event()
    async def learner(sid):
        learning.set()
        await release.wait()
    coordinator.finalize_legacy = learner
    cleanup = asyncio.create_task(coordinator.detach(attachment.token, clear_legacy=True))
    await asyncio.wait_for(learning.wait(), 2)
    async def writer():
        async with coordinator.write_scope("legacy"):
            assert history["legacy"][0]["content"] == "queued during learner"
    task = asyncio.create_task(writer())
    await asyncio.sleep(0)
    assert coordinator.has_writers("legacy")
    release.set()
    assert not await cleanup
    await task
    assert history["legacy"] and not coordinator.has_writers("legacy")


async def test_managed_auto_compaction_never_calls_legacy_summary_writer(activation_store):
    orch = make_orch(activation_store, [])
    attachment = await orch._context_checkpoints.attach("A", checkpoint_version=1)
    orch.memory = SimpleNamespace(compact_session=AsyncMock())
    assert not orch._schedule_compaction("A", "fixture threshold")
    orch.memory.compact_session.assert_not_awaited()
    await orch._context_checkpoints.detach(attachment.token)
    await orch.drain_background_tasks()


@pytest.mark.parametrize("requested", [False, True])
async def test_gateway_refused_attachment_cannot_opt_out_via_legacy_send_or_mutation(activation_store, requested):
    from gateway.protocol import GatewaySession, MethodRegistry, register_core_methods
    coordinator, _, history, _ = isolated_coordinator(activation_store)
    coordinator.legacy_passthrough = True
    await activation_store.conversation_save("refused", [{"role": "user", "content": "UI-only fixture"}])
    attachment = await coordinator.attach("refused", checkpoint_version=1)
    assert attachment.readiness.managed and not attachment.readiness.ready
    orch = SimpleNamespace(_context_checkpoints=coordinator, conversation_history=history,
                           handle_command_stream=AsyncMock(), handle_ui_event=AsyncMock())
    voice = SimpleNamespace(set_session_voice_mode=Mock(), stop_session_voice=AsyncMock(), handle_audio_from_client=AsyncMock())
    state = SimpleNamespace(memory=activation_store, orchestrator=orch, voice_router=voice, sessions={})
    registry = MethodRegistry()
    register_core_methods(registry, state)
    send = AsyncMock()
    session = GatewaySession("refused", SimpleNamespace(send_json=send), registry)
    state.sessions["refused"] = session._ws
    session.metadata["runtime_context_readiness"] = lambda: coordinator.readiness(attachment.token)
    session.metadata["context_checkpoint_requested"] = requested
    capability = await registry.get("chat.capabilities")("refused", {}, session)
    assert capability["context_managed"] is True and capability["context_ready"] is False
    assert capability["context_state"] == "legacy_unavailable"
    requests = [("chat.send", {"text": "must not execute"}), ("ui.action", {"action_id": "fixture"}),
                ("voice.config", {"mode": "realtime"}), ("voice.audio", {"data_b64": "fixture"})]
    requests.extend((f"session.{name}", {}) for name in ("reset", "compact", "snapshot", "branch", "restore"))
    for method, params in requests:
        request_id = str(uuid4())
        await session.handle_message({"type": "req", "id": request_id, "method": method, "params": params})
        response = send.await_args_list[-1].args[0]
        assert response["id"] == request_id and response["ok"] is False
        assert response["error"]["code"] == "context_legacy_unavailable"
        assert response["error"]["details"]["retry_safe"] is False
    assert activation_store.working_get("refused") == [] and "refused" not in history
    assert await activation_store.list_snapshots() == []
    assert (await activation_store.runtime_checkpoint_read("refused")).status == CheckpointStatus.ABSENT
    orch.handle_command_stream.assert_not_awaited()
    orch.handle_ui_event.assert_not_awaited()
    voice.set_session_voice_mode.assert_not_called()
    voice.handle_audio_from_client.assert_not_awaited()
    await registry.get("voice.config")("refused", {"mode": "disabled"}, session)
    voice.stop_session_voice.assert_awaited_once_with("refused")
    await coordinator.detach(attachment.token)


async def test_gateway_requested_context_missing_attachment_refuses_before_prompt(activation_store):
    from gateway.protocol import GatewaySession, MethodRegistry, register_core_methods
    orch = SimpleNamespace(handle_command_stream=AsyncMock())
    state = SimpleNamespace(memory=activation_store, orchestrator=orch)
    registry = MethodRegistry()
    register_core_methods(registry, state)
    send = AsyncMock()
    session = GatewaySession("missing", SimpleNamespace(send_json=send), registry)
    session.metadata["context_checkpoint_requested"] = True
    capability = await registry.get("chat.capabilities")("missing", {}, session)
    assert capability["context_managed"] is False and capability["context_ready"] is False
    await session.handle_message({"type": "req", "id": "fixture", "method": "chat.send", "params": {"text": "must not execute"}})
    response = send.await_args_list[-1].args[0]
    assert response["ok"] is False and response["error"]["code"] == "context_unavailable"
    assert activation_store.working_get("missing") == []
    orch.handle_command_stream.assert_not_awaited()


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_actual_ui_helper_effect_fenced_before_confirmation_consumption(activation_store, monkeypatch, confirm, commit_fails):
    orch = make_orch(activation_store, [])
    coordinator = orch._context_checkpoints
    coordinator.legacy_passthrough = True
    attachment = await coordinator.attach("ui", checkpoint_version=1)
    action = "confirm_fixture" if confirm else "call_fixture__effect"
    pending = {"session_id": "ui", "tool_call": {"name": "fixture__effect", "args": {}}}
    if confirm:
        orch._pending_confirmations["fixture"] = pending
    effects = []
    async def effect(sid, tool, skills):
        assert (await activation_store.runtime_checkpoint_read(sid)).status == CheckpointStatus.IN_PROGRESS
        assert coordinator.owns_writer(sid) and orch._get_session_lock(sid).locked()
        if confirm:
            assert "fixture" not in orch._pending_confirmations
        effects.append(tool["name"])
        activation_store.working_push(sid, {"role": "assistant", "text": "controlled UI result"})
    orch._execute_tool_call = AsyncMock(side_effect=effect)
    if commit_fails:
        monkeypatch.setattr(activation_store, "runtime_checkpoint_commit", AsyncMock(side_effect=RuntimeError("fixture commit unknown")))
    try:
        if commit_fails:
            with pytest.raises(RuntimeContextError) as raised:
                await orch.handle_ui_event("ui", action, "tap")
            assert raised.value.effects_may_have_occurred and not raised.value.retry_safe
            assert (await activation_store.runtime_checkpoint_read("ui")).status == CheckpointStatus.IN_PROGRESS
            with pytest.raises(RuntimeContextError):
                await orch.handle_ui_event("ui", action, "tap")
        else:
            await orch.handle_ui_event("ui", action, "tap")
            result = await activation_store.runtime_checkpoint_read("ui")
            assert result.status == CheckpointStatus.READY
            assert result.record.context.working()[0]["text"] == "controlled UI result"
            if confirm:
                await orch.handle_ui_event("ui", action, "tap")
        assert effects == ["fixture__effect"]
    finally:
        await coordinator.detach(attachment.token)
        await orch.drain_background_tasks()


async def test_ui_fallback_and_owned_confirmation_reuse_one_exact_scope(activation_store):
    captured = []
    orch = make_orch(activation_store, captured)
    coordinator = orch._context_checkpoints
    attachment = await coordinator.attach("ui", checkpoint_version=1)
    try:
        await orch.handle_ui_event("ui", "generic_fixture", "tap")
        assert len(captured) == 1
        assert any("generic_fixture" in str(row) for row in captured[0])
        before = await activation_store.runtime_checkpoint_read("ui")
        async with coordinator.write_scope("ui", command_handoff=True):
            await orch.handle_ui_event("ui", "confirm_absent", "tap")
            assert coordinator.owns_writer("ui")
            pending = await activation_store.runtime_checkpoint_read("ui")
            assert pending.status == CheckpointStatus.IN_PROGRESS
            assert pending.record.fence.revision == before.record.fence.revision + 1
        after = await activation_store.runtime_checkpoint_read("ui")
        assert after.status == CheckpointStatus.READY and after.record.fence.revision == before.record.fence.revision + 2
    finally:
        await coordinator.detach(attachment.token)
        await orch.drain_background_tasks()
