"""
FERAL Embedding Engine
========================
Provides vector embeddings for semantic memory search.

Vector Index Strategy:
  1. Try sqlite-vec extension → vec0 virtual table with vec_distance_cosine
  2. Fall back to numpy brute-force scan (degraded, still works)

Embedding Providers:
  1. OpenAI text-embedding-3-small (1536d) — preferred when OPENAI_API_KEY is set
  2. Local sentence-transformers all-MiniLM-L6-v2 (384d) — preferred when no key
  3. Hash fallback (no semantic similarity — degraded development/runtime fallback)

Runtime degrade & fallback
--------------------------
When the primary provider returns persistent quota / auth errors (e.g. HTTP 429
``insufficient_quota``, 401, 403 with an invalid-key body), :class:`EmbeddingProvider`
degrades the primary for a cooldown window and routes subsequent embeddings through
the configured fallback. Exactly ONE structured warning is emitted per cooldown
window per failure reason — repeat events are counted and folded into the next
window's warning so logs do not get flooded.

The :class:`EmbedQueue` cooperates with this: when the provider is degraded it
performs a single attempt (instead of three retries) and always persists the
chunk text to ``memory_chunks`` so FTS5 keyword search keeps working even when
the vector cannot be produced.

Configuration
-------------
``FERAL_EMBED_FALLBACK`` — fallback strategy when the primary is degraded.

  * ``hash`` (default) — deterministic SHA-256 hash projected to the primary's
    dimension. Keeps the vec0 / numpy index operational; semantic similarity
    quality drops to lexical-only but ranking does not break.
  * ``local`` — try to load sentence-transformers all-MiniLM-L6-v2. Only used
    when its 384-dim output matches the primary's dimension; otherwise falls
    back to ``hash`` automatically.
  * ``skip`` — raise :class:`EmbeddingSkipped`; the queue persists the chunk
    text without an embedding so FTS still indexes it.

``FERAL_EMBED_RATE_LIMIT_THRESHOLD`` — consecutive HTTP 429 rate-limit errors
before flipping primary into a 60s cooldown (default ``3``). ``insufficient_quota``
and hard auth errors flip on the FIRST event regardless of threshold.

``FERAL_EMBED_DEGRADE_LOG_INTERVAL_S`` — minimum seconds between repeat
structured warnings for the same condition (default ``300``).

``FERAL_EMBED_QUEUE_LOG_INTERVAL_S`` — minimum seconds between repeat queue
warnings (persist failures, skipped chunks). Default ``300``.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import struct
import time
from typing import Any, Optional

import aiosqlite
import numpy as np

logger = logging.getLogger("feral.memory.embeddings")

OPENAI_DIM = 1536
LOCAL_DIM = 384
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80


def _tokenize_rough(text: str) -> list[str]:
    return text.split()


def chunk_text(text: str, max_tokens: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks of approximately max_tokens words."""
    words = _tokenize_rough(text)
    if len(words) <= max_tokens:
        return [text]
    chunks = []
    start = 0
    while start < len(words):
        end = start + max_tokens
        chunks.append(" ".join(words[start:end]))
        start += max_tokens - overlap
    return chunks


def vec_to_blob(vec: list[float] | np.ndarray) -> bytes:
    arr = np.asarray(vec, dtype=np.float32)
    return arr.tobytes()


def blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ─────────────────────────────────────────────
# sqlite-vec integration
# ─────────────────────────────────────────────

_SQLITE_VEC_AVAILABLE: Optional[bool] = None


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension. Cache the result."""
    global _SQLITE_VEC_AVAILABLE
    if _SQLITE_VEC_AVAILABLE is False:
        return False
    if _SQLITE_VEC_AVAILABLE is True:
        try:
            conn.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(conn)
            return True
        except Exception:
            _SQLITE_VEC_AVAILABLE = False
            return False

    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        _SQLITE_VEC_AVAILABLE = True
        logger.info("sqlite-vec loaded — using vec0 virtual table for vector search")
        return True
    except ImportError:
        logger.info("sqlite-vec not installed — using numpy fallback for vector search")
        _SQLITE_VEC_AVAILABLE = False
        return False
    except Exception as e:
        logger.info(f"sqlite-vec load failed ({e}) — using numpy fallback")
        _SQLITE_VEC_AVAILABLE = False
        return False


def sqlite_vec_available() -> bool:
    """Check if sqlite-vec can be loaded."""
    global _SQLITE_VEC_AVAILABLE
    if _SQLITE_VEC_AVAILABLE is not None:
        return _SQLITE_VEC_AVAILABLE
    try:
        conn = sqlite3.connect(":memory:")
        result = _try_load_sqlite_vec(conn)
        conn.close()
        return result
    except Exception:
        _SQLITE_VEC_AVAILABLE = False
        return False


class VectorIndex:
    """
    Indexed vector search using sqlite-vec (vec0 virtual table)
    with automatic fallback to brute-force numpy scan.
    """

    def __init__(self, db_path: str, dimension: int, table_name: str = "vec_index"):
        self._db_path = db_path
        self._dim = dimension
        self._table_name = table_name
        self._use_vec = False
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        if self._use_vec:
            _try_load_sqlite_vec(conn)
        return conn

    def _init(self):
        self._use_vec = sqlite_vec_available()
        conn = self._conn()
        if self._use_vec:
            try:
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {self._table_name}
                    USING vec0(
                        chunk_id TEXT PRIMARY KEY,
                        embedding FLOAT[{self._dim}]
                    )
                """)
                conn.commit()
                logger.info(f"vec0 table '{self._table_name}' ready (dim={self._dim})")
            except Exception as e:
                logger.warning(f"vec0 creation failed: {e} — falling back to numpy")
                self._use_vec = False
        conn.close()

    @property
    def indexed(self) -> bool:
        return self._use_vec

    def upsert(self, chunk_id: str, embedding: np.ndarray):
        """Insert or update a vector in the index."""
        if not self._use_vec:
            return
        conn = self._conn()
        try:
            blob = vec_to_blob(embedding)
            conn.execute(
                f"INSERT OR REPLACE INTO {self._table_name}(chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, blob),
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"vec0 upsert failed: {e}")
        finally:
            conn.close()

    def upsert_batch(self, items: list[tuple[str, np.ndarray]]):
        if not self._use_vec or not items:
            return
        conn = self._conn()
        try:
            conn.executemany(
                f"INSERT OR REPLACE INTO {self._table_name}(chunk_id, embedding) VALUES (?, ?)",
                [(cid, vec_to_blob(vec)) for cid, vec in items],
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"vec0 batch upsert failed: {e}")
        finally:
            conn.close()

    def delete(self, chunk_id: str):
        if not self._use_vec:
            return
        conn = self._conn()
        try:
            conn.execute(f"DELETE FROM {self._table_name} WHERE chunk_id = ?", (chunk_id,))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    def search(self, query_vec: np.ndarray, limit: int = 20) -> list[tuple[str, float]]:
        """
        Search for nearest vectors. Returns [(chunk_id, distance), ...].
        Uses vec_distance_cosine when sqlite-vec is available.
        """
        if not self._use_vec:
            return []
        conn = self._conn()
        try:
            blob = vec_to_blob(query_vec)
            rows = conn.execute(f"""
                SELECT chunk_id, distance
                FROM {self._table_name}
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
            """, (blob, limit)).fetchall()
            return [(r["chunk_id"], float(r["distance"])) for r in rows]
        except Exception as e:
            logger.debug(f"vec0 search failed: {e}")
            return []
        finally:
            conn.close()

    def search_cosine(self, query_vec: np.ndarray, limit: int = 20) -> list[tuple[str, float]]:
        """
        Search returning cosine similarity (1.0 = identical).
        vec_distance_cosine returns distance (0 = identical), so we convert.
        """
        results = self.search(query_vec, limit)
        return [(cid, 1.0 - dist) for cid, dist in results]

    @property
    def count(self) -> int:
        if not self._use_vec:
            return 0
        conn = self._conn()
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {self._table_name}").fetchone()
            return row[0] if row else 0
        except Exception:
            return 0
        finally:
            conn.close()


# ─────────────────────────────────────────────
# Anti-spam log throttling + degrade signaling
# ─────────────────────────────────────────────


class EmbeddingSkipped(Exception):
    """Raised by EmbeddingProvider when fallback mode is ``skip`` and the primary
    provider is degraded.

    EmbedQueue catches this and persists the chunk text without an embedding so
    FTS5 keyword indexing still works for that content.
    """


class _LogThrottle:
    """Per-key warning suppressor.

    Tracks the last log time per key and the count of suppressed events between
    logs. Keeps log files clean during long degrade windows where the same
    condition would otherwise be reported every queue cycle.
    """

    def __init__(self, interval_seconds: float = 300.0):
        self._interval = max(0.0, float(interval_seconds))
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def should_log(self, key: str) -> tuple[bool, int]:
        """Return (allow_log, suppressed_count_since_last_log).

        On allow=True the suppression counter for ``key`` is reset to 0.
        On allow=False the suppression counter is incremented.
        """
        now = time.monotonic()
        last = self._last.get(key)
        if last is None or (now - last) >= self._interval:
            count = self._suppressed.pop(key, 0)
            self._last[key] = now
            return True, count
        self._suppressed[key] = self._suppressed.get(key, 0) + 1
        return False, 0

    def reset(self) -> None:
        self._last.clear()
        self._suppressed.clear()


# ─────────────────────────────────────────────
# Embedding queue for reliable async embedding
# ─────────────────────────────────────────────

class EmbedQueue:
    """
    Reliable async embedding queue. On transient failures the embed call is
    retried with linear backoff. On a degraded primary (see
    :class:`EmbeddingProvider`), the queue makes a single attempt instead so
    cycles don't pile up against a known-broken upstream.

    The chunk text is persisted to ``memory_chunks`` regardless of embedding
    outcome so FTS5 keyword search keeps working even when the vector cannot
    be produced. ``embedding`` is left NULL if the call was skipped or
    persistently failed.

    Warnings are routed through :class:`_LogThrottle` so a long degrade window
    produces ONE warning per ``FERAL_EMBED_QUEUE_LOG_INTERVAL_S`` seconds per
    condition, not one per cycle.
    """

    def __init__(
        self,
        embedder: "EmbeddingProvider",
        vector_index: Optional[Any] = None,  # VectorIndexBackend Protocol
    ):
        """Construct the embed queue.

        ``vector_index`` may be any object exposing the
        ``upsert(chunk_id, embedding)`` signature (the
        :class:`memory.vector_index_backends.VectorIndexBackend`
        Protocol). The legacy :class:`VectorIndex` satisfies it; the
        sync Chroma / Qdrant adapters added in audit-r12 D4 do too. The
        queue is intentionally permissive on the type so the selector
        can swap backends without touching this module."""
        self._embedder = embedder
        self._vector_index = vector_index
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        try:
            interval = float(os.getenv("FERAL_EMBED_QUEUE_LOG_INTERVAL_S", "300"))
        except ValueError:
            interval = 300.0
        self._log_throttle = _LogThrottle(interval)
        self._stats: dict[str, int] = {
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._process_loop())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    def enqueue(self, chunk_id: str, text: str, source_table: str, source_id: str,
                chunk_index: int, db_path: str):
        try:
            self._queue.put_nowait({
                "chunk_id": chunk_id, "text": text, "source_table": source_table,
                "source_id": source_id, "chunk_index": chunk_index, "db_path": db_path,
            })
        except asyncio.QueueFull:
            should_log, suppressed = self._log_throttle.should_log("queue_full")
            if should_log:
                logger.warning(
                    "embed_queue_full dropping chunk_id=%s suppressed_since_last_log=%d",
                    chunk_id, suppressed,
                )

    async def _process_loop(self):
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            try:
                await self._handle_item(item)
            except Exception as exc:
                # Defensive: never let a single bad item crash the worker.
                should_log, suppressed = self._log_throttle.should_log(
                    f"loop_unexpected:{type(exc).__name__}"
                )
                if should_log:
                    logger.warning(
                        "embed_queue_unexpected_error chunk_id=%s error=%r "
                        "suppressed_since_last_log=%d",
                        item.get("chunk_id"), exc, suppressed,
                    )

    async def _handle_item(self, item: dict) -> None:
        retries = 3
        vec: Optional[np.ndarray] = None
        skipped = False
        last_error: Optional[Exception] = None

        # When the provider is in a known-degraded state, do not loop with
        # backoff — the fallback path returns synchronously and one attempt
        # is enough. This is the core anti-spam fix: persistent 429s no
        # longer produce 3-attempt + sleep cycles per chunk.
        provider_degraded = bool(getattr(self._embedder, "degraded", False))
        if provider_degraded:
            try:
                vec = await self._embedder.embed(item["text"])
            except EmbeddingSkipped:
                skipped = True
            except Exception as exc:
                last_error = exc
        else:
            for attempt in range(retries):
                try:
                    vec = await self._embedder.embed(item["text"])
                    last_error = None
                    break
                except EmbeddingSkipped:
                    skipped = True
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt == retries - 1:
                        break
                    await asyncio.sleep(1.0 * (attempt + 1))

        blob = vec_to_blob(vec) if vec is not None else None
        try:
            conn = await aiosqlite.connect(item["db_path"])
            try:
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute(
                    """INSERT OR REPLACE INTO memory_chunks
                       (id, source_table, source_id, chunk_index, text_content, embedding, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (item["chunk_id"], item["source_table"], item["source_id"],
                     item["chunk_index"], item["text"][:2000], blob, time.time()),
                )
                await conn.commit()
            finally:
                await conn.close()
        except Exception as exc:
            self._stats["failed"] += 1
            should_log, suppressed = self._log_throttle.should_log("persist_fail")
            if should_log:
                logger.warning(
                    "embed_queue_persist_failed chunk_id=%s db_path=%s error=%r "
                    "suppressed_since_last_log=%d",
                    item["chunk_id"], item["db_path"], exc, suppressed,
                )
            return

        if vec is not None:
            self._stats["succeeded"] += 1
            if self._vector_index:
                try:
                    await self._vector_index.upsert(item["chunk_id"], vec)
                except Exception as exc:
                    logger.debug("vector_index upsert failed silently: %s", exc)
            return

        if skipped:
            self._stats["skipped"] += 1
            should_log, suppressed = self._log_throttle.should_log("embed_skipped")
            if should_log:
                logger.warning(
                    "embed_queue_chunk_skipped chunk_id=%s primary=%s reason=%s "
                    "fallback=skip suppressed_since_last_log=%d "
                    "(chunk text persisted; vector index entry skipped)",
                    item["chunk_id"],
                    getattr(self._embedder, "provider_name", "unknown"),
                    getattr(self._embedder, "degrade_reason", None) or "unknown",
                    suppressed,
                )
            return

        if last_error is not None:
            self._stats["failed"] += 1
            should_log, suppressed = self._log_throttle.should_log(
                f"embed_persistent_fail:{type(last_error).__name__}"
            )
            if should_log:
                logger.warning(
                    "embed_queue_embedding_failed chunk_id=%s provider=%s "
                    "attempts=%d error=%r suppressed_since_last_log=%d "
                    "(chunk text persisted; vector index entry skipped)",
                    item["chunk_id"],
                    getattr(self._embedder, "provider_name", "unknown"),
                    retries, last_error, suppressed,
                )

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def stats(self) -> dict:
        return {**self._stats, "pending": self.pending}


# ─────────────────────────────────────────────
# Embedding Provider with runtime degrade + fallback
# ─────────────────────────────────────────────

class EmbeddingProvider:
    """Pluggable embedding provider with auto-detection, runtime degrade, and LRU cache.

    See module docstring for the full provider chain and configuration surface.
    The contract is intentionally small:

    * :meth:`embed` and :meth:`embed_batch` always return numpy float32 arrays
      of :attr:`dimension`, OR raise :class:`EmbeddingSkipped` when the
      operator has explicitly opted into ``FERAL_EMBED_FALLBACK=skip`` and the
      primary is degraded.
    * Transient errors (sub-threshold 429, network) propagate to the caller
      so the embed queue can retry. Hard failures (insufficient_quota, invalid
      key) flip the provider into degrade and the next call returns through
      the fallback path immediately.
    * Logging during degrade is throttled — one structured warning per
      ``FERAL_EMBED_DEGRADE_LOG_INTERVAL_S`` per failure reason, regardless
      of how many embed attempts the queue makes during the cooldown.
    """

    _HARD_DEGRADE_S = 86400.0
    _RATE_LIMIT_DEGRADE_S = 60.0

    def __init__(self):
        self._provider: Optional[str] = None
        self._model = None
        self._dim = LOCAL_DIM
        self._cache: dict[str, np.ndarray] = {}

        raw_fallback = (os.getenv("FERAL_EMBED_FALLBACK") or "hash").strip().lower()
        if raw_fallback not in {"hash", "local", "skip"}:
            logger.warning(
                "Unknown FERAL_EMBED_FALLBACK=%r — defaulting to 'hash'", raw_fallback,
            )
            raw_fallback = "hash"
        self._fallback_mode = raw_fallback

        try:
            self._rl_threshold = max(1, int(os.getenv("FERAL_EMBED_RATE_LIMIT_THRESHOLD", "3")))
        except ValueError:
            self._rl_threshold = 3

        try:
            log_interval = float(os.getenv("FERAL_EMBED_DEGRADE_LOG_INTERVAL_S", "300"))
        except ValueError:
            log_interval = 300.0
        self._log_throttle = _LogThrottle(log_interval)

        self._degraded_until: float = 0.0
        self._degrade_reason: Optional[str] = None
        self._consecutive_rate_limits: int = 0

        self._detect_provider()

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def provider_name(self) -> str:
        return self._provider or "none"

    @property
    def fallback_mode(self) -> str:
        return self._fallback_mode

    @property
    def degraded(self) -> bool:
        return time.time() < self._degraded_until

    @property
    def degrade_reason(self) -> Optional[str]:
        return self._degrade_reason if self.degraded else None

    @property
    def degraded_until(self) -> float:
        return self._degraded_until

    @property
    def active_provider(self) -> str:
        if self.degraded:
            return f"fallback:{self._fallback_mode}"
        return self._provider or "hash"

    @property
    def available(self) -> bool:
        return self._provider is not None

    def _detect_provider(self):
        if os.getenv("OPENAI_API_KEY"):
            self._provider = "openai"
            self._dim = OPENAI_DIM
            logger.info(
                "Embedding provider: OpenAI text-embedding-3-small "
                "(fallback=%s, rate_limit_threshold=%d)",
                self._fallback_mode, self._rl_threshold,
            )
            return

        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            self._provider = "sentence_transformers"
            self._dim = LOCAL_DIM
            logger.info("Embedding provider: sentence-transformers (all-MiniLM-L6-v2)")
            return
        except ImportError:
            pass

        self._provider = "hash"
        self._dim = LOCAL_DIM
        logger.info("Embedding provider: hash fallback (no semantic similarity)")

    async def embed(self, text: str) -> np.ndarray:
        cache_key = hashlib.md5(text.encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        vec = await self._embed_impl(text)
        self._cache[cache_key] = vec
        if len(self._cache) > 5000:
            for k in list(self._cache.keys())[:1000]:
                del self._cache[k]
        return vec

    def embed_sync(self, text: str) -> Optional[np.ndarray]:
        """Best-effort synchronous embedding for sync callers.

        Returns a real semantic vector when one can be produced
        without blocking the asyncio event loop on a network call —
        i.e. when the active provider is the local ``sentence_transformers``
        model. Returns ``None`` in every other case so the caller knows
        to degrade to its lexical / FTS path:

        * primary provider is OpenAI — the embed path is HTTP, which
          we refuse to drive synchronously (would block the event loop
          if the caller happens to be on one). The async :meth:`embed`
          remains the supported entry point for OpenAI users.
        * primary provider is the deterministic ``hash`` fallback or
          ``none`` — there is no real semantic signal, so blending
          this into a hybrid score would only add noise.
        * the provider is currently in a degraded cooldown — same
          rationale as ``hash``: the queue is routing through the
          fallback right now, so a real vector isn't available.

        Cached the same way :meth:`embed` caches: an md5 hit short-
        circuits the model call entirely.
        """
        if not text:
            return None
        if self.degraded:
            return None
        if self._provider != "sentence_transformers":
            return None

        cache_key = hashlib.md5(text.encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        if self._model is None:
            # Loader race — the constructor reported the provider as
            # sentence_transformers but the model object never
            # materialised. Return None so the caller falls back to
            # lexical-only rather than crashing.
            return None
        try:
            vec = self._local_embed(text)
        except Exception:
            return None
        self._cache[cache_key] = vec
        if len(self._cache) > 5000:
            for k in list(self._cache.keys())[:1000]:
                del self._cache[k]
        return vec

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        results: list[Optional[np.ndarray]] = [None] * len(texts)
        uncached: list[str] = []
        uncached_idx: list[int] = []

        for i, text in enumerate(texts):
            cache_key = hashlib.md5(text.encode()).hexdigest()
            cached = self._cache.get(cache_key)
            if cached is not None:
                results[i] = cached
            else:
                uncached.append(text)
                uncached_idx.append(i)

        if uncached:
            vecs = await self._embed_batch_uncached(uncached)
            for j, idx in enumerate(uncached_idx):
                cache_key = hashlib.md5(uncached[j].encode()).hexdigest()
                self._cache[cache_key] = vecs[j]
                results[idx] = vecs[j]

        return results  # type: ignore[return-value]

    async def _embed_batch_uncached(self, texts: list[str]) -> list[np.ndarray]:
        if self.degraded or self._provider is None:
            return [self._fallback_embed(t) for t in texts]
        if self._provider == "openai":
            try:
                vecs = await self._openai_batch(texts)
                self._on_primary_success()
                return vecs
            except Exception as exc:
                fallback = self._classify_and_record_openai_error(exc)
                if not fallback:
                    raise
                return [self._fallback_embed(t) for t in texts]
        if self._provider == "sentence_transformers":
            return [self._local_embed(t) for t in texts]
        return [self._hash_embed(t, self._dim) for t in texts]

    async def _embed_impl(self, text: str) -> np.ndarray:
        if self.degraded or self._provider is None:
            return self._fallback_embed(text)
        if self._provider == "openai":
            try:
                vec = await self._openai_embed(text)
                self._on_primary_success()
                return vec
            except Exception as exc:
                fallback = self._classify_and_record_openai_error(exc)
                if not fallback:
                    raise
                return self._fallback_embed(text)
        if self._provider == "sentence_transformers":
            return self._local_embed(text)
        return self._hash_embed(text, self._dim)

    # ── OpenAI HTTP path ────────────────────────────────────────────

    async def _openai_embed(self, text: str) -> np.ndarray:
        import httpx
        api_key = os.getenv("OPENAI_API_KEY", "")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "text-embedding-3-small", "input": text[:8000]},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data["data"][0]["embedding"]
            return np.array(vec, dtype=np.float32)

    async def _openai_batch(self, texts: list[str]) -> list[np.ndarray]:
        import httpx
        api_key = os.getenv("OPENAI_API_KEY", "")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "text-embedding-3-small", "input": [t[:8000] for t in texts]},
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [np.array(d["embedding"], dtype=np.float32) for d in sorted_data]

    # ── Degrade classification ──────────────────────────────────────

    def _classify_and_record_openai_error(self, error: Exception) -> bool:
        """Update degrade state. Returns True if the caller should fall back
        immediately (degrade just engaged or was already engaged); False if
        the error was transient and the caller should propagate so the queue
        can retry with backoff.
        """
        status = 0
        body = ""
        try:
            import httpx
            if isinstance(error, httpx.HTTPStatusError):
                status = int(getattr(error.response, "status_code", 0) or 0)
                try:
                    body = error.response.text or ""
                except Exception:
                    body = ""
        except Exception:
            pass

        err_str = (body + " " + str(error)).lower()

        hard_quota = (
            status == 429 and (
                "insufficient_quota" in err_str
                or "exceeded your current quota" in err_str
                or "billing_hard_limit_reached" in err_str
            )
        )
        hard_auth = status in (401, 403) and (
            "invalid_api_key" in err_str
            or "incorrect api key" in err_str
            or "invalid api key" in err_str
            or "api key not valid" in err_str
        )

        if hard_quota or hard_auth:
            reason = "insufficient_quota" if hard_quota else "auth_invalid"
            self._set_degrade(reason, self._HARD_DEGRADE_S, permanent=True)
            return True

        is_rate_limit = (
            status == 429
            or "rate_limit_exceeded" in err_str
            or "rate limit" in err_str
        )
        if is_rate_limit:
            self._consecutive_rate_limits += 1
            if self._consecutive_rate_limits >= self._rl_threshold:
                self._set_degrade("rate_limit", self._RATE_LIMIT_DEGRADE_S, permanent=False)
                return True
            return False

        return False

    def _on_primary_success(self) -> None:
        if self._consecutive_rate_limits or self._degraded_until or self._degrade_reason:
            self._consecutive_rate_limits = 0
            self._degraded_until = 0.0
            self._degrade_reason = None

    def _set_degrade(self, reason: str, seconds: float, permanent: bool) -> None:
        self._degraded_until = time.time() + seconds
        self._degrade_reason = reason
        self._consecutive_rate_limits = 0

        log_key = f"degrade:{reason}"
        should_log, suppressed = self._log_throttle.should_log(log_key)
        if should_log:
            logger.warning(
                "embedding_provider_degraded provider=%s reason=%s permanent=%s "
                "cooldown_s=%d fallback=%s suppressed_since_last_log=%d "
                "(set FERAL_EMBED_FALLBACK={hash|local|skip} to control behaviour; "
                "fix the upstream API quota/key to resume primary embeddings)",
                self._provider, reason, permanent, int(seconds),
                self._fallback_mode, suppressed,
            )

        try:
            from observability.metrics import increment as _increment
            _increment(
                "feral_embeddings_degrades_total",
                attributes={"provider": self._provider or "unknown", "reason": reason},
            )
        except Exception:
            pass

    # ── Fallback paths ──────────────────────────────────────────────

    def _fallback_embed(self, text: str) -> np.ndarray:
        if self._fallback_mode == "skip":
            raise EmbeddingSkipped(
                f"primary embedding provider {self._provider!r} is degraded "
                f"(reason={self._degrade_reason or 'unknown'}); "
                "FERAL_EMBED_FALLBACK=skip — chunk persisted without vector"
            )
        if self._fallback_mode == "local":
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer("all-MiniLM-L6-v2")
                except Exception:
                    self._model = None
            if self._model is not None and self._dim == LOCAL_DIM:
                return self._local_embed(text)
            # dim mismatch or unavailable — fall through to hash so the index keeps shape
        return self._hash_embed(text, self._dim)

    def _local_embed(self, text: str) -> np.ndarray:
        vec = self._model.encode(text[:2000], normalize_embeddings=True)
        return np.array(vec, dtype=np.float32)

    def _hash_embed(self, text: str, dim: Optional[int] = None) -> np.ndarray:
        target_dim = dim if dim is not None else self._dim
        h = hashlib.sha256(text.lower().encode()).digest()
        repeated = h * (target_dim * 4 // len(h) + 1)
        vec = np.frombuffer(repeated, dtype=np.float32)[:target_dim]
        vec = vec / (np.linalg.norm(vec) + 1e-8)
        return vec.copy()
