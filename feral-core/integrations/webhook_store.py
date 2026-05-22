"""
Persistent custom-webhook registry — single source of truth.

Pre-Lane-10 the custom-webhooks router (api/routes/webhooks.py) used
an in-process ``dict`` so every brain restart wiped the operator's
configured webhooks. Finding 19 calls this out: "Custom operator
webhooks: persistent? **No** — module dict ``_webhooks``."

This module is the durable replacement, shared by both the create/list
HTTP routes and the receive route. We use ``aiosqlite`` (already a
project dependency for memory + cost) so we don't block the event loop
on disk writes.

Schema::

    CREATE TABLE custom_webhooks (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        secret        TEXT NOT NULL DEFAULT '',
        action        TEXT NOT NULL DEFAULT 'chat',
        action_params TEXT NOT NULL DEFAULT '{}',  -- JSON
        created_at    REAL NOT NULL,
        last_triggered REAL,
        trigger_count INTEGER NOT NULL DEFAULT 0,
        url           TEXT NOT NULL DEFAULT ''
    );

The store is exposed as :class:`WebhookStore` with ``create``,
``list_all``, ``get``, ``delete``, ``record_trigger`` async methods.
The HTTP routes hold a singleton instance (resolved through
``api.state``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

import aiosqlite

logger = logging.getLogger("feral.integrations.webhook_store")


def _default_db_path() -> Path:
    from config.loader import feral_home

    return feral_home() / "webhooks.db"


_INIT_SQL = """
CREATE TABLE IF NOT EXISTS custom_webhooks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    secret          TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL DEFAULT 'chat',
    action_params   TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL,
    last_triggered  REAL,
    trigger_count   INTEGER NOT NULL DEFAULT 0,
    url             TEXT NOT NULL DEFAULT ''
);
"""


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    try:
        params = json.loads(row[4]) if row[4] else {}
    except json.JSONDecodeError:
        params = {}
    return {
        "id": row[0],
        "name": row[1],
        "secret": row[2] or "",
        "action": row[3] or "chat",
        "action_params": params,
        "created_at": row[5],
        "last_triggered": row[6],
        "trigger_count": row[7] or 0,
        "url": row[8] or f"/api/custom-webhooks/{row[0]}/receive",
    }


class WebhookStore:
    """Async sqlite-backed registry of operator-configured webhooks.

    The store does NOT verify signatures or dispatch — that's the
    receive route's job. We only own durable storage + lookups so
    webhooks survive a restart and operators don't have to reconfigure
    after every deploy.
    """

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
        secret: str = "",
        action: str = "chat",
        action_params: Optional[dict] = None,
    ) -> dict:
        await self._ensure_init()
        webhook_id = str(uuid4())[:12]
        params_json = json.dumps(action_params or {})
        url = f"/api/custom-webhooks/{webhook_id}/receive"
        created = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO custom_webhooks
                  (id, name, secret, action, action_params, created_at,
                   last_triggered, trigger_count, url)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?)
                """,
                (webhook_id, name, secret or "", action or "chat",
                 params_json, created, url),
            )
            await db.commit()
        logger.info("custom-webhook %s created (action=%s)", webhook_id, action)
        return {
            "id": webhook_id,
            "name": name,
            "secret": secret or "",
            "action": action or "chat",
            "action_params": action_params or {},
            "created_at": created,
            "last_triggered": None,
            "trigger_count": 0,
            "url": url,
        }

    async def get(self, webhook_id: str) -> Optional[dict]:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT id, name, secret, action, action_params, created_at, "
                "last_triggered, trigger_count, url "
                "FROM custom_webhooks WHERE id = ?",
                (webhook_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return _row_to_dict(row)

    async def list_all(self) -> list[dict]:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT id, name, secret, action, action_params, created_at, "
                "last_triggered, trigger_count, url "
                "FROM custom_webhooks ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_row_to_dict(r) for r in rows]

    async def delete(self, webhook_id: str) -> bool:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM custom_webhooks WHERE id = ?",
                (webhook_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def record_trigger(self, webhook_id: str) -> None:
        """Bump the trigger_count and last_triggered timestamp.

        Called by the receive route after a verified delivery — never
        on a 401/403 so the metric stays honest.
        """
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE custom_webhooks
                SET last_triggered = ?,
                    trigger_count = trigger_count + 1
                WHERE id = ?
                """,
                (time.time(), webhook_id),
            )
            await db.commit()

    async def close(self) -> None:
        """No-op for now — we open per-call connections so there's
        nothing to release. Kept for symmetry with other stateful
        services so future pooling won't break callers."""
        return None
