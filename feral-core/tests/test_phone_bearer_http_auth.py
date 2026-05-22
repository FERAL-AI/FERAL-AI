"""v2026.5.26 — phone-bearer HTTP auth via APIKeyMiddleware.

Pre-fix: APIKeyMiddleware accepted only ``Bearer ${FERAL_API_KEY}``,
so the iOS app's phone_bearer (minted during pair flow + accepted on
the WebSocket handshake via ``verify_phone_bearer``) 401'd on every
HTTP call. Operator screenshot showed the Context tab stuck on
"Waiting for brain auth — Brain rejected this request (401). The
brain needs to accept the phone bearer on HTTP — update the brain or
re-pair."

The fix path-allowlists a curated set of read-mostly endpoints to
also accept any non-expired phone bearer. Destructive endpoints
remain locked to the dashboard API key.

Eight tests:
* allowlisted GET with valid bearer → 200
* allowlisted GET with no auth → 401
* allowlisted GET with bogus bearer → 401
* allowlisted GET with expired bearer → 401
* destructive endpoint (DELETE) with valid phone bearer → 401
* allowlisted POST with valid bearer → 200
* path NOT in allowlist with valid bearer → 401
* dashboard ``FERAL_API_KEY`` still works on every endpoint
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch


_PHONE_BEARER = "phone-bearer-token-abc123"
_BAD_BEARER = "definitely-not-valid"
_DASHBOARD_KEY = "feral-api-key-xyz"


@pytest.fixture
def app_with_middleware(monkeypatch):
    """Boot a minimal FastAPI app with the APIKeyMiddleware applied.

    Patches FERAL_API_KEY + state.device_pairing_store.verify_phone_bearer
    so tests can exercise both auth paths without real key files or a
    SQLite pairing DB.
    """
    # Patch the module-global API key BEFORE importing server-side
    # code so the middleware reads our value.
    monkeypatch.setenv("FERAL_API_KEY", _DASHBOARD_KEY)
    # Force "localhost bypass" off so tests exercise the middleware
    # acceptance path, not the loopback exception.
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "0")
    # audit-r14 / Lane 06 — the conftest ``_disable_api_key_middleware_for_tests``
    # autouse fixture monkeypatches ``is_localhost`` to treat the
    # Starlette TestClient host (``"testclient"``) as loopback. With
    # the audit-r12 A1 middleware change (loopback bypass is now
    # unconditional, not gated on ``FERAL_LOCAL_BYPASS``), the
    # testclient-as-loopback shim would force every request below to
    # 200 — defeating the whole point of testing the off-loopback
    # auth paths. Restore the real ``is_localhost`` for these tests
    # so testclient is treated as a remote host.
    from security import session_auth as _sa
    from api import server as _server_for_patch
    real_is_localhost = lambda host: host in ("127.0.0.1", "::1", "localhost")
    monkeypatch.setattr(_sa, "is_localhost", real_is_localhost)
    monkeypatch.setattr(_server_for_patch, "is_localhost", real_is_localhost, raising=False)

    # Stub the pairing store: returns a fake device_id for valid
    # bearer, None otherwise. Mirrors the real
    # DevicePairingStore.verify_phone_bearer contract.
    def fake_verify(bearer: str):
        if bearer == _PHONE_BEARER:
            return "device-id-fake"
        return None

    mock_state = MagicMock()
    mock_state.device_pairing_store = MagicMock()
    mock_state.device_pairing_store.verify_phone_bearer = fake_verify

    # We re-import the middleware module under patches so its
    # closure picks up the test environment. The middleware class
    # itself is small and stateless; patching the module-level
    # FERAL_API_KEY + state at runtime is the cleanest path.
    from api import server as server_module

    monkeypatch.setattr(server_module, "FERAL_API_KEY", _DASHBOARD_KEY)

    # `state` is imported into the middleware via
    # `from api.state import state as _state` INSIDE the function
    # body — so we patch `api.state.state` (the module-level singleton
    # the middleware re-imports per request) AND any cached reference
    # on `server_module`.
    monkeypatch.setattr("api.state.state", mock_state)

    app = FastAPI()
    app.add_middleware(server_module.APIKeyMiddleware)

    # Light routes covering the spectrum the middleware classifies.
    @app.get("/api/context/live")                  # phone GET allowed
    async def ctx():
        return {"ok": True}

    @app.get("/api/capabilities")                  # phone GET allowed
    async def caps():
        return {"ok": True}

    @app.post("/api/system/permissions/open")  # phone POST allowed (literal)
    async def perms_open(body: dict | None = None):
        return {"ok": True}

    # v2026.5.32 (audit-r12 D1): canonical parameterised POST in the
    # allowlist. The pre-r12 allowlist had a literal
    # ``/api/approvals/approve`` that didn't match the real path
    # ``/api/approvals/{request_id}/approve``; this stub mirrors the
    # real route shape so the new ``_PathAllowlist`` parameterised
    # matcher is exercised end-to-end here too.
    @app.post("/api/approvals/{request_id}/approve")
    async def approve_post(request_id: str):
        return {"ok": True, "id": request_id}

    @app.delete("/api/skills/foo")                 # destructive
    async def delete_skill():
        return {"ok": True}

    @app.get("/api/some_other_endpoint")           # not in allowlist
    async def other():
        return {"ok": True}

    return app


def _client(app):
    return TestClient(app, raise_server_exceptions=False)


def test_allowlisted_get_with_valid_phone_bearer_returns_200(app_with_middleware):
    r = _client(app_with_middleware).get(
        "/api/context/live",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


def test_allowlisted_get_with_no_auth_returns_401(app_with_middleware):
    r = _client(app_with_middleware).get("/api/context/live")
    assert r.status_code == 401
    assert "Unauthorized" in r.json().get("error", "")


def test_allowlisted_get_with_bogus_bearer_returns_401(app_with_middleware):
    r = _client(app_with_middleware).get(
        "/api/context/live",
        headers={"Authorization": f"Bearer {_BAD_BEARER}"},
    )
    assert r.status_code == 401


def test_expired_phone_bearer_returns_401(app_with_middleware):
    # Simulate verify_phone_bearer returning None (which it does for
    # expired bearers per its docstring). Bogus bearer above already
    # covers the None path, but this test names the case explicitly
    # so a future implementation that differentiates expired vs
    # bogus has a place to grow.
    expired = "phone-bearer-was-valid-but-expired"
    r = _client(app_with_middleware).get(
        "/api/context/live",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r.status_code == 401


def test_destructive_endpoint_with_phone_bearer_returns_401(app_with_middleware):
    # DELETE on a non-allowlisted path must reject even a valid
    # phone bearer. Operator-owned destructive ops still require
    # dashboard FERAL_API_KEY.
    r = _client(app_with_middleware).delete(
        "/api/skills/foo",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 401


def test_allowlisted_post_with_phone_bearer_returns_200(app_with_middleware):
    # Phase 13 — open Settings pane (literal POST in the allowlist).
    r = _client(app_with_middleware).post(
        "/api/system/permissions/open",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
        json={"pane": "Microphone"},
    )
    assert r.status_code == 200


def test_allowlisted_parameterised_post_with_phone_bearer_returns_200(app_with_middleware):
    """v2026.5.32 (audit-r12 D1): the operator-approval surface uses a
    parameterised path (``/api/approvals/{request_id}/approve``). Pins
    that the new ``_PathAllowlist`` matcher accepts the concrete path."""
    r = _client(app_with_middleware).post(
        "/api/approvals/req-99/approve",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "id": "req-99"}


def test_non_allowlisted_get_with_phone_bearer_returns_401(app_with_middleware):
    # Defense-in-depth: even a valid phone bearer must NOT pass on
    # an endpoint that isn't in the GET allowlist.
    r = _client(app_with_middleware).get(
        "/api/some_other_endpoint",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 401


def test_dashboard_api_key_still_works_on_every_endpoint(app_with_middleware):
    c = _client(app_with_middleware)
    headers = {"Authorization": f"Bearer {_DASHBOARD_KEY}"}
    # Allowlisted GET — works with both bearers.
    assert c.get("/api/context/live", headers=headers).status_code == 200
    # Destructive — dashboard key only.
    assert c.delete("/api/skills/foo", headers=headers).status_code == 200
    # Non-allowlisted — dashboard key only.
    assert c.get("/api/some_other_endpoint", headers=headers).status_code == 200
