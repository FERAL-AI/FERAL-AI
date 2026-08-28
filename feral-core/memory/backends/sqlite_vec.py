"""sqlite-vec + numpy-fallback memory backend (default).

Wraps :class:`memory.embeddings.VectorIndex` (which owns the vec0 table
when the sqlite-vec extension is installed, and a numpy brute-force
scan otherwise) behind the pluggable :class:`MemoryBackend` Protocol.

Records store: ``id`` (primary key), ``text``, and ``metadata`` (JSON
blob) in a companion table alongside the vec0 vectors. Search joins the
two so callers get the full :class:`MemoryRecord` back in one round-trip.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from config.loader import feral_data_home
from memory.embeddings import (
    VectorIndex,
    cosine_similarity_bulk,
    vec_to_blob,
)

from .base import MemoryBackend, MemoryRecord

logger = logging.getLogger("feral.memory.backends.sqlite_vec")

_METADATA_DDL = """
CREATE TABLE IF NOT EXISTS feral_memory_records (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    embedding BLOB,
    updated_at REAL NOT NULL
)
"""


class SQLiteVecBackend:
    """Default memory backend shipped with every FERAL install."""

    backend_id: str = "sqlite_vec"

    def __init__(self, *, dim: int, db_path: Optional[str] = None) -> None:
        self.dim = dim
        data_home = feral_data_home()
        data_home.mkdir(parents=True, exist_ok=True)
        self._db_path = str(Path(db_path) if db_path else data_home / "memory.db")
        self._vec = VectorIndex(db_path=self._db_path, dimension=dim, table_name="vec_index")
        self._init_metadata_table()

    # ─── Setup ────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_metadata_table(self) -> None:
        conn = self._conn()
        try:
            conn.execute(_METADATA_DDL)
            conn.commit()
        finally:
            conn.close()

    # ─── MemoryBackend surface ────────────────────────────

    async def upsert(self, records: Iterable[MemoryRecord]) -> None:
        batch = list(records)
        if not batch:
            return
        await asyncio.to_thread(self._upsert_sync, batch)

    def _upsert_sync(self, batch: list[MemoryRecord]) -> None:
        now = asyncio.get_event_loop_policy().get_event_loop().time() if False else 0.0
        # `asyncio.get_event_loop_policy()...time()` is only meaningful inside
        # a running loop; plain wall clock is fine here and simpler to mock.
        import time
        now = time.time()

        conn = self._conn()
        try:
            rows: list[tuple[str, str, str, bytes, float]] = []
            vec_items: list[tuple[str, np.ndarray]] = []
            for rec in batch:
                if rec.embedding is None:
                    logger.warning("sqlite_vec backend skipping record %s: no embedding", rec.id)
                    continue
                if len(rec.embedding) != self.dim:
                    raise ValueError(
                        f"embedding dim mismatch for record {rec.id}: "
                        f"expected {self.dim}, got {len(rec.embedding)}"
                    )
                arr = np.asarray(rec.embedding, dtype=np.float32)
                blob = vec_to_blob(arr)
                rows.append((rec.id, rec.text, json.dumps(rec.metadata), blob, now))
                vec_items.append((rec.id, arr))

            conn.executemany(
                "INSERT OR REPLACE INTO feral_memory_records "
                "(id, text, metadata, embedding, updated_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            if vec_items:
                self._vec.upsert_batch(vec_items)
        finally:
            conn.close()

    async def search(
        self,
        query_vec: list[float],
        *,
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
    ) -> list[MemoryRecord]:
        if len(query_vec) != self.dim:
            raise ValueError(
                f"query vector dim {len(query_vec)} != backend dim {self.dim}"
            )
        return await asyncio.to_thread(self._search_sync, query_vec, limit, filter or {})

    def _search_sync(
        self, query_vec: list[float], limit: int, filter: dict[str, Any]
    ) -> list[MemoryRecord]:
        q_arr = np.asarray(query_vec, dtype=np.float32)

        # sqlite-vec fast path.
        #
        # NOTE ON ``MemoryRecord.score``: this leg's score is
        # ``1 - L2`` (the vec0 table has no ``distance_metric``, see
        # ``memory/embeddings.VectorIndex.search_similarity``) while the
        # numpy fallback below scores a true cosine. Both orderings are
        # the same and nothing in this repo thresholds ``score``, but a
        # consumer that does must not assume a cosine, and must not
        # assume the two hosts agree.
        hits = self._vec.search_similarity(q_arr, limit=max(limit * 2, limit + 5))
        if hits:
            return self._hydrate(hits, filter, limit)

        # numpy brute-force fallback: stream rows, compute cosine in Python.
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, text, metadata, embedding FROM feral_memory_records"
            ).fetchall()
        finally:
            conn.close()

        # Width-filter first, then score the survivors in ONE vectorized
        # pass. The filter is not an optimisation, it preserves this
        # path's existing contract: a row of the wrong dimension is
        # SKIPPED here (a table can hold vectors from an earlier
        # provider), whereas cosine_similarity_bulk raises on a mismatch
        # because its callers want that to be loud.
        #
        # Measured on this machine, 384-dim: the per-row loop this
        # replaces cost 273ms at 100k rows against 23ms vectorized
        # (11.7x), and this is the live path whenever the sqlite-vec
        # extension cannot load, which is the default on any interpreter
        # built without loadable SQLite extension support.
        expected_bytes = self.dim * 4
        usable = [
            (row["id"], row["embedding"])
            for row in rows
            if row["embedding"] is not None
            and len(row["embedding"]) == expected_bytes
        ]
        scored: list[tuple[str, float]] = []
        if usable:
            sims = cosine_similarity_bulk(q_arr, [blob for _, blob in usable])
            scored = [
                (cid, float(sim)) for (cid, _), sim in zip(usable, sims)
            ]
        scored.sort(key=lambda t: t[1], reverse=True)
        return self._hydrate(scored, filter, limit)

    def _hydrate(
        self, hits: list[tuple[str, float]], filter: dict[str, Any], limit: int
    ) -> list[MemoryRecord]:
        if not hits:
            return []
        ids = [cid for cid, _ in hits]
        placeholders = ",".join("?" for _ in ids)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"SELECT id, text, metadata FROM feral_memory_records WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        finally:
            conn.close()

        by_id = {r["id"]: r for r in rows}
        out: list[MemoryRecord] = []
        for cid, score in hits:
            row = by_id.get(cid)
            if row is None:
                continue
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except json.JSONDecodeError:
                metadata = {}
            if filter and not _metadata_matches(metadata, filter):
                continue
            out.append(
                MemoryRecord(
                    id=cid,
                    text=row["text"],
                    metadata=metadata,
                    score=score,
                )
            )
            if len(out) >= limit:
                break
        return out

    async def delete(self, ids: Iterable[str]) -> None:
        batch = list(ids)
        if not batch:
            return
        await asyncio.to_thread(self._delete_sync, batch)

    def _delete_sync(self, batch: list[str]) -> None:
        placeholders = ",".join("?" for _ in batch)
        conn = self._conn()
        try:
            conn.execute(
                f"DELETE FROM feral_memory_records WHERE id IN ({placeholders})", batch
            )
            conn.commit()
        finally:
            conn.close()
        for cid in batch:
            self._vec.delete(cid)

    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, Any]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM feral_memory_records"
            ).fetchone()
            count = int(row["c"]) if row else 0
        finally:
            conn.close()
        return {
            "backend": self.backend_id,
            "count": count,
            "dim": self.dim,
            "vec_index_mode": "sqlite-vec" if self._vec.indexed else "numpy_fallback",
            "db_path": self._db_path,
        }

    async def close(self) -> None:
        # sqlite connections are opened per-call; nothing to release.
        return None


def _metadata_matches(metadata: dict[str, Any], filter: dict[str, Any]) -> bool:
    """Simple exact-match metadata filter (``{"tag": "foo"}``).

    Backends with richer filter DSLs (Chroma, Qdrant) implement their own.
    """
    for key, expected in filter.items():
        if metadata.get(key) != expected:
            return False
    return True


# ─────────────────────────────────────────────────────────────
# Factory used by the backend loader in ``base.py``.
# ─────────────────────────────────────────────────────────────


async def create(*, dim: int, db_path: Optional[str] = None, **_: Any) -> MemoryBackend:
    return SQLiteVecBackend(dim=dim, db_path=db_path)
