"""
Lane 10 — first-party OAuth + persistent pending state + probe-on-add.

These tests pin the contract added by the integrations+webhooks lane:

* ``BUILTIN_PROVIDERS`` carries setup_doc_url + setup_doc_summary for
  every OAuth-capable provider, so a fresh user lands on a clear
  registration walkthrough rather than an opaque "missing client_id"
  error toast.
* ``setup_status`` is ``provider_setup_required`` when no first-party
  client is baked in AND the operator hasn't supplied one. It flips to
  ``ready`` the moment a baked-in or operator-supplied client_id
  appears (env, vault, or first_party_clients.json overlay).
* ``build_authorize_response`` returns a structured dict — not just an
  optional URL — so the API surface can serve a 200 OK with
  ``setup_status`` instead of a misleading error.
* Pending OAuth states (PKCE code_verifier in particular) survive a
  brain restart by persisting to vault / disk.
* Probe-on-add wires ``security.probe`` so a successful token exchange
  immediately runs a live API round-trip and surfaces the result.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_feral_home(tmp_path, monkeypatch):
    """Pin FERAL_HOME so user-side overrides don't leak into tests."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    yield tmp_path


# ──────────────────────────────────────────────────────────────────────
# Setup-status surface
# ──────────────────────────────────────────────────────────────────────


def test_every_oauth_provider_advertises_setup_doc(isolated_feral_home):
    from integrations.oauth_manager import BUILTIN_PROVIDERS

    for pid, pdata in BUILTIN_PROVIDERS.items():
        assert pdata.get("setup_doc_url"), (
            f"{pid} missing setup_doc_url — operator can't self-serve "
            "credentials without a doc link"
        )
        assert pdata.get("setup_doc_summary"), (
            f"{pid} missing setup_doc_summary"
        )


def test_setup_status_is_required_when_no_credentials(isolated_feral_home, monkeypatch):
    """A fresh install with no env vars and no baked credentials must
    surface ``provider_setup_required`` for every OAuth provider so the
    WebUI can render the doc panel instead of a broken Connect button.
    """
    for env_key in (
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
        "MICROSOFT_OAUTH_CLIENT_ID", "MICROSOFT_OAUTH_CLIENT_SECRET",
        "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
        "NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET",
        "WHOOP_OAUTH_CLIENT_ID", "WHOOP_OAUTH_CLIENT_SECRET",
        "OURA_OAUTH_CLIENT_ID", "OURA_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(env_key, raising=False)
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    for pid in ("google", "spotify", "notion", "microsoft", "whoop", "oura"):
        assert mgr.setup_status(pid) == "provider_setup_required", pid
    assert mgr.setup_status("home_assistant") == "ready"


def test_setup_status_flips_to_ready_with_env_credentials(
    isolated_feral_home, monkeypatch,
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "g-sec")
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    assert mgr.setup_status("google") == "ready"


def test_baked_first_party_clients_overlay(isolated_feral_home, monkeypatch):
    """A release artifact dropping client_id values into the operator's
    first_party_clients.json overlay flips the provider to ready
    without any env vars."""
    overlay = isolated_feral_home / "first_party_clients.json"
    overlay.write_text(json.dumps({
        "google": {"client_id": "baked-google", "client_secret": "baked-sec"},
    }))
    for env_key in (
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(env_key, raising=False)
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    assert mgr.setup_status("google") == "ready"
    assert mgr._providers["google"].client_id == "baked-google"


# ──────────────────────────────────────────────────────────────────────
# Authorize-response shape
# ──────────────────────────────────────────────────────────────────────


def test_build_authorize_response_setup_required_structured(
    isolated_feral_home, monkeypatch,
):
    for env_key in (
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(env_key, raising=False)
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    resp = mgr.build_authorize_response("google")
    assert resp["success"] is False
    assert resp["reason"] == "provider_setup_required"
    assert resp["setup_status"] == "provider_setup_required"
    assert resp["setup_doc_url"]
    assert "Google" in resp["setup_doc_summary"] or "google" in resp["setup_doc_summary"].lower()


def test_build_authorize_response_unknown_provider(isolated_feral_home):
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    resp = mgr.build_authorize_response("not-a-real-provider")
    assert resp["success"] is False
    assert resp["reason"] == "unknown_provider"


def test_build_authorize_response_ready_returns_url(
    isolated_feral_home, monkeypatch,
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec-x")
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    resp = mgr.build_authorize_response("google")
    assert resp["success"] is True
    assert resp["url"].startswith("https://accounts.google.com/")
    assert "client_id=id-x" in resp["url"]
    assert "code_challenge_method=S256" in resp["url"]
    assert resp["state"]


# ──────────────────────────────────────────────────────────────────────
# Pending-state persistence (survives restart)
# ──────────────────────────────────────────────────────────────────────


def test_pending_states_persist_to_disk(isolated_feral_home, monkeypatch):
    """When no vault is provided, pending states must persist to a
    chmod-0600 JSON file under FERAL_HOME so a brain restart doesn't
    invalidate every in-flight OAuth flow."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-1")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec-1")
    from integrations.oauth_manager import OAuthManager, OAUTH_PENDING_PATH

    mgr = OAuthManager()
    resp = mgr.build_authorize_response("google")
    state = resp["state"]
    assert OAUTH_PENDING_PATH.exists()
    blob = json.loads(OAUTH_PENDING_PATH.read_text())
    assert state in blob
    assert blob[state]["provider_id"] == "google"
    assert blob[state]["code_verifier"]


def test_pending_states_restored_on_construction(
    isolated_feral_home, monkeypatch,
):
    """A second OAuthManager seeing the persisted state file picks up
    the in-flight authorization from the previous process."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-2")
    from integrations.oauth_manager import OAuthManager

    mgr1 = OAuthManager()
    resp = mgr1.build_authorize_response("google")
    state = resp["state"]
    verifier = mgr1._pending_states[state]["code_verifier"]

    mgr2 = OAuthManager()
    assert state in mgr2._pending_states
    assert mgr2._pending_states[state]["code_verifier"] == verifier


def test_pending_states_drop_on_ttl_expiry(isolated_feral_home, monkeypatch):
    """States older than PENDING_STATE_TTL_SECONDS are not restored —
    a stale flow can't hang around forever."""
    from integrations.oauth_manager import OAUTH_PENDING_PATH, OAuthManager

    OAUTH_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    OAUTH_PENDING_PATH.write_text(json.dumps({
        "stale_state": {
            "provider_id": "google",
            "code_verifier": "x",
            "created": 0,
        }
    }))
    mgr = OAuthManager()
    assert "stale_state" not in mgr._pending_states


def test_pending_states_use_vault_when_available(
    isolated_feral_home, monkeypatch,
):
    """When a vault is provided, the OAuth manager stores pending state
    encrypted-at-rest under the ``oauth_pending_*`` namespace rather
    than writing to disk in the clear."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-vault")
    from integrations.oauth_manager import OAuthManager, PENDING_VAULT_PREFIX

    class FakeVault:
        def __init__(self):
            self.store_calls: list[tuple[str, str]] = []
            self._data: dict[str, str] = {}

        def store(self, key, value, requester="oauth_manager"):
            self.store_calls.append((key, value))
            self._data[key] = value

        def retrieve(self, key, requester="oauth_manager"):
            return self._data.get(key)

        def remove(self, key, removed_by="oauth_manager"):
            self._data.pop(key, None)
            return True

        def list_keys(self):
            return list(self._data.keys())

    vault = FakeVault()
    mgr = OAuthManager(vault=vault)
    resp = mgr.build_authorize_response("google")
    state = resp["state"]
    keys_with_pending = [
        k for k, _ in vault.store_calls if k.startswith(PENDING_VAULT_PREFIX)
    ]
    assert any(k.endswith(state) for k in keys_with_pending)


# ──────────────────────────────────────────────────────────────────────
# Probe-on-add (live verification after token exchange)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_on_add_runs_after_successful_callback(
    isolated_feral_home, monkeypatch,
):
    """After ``handle_callback`` saves the token, the manager runs the
    registered ``security.probe`` for that provider with ``force=True``
    and surfaces the result so the caller can render a live status."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-id-probe")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "g-sec-probe")
    from integrations.oauth_manager import OAuthManager
    import security.probe as probe_mod

    mgr = OAuthManager(probe_on_add=True)
    resp = mgr.build_authorize_response("google")
    state = resp["state"]

    async def fake_post(self, url, *args, **kwargs):
        class Resp:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "access_token": "fresh-access",
                    "refresh_token": "fresh-refresh",
                    "expires_in": 3600,
                }
        return Resp()

    probe_calls: list[tuple[str, bool]] = []

    async def fake_probe(provider_id, *, vault=None, force=False):
        probe_calls.append((provider_id, force))
        return probe_mod.ProbeResult(
            provider=provider_id,
            ok=True,
            status_code=200,
            reason="ok",
            detail="probe ok",
            probed_at=time.time(),
            latency_ms=42.0,
        )

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    monkeypatch.setattr("security.probe.probe", fake_probe)

    result = await mgr.handle_callback(state, "auth-code")
    assert result["success"] is True
    assert result["provider"] == "google"
    assert result["probe"]["ok"] is True
    assert result["probe"]["latency_ms"] == 42.0
    assert probe_calls == [("google", True)]
    # Token was actually stored.
    assert mgr.is_connected("google")


@pytest.mark.asyncio
async def test_probe_on_add_disabled_omits_probe_field(
    isolated_feral_home, monkeypatch,
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-id-no-probe")
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager(probe_on_add=False)
    resp = mgr.build_authorize_response("google")
    state = resp["state"]

    async def fake_post(self, url, *args, **kwargs):
        class Resp:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def json(self):
                return {"access_token": "tok"}
        return Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    result = await mgr.handle_callback(state, "auth-code")
    assert result["success"] is True
    assert "probe" not in result


@pytest.mark.asyncio
async def test_callback_unknown_state_rejected(isolated_feral_home, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-id")
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    result = await mgr.handle_callback("never-seen-state", "auth-code")
    assert result.get("error")
    assert result.get("reason") == "unknown_state"


# ──────────────────────────────────────────────────────────────────────
# Refresh-token revocation behaviour
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_token_400_clears_stored_credentials(
    isolated_feral_home, monkeypatch,
):
    """When the provider rejects a refresh request with 400/401 the
    stored token is dropped so ``is_connected`` reports honestly."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-id")
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    mgr._tokens["google"] = {
        "access_token": "stale",
        "refresh_token": "rev",
        "expires_in": 3600,
        "obtained_at": time.time() - 4000,
    }
    assert mgr.is_connected("google")

    async def rejecting_post(self, url, *args, **kwargs):
        class Resp:
            status_code = 400
            text = "invalid_grant"
        return Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", rejecting_post)
    ok = await mgr._refresh_token("google")
    assert ok is False
    assert not mgr.is_connected("google")


# ──────────────────────────────────────────────────────────────────────
# list_providers honesty
# ──────────────────────────────────────────────────────────────────────


def test_list_providers_includes_setup_status_for_each(
    isolated_feral_home, monkeypatch,
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    for env_key in (
        "MICROSOFT_OAUTH_CLIENT_ID", "MICROSOFT_OAUTH_CLIENT_SECRET",
        "SPOTIFY_CLIENT_ID", "NOTION_CLIENT_ID",
        "WHOOP_OAUTH_CLIENT_ID", "OURA_OAUTH_CLIENT_ID",
    ):
        monkeypatch.delenv(env_key, raising=False)
    from integrations.oauth_manager import OAuthManager

    mgr = OAuthManager()
    listing = {p["id"]: p for p in mgr.list_providers()}
    assert listing["google"]["setup_status"] == "ready"
    assert listing["microsoft"]["setup_status"] == "provider_setup_required"
    assert listing["microsoft"]["setup_doc_url"]
    assert listing["home_assistant"]["setup_status"] == "ready"
    assert "scopes" in listing["google"]
