"""Exact committed-context receipts through the existing manager and real SQLite."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agents.chat_turns import ChatTurnManager, CommittedChatResult, turn_audit
from tests.test_runtime_context_attached_scope import attached as _attached_fixture, scope
from tests.test_runtime_context_ingress import capabilities, context_client as _context_fixture, send_tracked
from tests.test_chat_turn_abort import receive_type

attached = _attached_fixture
context_client = _context_fixture


async def settle(manager):
    tasks = [live.task for live in manager._live.values() if live.task is not None]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
    if manager._settlements:
        await asyncio.wait_for(asyncio.gather(*manager._settlements), 3)


async def submit(attached, *, outcome="completed", emit=None, run=None):
    state, coordinator, attachment, owner, store = attached
    manager = ChatTurnManager(state)
    caller = asyncio.current_task()
    owners = []
    async def execute():
        owners.append(asyncio.current_task())
        async with scope(state, attachment, owner) as receipt:
            assert coordinator.owns_writer("thread-A")
            async with coordinator.command_scope("thread-A"):
                audit = turn_audit("thread-A")
                audit.began = True
                if outcome == "awaiting_approval":
                    audit.approval_request_ids.append("synthetic-review")
                elif outcome == "refused":
                    audit.refused = True
                elif outcome == "failed":
                    audit.error = True
                coordinator.history["thread-A"] = [{"role": "assistant", "content": "exact final"}]
        return CommittedChatResult("exact final", receipt)
    sink = emit or AsyncMock()
    accepted = await manager.submit(owner=owner, session_id="thread-A", request_id=str(uuid4()),
        terms={"text": "synthetic fixture"}, run=run or execute, emit=sink)
    await settle(manager)
    if owners:
        assert owners[0] is not caller
    return manager, accepted, sink, store


@pytest.mark.parametrize("outcome", ["completed", "awaiting_approval", "refused"])
async def test_manager_persists_only_exact_successful_scope_fence(attached, outcome):
    manager, accepted, sink, store = await submit(attached, outcome=outcome)
    terminal = await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert terminal["processing_outcome"] == outcome
    checkpoint = terminal["context_checkpoint"]
    actual = (await store.runtime_checkpoint_read("thread-A")).record.fence
    assert checkpoint == {"contract_version": 1, "session_id": actual.session_id,
        "generation": actual.generation, "revision": actual.revision,
        "attempt_id": actual.attempt_id, "durable": True}
    assert sink.await_args_list[-1].args == ("chat_turn_terminal", terminal)


async def test_failed_processing_does_not_certify_checkpoint_for_speech(attached):
    manager, accepted, sink, _ = await submit(attached, outcome="failed")
    terminal = await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert terminal["processing_outcome"] == "failed"
    assert terminal["final_text"] == "" and "context_checkpoint" not in terminal
    assert sink.await_args_list[-1].args[0] == "chat_turn_terminal"


@pytest.mark.parametrize("failure", ["exception", "false"])
async def test_terminal_store_failure_never_emits_context_certificate(attached, monkeypatch, failure):
    state, _coordinator, _attachment, _owner, store = attached
    original = store.chat_turn_update
    async def update(**kwargs):
        if kwargs["status"] == "terminal":
            if failure == "exception":
                raise OSError("synthetic terminal failure")
            return False
        return await original(**kwargs)
    monkeypatch.setattr(store, "chat_turn_update", update)
    manager, accepted, sink, _ = await submit(attached)
    assert all(call.args[0] != "chat_turn_terminal" for call in sink.await_args_list)
    assert "context_checkpoint" not in await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert not manager.has_active_session("thread-A") and state.memory is store


async def test_next_turn_advancing_sid_during_terminal_storage_does_not_relabel_fence(attached, monkeypatch):
    state, _coordinator, attachment, owner, store = attached
    original = store.chat_turn_update
    paused, release = asyncio.Event(), asyncio.Event()
    captured = []
    async def update(**kwargs):
        if kwargs["status"] == "terminal":
            captured.append(dict(kwargs["receipt"]["context_checkpoint"]))
            paused.set()
            await release.wait()
        return await original(**kwargs)
    monkeypatch.setattr(store, "chat_turn_update", update)
    work = asyncio.create_task(submit(attached))
    await asyncio.wait_for(paused.wait(), 2)
    async with scope(state, attachment, owner):
        pass
    latest = (await store.runtime_checkpoint_read("thread-A")).record.fence
    assert latest.revision > captured[0]["revision"]
    release.set()
    manager, accepted, sink, _ = await work
    terminal = await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert terminal["context_checkpoint"] == captured[0]
    assert sink.await_args_list[-1].args[1]["context_checkpoint"] == captured[0]


async def test_stale_after_terminal_commit_is_historical_status_not_live_certification(attached, monkeypatch):
    state, _coordinator, _attachment, _owner, store = attached
    original = store.chat_turn_update
    async def update(**kwargs):
        result = await original(**kwargs)
        if kwargs["status"] == "terminal":
            state.sessions["thread-A"] = object()
        return result
    monkeypatch.setattr(store, "chat_turn_update", update)
    manager, accepted, sink, _ = await submit(attached)
    terminal = await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert terminal["context_checkpoint"]["durable"] is True
    assert [call.args[0] for call in sink.await_args_list] == ["chat_turn_accepted"]
    assert not manager.has_active_session("thread-A")


@pytest.mark.parametrize("malformed", ["dict", "foreign", "bool_revision", "partial_uuid", "text"])
async def test_runner_cannot_certify_partial_foreign_or_uncanonical_fields(attached, malformed):
    state, _coordinator, attachment, owner, _ = attached
    async def run():
        async with scope(state, attachment, owner) as receipt:
            pass
        if malformed == "dict":
            return CommittedChatResult("response", SimpleNamespace(committed_fence={"revision": 2}))
        if malformed == "text":
            return CommittedChatResult(False, receipt)
        fence = receipt._committed
        changes = {"foreign": ("session_id", "thread-B"), "bool_revision": ("revision", True),
                   "partial_uuid": ("attempt_id", "partial")}
        key, value = changes[malformed]
        object.__setattr__(fence, key, value)
        return CommittedChatResult("response", receipt)
    manager, accepted, sink, _ = await submit(attached, run=run)
    terminal = await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert terminal["processing_outcome"] == "failed"
    assert "context_checkpoint" not in terminal and terminal["final_text"] == ""
    assert sink.await_args_list[-1].args[0] == "chat_turn_terminal"


async def test_legacy_string_runner_remains_compatible(attached):
    manager, accepted, _sink, _ = await submit(attached, run=AsyncMock(return_value="legacy final"))
    terminal = await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert terminal["processing_outcome"] == "completed" and terminal["final_text"] == "legacy final"
    assert "context_checkpoint" not in terminal


async def test_superseded_owner_between_scope_exit_and_manager_result_never_certifies(attached):
    state, coordinator, attachment, owner, store = attached
    async def run():
        async with scope(state, attachment, owner) as receipt:
            turn_audit("thread-A").began = True
            coordinator.history["thread-A"] = [{"role": "assistant", "content": "historical final"}]
        state.sessions["thread-A"] = object()
        return CommittedChatResult("must not speak", receipt)
    manager, accepted, sink, _ = await submit(attached, run=run)
    terminal = await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert terminal["processing_outcome"] == "outcome_unknown"
    assert terminal["final_text"] == "" and "context_checkpoint" not in terminal
    assert (await store.runtime_checkpoint_read("thread-A")).status.value == "ready"
    assert sink.await_args_list[-1].args[1] == terminal


async def test_exact_manager_abort_leaves_pending_context_without_certificate(attached):
    state, coordinator, attachment, owner, store = attached
    manager, sink = ChatTurnManager(state), AsyncMock()
    entered = asyncio.Event()
    async def run():
        async with scope(state, attachment, owner) as receipt:
            async with coordinator.command_scope("thread-A"):
                turn_audit("thread-A").began = True
                entered.set()
                await asyncio.Event().wait()
        return CommittedChatResult("must not speak", receipt)
    accepted = await manager.submit(owner=owner, session_id="thread-A", request_id=str(uuid4()),
        terms={"text": "bounded synthetic held task"}, run=run, emit=sink)
    await asyncio.wait_for(entered.wait(), 2)
    result = await manager.abort(owner=owner, session_id="thread-A",
        request_id=accepted["request_id"], turn_id=accepted["turn_id"])
    assert result["cancel_requested"] is True
    await settle(manager)
    terminal = await manager.status(session_id="thread-A", request_id=accepted["request_id"])
    assert terminal["processing_outcome"] == "cancelled"
    assert terminal["action_outcome"] == "unknown" and terminal["final_text"] == ""
    assert "context_checkpoint" not in terminal
    assert (await store.runtime_checkpoint_read("thread-A")).status.value == "in_progress"


@pytest.mark.parametrize("mutation", ["owner", "store", "coordinator", "refusal"])
def test_registered_preparation_await_cannot_cross_attachment_replacement(context_client, monkeypatch, mutation):
    from agents.runtime_context_checkpoint import ContextReadinessState, RuntimeContextReadiness
    client, state, orch, store, captured = context_client
    entered, release = asyncio.Event(), asyncio.Event()
    async def refine(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(refined_text="must not execute")
    monkeypatch.setattr("agents.prompt_refiner.refine", refine)
    with client.websocket_connect("/v1/session?session_id=preparation-race&context_checkpoint_version=1") as ws:
        assert capabilities(ws)["context_ready"]
        send_tracked(ws)
        receive_type(ws, "chat_turn_accepted")
        client.portal.call(asyncio.wait_for, entered.wait(), 2)
        async def replace():
            if mutation == "owner":
                state.sessions["preparation-race"] = object()
            elif mutation == "store":
                state.memory = object()
            elif mutation == "coordinator":
                state.orchestrator._context_checkpoints = object()
            else:
                coordinator = orch._context_checkpoints
                token = next(t for t in coordinator._attachments if t.session_id == "preparation-race")
                coordinator._attachment_refusals[token] = RuntimeContextReadiness(
                    "preparation-race", ContextReadinessState.CONFLICT, True)
            release.set()
        client.portal.call(replace)
        terminal = receive_type(ws, "chat_turn_terminal")["payload"]
        assert terminal["processing_outcome"] == "failed"
        assert "context_checkpoint" not in terminal and captured == []
        assert (client.portal.call(store.runtime_checkpoint_read, "preparation-race")).status.value == "in_progress"
        state.memory = store


def test_registered_tracked_client_path_emits_exact_committed_fence(context_client):
    client, _state, _orch, store, _captured = context_client
    with client.websocket_connect("/v1/session?session_id=attached-wire&context_checkpoint_version=1") as ws:
        assert capabilities(ws)["managed_unsupported_paths"][0] == "voice"
        request = send_tracked(ws)
        receive_type(ws, "chat_turn_accepted")
        terminal = receive_type(ws, "chat_turn_terminal")["payload"]
        read = client.portal.call(store.runtime_checkpoint_read, "attached-wire")
        assert terminal["request_id"] == request
        assert terminal["context_checkpoint"]["revision"] == read.record.fence.revision
        assert terminal["context_checkpoint"]["attempt_id"] == read.record.fence.attempt_id
