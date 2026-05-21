"""OAuth, integrations, and webhook HTTP endpoints."""

import json

from fastapi import APIRouter, Query

from api.state import state

router = APIRouter()


# ─────────────────────────────────────────────
# OAuth & Integrations API
# ─────────────────────────────────────────────


@router.get("/api/integrations")
async def list_integrations():
    """List all available integrations and their connection status."""
    providers = state.oauth.list_providers() if state.oauth else []
    return {
        "providers": providers,
        "spotify_connected": state.spotify.connected if state.spotify else False,
        "home_assistant_connected": state.home_assistant.connected if state.home_assistant else False,
        "notion_connected": state.notion.connected if state.notion else False,
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
    """Store a long-lived API token (e.g., Home Assistant)."""
    provider_id = body.get("provider_id", "")
    token = body.get("token", "")
    if not provider_id or not token:
        return {"error": "provider_id and token are required"}
    if state.oauth:
        state.oauth.store_api_token(provider_id, token)
    return {"ok": True, "provider": provider_id}


@router.post("/api/integrations/disconnect/{provider_id}")
async def disconnect_integration(provider_id: str):
    """Disconnect an integration by revoking its tokens."""
    if state.oauth:
        state.oauth.revoke_token(provider_id)
    return {"ok": True, "provider": provider_id}


# ─────────────────────────────────────────────
# Webhook API
# ─────────────────────────────────────────────


@router.post("/api/webhooks/{app_id}")
async def receive_webhook(app_id: str, request_body: dict = None):
    """Receive an incoming webhook from an external app."""
    if not state.webhook_receiver:
        return {"error": "Webhook receiver not initialized"}
    body_bytes = json.dumps(request_body or {}).encode() if request_body else b"{}"
    result = await state.webhook_receiver.handle_request(
        app_id=app_id,
        body=body_bytes,
        headers={},
        content_type="application/json",
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
