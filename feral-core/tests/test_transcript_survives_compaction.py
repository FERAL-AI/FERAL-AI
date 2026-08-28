"""What the operator and the agent actually said must outlive compaction.

`compact_session` replaces `conversation_history[session_id]` with a
summary plus the last few turns. That is correct for a prompt and wrong
for a memory, so whatever the transcript held has to already be durable
somewhere else by the time compaction runs.

Two holes were found by probing a headless session, i.e. one with no web
client attached to autosave `conversations.messages_json`, which is the
shape of every voice, CLI and paired-phone session:

  1. The per-turn `user_command` episode stored `summary=text[:200]` and
     put the request context in `detail`. Everything a user said past
     character 200 existed only in the in-memory transcript, so
     compaction was the last time anyone could read it.

  2. Assistant replies were never persisted per turn at all. The only
     `episode_save` on the turn path was `user_command`, on both the
     streaming and non-streaming branches. What the agent committed to
     ("I booked the flight for Tuesday") survived only if a summariser
     chose to keep it, which is precisely the thing a summariser cannot
     be relied on to do.

The probe that found this ran a 49-turn session past the threshold and
then searched the whole database column by column: the tail of a long
user message and the assistant's reply were in no table anywhere.

These tests assert retrieval through the real search paths, not just
presence in a column, because content that is stored and unreachable is
not remembered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.store import MemoryStore, _NO_SELF_MODEL_EVENT_TYPES  # noqa: E402


LATE_TAIL = "emergency contact is 4417"
ASSISTANT_COMMITMENT = "booked the flight for tuesday"


def _long_user_message() -> str:
    """A user turn whose meaningful content sits well past character 200."""
    msg = "allergic to penicillin. " + ("filler word " * 40) + f" and also my {LATE_TAIL}."
    assert msg.index(LATE_TAIL) > 200, "fixture must place the tail past the summary cap"
    return msg


async def _store(tmp_path) -> MemoryStore:
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    init = getattr(store, "initialize", None)
    if init is not None:
        maybe = init()
        if hasattr(maybe, "__await__"):
            await maybe
    return store


@pytest.mark.asyncio
async def test_user_text_past_the_summary_cap_is_retrievable(tmp_path):
    """The 200-char `summary` cap must not be the only durable copy."""
    store = await _store(tmp_path)
    msg = _long_user_message()

    # Exactly what the orchestrator writes per user turn.
    await store.episode_save(
        session_id="s",
        event_type="user_command",
        summary=msg[:200],
        detail=json.dumps({"text": msg, "context": {}}),
    )

    hits = await store.episode_search(LATE_TAIL, limit=5)
    assert hits, (
        "the tail of a long user message is unreachable; the 200-char "
        "summary cap has become the only durable record again"
    )


@pytest.mark.asyncio
async def test_assistant_reply_is_persisted_and_retrievable(tmp_path):
    """What the agent said is memory too, not just what it was asked."""
    store = await _store(tmp_path)

    await store.episode_save(
        session_id="s",
        event_type="assistant_reply",
        summary=ASSISTANT_COMMITMENT[:200],
        detail=ASSISTANT_COMMITMENT,
    )

    hits = await store.episode_search(ASSISTANT_COMMITMENT, limit=5)
    assert hits, (
        "the assistant's own reply was not persisted; after compaction it "
        "survives only inside a summary"
    )


@pytest.mark.asyncio
async def test_transcript_outlives_compaction(tmp_path):
    """End to end: persist per turn, compact, then still find both."""
    store = await _store(tmp_path)
    msg = _long_user_message()

    history: list[dict] = [
        {"role": "user", "content": msg, "meta": {"created_at": 1000.0}},
        {"role": "assistant", "content": ASSISTANT_COMMITMENT, "meta": {"created_at": 1000.5}},
    ]
    for i in range(24):
        history.append({"role": "user", "content": f"question {i}", "meta": {"created_at": 1001.0 + i}})
        history.append({"role": "assistant", "content": f"reply {i}", "meta": {"created_at": 1001.5 + i}})

    for row in history:
        if row["role"] == "user":
            await store.episode_save(
                session_id="s", event_type="user_command",
                summary=row["content"][:200],
                detail=json.dumps({"text": row["content"], "context": {}}),
            )
        else:
            await store.episode_save(
                session_id="s", event_type="assistant_reply",
                summary=row["content"][:200], detail=row["content"],
            )

    # llm=None so the heuristic path runs and the result is deterministic.
    result = await store.compact_session("s", history, llm=None)
    assert result.get("compacted") is True
    assert len(result["history"]) < len(history), "compaction did not shrink the transcript"

    assert await store.episode_search(LATE_TAIL, limit=5), (
        "user text lost at compaction"
    )
    assert await store.episode_search(ASSISTANT_COMMITMENT, limit=5), (
        "assistant reply lost at compaction"
    )


def test_orchestrator_persists_assistant_rows():
    """Pin the producer, not just the store.

    The tests above write the fixed episode shape themselves, so they
    would keep passing if the orchestrator reverted to persisting only
    `user_command`. This one drives the orchestrator's own method and
    records what it schedules.
    """
    from agents.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    saved: list[dict] = []
    orch._save_episode_async = lambda **kw: saved.append(kw)

    orch._persist_assistant_rows("s", [
        {"role": "user", "content": "not this one"},
        {"role": "assistant", "content": ASSISTANT_COMMITMENT},
        # Anthropic-style typed blocks, not a bare string.
        {"role": "assistant", "content": [
            {"type": "text", "text": "block form reply"},
            {"type": "tool_use", "name": "irrelevant"},
        ]},
        {"role": "assistant", "content": ""},  # nothing said, nothing stored
    ])

    assert [s["event_type"] for s in saved] == ["assistant_reply", "assistant_reply"]
    details = [s["detail"] for s in saved]
    assert ASSISTANT_COMMITMENT in details
    assert "block form reply" in details, "typed content blocks must be flattened to text"
    assert all("tool_use" not in d for d in details)


def test_user_command_detail_carries_the_full_text_not_just_context():
    """The `user_command` write must not go back to storing only context.

    Source-level, because reaching this line through a real turn needs a
    provider, a session and a perception frame. The regression it guards
    is a two-token edit, so the guard is worth its bluntness.
    """
    import re

    src = (ROOT / "agents" / "orchestrator.py").read_text()
    calls = re.findall(
        r'event_type="user_command",\s*summary=text\[:200\],\s*(?:#[^\n]*\n\s*)*detail=([^\n]+)',
        src,
    )
    assert len(calls) == 2, (
        f"expected the streaming and non-streaming user_command saves, found {len(calls)}"
    )

    # A working _persist_assistant_rows that nothing calls stores nothing.
    # _finalize_turn is the hook because it runs in a `finally` on both
    # command paths, so an early return or a raised exception cannot skip it.
    finalize = src.split("def _finalize_turn", 1)
    assert len(finalize) == 2, "_finalize_turn is gone; the wiring moved"
    body = finalize[1].split("\n    def ", 1)[0]
    assert "_persist_assistant_rows" in body, (
        "_finalize_turn no longer persists assistant rows; replies stop "
        "being durable and only a summary survives compaction"
    )
    for detail in calls:
        assert '"text": text' in detail, (
            "user_command detail dropped the full text; everything past "
            f"character 200 becomes unrecoverable again. Got: {detail}"
        )


def test_assistant_replies_are_barred_from_the_self_model():
    """The agent's words must never become facts about the operator.

    An assistant that says "I have booked you a flight to Tokyo" would
    otherwise teach About-Me that the operator is flying to Tokyo. This
    is the same trust boundary `ambient_conversation` already sits
    behind: overheard or self-authored speech is not the operator
    stating something about themselves.
    """
    assert "assistant_reply" in _NO_SELF_MODEL_EVENT_TYPES
