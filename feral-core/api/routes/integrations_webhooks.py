"""OAuth, integrations, and webhook HTTP endpoints."""

import asyncio
import logging

from fastapi import APIRouter, Query, Request, Response

from api.state import state

logger = logging.getLogger("feral.api.integrations_webhooks")
router = APIRouter()


# ─────────────────────────────────────────────
# OAuth & Integrations API
# ─────────────────────────────────────────────


@router.get("/api/integrations")
async def list_integrations():
    """List all available integrations and their connection status."""
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
    return {
        "providers": providers,
        "spotify_connected": state.spotify.connected if state.spotify else False,
        "home_assistant_connected": state.home_assistant.connected if state.home_assistant else False,
        "notion_connected": state.notion.connected if state.notion else False,
        "gmail_connected": email_connected,
        "email_connected": email_connected,
    }


@router.get("/api/oauth/authorize/{provider_id}")
async def oauth_authorize(provider_id: str):
    """Start an OAuth2 flow — returns the authorization URL.

    When the provider hasn't been set up yet (no client_id baked in
    and operator hasn't supplied one) we surface
    ``setup_status=provider_setup_required`` plus the doc URL so the
    UI can render a "Configure your own Google/Notion/Spotify app"
    panel instead of a misleading error toast.
    """
    if not state.oauth:
        return {
            "error": "OAuth manager not initialized",
            "reason": "oauth_unavailable",
        }
    return state.oauth.build_authorize_response(provider_id)


@router.get("/api/oauth/callback")
async def oauth_callback(state_param: str = Query(alias="state", default=""), code: str = ""):
    """Handle OAuth2 callback from provider."""
    if not state.oauth:
        return {"error": "OAuth manager not initialized"}
    result = await state.oauth.handle_callback(state_param, code)
    return result


@router.post("/api/integrations/token")
async def store_integration_token(body: dict):
    """Store a long-lived API token (e.g., Home Assistant) or Gmail creds."""
    provider_id = body.get("provider_id", "")
    if provider_id == "gmail":
        return await _store_gmail_app_password(body)
    token = body.get("token", "")
    if not provider_id or not token:
        return {"error": "provider_id and token are required"}
    if state.oauth:
        state.oauth.store_api_token(provider_id, token)
    return {"ok": True, "provider": provider_id}


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


@router.post("/api/integrations/disconnect/{provider_id}")
async def disconnect_integration(provider_id: str):
    """Disconnect an integration by revoking its tokens."""
    if provider_id == "gmail" and state.email:
        state.email.clear_app_password()
        return {"ok": True, "provider": "gmail"}
    if state.oauth:
        state.oauth.revoke_token(provider_id)
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
