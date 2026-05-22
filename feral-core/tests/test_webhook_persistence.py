"""
Lane 10 — custom webhooks survive a brain restart.

Pre-Lane-10 the custom-webhooks router stored configurations in a
process-local module dict (``api.routes.webhooks._webhooks``). Every
restart wiped the operator's hooks and any external service that had
been pointed at them silently broke. Finding 19 names this directly:
"Custom operator webhooks → persistent? **No** — module dict
``_webhooks``."

These tests pin the durable contract:

* :class:`WebhookStore` writes through to sqlite at
  ``~/.feral/webhooks.db``.
* The HTTP routes (now mounted under ``/api/custom-webhooks/*``) round
  trip through the store, not the legacy dict.
* A second :class:`WebhookStore` instance pointed at the same DB sees
  the rows the first one created — restart simulation.
* ``record_trigger`` increments ``trigger_count`` and updates
  ``last_triggered`` only on accepted (verified-or-no-secret)
  deliveries.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def store_db(tmp_path):
    return tmp_path / "test-webhooks.db"


# ──────────────────────────────────────────────────────────────────────
# Direct WebhookStore contract
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_retrieve_round_trip(store_db):
    from integrations.webhook_store import WebhookStore

    store = WebhookStore(db_path=store_db)
    record = await store.create(
        name="GitHub Releases",
        secret="shh",
        action="chat",
        action_params={"prefix": "GH:"},
    )
    assert record["id"]
    assert record["name"] == "GitHub Releases"
    assert record["secret"] == "shh"
    assert record["url"] == f"/api/custom-webhooks/{record['id']}/receive"

    fetched = await store.get(record["id"])
    assert fetched is not None
    assert fetched["name"] == "GitHub Releases"
    assert fetched["action_params"] == {"prefix": "GH:"}


@pytest.mark.asyncio
async def test_persistence_across_store_instances(store_db):
    """The whole point: a second store instance reading the same DB
    sees rows the first one wrote. This is what a brain restart looks
    like at the storage layer."""
    from integrations.webhook_store import WebhookStore

    store_a = WebhookStore(db_path=store_db)
    record = await store_a.create(name="persists-across-restart")

    store_b = WebhookStore(db_path=store_db)
    rows = await store_b.list_all()
    ids = {r["id"] for r in rows}
    assert record["id"] in ids


@pytest.mark.asyncio
async def test_record_trigger_increments_counter(store_db):
    from integrations.webhook_store import WebhookStore

    store = WebhookStore(db_path=store_db)
    record = await store.create(name="counter")
    assert record["trigger_count"] == 0
    assert record["last_triggered"] is None

    await store.record_trigger(record["id"])
    await store.record_trigger(record["id"])
    fetched = await store.get(record["id"])
    assert fetched["trigger_count"] == 2
    assert fetched["last_triggered"] is not None


@pytest.mark.asyncio
async def test_delete_removes_row(store_db):
    from integrations.webhook_store import WebhookStore

    store = WebhookStore(db_path=store_db)
    record = await store.create(name="ephemeral")
    deleted = await store.delete(record["id"])
    assert deleted is True
    assert await store.get(record["id"]) is None
    deleted_again = await store.delete(record["id"])
    assert deleted_again is False


# ──────────────────────────────────────────────────────────────────────
# HTTP surface — routes now mounted at /api/custom-webhooks/*
# ──────────────────────────────────────────────────────────────────────


def test_create_via_http_uses_persistent_store(tmp_path):
    """``POST /api/custom-webhooks/create`` round-trips through
    :class:`WebhookStore`, NOT through the legacy ``_webhooks`` dict."""
    from integrations.webhook_store import WebhookStore
    from api.server import app
    from api.routes import webhooks as webhooks_mod

    store = WebhookStore(db_path=tmp_path / "http.db")
    webhooks_mod._webhooks.clear()

    with patch("api.routes.webhooks.state") as st:
        st.webhook_store = store
        st.orchestrator = None
        st.event_bus = None
        st.sessions = {}
        client = TestClient(app)
        r = client.post(
            "/api/custom-webhooks/create",
            json={"name": "via-http", "secret": "x", "action": "chat"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    webhook_id = body["webhook"]["id"]

    # And the legacy dict was NOT used:
    assert webhook_id not in webhooks_mod._webhooks

    # The DB has it persistently:
    import asyncio
    rec = asyncio.run(store.get(webhook_id))
    assert rec is not None
    assert rec["name"] == "via-http"


def test_list_via_http_returns_persistent_rows(tmp_path):
    from integrations.webhook_store import WebhookStore
    from api.server import app
    from api.routes import webhooks as webhooks_mod
    import asyncio

    store = WebhookStore(db_path=tmp_path / "list.db")
    webhooks_mod._webhooks.clear()
    record = asyncio.run(store.create(name="row-1"))

    with patch("api.routes.webhooks.state") as st:
        st.webhook_store = store
        st.orchestrator = None
        st.event_bus = None
        st.sessions = {}
        client = TestClient(app)
        r = client.get("/api/custom-webhooks/list")
    assert r.status_code == 200
    payload = r.json()
    ids = {h["id"] for h in payload["webhooks"]}
    assert record["id"] in ids


def test_delete_via_http_drops_persistent_row(tmp_path):
    from integrations.webhook_store import WebhookStore
    from api.server import app
    from api.routes import webhooks as webhooks_mod
    import asyncio

    store = WebhookStore(db_path=tmp_path / "delete.db")
    webhooks_mod._webhooks.clear()
    record = asyncio.run(store.create(name="will-die"))

    with patch("api.routes.webhooks.state") as st:
        st.webhook_store = store
        st.orchestrator = None
        st.event_bus = None
        st.sessions = {}
        client = TestClient(app)
        r = client.delete(f"/api/custom-webhooks/{record['id']}")
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert asyncio.run(store.get(record["id"])) is None


def test_receive_route_uses_persistent_store(tmp_path):
    """The receive route looks up hooks in the store, not the legacy
    dict — verifies the unification finding 19 demands."""
    from integrations.webhook_store import WebhookStore
    from api.server import app
    from api.routes import webhooks as webhooks_mod
    import asyncio

    store = WebhookStore(db_path=tmp_path / "receive.db")
    webhooks_mod._webhooks.clear()
    record = asyncio.run(store.create(name="r", secret="", action="noop"))

    with patch("api.routes.webhooks.state") as st:
        st.webhook_store = store
        st.orchestrator = None
        st.event_bus = None
        st.sessions = {}
        client = TestClient(app)
        r = client.post(
            f"/api/custom-webhooks/{record['id']}/receive",
            json={"event": "ping"},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    rec_after = asyncio.run(store.get(record["id"]))
    assert rec_after["trigger_count"] == 1
    assert rec_after["last_triggered"] is not None
