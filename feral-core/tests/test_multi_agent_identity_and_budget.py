"""The multi-agent path must know who it is, what day it is, and when to stop.

Every default turn routes through multi-agent ("Multi-agent routing:
['general'] (single)"), and ``handle_command``'s multi-agent branch
returns before the single-agent path builds an identity prompt. Two
defects followed from that, both observed on the audited install:

  * the worker's whole system prompt was its ~270-token role prompt plus
    an Environment and a Memory block. No agent name, no personality,
    none of the operator's IDENTITY rules, no SOUL.md, no MEMORY.md, no
    About-Me and no clock, and no conversation history replayed either.
    The assistant answered "I've scheduled a reminder for your design
    review meeting at 10 AM PST on October 4, 2023" on a brain running
    in 2026 with no such routine, and could not recall the prior turn.

  * a cost cap returns ``{"error": ..., "choices": [],
    "budget_exceeded": {...}}``, and this path ran that error string
    through ``extract_response`` and shipped it as the reply. The
    operator's chat showed "budget exceeded for chat: $9.992715 /
    $10.000000 (hour, resets at 1788541200)" in an assistant bubble,
    pushed to working memory and saved to the transcript.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.context_manager import ContextManager
from agents.identity_loader import IdentityLoader
from agents.multi_agent import AgentWorker, MultiAgentOrchestrator
from config.loader import feral_home

IDENTITY_YAML = """
name: "Jarvis"
tagline: "Test identity."
personality: |
  You are dry and precise.
rules:
  - Never make up sensor data or health readings. Only report what's actually connected.
  - If a tool call fails, explain what went wrong in plain language.
"""


class _Orchestrator:
    """The pieces of the real orchestrator a worker actually reaches."""

    def __init__(self, history=None):
        self.identity_loader = IdentityLoader()
        self.conversation_history = dict(history or {})
        self.context_manager = ContextManager(max_messages=15)

    def _compact_context(self, history):
        return self.context_manager.compact(history)


@pytest.fixture
def identity_file():
    path = feral_home() / "identity.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(IDENTITY_YAML)
    return path


def _llm(reply="ok", response=None):
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value=response if response is not None else {"choices": []})
    llm.extract_response = MagicMock(return_value=(reply, []))
    return llm


def _sent_messages(llm):
    return llm.chat.call_args[1]["messages"]


# ── Identity on the general path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_general_worker_prompt_carries_name_rules_and_today(identity_file):
    llm = _llm()
    worker = AgentWorker(
        "general", "General Assistant", "ROLE PROMPT", [],
        llm=llm, orchestrator=_Orchestrator(),
    )
    await worker.run("s1", "what's up")

    prompt = _sent_messages(llm)[0]["content"]
    assert "You are Jarvis." in prompt
    assert "Never make up sensor data or health readings" in prompt
    assert "## Current Time" in prompt
    # The real year, from the host clock. The fabricated answer this
    # guards claimed October 2023 because no clock reached the model.
    assert str(datetime.now().year) in prompt
    # The role prompt is still there; identity is added, not substituted.
    assert "ROLE PROMPT" in prompt


@pytest.mark.asyncio
async def test_static_identity_precedes_volatile_time_and_memory(identity_file):
    """Prompt-cache ordering: stable prefix first, clock and memory last."""
    llm = _llm()
    memory = MagicMock()
    memory.build_context_for_llm = AsyncMock(return_value="recent: something")
    worker = AgentWorker(
        "general", "General Assistant", "ROLE PROMPT", [],
        llm=llm, memory=memory, orchestrator=_Orchestrator(),
    )
    await worker.run("s1", "hi")

    prompt = _sent_messages(llm)[0]["content"]
    assert prompt.index("You are Jarvis.") < prompt.index("ROLE PROMPT")
    assert prompt.index("ROLE PROMPT") < prompt.index("## Current Time")
    assert prompt.index("## Current Time") < prompt.index("[Memory]")


@pytest.mark.asyncio
async def test_worker_without_orchestrator_still_runs():
    """No identity loader reachable degrades to the old prompt, not a crash."""
    llm = _llm()
    worker = AgentWorker("general", "G", "ROLE PROMPT", [], llm=llm)
    result = await worker.run("s1", "hi")
    assert result.text == "ok"
    assert _sent_messages(llm)[0]["content"].startswith("ROLE PROMPT")


# ── History replay ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prior_turn_is_replayed(identity_file):
    llm = _llm()
    orch = _Orchestrator({
        "s1": [
            {"role": "user", "content": "my dog is called Rex"},
            {"role": "assistant", "content": "Noted, Rex it is."},
        ],
    })
    worker = AgentWorker(
        "general", "G", "ROLE", [], llm=llm, orchestrator=orch,
    )
    await worker.run("s1", "what is my dog called")

    messages = _sent_messages(llm)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "my dog is called Rex"
    assert messages[2]["content"] == "Noted, Rex it is."
    assert messages[-1]["content"] == "what is my dog called"


@pytest.mark.asyncio
async def test_replay_drops_tool_traffic_from_other_paths(identity_file):
    """A worker declares its own tools; a replayed tool row can name one
    this request never sends, which the Responses API rejects."""
    orch = _Orchestrator({
        "s1": [
            {"role": "user", "content": "search for it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "web_search"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "{}"},
            {"role": "assistant", "content": "Here is what I found."},
        ],
    })
    worker = AgentWorker("general", "G", "ROLE", [], llm=_llm(), orchestrator=orch)
    rows = worker.replay_history("s1")
    assert rows == [
        {"role": "user", "content": "search for it"},
        {"role": "assistant", "content": "Here is what I found."},
    ]


@pytest.mark.asyncio
async def test_replay_flattens_multimodal_user_rows(identity_file):
    orch = _Orchestrator({
        "s1": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            },
            {"role": "assistant", "content": "A cat."},
        ],
    })
    worker = AgentWorker("general", "G", "ROLE", [], llm=_llm(), orchestrator=orch)
    assert worker.replay_history("s1") == [
        {"role": "user", "content": "what is this"},
        {"role": "assistant", "content": "A cat."},
    ]


def test_replay_of_unknown_session_is_empty():
    worker = AgentWorker("general", "G", "ROLE", [], llm=_llm(), orchestrator=_Orchestrator())
    assert worker.replay_history("never-seen") == []


# ── Budget refusal ───────────────────────────────────────────────────────────


BUDGET_RESPONSE = {
    "error": (
        "budget exceeded for chat: $9.992715 / $10.000000 "
        "(hour, resets at 1788541200)"
    ),
    "choices": [],
    "budget_exceeded": {
        "call_site": "chat",
        "cap_dollars": 10.0,
        "current_dollars": 9.992715,
        "window": "hour",
        "reset_at": 1788541200.0,
    },
}


@pytest.mark.asyncio
async def test_worker_returns_budget_block_and_no_text():
    llm = _llm(response=BUDGET_RESPONSE)
    # If the guard were missing, this is what would become the reply.
    llm.extract_response = MagicMock(return_value=(BUDGET_RESPONSE["error"], []))
    worker = AgentWorker("general", "G", "ROLE", [], llm=llm)

    result = await worker.run("s1", "hi")
    assert result.text == ""
    assert result.error == ""
    assert result.budget_exceeded["cap_dollars"] == 10.0
    assert "budget exceeded" not in result.text


@pytest.mark.asyncio
async def test_orchestrator_run_returns_empty_and_stashes_the_block():
    llm = _llm(response=BUDGET_RESPONSE)
    llm.extract_response = MagicMock(return_value=(BUDGET_RESPONSE["error"], []))
    orch = MultiAgentOrchestrator(llm=llm)

    async def _route(_text):
        return {"workers": ["general"], "strategy": "single"}

    orch._router.route = _route

    out = await orch.run("s1", "hello")
    assert out == ""
    block = orch.pop_budget_block("s1")
    assert block["current_dollars"] == pytest.approx(9.992715)
    # Popped, not peeked: a later affordable turn must not inherit it.
    assert orch.pop_budget_block("s1") == {}


@pytest.mark.asyncio
async def test_normal_turn_stashes_no_budget_block():
    llm = _llm(reply="all good")
    orch = MultiAgentOrchestrator(llm=llm)

    async def _route(_text):
        return {"workers": ["general"], "strategy": "single"}

    orch._router.route = _route
    assert await orch.run("s1", "hello") == "all good"
    assert orch.pop_budget_block("s1") == {}
