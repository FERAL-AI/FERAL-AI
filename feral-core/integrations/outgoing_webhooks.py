"""
Outgoing webhooks — POST internal events to operator-subscribed URLs.

Pre-Lane-10 the brain had inbound webhook ingress (GitHub, Stripe,
Home Assistant, custom hooks) but **no** outbound delivery surface:
finding 19 names this gap directly ("Outgoing webhooks: NONE"). An
operator who wanted to wire FERAL into Zapier, n8n, a private CI, an
ops alerting webhook etc. had no way to receive a "chat.completed"
ping when something happened inside the brain.

Lane 10 closes that gap with three pieces:

1. ``OutgoingWebhookStore`` — durable subscriber registry at
   ``~/.feral/outgoing_webhooks.db``. Each subscription has an id,
   name, target_url, optional secret, list of subscribed event_types
   ("*" for all), enabled flag, and per-subscription delivery counters.
2. ``OutgoingWebhookDeliverer`` — ``EventBus`` global handler that
   matches each emitted ``WebhookEvent`` against subscriptions and
   POSTs the structured payload to the target_url with an
   ``X-FERAL-Signature-256`` HMAC header (and ``X-FERAL-Event``,
   ``X-FERAL-Webhook-Id``, ``X-FERAL-Timestamp``). Retries with
   exponential backoff on 5xx / network errors (up to a configurable
   ``max_retries``). 4xx responses are non-retriable: the request was
   rejected by the operator's endpoint and retrying won't help.
3. ``api.routes.outgoing_webhooks`` — REST surface on
   ``/api/outgoing-webhooks/*`` so operators can subscribe / list /
   delete / test-fire.

The deliverer is a thin async wrapper around ``httpx.AsyncClient``;
delivery happens in the same event loop as the bus so we don't have
to babysit a worker process. Retries sleep with jittered exponential
backoff and never block the bus emit (the deliverer schedules a
task per subscription, fire-and-forget by design — operators
querying delivery state should hit ``GET /api/outgoing-webhooks``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

import aiosqlite
import httpx

logger = logging.getLogger("feral.integrations.outgoing_webhooks")


def _default_db_path() -> Path:
    from config.loader import feral_home

    return feral_home() / "outgoing_webhooks.db"


_INIT_SQL = """
CREATE TABLE IF NOT EXISTS outgoing_webhooks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    target_url      TEXT NOT NULL,
    secret          TEXT NOT NULL DEFAULT '',
    event_types     TEXT NOT NULL DEFAULT '["*"]',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    last_delivered  REAL,
    delivery_count  INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);
"""


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    try:
        events = json.loads(row[4]) if row[4] else ["*"]
    except json.JSONDecodeError:
        events = ["*"]
    return {
        "id": row[0],
        "name": row[1],
        "target_url": row[2],
        "secret": row[3] or "",
        "event_types": events,
        "enabled": bool(row[5]),
        "created_at": row[6],
        "last_delivered": row[7],
        "delivery_count": row[8] or 0,
        "failure_count": row[9] or 0,
        "last_error": row[10] or "",
    }


class OutgoingWebhookStore:
    """Durable subscriber registry for outgoing webhooks."""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path: Path = Path(db_path) if db_path else _default_db_path()
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                await db.executescript(_INIT_SQL)
                await db.commit()
            try:
                os.chmod(self._db_path, 0o600)
            except OSError:
                pass
            self._initialized = True

    async def create(
        self,
        *,
        name: str,
        target_url: str,
        secret: str = "",
        event_types: Optional[list[str]] = None,
        enabled: bool = True,
    ) -> dict:
        await self._ensure_init()
        webhook_id = str(uuid4())[:12]
        events_json = json.dumps(event_types or ["*"])
        created = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO outgoing_webhooks
                  (id, name, target_url, secret, event_types, enabled,
                   created_at, last_delivered, delivery_count,
                   failure_count, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, NULL)
                """,
                (webhook_id, name, target_url, secret or "",
                 events_json, 1 if enabled else 0, created),
            )
            await db.commit()
        logger.info("outgoing-webhook %s -> %s created (events=%s)",
                    webhook_id, target_url, event_types or ["*"])
        return await self.get(webhook_id)

    async def get(self, webhook_id: str) -> Optional[dict]:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT id, name, target_url, secret, event_types, enabled, "
                "created_at, last_delivered, delivery_count, "
                "failure_count, last_error "
                "FROM outgoing_webhooks WHERE id = ?",
                (webhook_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return _row_to_dict(row)

    async def list_all(self, *, enabled_only: bool = False) -> list[dict]:
        await self._ensure_init()
        sql = (
            "SELECT id, name, target_url, secret, event_types, enabled, "
            "created_at, last_delivered, delivery_count, failure_count, "
            "last_error FROM outgoing_webhooks "
        )
        if enabled_only:
            sql += "WHERE enabled = 1 "
        sql += "ORDER BY created_at ASC"
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(sql)
            rows = await cursor.fetchall()
            await cursor.close()
        return [_row_to_dict(r) for r in rows]

    async def delete(self, webhook_id: str) -> bool:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM outgoing_webhooks WHERE id = ?",
                (webhook_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def record_success(self, webhook_id: str) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE outgoing_webhooks
                SET last_delivered = ?,
                    delivery_count = delivery_count + 1,
                    last_error = NULL
                WHERE id = ?
                """,
                (time.time(), webhook_id),
            )
            await db.commit()

    async def record_failure(self, webhook_id: str, error: str) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE outgoing_webhooks
                SET failure_count = failure_count + 1,
                    last_error = ?
                WHERE id = ?
                """,
                (error[:500], webhook_id),
            )
            await db.commit()


def sign_payload(secret: str, body: bytes) -> str:
    """Compute the canonical X-FERAL-Signature-256 header value."""
    if not secret:
        return ""
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256,
    ).hexdigest()


def matches_event(subscription_events: list[str], event_type: str) -> bool:
    """A subscription matches an event when its ``event_types`` list
    contains ``"*"`` (wildcard) or the literal event type, or a
    namespace prefix like ``"chat.*"`` matching ``"chat.completed"``."""
    if not subscription_events:
        return True
    for pattern in subscription_events:
        if pattern == "*":
            return True
        if pattern == event_type:
            return True
        if pattern.endswith(".*") and event_type.startswith(pattern[:-1]):
            return True
    return False


class OutgoingWebhookDeliverer:
    """Bridges :class:`integrations.webhook_receiver.EventBus` events
    out to operator-subscribed URLs.

    Wire it once at boot::

        deliverer = OutgoingWebhookDeliverer(store=...)
        event_bus.on_all(deliverer.handle_event)

    Each emitted event triggers (a) a non-blocking match against the
    subscriber list and (b) one POST per matching subscription with
    HMAC-SHA256 signing + exponential backoff retries.

    Tests can pass an injected ``http_client`` to avoid real network
    traffic.
    """

    def __init__(
        self,
        store: OutgoingWebhookStore,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        max_retries: int = 3,
        base_backoff_seconds: float = 1.0,
        request_timeout_seconds: float = 10.0,
    ):
        self._store = store
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=request_timeout_seconds,
        )
        self._max_retries = max(0, int(max_retries))
        self._base_backoff = max(0.05, float(base_backoff_seconds))
        self._timeout_seconds = max(0.5, float(request_timeout_seconds))
        # Tasks we kicked off — retained so the operator can ``await``
        # them in the test surface, and so we can drain on close.
        self._pending: set[asyncio.Task] = set()

    async def close(self) -> None:
        for task in list(self._pending):
            if not task.done():
                task.cancel()
        if self._owns_client:
            await self._http.aclose()

    async def handle_event(self, event) -> None:
        """``EventBus.on_all`` handler — schedules deliveries without
        blocking the bus on slow operator endpoints."""
        try:
            subscriptions = await self._store.list_all(enabled_only=True)
        except Exception as exc:
            logger.warning("outgoing-webhooks: subscription list failed: %s",
                           exc)
            return
        for sub in subscriptions:
            if not matches_event(sub.get("event_types", ["*"]),
                                 event.event_type):
                continue
            task = asyncio.create_task(self._deliver(sub, event))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def deliver_now(self, subscription_id: str, event) -> bool:
        """Synchronous-feeling delivery call (await it). Used by the
        ``POST /api/outgoing-webhooks/{id}/test`` operator surface so
        the WebUI can show "delivered" / "failed" right away."""
        sub = await self._store.get(subscription_id)
        if sub is None:
            return False
        return await self._deliver(sub, event)

    async def _deliver(self, sub: dict, event) -> bool:
        webhook_id = sub["id"]
        secret = sub.get("secret", "") or ""
        target_url = sub["target_url"]
        envelope = {
            "id": str(uuid4()),
            "event_type": event.event_type,
            "app_id": getattr(event, "app_id", ""),
            "payload": getattr(event, "payload", {}),
            "timestamp": getattr(event, "timestamp", time.time()),
            "delivery_attempt": 1,
            "subscription_id": webhook_id,
        }

        last_error = ""
        for attempt in range(self._max_retries + 1):
            envelope["delivery_attempt"] = attempt + 1
            body = json.dumps(envelope, separators=(",", ":"),
                              sort_keys=True).encode()
            headers = {
                "Content-Type": "application/json",
                "X-FERAL-Event": event.event_type,
                "X-FERAL-Webhook-Id": webhook_id,
                "X-FERAL-Timestamp": str(int(envelope["timestamp"])),
                "X-FERAL-Delivery-Attempt": str(envelope["delivery_attempt"]),
            }
            if secret:
                headers["X-FERAL-Signature-256"] = sign_payload(secret, body)
            try:
                resp = await self._http.post(
                    target_url, content=body, headers=headers,
                    timeout=self._timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last_error = f"network_error: {exc}"
                logger.info(
                    "outgoing-webhook %s attempt %d to %s failed: %s",
                    webhook_id, attempt + 1, target_url, exc,
                )
                if attempt == self._max_retries:
                    break
                await self._sleep_backoff(attempt)
                continue

            status = resp.status_code
            if 200 <= status < 300:
                await self._store.record_success(webhook_id)
                logger.info(
                    "outgoing-webhook %s delivered (event=%s status=%d "
                    "attempt=%d)",
                    webhook_id, event.event_type, status,
                    envelope["delivery_attempt"],
                )
                return True
            if 400 <= status < 500:
                # 4xx is non-retriable: the operator's endpoint
                # rejected the payload; retrying won't help.
                last_error = f"http_{status}"
                logger.warning(
                    "outgoing-webhook %s rejected (status=%d body=%s)",
                    webhook_id, status, (resp.text or "")[:200],
                )
                break
            # 5xx — retry.
            last_error = f"http_{status}"
            if attempt == self._max_retries:
                break
            await self._sleep_backoff(attempt)

        await self._store.record_failure(webhook_id, last_error)
        logger.warning(
            "outgoing-webhook %s gave up after %d attempts: %s",
            webhook_id, self._max_retries + 1, last_error,
        )
        return False

    async def _sleep_backoff(self, attempt: int) -> None:
        # Exponential with full jitter — RFC-style: sleep in
        # [0, base * 2**attempt). Caps at 30s so a misconfigured
        # endpoint can't silently delay everything else for
        # arbitrarily long.
        upper = min(self._base_backoff * (2 ** attempt), 30.0)
        await asyncio.sleep(random.uniform(0, upper))
