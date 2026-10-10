"""A real worker's current query reaches existing memory retrieval."""
from unittest.mock import AsyncMock

from agents.multi_agent import AgentWorker
from memory.store import MemoryStore


class InertLLM:
    available = True
    provider = "fixture"

    def __init__(self):
        self.messages = []

    async def chat(self, messages, **kwargs):
        self.messages.append(messages)
        return {}

    def extract_response(self, response):
        return "Fixture complete.", []


async def test_current_query_retrieves_fact_for_actual_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
    monkeypatch.setenv("FERAL_EMBED_MODEL_CACHE_ONLY", "1")
    store = MemoryStore(db_path=str(tmp_path / "worker.db"))
    try:
        await store.knowledge_store(subject="user", predicate="uses", obj="Kestrel precision telescope")
        llm = InertLLM()
        original = store.build_context_for_llm
        store.build_context_for_llm = AsyncMock(wraps=original)
        worker = AgentWorker("fixture", "Fixture", "Help with astronomy.", [], llm=llm, memory=store)
        await worker.run("worker-session", "Tell me about Kestrel precision telescope")
        store.build_context_for_llm.assert_awaited_once_with(
            "worker-session", query="Tell me about Kestrel precision telescope", max_tokens_budget=300,
        )
        assert "Kestrel precision telescope" in llm.messages[0][0]["content"]
    finally:
        await store.aclose()
