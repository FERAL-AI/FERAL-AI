"""OAuth, integrations, and webhook HTTP endpoints."""

import asyncio
import logging
from html import escape as html_escape
from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from api.state import state

logger = logging.getLogger("feral.api.integrations_webhooks")
router = APIRouter()


# ─────────────────────────────────────────────
# OAuth & Integrations API
# ─────────────────────────────────────────────


async def _probe_provider(provider_id: str) -> tuple[Optional[bool], dict]:
    """Run one provider's probe now and return ``(ok, detail)``.

    ``ok`` is ``None`` when no probe is registered for the provider, so
    the caller can keep its existing fallback rather than reporting a
    disconnection it has no evidence for.
    """
    from integrations import _probe_status

    ok = await _probe_status.refresh(provider_id, vault=getattr(state, "vault", None))
    detail = _probe_status.snapshot().get(provider_id, {})
    return ok, detail


@router.get("/api/integrations")
async def list_integrations(refresh: bool = True):
    """List all available integrations and their connection status.

    Probes providers whose cached verdict has expired before answering,
    so the ``connected`` flags in the response are the result of a real
    round-trip rather than "a token string exists somewhere". Pass
    ``?refresh=0`` to read the cache as-is (a poller that does not want
    to trigger network calls).

    Nothing used to run the probes at all: ``probe_all`` and every
    integration's ``probe_connected`` had no caller in the brain, so
    after the 60 second cache from an OAuth callback expired, every badge
    reverted to token presence permanently. A revoked token stayed green
    until someone re-authorized.
    """
    if refresh:
        from integrations import probe_sweeper

        vault = getattr(state, "vault", None)
        try:
            await probe_sweeper.sweep_once(vault=vault, only_stale=True)
            probe_sweeper.ensure_started(vault=vault)
        except Exception as exc:
            # A probe sweep must never be able to take the settings page
            # down; a stale badge beats a 500.
            logger.warning("integration probe sweep failed: %s", exc)

    providers = state.oauth.list_providers() if state.oauth else []
    email_connected = bool(state.email and state.email.connected)
    # Surface the Gmail App Password path as a first-class provider row so
    # the Settings UI badge reflects the live IMAP/SMTP connection.
    if not any((p.get("id") == "gmail") for p in providers):
        providers = list(providers) + [{
            "id": "gmail",
            "name": "Gmail",
            "auth_type": "app_password",
            "connected": email_connected,
            "has_client_id": False,
        }]

    # Tell the client WHICH claim each badge is making. ``connected`` on
    # its own cannot distinguish "the provider answered us just now" from
    # "we are holding a token string and have never checked it", and a UI
    # that renders both as one green dot is the reason a revoked token
    # looked fine for months.
    from integrations import _probe_status

    statuses = _probe_status.snapshot()
    providers = [
        {
            **p,
            "probe_verified": _probe_status.verified(p.get("id", "")),
            "probe_reason": statuses.get(p.get("id", ""), {}).get("reason", ""),
            "probe_detail": statuses.get(p.get("id", ""), {}).get("detail", ""),
            "probe_age_s": statuses.get(p.get("id", ""), {}).get("age_s"),
        }
        for p in providers
    ]

    ha = getattr(state, "home_assistant", None)
    return {
        "providers": providers,
        "spotify_connected": state.spotify.connected if state.spotify else False,
        "home_assistant_connected": ha.connected if ha else False,
        "home_assistant_url": getattr(ha, "base_url", "") if ha else "",
        "notion_connected": state.notion.connected if state.notion else False,
        "gmail_connected": email_connected,
        "email_connected": email_connected,
        "probe_statuses": statuses,
    }


@router.get("/api/oauth/authorize/{provider_id}")
async def oauth_authorize(provider_id: str, redirect: bool = False):
    """Start an OAuth2 flow.

    Returns the authorization URL as JSON by default, which is what a
    fetch-then-open caller wants. Pass ``?redirect=1`` to get a 302 to
    the provider instead, which is what a plain ``window.open`` needs.

    Without that parameter a browser pointed straight at this route
    renders raw JSON and the user is stuck: the Settings card did
    exactly that, so clicking Authorize showed a popup full of
    ``{"success": true, "url": ...}`` rather than the provider's
    consent screen. The JSON form is kept because the v1 wizard reads
    it and because an error needs a body a UI can render.

    When the provider has no client_id (none baked in and the operator
    has not supplied one) the response carries
    ``setup_status=provider_setup_required`` plus the doc URL, so the
    UI can render a "configure your own app" panel rather than a
    misleading error toast. That case never redirects.
    """
    if not state.oauth:
        return {
            "error": "OAuth manager not initialized",
            "reason": "oauth_unavailable",
        }
    result = state.oauth.build_authorize_response(provider_id)
    if redirect and result.get("success") and result.get("url"):
        return RedirectResponse(url=result["url"], status_code=302)
    return result


def _callback_page(ok: bool, provider: str, detail: str) -> HTMLResponse:
    """Human-readable end of the OAuth round trip.

    The provider redirects a real browser here, so returning a dict
    left the user staring at ``{"success": true, "provider": "whoop"}``
    with no idea whether it worked or what to do next, and the Settings
    tab that opened the popup never learned the flow had finished.

    ``postMessage`` lets the opener refresh itself. It is sent to "*"
    because the brain is reached on many origins (localhost, a LAN IP,
    a tailnet name) and the message carries no secret, only the fact
    that a provider finished.
    """
    title = "Connected" if ok else "Connection failed"
    colour = "#16a34a" if ok else "#dc2626"
    safe_provider = html_escape(provider or "provider")
    safe_detail = html_escape(detail or "")
    body = f"""<!doctype html>
<meta charset="utf-8">
<title>FERAL {title}</title>
<style>
  body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif;
         display: grid; place-items: center; height: 100vh; margin: 0;
         color: #111; background: #fafafa; }}
  .card {{ text-align: center; max-width: 30rem; padding: 2rem; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; color: {colour}; }}
  p {{ margin: .25rem 0; color: #444; }}
  .hint {{ color: #777; font-size: 13px; margin-top: 1rem; }}
</style>
<div class="card">
  <h1>{title}</h1>
  <p><strong>{safe_provider}</strong></p>
  <p>{safe_detail}</p>
  <p class="hint">You can close this window and return to FERAL.</p>
</div>
<script>
  try {{
    if (window.opener) {{
      window.opener.postMessage(
        {{ type: "feral:oauth", ok: {str(ok).lower()},
           provider: {safe_provider!r} }}, "*");
    }}
  }} catch (e) {{}}
</script>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


@router.get("/api/oauth/callback")
async def oauth_callback(
    state_param: str = Query(alias="state", default=""),
    code: str = "",
    format: str = "",
):
    """Handle the OAuth2 callback from a provider.

    Renders HTML by default, because the thing that lands here is a
    real browser following a redirect, not a fetch. Pass ``?format=json``
    for the old machine-readable shape.
    """
    if not state.oauth:
        if format == "json":
            return {"error": "OAuth manager not initialized"}
        return _callback_page(False, "", "OAuth manager not initialized.")

    result = await state.oauth.handle_callback(state_param, code)
    if format == "json":
        return result

    ok = bool(result.get("success"))
    provider = str(result.get("provider") or "")
    detail = (
        "FERAL can now use this account."
        if ok
        else str(result.get("error") or "The provider did not return a usable token.")
    )
    return _callback_page(ok, provider, detail)


@router.post("/api/integrations/token")
async def store_integration_token(body: dict):
    """Store a long-lived API token (e.g., Home Assistant) or Gmail creds.

    Home Assistant additionally accepts ``url`` (alias ``base_url``).
    Until this existed the card had one field, a token, while
    ``HomeAssistantIntegration`` resolved its base URL only from
    ``FERAL_HA_URL`` / ``HA_URL`` and otherwise assumed
    ``http://homeassistant.local:8123``. Anyone whose Home Assistant sits
    on a static LAN IP, a non-default port, or a reverse proxy could not
    connect from the UI at all: they had to set an env var and restart.
    """
    provider_id = body.get("provider_id", "")
    if provider_id == "gmail":
        return await _store_gmail_app_password(body)
    if provider_id == "home_assistant":
        return await _store_home_assistant_token(body)
    token = body.get("token", "")
    if not provider_id or not token:
        return {"error": "provider_id and token are required"}
    if state.oauth:
        # An OAuth2 provider has no long-lived token to paste. The
        # WebUI used to POST the *client secret* here, which landed in
        # the access_token slot with a 30-year expiry: is_connected went
        # permanently True, every API call 401'd, and the IMAP/ICS
        # fallbacks that key off is_connected were switched off.
        provider = state.oauth.get_provider(provider_id)
        if provider is not None and provider.auth_type == "oauth2":
            return {
                "error": (
                    f"{provider.name} uses OAuth2 — a client secret is not "
                    "an access token. POST client_id + client_secret to "
                    "/api/integrations/oauth/client, then run the authorize "
                    "flow."
                ),
                "reason": "oauth2_provider",
                "provider": provider_id,
            }
        state.oauth.store_api_token(provider_id, token)
    return {"ok": True, "provider": provider_id}


@router.post("/api/integrations/oauth/client")
async def store_oauth_client(body: dict):
    """Save the operator's own OAuth app credentials for a provider.

    Client credentials identify the *application*; the user's tokens
    still come from the authorize flow afterwards. Persisted where
    ``OAuthManager`` resolves credentials (vault, or the
    ``first_party_clients.json`` overlay) and reloaded in place, so
    ``/api/oauth/authorize/{provider_id}`` works without a restart.
    """
    if not state.oauth:
        return {
            "error": "OAuth manager not initialized",
            "reason": "oauth_unavailable",
        }
    provider_id = (body.get("provider_id") or "").strip()
    client_id = (body.get("client_id") or "").strip()
    client_secret = (body.get("client_secret") or "").strip()
    try:
        state.oauth.store_client_credentials(
            provider_id, client_id, client_secret,
        )
    except ValueError as exc:
        return {
            "error": str(exc),
            "reason": "invalid_client_credentials",
            "provider": provider_id,
        }
    # An env var or a hand-edited ~/.feral/oauth_providers.json outranks
    # what we just saved. Report that instead of a green "saved" that
    # does nothing.
    provider = state.oauth.get_provider(provider_id)
    applied = provider is not None and provider.client_id == client_id
    return {
        "ok": True,
        "provider": provider_id,
        "applied": applied,
        "setup_status": state.oauth.setup_status(provider_id),
        "has_client_secret": bool(client_secret),
        **({} if applied else {
            "warning": (
                "Saved, but a higher-priority source (environment variable "
                "or ~/.feral/oauth_providers.json) still supplies the "
                "client_id for this provider."
            ),
        }),
    }


async def _store_home_assistant_token(body: dict) -> dict:
    """Save the Home Assistant URL + long-lived token, then probe for real.

    Both fields are optional individually so an operator can correct just
    the URL without re-pasting the token, but at least one must be
    present. The response carries the probe verdict rather than a bare
    ``ok``: "saved" and "reachable" are different claims and the card
    renders both.
    """
    raw_url = (body.get("url") or body.get("base_url") or "").strip()
    token = (body.get("token") or "").strip()
    if not raw_url and not token:
        return {"error": "token or url is required", "provider": "home_assistant"}

    ha = getattr(state, "home_assistant", None)
    base_url = ""
    if raw_url:
        if ha is None:
            return {
                "error": "Home Assistant integration not initialized",
                "provider": "home_assistant",
            }
        try:
            base_url = ha.set_base_url(raw_url)
        except ValueError as exc:
            return {
                "ok": False,
                "provider": "home_assistant",
                "error": str(exc),
                "reason": "invalid_url",
            }
    elif ha is not None:
        base_url = ha.base_url

    if token:
        if state.oauth:
            state.oauth.store_api_token("home_assistant", token)
        if ha is not None:
            # The integration caches the token it read from HA_TOKEN at
            # construction and prefers it over the OAuth manager, so a
            # token pasted into the UI would otherwise be shadowed by an
            # empty env var for the life of the process.
            ha._token = token
            ha._http = None

    connected, probe = await _probe_provider("home_assistant")
    return {
        "ok": True,
        "provider": "home_assistant",
        "base_url": base_url,
        "connected": bool(connected),
        "probe": probe,
    }


async def _store_gmail_app_password(body: dict) -> dict:
    """Validate Gmail address + App Password against Gmail's IMAP/SMTP
    servers and persist only on success, so a green badge is truthful."""
    address = (body.get("address") or "").strip()
    app_password = (body.get("app_password") or body.get("token") or "").replace(" ", "")
    if not address or not app_password:
        return {"error": "address and app_password are required"}
    if not state.email:
        return {"error": "Email integration not available"}
    probe = await asyncio.to_thread(
        state.email.probe_app_password, address, app_password
    )
    if not probe.get("imap"):
        logger.warning("Gmail App Password probe failed: %s", probe.get("imap_error"))
        return {
            "ok": False,
            "provider": "gmail",
            "connected": False,
            "error": probe.get("imap_error", "IMAP login failed"),
            "probe": probe,
        }
    state.email.store_app_password(address, app_password)
    return {"ok": True, "provider": "gmail", "connected": True, "probe": probe}


@router.post("/api/integrations/refresh")
async def refresh_integration_status(body: dict | None = None):
    """Re-probe integrations and report what each provider actually says.

    This is the operator-initiated "Refresh status" action that
    ``integrations/_probe_status.py`` has documented as a writer of the
    cache since it was written, and that no route implemented.

    Body (optional): ``{"provider_id": "spotify"}`` to refresh one
    provider; omit it to sweep every provider with a registered probe.
    Bypasses the cache TTL — the point of pressing the button is to
    distrust the cached answer.

    ``results`` maps provider id to ``true`` / ``false`` / ``null``,
    where ``null`` means no probe is registered for that provider and its
    badge keeps whatever fallback it had. ``statuses`` carries the
    reason and detail behind each verdict, which is what turns "not
    connected" into something an operator can act on.
    """
    from integrations import _probe_status, probe_sweeper

    provider_id = ((body or {}).get("provider_id") or "").strip()
    vault = getattr(state, "vault", None)

    if provider_id:
        known = probe_sweeper.known_provider_ids()
        if known and provider_id not in known:
            return {
                "ok": False,
                "error": f"no probe registered for {provider_id!r}",
                "reason": "no_probe",
                "known": sorted(known),
            }
        results = await probe_sweeper.sweep_once(
            vault=vault, provider_ids=[provider_id],
        )
    else:
        results = await probe_sweeper.sweep_once(vault=vault)

    probe_sweeper.ensure_started(vault=vault)
    return {
        "ok": True,
        "results": results,
        "statuses": _probe_status.snapshot(),
        "sweeper_running": probe_sweeper.is_running(),
    }


@router.post("/api/integrations/disconnect/{provider_id}")
async def disconnect_integration(provider_id: str):
    """Disconnect an integration by revoking its tokens."""
    if provider_id == "gmail" and state.email:
        state.email.clear_app_password()
        return {"ok": True, "provider": "gmail"}
    if state.oauth:
        state.oauth.revoke_token(provider_id)
    # A cached ``ok=True`` from a probe that ran seconds ago would keep
    # the badge green for the rest of the TTL after the operator pressed
    # Disconnect. Record the truth we already know.
    from integrations import _probe_status

    _probe_status.mark_probe_result(
        provider_id, ok=False, reason="disconnected",
        detail="credentials revoked by operator",
    )
    return {"ok": True, "provider": provider_id}


# ─────────────────────────────────────────────
# Webhook API
# ─────────────────────────────────────────────


@router.post("/api/webhooks/{app_id}")
async def receive_webhook(app_id: str, request: Request):
    """Receive an incoming webhook from an external app.

    Reads the **raw** request body (not a parsed JSON dict) so HMAC
    signatures stay byte-exact, and forwards the **real** request
    headers — pre-Lane-10 the route always passed ``headers={}`` which
    made GitHub/Stripe/HA HMAC verification unreachable through this
    HTTP path even when the operator had configured a secret. The
    receiver is fail-closed: when a secret is configured and the
    signature is missing or wrong the request is rejected with a 401
    or 403 rather than accepted with a misleading
    ``verified=false`` flag.
    """
    if not state.webhook_receiver:
        return Response(
            status_code=503,
            content='{"error":"Webhook receiver not initialized"}',
            media_type="application/json",
        )
    body_bytes = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    content_type = request.headers.get("content-type", "application/json")
    result = await state.webhook_receiver.handle_request(
        app_id=app_id,
        body=body_bytes,
        headers=headers,
        content_type=content_type,
    )
    if not result.get("accepted"):
        reason = result.get("reason") or "rejected"
        status_code = 401 if reason == "missing_signature" else (
            403 if reason == "invalid_signature" else 400
        )
        return Response(
            status_code=status_code,
            content=f'{{"error":"{result.get("error", "rejected")}",'
                    f'"reason":"{reason}"}}',
            media_type="application/json",
        )
    return result


@router.get("/api/webhooks")
async def list_webhooks():
    """List registered webhook configurations."""
    if not state.webhook_receiver:
        return {"webhooks": []}
    return {
        "webhooks": state.webhook_receiver.list_webhooks(),
        "events": state.event_bus.recent_events(20) if state.event_bus else [],
    }


@router.put("/api/webhooks/{app_id}/config")
async def update_webhook_config(app_id: str, body: dict):
    """Persist per-app HMAC secret / signature header / enabled flag
    for an integration ingress webhook (GitHub, Stripe, Home
    Assistant, Notion, …).

     (finding-19): pre-v2026.5.43 there was no production HTTP
    surface for setting these secrets — operators had to monkey-patch
    ``_configs`` in a Python shell and the value vanished on restart.
    The receiver now writes through to the WebhookStore so the secret
    survives a brain restart and external services keep validating
    against the same shared key.

    Body fields (all optional):

    * ``secret`` — HMAC shared secret. Stored in the integration
      webhook table; never echoed back in cleartext.
    * ``signature_header`` — header the provider stamps the HMAC into
      (e.g. ``X-Hub-Signature-256``).
    * ``signature_prefix`` — e.g. ``sha256=``.
    * ``hash_algorithm`` — ``sha256`` (default) or ``sha1``.
    * ``enabled`` — bool, default true.

    Returns the updated config with the secret **fingerprinted** so
    the UI can show "has_secret: true" without surfacing the raw key.
    """
    if not state.webhook_receiver:
        return Response(
            status_code=503,
            content='{"error":"Webhook receiver not initialized"}',
            media_type="application/json",
        )

    from integrations.webhook_receiver import WebhookConfig

    existing = state.webhook_receiver._configs.get(app_id)
    if existing is None:
        existing = WebhookConfig(app_id=app_id)

    secret = body.get("secret")
    if secret is not None:
        existing.secret = str(secret)
    sig_header = body.get("signature_header")
    if sig_header is not None:
        existing.signature_header = str(sig_header)
    sig_prefix = body.get("signature_prefix")
    if sig_prefix is not None:
        existing.signature_prefix = str(sig_prefix)
    hash_algo = body.get("hash_algorithm")
    if hash_algo is not None:
        existing.hash_algorithm = str(hash_algo)
    enabled = body.get("enabled")
    if enabled is not None:
        existing.enabled = bool(enabled)

    await state.webhook_receiver.upsert_config(existing)

    return {
        "ok": True,
        "app_id": app_id,
        "enabled": existing.enabled,
        "has_secret": bool(existing.secret),
        "signature_header": existing.signature_header,
        "signature_prefix": existing.signature_prefix,
        "hash_algorithm": existing.hash_algorithm,
    }
