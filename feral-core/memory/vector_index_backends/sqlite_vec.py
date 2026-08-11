"""Default async sqlite-vec backend for :class:`VectorIndexBackend`.

Talks to SQLite via ``aiosqlite`` — true async I/O on a dedicated thread
per connection so the asyncio event loop never blocks while a vector
upsert or search is in flight. Loads the ``sqlite-vec`` extension on
each connection when available; falls back to a no-op index when not
(``indexed=False``), and :class:`MemoryStore` then degrades to FTS5
keyword-only search.

No external dependency: ``sqlite-vec`` is optional at the SQLite layer,
not at the Python-package layer, so this backend is always loadable.
The ``indexed`` property reports whether the extension is actually
available on the host.

v2026.5.33 — Option C async rewrite. The previous sqlite-vec adapter
wrapped a sync :class:`memory.embeddings.VectorIndex` (now removed)
via ``asyncio.to_thread``. This implementation talks to ``aiosqlite``
directly.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterable, Optional

import aiosqlite
import numpy as np

from config.loader import feral_data_home
from memory.embeddings import vec_to_blob

logger = logging.getLogger("feral.memory.vector_index_backends.sqlite_vec")


_SQLITE_VEC_AVAILABLE: Optional[bool] = None


def _sqlite_vec_module():
    """Return the ``sqlite_vec`` module if importable, else ``None``.

    Cached implicitly by Python's import system; we don't need a manual
    cache.
    """
    try:
        import sqlite_vec  # type: ignore
        return sqlite_vec
    except ImportError:
        return None


def sqlite_vec_available() -> bool:
    """Probe whether ``sqlite-vec`` is loadable on this host. Cached so
    boot-time probing happens once per process.

    The probe is synchronous because it runs at adapter construction
    time (boot), not on any hot path. It opens a throwaway in-memory
    connection so it can't corrupt the persistent DB even if the
    extension loader misbehaves.
    """
    global _SQLITE_VEC_AVAILABLE
    if _SQLITE_VEC_AVAILABLE is not None:
        return _SQLITE_VEC_AVAILABLE
    mod = _sqlite_vec_module()
    if mod is None:
        logger.info(
            "sqlite-vec not installed, so this backend stays a no-op and the "
            "vector leg is answered over numpy, which is correct and measured "
            "faster (see memory.embeddings.cosine_similarity_bulk)"
        )
        _SQLITE_VEC_AVAILABLE = False
        return False
    try:
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        mod.load(conn)
        conn.close()
        _SQLITE_VEC_AVAILABLE = True
        logger.info("sqlite-vec available — vec0 virtual table will be used for vector search")
        return True
    except Exception as exc:
        logger.info(
            "sqlite-vec load failed (%s), so this backend stays a no-op and the "
            "vector leg is answered over numpy, which is correct and measured "
            "faster (see memory.embeddings.cosine_similarity_bulk)",
            exc,
        )
        _SQLITE_VEC_AVAILABLE = False
        return False


async def _open_conn(db_path: str) -> aiosqlite.Connection:
    """Open an aiosqlite connection with WAL + sqlite-vec extension
    loaded if available. Returns a ready-to-use connection that the
    caller must ``await conn.close()`` on.

    The extension load uses aiosqlite's public ``enable_load_extension``
    + ``load_extension`` methods (available since 0.18) — every call
    runs on aiosqlite's dedicated worker thread for this connection,
    so the event loop stays unblocked.
    """
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    if sqlite_vec_available():
        mod = _sqlite_vec_module()
        if mod is not None:
            try:
                await conn.enable_load_extension(True)
                await conn.load_extension(mod.loadable_path())
            except Exception as exc:
                logger.debug("sqlite-vec extension load on async conn failed: %s", exc)
    return conn


class SQLiteVecIndex:
    """sqlite-vec-backed vector index satisfying
    :class:`VectorIndexBackend`. Async-native via ``aiosqlite``.

    Connection lifecycle: each public method opens its own short-lived
    connection (matches the pattern :class:`MemoryStore` uses). This
    avoids holding a single long-lived connection across awaits, which
    would serialise the backend through aiosqlite's per-connection
    worker thread. The cost is a handful of sqlite-vec extension loads
    per call; SQLite caches the parsed extension after the first load
    so the overhead is negligible after warmup.
    """

    backend_id: str = "sqlite_vec"

    def __init__(self, *, dim: int, db_path: Optional[str] = None,
                 table_name: str = "vec_chunks") -> None:
        if db_path is None:
            data_dir = feral_data_home()
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(data_dir / "memory.db")
        self.dim = dim
        self.db_path = db_path
        self._table = table_name
        # Probe sqlite-vec availability up front so callers see a
        # truthful `indexed` flag before they issue queries. The probe
        # is sync (boot-time) by design.
        self._use_vec = sqlite_vec_available()
        if self._use_vec:
            # CREATE VIRTUAL TABLE has to happen on a real connection;
            # we use a sync sqlite3 + extension load so __init__ stays
            # sync. The downside (sync I/O at boot) is acceptable —
            # this fires once per MemoryStore lifetime.
            try:
                conn = sqlite3.connect(self.db_path)
                conn.enable_load_extension(True)
                mod = _sqlite_vec_module()
                if mod is not None:
                    mod.load(conn)
                conn.execute(
                    f"""CREATE VIRTUAL TABLE IF NOT EXISTS {self._table}
                        USING vec0(
                            chunk_id TEXT PRIMARY KEY,
                            embedding FLOAT[{self.dim}]
                        )"""
                )
                conn.commit()
                conn.close()
            except Exception as exc:
                logger.warning("vec0 table creation failed (%s) — degrading to no-op index", exc)
                self._use_vec = False

    @property
    def indexed(self) -> bool:
        return self._use_vec

    async def count(self) -> int:
        if not self._use_vec:
            return 0
        try:
            conn = await _open_conn(self.db_path)
            try:
                async with conn.execute(f"SELECT COUNT(*) FROM {self._table}") as cur:
                    row = await cur.fetchone()
                    return int(row[0]) if row else 0
            finally:
                await conn.close()
        except Exception as exc:
            logger.debug("sqlite_vec count() failed: %s", exc)
            return 0

    async def upsert(self, chunk_id: str, embedding: np.ndarray) -> None:
        if not self._use_vec:
            return
        try:
            blob = vec_to_blob(embedding)
            conn = await _open_conn(self.db_path)
            try:
                await conn.execute(
                    f"INSERT OR REPLACE INTO {self._table}(chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, blob),
                )
                await conn.commit()
            finally:
                await conn.close()
        except Exception as exc:
            logger.debug("sqlite_vec upsert(%r) failed: %s", chunk_id, exc)

    async def upsert_batch(self, items: Iterable[tuple[str, np.ndarray]]) -> None:
        if not self._use_vec:
            return
        rows = [(cid, vec_to_blob(vec)) for cid, vec in items]
        if not rows:
            return
        try:
            conn = await _open_conn(self.db_path)
            try:
                await conn.executemany(
                    f"INSERT OR REPLACE INTO {self._table}(chunk_id, embedding) VALUES (?, ?)",
                    rows,
                )
                await conn.commit()
            finally:
                await conn.close()
        except Exception as exc:
            logger.debug("sqlite_vec upsert_batch failed: %s", exc)

    async def delete(self, chunk_id: str) -> None:
        if not self._use_vec:
            return
        try:
            conn = await _open_conn(self.db_path)
            try:
                await conn.execute(
                    f"DELETE FROM {self._table} WHERE chunk_id = ?", (chunk_id,)
                )
                await conn.commit()
            finally:
                await conn.close()
        except Exception as exc:
            logger.debug("sqlite_vec delete(%r) failed: %s", chunk_id, exc)

    async def search(
        self, query_vec: np.ndarray, limit: int = 20
    ) -> list[tuple[str, float]]:
        if not self._use_vec:
            return []
        try:
            blob = vec_to_blob(query_vec)
            conn = await _open_conn(self.db_path)
            try:
                async with conn.execute(
                    f"""SELECT chunk_id, distance
                        FROM {self._table}
                        WHERE embedding MATCH ?
                        ORDER BY distance
                        LIMIT ?""",
                    (blob, max(1, int(limit))),
                ) as cur:
                    rows = await cur.fetchall()
                    return [(r["chunk_id"], float(r["distance"])) for r in rows]
            finally:
                await conn.close()
        except Exception as exc:
            logger.debug("sqlite_vec search failed: %s", exc)
            return []

    async def search_cosine(
        self, query_vec: np.ndarray, limit: int = 20
    ) -> list[tuple[str, float]]:
        return [(cid, 1.0 - dist) for cid, dist in await self.search(query_vec, limit)]

    async def close(self) -> None:
        # No persistent handle to release — connections are short-lived
        # per call.
        return None


def create(*, dim: int, db_path: Optional[str] = None,
           table_name: str = "vec_chunks", **_: Any) -> SQLiteVecIndex:
    """Factory the registry calls via :func:`load_vector_index`."""
    return SQLiteVecIndex(dim=dim, db_path=db_path, table_name=table_name)
