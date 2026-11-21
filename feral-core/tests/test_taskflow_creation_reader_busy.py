"""SQLite reader contention never duplicates creation or bypasses its guard."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time

import pytest

from agents.taskflow import TaskFlowRuntime


def test_creation_commit_busy_retries_after_reader_releases(tmp_path):
    runtime = TaskFlowRuntime(db_path=str(tmp_path / "flows.db"))
    first = runtime.create_flow(session_id="", title="first", steps=[{"type": "noop"}])
    reader = sqlite3.connect(runtime._db_path, timeout=0)
    reader.execute("BEGIN")
    reader.execute("SELECT id FROM taskflows").fetchall()
    guards = []
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                runtime.create_flow,
                session_id="",
                title="second",
                steps=[{"type": "noop"}],
                creation_guard=lambda: guards.append(time.monotonic()),
            )
            try:
                time.sleep(0.15)
                assert not result.done(), (
                    "Reader must delay COMMIT, without failing admission"
                )
            finally:
                reader.rollback()
            second = result.result(timeout=2)
        assert first["id"] != second["id"]
        assert len(runtime.list_flows()) == 2
        assert len(second["steps"]) == 1
        assert len(guards) > 3
    finally:
        reader.close()
        asyncio.run(runtime.stop())
        runtime._conn.close()


def test_revoked_creation_during_commit_retry_never_commits(tmp_path):
    runtime = TaskFlowRuntime(db_path=str(tmp_path / "flows.db"))
    runtime.create_flow(session_id="", title="existing", steps=[{"type": "noop"}])
    reader = sqlite3.connect(runtime._db_path, timeout=0)
    reader.execute("BEGIN")
    reader.execute("SELECT id FROM taskflows").fetchall()
    alive = {"value": True}

    def guard():
        if not alive["value"]:
            raise ValueError("Creation ownership revoked")

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                runtime.create_flow,
                session_id="",
                title="revoked",
                steps=[{"type": "noop"}],
                creation_guard=guard,
            )
            time.sleep(0.1)
            alive["value"] = False
            reader.rollback()
            with pytest.raises(ValueError, match="ownership revoked"):
                result.result(timeout=2)
        assert len(runtime.list_flows()) == 1
        assert runtime._conn.in_transaction is False
    finally:
        reader.close()
        asyncio.run(runtime.stop())
        runtime._conn.close()
