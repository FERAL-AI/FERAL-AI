"""Identity maintenance reads real asynchronous memory, never a coroutine object."""
from __future__ import annotations

import pytest

from identity.workspace import IdentityWorkspace
from memory.store import MemoryStore


class CapturedModel:
    available = True

    def __init__(self, *, fail=False):
        self.requests = []
        self.fail = fail

    async def chat(self, messages, tools=None):
        self.requests.append(messages)
        if self.fail:
            raise RuntimeError("synthetic provider unavailable")
        return {"text": "- Synthetic preference: use a quiet work area."}

    def extract_response(self, response):
        return response["text"], []


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["owner-thread", ""])
async def test_maintenance_reads_awaited_sqlite_episodes(tmp_path, session_id):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    workspace = IdentityWorkspace(home_dir=str(tmp_path / "identity"))
    model = CapturedModel()
    try:
        await store.episode_save("owner-thread", "user_command", "SYNTHETIC_OWNER_FACT")
        await store.episode_save("other-thread", "user_command", "SYNTHETIC_OTHER_FACT")
        await workspace.maintenance_cycle(memory_store=store, llm=model, session_id=session_id)
        assert len(model.requests) == 1
        prompt = model.requests[0][0]["content"]
        assert "SYNTHETIC_OWNER_FACT" in prompt
        assert ("SYNTHETIC_OTHER_FACT" in prompt) is (session_id == "")
        assert "Synthetic preference: use a quiet work area." in workspace.read_memory()
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_empty_or_unavailable_maintenance_does_not_generate(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    workspace = IdentityWorkspace(home_dir=str(tmp_path / "identity"))
    model = CapturedModel()
    before = workspace.read_memory()
    try:
        await workspace.maintenance_cycle(memory_store=store, llm=model, session_id="missing")
        await store.episode_save("owner-thread", "user_command", "SYNTHETIC_OWNER_FACT")
        model.available = False
        await workspace.maintenance_cycle(memory_store=store, llm=model, session_id="owner-thread")
        assert model.requests == []
        assert workspace.read_memory() == before
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_provider_failure_preserves_existing_identity_memory(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    workspace = IdentityWorkspace(home_dir=str(tmp_path / "identity"))
    workspace.write_memory("SYNTHETIC_EXISTING_MEMORY")
    model = CapturedModel(fail=True)
    try:
        await store.episode_save("owner-thread", "user_command", "SYNTHETIC_OWNER_FACT")
        await workspace.maintenance_cycle(memory_store=store, llm=model, session_id="owner-thread")
        assert len(model.requests) == 1
        assert workspace.read_memory() == "SYNTHETIC_EXISTING_MEMORY"
    finally:
        await store.aclose()
