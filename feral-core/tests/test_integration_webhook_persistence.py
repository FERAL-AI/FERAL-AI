"""
W19 (finding-19 cross-cut) — integration ingress webhook configs
survive a brain restart.

Pre-v2026.5.43 ``WebhookReceiver._configs`` was a process-local dict
seeded with stub entries at boot; any secret an operator set via
``set_secret("github", ...)`` lived only in RAM and silently vanished
on the next brain restart. From v2026.5.43 the receiver writes
through to :class:`integrations.webhook_store.WebhookStore` (table
``integration_webhooks``) and hydrates the cache from sqlite at boot,
so external services keep validating against the same shared key.

The tests below pin that contract:

1. A secret written via the store hydrates back into a fresh
   receiver and validates an HMAC-signed body.
2. ``hydrate_from_store`` seeds the four default integrations only
   when the store has no entry for them — an operator-edited GitHub
   secret keeps its value after restart.
3. ``PUT /api/webhooks/{app_id}/config`` persists through to the
   store; restart-simulated receiver still validates the same key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def store_db(tmp_path):
    return tmp_path / "integration-webhooks.db"


# ──────────────────────────────────────────────────────────────────────
# Store round-trip
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_and_get_integration_round_trip(store_db):
    from integrations.webhook_store import WebhookStore
    from integrations.webhook_receiver import WebhookConfig

    store = WebhookStore(db_path=store_db)
    await store.upsert_integration(WebhookConfig(
        app_id="github",
        secret="hub-secret",
        signature_header="X-Hub-Signature-256",
        signature_prefix="sha256=",
        hash_algorithm="sha256",
        enabled=True,
    ))
    fetched = await store.get_integration("github")
    assert fetched is not None
    assert fetched.app_id == "github"
    assert fetched.secret == "hub-secret"
    assert fetched.signature_header == "X-Hub-Signature-256"
    assert fetched.signature_prefix == "sha256="
    assert fetched.hash_algorithm == "sha256"
    assert fetched.enabled is True


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_overwrites(store_db):
    from integrations.webhook_store import WebhookStore
    from integrations.webhook_receiver import WebhookConfig

    store = WebhookStore(db_path=store_db)
    await store.upsert_integration(WebhookConfig(app_id="github", secret="first"))
    await store.upsert_integration(WebhookConfig(app_id="github", secret="second"))
    fetched = await store.get_integration("github")
    assert fetched.secret == "second"
    rows = await store.list_integrations()
    assert [r.app_id for r in rows] == ["github"]


@pytest.mark.asyncio
async def test_list_and_delete_integration(store_db):
    from integrations.webhook_store import WebhookStore
    from integrations.webhook_receiver import WebhookConfig

    store = WebhookStore(db_path=store_db)
    await store.upsert_integration(WebhookConfig(app_id="stripe", secret="s"))
    await store.upsert_integration(WebhookConfig(app_id="github", secret="g"))
    rows = await store.list_integrations()
    assert {r.app_id for r in rows} == {"stripe", "github"}

    assert await store.delete_integration("stripe") is True
    assert await store.delete_integration("stripe") is False
    rows = await store.list_integrations()
    assert {r.app_id for r in rows} == {"github"}


# ──────────────────────────────────────────────────────────────────────
# Receiver hydrate contract
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_integration_secret_survives_receiver_restart(tmp_path):
    """The whole point: secret persisted before restart still
    validates an HMAC-signed request after a fresh receiver loads
    from the same DB."""
    from integrations.webhook_store import WebhookStore
    from integrations.webhook_receiver import WebhookReceiver, WebhookConfig

    db_path = tmp_path / "wh.db"
    store = WebhookStore(db_path=db_path)
    await store.upsert_integration(WebhookConfig(
        app_id="github",
        secret="hub-secret",
        signature_header="X-Hub-Signature-256",
        signature_prefix="sha256=",
        hash_algorithm="sha256",
        enabled=True,
    ))

    store_after = WebhookStore(db_path=db_path)
    recv = WebhookReceiver(event_bus=AsyncMock(), store=store_after)
    await recv.hydrate_from_store()

    body = b'{"action":"opened"}'
    valid_sig = "sha256=" + hmac.new(b"hub-secret", body, hashlib.sha256).hexdigest()
    result = await recv.handle_request(
        "github", body,
        headers={
            "X-Hub-Signature-256": valid_sig,
            "X-GitHub-Event": "pull_request",
        },
        content_type="application/json",
    )
    assert result["accepted"] is True
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_default_apps_seeded_only_when_absent(tmp_path):
    """Hydrate seeds the four default integration stubs on first
    boot but does not overwrite an operator-edited secret on later
    boots."""
    from integrations.webhook_store import WebhookStore
    from integrations.webhook_receiver import WebhookReceiver, WebhookConfig

    db_path = tmp_path / "defaults.db"

    store = WebhookStore(db_path=db_path)
    recv = WebhookReceiver(event_bus=AsyncMock(), store=store)
    await recv.hydrate_from_store()
    seeded = {c.app_id for c in await store.list_integrations()}
    assert {"github", "stripe", "home_assistant", "notion"}.issubset(seeded)

    await store.upsert_integration(WebhookConfig(
        app_id="github",
        secret="custom-edited",
        signature_header="X-Hub-Signature-256",
        signature_prefix="sha256=",
        hash_algorithm="sha256",
        enabled=True,
    ))

    store2 = WebhookStore(db_path=db_path)
    recv2 = WebhookReceiver(event_bus=AsyncMock(), store=store2)
    await recv2.hydrate_from_store()

    refreshed = await store2.get_integration("github")
    assert refreshed.secret == "custom-edited"


@pytest.mark.asyncio
async def test_set_secret_persistent_writes_through(tmp_path):
    from integrations.webhook_store import WebhookStore
    from integrations.webhook_receiver import WebhookReceiver

    db_path = tmp_path / "persistent.db"
    store = WebhookStore(db_path=db_path)
    recv = WebhookReceiver(event_bus=AsyncMock(), store=store)
    await recv.hydrate_from_store()

    await recv.set_secret_persistent("github", "rotated-key")

    store2 = WebhookStore(db_path=db_path)
    fetched = await store2.get_integration("github")
    assert fetched is not None
    assert fetched.secret == "rotated-key"
    # signature header survived from the default config
    assert fetched.signature_header == "X-Hub-Signature-256"


# ──────────────────────────────────────────────────────────────────────
# HTTP surface — PUT /api/webhooks/{app_id}/config
# ──────────────────────────────────────────────────────────────────────


def test_put_config_route_persists(tmp_path):
    """PUT /api/webhooks/github/config writes through the receiver to
    the store. A second receiver pointed at the same DB sees the
    secret on hydrate."""
    import asyncio
    from integrations.webhook_store import WebhookStore
    from integrations.webhook_receiver import WebhookReceiver
    from api.server import app

    db_path = tmp_path / "put.db"
    store = WebhookStore(db_path=db_path)
    recv = WebhookReceiver(event_bus=AsyncMock(), store=store)
    asyncio.run(recv.hydrate_from_store())

    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = recv
        st.event_bus = None
        client = TestClient(app)
        r = client.put(
            "/api/webhooks/github/config",
            json={"secret": "via-put"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["has_secret"] is True
    assert "secret" not in body  # never echo raw secret back

    # Simulate restart: fresh receiver / fresh store instance, same DB.
    store2 = WebhookStore(db_path=db_path)
    recv2 = WebhookReceiver(event_bus=AsyncMock(), store=store2)
    asyncio.run(recv2.hydrate_from_store())
    assert recv2._configs["github"].secret == "via-put"


def test_put_config_route_can_disable_app(tmp_path):
    import asyncio
    from integrations.webhook_store import WebhookStore
    from integrations.webhook_receiver import WebhookReceiver
    from api.server import app

    db_path = tmp_path / "disable.db"
    store = WebhookStore(db_path=db_path)
    recv = WebhookReceiver(event_bus=AsyncMock(), store=store)
    asyncio.run(recv.hydrate_from_store())

    with patch("api.routes.integrations_webhooks.state") as st:
        st.webhook_receiver = recv
        st.event_bus = None
        client = TestClient(app)
        r = client.put(
            "/api/webhooks/github/config",
            json={"enabled": False},
        )
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    fetched = asyncio.run(store.get_integration("github"))
    assert fetched is not None
    assert fetched.enabled is False
