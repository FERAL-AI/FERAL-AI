"""
Custom-webhook HTTP surface — create / list / delete / receive.

This router is the operator-facing custom-webhook subsystem (separate
from the integration event-bus router in
``api.routes.integrations_webhooks``). Lane 10 unifies the two paths
behind a persistent registry (``integrations.webhook_store``) and
moves the routes under ``/api/custom-webhooks/*`` so they no longer
collide with the ``POST /api/webhooks/{app_id}`` integration ingress.

A small backwards-compat alias is kept on the legacy
``POST /api/webhooks/{id}/receive`` URL so any operator who already
configured an external service to post there keeps working — the
alias resolves the persistent store, not the long-gone module dict.

Signature verification is fail-closed when a secret is configured:
missing header → 401, mismatch → 403. Both bodies use the consistent
``signature`` keyword so existing tests can grep for it.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Request, Response

from api.state import state

logger = logging.getLogger("feral.api.webhooks")
router = APIRouter(tags=["webhooks"])


# ─────────────────────────────────────────────────────────────────────
# Backwards-compat shim — pre-Lane-10 tests poked rows into a module
# dict named ``_webhooks``. The persistent store is the truth now, but
# we expose a wrapper so legacy test fixtures (and any external code
# that imported ``_webhooks`` directly) keep functioning. Reads/writes
# on the dict are mirrored to the store at receive time.
# ─────────────────────────────────────────────────────────────────────


_webhooks: dict[str, dict] = {}


def _resolve_store():
    """Return the WebhookStore singleton, lazy-initialising on first
    use so we don't have to touch ``api/state.py`` boot wiring (owned
    by Lane 06). ``state.webhook_store`` is created on demand and
    cached on the state object so subsequent requests reuse it.

    Tests can override by setting ``state.webhook_store`` to ``None``
    (forces fall-through to the legacy in-memory ``_webhooks`` dict)
    or to a custom :class:`WebhookStore` instance pointing at a
    temp DB.
    """
    existing = getattr(state, "webhook_store", "__missing__")
    if existing is None:
        return None
    if existing != "__missing__":
        return existing
    try:
        from integrations.webhook_store import WebhookStore

        store = WebhookStore()
    except Exception as exc:  # pragma: no cover — aiosqlite import failure path
        logger.warning("custom-webhook store init failed: %s", exc)
        return None
    try:
        state.webhook_store = store
    except Exception:
        # ``state`` is a SimpleNamespace-ish singleton; in some test
        # patches it can refuse attribute assignment. Fall back to
        # returning a one-shot store rather than crashing the route.
        pass
    return store


def _legacy_record(webhook_id: str) -> Optional[dict]:
    return _webhooks.get(webhook_id)


# ─────────────────────────────────────────────────────────────────────
# Canonical /api/custom-webhooks/* surface
# ─────────────────────────────────────────────────────────────────────


@router.post("/api/custom-webhooks/create")
async def create_custom_webhook(body: dict):
    name = body.get("name") or "Untitled Webhook"
    secret = body.get("secret") or ""
    action = body.get("action") or "chat"
    action_params = body.get("action_params") or {}

    store = _resolve_store()
    if store is None:
        return {
            "success": False,
            "error": "webhook store not initialized",
            "reason": "store_unavailable",
        }
    record = await store.create(
        name=name,
        secret=secret,
        action=action,
        action_params=action_params,
    )
    return {"success": True, "webhook": record}


@router.get("/api/custom-webhooks/list")
async def list_custom_webhooks():
    store = _resolve_store()
    if store is None:
        return {"webhooks": list(_webhooks.values())}
    rows = await store.list_all()
    return {"webhooks": rows}


@router.delete("/api/custom-webhooks/{webhook_id}")
async def delete_custom_webhook(webhook_id: str):
    store = _resolve_store()
    if store is None:
        if webhook_id in _webhooks:
            del _webhooks[webhook_id]
            return {"success": True}
        return {"success": False, "error": "Webhook not found"}
    deleted = await store.delete(webhook_id)
    return {"success": bool(deleted),
            "error": None if deleted else "Webhook not found"}


@router.post("/api/custom-webhooks/{webhook_id}/receive")
async def receive_custom_webhook(webhook_id: str, request: Request):
    return await _receive_impl(webhook_id, request)


# ─────────────────────────────────────────────────────────────────────
# Receive impl shared by both the canonical and legacy URLs.
# Signature verification is fail-closed when a secret is configured.
# ─────────────────────────────────────────────────────────────────────


async def _receive_impl(webhook_id: str, request: Request) -> Response:
    hook = await _lookup_hook(webhook_id)
    if hook is None:
        return Response(status_code=404, content="Webhook not found")

    body = await request.body()

    if hook.get("secret"):
        sig_header = (
            request.headers.get("x-hub-signature-256", "")
            or request.headers.get("stripe-signature", "")
            or request.headers.get("x-signature", "")
        )
        if not sig_header:
            # Fail-closed: a configured secret means we MUST see a
            # signature header. No silent accept.
            return Response(status_code=401, content="Missing signature")
        expected = "sha256=" + hmac.new(
            hook["secret"].encode(), body, hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            return Response(status_code=403, content="Invalid signature")

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"raw": body.decode(errors="replace")}

    store = _resolve_store()
    if store is not None:
        try:
            await store.record_trigger(webhook_id)
        except Exception as exc:
            logger.warning("custom-webhook trigger record failed: %s", exc)
    elif webhook_id in _webhooks:
        _webhooks[webhook_id]["last_triggered"] = time.time()
        _webhooks[webhook_id]["trigger_count"] = (
            _webhooks[webhook_id].get("trigger_count", 0) + 1
        )

    action = hook.get("action", "chat")
    if action == "chat" and getattr(state, "orchestrator", None):
        text = (
            (hook.get("action_params", {}) or {}).get(
                "prefix", "Webhook received: ",
            )
            + json.dumps(payload)[:2000]
        )
        for sid in list(state.sessions.keys())[:1]:
            await state.orchestrator.handle_command(
                sid, text,
                context={"source": "webhook", "webhook_id": webhook_id},
            )

    if hasattr(state, "event_bus") and state.event_bus:
        from integrations.webhook_receiver import WebhookEvent

        event = WebhookEvent(
            app_id=f"custom_{webhook_id}",
            event_type="webhook.received",
            payload=payload,
        )
        await state.event_bus.emit(event)

    return Response(content=b'{"ok":true}', media_type="application/json")


async def _lookup_hook(webhook_id: str) -> Optional[dict]:
    """Resolve a webhook record from the persistent store, falling back
    to the legacy in-memory dict for tests that haven't booted full
    state."""
    store = _resolve_store()
    if store is not None:
        rec = await store.get(webhook_id)
        if rec is not None:
            return rec
    return _legacy_record(webhook_id)


# ─────────────────────────────────────────────────────────────────────
# Legacy /api/webhooks/{id}/receive alias (DEPRECATED but kept so any
# external service the operator already pointed at the old URL keeps
# working). The collision with /api/webhooks/{app_id} (integration
# ingress) is what the move to /api/custom-webhooks/* fixes — this
# alias only catches the trailing /receive shape that was never
# ambiguous.
# ─────────────────────────────────────────────────────────────────────


@router.post("/api/webhooks/{webhook_id}/receive")
async def receive_legacy_alias(webhook_id: str, request: Request):
    return await _receive_impl(webhook_id, request)


# Note: legacy /api/webhooks/create + /api/webhooks/list cannot be
# resurrected as aliases on this router. The integration ingress at
# POST /api/webhooks/{app_id} matches the single-component pattern
# first (because brain_rest_router is mounted before webhooks_router
# in api/server.py), so any alias of that shape would still be
# swallowed. The canonical URLs are /api/custom-webhooks/* — that
# move IS the fix for the route-collision bug finding 19 calls out.
# Operators with stored URLs only need to flip the prefix; the
# /receive shape was never ambiguous and continues to resolve via
# the alias above.
