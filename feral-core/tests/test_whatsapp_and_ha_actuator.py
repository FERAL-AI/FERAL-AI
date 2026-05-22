"""
Lane 10 — WhatsApp signature verify + Home Assistant actuator round-trip.

Two finding-19 + THESIS_SCENARIOS-S5 contracts pinned here:

1. WhatsApp inbound webhook now calls ``verify_signature`` before
   processing — pre-Lane-10 it just parsed the JSON and forwarded to
   ``handle_webhook`` regardless of the X-Hub-Signature-256 header.
   When ``FERAL_WHATSAPP_APP_SECRET`` is configured an unsigned or
   wrongly-signed request is rejected with a 403.

2. Home Assistant integration ships ``vacuum_start``, ``vacuum_stop``,
   ``vacuum_return_to_base``, ``light_turn_on``, ``light_turn_off``
   on the dispatch table so the orchestrator can fire the actuator
   round-trip the THESIS_SCENARIOS S5 demo depends on. Lane 11 builds
   the smart-glasses ingestion side that consumes these endpoints.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


pytestmark = pytest.mark.no_auto_feral_home


# ──────────────────────────────────────────────────────────────────────
# WhatsApp signature verification
# ──────────────────────────────────────────────────────────────────────


def _wa_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _FakeChannelManager:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, name: str):
        return self._channel if name == "whatsapp" else None


def _make_whatsapp_channel(app_secret: str):
    from channels.base import WhatsAppChannel

    channel = WhatsAppChannel({"app_secret": app_secret})
    channel._app_secret = app_secret
    channel._known_chat_ids = set()

    async def noop(_msg):
        return None

    channel._handler = noop
    return channel


def test_whatsapp_inbound_rejects_invalid_signature():
    from api.server import app

    secret = "wa-app-secret"
    channel = _make_whatsapp_channel(secret)

    body = json.dumps({"entry": []}).encode()

    with patch("api.routes.channels.state") as st:
        st.channel_manager = _FakeChannelManager(channel)
        client = TestClient(app)
        r = client.post(
            "/api/channels/whatsapp/webhook",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=ffffffffffffffffffffff",
            },
        )
    assert r.status_code == 403
    assert r.json()["reason"] == "invalid_signature"


def test_whatsapp_inbound_rejects_missing_signature_when_secret_configured():
    from api.server import app

    secret = "wa-app-secret-2"
    channel = _make_whatsapp_channel(secret)

    body = json.dumps({"entry": []}).encode()

    with patch("api.routes.channels.state") as st:
        st.channel_manager = _FakeChannelManager(channel)
        client = TestClient(app)
        r = client.post(
            "/api/channels/whatsapp/webhook",
            content=body,
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 403
    assert r.json()["reason"] == "invalid_signature"


def test_whatsapp_inbound_accepts_valid_signature():
    from api.server import app

    secret = "wa-app-secret-3"
    channel = _make_whatsapp_channel(secret)

    body = json.dumps({"entry": []}).encode()

    with patch("api.routes.channels.state") as st:
        st.channel_manager = _FakeChannelManager(channel)
        client = TestClient(app)
        r = client.post(
            "/api/channels/whatsapp/webhook",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": _wa_sig(secret, body),
            },
        )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_whatsapp_inbound_accepts_when_no_app_secret_configured():
    """When the operator has not set ``FERAL_WHATSAPP_APP_SECRET`` the
    channel's ``verify_signature`` returns True so existing operators
    are not regressed. They opt into fail-closed by setting the
    secret."""
    from api.server import app

    channel = _make_whatsapp_channel("")  # no secret

    body = json.dumps({"entry": []}).encode()

    with patch("api.routes.channels.state") as st:
        st.channel_manager = _FakeChannelManager(channel)
        client = TestClient(app)
        r = client.post(
            "/api/channels/whatsapp/webhook",
            content=body,
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_whatsapp_inbound_no_handler_returns_503():
    from api.server import app

    with patch("api.routes.channels.state") as st:
        st.channel_manager = None
        client = TestClient(app)
        r = client.post("/api/channels/whatsapp/webhook", content=b"{}")
    assert r.status_code == 503


# ──────────────────────────────────────────────────────────────────────
# Home Assistant actuator round-trip (THESIS_SCENARIOS S5)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ha_vacuum_start_returns_started_envelope(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "fake-token")
    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()

    captured = []

    class FakeResp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    async def fake_post(self, path, json=None, **kwargs):
        captured.append({"path": path, "json": json})
        return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    result = await ha.execute(
        "vacuum_start",
        {"entity_id": "vacuum.living_room"},
    )
    assert result["success"] is True
    assert result["data"] == {
        "started": True,
        "entity_id": "vacuum.living_room",
        "service": "vacuum.start",
    }
    assert captured == [{
        "path": "/api/services/vacuum/start",
        "json": {"entity_id": "vacuum.living_room"},
    }]


@pytest.mark.asyncio
async def test_ha_vacuum_start_requires_entity_id(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "fake-token")
    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()
    result = await ha.execute("vacuum_start", {})
    assert result["success"] is False
    assert result["reason"] == "missing_entity_id"


@pytest.mark.asyncio
async def test_ha_light_turn_on_dispatch_alias(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "fake-token")
    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()

    class FakeResp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    captured = []

    async def fake_post(self, path, json=None, **kwargs):
        captured.append({"path": path, "json": json})
        return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    result = await ha.execute(
        "light_turn_on",
        {"entity_id": "light.kitchen", "brightness": 200},
    )
    assert result["success"] is True
    assert result["data"]["on"] is True
    assert result["data"]["entity_id"] == "light.kitchen"
    assert captured[-1]["path"] == "/api/services/light/turn_on"
    assert captured[-1]["json"]["entity_id"] == "light.kitchen"
    assert captured[-1]["json"]["brightness"] == 200


@pytest.mark.asyncio
async def test_ha_unknown_endpoint_rejected(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "fake-token")
    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()
    result = await ha.execute("not_a_thing", {})
    assert result["success"] is False
    assert "Unknown endpoint" in result["error"]


def test_ha_dispatch_table_includes_vacuum_and_light():
    """Defensive: the manifest contract test from Lane 02 walks the
    dispatch table and every endpoint in skills/manifests/smart_home.json
    must resolve. Lane 05 will rewrite that manifest to point at our
    method names; pin the names HERE so future Lane 05 work knows what
    to expect."""
    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()
    # Trigger dispatch lookup via execute()
    import asyncio

    async def _peek(endpoint):
        # Dispatch into execute so we hit the real switch — return the
        # dispatch error message for missing endpoints.
        result = await ha.execute(endpoint, {"entity_id": ""})
        return result.get("error", "")

    for endpoint in (
        "vacuum_start", "vacuum_stop", "vacuum_return_to_base",
        "light_turn_on", "light_turn_off",
    ):
        # If the endpoint is missing from the dispatch table, the
        # error is "Unknown endpoint: ..." — assert that is NOT what
        # we get for these names.
        msg = asyncio.run(_peek(endpoint))
        assert "Unknown endpoint" not in msg, (
            f"{endpoint} missing from HA dispatch table"
        )
