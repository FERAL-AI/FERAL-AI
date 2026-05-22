"""
Lane 10 — outgoing webhook deliveries.

Pre-Lane-10 the brain had no way to POST internal events out to
operator-subscribed URLs. Finding 19: "Outgoing webhooks: NONE."
Lane 10 closes the gap with:

* :class:`OutgoingWebhookStore` — durable subscriptions at
  ``~/.feral/outgoing_webhooks.db``.
* :class:`OutgoingWebhookDeliverer` — ``EventBus`` subscriber that
  POSTs signed JSON to matching URLs with HMAC-SHA256 + exponential
  backoff retries on 5xx / network errors.
* ``/api/outgoing-webhooks/*`` REST surface for create / list /
  delete / test-fire.

These tests pin: signature contract, retry policy, 4xx non-retriable,
event-type pattern matching, and the REST round-trip.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def store_db(tmp_path):
    return tmp_path / "outgoing.db"


# ──────────────────────────────────────────────────────────────────────
# Store contract
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_create_get_round_trip(store_db):
    from integrations.outgoing_webhooks import OutgoingWebhookStore

    store = OutgoingWebhookStore(db_path=store_db)
    record = await store.create(
        name="zapier",
        target_url="https://hooks.zapier.com/abc",
        secret="shh",
        event_types=["chat.completed", "memory.*"],
    )
    fetched = await store.get(record["id"])
    assert fetched is not None
    assert fetched["target_url"] == "https://hooks.zapier.com/abc"
    assert fetched["event_types"] == ["chat.completed", "memory.*"]
    assert fetched["enabled"] is True
    assert fetched["delivery_count"] == 0
    assert fetched["failure_count"] == 0


@pytest.mark.asyncio
async def test_store_persists_across_instances(store_db):
    from integrations.outgoing_webhooks import OutgoingWebhookStore

    a = OutgoingWebhookStore(db_path=store_db)
    rec = await a.create(name="x", target_url="https://example.com/h")
    b = OutgoingWebhookStore(db_path=store_db)
    rows = await b.list_all()
    assert any(r["id"] == rec["id"] for r in rows)


# ──────────────────────────────────────────────────────────────────────
# Pattern matching
# ──────────────────────────────────────────────────────────────────────


def test_matches_event_wildcard():
    from integrations.outgoing_webhooks import matches_event

    assert matches_event(["*"], "chat.completed")
    assert matches_event([], "chat.completed")  # empty == accept all
    assert matches_event(["chat.completed"], "chat.completed")
    assert not matches_event(["chat.completed"], "memory.saved")
    assert matches_event(["memory.*"], "memory.saved")
    assert not matches_event(["memory.*"], "chat.completed")


# ──────────────────────────────────────────────────────────────────────
# Signing contract
# ──────────────────────────────────────────────────────────────────────


def test_sign_payload_uses_sha256_with_prefix():
    from integrations.outgoing_webhooks import sign_payload

    body = b'{"hello":"world"}'
    sig = sign_payload("secret", body)
    expected = (
        "sha256="
        + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    )
    assert sig == expected


def test_sign_payload_empty_secret_returns_empty_string():
    from integrations.outgoing_webhooks import sign_payload

    assert sign_payload("", b"body") == ""


# ──────────────────────────────────────────────────────────────────────
# Deliverer — happy path with HMAC header
# ──────────────────────────────────────────────────────────────────────


def _make_event(event_type: str = "chat.completed", payload=None):
    from integrations.webhook_receiver import WebhookEvent

    return WebhookEvent(
        app_id="brain",
        event_type=event_type,
        payload=payload or {"text": "hi"},
        timestamp=1700000000.0,
    )


@pytest.mark.asyncio
async def test_deliver_happy_path_hmac_header(store_db):
    from integrations.outgoing_webhooks import (
        OutgoingWebhookDeliverer,
        OutgoingWebhookStore,
    )

    store = OutgoingWebhookStore(db_path=store_db)
    sub = await store.create(
        name="z",
        target_url="https://example.invalid/hook",
        secret="my-secret",
        event_types=["chat.completed"],
    )

    captured: list[dict] = []

    async def handler(request):
        captured.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": request.content,
        })
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        deliverer = OutgoingWebhookDeliverer(store=store, http_client=client)
        ok = await deliverer.deliver_now(sub["id"], _make_event())
        assert ok is True

    assert len(captured) == 1
    sent = captured[0]
    assert sent["url"] == "https://example.invalid/hook"
    body = sent["body"]
    expected_sig = (
        "sha256=" + hmac.new(b"my-secret", body, hashlib.sha256).hexdigest()
    )
    assert sent["headers"]["x-feral-signature-256"] == expected_sig
    assert sent["headers"]["x-feral-event"] == "chat.completed"
    assert sent["headers"]["x-feral-webhook-id"] == sub["id"]
    envelope = json.loads(body)
    assert envelope["event_type"] == "chat.completed"
    assert envelope["delivery_attempt"] == 1
    assert envelope["payload"] == {"text": "hi"}

    fetched = await store.get(sub["id"])
    assert fetched["delivery_count"] == 1
    assert fetched["last_error"] in (None, "")
    assert fetched["last_delivered"] is not None


# ──────────────────────────────────────────────────────────────────────
# Retry behaviour
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_5xx_retries_with_backoff_and_eventually_succeeds(
    store_db, monkeypatch,
):
    from integrations.outgoing_webhooks import (
        OutgoingWebhookDeliverer,
        OutgoingWebhookStore,
    )

    store = OutgoingWebhookStore(db_path=store_db)
    sub = await store.create(
        name="flaky",
        target_url="https://example.invalid/flaky",
    )

    attempts = {"n": 0}

    async def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    sleeps: list[float] = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    async with httpx.AsyncClient(transport=transport) as client:
        deliverer = OutgoingWebhookDeliverer(
            store=store,
            http_client=client,
            base_backoff_seconds=0.5,
            max_retries=3,
        )
        ok = await deliverer.deliver_now(sub["id"], _make_event())
    assert ok is True
    assert attempts["n"] == 3
    # We slept twice (between attempts 1→2 and 2→3); jittered so we
    # only assert the count and rough cap.
    assert len(sleeps) == 2
    assert all(s <= 30.0 for s in sleeps)
    fetched = await store.get(sub["id"])
    assert fetched["delivery_count"] == 1


@pytest.mark.asyncio
async def test_4xx_is_non_retriable(store_db, monkeypatch):
    from integrations.outgoing_webhooks import (
        OutgoingWebhookDeliverer,
        OutgoingWebhookStore,
    )

    store = OutgoingWebhookStore(db_path=store_db)
    sub = await store.create(name="bad", target_url="https://x.invalid/h")

    attempts = {"n": 0}

    async def handler(request):
        attempts["n"] += 1
        return httpx.Response(400, text="malformed")

    transport = httpx.MockTransport(handler)

    async def fast_sleep(_s):
        return None

    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    async with httpx.AsyncClient(transport=transport) as client:
        deliverer = OutgoingWebhookDeliverer(
            store=store, http_client=client, max_retries=3,
        )
        ok = await deliverer.deliver_now(sub["id"], _make_event())
    assert ok is False
    assert attempts["n"] == 1, "4xx must NOT trigger a retry"
    fetched = await store.get(sub["id"])
    assert fetched["failure_count"] == 1
    assert "http_400" in (fetched["last_error"] or "")


@pytest.mark.asyncio
async def test_network_error_retries_then_gives_up(store_db, monkeypatch):
    from integrations.outgoing_webhooks import (
        OutgoingWebhookDeliverer,
        OutgoingWebhookStore,
    )

    store = OutgoingWebhookStore(db_path=store_db)
    sub = await store.create(name="dns", target_url="https://x.invalid/h")

    attempts = {"n": 0}

    async def handler(request):
        attempts["n"] += 1
        raise httpx.ConnectError("dns fail")

    transport = httpx.MockTransport(handler)

    async def fast_sleep(_s):
        return None

    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    async with httpx.AsyncClient(transport=transport) as client:
        deliverer = OutgoingWebhookDeliverer(
            store=store, http_client=client, max_retries=2,
        )
        ok = await deliverer.deliver_now(sub["id"], _make_event())
    assert ok is False
    # max_retries=2 → 1 initial + 2 retries = 3 attempts
    assert attempts["n"] == 3
    fetched = await store.get(sub["id"])
    assert fetched["failure_count"] == 1
    assert "network_error" in (fetched["last_error"] or "")


# ──────────────────────────────────────────────────────────────────────
# EventBus integration — handle_event filters by event_type
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_event_skips_non_matching_subscriptions(store_db):
    from integrations.outgoing_webhooks import (
        OutgoingWebhookDeliverer,
        OutgoingWebhookStore,
    )

    store = OutgoingWebhookStore(db_path=store_db)
    await store.create(
        name="m", target_url="https://example.invalid/m",
        event_types=["chat.completed"],
    )
    await store.create(
        name="s", target_url="https://example.invalid/s",
        event_types=["memory.saved"],
    )

    posts: list[str] = []

    async def handler(request):
        posts.append(str(request.url))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        deliverer = OutgoingWebhookDeliverer(store=store, http_client=client)
        await deliverer.handle_event(_make_event(event_type="chat.completed"))
        for task in list(deliverer._pending):
            await task
    assert posts == ["https://example.invalid/m"]


# ──────────────────────────────────────────────────────────────────────
# REST surface
# ──────────────────────────────────────────────────────────────────────


def test_rest_create_validates_target_url(tmp_path):
    from api.server import app
    from integrations.outgoing_webhooks import OutgoingWebhookStore

    store = OutgoingWebhookStore(db_path=tmp_path / "db.db")

    with patch("api.routes.outgoing_webhooks.state") as st:
        st.outgoing_webhook_store = store
        st.outgoing_webhook_deliverer = None
        st.event_bus = None
        client = TestClient(app)
        r = client.post(
            "/api/outgoing-webhooks",
            json={"name": "no-url"},
        )
    assert r.status_code == 400
    assert r.json()["reason"] == "missing_target_url"


def test_rest_create_rejects_non_http_url(tmp_path):
    from api.server import app
    from integrations.outgoing_webhooks import OutgoingWebhookStore

    store = OutgoingWebhookStore(db_path=tmp_path / "db2.db")

    with patch("api.routes.outgoing_webhooks.state") as st:
        st.outgoing_webhook_store = store
        st.outgoing_webhook_deliverer = None
        st.event_bus = None
        client = TestClient(app)
        r = client.post(
            "/api/outgoing-webhooks",
            json={"target_url": "ftp://hosts.invalid/x"},
        )
    assert r.status_code == 400
    assert r.json()["reason"] == "invalid_target_url"


def test_rest_create_then_list_round_trip(tmp_path):
    from api.server import app
    from integrations.outgoing_webhooks import OutgoingWebhookStore

    store = OutgoingWebhookStore(db_path=tmp_path / "rt.db")

    with patch("api.routes.outgoing_webhooks.state") as st:
        st.outgoing_webhook_store = store
        st.outgoing_webhook_deliverer = None
        st.event_bus = None
        client = TestClient(app)
        r = client.post(
            "/api/outgoing-webhooks",
            json={
                "name": "zap",
                "target_url": "https://example.invalid/h",
                "secret": "shh",
                "event_types": ["chat.completed"],
            },
        )
    assert r.status_code == 200
    webhook_id = r.json()["webhook"]["id"]

    with patch("api.routes.outgoing_webhooks.state") as st:
        st.outgoing_webhook_store = store
        st.outgoing_webhook_deliverer = None
        st.event_bus = None
        client = TestClient(app)
        r = client.get("/api/outgoing-webhooks")
    assert r.status_code == 200
    rows = r.json()["webhooks"]
    found = next((row for row in rows if row["id"] == webhook_id), None)
    assert found is not None
    # Secret is fingerprinted, never returned in cleartext.
    assert found["secret"] == "•" * 8
    assert found["has_secret"] is True
