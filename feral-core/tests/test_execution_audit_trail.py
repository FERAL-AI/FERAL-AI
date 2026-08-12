"""The tool-execution audit trail must cover every dispatch path.

Live evidence this pins (``~/.feral/memory.db``, read 2026-08-12):

    execution_log rows          206
    last row                    2026-05-21 15:16:47
    episodes(event_type=tool)   33 rows dated 2026-06-30 .. 2026-08-06

Every one of those 33 tool calls really executed, through
``voice/realtime_proxy.py``. None produced an audit row, because
``execution_log`` had exactly one writer: ``Orchestrator``, in its two
chat loops. The trail did not break, it was never connected to the
paths the traffic moved to.

These tests fail against the unfixed tree because ``SkillExecutor``
wrote nothing at all.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.execution_audit import (  # noqa: E402
    caller_has_claimed,
    claimed_by_caller,
    record_execution,
    status_of,
)
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest  # noqa: E402
from skills.call_context import bind_context  # noqa: E402
from skills.executor import SkillExecutor  # noqa: E402


class _RecordingStore:
    """Stands in for MemoryStore, keeping the real log_execution signature."""

    def __init__(self):
        self.rows: list[dict] = []

    async def log_execution(
        self, session_id: str, skill_id: str, endpoint_id: str, args: dict,
        result_status: str, result_summary: str = "", latency_ms: float = 0,
    ) -> str:
        self.rows.append({
            "session_id": session_id,
            "skill_id": skill_id,
            "endpoint_id": endpoint_id,
            "args": args,
            "result_status": result_status,
            "result_summary": result_summary,
            "latency_ms": latency_ms,
        })
        return f"row-{len(self.rows)}"


@pytest.fixture
def store(monkeypatch):
    """Install a fake ``api.state`` carrying a recording memory store.

    ``memory.execution_audit`` reaches the store through ``sys.modules``
    exactly like ``SkillExecutor._gate`` reaches the ToolRunner, so a
    module object is the honest way to exercise it.
    """
    recording = _RecordingStore()
    fake_state = types.SimpleNamespace(memory=recording, tool_runner=None, orchestrator=None)
    fake_module = types.ModuleType("api.state")
    fake_module.state = fake_state  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "api.state", fake_module)
    return recording


def _manifest() -> tuple[SkillManifest, SkillEndpoint]:
    endpoint = SkillEndpoint(
        id="web_search",
        method="PYTHON",
        url="python://web_search",
        description="search",
    )
    manifest = SkillManifest(
        skill_id="web_search",
        version="1.0.0",
        description="search",
        brand=BrandProfile(name="Web Search"),
        endpoints=[endpoint],
    )
    return manifest, endpoint


@pytest.fixture
def executor(monkeypatch):
    ex = SkillExecutor()

    async def _fake_inner(tool_name, args, skill, endpoint):
        return {"success": True, "status_code": 200, "data": {"hits": 1}, "error": None}

    monkeypatch.setattr(ex, "_execute_inner", _fake_inner)
    return ex


# ---------------------------------------------------------------------------
# The gap itself
# ---------------------------------------------------------------------------

def test_executor_writes_an_audit_row(store, executor):
    """A tool call through SkillExecutor lands in execution_log.

    This is the whole finding. Before the fix the executor wrote nothing
    and this list stayed empty for voice, MCP, the REST tool route and
    multi-agent runs alike.
    """
    manifest, endpoint = _manifest()
    asyncio.run(executor.execute("web_search__web_search", {"query": "x"}, manifest, endpoint))
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row["skill_id"] == "web_search"
    assert row["endpoint_id"] == "web_search"
    assert row["result_status"] == "success"
    assert row["args"] == {"query": "x"}


def test_audit_row_carries_the_bound_session(store, executor):
    """The voice fix: a bound context puts the real session on the row.

    Voice called the executor with no bound context, so even once a row
    existed it would have been attributed to session "" and been useless
    for "what did FERAL do in my session yesterday".
    """
    manifest, endpoint = _manifest()

    async def _run():
        with bind_context(session_id="voice-abc", surface="voice", tool_name="web_search__web_search"):
            await executor.execute("web_search__web_search", {"query": "x"}, manifest, endpoint)

    asyncio.run(_run())
    assert store.rows[0]["session_id"] == "voice-abc"


def test_a_failing_tool_is_recorded_as_failure(store, executor, monkeypatch):
    async def _fail(tool_name, args, skill, endpoint):
        return {"success": False, "status_code": 503, "data": None, "error": "nope"}

    monkeypatch.setattr(executor, "_execute_inner", _fail)
    manifest, endpoint = _manifest()
    asyncio.run(executor.execute("web_search__web_search", {"query": "x"}, manifest, endpoint))
    assert store.rows[0]["result_status"] == "failure"


def test_a_raising_tool_still_leaves_a_row(store, executor, monkeypatch):
    """An exception out of a skill must not also erase the audit trail."""

    async def _boom(tool_name, args, skill, endpoint):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(executor, "_execute_inner", _boom)
    manifest, endpoint = _manifest()
    with pytest.raises(RuntimeError):
        asyncio.run(executor.execute("web_search__web_search", {"query": "x"}, manifest, endpoint))
    assert len(store.rows) == 1
    assert store.rows[0]["result_status"] == "failure"


# ---------------------------------------------------------------------------
# Exactly-once: the orchestrator claims, the executor stands down
# ---------------------------------------------------------------------------

def test_a_claimed_call_is_not_written_twice(store, executor):
    """The chat path logs its own row, including for tools that never
    reach the executor (mcp_*, daemon_*, subagent). The claim is what
    stops that becoming two rows for one call."""
    manifest, endpoint = _manifest()

    async def _run():
        with claimed_by_caller():
            await executor.execute("web_search__web_search", {"query": "x"}, manifest, endpoint)

    asyncio.run(_run())
    assert store.rows == []


def test_the_claim_does_not_leak_out_of_its_block():
    assert caller_has_claimed() is False
    with claimed_by_caller():
        assert caller_has_claimed() is True
    assert caller_has_claimed() is False


def test_the_claim_reaches_tasks_created_inside_it(store, executor):
    """The orchestrator claims, then fans out over asyncio.gather.

    A Task copies the current context at creation, so the claim has to
    hold inside the children or the parallel branches each write a
    duplicate row.
    """
    manifest, endpoint = _manifest()

    async def _run():
        with claimed_by_caller():
            await asyncio.gather(*[
                executor.execute("web_search__web_search", {"q": i}, manifest, endpoint)
                for i in range(3)
            ])

    asyncio.run(_run())
    assert store.rows == []


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

def test_pending_approval_is_not_a_failure():
    """Three consecutive workspace_scripts__rerun rows in the live store
    carry identical args, distinct request_ids and result_status
    'failure'. The tool did not fail: FERAL asked the operator a
    question and the model, told it had failed, asked again."""
    envelope = {
        "status": "pending_approval",
        "tool_name": "workspace_scripts__rerun",
        "request_id": "abc",
    }
    assert status_of(envelope) == "pending_approval"


def test_hardware_daemon_ack_is_a_success():
    assert status_of({"status": "command_sent_to_hardware_daemon"}) == "success"


def test_success_and_failure_classify_normally():
    assert status_of({"success": True}) == "success"
    assert status_of({"success": False, "error": "x"}) == "failure"


# ---------------------------------------------------------------------------
# Failing to record must be loud, never silent
# ---------------------------------------------------------------------------

def test_a_store_that_raises_is_reported_not_swallowed(monkeypatch, caplog):
    class _Broken:
        async def log_execution(self, **kwargs):
            raise RuntimeError("disk full")

    fake_module = types.ModuleType("api.state")
    fake_module.state = types.SimpleNamespace(memory=_Broken())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "api.state", fake_module)
    monkeypatch.setattr("memory.execution_audit._warned", set())

    with caplog.at_level("WARNING", logger="feral.memory.execution_audit"):
        out = asyncio.run(record_execution(
            session_id="s", tool_name="a__b", args={}, result={"success": True},
        ))
    assert out is None
    assert "execution_log is" in caplog.text


def test_a_brain_with_no_memory_store_says_so(monkeypatch, caplog):
    fake_module = types.ModuleType("api.state")
    fake_module.state = types.SimpleNamespace(memory=None)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "api.state", fake_module)
    monkeypatch.setattr("memory.execution_audit._warned", set())

    with caplog.at_level("WARNING", logger="feral.memory.execution_audit"):
        asyncio.run(record_execution(
            session_id="s", tool_name="a__b", args={}, result={"success": True},
        ))
    assert "audit disabled" in caplog.text


def test_offline_tooling_stays_silent(monkeypatch, caplog):
    """No api.state at all is the CLI / tests / an embedder. Normal."""
    monkeypatch.delitem(sys.modules, "api.state", raising=False)
    monkeypatch.setattr("memory.execution_audit._warned", set())
    with caplog.at_level("WARNING", logger="feral.memory.execution_audit"):
        out = asyncio.run(record_execution(
            session_id="s", tool_name="a__b", args={}, result={"success": True},
        ))
    assert out is None
    assert caplog.text == ""
