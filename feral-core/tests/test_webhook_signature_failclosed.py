"""
Lane 10 — webhook signature verification is fail-closed.

Pre-Lane-10 the integration ingress accepted requests whose secret was
configured but whose signature header was missing or invalid, marking
the resulting event ``verified=false`` and emitting it into the bus
anyway. Finding 19 explicitly calls this out: "bad sig still
**accepted** with ``verified=False``."

These tests pin the fixed contract:

* Secret configured + missing signature header → 401 ``missing_signature``.
* Secret configured + bad HMAC → 403 ``invalid_signature``.
* Secret configured + valid HMAC → 200 ``verified=true``.
* No secret configured → 200 ``verified=true`` (nothing to enforce).
* The route now passes the **real** ``Request.headers`` and the
  **raw** body so HMAC verification actually works through the public
  HTTP path. Pre-Lane-10 it passed ``headers={}`` always.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


pytestmark = pytest.mark.no_auto_feral_home


def _gh_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# Receiver-level (unit) — fail-closed contract
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_receiver_missing_sig_with_secret_returns_failclosed():
    from integrations.webhook_receiver import EventBus, WebhookReceiver

    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.set_secret("github", "secret-1")
    body = b'{"action":"opened"}'

    out = await recv.handle_request("github", body, {}, "application/json")
    assert out["accepted"] is False
    assert out["reason"] == "missing_signature"


@pytest.mark.asyncio
async def test_receiver_bad_sig_with_secret_returns_failclosed():
    from integrations.webhook_receiver import EventBus, WebhookReceiver

    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.set_secret("github", "secret-2")
    body = b"{}"
    headers = {"X-Hub-Signature-256": "sha256=deadbeef"}
    out = await recv.handle_request("github", body, headers, "application/json")
    assert out["accepted"] is False
    assert out["reason"] == "invalid_signature"


@pytest.mark.asyncio
async def test_receiver_unsigned_when_no_secret_configured():
    from integrations.webhook_receiver import EventBus, WebhookReceiver

    bus = EventBus()
    recv = WebhookReceiver(bus)
    # ``home_assistant`` ships no signature_header — no secret is the
    # supported configuration. A request must still be accepted.
    out = await recv.handle_request(
        "home_assistant", b'{"event_type":"state_changed"}', {},
        "application/json",
    )
    assert out["accepted"] is True
    assert out["verified"] is True


@pytest.mark.asyncio
async def test_receiver_valid_signature_emits_verified_event():
    from integrations.webhook_receiver import (
        EventBus, WebhookEvent, WebhookReceiver,
    )

    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.set_secret("github", "secret-3")
    body = b'{"action":"opened"}'
    captured: list[WebhookEvent] = []

    async def cap(ev: WebhookEvent):
        captured.append(ev)

    bus.on("github", cap)
    headers = {
        "X-Hub-Signature-256": _gh_sig("secret-3", body),
        "X-GitHub-Event": "pull_request",
    }
    out = await recv.handle_request("github", body, headers, "application/json")
    assert out["accepted"] is True
    assert out["verified"] is True
    assert out["event_type"] == "pull_request"
    assert captured and captured[0].verified is True


# ──────────────────────────────────────────────────────────────────────
# Route-level — real Request.headers + raw body forwarding
# ──────────────────────────────────────────────────────────────────────


def test_route_passes_real_headers_and_raw_body_to_receiver():
    """Pre-Lane-10 the route passed ``headers={}``; this test would
    have been impossible to pass. Now headers + raw body propagate to
    the receiver and the route surfaces the structured rejection."""
    from integrations.webhook_receiver import EventBus, WebhookReceiver
    from api.server import app

    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.set_secret("github", "via-route")
    body = json.dumps({"hello": "world"}).encode()
    sig = _gh_sig("via-route", body)

    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = recv
        st.event_bus = bus
        client = TestClient(app)
        r = client.post(
            "/api/webhooks/github",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": sig,
                "x-github-event": "push",
            },
        )
    assert r.status_code == 200
    payload = r.json()
    assert payload["accepted"] is True
    assert payload["verified"] is True
    assert payload["event_type"] == "push"


def test_route_rejects_missing_signature_with_401():
    from integrations.webhook_receiver import EventBus, WebhookReceiver
    from api.server import app

    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.set_secret("github", "needs-sig")

    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = recv
        st.event_bus = bus
        client = TestClient(app)
        r = client.post(
            "/api/webhooks/github",
            content=b"{}",
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 401
    body = r.json()
    assert body["reason"] == "missing_signature"


def test_route_rejects_invalid_signature_with_403():
    from integrations.webhook_receiver import EventBus, WebhookReceiver
    from api.server import app

    bus = EventBus()
    recv = WebhookReceiver(bus)
    recv.set_secret("github", "real-secret")

    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = recv
        st.event_bus = bus
        client = TestClient(app)
        r = client.post(
            "/api/webhooks/github",
            content=b'{"hello":"world"}',
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=00",
            },
        )
    assert r.status_code == 403
    body = r.json()
    assert body["reason"] == "invalid_signature"


def test_route_unknown_app_returns_400():
    from integrations.webhook_receiver import EventBus, WebhookReceiver
    from api.server import app

    bus = EventBus()
    recv = WebhookReceiver(bus)

    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = recv
        st.event_bus = bus
        client = TestClient(app)
        r = client.post("/api/webhooks/never-heard-of-it", json={})
    assert r.status_code == 400
    assert r.json()["reason"] == "unknown_app"
