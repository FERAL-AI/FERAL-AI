"""The shared desktop/phone turn runner must never report failure as prose."""

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.test_runtime_context_attached_scope import attached as _attached_fixture, scope

attached = _attached_fixture


def retained_state(**kwargs):
    """Use the production retained-task lifecycle with isolated test state."""
    from api.state import BrainState
    state = SimpleNamespace(_background_tasks=set(), **kwargs)
    state.register_background_task = MethodType(BrainState.register_background_task, state)
    state.shutdown_background_tasks = MethodType(BrainState.shutdown_background_tasks, state)
    return state


async def drain_followups(state):
    tasks = tuple(state._background_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 3)


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
    state = retained_state(memory=store, orchestrator=SimpleNamespace(handle_command_stream=command),
                           skill_gen=skill_gen, sessions={"exact-A": ws})
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
        receipt = await manager.status(session_id="exact-A", request_id=request_id)
        await drain_followups(state)
        ws.send_json.assert_awaited_once()
        frame = ws.send_json.await_args.args[0]
        assert frame["session_id"] == "exact-A"
        assert frame["payload"]["chat_turn"] == {"contract_version": 1, "request_id": request_id, "turn_id": accepted["turn_id"]}
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
        await state.shutdown_background_tasks()
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("followup_result", ["pending_disconnect", "exception", "cancelled"])
async def test_completed_receipt_precedes_optional_detection_and_survives_followup_exit(tmp_path, monkeypatch, followup_result):
    import api.server as server
    from agents.chat_turns import ChatTurnManager, turn_audit
    from memory.store import MemoryStore

    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    entered, release = asyncio.Event(), asyncio.Event()
    ws = SimpleNamespace(send_json=AsyncMock())
    manager = None
    request_id = str(uuid4())

    async def command(**kwargs):
        turn_audit("exact-A").began = True
        return "31 + 11 equals 42."

    async def detect(history):
        # Actual SQLite terminal and its notification exist before any secondary
        # LLM; no foreground audit/writer authority is inherited by the followup.
        assert turn_audit("exact-A") is None
        terminal = await manager.status(session_id="exact-A", request_id=request_id)
        assert terminal["processing_outcome"] == "completed"
        assert emit.await_args_list[-1].args == ("chat_turn_terminal", terminal)
        entered.set()
        await release.wait()
        if followup_result == "exception":
            raise RuntimeError("PRIVATE_FOLLOWUP_SENTINEL")
        return {"capability": "fixture", "service": "local"}

    skill_gen = SimpleNamespace(detect_unmet_need=AsyncMock(side_effect=detect), generate_skill=AsyncMock())
    state = retained_state(memory=store, orchestrator=SimpleNamespace(handle_command_stream=command),
                           skill_gen=skill_gen, sessions={"exact-A": ws})
    monkeypatch.setattr(server, "state", state)
    manager, emit = ChatTurnManager(state), AsyncMock()
    async def run():
        return await server._build_chat_turn_runner(ws=ws, session_id="exact-A", refined_text="fixture", ctx={}, tracked=True)
    try:
        accepted = await manager.submit(owner=ws, session_id="exact-A", request_id=request_id,
                                        terms={"text": "fixture"}, run=run, emit=emit)
        await asyncio.wait_for(entered.wait(), 3)
        before = await manager.status(session_id="exact-A", request_id=request_id)
        assert before["turn_id"] == accepted["turn_id"] and before["final_text"] == "31 + 11 equals 42."
        if followup_result == "pending_disconnect":
            assert await asyncio.wait_for(manager.detach(ws), 0.5)
            state.sessions.pop("exact-A")
            release.set()
            await drain_followups(state)
        elif followup_result == "exception":
            release.set()
            await drain_followups(state)
        else:
            assert await state.shutdown_background_tasks() == 1
        assert await manager.status(session_id="exact-A", request_id=request_id) == before
        skill_gen.generate_skill.assert_not_awaited()
        ws.send_json.assert_not_awaited()
        assert not manager.has_active_session("exact-A")
    finally:
        await state.shutdown_background_tasks()
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["socket", "store", "generation"])
async def test_optional_detection_uses_captured_history_and_revoked_owner_cannot_generate(tmp_path, monkeypatch, revocation):
    import api.server as server
    from agents.chat_turns import ChatTurnManager
    from memory.store import MemoryStore

    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    entered, release = asyncio.Event(), asyncio.Event()
    ws = SimpleNamespace(send_json=AsyncMock())
    exact_history = [{"role": "user", "text": "exact-session capability"}]
    store.working_get = lambda sid: exact_history if sid == "exact-A" else [{"role": "user", "text": "FOREIGN_SESSION_SENTINEL"}]
    async def detect(history):
        entered.set()
        await release.wait()
        assert history == [{"role": "user", "text": "exact-session capability"}]
        return {"capability": "fixture", "service": "local"}
    skill_gen = SimpleNamespace(detect_unmet_need=AsyncMock(side_effect=detect), generate_skill=AsyncMock())
    state = retained_state(memory=store, orchestrator=SimpleNamespace(handle_command_stream=AsyncMock(return_value="done")),
                           skill_gen=skill_gen, sessions={"exact-A": ws})
    monkeypatch.setattr(server, "state", state)
    manager = ChatTurnManager(state)
    async def run():
        return await server._build_chat_turn_runner(ws=ws, session_id="exact-A", refined_text="fixture", ctx={}, tracked=True)
    try:
        accepted = await manager.submit(owner=ws, session_id="exact-A", request_id=str(uuid4()),
                                        terms={"text": "fixture"}, run=run, emit=AsyncMock())
        await asyncio.wait_for(entered.wait(), 3)
        exact_history[0]["text"] = "a later task's changed history"
        if revocation == "socket":
            state.sessions["exact-A"] = object()
        elif revocation == "store":
            state.memory = object()
        else:
            state._native_agent_turn_generation = 1
        release.set()
        await drain_followups(state)
        skill_gen.generate_skill.assert_not_awaited()
        ws.send_json.assert_not_awaited()
        assert (await store.chat_turn_get(session_id="exact-A", turn_id=accepted["turn_id"]))["processing_outcome"] == "completed"
    finally:
        await state.shutdown_background_tasks()
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("advance", [False, True])
async def test_managed_followup_requires_its_exact_committed_receipt_and_checkpoint(attached, monkeypatch, advance):
    import api.server as server
    from api.state import BrainState
    from agents.chat_turns import turn_audit

    state, coordinator, attachment, owner, store = attached
    entered, release = asyncio.Event(), asyncio.Event()
    owner_ws = SimpleNamespace(send_json=AsyncMock())
    state.sessions["thread-A"] = owner_ws
    state._background_tasks = set()
    state.register_background_task = MethodType(BrainState.register_background_task, state)
    state.shutdown_background_tasks = MethodType(BrainState.shutdown_background_tasks, state)
    request_id = str(uuid4())
    emit = AsyncMock()

    async def command(**kwargs):
        async with coordinator.command_scope("thread-A"):
            turn_audit("thread-A").began = True
            coordinator.history["thread-A"] = [{"role": "assistant", "content": "exact answer"}]
            return "exact answer"

    async def prepare(**kwargs):
        return "fixture", {}, "fixture"

    async def detect(history):
        assert not coordinator.owns_writer("thread-A")
        assert turn_audit("thread-A") is None
        terminal = await store.chat_turn_get(session_id="thread-A", request_id=request_id)
        actual = (await store.runtime_checkpoint_read("thread-A")).record.fence
        assert terminal["context_checkpoint"]["revision"] == actual.revision
        assert terminal["context_checkpoint"]["generation"] == actual.generation
        assert emit.await_args_list[-1].args == ("chat_turn_terminal", terminal)
        entered.set()
        await release.wait()
        return {"capability": "fixture", "service": "local"}

    state.orchestrator.handle_command_stream = command
    state.skill_gen = SimpleNamespace(detect_unmet_need=AsyncMock(side_effect=detect),
                                     generate_skill=AsyncMock(return_value={"skill_id": "unapproved-fixture"}))
    monkeypatch.setattr(server, "state", state)
    monkeypatch.setattr(server, "_prepare_chat_turn_context", prepare)
    expected = (await store.runtime_checkpoint_read("thread-A")).record.fence
    try:
        accepted = await server._submit_tracked_chat_turn(ws=owner_ws, session_id="thread-A",
            request_id=request_id, text="fixture", emit=emit, attachment=attachment, expected_fence=expected)
        await asyncio.wait_for(entered.wait(), 3)
        before = await store.chat_turn_get(session_id="thread-A", request_id=request_id)
        if advance:
            async with scope(state, attachment, owner_ws):
                pass
        release.set()
        await drain_followups(state)
        assert await store.chat_turn_get(session_id="thread-A", request_id=request_id) == before
        if advance:
            state.skill_gen.generate_skill.assert_not_awaited()
            owner_ws.send_json.assert_not_awaited()
        else:
            state.skill_gen.generate_skill.assert_awaited_once()
            frame = owner_ws.send_json.await_args.args[0]
            assert frame["type"] == "skill_proposal" and frame["session_id"] == "thread-A"
            assert frame["payload"]["chat_turn"] == {"contract_version": 1,
                "request_id": request_id, "turn_id": accepted["turn_id"]}
            assert frame["payload"]["context_checkpoint"] == before["context_checkpoint"]
        # Replaying the same receipt never repeats optional discovery either.
        await server._submit_tracked_chat_turn(ws=owner_ws, session_id="thread-A", request_id=request_id,
            text="fixture", emit=emit, attachment=attachment, expected_fence=expected)
        state.skill_gen.detect_unmet_need.assert_awaited_once()
    finally:
        await state.shutdown_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["detached_during_generation", "proposal_send_failed"])
async def test_undelivered_generated_proposal_is_not_left_available_for_approval(tmp_path, monkeypatch, failure):
    import api.server as server
    from agents.chat_turns import ChatTurnManager
    from agents.skill_generator import SkillGenerator
    from memory.store import MemoryStore

    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    entered, release = asyncio.Event(), asyncio.Event()
    ws = SimpleNamespace(send_json=AsyncMock(side_effect=OSError("synthetic closed transport")))
    class LLM:
        available = True
        async def chat(self, **kwargs):
            entered.set()
            await release.wait()
            return {"fixture": True}
        def extract_response(self, response):
            return '{"skill_id":"isolated-fixture","endpoints":[]}', []
    registry = SimpleNamespace(register_skill=AsyncMock())
    generator = SkillGenerator(LLM(), registry, skills_dir=str(tmp_path / "skills"))
    generator.detect_unmet_need = AsyncMock(return_value={"capability": "fixture"})
    state = retained_state(memory=store, orchestrator=SimpleNamespace(handle_command_stream=AsyncMock(return_value="done")),
                           skill_gen=generator, sessions={"exact-A": ws})
    monkeypatch.setattr(server, "state", state)
    manager = ChatTurnManager(state)
    async def run():
        return await server._build_chat_turn_runner(ws=ws, session_id="exact-A", refined_text="fixture", ctx={}, tracked=True)
    try:
        accepted = await manager.submit(owner=ws, session_id="exact-A", request_id=str(uuid4()),
                                        terms={"text": "fixture"}, run=run, emit=AsyncMock())
        await asyncio.wait_for(entered.wait(), 3)
        if failure == "detached_during_generation":
            state.sessions.pop("exact-A")
        release.set()
        await drain_followups(state)
        assert generator.get_pending_skills() == []
        registry.register_skill.assert_not_awaited()
        assert not (tmp_path / "skills").exists()
        if failure == "detached_during_generation":
            ws.send_json.assert_not_awaited()
        else:
            ws.send_json.assert_awaited_once()
        assert (await manager.status(session_id="exact-A", request_id=accepted["request_id"]))["processing_outcome"] == "completed"
    finally:
        await state.shutdown_background_tasks()
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_missing_terminal_commit_cannot_start_optional_discovery(tmp_path, monkeypatch, failure):
    import api.server as server
    from agents.chat_turns import ChatTurnManager
    from memory.store import MemoryStore

    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    original = store.chat_turn_update
    async def update(**kwargs):
        if kwargs["status"] == "terminal":
            if failure == "exception":
                raise OSError("synthetic durable-store failure")
            return False
        return await original(**kwargs)
    monkeypatch.setattr(store, "chat_turn_update", update)
    ws = SimpleNamespace(send_json=AsyncMock())
    generator = SimpleNamespace(detect_unmet_need=AsyncMock(), generate_skill=AsyncMock())
    state = retained_state(memory=store, orchestrator=SimpleNamespace(handle_command_stream=AsyncMock(return_value="done")),
                           skill_gen=generator, sessions={"exact-A": ws})
    monkeypatch.setattr(server, "state", state)
    manager, emit = ChatTurnManager(state), AsyncMock()
    async def run():
        return await server._build_chat_turn_runner(ws=ws, session_id="exact-A", refined_text="fixture", ctx={}, tracked=True)
    try:
        accepted = await manager.submit(owner=ws, session_id="exact-A", request_id=str(uuid4()),
                                        terms={"text": "fixture"}, run=run, emit=emit)
        tasks = tuple(live.task for live in manager._live.values() if live.task is not None)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        await drain_followups(state)
        assert "processing_outcome" not in await manager.status(session_id="exact-A", request_id=accepted["request_id"])
        kinds = [call.args[0] for call in emit.await_args_list]
        if failure == "exception":
            assert kinds == ["chat_turn_accepted", "error"]
            assert emit.await_args_list[-1].args[1]["code"] == "chat_turn_receipt_unavailable"
            assert emit.await_args_list[-1].args[1]["request_id"] == accepted["request_id"]
        else:
            assert kinds == ["chat_turn_accepted"]
        generator.detect_unmet_need.assert_not_awaited()
        generator.generate_skill.assert_not_awaited()
        ws.send_json.assert_not_awaited()
    finally:
        await state.shutdown_background_tasks()
        await store.aclose()
