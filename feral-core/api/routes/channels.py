"""Channel management and WhatsApp webhook endpoints."""

import logging
import os

from fastapi import APIRouter, Request, Response

from api.state import state

logger = logging.getLogger("feral.brain")

router = APIRouter()


@router.get("/api/channels")
async def list_channels():
    if not state.channel_manager:
        return {"channels": []}
    return state.channel_manager.stats


@router.post("/api/channels/start")
async def start_channel(body: dict):
    channel_type = body.get("type", "")
    config = body.get("config", {})
    if not state.channel_manager:
        return {"error": "Channel manager not initialized"}
    await state.channel_manager.start_channel(channel_type, config)
    return {"ok": True, "channel": channel_type}


@router.get("/api/channels/whatsapp/webhook")
async def whatsapp_webhook_verify(request: Request):
    """WhatsApp webhook verification (GET challenge)."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    # Canonical env key is ``FERAL_WHATSAPP_VERIFY_TOKEN`` (matches the
    # rest of the FERAL_* credential namespace and what bootstrap/config
    # expects). The unprefixed ``WHATSAPP_VERIFY_TOKEN`` is kept as a
    # backward-compat fallback so existing deployments don't break.
    verify_token = ""
    try:
        if getattr(state, "config", None) and hasattr(state.config, "get_credential"):
            verify_token = state.config.get_credential("FERAL_WHATSAPP_VERIFY_TOKEN", "") or ""
    except Exception:
        verify_token = ""
    if not verify_token:
        try:
            if getattr(state, "vault", None) and hasattr(state.vault, "retrieve"):
                verify_token = state.vault.retrieve("FERAL_WHATSAPP_VERIFY_TOKEN") or ""
        except Exception:
            verify_token = ""
    if not verify_token:
        verify_token = (
            os.environ.get("FERAL_WHATSAPP_VERIFY_TOKEN")
            or os.environ.get("WHATSAPP_VERIFY_TOKEN")
        )
    if mode == "subscribe" and token == verify_token:
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Forbidden", status_code=403)


@router.post("/api/channels/whatsapp/webhook")
async def whatsapp_webhook_inbound(request: Request):
    """Handle inbound WhatsApp messages.

    **Fail-closed signature verification.** Pre-Lane-10 this route
    parsed the JSON body and called ``handle_webhook`` without ever
    invoking ``WhatsAppChannel.verify_signature`` — finding 19 names
    this directly: "WhatsApp channel webhook → POST **unsigned**."
    Any request hitting the public URL was treated as legitimate
    Meta traffic, regardless of the X-Hub-Signature-256 header.

    Lane 10 reads the **raw** request body, asks the channel to
    verify the signature against the configured ``app_secret``, and
    only forwards verified payloads to the channel handler. When no
    ``app_secret`` is configured the channel's ``verify_signature``
    returns ``True`` (unsigned-mode) so an operator who explicitly
    chose not to enforce a signature is not regressed; setting
    ``FERAL_WHATSAPP_APP_SECRET`` flips the flow to fail-closed.
    """
    try:
        from channels.base import WhatsAppChannel
        import json as _json

        raw_body = await request.body()
        signature = (
            request.headers.get("x-hub-signature-256")
            or request.headers.get("X-Hub-Signature-256")
            or ""
        )

        channel_mgr = getattr(state, "channel_manager", None)
        if not channel_mgr:
            return Response(
                status_code=503,
                content='{"status":"no_handler"}',
                media_type="application/json",
            )
        wa = channel_mgr.get_channel("whatsapp")
        if wa is None or not isinstance(wa, WhatsAppChannel):
            return Response(
                status_code=503,
                content='{"status":"no_handler"}',
                media_type="application/json",
            )

        if not wa.verify_signature(raw_body, signature):
            logger.warning(
                "WhatsApp inbound rejected: signature invalid "
                "(len=%d, sig=%r)", len(raw_body), signature[:32],
            )
            return Response(
                status_code=403,
                content='{"status":"error","reason":"invalid_signature"}',
                media_type="application/json",
            )

        try:
            body = _json.loads(raw_body) if raw_body else {}
        except _json.JSONDecodeError:
            return Response(
                status_code=400,
                content='{"status":"error","reason":"invalid_json"}',
                media_type="application/json",
            )

        response = await wa.handle_webhook(body)
        return {"status": "ok", "response": response}
    except Exception as e:
        logger.error(f"WhatsApp webhook error: {e}")
        return {"status": "error", "detail": str(e)}
