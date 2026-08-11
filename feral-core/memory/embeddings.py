"""
FERAL Embedding Engine
========================
Provides vector embeddings for semantic memory search.

Vector Index Strategy:
  1. Try sqlite-vec extension → vec0 virtual table with vec_distance_cosine
  2. Otherwise a numpy scan over ``memory_chunks``

Neither of those is the "good" one and neither is broken. See
:func:`cosine_similarity_bulk` for the measurements: sqlite-vec 0.1.9 builds
no ANN index, so both paths are linear in the corpus and return identical
top-k, and the numpy path is the faster of the two at every size measured.
What sqlite-vec buys is resident memory, because it leaves the vectors on
disk. Pick on that basis, not on a promised speedup.

Embedding Providers (local-first, see ``FERAL_EMBED_PROVIDER`` below):
  1. fastembed BAAI/bge-small-en-v1.5 (384d) — default when importable
  2. Local sentence-transformers all-MiniLM-L6-v2 (384d) — next in line
  3. Hash fallback (no semantic similarity, degraded development/runtime fallback)
  4. OpenAI text-embedding-3-small (1536d) — ONLY on explicit opt-in

Provider selection is local-first on purpose. Until v2026.6 the mere
presence of ``OPENAI_API_KEY`` in the environment selected OpenAI, so a
key exported for chat completions silently turned every memory write
into a billed API call. Measured: ``pytest tests/test_memory.py`` with a
key exported posted to ``api.openai.com`` from 11 tests. A key is not
consent to spend money, so paid embeddings now require
``FERAL_EMBED_PROVIDER=openai``.

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
``FERAL_EMBED_PROVIDER`` — which primary provider to use. Default ``auto``.

  * ``auto`` (default) — fastembed if importable, else sentence-transformers
    if importable, else hash. NEVER selects a paid provider, whatever keys
    happen to be exported.
  * ``local`` — same chain as ``auto`` minus any possibility of a network
    provider ever being reachable from this setting. Identical behaviour
    today; kept separate so ``auto`` can gain a smarter (still free)
    default later without silently changing what ``local`` promises.
  * ``openai`` — text-embedding-3-small, and only when ``OPENAI_API_KEY``
    is set. With no key it warns and degrades down the ``auto`` chain
    rather than raising, because embedding is a background path and a
    missing key must not take the process down.
  * ``hash`` — force the deterministic hash fallback. Useful in tests and
    on machines where a model download is not acceptable.

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
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Sequence
from typing import Any, Optional

import aiosqlite
import numpy as np

logger = logging.getLogger("feral.memory.embeddings")

OPENAI_DIM = 1536
LOCAL_DIM = 384
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

# Default local model. Chosen so the local-first default costs nothing and
# fits anywhere: fastembed 0.8 runs the model on onnxruntime and has NO torch
# dependency (verified against the published 0.8.0 metadata: huggingface-hub,
# loguru, mmh3, numpy, onnxruntime, pillow, py-rust-stemmers, requests,
# tokenizers, tqdm). Measured in a clean venv: ~193MB installed, of which
# onnxruntime is 75MB and numpy (already a FERAL dep) is 36MB, versus ~2.5GB
# for a torch-based sentence-transformers install.
#
# bge-small-en-v1.5 emits 384 dims — the same as all-MiniLM-L6-v2 and the
# existing LOCAL_DIM — so adding this backend needs no index migration and
# no dimension change for anyone already on the sentence-transformers path.
# Verified by running it: shape (384,), float32, L2-normalised (norm 1.0).
FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"

_VALID_PROVIDER_MODES = ("auto", "local", "openai", "hash")


# ──────────────────────────────────────────────────────────────────────
# Process-wide "this local backend cannot load here" memo
# ──────────────────────────────────────────────────────────────────────
# Constructing either local backend can block for the full model-download
# timeout when the package is present but the model is not cached and the
# hub is unreachable. Measured on GitHub's ubuntu runners: ~39s per
# attempt.
#
# Whether a backend can load is a property of the PROCESS (is the package
# importable, is the model cached, is the hub reachable), not of one
# EmbeddingProvider instance. Caching the failure per instance therefore
# re-pays that timeout for every provider the process constructs, and the
# sentence-transformers path below did not cache it at all, so it re-paid
# it on every single embed call.
#
# That is what made CI red rather than slow: in the 2026-08-06 matrix run
# (job 92525495791) 50 separate stalls of ~39s accounted for 33.3 of the
# job's 45 minutes, while the other 5788 tests took 4.1 minutes between
# them. The job hit its ceiling and was cancelled with no named failure.
# The suite's own outbound-network guard did not catch it: that guard
# patches httpx, and these downloads go out over requests/urllib3.
#
# Note the failure once, globally, and let every later caller fall
# straight through to the hash embedding.
_LOCAL_BACKEND_FAILURES: dict[str, str] = {}
_LOCAL_BACKEND_LOCK = threading.Lock()


def _local_backend_failed(key: str) -> Optional[str]:
    """The recorded failure reason for ``key``, or None if untried/ok."""
    with _LOCAL_BACKEND_LOCK:
        return _LOCAL_BACKEND_FAILURES.get(key)


def _note_local_backend_failure(key: str, exc: BaseException) -> None:
    """Record, once per process, that ``key`` cannot be constructed."""
    with _LOCAL_BACKEND_LOCK:
        first = key not in _LOCAL_BACKEND_FAILURES
        _LOCAL_BACKEND_FAILURES[key] = str(exc)
    if first:
        logger.warning("%s lazy load failed: %s", key, exc)


def reset_local_backend_failures() -> None:
    """Forget every recorded backend failure.

    For tests that need a provider to genuinely re-attempt a load, and
    for a caller that has just installed or warmed a model and wants the
    process to notice without a restart.
    """
    with _LOCAL_BACKEND_LOCK:
        _LOCAL_BACKEND_FAILURES.clear()


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


class EmbeddingDimensionMismatch(ValueError):
    """A query vector was compared against a stored vector of another dimension.

    Subclasses :class:`ValueError` because that is what ``np.dot`` already
    raised for this case, so every existing caller's exception handling keeps
    working unchanged.
    """


# One error line per distinct (query_dim, stored_dim) pair per process. The
# mismatch is discovered inside per-row scan loops (memory/store.py,
# memory/notes_legacy.py, memory/knowledge_graph.py all iterate every stored
# vector), so an unthrottled log would emit one line per row.
_REPORTED_DIM_MISMATCHES: set[tuple[int, int]] = set()


def _report_dim_mismatch(dim_a: int, dim_b: int) -> None:
    """Emit the once-per-(query_dim, stored_dim)-pair mismatch ERROR.

    Shared by the scalar :func:`cosine_similarity` and the vectorized
    :func:`cosine_similarity_bulk` so the throttle state is one set for the
    whole process. Converting a call site from the scalar loop to the bulk
    path must not un-throttle the log, and must not re-report a pair the
    scalar path already reported.
    """
    key = (dim_a, dim_b)
    if key in _REPORTED_DIM_MISMATCHES:
        return
    _REPORTED_DIM_MISMATCHES.add(key)
    logger.error(
        "embedding_dimension_mismatch query_dim=%d stored_dim=%d — the "
        "stored vectors were written by a different embedding provider "
        "(OpenAI=%d, local=%d). Vector search is dead for this data "
        "until the two agree: either set FERAL_EMBED_PROVIDER back to "
        "the provider that wrote them, or clear the vector tables so "
        "they are re-embedded at the current dimension. Keyword/FTS "
        "search is unaffected.",
        dim_a, dim_b, OPENAI_DIM, LOCAL_DIM,
    )


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    # Dimension mismatch means the vectors were written by a DIFFERENT
    # embedding provider than the one running now (OpenAI is 1536, every local
    # backend is 384). Without this check the failure is close to invisible:
    # np.dot raises a shape ValueError, and the three numpy-scan callers all
    # wrap the loop in `except Exception: logger.debug(...)`, so switching
    # FERAL_EMBED_PROVIDER silently turns vector search into "returns nothing"
    # with a debug line nobody reads. There is no re-embedding migration in
    # this codebase, so the honest behaviour is to say so loudly and let the
    # caller degrade to its FTS path rather than to guess at a conversion.
    shape_a = getattr(a, "shape", None)
    shape_b = getattr(b, "shape", None)
    if shape_a is not None and shape_b is not None and shape_a != shape_b:
        dim_a = int(shape_a[-1]) if shape_a else 0
        dim_b = int(shape_b[-1]) if shape_b else 0
        _report_dim_mismatch(dim_a, dim_b)
        raise EmbeddingDimensionMismatch(
            f"query vector has {dim_a} dims but stored vector has {dim_b}; "
            "embedding provider changed and stored vectors were not re-embedded"
        )

    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# Rows decoded and scored per block in ``cosine_similarity_bulk``. Small
# enough that the joined byte buffer (2048 * 384 * 4 = 3MB at the local
# dimension) stays close to cache and never doubles process RSS the way a
# single 150MB join over a 100k-row table would; large enough that the
# per-block numpy call overhead is noise. Measured on this machine at 100k
# rows x 384 dims: 2048 -> 23.5ms, 4096 -> 25.0ms, 8192 -> 29.7ms,
# monolithic single join -> 29.3ms.
_BULK_SCAN_BLOCK_ROWS = 2048


def cosine_similarity_bulk(
    query_vec: np.ndarray, blobs: Sequence[bytes]
) -> np.ndarray:
    """Cosine similarity of ``query_vec`` against many stored float32 blobs.

    Returns a float32 array of ``len(blobs)`` scores, in input order, with
    ``result[i] == cosine_similarity(query_vec, blob_to_vec(blobs[i]))``.

    Why this exists
    ---------------
    sqlite-vec is an *extension*, and it cannot load on an interpreter built
    without ``enable_load_extension`` (pyenv on macOS builds it out by
    default, confirmed on this machine, see ``_try_load_sqlite_vec``). So the
    numpy scan is not an exotic path, it is the DEFAULT path for a large
    share of installs, and every one of those scans used to run as a Python
    per-row loop of ``blob_to_vec`` + ``cosine_similarity``, i.e. one
    ``np.frombuffer`` + one ``.copy()`` + one dot product per row.

    Measured on this machine, 384-dim float32, same blobs and same query in
    one process, per-row loop versus THIS function (not versus a bare
    matmul: the numbers below include the width guard and the byte-buffer
    decode, which are most of the remaining cost):

    ======  ============  ==========  =====
    chunks  per-row loop  this        ratio
    ======  ============  ==========  =====
      1000         2.8ms       0.2ms  15.6x
     10000        26.6ms       1.8ms  14.9x
     50000       135.3ms      11.4ms  11.9x
    100000       266.9ms      23.7ms  11.3x
    ======  ============  ==========  =====

    At 100k chunks that was ~0.27s of pure interpreter overhead on every
    memory query. Nothing about the arithmetic was slow, only the loop.

    The floor for the arithmetic alone at 100k x 384 is ~9ms (3.9ms for the
    matvec, 5.4ms for the row norms). The other ~14ms is ~9ms of width
    guard and ~11ms of ``b"".join`` decode, overlapped by blocking. Both are
    the price of reading rows that arrive as a Python list of ``bytes``.
    The full-corpus caller removes them a different way, by keeping the
    decoded matrix between queries: see ``MemoryStore._centered_corpus``.

    This is not the slow path
    -------------------------
    Earlier revisions of this file, of ``feral doctor`` and of the setup
    wizard called this scan "degraded" and told the user to rebuild CPython
    with ``--enable-loadable-sqlite-extensions`` to escape it. That advice
    was wrong on performance and is gone.

    sqlite-vec 0.1.9 (the pinned version) builds no ANN index. A vec0
    ``MATCH`` is itself a full scan, so BOTH paths are O(n) and the choice
    was never linear-versus-sublinear. Measured on this machine, top-5 over
    384-dim vectors, same corpus, identical results to seven decimals:

    ======  =========  ===============
    corpus  numpy      sqlite-vec vec0
    ======  =========  ===============
     12000     0.46ms   7.08ms
     50000     2.42ms   10.98 - 28.53ms
    100000     3.97ms   56.99ms
    ======  =========  ===============

    So numpy is roughly an order of magnitude FASTER, and rebuilding the
    interpreter to reach vec0 buys a slowdown.

    The honest argument for sqlite-vec is resident memory, not latency.
    numpy holds the whole matrix (~18MB at 12k rows, ~154MB at 100k);
    sqlite-vec leaves the vectors on disk. On a large store that is the
    trade worth making, and the rebuild instructions are still printed for
    exactly that reason. Nothing here is broken without it.

    Ragged and mismatched input
    ---------------------------
    ``np.frombuffer(b"".join(blobs))`` over blobs of MIXED widths does not
    fail, it silently reinterprets the byte stream and returns confident
    garbage. That is a real state to be in: a provider switch (1536-dim
    OpenAI vs 384-dim local) leaves the table holding both widths, because
    there is no re-embedding migration in this codebase.

    So every blob's width is checked against the query's before any decode.
    If any row is the wrong width, the first such row (in input order) is
    reported through the same throttled ERROR as the scalar path and
    :class:`EmbeddingDimensionMismatch` is raised, which is exactly what the
    per-row loop did when it reached that row. The failure must stay loud:
    the callers all wrap their scan in ``except Exception: logger.debug(...)``,
    so a silent wrong answer here is indistinguishable from a working index.

    The width pass costs ~9ms of the ~23ms total at 100k rows. It is kept
    anyway, and kept exact (a set of distinct widths, not a cheaper
    total-bytes check), because a total-bytes check accepts e.g. one
    zero-length blob paired with one double-width blob and hands the reshape
    a buffer that is the right size and the wrong data.
    """
    q = np.asarray(query_vec, dtype=np.float32).ravel()
    dim = int(q.shape[0])
    n = len(blobs)
    out = np.zeros(n, dtype=np.float32)
    if n == 0:
        return out

    expected_bytes = dim * 4
    widths = set(map(len, blobs))
    if widths != {expected_bytes}:
        # Slow path, only reached when the data is already broken. Find the
        # first offending row so the reported (query_dim, stored_dim) pair is
        # the same one the per-row loop would have reported.
        bad_width = next(len(b) for b in blobs if len(b) != expected_bytes)
        stored_dim = bad_width // 4
        _report_dim_mismatch(dim, stored_dim)
        raise EmbeddingDimensionMismatch(
            f"query vector has {dim} dims but stored vector has {stored_dim} "
            f"({bad_width} bytes); embedding provider changed and stored "
            "vectors were not re-embedded"
        )

    norm_q = float(np.linalg.norm(q))
    if norm_q == 0.0 or dim == 0:
        # Scalar path returns 0.0 for a zero-norm operand rather than
        # dividing. Match it, and never let a 0/0 NaN reach a ranking.
        return out

    for start in range(0, n, _BULK_SCAN_BLOCK_ROWS):
        block = blobs[start:start + _BULK_SCAN_BLOCK_ROWS]
        rows = len(block)
        # Read-only view over the joined buffer. Nothing writes through it:
        # the matmul and the einsum both allocate their own outputs, and the
        # array returned to the caller is the writable ``out`` above.
        mat = np.frombuffer(b"".join(block), dtype=np.float32).reshape(rows, dim)
        dots = mat @ q
        # einsum beats both np.linalg.norm(axis=1) and (mat*mat).sum(axis=1)
        # here (measured 5.4ms vs 12.5ms vs 12.5ms at 100k x 384) because it
        # makes no temporary the size of the matrix.
        norms = np.sqrt(np.einsum("ij,ij->i", mat, mat))
        np.divide(
            dots, norms * norm_q,
            out=out[start:start + rows],
            where=norms > 0,
        )
    return out


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

    # Checked before the load attempt, because the failure it produces is
    # an AttributeError that reads like a bug in FERAL rather than what it
    # is: a Python interpreter compiled without loadable SQLite extension
    # support. pyenv builds it out by default on macOS, so "pip install
    # sqlite-vec" succeeds, the import succeeds, and the extension can
    # still never load. Observed on pyenv 3.11.11 with SQLite 3.51.0.
    #
    # Logged at INFO, not WARNING, and phrased as a fact rather than a
    # defect. This used to warn that search had fallen back to a scan that
    # is "correct, but O(n) per query" and tell the operator to rebuild
    # CPython. Measured, vec0 is also O(n) and is ~10x SLOWER than the numpy
    # path at every corpus size tested (see cosine_similarity_bulk), so that
    # warning sent people to rebuild their interpreter for a regression.
    # The rebuild line stays, attached to the reason that survives
    # measurement: memory, on a large store.
    if not hasattr(conn, "enable_load_extension"):
        logger.info(
            "sqlite-vec cannot load: this Python was built without loadable "
            "SQLite extension support. Vector search runs over numpy instead, "
            "which is correct and, at every corpus size measured, faster; "
            "sqlite-vec's advantage is that it keeps vectors on disk rather "
            "than in RAM (~18MB at 12k chunks, ~154MB at 100k). If that "
            "memory matters on your store, rebuild with PYTHON_CONFIGURE_OPTS="
            "\"--enable-loadable-sqlite-extensions\" (pyenv) or use a "
            "python.org / Homebrew interpreter.",
        )
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
        logger.info(
            "sqlite-vec not installed — using numpy fallback for vector search. "
            "Install it with: pip install 'feral-ai[embeddings]'",
        )
        _SQLITE_VEC_AVAILABLE = False
        return False
    except Exception as e:
        logger.warning(f"sqlite-vec load failed ({e}) — using numpy fallback")
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
    Vector search over a sqlite-vec vec0 virtual table, with an automatic
    numpy scan when the extension cannot load.

    Neither is the "real" one. sqlite-vec 0.1.9 builds no ANN index, so
    both are full scans returning the same top-k; the numpy path measures
    faster and the vec0 path holds less in RAM. See
    :func:`cosine_similarity_bulk`.
    """

    _VEC0_DIM_RE = re.compile(r"FLOAT\s*\[\s*(\d+)\s*\]", re.IGNORECASE)

    def __init__(self, db_path: str, dimension: int, table_name: str = "vec_index"):
        self._db_path = db_path
        self._dim = dimension
        self._table_name = table_name
        self._use_vec = False
        self._dim_mismatch: Optional[tuple[int, int]] = None
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        if self._use_vec:
            _try_load_sqlite_vec(conn)
        return conn

    @classmethod
    def _declared_dim(cls, create_sql: Optional[str]) -> Optional[int]:
        """Dimension a vec0 table was CREATEd with, parsed from its stored DDL.

        sqlite-vec bakes the dimension into the column type (``FLOAT[384]``)
        and there is no pragma that reports it back, so sqlite_master.sql is
        the only place the on-disk dimension is recorded.
        """
        if not create_sql:
            return None
        match = cls._VEC0_DIM_RE.search(create_sql)
        return int(match.group(1)) if match else None

    def _existing_dim(self, conn: sqlite3.Connection) -> Optional[int]:
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (self._table_name,),
            ).fetchone()
        except Exception:
            return None
        return self._declared_dim(row["sql"] if row else None)

    def _init(self):
        self._use_vec = sqlite_vec_available()
        conn = self._conn()
        if self._use_vec:
            # A vec0 table's dimension is fixed at CREATE time, and
            # ``CREATE VIRTUAL TABLE IF NOT EXISTS`` is a silent no-op when
            # the table already exists at another dimension. Without this
            # check, switching FERAL_EMBED_PROVIDER (1536-dim OpenAI vs
            # 384-dim local) leaves a table whose every upsert is rejected by
            # sqlite-vec and swallowed by the debug-level handler in
            # ``upsert``, and whose searches return nothing. Refuse the stale
            # table instead: ``indexed`` goes False, the caller degrades to
            # its FTS/brute-force path, and the operator gets one loud line
            # saying exactly what to do.
            existing_dim = self._existing_dim(conn)
            if existing_dim is not None and existing_dim != self._dim:
                self._dim_mismatch = (existing_dim, self._dim)
                self._use_vec = False
                logger.error(
                    "vec0 table '%s' exists at dim=%d but the active embedding "
                    "provider produces dim=%d — refusing to use the index "
                    "(vector search disabled for this table; keyword search is "
                    "unaffected). Set FERAL_EMBED_PROVIDER back to the provider "
                    "that wrote it, or drop the table so it is rebuilt at the "
                    "current dimension.",
                    self._table_name, existing_dim, self._dim,
                )
                conn.close()
                return
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

    @property
    def dim_mismatch(self) -> Optional[tuple[int, int]]:
        """``(on_disk_dim, active_dim)`` when the index was refused, else None."""
        return self._dim_mismatch

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
        self._fastembed_model = None
        self._fastembed_unavailable = False
        self._dim = LOCAL_DIM
        self._cache: dict[str, np.ndarray] = {}
        # Default assigned here as well as in _detect_provider: tests patch
        # _detect_provider out to force a provider, and everything reading the
        # mode has to keep working when they do.
        self._provider_mode = "auto"

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
    def provider_mode(self) -> str:
        """Normalised ``FERAL_EMBED_PROVIDER`` value actually in effect."""
        return self._provider_mode

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
        """Pick the primary provider. Local-first, paid only on explicit opt-in.

        The precedence used to be "OPENAI_API_KEY is set, therefore OpenAI",
        which billed users who exported that key for chat completions and never
        asked for cloud embeddings (verified: 11 tests in tests/test_memory.py
        posted to api.openai.com under a plain `pytest` run). Selecting a paid
        provider is now something an operator does on purpose, via
        FERAL_EMBED_PROVIDER=openai, and nothing else.
        """
        mode = (os.getenv("FERAL_EMBED_PROVIDER") or "auto").strip().lower()
        if mode not in _VALID_PROVIDER_MODES:
            logger.warning(
                "Unknown FERAL_EMBED_PROVIDER=%r — defaulting to 'auto' (%s)",
                mode, "|".join(_VALID_PROVIDER_MODES),
            )
            mode = "auto"
        self._provider_mode = mode

        if mode == "hash":
            self._select_hash()
            return

        if mode == "openai":
            if os.getenv("OPENAI_API_KEY"):
                self._provider = "openai"
                self._dim = OPENAI_DIM
                logger.info(
                    "Embedding provider: OpenAI text-embedding-3-small "
                    "(explicitly selected via FERAL_EMBED_PROVIDER=openai; "
                    "fallback=%s, rate_limit_threshold=%d)",
                    self._fallback_mode, self._rl_threshold,
                )
                return
            # Do not raise. Embedding runs on background queues, and a missing
            # key is an operator mistake to report, not a reason to take down
            # memory writes. Degrade down the local chain instead.
            logger.warning(
                "FERAL_EMBED_PROVIDER=openai but OPENAI_API_KEY is not set — "
                "falling back to local embeddings. Export the key or unset "
                "FERAL_EMBED_PROVIDER to silence this."
            )

        # auto / local / opted-into-openai-but-keyless all land here.
        #
        # Lazy: detect availability without paying the (multi-second,
        # first-run-downloads-a-model) construction cost on the boot
        # critical path. The models are built on first embed via
        # ``_ensure_fastembed_model`` / ``_ensure_local_model``. This keeps
        # ``feral serve`` fast regardless of the configured vector backend.
        import importlib.util
        if importlib.util.find_spec("fastembed") is not None:
            self._provider = "fastembed"
            self._dim = LOCAL_DIM
            logger.info(
                "Embedding provider: fastembed (%s, %dd, lazy-loaded on first embed)",
                FASTEMBED_MODEL, LOCAL_DIM,
            )
            return

        if importlib.util.find_spec("sentence_transformers") is not None:
            self._provider = "sentence_transformers"
            self._dim = LOCAL_DIM
            logger.info(
                "Embedding provider: sentence-transformers "
                "(all-MiniLM-L6-v2, lazy-loaded on first embed)"
            )
            return

        self._select_hash()

    def _select_hash(self):
        self._provider = "hash"
        self._dim = LOCAL_DIM
        logger.info(
            "Embedding provider: hash fallback (no semantic similarity — "
            "install the 'embeddings' extra for real local vectors)"
        )

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
        without blocking the asyncio event loop on a network call,
        i.e. when the active provider is one of the local models
        (``fastembed`` or ``sentence_transformers``). Returns ``None``
        in every other case so the caller knows to degrade to its
        lexical / FTS path:

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
        if self._provider not in ("sentence_transformers", "fastembed"):
            return None

        cache_key = hashlib.md5(text.encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        model = self._fastembed_model if self._provider == "fastembed" else self._model
        if model is None:
            # Loader race — the constructor reported a local provider but
            # the model object never materialised. Return None so the
            # caller falls back to lexical-only rather than crashing.
            # Deliberately NOT loading it here: construction downloads and
            # initialises a model, which is not something a sync call on a
            # request path should ever block on.
            return None
        try:
            vec = (
                self._fastembed_embed(text) if self._provider == "fastembed"
                else self._local_embed(text)
            )
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
            # _fallback_embed is blocking too: it can reach
            # _ensure_local_model, whose own docstring notes that
            # construction triggers a ~130 MB download. The finding
            # names that risk but cites the wrong lines; this is where
            # it is reachable from the loop. See AUDIT-FIXES F-04.
            return await asyncio.to_thread(
                lambda: [self._fallback_embed(t) for t in texts]
            )
        if self._provider == "openai":
            try:
                vecs = await self._openai_batch(texts)
                self._on_primary_success()
                return vecs
            except Exception as exc:
                fallback = self._classify_and_record_openai_error(exc)
                if not fallback:
                    raise
                return await asyncio.to_thread(
                    lambda: [self._fallback_embed(t) for t in texts]
                )
        # Same offload as _embed_impl, and it matters more here: the
        # sentence-transformers branch pays the blocking cost once per
        # text, so a batch of 200 held the loop for 200 forward passes.
        # One thread hop covers the whole batch rather than one per item.
        if self._provider == "fastembed":
            return await asyncio.to_thread(self._fastembed_batch, texts)
        if self._provider == "sentence_transformers":
            return await asyncio.to_thread(
                lambda: [self._local_embed(t) for t in texts]
            )
        return [self._hash_embed(t, self._dim) for t in texts]

    async def _embed_impl(self, text: str) -> np.ndarray:
        if self.degraded or self._provider is None:
            return await asyncio.to_thread(self._fallback_embed, text)
        if self._provider == "openai":
            try:
                vec = await self._openai_embed(text)
                self._on_primary_success()
                return vec
            except Exception as exc:
                fallback = self._classify_and_record_openai_error(exc)
                if not fallback:
                    raise
                return await asyncio.to_thread(self._fallback_embed, text)
        # Both local branches are CPU-bound and synchronous:
        # _local_embed ends in SentenceTransformer.encode(), a full
        # transformer forward pass, and _fastembed_embed runs an ONNX
        # session. Called inline from this async function they hold the
        # event loop for their whole duration, so voice streaming,
        # websocket heartbeats and every concurrent request stop dead.
        # _detect_provider defaults to "auto", which resolves to exactly
        # these two, so this was the default install rather than an
        # exotic configuration. See AUDIT-FIXES F-04.
        #
        # _ensure_local_model is reachable from inside these and can
        # trigger a model download, so the worst case being moved off the
        # loop is not milliseconds, it is the length of a fetch.
        if self._provider == "fastembed":
            return await asyncio.to_thread(self._fastembed_embed, text)
        if self._provider == "sentence_transformers":
            return await asyncio.to_thread(self._local_embed, text)
        # _hash_embed is pure arithmetic over a short string and stays
        # inline: a thread hop would cost more than the work.
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
            # Both local backends emit LOCAL_DIM, so the dim guard below is
            # the same for either. fastembed is tried first to match the
            # local-first precedence in _detect_provider; without it a host
            # that has only fastembed installed would silently hash here.
            if self._dim == LOCAL_DIM:
                if self._fastembed_model is not None or self._ensure_fastembed_model():
                    return self._fastembed_embed(text)
            if self._model is None:
                self._ensure_local_model()
            if self._model is not None and self._dim == LOCAL_DIM:
                return self._local_embed(text)
            # dim mismatch or unavailable — fall through to hash so the index keeps shape
        return self._hash_embed(text, self._dim)

    def _ensure_fastembed_model(self) -> bool:
        """Lazily construct the fastembed model on first use. Idempotent.

        Construction is where fastembed downloads the ONNX model (~130MB for
        bge-small-en-v1.5, measured: 5 files, ~10s on a warm connection) into
        its own on-disk cache, so it is deferred out of ``__init__`` for the
        same reason the sentence-transformers load is: boot must not block on
        a model download. Every later process start reads the cache and makes
        no network call.
        """
        if self._fastembed_model is not None:
            return True
        # One attempt, one warning, PER PROCESS. _fallback_embed can reach
        # this on every single embed during a degrade window, and a
        # retry-plus-log per call would both spam the log and re-pay the
        # load failure — which costs a full model-download timeout, not an
        # import error, whenever the package is present but the model is
        # not cached. See _LOCAL_BACKEND_FAILURES.
        if self._fastembed_unavailable or _local_backend_failed("fastembed"):
            self._fastembed_unavailable = True
            return False
        try:
            from fastembed import TextEmbedding
            self._fastembed_model = TextEmbedding(model_name=FASTEMBED_MODEL)
            return True
        except Exception as exc:  # noqa: BLE001 — degrade to hash, never crash
            self._fastembed_unavailable = True
            _note_local_backend_failure("fastembed", exc)
            return False


    def _fastembed_embed(self, text: str) -> np.ndarray:
        if self._fastembed_model is None and not self._ensure_fastembed_model():
            return self._hash_embed(text, self._dim)
        return self._fastembed_batch([text])[0]

    def _fastembed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """Embed a batch through fastembed.

        ``TextEmbedding.embed`` takes an iterable of documents and yields one
        numpy array per document, in input order. Verified against fastembed
        0.8.0: yields ``np.ndarray`` of shape ``(384,)``, dtype float32,
        already L2-normalised (measured norm 1.0), so no extra normalisation
        step is applied here and cosine distance behaves the same as it does
        for the sentence-transformers path, which normalises at encode time.
        """
        if self._fastembed_model is None and not self._ensure_fastembed_model():
            return [self._hash_embed(t, self._dim) for t in texts]
        try:
            vecs = list(self._fastembed_model.embed([t[:2000] for t in texts]))
        except Exception as exc:  # noqa: BLE001 — degrade to hash, never crash
            logger.warning("fastembed embed failed: %s — using hash fallback", exc)
            return [self._hash_embed(t, self._dim) for t in texts]
        return [np.asarray(v, dtype=np.float32) for v in vecs]

    def _ensure_local_model(self) -> bool:
        """Lazily construct the sentence-transformers model on first use.

        Idempotent. Returns ``True`` once a usable model is loaded.
        Construction is deferred out of ``__init__`` so boot never blocks
        on the (slow, first-run-downloads) model load; the cost is paid
        on the first real embed instead (typically a background task).

        A failure is remembered process-wide (see _LOCAL_BACKEND_FAILURES)
        rather than not at all. Before, every caller that reached here
        after a failure re-ran the constructor, because a failed load
        leaves ``self._model`` as ``None`` — exactly the state that
        triggers the retry. Where the package imports but the model is
        not cached, that constructor is a network download, so the retry
        cost a full download timeout per embed rather than an import
        error.
        """
        if self._model is not None:
            return True
        if _local_backend_failed("sentence-transformers"):
            return False
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            return True
        except Exception as exc:  # noqa: BLE001 — degrade to hash, never crash
            self._model = None
            _note_local_backend_failure("sentence-transformers", exc)
            return False

    def _local_embed(self, text: str) -> np.ndarray:
        if self._model is None and not self._ensure_local_model():
            return self._hash_embed(text, self._dim)
        vec = self._model.encode(text[:2000], normalize_embeddings=True)
        return np.array(vec, dtype=np.float32)

    def _hash_embed(self, text: str, dim: Optional[int] = None) -> np.ndarray:
        target_dim = dim if dim is not None else self._dim
        h = hashlib.sha256(text.lower().encode()).digest()
        repeated = h * (target_dim * 4 // len(h) + 1)
        vec = np.frombuffer(repeated, dtype=np.float32)[:target_dim]
        vec = vec / (np.linalg.norm(vec) + 1e-8)
        return vec.copy()
