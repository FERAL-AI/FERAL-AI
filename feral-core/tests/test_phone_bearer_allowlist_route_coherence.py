"""D1 — Phone-bearer allowlist ↔ real route coherence (audit-r12).

Pins the contract that every entry in every API-key middleware
allowlist (``_OPEN_PATHS``, ``_OPEN_GET_PATHS``, ``_PHONE_BEARER_GET``,
``_PHONE_BEARER_POST``) MUST resolve to at least one route actually
registered on ``api.server.app``.

Before this PR five entries had drifted:

- ``_PHONE_BEARER_GET`` advertised ``/api/ambient/digest``; the real
  ambient summary route is ``/api/ambient/briefing``
  (``api/routes/ambient.py:30``). iOS callers got 401 silently.
- ``_PHONE_BEARER_POST`` advertised ``/api/approvals/approve`` and
  ``/api/approvals/deny``; the real routes are
  ``/api/approvals/{request_id}/approve`` and
  ``/api/approvals/{request_id}/reject``
  (``api/routes/approvals.py:79, 93``). Operator approval gestures
  on the iOS Approvals tab 401'd silently.
- ``_PHONE_BEARER_POST`` advertised ``/api/sessions/primary/transcript``
  and ``/api/capabilities/has`` as POSTs; both routes are GET only
  (``api/routes/sessions.py:34``, ``api/routes/capabilities.py:129``).
  Dead allowlist entries — they didn't break anything operationally
  but proved the allowlist had no integrity check.

The fix is two parts:

1. Replace the per-method ``frozenset`` literals with a ``_PathAllowlist``
   that supports literal paths, prefix matches, and FastAPI-style
   parameterized patterns (``/api/approvals/{request_id}/approve``)
   compiled via Starlette's ``compile_path`` — so the matcher cannot
   diverge from FastAPI's own router by construction.
2. Run a startup invariant after ``app.include_router(...)`` is done
   that iterates ``app.routes`` and refuses to boot the brain if any
   allowlist entry doesn't resolve to a real route.

Tests in this file:

* ``test_startup_invariant_passes_against_real_app`` — the canonical
  coherence test. Runs ``_assert_allowlist_routes_exist(app)`` against
  the real ``api.server.app``; would have failed before the fix
  because of the five drifted entries above.
* Middleware behavior on the canonical paths under a phone bearer
  (approve / reject / briefing return non-401) and without auth (401).
* Removed paths (``/api/ambient/digest``, ``/api/approvals/approve``)
  must NOT be honored on a phone bearer after the fix.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock


_PHONE_BEARER = "phone-bearer-token-d1-coherence"
_DASHBOARD_KEY = "dashboard-api-key-d1-coherence"


# ──────────────────────────────────────────────────────────────────────
# Coherence invariant against the real app
# ──────────────────────────────────────────────────────────────────────


def test_startup_invariant_passes_against_real_app():
    """Every allowlist entry must resolve to a registered route on
    ``api.server.app``. This is the assertion the brain runs at boot;
    pinning it here means a future route rename can't silently re-drift
    the iOS surface."""
    from api.server import app, _assert_allowlist_routes_exist

    _assert_allowlist_routes_exist(app)


def test_startup_invariant_detects_synthetic_drift(monkeypatch):
    """If we inject a stale entry into one of the allowlists, the
    invariant must raise — proving it actually checks rather than
    silently passing."""
    from api import server as server_module

    server_module._PHONE_BEARER_GET.add_literal("/api/does-not-exist-r12")
    try:
        with pytest.raises(RuntimeError, match=r"does-not-exist-r12"):
            server_module._assert_allowlist_routes_exist(server_module.app)
    finally:
        # The literal set is internal but exposed for the invariant; we
        # tear it down so the rest of the suite keeps the real app
        # invariant green.
        server_module._PHONE_BEARER_GET._literals.discard(  # noqa: SLF001
            "/api/does-not-exist-r12"
        )


# ──────────────────────────────────────────────────────────────────────
# Middleware behavior on canonical paths (parameterized matcher)
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def app_with_middleware(monkeypatch):
    """Minimal FastAPI app wired with the production ``APIKeyMiddleware``
    and stub handlers that mirror the canonical phone-callable routes
    we want to pin. The middleware decides 200/401; the handlers just
    return ``{"ok": True}`` so behavior depends purely on auth."""
    monkeypatch.setenv("FERAL_API_KEY", _DASHBOARD_KEY)
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "0")

    # audit-r14 / Lane 06 — restore the production ``is_localhost``
    # so the conftest testclient-as-loopback shim doesn't short-circuit
    # the off-loopback auth path these tests exercise. With the
    # audit-r12 A1 middleware change, loopback bypass is unconditional
    # and the shim would otherwise turn every assertion below into a 200.
    from security import session_auth as _sa
    from api import server as _server_for_patch
    def real_is_localhost(host):
        return host in ("127.0.0.1", "::1", "localhost")
    monkeypatch.setattr(_sa, "is_localhost", real_is_localhost)
    monkeypatch.setattr(_server_for_patch, "is_localhost", real_is_localhost, raising=False)

    def fake_verify(bearer: str):
        if bearer == _PHONE_BEARER:
            return "device-id-stub"
        return None

    mock_state = MagicMock()
    mock_state.device_pairing_store = MagicMock()
    mock_state.device_pairing_store.verify_phone_bearer = fake_verify

    from api import server as server_module
    monkeypatch.setattr(server_module, "FERAL_API_KEY", _DASHBOARD_KEY)
    monkeypatch.setattr("api.state.state", mock_state)

    app = FastAPI()
    app.add_middleware(server_module.APIKeyMiddleware)

    # Stubs mirror the EXACT shape of the canonical routes (including
    # `{request_id}` path params) so the parameterized matcher in the
    # allowlist is what's actually exercised end-to-end.
    @app.post("/api/approvals/{request_id}/approve")
    async def approve(request_id: str):
        return {"ok": True, "id": request_id}

    @app.post("/api/approvals/{request_id}/reject")
    async def reject(request_id: str):
        return {"ok": True, "id": request_id}

    @app.get("/api/ambient/briefing")
    async def briefing():
        return {"ok": True}

    return app


def _client(app):
    return TestClient(app, raise_server_exceptions=False)


def test_phone_bearer_can_approve_request(app_with_middleware):
    """POST /api/approvals/{id}/approve with phone bearer → 200.

    Before the fix this 401'd because the allowlist literal was
    ``/api/approvals/approve`` (the path-param form was never matched)."""
    r = _client(app_with_middleware).post(
        "/api/approvals/req-abc-123/approve",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "id": "req-abc-123"}


def test_phone_bearer_can_reject_request(app_with_middleware):
    """POST /api/approvals/{id}/reject with phone bearer → 200.

    Audit listed the legacy verb as ``deny``; canonical is ``reject``."""
    r = _client(app_with_middleware).post(
        "/api/approvals/req-xyz-789/reject",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 200, r.text


def test_phone_bearer_can_read_ambient_briefing(app_with_middleware):
    """GET /api/ambient/briefing with phone bearer → 200.

    Before the fix the allowlist named ``/api/ambient/digest`` (a path
    that has never existed); the iOS ambient summary panel 401'd."""
    r = _client(app_with_middleware).get(
        "/api/ambient/briefing",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 200, r.text


def test_unauthenticated_approve_returns_401(app_with_middleware):
    """No auth → 401, regardless of the canonical path being on the
    allowlist (defense-in-depth)."""
    r = _client(app_with_middleware).post("/api/approvals/req-abc-123/approve")
    assert r.status_code == 401


def test_bogus_bearer_on_approve_returns_401(app_with_middleware):
    """Bearer that doesn't verify as a phone bearer → 401."""
    r = _client(app_with_middleware).post(
        "/api/approvals/req-abc-123/approve",
        headers={"Authorization": "Bearer definitely-not-paired"},
    )
    assert r.status_code == 401


def test_legacy_paths_no_longer_honoured(app_with_middleware):
    """The pre-audit literals (``/api/approvals/approve``,
    ``/api/ambient/digest``) must NOT match the canonical allowlist
    after the fix — otherwise the cleanup was cosmetic. (These routes
    don't exist on the stub app either, so we expect 404 or 401, not
    200. The middleware path is what we pin: it rejects auth before
    reaching the route lookup.)"""
    c = _client(app_with_middleware)

    r = c.post(
        "/api/approvals/approve",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 401, (
        "Stale literal /api/approvals/approve must not match the new "
        f"parameterized allowlist; got {r.status_code}: {r.text}"
    )

    r = c.get(
        "/api/ambient/digest",
        headers={"Authorization": f"Bearer {_PHONE_BEARER}"},
    )
    assert r.status_code == 401, (
        "Stale literal /api/ambient/digest must not match the new "
        f"allowlist; got {r.status_code}: {r.text}"
    )


def test_dashboard_api_key_still_works_on_canonical_paths(app_with_middleware):
    """Phone bearer is a SUBSET of the dashboard key — anything a phone
    can do, the dashboard key can do. This pins that we didn't break
    the dashboard auth path while reworking the allowlist."""
    c = _client(app_with_middleware)
    h = {"Authorization": f"Bearer {_DASHBOARD_KEY}"}

    assert c.post("/api/approvals/req-1/approve", headers=h).status_code == 200
    assert c.post("/api/approvals/req-1/reject", headers=h).status_code == 200
    assert c.get("/api/ambient/briefing", headers=h).status_code == 200
