"""Google OAuth lifecycle regressions — refresh tokens, PKCE exchange,
and the Settings "use your own OAuth app" path.

Three independent breakages on the Google path, each sufficient on its
own to make Gmail/Calendar stop working:

D1  ``build_authorize_response`` never asked for ``access_type=offline``,
    so Google issued an access token and no ``refresh_token``. Every
    Google connection died ~55 minutes after consent and could not
    self-heal (``_refresh_token`` logs "No refresh token" and returns
    False). The fix is a per-provider ``extra_auth_params`` descriptor
    field, not a hardcoded Google branch in the shared URL builder.

D3  The PKCE branch of ``handle_callback`` sent ``code_verifier`` +
    ``client_id`` but dropped ``client_secret``. Google's installed-app
    and Web client types both require it, so the exchange 401'd. The
    refresh path already sent it — the two paths disagreed.

D2  The Settings OAuth card POSTs ``client_id`` + ``client_secret`` to
    ``/api/integrations/token``, which read only ``token`` and wrote the
    *client secret* into the ``access_token`` slot with a 30-year
    expiry. That made ``is_connected("google")`` permanently True, 401'd
    every API call, and — because ``email._use_imap`` and
    ``calendar._use_ics`` key off ``is_connected`` — disabled the
    working IMAP/ICS fallbacks.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from integrations.oauth_manager import BUILTIN_PROVIDERS, OAuthManager


class FakeVault:
    """In-memory stand-in for the BlindVault surface OAuthManager uses."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def store(self, key: str, value: str, stored_by: str = "user") -> None:
        self.data[key] = value

    def retrieve(self, key: str, requester: str = "executor") -> str | None:
        return self.data.get(key)

    def remove(self, key: str, removed_by: str = "user") -> bool:
        return self.data.pop(key, None) is not None

    def list_keys(self) -> list[str]:
        return list(self.data)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


@pytest.fixture(autouse=True)
def isolated_oauth_state(tmp_path, monkeypatch):
    """``OAUTH_STATE_PATH`` and friends are module-level constants bound
    at import time, so the conftest's per-test ``FERAL_HOME`` does not
    stop saved tokens bleeding from one test into the next."""
    from integrations import oauth_manager as om

    monkeypatch.setattr(om, "OAUTH_STATE_PATH", tmp_path / "oauth_state.json")
    monkeypatch.setattr(om, "OAUTH_PENDING_PATH",
                        tmp_path / "oauth_pending.json")
    monkeypatch.setattr(om, "OAUTH_CONFIG_PATH",
                        tmp_path / "oauth_providers.json")


@pytest.fixture
def manager(monkeypatch):
    """OAuthManager with a Google client configured and probes off."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-client-secret")
    return OAuthManager(probe_on_add=False)


# ── D1: access_type=offline / prompt=consent ─────────────────────────


def test_google_authorize_url_requests_a_refresh_token(manager):
    resp = manager.build_authorize_response("google")
    assert resp["success"] is True
    params = parse_qs(urlparse(resp["url"]).query)
    # Without access_type=offline Google returns no refresh_token at all.
    assert params["access_type"] == ["offline"]
    # Without prompt=consent Google withholds the refresh_token on every
    # re-authorization after the first, so a reconnect can't self-heal.
    assert params["prompt"] == ["consent"]


def test_extra_auth_params_are_a_provider_descriptor_field():
    """The offline-access request lives on the Google descriptor, not in
    a hardcoded branch of the shared URL builder."""
    assert BUILTIN_PROVIDERS["google"]["extra_auth_params"] == {
        "access_type": "offline",
        "prompt": "consent",
    }


def test_providers_without_extra_params_are_unchanged(manager):
    """Spotify must not inherit Google's Google-specific parameters."""
    manager._providers["spotify"].client_id = "spotify-client-id"
    resp = manager.build_authorize_response("spotify")
    params = parse_qs(urlparse(resp["url"]).query)
    assert "access_type" not in params
    assert "prompt" not in params
    assert params["code_challenge_method"] == ["S256"]


# ── D3: PKCE exchange sends the client secret ────────────────────────


async def test_pkce_token_exchange_sends_client_secret(manager, monkeypatch):
    captured: dict = {}

    async def fake_post(url, data=None, headers=None):
        captured.update(data or {})
        return FakeResponse({
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3599,
        })

    monkeypatch.setattr(manager._http, "post", fake_post)

    authorize = manager.build_authorize_response("google")
    result = await manager.handle_callback(authorize["state"], "auth-code")

    assert result["success"] is True
    assert captured["client_id"] == "google-client-id"
    # Google's installed-app / Web client types reject a PKCE exchange
    # that omits the secret. The refresh path already sent it.
    assert captured["client_secret"] == "google-client-secret"
    assert captured["code_verifier"]


async def test_pkce_exchange_omits_secret_when_none_configured(monkeypatch):
    """A true public client (no secret) must not send an empty one."""
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "spotify-client-id")
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    mgr = OAuthManager(probe_on_add=False)
    captured: dict = {}

    async def fake_post(url, data=None, headers=None):
        captured.update(data or {})
        return FakeResponse({"access_token": "at", "expires_in": 3600})

    monkeypatch.setattr(mgr._http, "post", fake_post)
    authorize = mgr.build_authorize_response("spotify")
    await mgr.handle_callback(authorize["state"], "auth-code")

    assert captured["client_id"] == "spotify-client-id"
    assert "client_secret" not in captured


# ── D2: client credentials are not access tokens ─────────────────────


def test_store_api_token_refuses_oauth2_providers(manager):
    with pytest.raises(ValueError, match="oauth2"):
        manager.store_api_token("google", "google-client-secret")
    # The corrupt entry must not exist: a client secret parked in the
    # access_token slot makes is_connected lie forever and disables the
    # IMAP/ICS fallbacks that key off it.
    assert manager.is_connected("google") is False


def test_store_api_token_still_works_for_token_providers(manager):
    manager.store_api_token("home_assistant", "llat-token")
    assert manager.is_connected("home_assistant") is True


def test_store_client_credentials_takes_effect_without_restart(monkeypatch):
    vault = FakeVault()
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    mgr = OAuthManager(vault=vault, probe_on_add=False)
    assert mgr.setup_status("google") == "provider_setup_required"

    mgr.store_client_credentials("google", "cid-from-ui", "csec-from-ui")

    # Live provider object updated (no restart) …
    assert mgr.get_provider("google").client_id == "cid-from-ui"
    assert mgr.get_provider("google").client_secret == "csec-from-ui"
    assert mgr.setup_status("google") == "ready"
    # … and no token was fabricated, so the fallbacks stay enabled.
    assert mgr.is_connected("google") is False
    # … and persisted where a fresh OAuthManager will find them.
    assert OAuthManager(vault=vault, probe_on_add=False).get_provider(
        "google"
    ).client_id == "cid-from-ui"


def test_store_client_credentials_persists_without_a_vault(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    mgr = OAuthManager(probe_on_add=False)
    mgr.store_client_credentials("google", "file-cid", "file-csec")

    reloaded = OAuthManager(probe_on_add=False)
    assert reloaded.get_provider("google").client_id == "file-cid"
    assert reloaded.get_provider("google").client_secret == "file-csec"


def test_store_client_credentials_rejects_non_oauth_providers(manager):
    with pytest.raises(ValueError):
        manager.store_client_credentials("home_assistant", "cid", "csec")
    with pytest.raises(ValueError):
        manager.store_client_credentials("nope", "cid", "csec")
    with pytest.raises(ValueError):
        manager.store_client_credentials("google", "", "csec")


# ── D2: the HTTP surface the Settings card actually calls ────────────


def _client(oauth):
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from api.server import app

    patcher = patch("api.routes.integrations_webhooks.state")
    st = patcher.start()
    st.oauth = oauth
    st.email = None
    return TestClient(app), patcher


def test_token_route_rejects_oauth2_client_secrets(manager):
    client, patcher = _client(manager)
    try:
        r = client.post(
            "/api/integrations/token",
            json={"provider_id": "google", "token": "google-client-secret"},
        )
    finally:
        patcher.stop()
    assert r.status_code == 200
    assert r.json()["reason"] == "oauth2_provider"
    assert manager.is_connected("google") is False


def test_oauth_client_route_stores_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    mgr = OAuthManager(vault=FakeVault(), probe_on_add=False)
    client, patcher = _client(mgr)
    try:
        r = client.post(
            "/api/integrations/oauth/client",
            json={
                "provider_id": "google",
                "client_id": "cid-from-ui",
                "client_secret": "csec-from-ui",
            },
        )
    finally:
        patcher.stop()
    body = r.json()
    assert body["ok"] is True
    assert body["setup_status"] == "ready"
    assert mgr.get_provider("google").client_id == "cid-from-ui"
    assert mgr.is_connected("google") is False
