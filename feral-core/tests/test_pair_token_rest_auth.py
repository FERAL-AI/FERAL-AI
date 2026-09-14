"""A native node's pair token authenticates the phone REST allowlist.

The native iOS app holds only its pair token: /pair/complete mints a phone
bearer solely for ``browser_node_v2``, and APIKeyMiddleware accepted only
phone bearers on ``_PHONE_BEARER_GET`` / ``_PHONE_BEARER_POST``. So
``GET /api/sessions/primary``, its transcript and
``/api/memory/recent_summary`` all returned 401 to the app.

A pair token is now accepted on those paths, but only once its device has
claimed it and its PIN (if any) has been verified. The /v1/node handshake's
``_verify_credential`` is not reused, because its ``verify_device`` claims an
unclaimed token as a side effect and never checks the PIN: over HTTP that
would let anyone holding a pairing QR code read the transcript without it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from security.device_pairing import DevicePairingStore

_DASHBOARD_KEY = "dashboard-key-pair-token-rest"


@pytest.fixture
def store(tmp_path):
    return DevicePairingStore(db_path=str(tmp_path / "pairs.db"))


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setenv("FERAL_API_KEY", _DASHBOARD_KEY)
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "0")
    from security import session_auth as _sa
    from api import server as server_module

    def real_is_localhost(host):
        return host in ("127.0.0.1", "::1", "localhost")

    monkeypatch.setattr(_sa, "is_localhost", real_is_localhost)
    monkeypatch.setattr(server_module, "is_localhost", real_is_localhost, raising=False)
    monkeypatch.setattr(server_module, "FERAL_API_KEY", _DASHBOARD_KEY)

    mock_state = MagicMock()
    mock_state.device_pairing_store = store
    mock_state.primary_session_id = "primary-native-node"
    monkeypatch.setattr("api.state.state", mock_state)
    monkeypatch.setattr("api.routes.sessions.state", mock_state)

    from api.routes.sessions import router as sessions_router

    app = FastAPI()
    app.add_middleware(server_module.APIKeyMiddleware)
    app.include_router(sessions_router)

    @app.post("/api/health/ingest")
    async def ingest(request: Request):
        return {"ok": True, "kind": getattr(request.state, "device_credential_kind", None)}

    @app.get("/api/config")
    async def config():
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def _native_node(store, *, require_pin=False):
    return store.pair_device("Omar's iPhone", kind="hup", node_id="feral-iphone-native",
                             require_pin=require_pin)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_native_node_reads_primary_session_with_its_pair_token(store, client):
    issued = _native_node(store)
    assert "phone_bearer" not in issued, "a native node never receives a phone bearer"
    store.mark_claimed(issued["token"])

    resp = client.get("/api/sessions/primary", headers=_bearer(issued["token"]))
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "primary-native-node"


def test_pair_token_also_covers_allowlisted_posts(store, client):
    issued = _native_node(store)
    store.mark_claimed(issued["token"])
    resp = client.post("/api/health/ingest", headers=_bearer(issued["token"]), json={})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "pair_token"


def test_an_unclaimed_pairing_code_is_refused_and_stays_unclaimed(store, client):
    """A QR code on screen is not yet a device."""
    issued = _native_node(store)
    resp = client.get("/api/sessions/primary", headers=_bearer(issued["token"]))
    assert resp.status_code == 401
    assert store.token_claimed(issued["token"]) is False, "an HTTP read must not claim a token"


def test_a_claimed_token_whose_pin_was_never_verified_is_refused(store, client):
    issued = _native_node(store, require_pin=True)
    store.mark_claimed(issued["token"])
    assert client.get("/api/sessions/primary", headers=_bearer(issued["token"])).status_code == 401

    ok, _ = store.verify_pin(issued["token"], issued["pin"])
    assert ok
    assert client.get("/api/sessions/primary", headers=_bearer(issued["token"])).status_code == 200


def test_a_revoked_device_is_refused(store, client):
    issued = _native_node(store)
    store.mark_claimed(issued["token"])
    store.revoke_device(issued["device_id"])
    assert client.get("/api/sessions/primary", headers=_bearer(issued["token"])).status_code == 401


def test_a_pair_token_does_not_open_paths_outside_the_phone_allowlist(store, client):
    issued = _native_node(store)
    store.mark_claimed(issued["token"])
    assert client.get("/api/config", headers=_bearer(issued["token"])).status_code == 401


def test_a_wrong_token_is_refused(store, client):
    _native_node(store)
    assert client.get("/api/sessions/primary", headers=_bearer("not-a-real-token")).status_code == 401
