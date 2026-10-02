"""Atomic canonical-thread creation: genuine SQLite and opt-in route contracts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory.store import MemoryStore

pytestmark = pytest.mark.no_auto_feral_home


@pytest.mark.asyncio
async def test_existing_document_is_never_modified(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "existing.db"))
    try:
        rows = [{"id": "rich", "role": "assistant", "content": "Existing answer", "tools": [{"id": "kept"}], "opaque": {"nested": [1, 2]}}]
        await store.conversation_save("primary", rows)
        await store.conversation_rename("primary", "Custom title")
        await store.conversation_set_pinned("primary", True)
        before = await store.conversation_get("primary")
        receipts = await asyncio.gather(*(store.conversation_create_if_missing("primary", title="Overwrite attempt") for _ in range(12)))
        assert all(r == {"created": False, "conversation": before} for r in receipts)
        assert await store.conversation_get("primary") == before
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_concurrent_creators_across_independent_pools_have_one_winner(tmp_path):
    path = str(tmp_path / "concurrent.db")
    stores = [MemoryStore(db_path=path), MemoryStore(db_path=path)]
    try:
        receipts = await asyncio.gather(*(stores[i % 2].conversation_create_if_missing("primary", title=f"Title {i}") for i in range(30)))
        assert sum(r["created"] for r in receipts) == 1
        winning = next(r["conversation"] for r in receipts if r["created"])
        assert winning["messages"] == []
        assert all(r["conversation"] == winning for r in receipts)
        assert await stores[1].conversation_get("primary") == winning
    finally:
        for store in stores:
            await store.aclose()


@pytest.mark.asyncio
async def test_create_racing_append_cannot_erase_messages(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "append.db"))
    try:
        tasks = [store.conversation_create_if_missing("primary", title="Shared") for _ in range(10)]
        tasks += [store.conversation_append("primary", "user", f"Message {i}") for i in range(10)]
        await asyncio.gather(*tasks)
        document = await store.conversation_get("primary")
        assert len(document["messages"]) == 10
        assert {r["content"] for r in document["messages"]} == {f"Message {i}" for i in range(10)}
    finally:
        await store.aclose()


def client_for(monkeypatch, memory):
    from api.routes import conversations
    monkeypatch.setattr(conversations, "state", SimpleNamespace(memory=memory))
    app = FastAPI()
    app.include_router(conversations.router)
    return TestClient(app)


def test_opt_in_returns_exact_winning_document(monkeypatch):
    document = {"id": "primary", "messages": [{"role": "assistant", "content": "Keep", "tools": ["kept"]}], "title": "Custom", "pinned": True}
    atomic = AsyncMock(return_value={"created": False, "conversation": document})
    legacy = AsyncMock()
    client = client_for(monkeypatch, SimpleNamespace(conversation_create_if_missing=atomic, conversation_save=legacy))
    response = client.post("/api/conversations/new", json={"id": "primary", "title": "Shared", "create_if_missing": True})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": "primary", "create_if_missing": True, "created": False, "conversation": document}
    atomic.assert_awaited_once_with("primary", title="Shared")
    legacy.assert_not_awaited()


def test_unsupported_storage_never_falls_back_to_upsert(monkeypatch):
    legacy = AsyncMock()
    client = client_for(monkeypatch, SimpleNamespace(conversation_save=legacy))
    assert client.post("/api/conversations/new", json={"id": "primary", "create_if_missing": True}).status_code == 501
    legacy.assert_not_awaited()


@pytest.mark.parametrize("payload", [
    {"id": "primary", "create_if_missing": "true"},
    {"id": "primary", "create_if_missing": 1},
    {"id": "", "create_if_missing": True},
    {"id": " x ", "create_if_missing": True},
    {"id": "x\n", "create_if_missing": True},
    {"id": "x" * 257, "create_if_missing": True},
    {"id": "primary", "create_if_missing": True, "messages": []},
])
def test_invalid_atomic_requests_perform_no_writes(monkeypatch, payload):
    atomic, legacy = AsyncMock(), AsyncMock()
    client = client_for(monkeypatch, SimpleNamespace(conversation_create_if_missing=atomic, conversation_save=legacy))
    assert client.post("/api/conversations/new", json=payload).status_code == 422
    atomic.assert_not_awaited()
    legacy.assert_not_awaited()


@pytest.mark.parametrize("result", [None, {"created": 1, "conversation": {"id": "primary", "messages": []}}, {"created": True, "conversation": {"id": "foreign", "messages": []}}])
def test_unknown_storage_receipt_never_claims_success_or_retries(monkeypatch, result):
    atomic, legacy = AsyncMock(return_value=result), AsyncMock()
    client = client_for(monkeypatch, SimpleNamespace(conversation_create_if_missing=atomic, conversation_save=legacy))
    assert client.post("/api/conversations/new", json={"id": "primary", "create_if_missing": True}).status_code == 502
    assert atomic.await_count == 1
    legacy.assert_not_awaited()


def test_legacy_default_retains_existing_upsert_contract(monkeypatch):
    legacy, atomic = AsyncMock(return_value={"id": "legacy", "message_count": 0}), AsyncMock()
    client = client_for(monkeypatch, SimpleNamespace(conversation_save=legacy, conversation_create_if_missing=atomic))
    response = client.post("/api/conversations/new", json={"id": "legacy", "title": "Old behavior"})
    assert response.json() == {"ok": True, "id": "legacy", "message_count": 0}
    legacy.assert_awaited_once_with("legacy", [], title="Old behavior")
    atomic.assert_not_awaited()


def test_storage_error_is_uncertain_without_upsert_retry(monkeypatch):
    atomic = AsyncMock(side_effect=RuntimeError("private path /secret"))
    legacy = AsyncMock()
    client = client_for(monkeypatch, SimpleNamespace(conversation_create_if_missing=atomic, conversation_save=legacy))
    response = client.post("/api/conversations/new", json={"id": "primary", "create_if_missing": True})
    assert response.status_code == 502
    assert "/secret" not in response.text
    assert atomic.await_count == 1
    legacy.assert_not_awaited()
