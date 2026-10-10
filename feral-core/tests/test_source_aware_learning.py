"""Learning uses committed user assertions, not generated or ambient speech."""
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agents.learner import Learner
from memory.runtime_session_checkpoint import encode_context
from memory.store import MemoryStore


class BrokenLLM:
    available = True

    async def chat(self, *args, **kwargs):
        raise RuntimeError("inert extraction failure")


@pytest_asyncio.fixture
async def memory(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
    monkeypatch.setenv("FERAL_EMBED_MODEL_CACHE_ONLY", "1")
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    yield store
    await store.aclose()


@pytest.mark.parametrize("source", ["ambient_conversation", "assistant", "tool", "unknown_ingress"])
@pytest.mark.parametrize("llm", [None, BrokenLLM()])
async def test_every_heuristic_fallback_refuses_non_operator_source(memory, source, llm):
    assert await memory.kg.extract_and_store("I live in Munich.", llm, source=source) == []
    assert await memory.knowledge_query(subject="user") == []


@pytest.mark.parametrize("source", [None, "user_assertion"])
async def test_legacy_explicit_operator_text_and_user_assertion_survive_failure(memory, source):
    await memory.kg.extract_and_store("I live in Berlin.", BrokenLLM(), source=source)
    facts = await memory.knowledge_query(subject="user", predicate="lives_in")
    assert [fact["object"] for fact in facts] == ["Berlin"]


@pytest.mark.parametrize("text", ["assistant: I live in Munich.", "[assistant] I live in Munich."])
async def test_role_labelled_transcript_fallback_does_not_make_assistant_the_user(memory, text):
    assert await memory.kg.extract_and_store(text, BrokenLLM()) == []


async def test_learner_excludes_assistant_tool_ambient_and_provisional_rows(memory):
    memory.working_push("legacy", {"role": "user", "text": "Please help with a shopping list."})
    for row in [
        {"role": "assistant", "text": "I live in Munich."},
        {"role": "tool", "text": "I live in Munich."},
        {"role": "user", "text": "I live in Munich.", "source": "ambient_conversation"},
        {"role": "user", "text": "I live in Munich.", "committed": False},
        {"role": "user", "text": "I live in Munich.", "authenticated": False},
        {"role": "user", "text": "I live in Munich.", "event_type": "ambient_conversation"},
    ]:
        memory.working_push("legacy", row)
    await Learner(BrokenLLM(), memory).extract_knowledge("legacy")
    assert await memory.knowledge_query(subject="user") == []


async def commit_rows(memory, sid, rows):
    pending = await memory.runtime_checkpoint_begin(sid, attempt_id=str(uuid4()))
    await memory.runtime_checkpoint_commit(pending.record.fence, encode_context([], rows))


async def test_learner_uses_ready_checkpoint_not_mutable_working_input(memory):
    await commit_rows(memory, "managed", [{"role": "user", "text": "I live in Berlin and enjoy the city."}])
    memory.working_push("managed", {"role": "user", "text": "I live in Munich."})
    await Learner(BrokenLLM(), memory).extract_knowledge("managed")
    facts = await memory.knowledge_query(subject="user", predicate="lives_in")
    assert [fact["object"] for fact in facts] == ["Berlin and enjoy the city"]


async def test_available_model_cannot_turn_ambient_speaker_into_operator(memory):
    llm = BrokenLLM()
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = lambda response: (
        '[{"subject":"user","predicate":"lives_in","object":"Munich"},'
        '{"subject":"Alice","predicate":"lives_in","object":"Rome"}]', [],
    )
    stored = await memory.kg.extract_and_store("Alice said she lives in Rome.", llm, source="ambient_conversation")
    assert len(stored) == 1
    assert await memory.knowledge_query(subject="user") == []
    facts = await memory.knowledge_query(subject="Alice", predicate="lives_in")
    assert [fact["object"] for fact in facts] == ["Rome"]


@pytest.mark.parametrize("subject", ["me", "operator", "OwnerName"])
async def test_untrusted_model_subject_cannot_resolve_to_operator_alias(memory, subject):
    import json
    user = await memory.kg.add_entity("user", "person")
    await memory.kg._add_alias(user["id"], subject)
    llm = BrokenLLM()
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = lambda response: (json.dumps([
        {"subject": subject, "predicate": "lives_in", "object": "Munich"},
    ]), [])
    assert await memory.kg.extract_and_store("Someone lives in Munich.", llm, source="ambient_conversation") == []
    assert await memory.knowledge_query(subject="user") == []


async def test_learner_refuses_pending_checkpoint(memory):
    await memory.runtime_checkpoint_begin("pending", attempt_id=str(uuid4()))
    memory.working_push("pending", {"role": "user", "text": "I live in Munich and love the city."})
    await Learner(BrokenLLM(), memory).extract_knowledge("pending")
    assert await memory.knowledge_query(subject="user") == []


async def test_learner_refuses_checkpoint_reader_error(memory):
    memory.working_push("managed", {"role": "user", "text": "I live in Munich and love the city."})
    memory.runtime_checkpoint_read = AsyncMock(side_effect=RuntimeError("inert storage failure"))
    await Learner(BrokenLLM(), memory).extract_knowledge("managed")
    assert await memory.knowledge_query(subject="user") == []


async def test_legacy_learner_preserves_user_assertions(memory):
    memory.working_push("legacy", {"role": "user", "text": "I live in Berlin and enjoy the city."})
    await Learner(BrokenLLM(), memory).extract_knowledge("legacy")
    facts = await memory.knowledge_query(subject="user", predicate="lives_in")
    assert [fact["object"] for fact in facts] == ["Berlin and enjoy the city"]


@pytest.mark.parametrize("existing_alias", [False, True])
async def test_rejected_ambient_resolution_does_not_mutate_operator(memory, existing_alias):
    import json
    import sqlite3
    user = await memory.kg.add_entity("user", "person")
    if existing_alias:
        await memory.kg._add_alias(user["id"], "OverheardName")
    else:
        # Keep the actual stored operator; replace only the read-only embedding
        # boundary so linking is deterministic without a model download.
        memory.kg._link_entity = AsyncMock(return_value=user)

    def snapshot():
        with sqlite3.connect(memory.db_path) as conn:
            return (
                conn.execute("SELECT mention_count, updated_at FROM entities WHERE id=?", (user["id"],)).fetchone(),
                conn.execute("SELECT alias FROM entity_aliases WHERE entity_id=? ORDER BY alias", (user["id"],)).fetchall(),
                conn.execute("SELECT COUNT(*) FROM entities").fetchone(),
                conn.execute("SELECT COUNT(*) FROM relations").fetchone(),
            )

    before = snapshot()
    llm = BrokenLLM()
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = lambda response: (json.dumps([
        {"subject": "OverheardName", "predicate": "lives_in", "object": "Munich"},
    ]), [])
    assert await memory.kg.extract_and_store("Someone lives in Munich.", llm, source="ambient_conversation") == []
    assert snapshot() == before
