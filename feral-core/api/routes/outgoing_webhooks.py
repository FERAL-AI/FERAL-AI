"""
Outgoing webhooks REST surface — ``/api/outgoing-webhooks/*``.

The operator subscribes a target URL to one or more internal event
types ("chat.completed", "memory.episode_saved", "*", etc.). On each
matching event the brain POSTs a signed JSON envelope to the URL.
See ``integrations.outgoing_webhooks`` for the deliverer + signing
contract.
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Request, Response

from api.state import state

logger = logging.getLogger("feral.api.outgoing_webhooks")
router = APIRouter(tags=["outgoing-webhooks"])


def _resolve_store():
    """Lazy-init the outgoing webhook store the same way the inbound
    custom-webhook router does. Keeps boot wiring out of api/state.py
    (Lane 06's territory) while still giving every request a single
    durable instance."""
    existing = getattr(state, "outgoing_webhook_store", "__missing__")
    if existing is None:
        return None
    if existing != "__missing__":
        return existing
    try:
        from integrations.outgoing_webhooks import OutgoingWebhookStore

        store = OutgoingWebhookStore()
    except Exception as exc:  # pragma: no cover
        logger.warning("outgoing-webhook store init failed: %s", exc)
        return None
    try:
        state.outgoing_webhook_store = store
    except Exception:
        pass
    return store


def _resolve_deliverer():
    """Same lazy-init pattern for the deliverer. Wired into the event
    bus on first use so the operator can subscribe before any other
    code touches the bus."""
    existing = getattr(state, "outgoing_webhook_deliverer", "__missing__")
    if existing is None:
        return None
    if existing != "__missing__":
        return existing
    store = _resolve_store()
    if store is None:
        return None
    try:
        from integrations.outgoing_webhooks import OutgoingWebhookDeliverer

        deliverer = OutgoingWebhookDeliverer(store=store)
    except Exception as exc:  # pragma: no cover
        logger.warning("outgoing-webhook deliverer init failed: %s", exc)
        return None
    bus = getattr(state, "event_bus", None)
    if bus is not None:
        try:
            bus.on_all(deliverer.handle_event)
        except Exception as exc:
            logger.warning("outgoing-webhook bus subscription failed: %s",
                           exc)
    try:
        state.outgoing_webhook_deliverer = deliverer
    except Exception:
        pass
    return deliverer


@router.post("/api/outgoing-webhooks")
async def create_subscription(body: dict):
    """Subscribe a target URL to one or more event types.

    Body: ``{name, target_url, secret?, event_types?, enabled?}``

    ``event_types`` is a list of strings — ``"*"`` for every event,
    a literal type like ``"chat.completed"``, or a namespace prefix
    like ``"memory.*"``.
    """
    name = (body.get("name") or "").strip()
    target_url = (body.get("target_url") or "").strip()
    if not target_url:
        return Response(
            status_code=400,
            content='{"error":"target_url is required",'
                    '"reason":"missing_target_url"}',
            media_type="application/json",
        )
    if not target_url.lower().startswith(("http://", "https://")):
        return Response(
            status_code=400,
            content='{"error":"target_url must be http(s)://...",'
                    '"reason":"invalid_target_url"}',
            media_type="application/json",
        )
    secret = body.get("secret") or ""
    event_types = body.get("event_types") or ["*"]
    enabled = body.get("enabled", True)
    store = _resolve_store()
    if store is None:
        return Response(
            status_code=503,
            content='{"error":"outgoing webhook store not initialized"}',
            media_type="application/json",
        )
    record = await store.create(
        name=name or target_url,
        target_url=target_url,
        secret=secret,
        event_types=event_types,
        enabled=bool(enabled),
    )
    # Make sure the deliverer is wired even before the first event.
    _resolve_deliverer()
    return {"success": True, "webhook": record}


@router.get("/api/outgoing-webhooks")
async def list_subscriptions():
    store = _resolve_store()
    if store is None:
        return {"webhooks": []}
    rows = await store.list_all()
    # Strip the secret in the listing — fingerprint only.
    public_rows = []
    for r in rows:
        copy = dict(r)
        if copy.get("secret"):
            copy["secret"] = "•" * 8
            copy["has_secret"] = True
        else:
            copy["has_secret"] = False
        public_rows.append(copy)
    return {"webhooks": public_rows}


@router.delete("/api/outgoing-webhooks/{webhook_id}")
async def delete_subscription(webhook_id: str):
    store = _resolve_store()
    if store is None:
        return Response(
            status_code=503,
            content='{"error":"outgoing webhook store not initialized"}',
            media_type="application/json",
        )
    deleted = await store.delete(webhook_id)
    return {"success": bool(deleted),
            "error": None if deleted else "Webhook not found"}


@router.post("/api/outgoing-webhooks/{webhook_id}/test")
async def test_fire_subscription(webhook_id: str, body: Optional[dict] = None):
    """Fire a synthetic event so the operator can verify wiring before
    going live. The deliverer reports ok / failed inline so the WebUI
    can render the result without polling."""
    deliverer = _resolve_deliverer()
    if deliverer is None:
        return Response(
            status_code=503,
            content='{"error":"outgoing webhook deliverer not initialized"}',
            media_type="application/json",
        )
    from integrations.webhook_receiver import WebhookEvent

    payload = (body or {}).get("payload", {"hello": "from FERAL test fire"})
    event_type = (body or {}).get("event_type", "test.ping")
    event = WebhookEvent(
        app_id="feral_test",
        event_type=event_type,
        payload=payload,
        timestamp=time.time(),
    )
    delivered = await deliverer.deliver_now(webhook_id, event)
    return {"delivered": delivered, "event_type": event_type}
