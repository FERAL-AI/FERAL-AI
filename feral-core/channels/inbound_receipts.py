"""Single-writer inbound receipts; uncertain execution never auto-replays."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path


class InboundReceiptConflict(ValueError):
    pass


class InboundReceipts:
    def __init__(self, path: Path, *, capacity: int = 5000):
        self._lock = threading.Lock()
        self._capacity = capacity
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise InboundReceiptConflict("Inbound receipt path is not an owned regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=2)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS inbound_receipts (
            scope TEXT NOT NULL, event_id TEXT NOT NULL, digest TEXT NOT NULL,
            state TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(scope, event_id))""")
        self._db.commit()

    def claim(self, scope: str, event_id: str, terms: dict) -> str:
        digest = hashlib.sha256(
            json.dumps(
                terms,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        with self._lock, self._db:
            # BEGIN IMMEDIATE prevents two independent connections from
            # admitting the same event or exceeding the persistent cap.
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT digest, state FROM inbound_receipts WHERE scope=? AND event_id=?",
                (scope, event_id),
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise InboundReceiptConflict(
                        "Inbound event identity was reused with different content"
                    )
                return row[1]
            count = self._db.execute(
                "SELECT count(*) FROM inbound_receipts"
            ).fetchone()[0]
            if count >= self._capacity:
                raise InboundReceiptConflict(
                    "Inbound receipt capacity requires explicit retention maintenance"
                )
            self._db.execute(
                "INSERT INTO inbound_receipts VALUES (?, ?, ?, 'claimed', ?)",
                (scope, event_id, digest, time.time()),
            )
            return "new"

    def settle(self, scope: str, event_id: str, state: str) -> None:
        if state not in {"processed", "reply_unverified", "outcome_unknown"}:
            raise ValueError("Invalid inbound outcome")
        with self._lock, self._db:
            cursor = self._db.execute(
                """UPDATE inbound_receipts SET state=?, updated_at=?
                WHERE scope=? AND event_id=? AND state='claimed'""",
                (state, time.time(), scope, event_id),
            )
            if cursor.rowcount != 1:
                raise InboundReceiptConflict("Inbound claim could not be settled")

    def close(self) -> None:
        with self._lock:
            self._db.close()
