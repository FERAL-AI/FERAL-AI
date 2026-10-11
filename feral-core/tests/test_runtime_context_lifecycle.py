"""Trusted-writer lifecycle; production activation/writer closure is separate."""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agents.llm_provider import LLMProvider
from agents.runtime_context_checkpoint import RuntimeContextError
from memory.runtime_session_checkpoint import CheckpointLimits, CheckpointRecord, CheckpointResult, CheckpointStatus as Status, encode_context
from memory.store import MemoryStore
from tests.test_stream_nonstream_parity import _make_default_gate_orchestrator


def make_orch(store, captured, *, streaming=False):
    orch = _make_default_gate_orchestrator()
    orch.memory = None  # Isolate legacy memory/episode services, not checkpoint SQLite.
    orch._multi_agent_enabled = False
    orch._streaming_enabled = streaming
    orch.llm.extract_response = LLMProvider.__new__(LLMProvider).extract_response

    async def chat(messages, **kwargs):
        captured.append(json.loads(json.dumps(messages)))
        return {"choices": [{"message": {"role": "assistant", "content": "fixture response"}}]}

    async def stream(messages, **kwargs):
        captured.append(json.loads(json.dumps(messages)))
        yield {"type": "text_delta", "content": "fixture response"}
        yield {"type": "done"}

    orch.llm.chat_with_failover = chat
    orch.llm.chat_stream = stream
    orch.install_runtime_context_checkpoints(store)
    return orch


@pytest.fixture
async def store(tmp_path):
    value = MemoryStore(db_path=str(tmp_path / "context.db"), conn_pool_size=2)
    try:
        yield value
    finally:
        await value.aclose()


@pytest.mark.parametrize("streaming", [False, True])
async def test_reopen_sqlite_new_orchestrator_restores_next_actual_request(tmp_path, streaming):
    path = str(tmp_path / "reopen.db")
    first = MemoryStore(db_path=path)
    captured = []
    orch = make_orch(first, captured, streaming=streaming)
    handler = "handle_command_stream" if streaming else "handle_command"
    try:
        await getattr(orch, handler)("thread-A", "first fixture prompt")
        assert (await first.runtime_checkpoint_read("thread-A")).status == Status.READY
        await orch.drain_background_tasks()
    finally:
        await first.aclose()
    second = MemoryStore(db_path=path)
    captured_next = []
    reopened = make_orch(second, captured_next, streaming=streaming)
    try:
        await getattr(reopened, handler)("thread-A", "second fixture prompt")
        rows = captured_next[0]
        assert any(row.get("role") == "user" and row.get("content") == "first fixture prompt" for row in rows)
        assert any(row.get("role") == "assistant" and row.get("content") == "fixture response" for row in rows)
        assert any(row.get("role") == "user" and row.get("content") == "second fixture prompt" for row in rows)
        await reopened.drain_background_tasks()
    finally:
        await second.aclose()


async def test_simultaneous_exact_threads_and_sequential_isolation(store):
    captured = []
    orch = make_orch(store, captured)
    entered = asyncio.Event()
    calls = 0

    async def concurrent(messages, **kwargs):
        nonlocal calls
        captured.append(json.loads(json.dumps(messages)))
        calls += 1
        if calls == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), 2)
        return {"choices": [{"message": {"content": "fixture response"}}]}

    orch.llm.chat_with_failover = concurrent
    await asyncio.gather(orch.handle_command("A", "A-only first"), orch.handle_command("B", "B-only first"))
    await asyncio.gather(orch.handle_command("A", "A-only next"), orch.handle_command("B", "B-only next"))
    for rows in captured:
        text = json.dumps(rows)
        assert ("A-only" in text) != ("B-only" in text)
    assert (await store.runtime_checkpoint_read("A")).record.fence.generation != (await store.runtime_checkpoint_read("B")).record.fence.generation
    await orch.drain_background_tasks()


async def test_pending_precedes_provider_and_awaited_commit_precedes_return(store, monkeypatch):
    captured = []
    orch = make_orch(store, captured)
    original_provider = orch.llm.chat_with_failover

    async def provider(*args, **kwargs):
        assert (await store.runtime_checkpoint_read("A")).status == Status.IN_PROGRESS
        return await original_provider(*args, **kwargs)

    orch.llm.chat_with_failover = provider
    original_commit = store.runtime_checkpoint_commit
    committing, release = asyncio.Event(), asyncio.Event()

    async def held_commit(*args, **kwargs):
        committing.set()
        await release.wait()
        return await original_commit(*args, **kwargs)

    monkeypatch.setattr(store, "runtime_checkpoint_commit", held_commit)
    task = asyncio.create_task(orch.handle_command("A", "fixture prompt"))
    await asyncio.wait_for(committing.wait(), 2)
    assert not task.done() and orch._get_session_lock("A").locked()
    assert (await store.runtime_checkpoint_read("A")).status == Status.IN_PROGRESS
    release.set()
    await task
    assert (await store.runtime_checkpoint_read("A")).status == Status.READY
    assert not orch._get_session_lock("A").locked()
    await orch.drain_background_tasks()


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_existing_core04_terminal_waits_for_context_commit(store, monkeypatch, commit_fails):
    from agents.chat_turns import ChatTurnManager
    from tests.test_chat_turn_receipts import submitted, terminal

    orch = make_orch(store, [])
    manager = ChatTurnManager(SimpleNamespace(memory=store, orchestrator=orch))
    original = store.runtime_checkpoint_commit
    committing, release = asyncio.Event(), asyncio.Event()

    async def commit(*args):
        committing.set()
        await release.wait()
        if commit_fails:
            raise sqlite3.OperationalError("fixture commit failed")
        return await original(*args)

    monkeypatch.setattr(store, "runtime_checkpoint_commit", commit)
    receipt, _owner, emit, _run = await submitted(manager, run=lambda: orch.handle_command("thread-A", "fixture tracked turn"))
    await asyncio.wait_for(committing.wait(), 2)
    before = await manager.status(session_id="thread-A", turn_id=receipt["turn_id"])
    assert "processing_outcome" not in before
    assert (await store.runtime_checkpoint_read("thread-A")).status == Status.IN_PROGRESS
    assert not any(call.args[0] == "chat_turn_terminal" for call in emit.await_args_list)
    release.set()
    done = await terminal(manager, receipt)
    if commit_fails:
        assert done["processing_outcome"] == "outcome_unknown" and done["action_outcome"] == "unknown"
        assert (await store.runtime_checkpoint_read("thread-A")).status == Status.IN_PROGRESS
    else:
        assert done["processing_outcome"] == "completed" and done["action_outcome"] == "not_asserted"
        assert (await store.runtime_checkpoint_read("thread-A")).status == Status.READY
    if manager._settlements:
        await asyncio.gather(*manager._settlements)
    await orch.drain_background_tasks()


async def test_explicit_preparation_scope_restores_before_prelude_one_command(store):
    first = make_orch(store, [])
    await first.handle_command("A", "prior fixture")
    store.working_push("A", {"role": "assistant", "text": "not committed outside guard"})
    store.working_clear("A")
    captured = []
    reopened = make_orch(store, captured)
    coordinator = reopened._context_checkpoints
    async with coordinator.write_scope("A", command_handoff=True):
        assert reopened.conversation_history["A"][0]["content"] == "prior fixture"
        store.working_push("A", {"role": "user", "text": "new prelude"})
        await reopened.handle_command("A", "new fixture")
        assert (await store.runtime_checkpoint_read("A")).status == Status.IN_PROGRESS
        with pytest.raises(RuntimeContextError, match="nested_context_command"):
            await reopened.handle_command("A", "cannot recursively execute another command")
    read = await store.runtime_checkpoint_read("A")
    assert read.status == Status.READY and read.record.context.working()[0]["text"] == "new prelude"
    await first.drain_background_tasks()
    await reopened.drain_background_tasks()


async def test_inherited_child_task_does_not_bypass_owned_session_lock(store):
    captured = []
    orch = make_orch(store, captured)
    coordinator = orch._context_checkpoints
    async with coordinator.write_scope("A", command_handoff=True):
        child = asyncio.create_task(orch.handle_command("A", "child fixture"))
        await asyncio.sleep(0)
        assert not child.done() and captured == []
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await child
        await orch.handle_command("A", "owner fixture")
    assert len(captured) == 1
    await orch.drain_background_tasks()


async def test_cancellation_leaves_pending_and_refuses_new_orchestrator(store):
    captured = []
    orch = make_orch(store, captured)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    orch.llm.chat_with_failover = blocked
    task = asyncio.create_task(orch.handle_command("A", "fixture cancellation"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.runtime_checkpoint_read("A")).status == Status.IN_PROGRESS
    assert not orch._get_session_lock("A").locked()
    reopened = make_orch(store, captured)
    with pytest.raises(RuntimeContextError, match="checkpoint_in_progress"):
        await reopened.handle_command("A", "must not replay")
    assert captured == []
    await reopened.handle_command("B", "explicit new thread works")
    await orch.drain_background_tasks()
    await reopened.drain_background_tasks()


async def test_commit_failure_is_not_retry_safe_and_blocks_next_request(store, monkeypatch):
    captured = []
    orch = make_orch(store, captured)

    async def fail(*args):
        raise sqlite3.OperationalError("fixture disk error")

    monkeypatch.setattr(store, "runtime_checkpoint_commit", fail)
    with pytest.raises(RuntimeContextError) as error:
        await orch.handle_command("A", "fixture complete processing")
    assert error.value.effects_may_have_occurred and not error.value.retry_safe
    assert (await store.runtime_checkpoint_read("A")).status == Status.IN_PROGRESS
    with pytest.raises(RuntimeContextError, match="checkpoint_in_progress"):
        await orch.handle_command("A", "no retry")
    assert len(captured) == 1
    await orch.drain_background_tasks()


async def test_cancel_while_commit_is_waiting_does_not_publish_ready(store, monkeypatch):
    orch = make_orch(store, [])
    committing = asyncio.Event()

    async def blocked_commit(*args):
        committing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "runtime_checkpoint_commit", blocked_commit)
    task = asyncio.create_task(orch.handle_command("A", "fixture finished provider"))
    await asyncio.wait_for(committing.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.runtime_checkpoint_read("A")).status == Status.IN_PROGRESS
    assert not orch._get_session_lock("A").locked()
    await orch.drain_background_tasks()


@pytest.mark.parametrize("bad", ["phase", "fence", "context"])
async def test_applied_without_exact_ready_context_is_not_success(store, monkeypatch, bad):
    orch = make_orch(store, [])

    async def unverifiable(fence, context):
        updated = replace(fence, revision=fence.revision + 1)
        if bad == "fence":
            updated = replace(updated, attempt_id=str(uuid4()))
        row = CheckpointRecord(updated, Status.IN_PROGRESS if bad == "phase" else Status.READY,
                               1.0, None if bad == "context" else context)
        return CheckpointResult(Status.APPLIED, row)  # No SQL commit occurred.

    monkeypatch.setattr(store, "runtime_checkpoint_commit", unverifiable)
    with pytest.raises(RuntimeContextError, match="checkpoint_commit_unverified") as error:
        await orch.handle_command("A", "cannot assert false ready")
    assert error.value.effects_may_have_occurred and not error.value.retry_safe
    assert (await store.runtime_checkpoint_read("A")).status == Status.IN_PROGRESS
    await orch.drain_background_tasks()


async def test_total_context_quota_refusal_is_pending_and_not_retry_safe(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "byte-quota.db"), runtime_checkpoint_limits=CheckpointLimits(total_bytes=10))
    try:
        captured = []
        orch = make_orch(store, captured)
        with pytest.raises(RuntimeContextError, match="checkpoint_commit_quota") as error:
            await orch.handle_command("A", "fixture already processed")
        assert error.value.effects_may_have_occurred and not error.value.retry_safe
        assert (await store.runtime_checkpoint_read("A")).status == Status.IN_PROGRESS
        assert len(captured) == 1
        await orch.drain_background_tasks()
    finally:
        await store.aclose()


async def test_changed_ready_revision_refuses_stale_ram_before_provider(store):
    captured = []
    orch = make_orch(store, captured)
    await orch.handle_command("A", "fixture original")
    ready = await store.runtime_checkpoint_read("A")
    pending = await store.runtime_checkpoint_begin("A", attempt_id=str(uuid4()), expected=ready.record.fence)
    await store.runtime_checkpoint_commit(pending.record.fence, encode_context([{"role": "user", "content": "other trusted writer"}], []))
    with pytest.raises(RuntimeContextError, match="checkpoint_conflict"):
        await orch.handle_command("A", "do not overwrite authoritative state")
    assert len(captured) == 1
    assert (await store.runtime_checkpoint_read("A")).record.context.history()[0]["content"] == "other trusted writer"
    await orch.drain_background_tasks()


async def test_delete_during_processing_cannot_be_resurrected(store):
    captured = []
    orch = make_orch(store, captured)
    original = orch.llm.chat_with_failover

    async def deleting(*args, **kwargs):
        read = await store.runtime_checkpoint_read("A")
        await store.runtime_checkpoint_delete(read.record.fence)
        return await original(*args, **kwargs)

    orch.llm.chat_with_failover = deleting
    with pytest.raises(RuntimeContextError, match="checkpoint_commit_deleted") as error:
        await orch.handle_command("A", "fixture delete race")
    assert error.value.effects_may_have_occurred and not error.value.retry_safe
    assert (await store.runtime_checkpoint_read("A")).status == Status.DELETED
    with pytest.raises(RuntimeContextError, match="checkpoint_deleted"):
        await orch.handle_command("A", "no resurrection")
    assert len(captured) == 1
    await orch.drain_background_tasks()


@pytest.mark.parametrize("fault,code", [("future", "checkpoint_unsupported"), ("corrupt", "checkpoint_corrupt"),
                                      ("deleted", "checkpoint_deleted"), ("pending", "checkpoint_in_progress")])
async def test_bad_checkpoint_refuses_before_actual_provider(store, fault, code):
    pending = await store.runtime_checkpoint_begin("A", attempt_id=str(uuid4()))
    if fault != "pending":
        ready = await store.runtime_checkpoint_commit(pending.record.fence, encode_context([{"role": "user", "content": "old fixture"}], []))
        if fault == "deleted":
            await store.runtime_checkpoint_delete(ready.record.fence)
        else:
            with sqlite3.connect(store.db_path) as conn:
                if fault == "future":
                    conn.execute("UPDATE runtime_session_checkpoints SET format_version=999")
                else:
                    conn.execute("UPDATE runtime_session_checkpoints SET payload_json='{broken', payload_bytes=7")
    captured = []
    orch = make_orch(store, captured)
    with pytest.raises(RuntimeContextError, match=code):
        await orch.handle_command("A", "never submitted")
    assert captured == [] and "A" not in orch.conversation_history


async def test_ui_transcript_not_imported_and_existing_unmanaged_ram_refused(store):
    await store.conversation_save("legacy", [{"role": "assistant", "content": "UI cannot authorize context"}])
    captured = []
    orch = make_orch(store, captured)
    with pytest.raises(RuntimeContextError, match="legacy_context_unavailable"):
        await orch.handle_command("legacy", "never submitted")
    orch.conversation_history["unmanaged"] = [{"role": "assistant", "content": "unknown provenance"}]
    with pytest.raises(RuntimeContextError, match="unmanaged_runtime_context"):
        await orch.handle_command("unmanaged", "never submitted")
    assert captured == []
    assert (await store.runtime_checkpoint_read("legacy")).status == Status.ABSENT


async def test_lock_identity_retained_and_attempted_ids_bounded(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "quota.db"), runtime_checkpoint_limits=CheckpointLimits(sessions=1))
    try:
        orch = make_orch(store, [])
        existing = orch._get_session_lock("A")
        await orch.handle_command("A", "fixture")
        assert orch._get_session_lock("A") is existing
        with pytest.raises(RuntimeContextError, match="context_session_quota"):
            await orch.handle_command("B", "never submitted")
        assert "B" not in orch._session_locks
        await orch.drain_background_tasks()
    finally:
        await store.aclose()


def test_real_process_restart_restores_request_without_executing_old_tool(tmp_path):
    script = r'''
import asyncio,json,sys
from unittest.mock import AsyncMock
from types import SimpleNamespace
import security.vault as vault
vault._keyring_get_password=lambda *a: None
vault._keyring_set_password=lambda *a: None
vault._keyring_delete_password=lambda *a: None
vault.get_vault=lambda: SimpleNamespace(get_credential=lambda *a: None, inject_headers=lambda sid,h:h)
from memory.store import MemoryStore
from agents.llm_provider import LLMProvider
from tests.test_stream_nonstream_parity import _make_default_gate_orchestrator
async def run():
    store=MemoryStore(db_path=sys.argv[1])
    orch=_make_default_gate_orchestrator()
    orch.memory=None
    orch._multi_agent_enabled=False
    orch.llm.extract_response=LLMProvider.__new__(LLMProvider).extract_response
    orch.install_runtime_context_checkpoints(store)
    captured=[]
    call={"id":"past-call","type":"function","function":{"name":"fixture_read","arguments":"{}"}}
    replies=([{"choices":[{"message":{"content":None,"tool_calls":[call]}}]},
              {"choices":[{"message":{"content":"first process final"}}]}] if sys.argv[2]=="first" else
             [{"choices":[{"message":{"content":"second process final"}}]}])
    async def provider(messages,**kwargs):
        captured.append(json.loads(json.dumps(messages)))
        return replies.pop(0)
    orch.llm.chat_with_failover=provider
    orch._execute_tool_call_for_llm=AsyncMock(return_value={"success":True,"data":{"fixture":"read"}})
    orch._try_genui_for_result=AsyncMock()
    try:
        await orch.handle_command("restart-thread", "first process prompt" if sys.argv[2]=="first" else "new process prompt")
        print(json.dumps({"messages":captured[-1],"executions":orch._execute_tool_call_for_llm.await_count}))
        await orch.drain_background_tasks()
    finally:
        await store.aclose()
asyncio.run(run())
'''
    environment = dict(os.environ, FERAL_HOME=str(tmp_path / "home"), FERAL_DATA_HOME=str(tmp_path / "home"),
                       FERAL_NATIVE_VAULT_DEFER="1", FERAL_AUTO_APPROVE="0")
    database = str(tmp_path / "restart.db")
    results = []
    for phase in ("first", "second"):
        child = subprocess.run([sys.executable, "-c", script, database, phase], cwd=Path(__file__).resolve().parents[1],
                               env=environment, capture_output=True, text=True, timeout=30)
        assert child.returncode == 0, child.stderr
        results.append(json.loads(child.stdout.strip().splitlines()[-1]))
    assert results[0]["executions"] == 1 and results[1]["executions"] == 0
    rows = results[1]["messages"]
    assert any(row.get("role") == "user" and row.get("content") == "first process prompt" for row in rows)
    assert any(row.get("role") == "tool" and row.get("tool_call_id") == "past-call" for row in rows)
    assert any(row.get("content") == "first process final" for row in rows)
    assert any(row.get("content") == "new process prompt" for row in rows)
