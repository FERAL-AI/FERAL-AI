"""Real SQLite/gateway task-result reads; all execution collaborators inert."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from agents.taskflow import TaskFlowRuntime
from api.routes.taskflows import _start_task
from gateway.protocol import GatewaySession, MethodRegistry, _TASK_RECEIPT_WORKERS, register_core_methods


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    runtime = TaskFlowRuntime(db_path=str(tmp_path / "tasks.db"))
    ws = SimpleNamespace(send_json=AsyncMock())
    brain = SimpleNamespace(taskflows=runtime, sessions={"origin-A": ws}, memory=None, orchestrator=None)
    monkeypatch.setattr("api.state.state", brain)
    registry = MethodRegistry()
    register_core_methods(registry, brain)
    session = GatewaySession("origin-A", ws, registry)
    yield runtime, brain, session
    if _TASK_RECEIPT_WORKERS:
        await asyncio.wait_for(asyncio.gather(*_TASK_RECEIPT_WORKERS, return_exceptions=True), 3)
    await runtime.stop()
    runtime._conn.close()


def create(runtime, *, sid="origin-A", goal="inert research", subtasks=None):
    origin = {"contract_version": 1, "source": "tracked_chat_turn", "owner_verified": True,
              "session_id": sid, "request_id": str(uuid4()), "turn_id": str(uuid4()),
              "tool_call_id": "fixture-call", "surface": "http_api", "input_revision": None,
              "context_commit": "pending"}
    body = {"goal": goal, **({"subtasks": subtasks} if subtasks is not None else {})}
    result = _start_task(body, origin, lambda: None, runtime)
    assert result["ok"], result
    return result["flow_id"]


def record(runtime, flow_id, output="recorded nonce", *, status="completed", step_status="completed"):
    with runtime._lock:
        runtime._conn.execute("UPDATE taskflows SET status=?,current_step=1,updated_at=123456.5 WHERE id=?",
                              (status, flow_id))
        runtime._conn.execute("UPDATE taskflow_steps SET status=?,result_json=?,finished_at=123456.0 WHERE flow_id=?",
                              (step_status, json.dumps({"status": step_status, "output": output}), flow_id))
        runtime._conn.commit()


async def rpc(session, method="task.receipt", params=None):
    req_id = str(uuid4())
    await session.handle_message({"type": "req", "id": req_id, "method": method,
                                  "params": params or {"contract_version": 1}})
    response = session._ws.send_json.await_args.args[0]
    assert response["id"] == req_id
    return response


async def receipt(session, flow_id):
    return await rpc(session, params={"contract_version": 1, "flow_id": flow_id})


async def test_actual_gateway_returns_persisted_result_and_no_execution(rig):
    runtime, brain, session = rig
    flow_id = create(runtime)
    orch = runtime._orchestrator = SimpleNamespace(handle_command=AsyncMock(return_value="inert nonce"))
    await runtime._run_flow(flow_id)
    assert orch.handle_command.await_count == 1
    result = await receipt(session, flow_id)
    data = result["payload"]["receipt"]
    assert data["result_text"] == "inert nonce" and data["processing_outcome"] == "completed"
    assert data["action_outcome"] == "not_asserted" and data["origin_session_id"] == "origin-A"
    assert data["result_present"] and data["result_step_index"] == 0
    assert "request_id" not in data and "task_origin" not in data
    assert orch.handle_command.await_count == 1
    capabilities = await rpc(session, "chat.capabilities")
    assert capabilities["payload"]["task_result_contract_versions"] == [1]
    filename = os.environ.get("FERAL_TASK_RESULT_FIXTURE")
    if filename:
        Path(filename).write_text(json.dumps({"capabilities": capabilities, "result": result}, indent=2) + "\n")


async def test_other_origin_and_raw_operator_flow_are_not_found(rig):
    runtime, _, session = rig
    foreign = create(runtime, sid="origin-B")
    raw = runtime.create_flow(session_id="origin-A", title="raw", steps=[{"type": "noop"}],
                              context={"task_origin": {"session_id": "origin-A"}})
    for flow_id in (foreign, raw["id"], "absent"):
        data = (await receipt(session, flow_id))["payload"]
        assert data["found"] is False and data["receipt"] is None


@pytest.mark.parametrize("value,expected", [("", ""), (False, "false"), (0, "0"), ([], "[]"), ({}, "{}"), (None, "null")])
async def test_recorded_falsy_presence_survives(rig, value, expected):
    runtime, _, session = rig
    flow_id = create(runtime)
    record(runtime, flow_id, value)
    data = (await receipt(session, flow_id))["payload"]["receipt"]
    assert data["result_present"] is True and data["result_text"] == expected
    assert data["result_truncated"] is False


async def test_absent_output_is_not_empty_success(rig):
    runtime, _, session = rig
    data = (await receipt(session, create(runtime)))["payload"]["receipt"]
    assert data["status"] == "queued" and data["processing_outcome"] == "in_progress"
    assert data["result_present"] is False and data["result_text"] is None and data["result_step_index"] is None


@pytest.mark.parametrize("status,step_status,outcome", [("failed", "failed", "failed"),
    ("cancelled", "completed", "cancelled"), ("waiting", "outcome_unknown", "outcome_unknown"),
    ("cancelled", "outcome_unknown", "outcome_unknown")])
async def test_failure_cancel_unknown_remain_distinct(rig, status, step_status, outcome):
    runtime, _, session = rig
    flow_id = create(runtime)
    record(runtime, flow_id, status=status, step_status=step_status)
    data = (await receipt(session, flow_id))["payload"]["receipt"]
    assert data["processing_outcome"] == outcome and data["action_outcome"] == "not_asserted"


async def test_utf8_bounded_result_and_deterministic_reopen(rig):
    runtime, brain, session = rig
    flow_id = create(runtime)
    record(runtime, flow_id, "🐾" * 2000)
    first = (await receipt(session, flow_id))["payload"]["receipt"]
    assert len(first["result_text"].encode()) == 4096 and first["result_truncated"]
    assert (await receipt(session, flow_id))["payload"]["receipt"] == first
    digest = first.pop("result_digest")
    assert hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == digest
    reopened = TaskFlowRuntime(db_path=runtime._db_path)
    try:
        brain.taskflows = reopened
        second = (await receipt(session, flow_id))["payload"]["receipt"]
        assert second["result_digest"] == digest
    finally:
        brain.taskflows = runtime
        await reopened.stop()
        reopened._conn.close()


@pytest.mark.parametrize("damage", ["origin", "handoff", "terms", "prompt", "missing", "surface"])
async def test_forged_or_mutated_durable_identity_is_not_found(rig, damage):
    runtime, _, session = rig
    flow_id = create(runtime)
    row = runtime.get_flow(flow_id)
    with runtime._lock:
        if damage == "handoff":
            runtime._conn.execute("UPDATE taskflows SET handoff_key=? WHERE id=?", ("a" * 64, flow_id))
        elif damage == "terms":
            runtime._conn.execute("UPDATE taskflows SET terms_digest=? WHERE id=?", ("b" * 64, flow_id))
        elif damage == "prompt":
            runtime._conn.execute("UPDATE taskflow_steps SET payload_json=? WHERE flow_id=?", ('{"prompt":"forged"}', flow_id))
        else:
            context = row["context"]
            if damage == "missing":
                context.pop("task_origin")
            elif damage == "origin":
                context["task_origin"]["session_id"] = "origin-B"
            else:
                context["task_origin"]["surface"] = "cron"
            runtime._conn.execute("UPDATE taskflows SET context_json=? WHERE id=?", (json.dumps(context), flow_id))
        runtime._conn.commit()
    data = (await receipt(session, flow_id))["payload"]
    assert data["found"] is False


async def test_equal_time_pagination_advances_past_invalid_row(rig):
    runtime, _, session = rig
    ids = [create(runtime) for _ in range(5)]
    with runtime._lock:
        runtime._conn.execute("UPDATE taskflows SET created_at=100")
        bad = sorted(ids)[2]
        runtime._conn.execute("UPDATE taskflows SET context_json='{}' WHERE id=?", (bad,))
        runtime._conn.commit()
    cursor, found, cursors = None, [], []
    for _ in range(5):
        page = (await rpc(session, "task.receipts", {"contract_version": 1, "limit": 1, "cursor": cursor}))["payload"]
        found.extend(r["flow_id"] for r in page["receipts"])
        cursor = page["next_cursor"]
        cursors.append(cursor["flow_id"])
        if not page["has_more"]:
            break
    assert cursors == sorted(ids) and found == sorted(set(ids) - {bad})


@pytest.mark.parametrize("params", [{"contract_version": True}, {"contract_version": 1, "session_id": "origin-B"},
    {"contract_version": 1, "limit": 0}, {"contract_version": 1, "limit": True},
    {"contract_version": 1, "cursor": {"created_at": float("nan"), "flow_id": "x"}},
    {"contract_version": 1, "cursor": {"created_at": True, "flow_id": "x"}},
    {"contract_version": 1, "cursor": {"created_at": 1e100, "flow_id": "x"}},
    {"contract_version": 1, "cursor": {"created_at": 10 ** 1000, "flow_id": "x"}},
    {"contract_version": 1, "cursor": {"created_at": 0, "flow_id": "x", "owner": "x"}},
    {"contract_version": 1, "cursor": {"created_at": 0, "flow_id": "x\n"}}])
async def test_invalid_caller_scope_and_cursor_refuse(rig, params):
    response = await rpc(rig[2], "task.receipts", params)
    assert response["ok"] is False and response["error"]["code"] == "INVALID_PARAMS"


async def test_subtask_empty_original_goal_validates(rig):
    runtime, _, session = rig
    flow_id = create(runtime, goal="", subtasks=["one", "two"])
    data = (await receipt(session, flow_id))["payload"]
    assert data["found"] is True and data["receipt"]["steps_total"] == 2


@pytest.mark.parametrize("field,value", [("created_at", -1), ("created_at", 1e100),
    ("created_at", "invalid"), ("updated_at", -1), ("current_step", 33)])
async def test_invalid_persisted_projection_never_issues_invalid_wire(rig, field, value):
    runtime, _, session = rig
    flow_id = create(runtime)
    with runtime._lock:
        runtime._conn.execute(f"UPDATE taskflows SET {field}=? WHERE id=?", (value, flow_id))
        runtime._conn.commit()
    response = await receipt(session, flow_id)
    if field == "created_at":
        assert response["ok"] is False and response["error"]["code"] == "task_result_unavailable"
    else:
        assert response["payload"]["found"] is False


@pytest.mark.parametrize("method,kwargs", [("read_origin_receipts", {"limit": -1}),
    ("read_origin_receipts", {"limit": True}), ("read_origin_receipts", {"cursor": {"created_at": True, "flow_id": "x"}}),
    ("read_origin_receipt", {"flow_id": "x\x7f"})])
async def test_runtime_helpers_also_reject_unbounded_inputs(rig, method, kwargs):
    from agents.taskflow import TaskFlowReceiptError
    with pytest.raises(TaskFlowReceiptError):
        getattr(rig[0], method)("origin-A", **kwargs)


@pytest.mark.parametrize("replacement", ["owner", "runtime", "state"])
async def test_owner_or_runtime_replaced_during_read_refuses(rig, monkeypatch, replacement):
    runtime, brain, session = rig
    flow_id = create(runtime)
    entered, release = threading.Event(), threading.Event()
    original = runtime.read_origin_receipt
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime, "read_origin_receipt", delayed)
    task = asyncio.create_task(receipt(session, flow_id))
    await asyncio.wait_for(asyncio.to_thread(entered.wait), 2)
    if replacement == "owner":
        brain.sessions["origin-A"] = object()
    elif replacement == "runtime":
        brain.taskflows = None
    else:
        monkeypatch.setattr("api.state.state", SimpleNamespace())
    release.set()
    response = await task
    assert response["ok"] is False and response["error"]["code"] == "task_result_owner_unavailable"


async def test_exclusive_sql_lock_cancel_drains_without_blocking_loop_or_runner_mutex(rig):
    runtime, _, session = rig
    flow_id = create(runtime)
    writer = sqlite3.connect(runtime._db_path)
    writer.execute("BEGIN EXCLUSIVE")
    task = asyncio.create_task(receipt(session, flow_id))
    try:
        gaps, previous = [], time.monotonic()
        for _ in range(15):
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now
        assert max(gaps) < 0.15 and not task.done()
        assert runtime._lock.acquire(blocking=False)
        runtime._lock.release()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(50):
            if not _TASK_RECEIPT_WORKERS:
                break
            await asyncio.sleep(0.01)
        assert not _TASK_RECEIPT_WORKERS and session._ws.send_json.await_count == 0
    finally:
        writer.rollback()
        writer.close()
    assert runtime._conn.execute("SELECT COUNT(*) FROM taskflows").fetchone()[0] == 1


async def test_closed_database_error_redacted(rig):
    runtime, _, session = rig
    flow_id = create(runtime)
    original = runtime._db_path
    runtime._db_path = original + "-missing"
    try:
        response = await receipt(session, flow_id)
        assert response["error"]["code"] == "task_result_unavailable"
        assert original not in json.dumps(response)
    finally:
        runtime._db_path = original
