"""Regression guards for the multi-turn amnesia defect.

Reproduction that motivated these tests: the assistant asked "What
timezone are you in?", the user replied "10 am PST", and the assistant
answered "Looks like we got cut off earlier — you'd started asking about
connecting to Gmail, and now you're back with '10 am PST.'" It had no
memory of having spoken one turn earlier.

Nothing in the repo emits that sentence. The model was describing the
array it was handed:

    system     'SYSTEM'
    user       'how do I connect Gmail to you?'
    user       'schedule the design review with Dana'
    user       '10 am PST'

Assistant turns reached ``Orchestrator.conversation_history`` from
exactly one code path — the bottom of the single-agent text loop. Every
other reply path recorded the user row and returned before the assistant
row was appended, and the compacted window was written back over the
stored transcript so truncation was permanent.

The invariant under test: the model must never receive two consecutive
user messages, and every path that sends text must record what it sent.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator
from memory.store import MemoryStore

REFUSAL = "I don't have access to your calendar yet. What timezone are you in?"


def _orchestrator() -> Orchestrator:
    """Orchestrator wired for a text turn with a stubbed LLM."""
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    orch = Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
    )
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "test-model"
    orch._streaming_enabled = False
    orch._route_prompt = AsyncMock(return_value=[])
    orch._maybe_emit_temporal_timeline = AsyncMock(return_value=False)
    orch._build_system_prompt = AsyncMock(return_value="SYSTEM")
    return orch


def _text_reply(orch: Orchestrator, reply: str) -> list[list[dict]]:
    """Stub the LLM to answer ``reply`` in one pass. Returns the list the
    outgoing ``messages`` arrays are captured into."""
    captured: list[list[dict]] = []

    async def fake_chat(*, messages, **_kwargs):
        captured.append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": reply}}]}

    orch._call_llm_chat = fake_chat
    orch.llm.extract_response = MagicMock(return_value=(reply, []))
    return captured


def _consecutive_user_pairs(messages: list[dict]) -> list[tuple[int, int]]:
    roles = [m.get("role") for m in messages]
    return [
        (i, i + 1)
        for i, (a, b) in enumerate(zip(roles, roles[1:]))
        if a == "user" and b == "user"
    ]


# ─────────────────────────────────────────────────────────────────────
# Loss path 1 — live voice
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_voice_then_chat_never_sends_two_consecutive_user_rows():
    """The exact reproduction: a live-voice exchange followed by a text
    turn. The realtime providers stream audio straight to the client, so
    the assistant's transcript only ever reached working memory — the
    text turn that followed saw user, user."""
    orch = _orchestrator()
    sid = "sess-voice-then-chat"
    captured = _text_reply(orch, "Got it, 10am Pacific.")

    await orch.note_voice_user_turn(sid, "how do I connect Gmail to you?")
    await orch.note_voice_assistant_turn(sid, REFUSAL)

    await orch.handle_command(sid, "10 am PST")

    messages = captured[-1]
    assert not _consecutive_user_pairs(messages), [
        (m.get("role"), m.get("content")) for m in messages
    ]
    roles = [m.get("role") for m in messages]
    assert roles == ["system", "user", "assistant", "user"], roles
    assert messages[2]["content"] == REFUSAL


@pytest.mark.asyncio
async def test_note_voice_assistant_turn_is_idempotent_on_replay():
    """Providers re-emit the same final transcript on reconnect."""
    orch = _orchestrator()
    sid = "sess-voice-replay"
    await orch.note_voice_assistant_turn(sid, "Sure thing.")
    await orch.note_voice_assistant_turn(sid, "Sure thing.")
    assert orch.conversation_history[sid] == [
        {"role": "assistant", "content": "Sure thing.", "source": "voice_realtime"},
    ]


@pytest.mark.asyncio
async def test_proactive_turn_is_visible_to_the_users_follow_up():
    orch = _orchestrator()
    sid = "sess-proactive-then-chat"
    memory = MagicMock()
    memory.episode_save = AsyncMock(return_value={"id": "episode-1"})
    orch.memory = memory
    captured = _text_reply(orch, "Because the live reading stayed elevated.")

    await orch.note_proactive_assistant_turn(
        sid,
        "I noticed your heart rate stayed elevated.",
        trigger_id="hr_elevated",
        priority="IMPORTANT",
        context={"source": "jw_health_glasses", "sample_age_s": 2},
    )
    await orch.handle_command(sid, "Why?")

    messages = captured[-1]
    assert [m.get("role") for m in messages] == [
        "system", "assistant", "user",
    ]
    assert messages[1]["content"] == "I noticed your heart rate stayed elevated."
    memory.working_push.assert_any_call(
        sid,
        {
            "role": "assistant",
            "text": "I noticed your heart rate stayed elevated.",
            "source": "proactive",
            "trigger_id": "hr_elevated",
        },
    )
    memory.episode_save.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────
# Loss path 2 — early returns that skipped the write-back
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refusal_fallback_turn_records_its_assistant_reply():
    """``REFUSAL_PHRASES`` matches legitimate replies ("i don't have
    access"), so this fallback fires on real turns. It returned before
    the only line that wrote the assistant row back."""
    orch = _orchestrator()
    sid = "sess-refusal"
    _text_reply(orch, REFUSAL)
    orch._execute_action_intent_fallback = AsyncMock(return_value=False)
    orch._on_capability_gap = AsyncMock(return_value={})

    async def fake_direct(session_id, text, skills):
        await orch._send_text(session_id, "Booked it for 10am Pacific.")

    orch._direct_execute = fake_direct

    await orch.handle_command(sid, "schedule the design review with Dana")

    history = orch.conversation_history[sid]
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == "Booked it for 10am Pacific."
    assert not _consecutive_user_pairs(history)


@pytest.mark.asyncio
async def test_stream_refusal_fallback_turn_records_its_assistant_reply():
    """The stream path had the identical structure: the refusal fallback
    returned over the one line that wrote the assistant row back."""
    orch = _orchestrator()
    sid = "sess-stream-refusal"
    orch._streaming_enabled = True

    async def fake_stream(*, messages, tools=None, **_kwargs):
        yield {"type": "text_delta", "content": REFUSAL}
        yield {"type": "done"}

    orch.llm.chat_stream = fake_stream
    orch._execute_action_intent_fallback = AsyncMock(return_value=False)
    orch._on_capability_gap = AsyncMock(return_value={})

    async def fake_direct(session_id, text, skills):
        await orch._send_text(session_id, "Sent it.")

    orch._direct_execute = fake_direct

    await orch.handle_command_stream(sid, "email Dana the design doc")

    history = orch.conversation_history[sid]
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == "Sent it."
    assert not _consecutive_user_pairs(history)


@pytest.mark.asyncio
async def test_budget_exceeded_turn_records_its_assistant_reply():
    """The cost-cap short circuit sends a banner + text and returned."""
    orch = _orchestrator()
    sid = "sess-budget"

    async def fake_chat(*, messages, **_kwargs):
        return {
            "budget_exceeded": {
                "call_site": "chat", "cap_dollars": 1.0,
                "current_dollars": 1.5, "window": "hour", "reset_at": 0.0,
            },
        }

    orch._call_llm_chat = fake_chat

    await orch.handle_command(sid, "what's on my calendar?")

    history = orch.conversation_history[sid]
    assert history[-1]["role"] == "assistant"
    assert "Cost cap reached" in history[-1]["content"]


@pytest.mark.asyncio
async def test_llm_exception_turn_records_its_assistant_reply():
    orch = _orchestrator()
    sid = "sess-llm-error"

    async def boom(*, messages, **_kwargs):
        raise RuntimeError("provider exploded")

    orch._call_llm_chat = boom

    async def fake_direct(session_id, text, skills):
        await orch._send_text(session_id, "I'm having trouble reaching the model.")

    orch._direct_execute = fake_direct

    await orch.handle_command(sid, "turn on the lights")

    history = orch.conversation_history[sid]
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == "I'm having trouble reaching the model."


@pytest.mark.asyncio
async def test_multi_agent_turn_records_both_rows():
    """The multi-agent hand-off never touched ``conversation_history``,
    so the whole exchange vanished from the next turn's context."""
    orch = _orchestrator()
    sid = "sess-multi-agent"
    orch._multi_agent_enabled = True
    orch._multi_agent = MagicMock()
    orch._multi_agent.run = AsyncMock(return_value="Three specialists agree: ship it.")

    await orch.handle_command(sid, "should we ship on Friday?")

    history = orch.conversation_history[sid]
    assert [row["role"] for row in history] == ["user", "assistant"]
    assert history[0]["content"] == "should we ship on Friday?"
    assert history[1]["content"] == "Three specialists agree: ship it."


# ─────────────────────────────────────────────────────────────────────
# Loss path 3 — compaction was destructive
# ─────────────────────────────────────────────────────────────────────


async def _run_tool_turn(orch: Orchestrator, sid: str, text: str, n_tools: int) -> list[dict]:
    """Drive one turn that makes ``n_tools`` parallel tool calls and then
    answers. Returns the ``messages`` array of the final LLM call."""
    tool_calls = [
        {"id": f"{text}-c{i}", "name": "demo__ping", "args": {}}
        for i in range(n_tools)
    ]
    raw_calls = [
        {
            "id": tc["id"],
            "type": "function",
            "function": {"name": "demo__ping", "arguments": "{}"},
        }
        for tc in tool_calls
    ]
    state = {"pass": 0}
    captured: list[list[dict]] = []

    async def fake_chat(*, messages, **_kwargs):
        captured.append(list(messages))
        state["pass"] += 1
        if state["pass"] == 1 and n_tools:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": raw_calls}}]}
        return {"choices": [{"message": {"role": "assistant", "content": f"answer to {text}"}}]}

    def fake_extract(response):
        message = response["choices"][0]["message"]
        if message.get("tool_calls"):
            return ("", tool_calls)
        return (message.get("content", ""), [])

    orch._call_llm_chat = fake_chat
    orch.llm.extract_response = fake_extract
    orch._execute_tool_call_for_llm = AsyncMock(return_value={"success": True})

    await orch.handle_command(sid, text)
    return captured[-1]


async def _turns_surviving(n_tools: int, n_turns: int = 6) -> int:
    """How many of ``n_turns`` user utterances are still visible in the
    window handed to the LLM on the final turn."""
    orch = _orchestrator()
    sid = f"sess-survival-{n_tools}"
    utterances = [f"turn number {i}" for i in range(n_turns)]
    messages: list[dict] = []
    for text in utterances:
        messages = await _run_tool_turn(orch, sid, text, n_tools)
    rendered = " | ".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    )
    return sum(1 for text in utterances if text in rendered)


@pytest.mark.asyncio
@pytest.mark.parametrize("n_tools", [0, 2, 5, 13])
async def test_all_turns_survive_regardless_of_tool_fanout(n_tools: int):
    """The window was 15 RAW ROWS, so one assistant turn with six
    parallel tool calls consumed seven slots. Measured survival across
    six sequential turns was 6 / 3 / 2 / 1 for 0 / 2-3 / 5-6 / 13+ tool
    calls. It is now measured in turns."""
    assert await _turns_surviving(n_tools) == 6


@pytest.mark.asyncio
async def test_compaction_is_a_view_not_a_replacement():
    """``conversation_history`` was assigned the compacted window, so
    truncation was permanent and cumulative and
    ``_conversation_max_per_session`` was dead code on the text path."""
    orch = _orchestrator()
    sid = "sess-full-transcript"
    # 12 turns = 24 rows, comfortably past the old 15-row window, so the
    # old write-back had already eaten the head of the transcript.
    for i in range(12):
        _text_reply(orch, f"reply {i}")
        await orch.handle_command(sid, f"question {i}")

    history = orch.conversation_history[sid]
    assert len(history) == 24, [row.get("content") for row in history]
    assert history[0]["content"] == "question 0"
    assert history[-1]["content"] == "reply 11"


@pytest.mark.asyncio
async def test_window_never_drops_the_last_assistant_message():
    """Even squeezed to a single turn's worth of tokens the window keeps
    the newest assistant row — dropping it is what produced the two
    consecutive user rows in the first place."""
    orch = _orchestrator()
    sid = "sess-tight-window"
    orch.context_manager.max_messages = 1
    orch.context_manager.context_window_tokens = 1
    captured = _text_reply(orch, "first answer")
    await orch.handle_command(sid, "first question")
    captured = _text_reply(orch, "second answer")
    await orch.handle_command(sid, "second question")

    messages = captured[-1]
    assert not _consecutive_user_pairs(messages)
    assert any(
        m.get("role") == "assistant" and m.get("content") == "first answer"
        for m in messages
    ), messages


# ─────────────────────────────────────────────────────────────────────
# Loss path 4 — the memory block showed the OLDEST fragment
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = MemoryStore(db_path=path)
    yield s
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_recent_context_block_shows_the_newest_entries(store):
    """``working_context_string`` joins oldest→newest and the builder
    sliced ``working[:budget]`` — so the block the prompt labels "Recent
    Context" carried the OLDEST entries. That is how a question the user
    had moved on from resurfaced as the model's idea of the present."""
    sid = "sess-recent-context"
    for i in range(8):
        store.working_push(sid, {"role": "user", "text": f"entry-{i} " + "x" * 100})

    context = await store.build_context_for_llm_async(sid, max_tokens_budget=800)

    assert "entry-7" in context
    assert "entry-0" not in context
