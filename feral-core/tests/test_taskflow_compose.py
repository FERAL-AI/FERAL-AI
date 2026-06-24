"""L4 — TaskFlowRuntime composition: templating, output capture, branch-jump."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest
import pytest_asyncio

from agents.taskflow import TaskFlowRuntime
from memory.store import MemoryStore


class _FakeOrchestrator:
    """Returns a deterministic reply so llm.chat output capture is testable."""

    def __init__(self, reply="HELLO_FROM_LLM"):
        self.reply = reply
        self.prompts = []

    async def handle_command(self, session_id, prompt, context=None):
        self.prompts.append(prompt)
        return self.reply


@pytest_asyncio.fixture
async def runtime():
    fd_mem, mem_path = tempfile.mkstemp(suffix=".db")
    os.close(fd_mem)
    fd_flow, flow_path = tempfile.mkstemp(suffix=".db")
    os.close(fd_flow)

    store = MemoryStore(db_path=mem_path)
    orch = _FakeOrchestrator()
    rt = TaskFlowRuntime(db_path=flow_path, memory_store=store, orchestrator=orch)
    await rt.start()
    yield rt, store, orch
    await rt.stop()
    os.unlink(mem_path)
    os.unlink(flow_path)


async def _wait_done(rt, flow_id, timeout=8):
    deadline = time.time() + timeout
    latest = None
    while time.time() < deadline:
        latest = rt.get_flow(flow_id)
        if latest and latest["status"] in ("completed", "failed", "cancelled"):
            return latest
        await asyncio.sleep(0.1)
    return latest


@pytest.mark.asyncio
async def test_three_step_chain_passes_output(runtime):
    rt, store, orch = runtime
    flow = rt.create_flow(
        session_id="chain",
        title="chain",
        steps=[
            {"type": "llm.chat", "prompt": "say hi"},
            {"type": "note.save", "content": "got: {{ previous_output }}"},
            {"type": "note.save", "content": "step0 was {{ step_0 }}"},
        ],
    )
    latest = await _wait_done(rt, flow["id"])
    assert latest["status"] == "completed"

    # llm.chat output captured into context as step 0's output.
    assert latest["context"]["step_results"]["0"]["output"] == "HELLO_FROM_LLM"

    # Downstream note.save steps consumed the templated value.
    notes_prev = await store.search("got: HELLO_FROM_LLM", limit=5)
    assert len(notes_prev) >= 1
    notes_step = await store.search("step0 was HELLO_FROM_LLM", limit=5)
    assert len(notes_step) >= 1


@pytest.mark.asyncio
async def test_condition_branch_jump_skips_then(runtime):
    rt, store, _ = runtime
    # go is falsy → else=2 → step1 (THEN_NOTE) must be skipped.
    flow = rt.create_flow(
        session_id="branch",
        title="branch",
        steps=[
            {"type": "condition", "field": "go", "op": "truthy", "then": 1, "else": 2},
            {"type": "note.save", "content": "THEN_NOTE_UNIQUE"},
            {"type": "note.save", "content": "ELSE_NOTE_UNIQUE"},
        ],
        context={"go": False},
    )
    latest = await _wait_done(rt, flow["id"])
    assert latest["status"] == "completed"

    else_notes = await store.search("ELSE_NOTE_UNIQUE", limit=5)
    assert len(else_notes) >= 1
    then_notes = await store.search("THEN_NOTE_UNIQUE", limit=5)
    assert len(then_notes) == 0


@pytest.mark.asyncio
async def test_prompt_template_alias(runtime):
    rt, store, orch = runtime
    flow = rt.create_flow(
        session_id="alias",
        title="alias",
        steps=[
            {"type": "llm.chat", "prompt": "produce a value"},
            {"type": "llm.chat", "prompt_template": "refine: {{ previous_output }}"},
        ],
    )
    latest = await _wait_done(rt, flow["id"])
    assert latest["status"] == "completed"
    # prompt_template was rendered + aliased to prompt and sent to the LLM.
    assert any("refine: HELLO_FROM_LLM" == p for p in orch.prompts)
