"""
FERAL OAuth Manager — Secure Token Lifecycle
===============================================
Manages OAuth2 flows (Authorization Code + PKCE) and long-lived
API tokens.  All tokens are stored in the BlindVault — encrypted,
audited, and never exposed to the LLM.

Supports:
  - OAuth2 Authorization Code with PKCE (Spotify, Notion, Google,
    Microsoft, Whoop, Oura)
  - Long-lived API tokens (Home Assistant)
  - Automatic token refresh via refresh_token
  - First-party client_id baking when credentials are provided at
    build/install time; degraded ``provider_setup_required`` mode
    with operator-facing setup docs when they are not.
  - Pending OAuth states persisted to the BlindVault so an in-flight
    authorization survives a brain restart without losing the PKCE
    verifier (which would otherwise force the user to re-authorize).
  - Optional probe-on-add: after a successful token exchange we run
    the live ``probe(provider)`` registered in
    ``security.probe`` so the integration's ``connected`` property
    reflects an actual API round-trip, not just token presence.
"""

from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from config.loader import feral_home
from config.runtime import brain_public_base_url

logger = logging.getLogger("feral.oauth")

OAUTH_CONFIG_PATH = feral_home() / "oauth_providers.json"
OAUTH_STATE_PATH = feral_home() / "oauth_state.json"
OAUTH_PENDING_PATH = feral_home() / "oauth_pending.json"

PENDING_STATE_TTL_SECONDS = 30 * 60
PENDING_VAULT_PREFIX = "oauth_pending_"


def _default_redirect_uri() -> str:
    return f"{brain_public_base_url()}/api/oauth/callback"


class OAuthProvider:
    """Configuration for a single OAuth2 provider."""

    def __init__(self, data: dict):
        self.id: str = data.get("id", "")
        self.name: str = data.get("name", "")
        self.auth_url: str = data.get("auth_url", "")
        self.token_url: str = data.get("token_url", "")
        self.client_id: str = data.get("client_id", "")
        self.client_secret: str = data.get("client_secret", "")
        self.scopes: list[str] = data.get("scopes", [])
        self.redirect_uri: str = data.get("redirect_uri", _default_redirect_uri())
        self.pkce: bool = data.get("pkce", True)
        self.auth_type: str = data.get("auth_type", "oauth2")
        self.setup_doc_url: str = data.get("setup_doc_url", "")
        self.setup_doc_summary: str = data.get("setup_doc_summary", "")


# Operator-facing docs anchored on the user's own brain
# (``http(s)://<host>/docs/integrations/oauth/<provider>``). The
# WebUI surfaces this URL when ``setup_status == provider_setup_required``
# so the user can register their own OAuth app without leaving the
# Settings → Integrations page. The docs themselves live under
# ``docs/mintlify/integrations/oauth/`` so they version with the
# brain.
def _setup_doc_url(provider_id: str) -> str:
    return f"/docs/integrations/oauth/{provider_id}"


BUILTIN_PROVIDERS = {
    "spotify": {
        "id": "spotify",
        "name": "Spotify",
        "auth_url": "https://accounts.spotify.com/authorize",
        "token_url": "https://accounts.spotify.com/api/token",
        "client_id": "",
        "scopes": [
            "user-read-playback-state",
            "user-modify-playback-state",
            "user-read-currently-playing",
            "playlist-read-private",
            "user-library-read",
        ],
        "pkce": True,
        "auth_type": "oauth2",
        "setup_doc_url": _setup_doc_url("spotify"),
        "setup_doc_summary": (
            "Register a Spotify app at developer.spotify.com → set redirect "
            "URI to your brain's /api/oauth/callback → copy Client ID + "
            "Client Secret into Settings → Integrations → Spotify."
        ),
    },
    "notion": {
        "id": "notion",
        "name": "Notion",
        "auth_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "client_id": "",
        "scopes": [],
        "pkce": False,
        "auth_type": "oauth2",
        "setup_doc_url": _setup_doc_url("notion"),
        "setup_doc_summary": (
            "Create a Notion integration at notion.so/my-integrations → "
            "select OAuth → set redirect URI → copy Client ID + Client "
            "Secret into Settings → Integrations → Notion."
        ),
    },
    "home_assistant": {
        "id": "home_assistant",
        "name": "Home Assistant",
        "auth_type": "token",
        "setup_doc_url": _setup_doc_url("home_assistant"),
        "setup_doc_summary": (
            "In Home Assistant: Profile → Long-Lived Access Tokens → "
            "Create. Paste the token into Settings → Integrations → "
            "Home Assistant alongside your HA URL."
        ),
    },
    # ──────────────────────────────────────────────────────────────────
    # PR 11: Google + Microsoft built-ins.
    #
    # ``integrations/google_drive.py``, ``google_contacts.py``,
    # ``email.py`` (Gmail), ``calendar.py``, and ``microsoft365.py``
    # all call ``OAuthManager.get_token("google")`` or
    # ``get_token("microsoft")``. Until now those provider ids were
    # absent from ``BUILTIN_PROVIDERS``, so unless the operator
    # hand-rolled ``~/.feral/oauth_providers.json`` the integrations
    # silently had no token. The truthfulness mission says: fail
    # honestly with a real OAuth URL instead of returning a token that
    # was never going to exist.
    #
    # Scopes are the *intersection* of what the integrations actually
    # call (People API contact reads, Drive metadata + files, Gmail
    # read+modify, Calendar read+write, Graph user/files/mail). PKCE
    # is enabled where the provider supports it for installed-app
    # security.
    "google": {
        "id": "google",
        "name": "Google",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id": "",
        "setup_doc_url": _setup_doc_url("google"),
        "setup_doc_summary": (
            "Create a Google Cloud project → enable Calendar / Gmail / "
            "Drive / People APIs → OAuth consent screen → create OAuth "
            "Client ID for a Desktop app with redirect URI "
            "http://localhost:9090/api/oauth/callback → copy Client ID + "
            "Client Secret into Settings → Integrations → Google."
        ),
        "scopes": [
            # Identity for the connected account.
            "openid",
            "email",
            "profile",
            # Gmail: read + send + modify labels (matches
            # integrations/email.py call sites).
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
            # Drive: read + write metadata + file content for
            # integrations/google_drive.py.
            "https://www.googleapis.com/auth/drive",
            # Contacts (People API): read directory + connections.
            "https://www.googleapis.com/auth/contacts.readonly",
            # Calendar: read + write events.
            "https://www.googleapis.com/auth/calendar",
        ],
        "pkce": True,
        "auth_type": "oauth2",
    },
    "microsoft": {
        "id": "microsoft",
        "name": "Microsoft",
        # The "common" tenant accepts both personal and work accounts;
        # operators with single-tenant apps can override via
        # ~/.feral/oauth_providers.json.
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "client_id": "",
        "setup_doc_url": _setup_doc_url("microsoft"),
        "setup_doc_summary": (
            "Register an app at portal.azure.com → App registrations → "
            "New registration → set redirect URI to your brain's "
            "/api/oauth/callback → API permissions → grant "
            "Mail.Read/Mail.Send/Calendars.ReadWrite/Files.ReadWrite → "
            "copy Application (client) ID + a Client Secret into "
            "Settings → Integrations → Microsoft."
        ),
        "scopes": [
            # Graph identity / refresh.
            "openid",
            "email",
            "profile",
            "offline_access",
            # Calendar/Mail/Files surfaces used by microsoft365.py.
            "User.Read",
            "Mail.Read",
            "Mail.Send",
            "Calendars.ReadWrite",
            "Files.ReadWrite",
            "Contacts.Read",
        ],
        "pkce": True,
        "auth_type": "oauth2",
    },
    # ──────────────────────────────────────────────────────────────────
    # audit-r12 D9: Whoop + Oura built-ins.
    #
    # ``integrations/health_platforms.py`` calls
    # ``OAuthManager.get_token("whoop")`` /
    # ``get_token("oura")`` but neither id was registered, so the
    # call returned ``None`` and ``WhoopClient.connected`` /
    # ``OuraClient.connected`` were silently False forever. Same
    # silent-degrade pattern as the Google/Microsoft gap PR 11
    # closed; this entry registers the two so the health platforms
    # actually authenticate.
    #
    # Endpoints + scopes verified against the live vendor docs on
    # 2026-05-19 — do not regress these without re-verifying:
    #
    # Whoop:
    #   https://developer.whoop.com/docs/developing/oauth
    #   - Auth URL: https://api.prod.whoop.com/oauth/oauth2/auth
    #   - Token URL: https://api.prod.whoop.com/oauth/oauth2/token
    #   - Access tokens ~1h; refresh tokens require ``offline`` scope.
    #
    # Oura (Cloud API v2):
    #   https://cloud.ouraring.com/v2/docs
    #   - Auth URL: https://cloud.ouraring.com/oauth/authorize
    #   - Token URL: https://api.ouraring.com/oauth/token
    #   - Access tokens long-lived; refresh tokens issued under the
    #     ``offline_access`` scope.
    "whoop": {
        "id": "whoop",
        "name": "Whoop",
        "auth_url": "https://api.prod.whoop.com/oauth/oauth2/auth",
        "token_url": "https://api.prod.whoop.com/oauth/oauth2/token",
        "client_id": "",
        "setup_doc_url": _setup_doc_url("whoop"),
        "setup_doc_summary": (
            "Register an app at developer.whoop.com → set redirect URI "
            "to your brain's /api/oauth/callback → copy Client ID + "
            "Client Secret into Settings → Integrations → Whoop."
        ),
        "scopes": [
            # MUST include ``offline`` to receive a refresh_token per
            # the Whoop OAuth docs (Receiving a Refresh Token).
            "offline",
            # Match the surfaces ``WhoopClient`` actually queries:
            # recovery, sleep, cycles, workouts, body_measurement, and
            # profile (for the connected member's id).
            "read:recovery",
            "read:sleep",
            "read:cycles",
            "read:workout",
            "read:body_measurement",
            "read:profile",
        ],
        "pkce": True,
        "auth_type": "oauth2",
    },
    "oura": {
        "id": "oura",
        "name": "Oura Ring",
        "auth_url": "https://cloud.ouraring.com/oauth/authorize",
        "token_url": "https://api.ouraring.com/oauth/token",
        "client_id": "",
        "setup_doc_url": _setup_doc_url("oura"),
        "setup_doc_summary": (
            "Register an app at cloud.ouraring.com/oauth/applications → "
            "set redirect URI to your brain's /api/oauth/callback → "
            "copy Client ID + Client Secret into Settings → "
            "Integrations → Oura."
        ),
        "scopes": [
            "email",
            "personal",
            # Match the v2 endpoints ``OuraClient`` queries:
            # ``daily_sleep``, ``daily_readiness``, ``daily_activity``,
            # and the heartrate time series.
            "daily",
            "heartrate",
            "workout",
            "session",
            "tag",
        ],
        "pkce": True,
        "auth_type": "oauth2",
    },
}


# ─────────────────────────────────────────────────────────────────────
# First-party client credentials.
#
# When FERAL ships a release with credentials registered under the
# FERAL-AI brand (Google, Notion, Spotify), the release pipeline writes
# them into ``feral-core/integrations/_first_party_clients.json`` —
# committed as a *placeholder file* whose values are empty strings until
# the user (Mahmoud) hands over real credentials. The release artifact
# generator overlays the real values during ``feral build`` from a
# private credential bundle pulled out of the operator's vault.
#
# We resolve credentials in priority order:
#   1. ``~/.feral/oauth_providers.json`` (operator-edited)
#   2. ``GOOGLE_OAUTH_CLIENT_ID`` / etc. env vars
#   3. Vault credential keys (for operator-supplied credentials saved
#      via the Settings UI without leaving the brain process)
#   4. Baked-in first-party JSON (release artifact)
#   5. Empty → ``provider_setup_required``
#
# Native-app threat model: Google's OAuth docs explicitly note that
# desktop client secrets cannot be confidentially stored. PKCE is the
# real defense-in-depth; the secret is only weakly authenticating. We
# follow Google's documented installed-app pattern (see
# https://developers.google.com/identity/protocols/oauth2/native-app).
# ─────────────────────────────────────────────────────────────────────

def _first_party_overlay_path():
    """Operator-side overlay path. Resolved each call so tests that
    bounce ``FERAL_HOME`` between cases pick the right directory."""
    return feral_home() / "first_party_clients.json"


BAKED_CLIENTS_PATH = (
    # Walk one directory up from this module: feral-core/integrations →
    # feral-core. The release artifact drops the file at the integrations
    # package root so an installed wheel still finds it.
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "_first_party_clients.json")
)


def _load_first_party_clients() -> dict:
    """Merge first-party client registry from baked-in JSON and the
    operator's ``~/.feral`` overlay. Returns ``{provider_id: {client_id,
    client_secret}}``. Both keys may be empty strings if a placeholder
    is registered without real credentials yet — the loader treats
    empty values as "not provided" rather than as a configured client.
    """
    merged: dict = {}
    for src_path in (BAKED_CLIENTS_PATH, str(_first_party_overlay_path())):
        try:
            if not os.path.exists(src_path):
                continue
            with open(src_path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except Exception as exc:
            logger.warning("oauth: failed to load first-party clients %s: %s",
                           src_path, exc)
            continue
        for pid, entry in (blob or {}).items():
            if not isinstance(entry, dict):
                continue
            cid = (entry.get("client_id") or "").strip()
            csec = (entry.get("client_secret") or "").strip()
            if not cid:
                continue
            merged[pid] = {"client_id": cid, "client_secret": csec}
    return merged


# Mapping of provider id → the credential keys we look up in the vault
# when credentials were stored through the Settings UI (so the operator
# didn't have to set env vars). Keep these stable: docs reference them.
_VAULT_CREDENTIAL_KEYS = {
    "spotify": ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"),
    "notion": ("NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET"),
    "google": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
    "microsoft": (
        "MICROSOFT_OAUTH_CLIENT_ID",
        "MICROSOFT_OAUTH_CLIENT_SECRET",
    ),
    "whoop": ("WHOOP_OAUTH_CLIENT_ID", "WHOOP_OAUTH_CLIENT_SECRET"),
    "oura": ("OURA_OAUTH_CLIENT_ID", "OURA_OAUTH_CLIENT_SECRET"),
}


class OAuthManager:
    """
    Manages the full OAuth2 lifecycle and token storage.
    Tokens are persisted in the BlindVault (or a local JSON fallback).
    """

    def __init__(self, vault=None, *, probe_on_add: bool = True):
        self._vault = vault
        self._providers: dict[str, OAuthProvider] = {}
        self._pending_states: dict[str, dict] = {}
        self._tokens: dict[str, dict] = {}
        self._http = httpx.AsyncClient(timeout=15.0)
        self._probe_on_add = probe_on_add
        self._load_providers()
        self._load_tokens()
        self._load_pending_states()

    def _load_providers(self):
        """Load provider configs and merge credentials in priority order.

        Priority (lowest → highest, last wins):
          1. ``BUILTIN_PROVIDERS`` (auth/token URLs, scopes, doc URLs)
          2. Baked-in first-party clients (release artifact)
          3. Vault-stored credentials (Settings UI)
          4. Environment variables
          5. ``~/.feral/oauth_providers.json`` overlay (operator override)
        """
        for pid, pdata in BUILTIN_PROVIDERS.items():
            self._providers[pid] = OAuthProvider(pdata)

        # Layer 2: baked-in first-party credentials shipped with the
        # release artifact. These are typically empty strings during
        # development and the placeholder file ships in the repo so
        # release pipelines have a single, well-known location to
        # overlay.
        baked = _load_first_party_clients()
        for pid, creds in baked.items():
            if pid not in self._providers:
                continue
            self._providers[pid].client_id = creds.get("client_id", "")
            self._providers[pid].client_secret = creds.get("client_secret", "")
            logger.info("oauth: %s using baked first-party client_id", pid)

        # Layer 3: vault-stored credentials (set via Settings UI). Vault
        # access is best-effort; missing keys are normal during fresh
        # install before the operator has configured anything.
        if self._vault is not None:
            for pid, (id_key, secret_key) in _VAULT_CREDENTIAL_KEYS.items():
                if pid not in self._providers:
                    continue
                try:
                    cid = (self._vault.retrieve(id_key,
                                                requester="oauth_manager")
                           or "").strip()
                    csec = (self._vault.retrieve(secret_key,
                                                 requester="oauth_manager")
                            or "").strip()
                except Exception:
                    cid = ""
                    csec = ""
                if cid:
                    self._providers[pid].client_id = cid
                    self._providers[pid].client_secret = csec

        # Layer 4: environment variables.
        for pid, (id_key, secret_key) in _VAULT_CREDENTIAL_KEYS.items():
            if pid not in self._providers:
                continue
            env_id = os.getenv(id_key, "").strip()
            if env_id:
                self._providers[pid].client_id = env_id
                self._providers[pid].client_secret = os.getenv(
                    secret_key, ""
                ).strip()

        # Layer 5: operator overlay file. Highest priority because the
        # operator deliberately edited it.
        if OAUTH_CONFIG_PATH.exists():
            try:
                custom = json.loads(OAUTH_CONFIG_PATH.read_text())
                for pid, pdata in custom.items():
                    base = BUILTIN_PROVIDERS.get(pid, {}).copy()
                    if pid in self._providers:
                        base["client_id"] = self._providers[pid].client_id
                        base["client_secret"] = (
                            self._providers[pid].client_secret
                        )
                    base.update(pdata)
                    self._providers[pid] = OAuthProvider(base)
            except Exception as e:
                logger.warning(f"Failed to load custom OAuth providers: {e}")

    def _load_tokens(self):
        """Load saved tokens from BlindVault or fallback JSON."""
        if self._vault:
            for provider_id in self._providers:
                token_json = self._vault.retrieve(f"oauth_{provider_id}", requester="oauth_manager")
                if token_json:
                    try:
                        self._tokens[provider_id] = json.loads(token_json)
                    except json.JSONDecodeError:
                        pass
        elif OAUTH_STATE_PATH.exists():
            try:
                self._tokens = json.loads(OAUTH_STATE_PATH.read_text())
            except Exception:
                pass

    def _save_token(self, provider_id: str, token_data: dict):
        self._tokens[provider_id] = token_data
        if self._vault:
            self._vault.store(
                f"oauth_{provider_id}",
                json.dumps(token_data),
                stored_by="oauth_manager",
            )
        else:
            OAUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            OAUTH_STATE_PATH.write_text(json.dumps(self._tokens, indent=2))
            os.chmod(OAUTH_STATE_PATH, 0o600)

    # ─────────────────────────────────────────────────────────────────
    # Pending-state persistence — survive a brain restart mid-flow.
    #
    # The PKCE code_verifier is generated when the authorize URL is
    # built and is required at callback time to redeem the code. If we
    # only store it in process memory, restarting the brain between
    # steps (a real scenario when an operator runs ``feral setup``,
    # opens the browser, and the brain auto-restarts) loses the
    # verifier and the user has to start the flow over.
    #
    # The vault path is used when available (encrypted at rest); when
    # there's no vault we fall back to a JSON file under ~/.feral/ that
    # we chmod 0600 — same posture as oauth_state.json today.
    # ─────────────────────────────────────────────────────────────────

    def _persist_pending_state(self, state: str, pending: dict) -> None:
        if self._vault:
            try:
                self._vault.store(
                    f"{PENDING_VAULT_PREFIX}{state}",
                    json.dumps(pending),
                    stored_by="oauth_manager",
                )
                return
            except Exception as exc:
                logger.warning("oauth: vault store of pending state failed: %s",
                               exc)
        try:
            OAUTH_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
            blob = {}
            if OAUTH_PENDING_PATH.exists():
                try:
                    blob = json.loads(OAUTH_PENDING_PATH.read_text())
                except Exception:
                    blob = {}
            blob[state] = pending
            OAUTH_PENDING_PATH.write_text(json.dumps(blob, indent=2))
            os.chmod(OAUTH_PENDING_PATH, 0o600)
        except Exception as exc:
            logger.warning("oauth: pending-state file persist failed: %s", exc)

    def _drop_pending_state(self, state: str) -> None:
        if self._vault:
            try:
                self._vault.remove(
                    f"{PENDING_VAULT_PREFIX}{state}",
                    removed_by="oauth_manager",
                )
            except Exception:
                pass
        try:
            if OAUTH_PENDING_PATH.exists():
                blob = json.loads(OAUTH_PENDING_PATH.read_text())
                blob.pop(state, None)
                OAUTH_PENDING_PATH.write_text(json.dumps(blob, indent=2))
        except Exception:
            pass

    def _load_pending_states(self) -> None:
        """Restore pending OAuth states from disk/vault on boot. Drop
        any whose creation timestamp is older than
        :data:`PENDING_STATE_TTL_SECONDS` so a stale state can't hang
        around forever."""
        cutoff = time.time() - PENDING_STATE_TTL_SECONDS
        loaded: dict[str, dict] = {}
        if self._vault and hasattr(self._vault, "list_keys"):
            try:
                keys = self._vault.list_keys()
            except Exception:
                keys = []
            for key in keys:
                if not key.startswith(PENDING_VAULT_PREFIX):
                    continue
                state = key[len(PENDING_VAULT_PREFIX):]
                try:
                    raw = self._vault.retrieve(key, requester="oauth_manager")
                    if not raw:
                        continue
                    pending = json.loads(raw)
                except Exception:
                    continue
                if pending.get("created", 0) < cutoff:
                    self._drop_pending_state(state)
                    continue
                loaded[state] = pending
        elif OAUTH_PENDING_PATH.exists():
            try:
                blob = json.loads(OAUTH_PENDING_PATH.read_text())
            except Exception:
                blob = {}
            for state, pending in (blob or {}).items():
                if pending.get("created", 0) < cutoff:
                    continue
                loaded[state] = pending
        self._pending_states.update(loaded)
        if loaded:
            logger.info("oauth: restored %d pending OAuth state(s) from vault",
                        len(loaded))

    def get_provider(self, provider_id: str) -> Optional[OAuthProvider]:
        return self._providers.get(provider_id)

    def setup_status(self, provider_id: str) -> str:
        """Return ``ready`` when a client_id is configured (operator can
        start an authorization flow), or ``provider_setup_required``
        when the operator still needs to register their own OAuth app
        because the release didn't ship a first-party client.

        ``home_assistant`` is always ``ready`` because it uses a
        long-lived token rather than OAuth — the setup doc just tells
        the operator how to mint the token.
        """
        provider = self._providers.get(provider_id)
        if not provider:
            return "unknown_provider"
        if provider.auth_type == "token":
            return "ready"
        return "ready" if provider.client_id else "provider_setup_required"

    def list_providers(self) -> list[dict]:
        result = []
        for pid, p in self._providers.items():
            connected = pid in self._tokens and bool(
                self._tokens[pid].get("access_token")
            )
            result.append({
                "id": pid,
                "name": p.name,
                "auth_type": p.auth_type,
                "connected": connected,
                "has_client_id": bool(p.client_id),
                "setup_status": self.setup_status(pid),
                "setup_doc_url": p.setup_doc_url,
                "setup_doc_summary": p.setup_doc_summary,
                "scopes": list(p.scopes) if p.scopes else [],
            })
        return result

    def build_authorize_url(self, provider_id: str) -> Optional[str]:
        """Generate the OAuth2 authorization URL with PKCE.

        Returns ``None`` when the provider isn't OAuth-capable or when
        no client_id is configured. Callers that need the structured
        reason should use :meth:`build_authorize_response`.
        """
        resp = self.build_authorize_response(provider_id)
        return resp.get("url") if resp.get("success") else None

    def build_authorize_response(self, provider_id: str) -> dict:
        """Structured authorize-URL build. Return shape:

        Success: ``{success: True, provider, url, state, scopes}``
        Failure: ``{success: False, provider, error, reason,
                    setup_status, setup_doc_url, setup_doc_summary}``
        """
        provider = self._providers.get(provider_id)
        if not provider:
            return {
                "success": False,
                "provider": provider_id,
                "error": f"Unknown OAuth provider: {provider_id}",
                "reason": "unknown_provider",
            }
        if provider.auth_type != "oauth2":
            return {
                "success": False,
                "provider": provider_id,
                "error": (
                    f"Provider {provider_id} uses {provider.auth_type} "
                    "auth, not OAuth2 — store a token via "
                    "POST /api/integrations/token instead."
                ),
                "reason": "not_oauth2",
                "setup_status": self.setup_status(provider_id),
                "setup_doc_url": provider.setup_doc_url,
                "setup_doc_summary": provider.setup_doc_summary,
            }
        if not provider.client_id:
            logger.warning("oauth: %s missing client_id; surfacing "
                           "provider_setup_required", provider_id)
            return {
                "success": False,
                "provider": provider_id,
                "error": (
                    f"{provider.name} requires operator setup: register "
                    "an OAuth app and configure its Client ID + Secret. "
                    "See setup_doc_url for instructions."
                ),
                "reason": "provider_setup_required",
                "setup_status": "provider_setup_required",
                "setup_doc_url": provider.setup_doc_url,
                "setup_doc_summary": provider.setup_doc_summary,
            }

        state = secrets.token_urlsafe(32)

        params = {
            "client_id": provider.client_id,
            "response_type": "code",
            "redirect_uri": provider.redirect_uri,
            "state": state,
        }

        if provider.scopes:
            params["scope"] = " ".join(provider.scopes)

        pending = {"provider_id": provider_id, "created": time.time()}

        if provider.pkce:
            code_verifier = secrets.token_urlsafe(64)
            code_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).rstrip(b"=").decode()
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
            pending["code_verifier"] = code_verifier

        self._pending_states[state] = pending
        self._persist_pending_state(state, pending)

        url = f"{provider.auth_url}?{urlencode(params)}"
        logger.info(f"OAuth authorize URL generated for {provider_id}")
        return {
            "success": True,
            "provider": provider_id,
            "url": url,
            "state": state,
            "scopes": list(provider.scopes) if provider.scopes else [],
        }

    async def handle_callback(self, state: str, code: str) -> dict:
        """Exchange the authorization code for tokens.

        On success we run a probe-on-add round trip if a probe is
        registered for this provider, surfacing the live ``ok`` /
        ``reason`` to the caller so the UI can render a real "live"
        check immediately rather than waiting for the integration's
        own ``connected`` property to be polled.
        """
        pending = self._pending_states.pop(state, None)
        if pending is None:
            # Could be a state we persisted but haven't restored yet —
            # try the vault directly. ``_load_pending_states`` only runs
            # at construction; an OAuth callback that lands before a
            # restart-recovered run would otherwise be rejected.
            pending = self._read_persisted_state(state)
            if pending is None:
                return {
                    "error": "Invalid or expired state parameter",
                    "reason": "unknown_state",
                }
        self._drop_pending_state(state)

        provider_id = pending["provider_id"]
        provider = self._providers.get(provider_id)
        if not provider:
            return {
                "error": f"Unknown provider: {provider_id}",
                "reason": "unknown_provider",
            }

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": provider.redirect_uri,
        }

        if provider.pkce:
            data["code_verifier"] = pending.get("code_verifier", "")
            data["client_id"] = provider.client_id
        else:
            data["client_id"] = provider.client_id
            if provider.client_secret:
                data["client_secret"] = provider.client_secret

        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if provider_id == "notion" and provider.client_secret:
                creds = base64.b64encode(
                    f"{provider.client_id}:{provider.client_secret}".encode()
                ).decode()
                headers["Authorization"] = f"Basic {creds}"

            resp = await self._http.post(provider.token_url, data=data, headers=headers)
            resp.raise_for_status()
            token_data = resp.json()
            token_data["obtained_at"] = time.time()
            self._save_token(provider_id, token_data)
            logger.info(f"OAuth tokens obtained for {provider_id}")

            probe_summary = await self._probe_after_callback(provider_id)
            result = {"success": True, "provider": provider_id}
            if probe_summary is not None:
                result["probe"] = probe_summary
            return result
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:400] if exc.response is not None else str(exc)
            logger.error(
                "OAuth token exchange failed for %s: %s %s",
                provider_id, exc.response.status_code if exc.response is not None else "?",
                detail,
            )
            return {
                "error": f"token exchange HTTP {exc.response.status_code}",
                "reason": "token_exchange_failed",
                "detail": detail,
            }
        except Exception as e:
            logger.error(f"OAuth token exchange failed for {provider_id}: {e}")
            return {"error": str(e), "reason": "token_exchange_failed"}

    def _read_persisted_state(self, state: str) -> Optional[dict]:
        """Direct vault/file read for a state we don't have in memory."""
        if self._vault is not None:
            try:
                raw = self._vault.retrieve(
                    f"{PENDING_VAULT_PREFIX}{state}",
                    requester="oauth_manager",
                )
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        try:
            if OAUTH_PENDING_PATH.exists():
                blob = json.loads(OAUTH_PENDING_PATH.read_text())
                pending = (blob or {}).get(state)
                if pending:
                    return pending
        except Exception:
            pass
        return None

    async def _probe_after_callback(self, provider_id: str) -> Optional[dict]:
        """Run a live probe right after a successful token exchange.

        Returns the probe result as a dict, or ``None`` if no probe is
        registered for this provider or probe-on-add is disabled. We
        force the probe (bypassing cache) so the result reflects the
        token we *just* stored, and we record the outcome in
        ``integrations._probe_status`` so the integration's
        ``connected`` property can immediately reflect the live result.
        """
        if not self._probe_on_add:
            return None
        try:
            from security.probe import probe as _probe, registered_probe_ids
            from integrations._probe_status import mark_probe_result
        except Exception as exc:
            logger.debug("oauth: probe import failed: %s", exc)
            return None
        if provider_id not in registered_probe_ids():
            return None
        try:
            result = await _probe(provider_id, vault=self._vault, force=True)
        except Exception as exc:
            logger.warning("oauth: probe-on-add for %s raised: %s",
                           provider_id, exc)
            return {"ok": False, "reason": "probe_raised", "detail": str(exc)}
        mark_probe_result(
            provider_id,
            ok=bool(result.ok),
            reason=result.reason or "",
            detail=result.detail or "",
        )
        return {
            "ok": result.ok,
            "status_code": result.status_code,
            "reason": result.reason,
            "detail": result.detail,
            "latency_ms": round(result.latency_ms, 1),
        }

    async def get_token(self, provider_id: str) -> Optional[str]:
        """Get a valid access token, refreshing if expired."""
        token_data = self._tokens.get(provider_id)
        if not token_data:
            return None

        access_token = token_data.get("access_token", "")
        if not access_token:
            return None

        expires_in = token_data.get("expires_in", 3600)
        obtained_at = token_data.get("obtained_at", 0)
        if time.time() > obtained_at + expires_in - 60:
            refreshed = await self._refresh_token(provider_id)
            if refreshed:
                return self._tokens[provider_id].get("access_token")
            return None

        return access_token

    async def _refresh_token(self, provider_id: str) -> bool:
        """Use the refresh_token to get a new access_token.

        On HTTP 400/401 from the provider (refresh token revoked or
        expired) we drop the stored tokens so subsequent
        ``is_connected`` calls report ``False`` honestly, rather than
        silently returning the stale access token forever. The
        operator-facing UI will show "Connection expired — reconnect"
        and the integration's ``connected`` property will go ``False``.
        """
        token_data = self._tokens.get(provider_id, {})
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            logger.warning(f"No refresh token for {provider_id}")
            return False

        provider = self._providers.get(provider_id)
        if not provider:
            return False

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": provider.client_id,
        }
        if provider.client_secret:
            data["client_secret"] = provider.client_secret

        try:
            resp = await self._http.post(
                provider.token_url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code in (400, 401):
                logger.warning(
                    "Token refresh rejected for %s (HTTP %s) — clearing "
                    "stored tokens so the operator gets a clean reconnect "
                    "prompt. Detail: %s",
                    provider_id, resp.status_code, resp.text[:200],
                )
                self.revoke_token(provider_id)
                return False
            resp.raise_for_status()
            new_tokens = resp.json()
            new_tokens["obtained_at"] = time.time()
            if "refresh_token" not in new_tokens:
                new_tokens["refresh_token"] = refresh_token
            self._save_token(provider_id, new_tokens)
            logger.info(f"Token refreshed for {provider_id}")
            return True
        except Exception as e:
            logger.error(f"Token refresh failed for {provider_id}: {e}")
            return False

    def store_api_token(self, provider_id: str, token: str):
        """Store a long-lived API token (e.g., Home Assistant)."""
        self._save_token(provider_id, {
            "access_token": token,
            "token_type": "bearer",
            "obtained_at": time.time(),
            "expires_in": 999999999,
        })
        logger.info(f"API token stored for {provider_id}")

    def revoke_token(self, provider_id: str):
        self._tokens.pop(provider_id, None)
        if self._vault:
            self._vault.remove(f"oauth_{provider_id}", removed_by="oauth_manager")

    def is_connected(self, provider_id: str) -> bool:
        return provider_id in self._tokens and bool(
            self._tokens[provider_id].get("access_token")
        )

    def status(self) -> dict:
        return {
            "providers": len(self._providers),
            "connected": [pid for pid in self._tokens if self.is_connected(pid)],
            "pending_flows": len(self._pending_states),
            "setup_required": [
                pid for pid in self._providers
                if self.setup_status(pid) == "provider_setup_required"
            ],
        }

    async def close(self):
        await self._http.aclose()
