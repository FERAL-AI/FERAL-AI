"""Actual SQLite creation and tracked chat boundaries with inert execution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import pytest_asyncio

from agents.chat_turns import ChatTurnManager, turn_audit
from agents.taskflow import TaskFlowHandoffConflict, TaskFlowRuntime
from api.routes import taskflows
from memory.store import MemoryStore
from skills.call_context import bind_context
from skills.impl.background_task import BackgroundTaskSkill


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    runtime = TaskFlowRuntime(db_path=str(tmp_path / "flows.db"))
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    brain = SimpleNamespace(taskflows=runtime, memory=store, sessions={}, orchestrator=None,
                            chat_turns=None, tool_runner=None)
    manager = brain.chat_turns = ChatTurnManager(brain)
    monkeypatch.setattr(taskflows, "state", brain)
    monkeypatch.setattr("api.state.state", brain)
    yield brain, manager, BackgroundTaskSkill()
    active = [live.task for live in manager._live.values() if live.task]
    for task in active:
        task.cancel()
    if active:
        await asyncio.gather(*active, return_exceptions=True)
    if manager._settlements:
        await asyncio.gather(*manager._settlements)
    if taskflows._CREATION_WORKERS:
        await asyncio.gather(*taskflows._CREATION_WORKERS, return_exceptions=True)
    await runtime.stop()
    runtime._conn.close()
    await store.aclose()


async def invoke(rig, args=None, *, endpoint="start", session="thread-A", surface="http_api", callback=None):
    brain, manager, skill = rig
    owner = object()
    brain.sessions[session] = owner
    result = []

    async def run():
        with bind_context(session_id=session, surface=surface, tool_name=f"background_task__{endpoint}", call_id="call-fixture"):
            # Reproduces SkillExecutor's legitimate inherited child task.
            if callback is None:
                result.append(await asyncio.wait_for(skill.execute(endpoint, args or {"goal": "inert research"}, {}), 5))
            else:
                result.append(await callback(skill))
        return "inert processing complete"

    accepted = await manager.submit(owner=owner, session_id=session, request_id=str(uuid4()),
                                    terms={"text": "fixture user task"}, run=run, emit=AsyncMock())
    live = manager._live[(session, accepted["turn_id"])]
    await asyncio.wait_for(live.task, 5)
    return result[0], accepted


def test_backing_is_registered_without_http_port_dependency():
    from skills.impl import AUTOLOAD_MODULES, get_implementation
    assert "background_task" in AUTOLOAD_MODULES
    assert isinstance(get_implementation("background_task"), BackgroundTaskSkill)


async def test_tracked_creation_binds_persisted_origin_and_dedicated_session(rig):
    result, receipt = await invoke(rig)
    assert result["success"] is True
    accepted = result["data"]
    handoff = accepted["handoff"]
    assert handoff["accepted"] and handoff["durable"] and handoff["replay_protected"]
    assert handoff["action_outcome"] == "not_asserted"
    assert handoff["result_subscription"] == "not_attached"
    origin = handoff["origin"]
    assert origin["request_id"] == receipt["request_id"]
    assert origin["turn_id"] == receipt["turn_id"]
    assert origin["input_revision"] is None and origin["context_commit"] == "pending"
    assert origin["surface"] == "http_api"
    flow = rig[0].taskflows.get_flow(accepted["flow_id"])
    assert flow["session_id"] == "" and flow["context"]["task_origin"] == origin
    stored = rig[0].taskflows._conn.execute("SELECT handoff_key, terms_digest FROM taskflows").fetchone()
    assert tuple(stored) == (origin["handoff_key"], origin["terms_digest"])


async def test_same_call_reuses_flow_changed_terms_refuse_and_no_second_steps(rig):
    async def calls(skill):
        first = await skill.execute("start", {"goal": "inert research"}, {})
        same = await skill.execute("start", {"goal": "inert research"}, {})
        changed = await skill.execute("start", {"goal": "different research"}, {})
        assert first["data"]["flow_id"] == same["data"]["flow_id"]
        assert same["data"]["handoff"]["replayed"] is True
        assert changed["error_code"] == "task_handoff_conflict"
        return first
    await invoke(rig, callback=calls)
    assert rig[0].taskflows._conn.execute("SELECT COUNT(*) FROM taskflows").fetchone()[0] == 1
    assert rig[0].taskflows._conn.execute("SELECT COUNT(*) FROM taskflow_steps").fetchone()[0] == 1


@pytest.mark.parametrize("body", [
    {"goal": "fixture", "origin": {"source": "tracked_chat_turn"}},
    {"goal": "fixture", "request_id": str(uuid4())},
    {"goal": "fixture", "session_id": "foreign-session"},
    {"goal": {"not": "text"}}, {"subtasks": [1]}, {"subtasks": ["x"] * 33},
    {"goal": "x" * 16385}, {"subtasks": ["x" * 16384] * 5},
])
async def test_invalid_or_body_supplied_identity_creates_no_flow(rig, body):
    result, _ = await invoke(rig, body)
    assert result["success"] is False and result["error_code"] == "task_invalid_request"
    assert rig[0].taskflows.list_flows() == []


@pytest.mark.parametrize("failure", ["owner", "cancel", "terminal", "receipt", "store"])
async def test_revalidates_live_owner_and_receipt_after_await(rig, monkeypatch, failure):
    brain = rig[0]
    original = brain.memory.chat_turn_get

    async def changed(**kwargs):
        receipt = await original(**kwargs)
        if failure == "owner":
            brain.sessions["thread-A"] = object()
        elif failure == "cancel":
            turn_audit("thread-A").cancel_requested = True
        elif failure == "terminal":
            receipt = {**receipt, "status": "terminal"}
        elif failure == "receipt":
            receipt = {**receipt, "request_id": str(uuid4())}
        else:
            brain.chat_turns._store = object()
        return receipt
    monkeypatch.setattr(brain.memory, "chat_turn_get", changed)
    result, _ = await invoke(rig)
    assert result["success"] is False
    assert result["error_code"] in {"task_origin_superseded", "task_origin_unavailable"}
    assert brain.taskflows.list_flows() == []


async def test_closed_inherited_audit_cannot_downgrade_to_legacy_creation(rig):
    copied = []
    async def capture(skill):
        copied.append(contextvars.copy_context())
        return {"success": True}
    await invoke(rig, callback=capture)
    child = copied[0].run(asyncio.create_task, rig[2].execute("start", {"goal": "late task"}, {}))
    result = await child
    assert result["error_code"] == "task_origin_superseded"
    assert rig[0].taskflows.list_flows() == []


async def test_disabled_identity_context_refuses_in_process_handoff(rig, monkeypatch):
    monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "off")
    result, _ = await invoke(rig)
    assert result["error_code"] == "task_origin_unavailable"
    assert rig[0].taskflows.list_flows() == []


async def test_legacy_http_remains_unbound_and_body_origin_refuses(rig):
    app = FastAPI()
    app.include_router(taskflows.router)
    with TestClient(app) as client:
        result = client.post("/internal/task/start", json={"goal": "legacy fixture"}).json()
        assert result["ok"] and result["handoff"]["origin"]["owner_verified"] is False
        assert result["handoff"]["origin"]["source"] == "legacy_local_operator"
        assert result["handoff"]["replay_protected"] is False
        fake = client.post("/internal/task/start", json={"goal": "fake", "origin": {"source": "tracked_chat_turn"}}).json()
        assert fake["error_code"] == "task_invalid_request"


async def test_unbound_agent_cannot_enumerate_other_origins(rig):
    started, _ = await invoke(rig)
    listed = await rig[2].execute("list", {}, {})
    status = await rig[2].execute("status", {"flow_id": started["data"]["flow_id"]}, {})
    assert listed["error_code"] == status["error_code"] == "task_origin_unavailable"
    unknown = await rig[2].execute("start", {"goal": "unknown surface"}, {})
    assert unknown["error_code"] == "task_origin_unavailable"


async def test_bound_status_and_list_only_match_originating_session(rig):
    first, _ = await invoke(rig, session="thread-A")
    second, _ = await invoke(rig, session="thread-B")
    status, _ = await invoke(rig, {"flow_id": first["data"]["flow_id"]}, endpoint="status", session="thread-B")
    assert status["error_code"] == "task_not_found"
    listed, _ = await invoke(rig, {"limit": 10}, endpoint="list", session="thread-B")
    assert [t["flow_id"] for t in listed["data"]["tasks"]] == [second["data"]["flow_id"]]


async def test_scoped_list_finds_old_owned_flow_beyond_foreign_two_hundred(rig):
    owned, _ = await invoke(rig, session="thread-A")
    runtime = rig[0].taskflows
    for i in range(205):
        runtime.create_flow(session_id="", title=f"foreign-{i}", steps=[{"type": "noop"}],
                            context={"task_origin": {"session_id": "thread-A"}}, origin_session_id="thread-B")
    listed, _ = await invoke(rig, {"limit": 10}, endpoint="list", session="thread-A")
    assert [t["flow_id"] for t in listed["data"]["tasks"]] == [owned["data"]["flow_id"]]
    foreign = runtime.list_origin_flows("thread-B", limit=1)[0]
    status, _ = await invoke(rig, {"flow_id": foreign["id"]}, endpoint="status", session="thread-A")
    assert status["error_code"] == "task_not_found"


@pytest.mark.parametrize("cancel", [False, True])
async def test_blocked_sql_creation_keeps_loop_responsive_and_cancel_guarded(rig, monkeypatch, cancel):
    runtime = rig[0].taskflows
    competing = sqlite3.connect(runtime._db_path)
    competing.execute("BEGIN IMMEDIATE")
    entered = threading.Event()
    release_worker = threading.Event()
    original = runtime.create_flow

    def blocking_create(**kwargs):
        entered.set()
        if cancel and not release_worker.wait(3):
            raise RuntimeError("fixture creation worker release timed out")
        return original(**kwargs)
    monkeypatch.setattr(runtime, "create_flow", blocking_create)
    caller = asyncio.create_task(invoke(rig))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pulse = asyncio.Event()
        asyncio.get_running_loop().call_soon(pulse.set)
        await asyncio.wait_for(pulse.wait(), 0.1)
        assert not caller.done()
        if cancel:
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            assert taskflows._CREATION_WORKERS  # Worker ownership outlives its cancelled subscriber.
            workers = list(taskflows._CREATION_WORKERS)
            assert all(not worker.done() for worker in workers)
        competing.commit()
        release_worker.set()
        if cancel:
            await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), 2)
            assert runtime.list_flows() == []
        else:
            result, _ = await asyncio.wait_for(caller, 2)
            assert result["success"] is True
    finally:
        release_worker.set()
        competing.rollback()
        competing.close()
        if not caller.done():
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)


async def test_async_creation_workers_have_bounded_admission(rig, monkeypatch):
    monkeypatch.setattr(taskflows, "_CREATION_WORKERS", {object() for _ in range(32)})
    result, _ = await invoke(rig)
    assert result["error_code"] == "task_runtime_busy"
    assert rig[0].taskflows.list_flows() == []
    monkeypatch.undo()


async def test_thread_creation_wakes_active_runner_only_on_owning_loop(rig, monkeypatch):
    runtime = rig[0].taskflows
    loop_thread = threading.get_ident()
    observed = []
    original = runtime._wake_event.set
    def set_event():
        observed.append(threading.get_ident())
        original()
    monkeypatch.setattr(runtime._wake_event, "set", set_event)
    await runtime.start()
    flow = await asyncio.to_thread(runtime.create_flow, session_id="", title="wake fixture", steps=[{"type": "noop"}])
    async def completed():
        while runtime.get_flow(flow["id"])["status"] != "completed":
            await asyncio.sleep(0.005)
    await asyncio.wait_for(completed(), 2)
    assert observed and set(observed) == {loop_thread}


async def test_active_supervisor_keeps_intake_responsive_during_creation_contention(rig, monkeypatch):
    runtime = rig[0].taskflows
    await runtime.start()
    competing = sqlite3.connect(runtime._db_path, check_same_thread=False)
    competing.execute("BEGIN IMMEDIATE")
    entered = threading.Event()
    original = runtime.create_flow
    def observed_create(**kwargs):
        entered.set()
        return original(**kwargs)
    monkeypatch.setattr(runtime, "create_flow", observed_create)
    # An independent release bounds this regression even if the event loop stalls.
    release = threading.Timer(0.8, competing.commit)
    release.start()
    caller = asyncio.create_task(invoke(rig))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        runtime._wake_event.set()  # Force the actual supervisor to poll while creation is blocked.
        began = time.monotonic()
        gaps = []
        while time.monotonic() - began < 0.3:
            before = time.monotonic()
            await asyncio.sleep(0.01)
            gaps.append(time.monotonic() - before)
        assert max(gaps) < 0.15
        assert not caller.done()
        result, _ = await asyncio.wait_for(caller, 2)
        assert result["success"] is True
        assert runtime._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        await asyncio.to_thread(release.join)
        competing.close()
        if not caller.done():
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)


def creation(runtime, *, key="a" * 64, digest="b" * 64):
    return runtime.create_flow(session_id="", title="fixture", steps=[{"type": "llm.chat", "prompt": "one"},
        {"type": "llm.chat", "prompt": "two"}], context={"fixture": True}, handoff_key=key, terms_digest=digest)


async def test_two_runtime_instances_atomic_creation_and_reopen(rig):
    first = rig[0].taskflows
    second = TaskFlowRuntime(db_path=first._db_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(creation, runtime) for runtime in (first, second)]
            results = [f.result(timeout=5) for f in futures]
        assert results[0]["id"] == results[1]["id"]
        assert sorted(r["handoff_replayed"] for r in results) == [False, True]
        assert first._conn.execute("SELECT COUNT(*) FROM taskflow_steps").fetchone()[0] == 2
    finally:
        await second.stop()
        second._conn.close()
    reopened = TaskFlowRuntime(db_path=first._db_path)
    try:
        assert creation(reopened)["id"] == results[0]["id"]
        with pytest.raises(TaskFlowHandoffConflict):
            creation(reopened, digest="c" * 64)
    finally:
        await reopened.stop()
        reopened._conn.close()


async def test_failed_second_step_insert_rolls_back_flow_and_identity(rig):
    runtime = rig[0].taskflows
    runtime._conn.execute("CREATE TRIGGER inert_failure BEFORE INSERT ON taskflow_steps WHEN NEW.step_index = 1 BEGIN SELECT RAISE(ABORT, 'inert failure'); END")
    runtime._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        creation(runtime)
    assert runtime._conn.execute("SELECT COUNT(*) FROM taskflows").fetchone()[0] == 0
    assert runtime._conn.execute("SELECT COUNT(*) FROM taskflow_steps").fetchone()[0] == 0
    runtime._conn.execute("DROP TRIGGER inert_failure")
    runtime._conn.commit()
    assert creation(runtime)["handoff_replayed"] is False


async def test_old_schema_additive_migration_preserves_rows(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE taskflows (id TEXT PRIMARY KEY, session_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
        status TEXT NOT NULL, current_step INTEGER NOT NULL DEFAULT 0, context_json TEXT NOT NULL DEFAULT '{}', error TEXT,
        wait_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, completed_at REAL)""")
    conn.execute("INSERT INTO taskflows (id,title,status,created_at,updated_at) VALUES ('legacy','kept','queued',1,1)")
    conn.commit()
    conn.close()
    runtime = TaskFlowRuntime(db_path=path)
    try:
        assert runtime.get_flow("legacy")["title"] == "kept"
        assert creation(runtime)["handoff_replayed"] is False
        assert runtime._conn.execute("SELECT handoff_key FROM taskflows WHERE id='legacy'").fetchone()[0] is None
    finally:
        await runtime.stop()
        runtime._conn.close()


async def test_readback_failure_is_unknown_not_acceptance(rig, monkeypatch):
    original = rig[0].taskflows.get_flow
    monkeypatch.setattr(rig[0].taskflows, "get_flow", lambda _id: None)
    result, _ = await invoke(rig)
    assert result["error_code"] == "task_creation_outcome_unknown"
    assert "handoff" not in result["data"]
    monkeypatch.setattr(rig[0].taskflows, "get_flow", original)
    assert len(rig[0].taskflows.list_flows()) == 1


async def test_surface_is_preserved_at_existing_orchestrator_entry(rig):
    result, _ = await invoke(rig, surface="http_api")
    runtime = rig[0].taskflows
    runtime._orchestrator = SimpleNamespace(handle_command=AsyncMock(return_value="inert response"))
    flow = runtime.get_flow(result["data"]["flow_id"])
    step_result = await runtime._execute_step(flow, flow["steps"][0])
    assert step_result["status"] == "completed"
    runtime._orchestrator.handle_command.assert_awaited_once()
    call = runtime._orchestrator.handle_command.await_args
    assert call.args[0] == f"taskflow-{flow['id']}" and call.kwargs == {"context": {"surface": "http_api"}}
    assert '"evidence_kind": "accepted_task_goal"' in call.args[1] and '"text": "inert research"' in call.args[1]
    assert call.args[1].endswith("Current requested step:\ninert research")


async def test_background_handoff_keeps_actual_http_surface_hard_deny(rig):
    from agents.orchestrator import Orchestrator
    from agents.tool_runner import ToolRunner
    from security.exec_approvals import ApprovalManager
    from skills.registry import SkillRegistry
    runtime = rig[0].taskflows
    orch = Orchestrator.__new__(Orchestrator)
    orch.skills = SkillRegistry()
    orch._session_surfaces = {}
    orch._active_turns = {}
    orch._mcp_client = None
    runner = ToolRunner(orch, approval_manager=ApprovalManager())
    actual_verdicts = []

    async def handle_command(session_id, text, context=None):
        orch._stamp_session_surface(session_id, context)
        surface = runner._resolve_surface_for_session(session_id)
        actual_verdicts.append(runner.enforce_safety("desktop_control__shell_command",
                                                   {"command": "inert fixture"}, session_id, surface))
        return "inert policy result"
    runtime._orchestrator = SimpleNamespace(handle_command=handle_command)
    started, _ = await invoke(rig, surface="http_api")
    flow = runtime.get_flow(started["data"]["flow_id"])
    await runtime._execute_step(flow, flow["steps"][0])
    assert actual_verdicts[0]["status"] == "PermissionOutcome::Deny"
    assert "surface" in actual_verdicts[0]["error"].lower()


async def test_raw_rest_context_cannot_activate_tracked_surface(rig):
    runtime = rig[0].taskflows
    runtime._orchestrator = SimpleNamespace(handle_command=AsyncMock(return_value="never"))
    fake_origin = {"contract_version": 1, "source": "tracked_chat_turn", "surface": "local_cli",
                   "handoff_key": "a" * 64, "terms_digest": "b" * 64}
    flow = await taskflows.create_taskflow({"steps": [{"type": "llm.chat", "prompt": "inert"}],
                                           "context": {"task_origin": fake_origin}})
    result = await runtime._execute_step(flow, flow["steps"][0])
    assert result["status"] == "failed" and result["dispatch_started"] is False
    runtime._orchestrator.handle_command.assert_not_awaited()


@pytest.mark.parametrize("surface", ["http_api", "phone_actuator", "voice_realtime"])
async def test_untracked_agent_preserves_known_surface_or_refuses(rig, surface):
    from security.dangerous_tools import known_surfaces
    with bind_context(session_id="headless-thread", tool_name="background_task__start", surface=surface, call_id="legacy-call"):
        result = await rig[2].execute("start", {"goal": "headless fixture"}, {})
    runtime = rig[0].taskflows
    if surface not in known_surfaces():
        assert result["error_code"] == "task_origin_unavailable"
        assert runtime.list_flows() == []
        return
    assert result["success"] is True
    origin = result["data"]["handoff"]["origin"]
    assert origin["owner_verified"] is False and origin["surface"] == surface
    flow = runtime.get_flow(result["data"]["flow_id"])
    assert runtime.origin_surface_for_flow(flow["id"]) == surface
    from agents.orchestrator import Orchestrator
    from agents.tool_runner import ToolRunner
    from security.exec_approvals import ApprovalManager
    from skills.registry import SkillRegistry
    orch = Orchestrator.__new__(Orchestrator)
    orch.skills = SkillRegistry()
    orch._session_surfaces, orch._active_turns, orch._mcp_client = {}, {}, None
    runner = ToolRunner(orch, approval_manager=ApprovalManager())
    verdicts = []
    async def actual_policy(session_id, prompt, context):
        orch._stamp_session_surface(session_id, context)
        verdicts.append(runner.enforce_safety("desktop_control__shell_command", {"command": "inert"},
                                             session_id, runner._resolve_surface_for_session(session_id)))
        return "inert denied action"
    runtime._orchestrator = SimpleNamespace(handle_command=AsyncMock(side_effect=actual_policy))
    await runtime._execute_step(flow, flow["steps"][0])
    runtime._orchestrator.handle_command.assert_awaited_once()
    call = runtime._orchestrator.handle_command.await_args
    assert call.args[0] == f"taskflow-{flow['id']}" and call.kwargs == {"context": {"surface": surface}}
    assert '"evidence_kind": "accepted_task_goal"' in call.args[1] and '"text": "headless fixture"' in call.args[1]
    assert call.args[1].endswith("Current requested step:\nheadless fixture")
    assert verdicts[0]["status"] == "PermissionOutcome::Deny"


async def test_raw_rest_legacy_agent_context_cannot_mint_surface_column(rig):
    runtime = rig[0].taskflows
    runtime._orchestrator = SimpleNamespace(handle_command=AsyncMock(return_value="never"))
    flow = await taskflows.create_taskflow({"steps": [{"type": "llm.chat", "prompt": "inert"}],
        "context": {"task_origin": {"contract_version": 1, "source": "legacy_agent_context", "surface": "local_cli", "session_id": None}}})
    assert (await runtime._execute_step(flow, flow["steps"][0]))["dispatch_started"] is False
    runtime._orchestrator.handle_command.assert_not_awaited()


async def test_legacy_flow_execution_signature_preserved(rig):
    runtime = rig[0].taskflows
    runtime._orchestrator = SimpleNamespace(handle_command=AsyncMock(return_value="inert"))
    flow = runtime.create_flow(session_id="legacy", title="fixture", steps=[{"type": "llm.chat", "prompt": "read"}])
    assert (await runtime._execute_step(flow, flow["steps"][0]))["status"] == "completed"
    runtime._orchestrator.handle_command.assert_awaited_once_with("legacy", "read")


async def test_public_executor_uses_registered_adapter_and_retains_policy_gate(rig):
    from models.skill_manifest import SkillManifest
    from skills.executor import SkillExecutor
    from pathlib import Path
    manifest = SkillManifest.model_validate(json.loads((Path(__file__).parents[1] / "skills/manifests/task.json").read_text()))
    endpoint = next(e for e in manifest.endpoints if e.id == "start")
    executor = SkillExecutor()
    try:
        # Fail any HTTP attempt: native-port independence is actual dispatch evidence.
        executor.client.request = AsyncMock(side_effect=AssertionError("HTTP must not be used"))
        with bind_context(session_id="legacy-operator", surface="local_cli", tool_name="background_task__start"):
            result = await executor.execute("background_task__start", {"goal": "inert"}, manifest, endpoint)
        assert result["success"] is True
        executor.client.request.assert_not_awaited()
        async def tracked_execute(_skill):
            return await executor.execute("background_task__start", {"goal": "tracked inert"}, manifest, endpoint)
        tracked, receipt = await invoke(rig, callback=tracked_execute)
        assert tracked["data"]["handoff"]["origin"]["request_id"] == receipt["request_id"]
        assert tracked["data"]["handoff"]["replay_protected"] is True
        class Gate:
            def enforce_plan_mode(self, *_):
                return {"success": False, "error_code": "plan_mode_blocked"}
        rig[0].tool_runner = Gate()
        refusal = await executor.execute("background_task__start", {"goal": "blocked"}, manifest, endpoint)
        assert refusal["error_code"] == "plan_mode_blocked"
        assert len(rig[0].taskflows.list_flows()) == 2
    finally:
        await executor.client.aclose()


@pytest.mark.parametrize("job_failed", [False, True])
async def test_public_status_read_success_is_distinct_from_job_outcome(rig, job_failed):
    from pathlib import Path
    from models.skill_manifest import SkillManifest
    from skills.executor import SkillExecutor

    started, _ = await invoke(rig)
    flow_id = started["data"]["flow_id"]
    runtime = rig[0].taskflows
    if job_failed:
        # The real runner cannot execute an llm.chat step without an
        # orchestrator. Its persisted job failure is data, not a failed read.
        await runtime.start()
        deadline = time.monotonic() + 2
        while runtime.get_flow(flow_id)["status"] != "failed":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        await runtime.stop()
    expected = runtime.get_flow(flow_id)
    manifest = SkillManifest.model_validate(json.loads(
        (Path(__file__).parents[1] / "skills/manifests/task.json").read_text()))
    endpoint = next(e for e in manifest.endpoints if e.id == "status")
    executor = SkillExecutor()
    try:
        executor.client.request = AsyncMock(side_effect=AssertionError("HTTP must not be used"))
        async def read(_skill):
            return await executor.execute("background_task__status", {"flow_id": flow_id}, manifest, endpoint)
        result, _ = await invoke(rig, endpoint="status", callback=read)
        assert result["success"] is True and result["status_code"] == 200
        assert result["error"] is None
        assert result["data"]["ok"] is True
        assert result["data"]["status"] == expected["status"]
        assert result["data"]["error"] == expected["error"]
        assert bool(result["data"]["error"]) is job_failed
        assert result["data"]["origin"] == started["data"]["handoff"]["origin"]
        assert result["data"]["action_outcome"] == "not_asserted"
        executor.client.request.assert_not_awaited()
    finally:
        await executor.client.aclose()


def test_registered_task_adapter_declares_and_validates_manifest_endpoints():
    from agents.tool_dispatch_validator import ToolDispatchValidator
    validator = ToolDispatchValidator()
    calls = {"start": {"goal": "inert research"}, "status": {"flow_id": "inert-flow"}, "list": {"limit": 10}}
    for endpoint, args in calls.items():
        schema = validator.get_endpoint_schema("background_task", endpoint)
        assert schema is not None and schema.dispatch_keys == set(calls)
        assert validator.contract_violations("background_task", endpoint) == []
        assert validator.validate("background_task", endpoint, args).ok is True
    assert validator.validate("background_task", "unsupported", {}).ok is False


async def test_task_adapter_unknown_endpoint_refuses_without_delegation(rig, monkeypatch):
    delegate = AsyncMock(side_effect=AssertionError("Unsupported endpoint must not reach the task control plane"))
    monkeypatch.setattr(taskflows, "execute_background_task_skill", delegate)
    result = await rig[2].execute("unsupported", {}, {})
    assert result["success"] is False and result["status_code"] == 409
    assert result["error_code"] == result["data"]["error_code"] == "task_invalid_endpoint"
    assert result["data"]["ok"] is False
    delegate.assert_not_awaited()
