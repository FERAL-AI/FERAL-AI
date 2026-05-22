"""
Tests for EventBus, WebhookReceiver, and FastAPI webhook routes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.server import app
from integrations.webhook_receiver import EventBus, WebhookEvent, WebhookReceiver, WebhookConfig

pytestmark = pytest.mark.no_auto_feral_home


# ── EventBus (subscribe via `on` / publish via `emit`) ──────────────────────


@pytest.mark.asyncio
async def test_event_bus_on_and_emit_delivers_to_handler():
    bus = EventBus()
    seen: list[WebhookEvent] = []

    async def h(ev: WebhookEvent):
        seen.append(ev)

    bus.on("my_app", h)
    ev = WebhookEvent(app_id="my_app", event_type="ping", payload={"x": 1})
    await bus.emit(ev)
    assert len(seen) == 1
    assert seen[0].app_id == "my_app"
    assert seen[0].payload["x"] == 1


@pytest.mark.asyncio
async def test_event_bus_on_all_receives_every_emit():
    bus = EventBus()
    global_hits: list[str] = []
    specific_hits: list[str] = []

    async def global_h(ev: WebhookEvent):
        global_hits.append(ev.app_id)

    async def specific_h(ev: WebhookEvent):
        specific_hits.append(ev.event_type)

    bus.on_all(global_h)
    bus.on("app_a", specific_h)
    await bus.emit(WebhookEvent(app_id="app_a", event_type="t1", payload={}))
    await bus.emit(WebhookEvent(app_id="other", event_type="t2", payload={}))
    assert global_hits == ["app_a", "other"]
    assert specific_hits == ["t1"]


@pytest.mark.asyncio
async def test_event_bus_emit_logs_and_recent_events_and_stats():
    bus = EventBus()
    bus.on("z", AsyncMock())
    await bus.emit(WebhookEvent(app_id="z", event_type="e", payload={"k": 2}))
    recent = bus.recent_events(5)
    assert recent[-1]["app_id"] == "z"
    assert recent[-1]["event_type"] == "e"
    st = bus.stats()
    assert "z" in st["registered_apps"]
    assert st["total_events"] == 1
    assert st["global_handlers"] == 0


@pytest.mark.asyncio
async def test_event_bus_handler_exception_does_not_block_following_handlers():
    bus = EventBus()

    async def bad(_ev: WebhookEvent):
        raise RuntimeError("boom")

    ok_calls = []

    async def good(ev: WebhookEvent):
        ok_calls.append(ev.event_type)

    bus.on("multi", bad)
    bus.on("multi", good)
    await bus.emit(WebhookEvent(app_id="multi", event_type="x", payload={}))
    assert ok_calls == ["x"]


@pytest.mark.asyncio
async def test_event_bus_global_handler_runs_alongside_app_handlers():
    bus = EventBus()
    seq: list[str] = []

    async def g1(ev: WebhookEvent):
        seq.append("g")

    async def a1(ev: WebhookEvent):
        seq.append("a")

    bus.on_all(g1)
    bus.on("app", a1)
    await bus.emit(WebhookEvent(app_id="app", event_type="t", payload={}))
    assert seq == ["a", "g"]


# ── WebhookReceiver ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_receiver_unknown_app_rejected():
    bus = EventBus()
    recv = WebhookReceiver(bus)
    out = await recv.handle_request(
        "not_registered_xyz",
        b"{}",
        {},
        "application/json",
    )
    assert out["accepted"] is False
    assert "error" in out


@pytest.mark.asyncio
async def test_webhook_receiver_github_hmac_verified_when_secret_matches():
    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.set_secret("github", "mysecret")
    body = b'{"action":"opened"}'
    sig = hmac.new(b"mysecret", body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": f"sha256={sig}",
        "X-GitHub-Event": "pull_request",
    }
    captured: list[WebhookEvent] = []

    async def cap(ev: WebhookEvent):
        captured.append(ev)

    bus.on("github", cap)
    out = await recv.handle_request("github", body, headers, "application/json")
    assert out["accepted"] is True
    assert out["verified"] is True
    assert out["event_type"] == "pull_request"
    assert captured and captured[0].verified is True


@pytest.mark.asyncio
async def test_webhook_receiver_github_bad_signature_failclosed():
    """Lane 10: a configured secret + bad signature is now fail-closed.
    Pre-Lane-10 this returned ``accepted=True, verified=False`` which
    let unauthenticated callers trigger downstream handlers anyway —
    finding 19 calls this out as the lying surface to fix.
    """
    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.set_secret("github", "mysecret")
    body = b"{}"
    headers = {"X-Hub-Signature-256": "sha256=deadbeef", "X-GitHub-Event": "push"}
    out = await recv.handle_request("github", body, headers, "application/json")
    assert out["accepted"] is False
    assert out["reason"] == "invalid_signature"


@pytest.mark.asyncio
async def test_webhook_receiver_stripe_event_type_from_payload():
    bus = EventBus()
    recv = WebhookReceiver(bus)
    body_dict = {"type": "invoice.paid", "data": {"object": {"id": "in_1"}}}
    body = json.dumps(body_dict).encode()
    out = await recv.handle_request("stripe", body, {}, "application/json")
    assert out["accepted"] is True
    assert out["event_type"] == "invoice.paid"


@pytest.mark.asyncio
async def test_webhook_receiver_home_assistant_event_type_resolution():
    bus = EventBus()
    recv = WebhookReceiver(bus)
    body = json.dumps({"event_type": "automation_triggered"}).encode()
    out = await recv.handle_request("home_assistant", body, {}, "application/json")
    assert out["accepted"] is True
    assert out["event_type"] == "automation_triggered"


# ── HTTP routes (verification against patched state) ──────────────────────


def test_receive_webhook_route_delegates_to_receiver():
    bus = EventBus()
    recv = WebhookReceiver(bus)

    async def track(ev: WebhookEvent):
        pass

    bus.on("notion", track)

    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = recv
        st.event_bus = bus
        client = TestClient(app)
        r = client.post("/api/webhooks/notion", json={"type": "page_updated", "id": "p1"})
    assert r.status_code == 200
    data = r.json()
    assert data.get("accepted") is True
    assert data.get("event_type") == "page_updated"


def test_list_webhooks_route_returns_configs_and_recent_events():
    bus = EventBus()
    recv = WebhookReceiver(bus)

    async def dummy(_ev: WebhookEvent):
        return None

    bus.on("github", dummy)

    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = recv
        st.event_bus = bus
        client = TestClient(app)
        r = client.get("/api/webhooks")
    assert r.status_code == 200
    payload = r.json()
    assert "webhooks" in payload
    assert isinstance(payload["webhooks"], list)
    assert "events" in payload


def test_receive_webhook_route_when_receiver_missing():
    """Lane 10: an unconfigured receiver returns 503 (service
    unavailable) rather than 200 with an error string in the body —
    that matches how the integration ingress now signals "this brain
    can't accept webhook traffic yet" without confusing the caller
    into thinking the request was processed."""
    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = None
        st.event_bus = None
        client = TestClient(app)
        r = client.post("/api/webhooks/github", json={})
    assert r.status_code == 503
    assert "error" in r.json()


def test_webhook_receiver_register_webhook_custom_app():
    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.register_webhook(WebhookConfig(app_id="custom", enabled=True))
    out = recv.list_webhooks()
    ids = {x["app_id"] for x in out}
    assert "custom" in ids


# ── Custom-webhook ingress hardening (fail-closed signature check) ──────────
#
# /api/webhooks/{id}/receive previously fell open: when a webhook had a
# secret configured but the inbound request omitted the signature header
# it skipped HMAC verification and accepted the payload anyway. These
# tests pin the fixed contract:
#   * valid HMAC → 200
#   * secret set but signature header missing → 401
#   * secret set but signature mismatched → 403
#   * no secret configured → unsigned request still accepted (200)


def _register_custom_webhook(secret: str = "") -> str:
    """Insert a webhook directly into the route module's registry.

    The /api/webhooks/create POST route shares its prefix with the
    integrations webhooks router (`/api/webhooks/{app_id}`) which is
    registered first and therefore swallows the create call. To keep
    these tests focused on the signature-validation contract — and
    independent of that unrelated routing collision — we bypass the
    create endpoint and seed the in-memory store directly.
    """
    import time as _time
    from uuid import uuid4

    from api.routes import webhooks as webhooks_mod

    webhook_id = str(uuid4())[:12]
    webhooks_mod._webhooks[webhook_id] = {
        "id": webhook_id,
        "name": "test-hook",
        "secret": secret,
        "action": "noop",
        "action_params": {},
        "created_at": _time.time(),
        "last_triggered": None,
        "trigger_count": 0,
        "url": f"/api/webhooks/{webhook_id}/receive",
    }
    return webhook_id


@pytest.fixture(autouse=True)
def _clear_custom_webhook_registry():
    from api.routes import webhooks as webhooks_mod

    snapshot = dict(webhooks_mod._webhooks)
    webhooks_mod._webhooks.clear()
    yield
    webhooks_mod._webhooks.clear()
    webhooks_mod._webhooks.update(snapshot)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_custom_webhook_valid_signature_accepted():
    secret = "shh-its-a-secret"
    webhook_id = _register_custom_webhook(secret=secret)
    body = json.dumps({"event": "ping"}).encode()
    client = TestClient(app)
    r = client.post(
        f"/api/webhooks/{webhook_id}/receive",
        content=body,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": _sign(secret, body),
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_custom_webhook_missing_signature_rejected_when_secret_configured():
    secret = "another-secret"
    webhook_id = _register_custom_webhook(secret=secret)
    body = json.dumps({"event": "ping"}).encode()
    client = TestClient(app)
    r = client.post(
        f"/api/webhooks/{webhook_id}/receive",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 401
    assert "signature" in r.text.lower()


def test_custom_webhook_invalid_signature_rejected():
    secret = "yet-another-secret"
    webhook_id = _register_custom_webhook(secret=secret)
    body = json.dumps({"event": "ping"}).encode()
    client = TestClient(app)
    r = client.post(
        f"/api/webhooks/{webhook_id}/receive",
        content=body,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": "sha256=deadbeef",
        },
    )
    assert r.status_code == 403
    assert "signature" in r.text.lower()


def test_custom_webhook_without_secret_accepts_unsigned_request():
    webhook_id = _register_custom_webhook(secret="")
    body = json.dumps({"event": "ping"}).encode()
    client = TestClient(app)
    r = client.post(
        f"/api/webhooks/{webhook_id}/receive",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
