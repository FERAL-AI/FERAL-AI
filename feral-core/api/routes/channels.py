"""Channel management and WhatsApp webhook endpoints."""

import logging
import os
import asyncio
import re

from fastapi import APIRouter, Request, Response

from api.state import state

logger = logging.getLogger("feral.brain")

router = APIRouter()


@router.post("/api/channels/stop")
async def stop_channel(body: dict, response: Response):
    """Stop the captured listener and its owned tasks, without deleting credentials."""
    channel_type = body.get("type")
    if not isinstance(channel_type, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", channel_type):
        response.status_code = 422
        return {"ok": False, "reason": "invalid_type"}
    manager = state.channel_manager
    if manager is None:
        response.status_code = 503
        return {"ok": False, "reason": "manager_unavailable"}
    channels = getattr(manager, "_channels", None)
    if not isinstance(channels, dict):
        response.status_code = 501
        return {"ok": False, "reason": "unsupported_manager"}
    channel = channels.get(channel_type)
    if channel is None:
        response.status_code = 404
        return {"ok": False, "reason": "not_active", "channel": channel_type}
    stop = getattr(channel, "stop", None)
    if not callable(stop):
        response.status_code = 501
        return {"ok": False, "reason": "unsupported_channel", "channel": channel_type}
    http_client = getattr(channel, "_http", None)
    try:
        # These fields are channel-owned task registries. Never inspect or
        # cancel the event loop's tasks, orchestrator, or shared scheduler.
        channel._running = False
        tasks = set(getattr(channel, "_bg_tasks", set()))
        poll_task = getattr(channel, "_poll_task", None)
        if poll_task is not None:
            tasks.add(poll_task)
        tasks = {task for task in tasks if isinstance(task, asyncio.Task)
                 and task is not asyncio.current_task()}
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in done:
                if not task.cancelled():
                    task.exception()  # Observe failures while draining.
            if pending:
                raise RuntimeError("owned tasks did not drain")
        await asyncio.wait_for(stop(), timeout=10)
        if bool(getattr(channel, "_running", False)):
            raise RuntimeError("listener still running")
        if http_client is not None and getattr(http_client, "is_closed", None) is False:
            raise RuntimeError("captured HTTP client did not close")
    except Exception:
        response.status_code = 502
        return {"ok": False, "reason": "stop_failed", "channel": channel_type,
                "runtime_outcome": "uncertain"}
    if (state.channel_manager is not manager or getattr(manager, "_channels", None) is not channels
            or channels.get(channel_type) is not channel):
        response.status_code = 409
        return {"ok": False, "reason": "channel_replaced", "channel": channel_type,
                "captured_channel_stopped": True}
    del channels[channel_type]
    return {"ok": True, "channel": channel_type, "stopped": True,
            "scope": "runtime_only", "saved_configuration_changed": False,
            "credentials_revoked": False}


@router.get("/api/channels")
async def list_channels():
    if not state.channel_manager:
        return {"channels": []}
    return state.channel_manager.stats


@router.post("/api/channels/start")
async def start_channel(body: dict, response: Response):
    """Start a messaging channel and report what actually happened.

    This used to answer ``{"ok": True, "channel": <type>}`` no matter
    what. Asking it to start ``signal`` logged "Unknown channel type"
    server-side and reported success to the caller; so did a channel that
    came up degraded or never connected. ``ChannelManager.start_channel``
    now returns a status and this surfaces it.
    """
    channel_type = body.get("type", "")
    config = body.get("config", {})
    if not state.channel_manager:
        response.status_code = 503
        return {"ok": False, "error": "Channel manager not initialized"}

    outcome = await state.channel_manager.start_channel(channel_type, config)
    # Tolerate a manager that predates the status return rather than
    # calling an unknown result a failure.
    if not isinstance(outcome, dict):
        return {"ok": True, "channel": channel_type}

    if outcome.get("started"):
        return {"ok": True, "channel": channel_type}

    reason = str(outcome.get("reason") or "start_failed")
    response.status_code = 404 if reason == "unknown_channel_type" else 502
    return {
        "ok": False,
        "channel": channel_type,
        "reason": reason,
        "error": outcome.get("detail") or f"{channel_type} did not start",
    }


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
