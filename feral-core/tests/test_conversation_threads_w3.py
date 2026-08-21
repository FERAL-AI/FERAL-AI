"""Thread management: sticky rename, pin, search, pagination.

Every test here drives a REAL ``MemoryStore`` on a temp DB. The existing
``tests/test_conversations_routes.py`` binds ``state.memory`` to a
``MagicMock``, so it asserts what the route asked for and can never
observe what the store actually stored. The title-clobber defect below
lived under that mock indefinitely: the route was calling
``conversation_save`` exactly as designed, and the store was throwing
the user's title away one layer down.

The defect, measured before the fix on a throwaway store:

    save("t", msgs, title="My renamed thread")  -> title "My renamed thread"
    save("t", msgs)                             -> title "hello number 1 about pytest"

A rename survived zero autosaves. The v2 client autosaves 450ms after
every message change and passes a title derived from the first user
message every time.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "conv.db"))


@pytest.fixture
def client(store, monkeypatch):
    from unittest.mock import MagicMock, patch

    mock = MagicMock()
    mock.memory = store
    mock.orchestrator = MagicMock()
    with patch("api.state.state", mock), patch("api.routes.conversations.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


def _msgs(text="hello number 1 about pytest"):
    return [{"role": "user", "content": text}, {"role": "assistant", "content": "hi"}]


# ── The clobber ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_save_still_derives_a_title(store):
    """Unrenamed threads keep the existing derive-from-first-message behaviour."""
    await store.conversation_save("t", _msgs())
    conv = await store.conversation_get("t")
    assert conv["title"] == "hello number 1 about pytest"
    assert conv["title_custom"] is False


@pytest.mark.asyncio
async def test_rename_survives_autosave(store):
    """The regression guard. Autosave must not overwrite a user's title."""
    await store.conversation_save("t", _msgs())
    await store.conversation_rename("t", "My renamed thread")

    # Exactly what the v2 client's 450ms autosave sends: a derived title.
    await store.conversation_save("t", _msgs(), title="hello number 1 about pytest")
    await store.conversation_save("t", _msgs())

    conv = await store.conversation_get("t")
    assert conv["title"] == "My renamed thread"
    assert conv["title_custom"] is True


@pytest.mark.asyncio
async def test_save_reports_the_stored_title_not_the_requested_one(store):
    """A save that was refused must not report success with the new name."""
    await store.conversation_save("t", _msgs())
    await store.conversation_rename("t", "Kept")
    out = await store.conversation_save("t", _msgs(), title="Derived")
    assert out["title"] == "Kept"


@pytest.mark.asyncio
async def test_autosave_still_updates_preview_and_count_on_renamed_thread(store):
    """Sticky title must not freeze the rest of the row."""
    await store.conversation_save("t", _msgs())
    await store.conversation_rename("t", "Kept")
    await store.conversation_save("t", [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "the latest thing I said"},
    ])
    conv = await store.conversation_get("t")
    assert conv["title"] == "Kept"
    assert conv["preview"] == "the latest thing I said"
    assert conv["message_count"] == 3


@pytest.mark.asyncio
async def test_rename_does_not_bump_updated_at(store):
    """Renaming is not activity; it must not reorder the thread list."""
    await store.conversation_save("a", _msgs())
    await store.conversation_save("b", _msgs())
    before = (await store.conversation_get("a"))["updated_at"]
    await store.conversation_rename("a", "Renamed")
    assert (await store.conversation_get("a"))["updated_at"] == before
    assert [c["id"] for c in await store.conversation_list()] == ["b", "a"]


@pytest.mark.asyncio
async def test_rename_rejects_blank_and_missing(store):
    await store.conversation_save("t", _msgs())
    assert await store.conversation_rename("t", "   ") is None
    assert await store.conversation_rename("missing", "x") is None


# ── Pin ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pin_sorts_first_and_survives_autosave(store):
    for i in range(3):
        await store.conversation_save(f"t{i}", _msgs())
    await store.conversation_set_pinned("t0", True)
    assert [c["id"] for c in await store.conversation_list()] == ["t0", "t2", "t1"]

    await store.conversation_save("t0", _msgs())
    assert (await store.conversation_get("t0"))["pinned"] is True

    await store.conversation_set_pinned("t0", False)
    assert (await store.conversation_get("t0"))["pinned"] is False


@pytest.mark.asyncio
async def test_pin_missing_thread_returns_none(store):
    assert await store.conversation_set_pinned("missing", True) is None


# ── Search + pagination ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_page_reports_total_beyond_the_page(store):
    """Thread 51 must be reachable. It was not: no limit, no pagination."""
    for i in range(60):
        await store.conversation_save(f"t{i:02d}", _msgs(f"question {i}"))
    page = await store.conversation_page(limit=25, offset=0)
    assert len(page["items"]) == 25
    assert page["total"] == 60
    assert page["has_more"] is True

    last = await store.conversation_page(limit=25, offset=50)
    assert len(last["items"]) == 10
    assert last["has_more"] is False
    # thread 51 in list order (0-indexed 50) is reachable.
    assert last["items"][0]["id"] == "t09"


@pytest.mark.asyncio
async def test_page_limit_is_clamped_not_unbounded(store):
    for i in range(5):
        await store.conversation_save(f"t{i}", _msgs())
    page = await store.conversation_page(limit=100_000)
    assert page["limit"] == MemoryStore.CONVERSATION_PAGE_MAX


@pytest.mark.asyncio
async def test_search_matches_title_preview_and_body(store):
    await store.conversation_save("hit-title", _msgs("regenerate the vault key"))
    await store.conversation_save("hit-body", [
        {"role": "user", "content": "unrelated opener"},
        {"role": "assistant", "content": "you can rotate the vault key with feral key"},
        {"role": "user", "content": "thanks"},
    ])
    await store.conversation_save("miss", _msgs("something else entirely"))

    ids = {c["id"] for c in (await store.conversation_page(query="vault"))["items"]}
    assert ids == {"hit-title", "hit-body"}


@pytest.mark.asyncio
async def test_search_escapes_like_wildcards(store):
    """A literal '%' must not match every thread."""
    await store.conversation_save("plain", _msgs("no wildcard here"))
    await store.conversation_save("percent", _msgs("battery at 80% now"))

    page = await store.conversation_page(query="%")
    assert [c["id"] for c in page["items"]] == ["percent"]
    assert page["total"] == 1

    under = await store.conversation_page(query="_")
    assert under["total"] == 0


@pytest.mark.asyncio
async def test_search_total_counts_matches_not_all_rows(store):
    for i in range(30):
        await store.conversation_save(f"t{i:02d}", _msgs(f"question {i} about sqlite"))
    await store.conversation_save("needle", _msgs("about the vault"))
    page = await store.conversation_page(query="vault", limit=5)
    assert page["total"] == 1
    assert page["has_more"] is False


@pytest.mark.asyncio
async def test_pinned_sort_holds_across_pages(store):
    for i in range(30):
        await store.conversation_save(f"t{i:02d}", _msgs())
    await store.conversation_set_pinned("t00", True)
    first = await store.conversation_page(limit=5, offset=0)
    assert first["items"][0]["id"] == "t00"
    assert all(not c["pinned"] for c in (await store.conversation_page(limit=5, offset=5))["items"])


# ── Routes (real store behind the real app) ───────────────────────


def test_route_list_returns_pagination_envelope(client, store):
    for i in range(30):
        client.post("/api/conversations/save", json={"id": f"t{i:02d}", "messages": _msgs()})
    r = client.get("/api/conversations?limit=10&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"conversations", "total", "limit", "offset", "has_more", "query"}
    assert body["total"] == 30
    assert len(body["conversations"]) == 10
    assert body["has_more"] is True


def test_route_list_row_carries_preview_and_message_count(client):
    client.post("/api/conversations/save", json={"id": "t", "messages": _msgs()})
    row = client.get("/api/conversations").json()["conversations"][0]
    # The v2 thread pane discarded both of these; they are in the payload.
    assert row["preview"] == "hello number 1 about pytest"
    assert row["message_count"] == 2
    assert row["pinned"] is False


def test_route_rename_then_autosave_keeps_the_name(client):
    client.post("/api/conversations/save", json={"id": "t", "messages": _msgs()})
    r = client.post("/api/conversations/t/rename", json={"title": "Renamed by hand"})
    assert r.json() == {"ok": True, "id": "t", "title": "Renamed by hand", "title_custom": True}

    client.post("/api/conversations/save", json={
        "id": "t", "messages": _msgs(), "title": "hello number 1 about pytest",
    })
    assert client.get("/api/conversations/t").json()["title"] == "Renamed by hand"


def test_route_pin_toggles_and_reorders(client):
    for i in range(3):
        client.post("/api/conversations/save", json={"id": f"t{i}", "messages": _msgs()})
    assert client.post("/api/conversations/t0/pin", json={"pinned": True}).json()["pinned"] is True
    ids = [c["id"] for c in client.get("/api/conversations").json()["conversations"]]
    assert ids[0] == "t0"
    assert client.post("/api/conversations/t0/pin", json={"pinned": False}).json()["pinned"] is False


def test_route_errors_are_200_with_an_error_envelope(client):
    """apiFetch throws on a non-empty ``error`` even at 200.

    The client must call these with ``silent: true`` and read the
    envelope off ``.raw``; this test pins the shape it has to read.
    """
    assert client.post("/api/conversations/missing/rename", json={"title": "x"}).json() == {"error": "Not found"}
    assert client.post("/api/conversations/missing/pin", json={"pinned": True}).json() == {"error": "Not found"}
    client.post("/api/conversations/save", json={"id": "t", "messages": _msgs()})
    assert client.post("/api/conversations/t/rename", json={"title": "  "}).json() == {"error": "title is required"}


def test_route_search_passes_the_query_through(client):
    client.post("/api/conversations/save", json={"id": "a", "messages": _msgs("about the vault key")})
    client.post("/api/conversations/save", json={"id": "b", "messages": _msgs("about lunch")})
    body = client.get("/api/conversations?q=vault").json()
    assert body["query"] == "vault"
    assert [c["id"] for c in body["conversations"]] == ["a"]
    assert body["total"] == 1
