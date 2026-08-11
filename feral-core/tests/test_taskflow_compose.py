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

    # Mirrors Orchestrator.handle_command(session_id, text, context).
    # Real callers already use text=; this double answered to prompt=.
    async def handle_command(self, session_id, text, context=None):
        self.prompts.append(text)
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


# ── Dotted context paths in templates ──────────────────────────────
#
# Templates could reference only `previous_output` and `step_N`, so a
# workflow had no way to read a value its trigger supplied.
# `workflows/meeting_recap.json` is built entirely on `{{ context.* }}`
# tokens, which matched nothing and were passed through as literal text.


def _render(text, context):
    rt = TaskFlowRuntime.__new__(TaskFlowRuntime)
    return TaskFlowRuntime._render_string(rt, text, context)


def test_dotted_context_path_is_substituted():
    out = _render("Recap of {{ context.meeting_topic }}", {"meeting_topic": "Q3 plan"})
    assert out == "Recap of Q3 plan"


def test_nested_dotted_path_walks_mappings():
    out = _render("{{ context.meeting.title }}", {"meeting": {"title": "Standup"}})
    assert out == "Standup"


def test_missing_context_path_renders_empty_not_the_literal_token():
    """A model handed `{{ context.foo }}` will try to interpret it."""
    out = _render("[{{ context.nope }}]", {"meeting_topic": "x"})
    assert out == "[]"


def test_dotted_path_does_not_reach_attributes():
    """Mappings only. Attribute walking would expose arbitrary objects."""

    class Sneaky:
        secret = "leaked"

    out = _render("{{ context.obj.secret }}", {"obj": Sneaky()})
    assert out == ""


def test_non_string_context_values_are_json_encoded():
    out = _render("{{ context.items }}", {"items": ["a", "b"]})
    assert out == '["a", "b"]'


def test_original_tokens_still_work():
    assert _render("{{ previous_output }}", {"previous_output": "prior"}) == "prior"
    ctx = {"step_results": {"2": {"output": "second"}}}
    assert _render("{{ step_2 }}", ctx) == "second"


def test_meeting_recap_workflow_templates_all_resolve():
    """The shipped workflow is the reason this exists; assert it works."""
    import re as _re
    from pathlib import Path as _Path

    path = _Path(__file__).resolve().parent.parent / "workflows" / "meeting_recap.json"
    tokens = set(_re.findall(r"\{\{[^}]*\}\}", path.read_text()))
    assert tokens, "meeting_recap.json has no templates; test is stale"

    context = {
        "meeting_id": "m-1",
        "meeting_topic": "Roadmap",
        "transcript": "we talked",
        "previous_output": "prior step",
    }
    for token in tokens:
        rendered = _render(token, context)
        assert "{{" not in rendered, f"{token} was not substituted"
        assert rendered, f"{token} resolved to empty against a full context"
