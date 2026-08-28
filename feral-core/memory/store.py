"""
FERAL Memory System — Production Cognitive Architecture
=========================================================
4-tier memory with real vector search, hybrid ranking, temporal decay,
multi-stage compaction, and knowledge graph integration.

  ┌─────────────────────────────────────┐
  │  Working Memory (current session)   │  ← volatile, per-session
  │  Episodic Memory (past events)      │  ← timestamped, decayable, embedded
  │  Semantic Memory (knowledge graph)  │  ← entity-linked graph
  │  Execution Log (learns from actions)│  ← every skill invocation
  └─────────────────────────────────────┘

Hybrid search: FTS5 text (weight 0.3) + vector similarity (weight 0.7)
with MMR diversity reranking and temporal decay.

v2026.5.33 (Option C async-native rewrite)
------------------------------------------
Every method that touches SQLite is async via aiosqlite. The asyncio
event loop never blocks on a memory call — concurrent searches /
upserts parallelise through aiosqlite's per-connection worker threads.
``__init__`` stays sync (one-shot boot DDL); ``start_background_tasks``
must be called from inside a running event loop after construction.

Working-memory operations (in-RAM deques) stay sync — they don't hit
I/O and don't benefit from async. Pure helpers (``_episode_row_to_dict``,
``_mmr_rerank_episodes``, ``_wiki_slug``, ``_heuristic_summarize``)
stay sync for the same reason.
"""

from __future__ import annotations
import asyncio
import json
import logging
import math
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

import aiosqlite

# aiosqlite spawns one non-daemon worker Thread per Connection. A
# pooled MemoryStore holds N connections — if any caller forgets to
# call aclose() (tests, signal-driven shutdowns, exceptions on the
# boot path), the live threads block process exit indefinitely. Force
# the worker thread to start as daemon so the interpreter can exit
# cleanly even when the pool isn't drained. This is a global setting
# applied at import time; aiosqlite users that need non-daemon
# workers can override after import.
try:
    _AiosqliteConnection = aiosqlite.Connection
    _orig_aiosqlite_init = _AiosqliteConnection.__init__

    def _daemonize_aiosqlite_init(self, *args, **kwargs):
        _orig_aiosqlite_init(self, *args, **kwargs)
        try:
            self._thread.daemon = True
        except Exception:
            pass

    _AiosqliteConnection.__init__ = _daemonize_aiosqlite_init  # type: ignore[assignment]
except Exception:
    # Best-effort. Worst case the process needs an extra SIGTERM at
    # shutdown, which a wrapper script can deliver.
    pass

from config.loader import feral_data_home
from memory.fts_query import STRICT as FTS_STRICT, fts5_match_query
from memory.sqlite_features import require_fts5
from memory.context_builder import (
    build_context_for_llm_async as context_build_context_for_llm_async,
    compact_session as context_compact_session,
    heuristic_summarize as context_heuristic_summarize,
    llm_summarize as context_llm_summarize,
    search_all as context_search_all,
)
import numpy as np

from memory.embeddings import (
    EmbeddingDimensionMismatch,
    EmbeddingProvider,
    EmbedQueue,
    chunk_text,
    cosine_similarity_bulk,
)
from memory.vector_index_backends import VectorIndexBackend
from memory.notes_legacy import (
    count_notes,
    delete_note,
    list_recent_notes,
    save_note,
    search_notes,
)
from memory.wiki import (
    wiki_compile as helper_wiki_compile,
    wiki_get_page as helper_wiki_get_page,
    wiki_list_pages as helper_wiki_list_pages,
    wiki_slug as helper_wiki_slug,
    wiki_stats as helper_wiki_stats,
    wiki_upsert_page as helper_wiki_upsert_page,
)

logger = logging.getLogger("feral.memory")

#: Episode types whose text must NEVER train the operator's self-model.
#:
#: The AboutMe extractor is entirely first-person ("I prefer X", "My
#: wife <Name>", "I live in X"). That is sound for a chat episode, where
#: "I" is the operator by construction, and wrong for anything the
#: operator merely OVERHEARD, where "I" is whoever was speaking.
#:
#: ``ambient_conversation`` is the recorded-speech type written by
#: agents/ambient_transcript.py (EVENT_TYPE there). Keep the two in step:
#: a new capture type belongs here too.
_NO_SELF_MODEL_EVENT_TYPES = frozenset({
    "ambient_conversation",
    # The agent's own replies, persisted per turn so what it said
    # survives compaction. They are the least eligible text in the
    # system for self-model extraction: an assistant that says "I have
    # booked you a flight to Tokyo" would otherwise teach About-Me that
    # the operator is flying to Tokyo, and an assistant describing its
    # own capabilities would install those as facts about the user.
    "assistant_reply",
})


# ── Semantic relevance floor ────────────────────────────────────────────────
#
# The old floor was a raw cosine of 0.25 and it never rejected anything. On
# the reporter's 11,996-chunk store every single chunk cleared it for every
# query, including "asdfgh zxcvbn qwerty", so search could not say "nothing
# matches" and always returned its limit in results however irrelevant.
#
# The cause is anisotropy, which is a property of the embedding model rather
# than a bug in this store: sentence embeddings do not spread over the sphere,
# they occupy a narrow cone. Measured on that corpus, the mean vector has norm
# 0.799 and the average chunk sits at cosine 0.799 from it. Every pair of
# vectors therefore shares a large constant component, and cos(query, doc) is
# dominated by that shared direction rather than by meaning. Raw cosines land
# in a narrow band near 0.53 whatever you ask, so no absolute threshold in that
# space can separate a hit from a miss. It is not that 0.25 was the wrong
# number; any single number is wrong there.
#
# Subtracting the corpus mean before comparing removes the shared component and
# leaves the part that carries meaning. Same corpus, same queries:
#
#                                        raw max   centered max
#   "CuteBot lights"       (present)       0.878        0.759
#   "heart rate"           (present)       0.774        0.774
#   "how does the relay work" (absent)     0.636        0.350
#   "asdfgh zxcvbn qwerty"    (nonsense)   0.696        0.343
#
# Raw, nonsense outscores a real question. Centered, the two populations
# separate: over five queries with a true answer in the corpus and five
# without, real hits bottomed out at 0.512 and non-hits topped out at 0.423.
# 0.47 is the midpoint of that empty band, so it is the maximum-margin choice
# on the evidence rather than a round number.
_CENTERED_SEMANTIC_FLOOR = 0.47

# Used only when centring is not possible. Kept at the historical value so a
# store too small to have a meaningful centre behaves exactly as before.
_RAW_SEMANTIC_FLOOR = 0.25

# Below this many chunks the mean vector is dominated by whichever handful of
# documents happens to exist, so centring would subtract noise. A new install
# searching twenty memories keeps the old behaviour.
_MIN_CHUNKS_FOR_CENTERING = 200

# ── Corpus matrix cache ─────────────────────────────────────────────────────
#
# The centred document matrix does not depend on the query. Only the final
# mat-vec does. Measured on a copy of the reporter's 11,613-chunk store
# (384 dims, this machine):
#
#   SELECT every embedding BLOB     23.3 ms
#   join + decode + centre           9.1 ms
#   ------------------------------------------
#   rebuilt on EVERY query          32.4 ms
#   the mat-vec that actually uses the query    0.35 ms
#
# So 99% of the vector leg was recomputing a constant. The matrix is built
# once and kept, and the change detector below decides when it is no longer
# the corpus.
#
# The cost of keeping it is resident memory: n * dim * 4 bytes, which is
# 17.8 MB on that store and 154 MB at 100k chunks. Above this cap the matrix
# is still built and used for the query, it is simply not retained, so a very
# large store degrades to the old per-query cost instead of to an OOM. 100k
# chunks fits; that is deliberate, since it is the largest corpus anyone has
# reported.
_CORPUS_CACHE_MAX_BYTES = 256 * 1024 * 1024


@dataclass
class _CenteredCorpus:
    """The query-independent half of a centred vector scan, kept between
    queries.

    ``docs`` holds one unit-length row per chunk with the corpus centre
    already subtracted and the row renormalised, which is exactly what
    ``_centered_similarity`` used to build and throw away every query.
    Scoring a query against it is one mat-vec.

    ``docs`` is the ONLY matrix retained. It is produced by mutating the
    unit-normalised matrix in place rather than by allocating a second one,
    so a store never holds two copies of its corpus.
    """

    source_ids: list[str]
    docs: "np.ndarray"
    live: "np.ndarray"
    centroid: "np.ndarray"
    fingerprint: tuple

_SCHEMA_VERSION = 6  # v2026.5.34: D11 decay + D12 sync HLC columns


def _message_text(content: Any) -> str:
    """Flatten a chat message's ``content`` to plain text.

    OpenAI-shaped messages carry either a plain string or a list of typed
    content blocks (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``)
    once vision, screen-attach or file attachments are in play. Callers that
    want a human-readable excerpt must not assume the string shape:
    ``content[:120]`` on a list returns a *list*, and binding that to a TEXT
    column raises

        sqlite3.ProgrammingError: Error binding parameter 2:
        type 'list' is not supported

    which surfaced as a 500 on ``POST /api/conversations/save`` for any thread
    containing a multimodal turn, silently losing the whole conversation
    (operator report 2026-07-30).

    Image blocks are represented by a short placeholder rather than their
    base64 payload, which would otherwise dominate the preview.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = str(block.get("type", ""))
                if block_type in ("image_url", "input_image", "image", "image_base64"):
                    parts.append("[image]")
                else:
                    text = block.get("text") or block.get("content") or ""
                    if isinstance(text, str) and text:
                        parts.append(text)
        return " ".join(parts).strip()
    if content is None:
        return ""
    return str(content)


def _stable_knowledge_id(subject: str, predicate: str) -> str:
    """Deterministic id for a (subject, predicate) knowledge tuple.

    Used by :meth:`MemoryStore.knowledge_store` to keep two-brain CRDT
    convergence working: if both brains write ``("user", "name")``
    independently, they must produce the same row id so the receiving
    side's ``(subject, predicate)`` dedup gate recognises the
    incoming op as an update to the existing row rather than a brand
    new fact. A 12-hex-char SHA-256 prefix matches the legacy
    ``uuid4()[:12]`` width.
    """
    import hashlib

    digest = hashlib.sha256(f"{subject}\0{predicate}".encode("utf-8")).hexdigest()
    return digest[:12]

# ── Hybrid ranking (rewritten v2026.7.x) ────────────────────────────
#
# The previous blend was ``0.3 * fts_score + 0.7 * vec_score`` scaled by
# an exponential temporal factor, and it carried four independent
# sign/scale errors that all pushed the wrong way:
#
#   1. ``fts_score = 1/(1 + abs(rank))``. FTS5's ``rank`` is BM25:
#      negative, and *more* negative means a better match. ``abs()``
#      folded the sign away, so the ordering was exactly reversed.
#      Measured on a 28-row corpus, the weakest match scored 3.5x the
#      best one and came back first.
#   2. ``decay_factor`` was placed in the *exponent*. It is retention
#      strength in (0, 1] where 1.0 is pristine, so a nearly-forgotten
#      memory got a smaller decay rate and therefore ranked *higher*,
#      by 423x at 30 days.
#   3. The rate itself was 0.01/hour, a 69-hour half-life, which let a
#      one-hour-old garbage match beat a perfect match from 14 days ago.
#   4. The two legs' scores were never on a comparable scale to begin
#      with, so the 0.3/0.7 weights were tuning noise on top of noise.
#
# Reciprocal Rank Fusion (Cormack, Clarke & Buettcher, SIGIR 2009)
# replaces the blend. Each leg contributes ``1 / (K + position)`` for
# the documents it ranked, and the contributions are summed. It fuses
# incompatible scales correctly because it never touches the scores,
# only the orderings, which is precisely why error (1) becomes
# structurally impossible: a document's position in the BM25 ordering
# already carries the sign, so there is no sign left to get wrong.
# It also needs no tuning, which errors (3) and (4) show we are bad at.
RRF_K = 60  # Standard damping constant from the original RRF paper.

# RRF sums are tiny (a document ranked first in both legs scores
# 2/61 ≈ 0.033). ``_mmr_rerank_episodes`` subtracts a fixed 0.3 * 0.5
# session-overlap penalty from the relevance term, which would swamp a
# raw RRF score and turn the rerank into pure diversity. Scaling by
# ``(RRF_K + 1) / <number of legs that returned anything>`` maps the
# fused score onto [0, 1] (first place everywhere is 1.0) without
# changing any ordering, since it is a positive constant per query.
#
# Recency is then a *bounded additive* prior rather than the old
# multiplicative exponential. At 0.05 it can move a document by about
# three rank positions at the head of the list: enough to break ties
# between comparable matches, never enough to promote a weak match over
# a strong one, which is the failure mode error (3) produced.
RECENCY_PRIOR_WEIGHT = 0.05

# Hourly retention rate for the recency prior. Reconciled with
# ``memory.decay.DecayConfig.decay_rate``: both model the same
# Ebbinghaus curve over the same ``episodes`` rows, and they were
# 10x apart (0.01 here vs 0.001 there), so the search-time view of how
# fast a memory fades disagreed with the sweep that actually writes
# ``decay_factor``. 0.001/hour is a ~29-day half-life.
# ``tests/test_memory_ranking.py`` pins the two together so they cannot
# drift again.
DEFAULT_DECAY_RATE = 0.001


class MemoryStore:
    """
    The full FERAL memory layer with vector search, hybrid ranking,
    temporal decay, and multi-stage compaction.

    Async-native (v2026.5.33). All I/O methods are coroutines; call
    sites use ``await store.X(...)`` directly. Construction stays
    sync (boot DDL is one-shot).
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        vec_index: Optional[VectorIndexBackend] = None,
        conn_pool_size: int = 4,
    ):
        """Construct a MemoryStore.

        Parameters
        ----------
        db_path :
            SQLite DB path. Defaults to ``~/.feral/memory.db``.
        vec_index :
            Pluggable async vector index backend conforming to
            :class:`memory.vector_index_backends.VectorIndexBackend`.
            If ``None``, defaults to the sqlite-vec backend.
            ``BrainState.__init__`` reads ``settings.memory.backend``
            and injects the configured backend here — selecting
            ``chroma`` or ``qdrant`` in settings.yaml swaps this
            end-to-end.
        conn_pool_size :
            Size of the aiosqlite connection pool. Connections are
            created lazily on first ``_conn()`` call and reused across
            every memory operation. WAL mode lets multiple connections
            read in parallel; the pool eliminates the per-call
            ``aiosqlite.connect()`` + PRAGMA round-trips that would
            otherwise dominate latency. Default 4 is enough to
            saturate a typical brain workload without over-spawning
            worker threads.

        Boot-time SQL (CREATE TABLE / CREATE INDEX) runs synchronously
        through stdlib ``sqlite3``. This is intentional: ``__init__``
        is not async and the brain's boot wiring is sync. The async
        surface kicks in for every operation after construction.
        """
        if db_path is None:
            data_dir = feral_data_home()
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "memory.db")

        from pathlib import Path as _Path
        _db_path_obj = _Path(db_path)
        _enc_path_obj = _db_path_obj.with_name(_db_path_obj.name + ".enc")
        if _enc_path_obj.exists():
            try:
                from memory.at_rest import ensure_plaintext_db
                from security.vault import get_vault
                ensure_plaintext_db(vault=get_vault(), db_path=_db_path_obj)
            except Exception as exc:
                logger.warning(
                    "MemoryStore boot: ensure_plaintext_db failed (%s); "
                    "proceeding with whatever %s contains. Operator may "
                    "need to run `feral key recover` or restore the "
                    "plaintext backup at %s.",
                    exc, _db_path_obj,
                    _db_path_obj.with_name(_db_path_obj.name + ".bak.plaintext"),
                )

        #: Last failure from the vector leg of hybrid search, or None.
        #: Surfaced by the memory backend endpoint so a degraded
        #: semantic search cannot masquerade as "nothing matched".
        self._vector_leg_error: str | None = None
        #: Per-tier failures from the last :meth:`search_all` call, as
        #: ``[{"tier": ..., "error": ...}]``. Declared here rather than
        #: only being set by the aggregator so a caller can distinguish
        #: "no search has run yet" from "the last search was complete".
        self.last_search_degradations: list[dict] = []
        # Corpus mean vector, cached with the chunk count it was built from.
        # Recomputed when the corpus grows enough to move it; see
        # _centered_similarity for why it exists at all.
        self._centroid = None
        self._centroid_n = 0
        # Centred corpus matrix, rebuilt only when the corpus changes. See
        # _CORPUS_CACHE_MAX_BYTES for the measurement that motivated it and
        # _corpus_fingerprint for how a change is detected.
        self._corpus_cache: Optional[_CenteredCorpus] = None
        # Fingerprint of the corpus the last time the centroid was refreshed,
        # used by the sqlite-vec path, which needs the centre but must NOT
        # retain the matrix (saving that memory is the whole point of having
        # an index).
        self._centroid_fingerprint: Optional[tuple] = None
        # Bumped only when the fingerprint query itself fails, so an
        # unreadable fingerprint can never compare equal to a cached one.
        self._corpus_epoch = 0
        self.db_path = db_path
        self._working: dict[str, deque[dict]] = {}
        self._working_max = 50
        self._working_max_sessions = 500
        self._sync_engine = None
        self._about_me_store = None
        self._embedder = EmbeddingProvider()
        self._kg = None
        # Lane 05  (AUDIT-r14 finding 14): track fire-and-forget
        # extractor tasks so we can drain them on shutdown and so
        # asyncio's "Task was destroyed but it is pending" warning
        # doesn't fire when an event loop closes mid-extraction.
        self._bg_tasks: set[asyncio.Task] = set()

        # Async connection pool. Lazily populated on first acquire so
        # we can be constructed outside a running event loop (the brain
        # boot sequence is sync; the loop only starts later).
        self._pool_size = max(1, conn_pool_size)
        self._pool: Optional["asyncio.Queue[aiosqlite.Connection]"] = None
        self._pool_lock: Optional[asyncio.Lock] = None

        # ── stats() short-TTL cache + dedicated read connection ──
        # The dashboard polls /api/memory/stats continuously (~1Hz) via
        # /api/dashboard. With ~5k episodes + background services
        # (sync_scheduler / decay sweeper / proactive engine / etc.)
        # holding pool connections, the COUNT(*) round-trip used to
        # queue behind writers and trip the 2.5s safety budget every
        # poll, which surfaced as a flood of degraded-payload warnings
        # and a permanently-degraded dashboard. The cache below lets
        # rapid polls share a single COUNT round; the dedicated
        # read-only aiosqlite connection (opened lazily, separate from
        # the writer pool) ensures that round runs on a connection
        # WAL readers never have to wait on. Together they take the
        # steady-state stats path off the writer-contention surface.
        self._stats_cache: Optional[dict] = None
        self._stats_cache_at: float = 0.0
        self._stats_cache_lock: Optional[asyncio.Lock] = None
        self._stats_read_conn: Optional[aiosqlite.Connection] = None
        self._stats_read_lock: Optional[asyncio.Lock] = None

        self._init_db()

        if vec_index is None:
            from memory.vector_index_backends.sqlite_vec import SQLiteVecIndex
            vec_index = SQLiteVecIndex(
                dim=self._embedder.dimension,
                db_path=self.db_path,
                table_name="vec_chunks",
            )
        self._vec_index: VectorIndexBackend = vec_index
        self._embed_queue = EmbedQueue(self._embedder, vec_index)
        self._backend_id = getattr(vec_index, "backend_id", "unknown")

        self._init_knowledge_graph()
        logger.info(
            "Memory store v%d at %s | embeddings: %s | backend: %s (indexed=%s)",
            _SCHEMA_VERSION, self.db_path, self._embedder.provider_name,
            self._backend_id, vec_index.indexed,
        )

    def start_background_tasks(self) -> None:
        """Start the embed queue processor. Must be called from within
        a running event loop after construction."""
        self._embed_queue.start()
        logger.info("Embed queue started")

    def encryption_status(self) -> dict:
        """Snapshot of memory-at-rest state for ``feral memory status``
        / ``feral doctor`` consumers.

        Pure file-existence probe — never touches the vault.
        """
        from memory.at_rest import encryption_status as _encryption_status
        return _encryption_status(self.db_path)

    async def drain_background_tasks(self, timeout: float = 5.0) -> None:
        """Wait for outstanding fire-and-forget tasks (AboutMe extractor
        runs spawned by ``episode_save``) to finish.

        Called from the brain shutdown path so we don't leak the
        "Task was destroyed but it is pending" asyncio warning.
        Tests that exercise ``episode_save`` should call this in a
        finally clause to keep the loop clean.
        """
        if not self._bg_tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*list(self._bg_tasks), return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "drain_background_tasks: %d tasks still pending after %.1fs",
                len(self._bg_tasks), timeout,
            )

    def _init_knowledge_graph(self):
        try:
            from memory.knowledge_graph import KnowledgeGraph
            self._kg = KnowledgeGraph(self.db_path, self._embedder)
            # v2026.5.35 (F1) — back-reference so the KG can log every
            # write to the sync WAL with HLC. Without this, KG-native
            # writes (``add_entity``/``add_relation``) wouldn't
            # replicate across federated brains; with it, the unified
            # KG inherits the D12 LWW semantics from PR 2.
            self._kg._store = self
            kg_stats = self._kg.stats()
            logger.info(
                "Knowledge graph: %d entities, %d relations",
                kg_stats.get("entities", 0), kg_stats.get("relations", 0),
            )
        except Exception as e:
            logger.warning("Knowledge graph init failed: %s", e)

    @property
    def kg(self):
        return self._kg

    @property
    def embedder(self) -> EmbeddingProvider:
        return self._embedder

    def set_sync_engine(self, engine):
        self._sync_engine = engine

    def set_about_me_store(self, about_me_store):
        """Attach an AboutMeStore so episode_save can auto-extract self-facts.

        The store reference stays optional — unit tests instantiate a bare
        MemoryStore without an about_me store attached, and both tiers work
        independently.
        """
        self._about_me_store = about_me_store

    def _log_sync(self, table: str, op_type: str, row_id: str, data: dict) -> str:
        """Synchronous WAL log. Used by the few non-async writers
        that survive in the codebase (boot path, peer-applied changes,
        unit tests). Async hot paths MUST use :meth:`_log_sync_async`
        — calling this from inside an ``await`` blocks the event
        loop on the underlying ``sqlite3`` fsync (~tens of ms on a
        slow disk, the dominant slow-callback offender flagged by
        AUDIT-r14 finding 14).

        Returns the HLC string so the caller can persist
        ``hlc_string`` into the row column — without that the
        receiving side has no basis for the D12 LWW comparison. An
        empty string is returned when sync is disabled or the WAL
        append failed (the row still lands locally; replication just
        won't carry an HLC).
        """
        if not self._sync_engine:
            return ""
        try:
            return self._sync_engine.log_operation(table, op_type, row_id, data) or ""
        except Exception as exc:
            logger.debug("_log_sync swallowed exception: %s", exc)
            return ""

    async def _log_sync_async(
        self, table: str, op_type: str, row_id: str, data: dict
    ) -> str:
        """Async-offloaded WAL log. Same contract as :meth:`_log_sync`
        but the underlying sqlite3 ``INSERT OR REPLACE`` runs on a
        worker thread so the calling coroutine yields control while
        the disk fsync resolves. Used by every async write path on
        ``MemoryStore`` — episodes, knowledge graph entities/relations,
        knowledge entries — to keep the event loop free.
        """
        if not self._sync_engine:
            return ""
        try:
            return (
                await self._sync_engine.log_operation_async(
                    table, op_type, row_id, data
                )
                or ""
            )
        except Exception as exc:
            logger.debug("_log_sync_async swallowed exception: %s", exc)
            return ""

    async def _conn(self) -> aiosqlite.Connection:
        """Acquire a pooled aiosqlite connection.

        Connections live in ``self._pool`` for the lifetime of the
        store. Each one has WAL mode + a 5s busy timeout already set,
        so a caller pays zero connection-open overhead on the hot path
        — every memory operation amortises to a single SQL round-trip.
        WAL allows the pool's reader connections to run in parallel.

        Callers MUST release with :meth:`_release` in a ``finally``
        block. The existing pattern::

            conn = await self._conn()
            try:
                ...
            finally:
                await self._release(conn)

        is enforced across every method in this file.

        Lazy initialisation: the first ``_conn()`` inside a running
        event loop creates the pool. We can't build the pool in
        ``__init__`` because the loop isn't up yet at brain boot.
        """
        if self._pool is None:
            if self._pool_lock is None:
                self._pool_lock = asyncio.Lock()
            async with self._pool_lock:
                if self._pool is None:
                    pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(
                        maxsize=self._pool_size
                    )
                    for _ in range(self._pool_size):
                        c = await aiosqlite.connect(self.db_path)
                        # aiosqlite uses a non-daemon worker Thread per
                        # connection; an orphaned pool would block
                        # process exit (tests + CI shutdown). Mark the
                        # thread daemon so the interpreter can exit
                        # even if aclose() is missed. Production code
                        # paths still call aclose() for an orderly
                        # shutdown.
                        try:
                            c._thread.daemon = True  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        c.row_factory = aiosqlite.Row
                        await c.execute("PRAGMA journal_mode=WAL")
                        await c.execute("PRAGMA busy_timeout=5000")
                        await pool.put(c)
                    self._pool = pool
        return await self._pool.get()

    async def _release(self, conn: Optional[aiosqlite.Connection]) -> None:
        """Return a pooled connection to the pool.

        Tolerates ``None`` and a closed pool (during shutdown) so the
        canonical ``try/finally`` pattern stays correct under every
        unwind path.
        """
        if conn is None or self._pool is None:
            return
        try:
            self._pool.put_nowait(conn)
        except asyncio.QueueFull:
            # Pool was resized down or a stray connection appeared.
            # Close it rather than dropping silently — leaking a
            # SQLite connection holds a file lock.
            #
            # This used to recurse into ``self._release(conn)``, which
            # re-hit the full queue every time: measured 494 frames
            # deep, then ``RecursionError`` swallowed by the broad
            # ``except``, so the connection was never closed and the
            # lock was held anyway, i.e. the exact opposite of what
            # the comment above promises. Close it, for real.
            logger.warning(
                "Connection pool full on release; closing surplus connection"
            )
            try:
                await conn.close()
            except Exception as exc:
                # Reported, not hidden. ``_release`` runs inside the
                # ``finally`` of every read/write in this file, so a
                # raise here would replace whatever exception the caller
                # was already unwinding with an unrelated one.
                logger.warning("Failed to close surplus connection: %s", exc)

    async def _get_stats_read_conn(self) -> aiosqlite.Connection:
        """Lazy-open a dedicated read-only aiosqlite connection used
        only by :meth:`stats` COUNT queries.

        The writer pool can be fully claimed by the brain's background
        services (sync scheduler, decay sweeper, proactive engine,
        learner, cron, screen loop) — each of those holds a pool
        connection across a multi-statement transaction. The dashboard
        polls ``/api/memory/stats`` ~1Hz, and queueing the COUNTs
        behind those writers is what drove the 2.5s-budget flood. WAL
        mode lets readers run concurrently with a writer; opening this
        connection ``mode=ro`` keeps it permanently outside the writer
        pool so the COUNTs never have to wait on a busy/reserved lock.

        The connection is held for the lifetime of the store so we
        don't pay open-cost on every poll; :meth:`aclose` drops it.
        """
        if self._stats_read_conn is not None:
            return self._stats_read_conn
        if self._stats_read_lock is None:
            self._stats_read_lock = asyncio.Lock()
        async with self._stats_read_lock:
            if self._stats_read_conn is not None:
                return self._stats_read_conn
            uri = f"file:{self.db_path}?mode=ro"
            c = await aiosqlite.connect(uri, uri=True)
            try:
                c._thread.daemon = True  # type: ignore[attr-defined]
            except Exception:
                pass
            c.row_factory = aiosqlite.Row
            try:
                # ``query_only=ON`` is belt-and-braces: even if some
                # future caller mistakenly passes a write statement on
                # this connection, SQLite refuses it instead of
                # silently acquiring a write lock and breaking the
                # "stats never blocks" contract.
                await c.execute("PRAGMA query_only=ON")
                await c.execute("PRAGMA busy_timeout=2000")
            except Exception:
                pass
            self._stats_read_conn = c
            return c

    # ─────────────────────────────────────────────
    # Schema
    # ─────────────────────────────────────────────

    def close(self) -> None:
        """Shut down background tasks and release resources.

        Stays synchronous to preserve the existing boot/shutdown
        wiring (called from non-async paths). Pool draining is
        delegated to :meth:`aclose` for any caller already inside an
        event loop that wants a clean async teardown.
        """
        try:
            self._embed_queue.stop()
        except Exception:
            pass

    async def aclose(self) -> None:
        """Async-native shutdown: drain in-flight fire-and-forget
        access-tracking tasks, stop the embed queue, then close every
        pooled aiosqlite connection. Safe to call multiple times.

        Access-tracking tasks are drained first so they don't try to
        acquire a connection from a closed pool. Pending tasks get a
        500 ms grace window; anything still in flight after that is
        cancelled and joined to avoid leaving "Task was destroyed but
        it is pending" warnings on interpreter shutdown.
        """
        access_tasks = getattr(self, "_access_tasks", None)
        if access_tasks:
            pending = [t for t in access_tasks if not t.done()]
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=0.5,
                    )
                except asyncio.TimeoutError:
                    for t in pending:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            access_tasks.clear()
        try:
            self._embed_queue.stop()
        except Exception:
            pass
        # Drop the dedicated stats read connection if we opened one.
        # It lives outside the pool, so the pool drain below never
        # touches it.
        stats_conn = self._stats_read_conn
        self._stats_read_conn = None
        if stats_conn is not None:
            try:
                await stats_conn.close()
            except Exception:
                pass
        pool = self._pool
        if pool is None:
            return
        self._pool = None
        while not pool.empty():
            try:
                conn = pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await conn.close()
            except Exception:
                pass

    async def refresh(self) -> dict:
        """Re-validate the on-disk memory + sync WAL after suspected corruption.

        Returns a dict shaped like:
            {"ok": True, "memory_db": "ok", "sync_wal": "ok"}                       # healthy
            {"ok": False, "error": "wal_corruption", "memory_db": "...", "sync_wal": "..."}  # recoverable

        The caller (UI banner, sync_with_peer pre-flight, chaos test) is
        expected to refuse to apply remote changes until refresh() returns
        ok=True. The function never raises — it always returns a dict so
        the failure mode is "surface the error", never "crash the brain".
        """
        result: dict = {"ok": True}

        memory_status = "ok"
        memory_detail = ""
        try:
            conn = await aiosqlite.connect(self.db_path)
        except sqlite3.Error as exc:
            memory_status = "open_failed"
            memory_detail = str(exc)
        else:
            try:
                async with conn.execute("PRAGMA integrity_check") as cur:
                    rows = await cur.fetchall()
                statuses = [r[0] for r in rows] if rows else []
                if statuses != ["ok"]:
                    memory_status = "corruption"
                    memory_detail = "; ".join(statuses) or "integrity_check returned no rows"
            except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
                memory_status = "corruption"
                memory_detail = str(exc)
            finally:
                # This connection was opened here, not taken from the
                # pool, so it must be closed here. ``_release`` used to
                # graft it *into* the pool, growing the pool past its
                # configured size with a connection that never had the
                # pool's row_factory or PRAGMAs applied, or (once the
                # pool was full) hitting the recursion bug above. This
                # runs on every inbound sync, so it compounded fast.
                try:
                    await conn.close()
                except Exception as exc:
                    # refresh()'s contract is that it never raises: it
                    # always returns a dict so callers surface the error
                    # rather than crash the brain. Log and carry on.
                    logger.warning("refresh() failed to close its connection: %s", exc)
        result["memory_db"] = memory_status
        if memory_status != "ok":
            result["memory_db_detail"] = memory_detail
            result["ok"] = False
            result["error"] = "memory_db_corruption"

        if self._sync_engine is not None:
            try:
                wal_check = self._sync_engine._wal.integrity_check()
            except Exception as exc:
                wal_check = {"ok": False, "error": "wal_check_raised", "detail": str(exc)}
            if wal_check.get("ok"):
                result["sync_wal"] = "ok"
            else:
                result["sync_wal"] = wal_check.get("error", "wal_corruption")
                result["sync_wal_detail"] = wal_check.get("detail", "")
                result["ok"] = False
                result["error"] = wal_check.get("error", "wal_corruption")

        return result

    def _init_db(self):
        """Create / migrate the schema. Sync sqlite3 because this runs
        once at construction time, before the event loop is even up.

        WAL mode is set here, eagerly, so the database is in WAL the
        moment any reader (the dedicated stats read connection in
        particular) opens it. Setting it lazily on the first pool
        connection used to leave a brief window where a read-only
        connection could open against a delete-mode journal and queue
        behind a writer; setting it during boot DDL closes that race.
        ``synchronous=NORMAL`` is the WAL-recommended pairing for
        local-first workloads — durable across crashes, half the fsync
        traffic of FULL.
        """
        # Checked before the first statement, not lazily at the first
        # CREATE VIRTUAL TABLE. Without FTS5 this method used to die at
        # line 890 with `sqlite3.OperationalError: no such module: fts5`
        # (reproduced on python-build-standalone 3.11.13 / SQLite 3.49.1,
        # the build an earlier desktop-bundling spike selected). That
        # traceback points at a triple-quoted string inside store.py and
        # never names the interpreter, so it reads like a FERAL bug.
        # It also fired *after* `notes` and its triggers were created,
        # leaving a database whose triggers reference a missing FTS
        # table. Failing first keeps the file untouched.
        require_fts5("FERAL's memory store")

        conn = sqlite3.connect(self.db_path)
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError as exc:
                # An encrypted or freshly-restored DB can occasionally
                # refuse the pragma until a transaction warms it up.
                # Don't fail boot — the per-connection pragma in
                # ``_conn`` will still flip it once the pool warms.
                logger.debug("_init_db: pragma journal_mode=WAL deferred (%s)", exc)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    importance TEXT DEFAULT 'normal',
                    source TEXT DEFAULT 'user',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
                USING fts5(content, tags, tokenize='porter')
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                    INSERT INTO notes_fts(rowid, content, tags)
                    VALUES (new.rowid, new.content, new.tags);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                    DELETE FROM notes_fts WHERE rowid = old.rowid;
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS notes_fts_update AFTER UPDATE ON notes BEGIN
                    DELETE FROM notes_fts WHERE rowid = old.rowid;
                    INSERT INTO notes_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
                END
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    emotions TEXT DEFAULT '[]',
                    location TEXT DEFAULT '',
                    participants TEXT DEFAULT '[]',
                    importance REAL DEFAULT 0.5,
                    created_at REAL NOT NULL,
                    decay_factor REAL DEFAULT 1.0
                )
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts
                USING fts5(summary, detail, tokenize='porter')
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
                    INSERT INTO episodes_fts(rowid, summary, detail)
                    VALUES (new.rowid, new.summary, new.detail);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
                    DELETE FROM episodes_fts WHERE rowid = old.rowid;
                END
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(created_at DESC)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    source TEXT DEFAULT 'user',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
                USING fts5(subject, predicate, object, tokenize='porter')
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
                    INSERT INTO knowledge_fts(rowid, subject, predicate, object)
                    VALUES (new.rowid, new.subject, new.predicate, new.object);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
                    DELETE FROM knowledge_fts WHERE rowid = old.rowid;
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS knowledge_fts_update AFTER UPDATE ON knowledge BEGIN
                    DELETE FROM knowledge_fts WHERE rowid = old.rowid;
                    INSERT INTO knowledge_fts(rowid, subject, predicate, object) VALUES (new.rowid, new.subject, new.predicate, new.object);
                END
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_subject ON knowledge(subject)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_log (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    args TEXT DEFAULT '{}',
                    result_status TEXT NOT NULL,
                    result_summary TEXT DEFAULT '',
                    latency_ms REAL DEFAULT 0,
                    user_feedback TEXT DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_execlog_skill ON execution_log(skill_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_execlog_time ON execution_log(created_at DESC)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_chunks (
                    id TEXT PRIMARY KEY,
                    source_table TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    chunk_index INTEGER DEFAULT 0,
                    text_content TEXT NOT NULL,
                    embedding BLOB,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON memory_chunks(source_table, source_id)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS wiki_pages (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body_markdown TEXT NOT NULL,
                    source_refs TEXT DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_kind ON wiki_pages(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki_pages(updated_at DESC)")
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS wiki_pages_fts
                USING fts5(title, body_markdown, tokenize='porter')
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_pages_ai AFTER INSERT ON wiki_pages BEGIN
                    INSERT INTO wiki_pages_fts(rowid, title, body_markdown)
                    VALUES (new.rowid, new.title, new.body_markdown);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_pages_au AFTER UPDATE ON wiki_pages BEGIN
                    DELETE FROM wiki_pages_fts WHERE rowid = old.rowid;
                    INSERT INTO wiki_pages_fts(rowid, title, body_markdown)
                    VALUES (new.rowid, new.title, new.body_markdown);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_pages_ad AFTER DELETE ON wiki_pages BEGIN
                    DELETE FROM wiki_pages_fts WHERE rowid = old.rowid;
                END
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_snapshots (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    branch_name TEXT NOT NULL DEFAULT 'main',
                    label TEXT NOT NULL DEFAULT '',
                    working_json TEXT NOT NULL DEFAULT '[]',
                    history_json TEXT NOT NULL DEFAULT '[]',
                    source_snapshot_id TEXT,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_session ON session_snapshots(session_id, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_branch ON session_snapshots(branch_name, created_at DESC)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT 'New conversation',
                    preview TEXT NOT NULL DEFAULT '',
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)")

            # W3: thread management columns.
            #
            # ``title_custom`` is the fix for a rename that survived zero
            # autosaves. ``conversation_save`` derives a title from the
            # first user message whenever the caller passes none, and the
            # v2 client's 450ms autosave passes the SAME derived title on
            # every keystroke-settled change. The upsert then wrote it
            # over whatever the user had renamed the thread to, so the
            # rename was gone within half a second. Measured before the
            # fix on a throwaway store: save(title="My renamed thread")
            # then save() with no title left the row reading "hello
            # number 1 about pytest" again.
            #
            # A flag rather than a heuristic, because the derived title
            # and a user-typed title are byte-identical when the user
            # renames a thread to its own first message. Only the
            # explicit rename path sets it.
            self._add_column_if_missing(conn, "conversations", "title_custom", "INTEGER NOT NULL DEFAULT 0")
            self._add_column_if_missing(conn, "conversations", "pinned", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_pinned "
                "ON conversations(pinned DESC, updated_at DESC)"
            )

            # ─────────────────────────────────────────────────────────────
            # v2026.5.34 (PR 2 — feat/memory-v2-truth) schema additions.
            # Idempotent ALTER TABLE statements: SQLite raises
            # "duplicate column name" if the column already exists, which
            # we treat as a no-op so reruns on already-migrated
            # databases (existing installs + fresh CREATE TABLE paths
            # for the same release) succeed. The full versioned
            # migration framework lands in PR 4 (A6); this is the
            # bridge until then.
            # ─────────────────────────────────────────────────────────────
            self._add_column_if_missing(conn, "episodes", "last_accessed_at", "REAL DEFAULT 0")
            self._add_column_if_missing(conn, "episodes", "access_count", "INTEGER DEFAULT 0")
            self._add_column_if_missing(conn, "episodes", "forgotten_at", "REAL DEFAULT NULL")
            self._add_column_if_missing(conn, "episodes", "hlc_string", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "notes", "hlc_string", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "knowledge", "hlc_string", "TEXT DEFAULT ''")
            # ``execution_log`` is created lazily in ``log_execution``,
            # so ensure it exists before the ALTER. Done with the same
            # DDL as the runtime path so the table shape stays in sync.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_log (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    args TEXT NOT NULL DEFAULT '{}',
                    result_status TEXT NOT NULL DEFAULT 'unknown',
                    result_summary TEXT NOT NULL DEFAULT '',
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            self._add_column_if_missing(conn, "execution_log", "hlc_string", "TEXT DEFAULT ''")

            # F1 (v2026.5.35) — track whether each flat knowledge row
            # has been mirrored into the KG, so the bulk migration on
            # next boot is idempotent and incremental.
            self._add_column_if_missing(conn, "knowledge", "kg_migrated_at", "REAL DEFAULT 0")

            # D11 indexes:
            # (forgotten_at, decay_factor) — sweep query "find episodes
            # newly under the threshold" hits this composite directly.
            # (last_accessed_at) — supports the future "least-recently-
            # accessed first" recall heuristic and keeps decay sweep
            # cheap on multi-million-row stores.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_forgotten ON episodes(forgotten_at, decay_factor)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_last_accessed ON episodes(last_accessed_at)")

            self._repair_knowledge_fts(conn)

            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _repair_knowledge_fts(conn: sqlite3.Connection) -> bool:
        """Re-point ``knowledge_fts`` at the live ``knowledge`` table.

        The F1 migration ends with ``ALTER TABLE knowledge RENAME TO
        knowledge__deprecated``. SQLite rewrites every trigger that
        referenced the old name to reference the new one, so
        ``knowledge_ai`` / ``knowledge_ad`` / ``knowledge_fts_update`` all
        followed the table into deprecation and kept maintaining
        ``knowledge_fts`` from the corpse. ``_init_db`` then recreates an
        empty ``knowledge`` table, but its ``CREATE TRIGGER IF NOT EXISTS``
        statements are no-ops, because triggers with those names already
        exist on the renamed table.

        Found on the real store: ``knowledge`` 0 rows,
        ``knowledge__deprecated`` 29 rows, ``knowledge_fts`` 29 rows
        indexing the deprecated ones. That is not merely a stale index, it
        is a WRONG-ANSWER generator: ``_knowledge_search_flat`` joins
        ``knowledge_fts.rowid`` to ``knowledge.rowid``, and rowids in a
        freshly recreated table restart at 1, so the first new row written
        on the legacy path (``memory.kg.unified = false``) inherits the
        deprecated row 1's search terms and is returned for queries that
        match text it does not contain.

        Idempotent: does nothing once the triggers are bound to
        ``knowledge``. Returns True when it repaired something.
        """
        misbound = [
            row[0]
            for row in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' "
                "AND name IN ('knowledge_ai', 'knowledge_ad', 'knowledge_fts_update')"
            ).fetchall()
            if row[1] != "knowledge"
        ]
        if not misbound:
            return False

        for name in misbound:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
                INSERT INTO knowledge_fts(rowid, subject, predicate, object)
                VALUES (new.rowid, new.subject, new.predicate, new.object);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
                DELETE FROM knowledge_fts WHERE rowid = old.rowid;
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_fts_update AFTER UPDATE ON knowledge BEGIN
                DELETE FROM knowledge_fts WHERE rowid = old.rowid;
                INSERT INTO knowledge_fts(rowid, subject, predicate, object) VALUES (new.rowid, new.subject, new.predicate, new.object);
            END
        """)
        # Rebuild the index from the live table. knowledge_fts stores its
        # own content (it is not an external-content fts5 table), so the
        # 'rebuild' command is unavailable and the rows have to be
        # replaced by hand. The deprecated rows are not lost by this: the
        # F1 migration already ported every one of them into the KG
        # entities/relations tables, which is what knowledge_search reads
        # when memory.kg.unified is on.
        conn.execute("DELETE FROM knowledge_fts")
        conn.execute(
            "INSERT INTO knowledge_fts(rowid, subject, predicate, object) "
            "SELECT rowid, subject, predicate, object FROM knowledge"
        )
        conn.commit()
        logger.warning(
            "Repaired knowledge_fts: %s were bound to a deprecated table and "
            "have been re-pointed at the live `knowledge` table; the index was "
            "rebuilt from it.",
            ", ".join(sorted(misbound)),
        )
        return True

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
        """Idempotent ``ALTER TABLE ADD COLUMN``.

        SQLite refuses to add a column that already exists. The cheapest
        cross-version check is to inspect ``PRAGMA table_info`` and only
        emit the ALTER when the column is genuinely missing — this
        avoids both the spurious error log and the cost of catching the
        exception on the hot upgrade path.
        """
        cur = conn.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        if column in existing:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # ─────────────────────────────────────────────
    # Tier 1: Working Memory (in-RAM, sync)
    # ─────────────────────────────────────────────

    def working_push(self, session_id: str, entry: dict):
        if session_id not in self._working:
            if len(self._working) >= self._working_max_sessions:
                oldest = min(self._working, key=lambda s: self._working[s][-1]["ts"] if self._working[s] else 0)
                del self._working[oldest]
            self._working[session_id] = deque(maxlen=self._working_max)
        self._working[session_id].append({**entry, "ts": time.time()})

    def working_get(self, session_id: str, limit: int = 20) -> list[dict]:
        buf = self._working.get(session_id, deque())
        return list(buf)[-limit:]

    def working_context_string(self, session_id: str, limit: int = 10) -> str:
        entries = self.working_get(session_id, limit)
        if not entries:
            return ""
        lines = []
        for e in entries:
            role = e.get("role", "system")
            text = e.get("text", e.get("summary", ""))[:200]
            if text:
                lines.append(f"[{role}] {text}")
        return "\n".join(lines)

    def working_clear(self, session_id: str):
        self._working.pop(session_id, None)

    def working_replace(self, session_id: str, entries: list[dict]):
        buf = deque(maxlen=self._working_max)
        for item in entries[-self._working_max:]:
            entry = dict(item)
            entry.setdefault("ts", time.time())
            buf.append(entry)
        self._working[session_id] = buf

    # ─────────────────────────────────────────────
    # Conversation Threads (persistent chat history)
    # ─────────────────────────────────────────────

    async def conversation_save(self, conversation_id: str, messages: list[dict], title: str = "") -> dict:
        """Save/update a conversation thread."""
        now = time.time()
        preview = ""
        for msg in reversed(messages):
            if msg.get("role") == "user" and msg.get("content"):
                preview = _message_text(msg["content"])[:120]
                break
        if not title and messages:
            for msg in messages:
                if msg.get("role") == "user" and msg.get("content"):
                    title = _message_text(msg["content"])[:80]
                    break
        title = title or "New conversation"

        conn = await self._conn()
        try:
            # ``title`` here is always derived or caller-supplied on the
            # AUTOSAVE path. A thread the user renamed carries
            # ``title_custom = 1`` and keeps its own title: the upsert
            # must not clobber it. Use ``conversation_rename`` to change
            # a renamed thread's title.
            await conn.execute("""
                INSERT INTO conversations (id, title, preview, messages_json, message_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = CASE WHEN conversations.title_custom = 1
                                 THEN conversations.title
                                 ELSE excluded.title END,
                    preview = excluded.preview,
                    messages_json = excluded.messages_json,
                    message_count = excluded.message_count,
                    updated_at = excluded.updated_at
            """, (conversation_id, title, preview, json.dumps(messages[-500:]), len(messages), now, now))
            await conn.commit()
            # Report the title the row actually holds, not the one we
            # asked for. Returning the requested title would let the
            # client believe an autosave had renamed a pinned-title
            # thread and repaint the list with a name the store rejected.
            async with conn.execute(
                "SELECT title, title_custom, pinned FROM conversations WHERE id = ?",
                (conversation_id,),
            ) as cur:
                row = await cur.fetchone()
        finally:
            await self._release(conn)
        effective_title = row[0] if row else title
        return {
            "id": conversation_id,
            "title": effective_title,
            "title_custom": bool(row[1]) if row else False,
            "pinned": bool(row[2]) if row else False,
            "preview": preview,
            "message_count": len(messages),
            "updated_at": now,
        }

    async def conversation_rename(self, conversation_id: str, title: str) -> dict | None:
        """Set a user-chosen title and mark it as sticky.

        Marks ``title_custom`` so no later ``conversation_save`` derives
        a title over the top of it. ``updated_at`` is deliberately left
        alone: renaming a thread is not activity in it, and bumping the
        timestamp would jump the thread to the top of a
        recently-updated list the user did not touch.
        """
        clean = (title or "").strip()[:200]
        if not clean:
            return None
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "UPDATE conversations SET title = ?, title_custom = 1 WHERE id = ?",
                (clean, conversation_id),
            )
            await conn.commit()
            if not cur.rowcount:
                return None
        finally:
            await self._release(conn)
        return await self.conversation_get(conversation_id)

    async def conversation_set_pinned(self, conversation_id: str, pinned: bool) -> dict | None:
        """Pin or unpin a thread. Pinned threads sort first in the list."""
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "UPDATE conversations SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, conversation_id),
            )
            await conn.commit()
            if not cur.rowcount:
                return None
        finally:
            await self._release(conn)
        return await self.conversation_get(conversation_id)

    async def conversation_append(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        source: str = "",
        title: str = "",
    ) -> dict:
        """Append a single message to an existing conversation or
        create-and-append if the conversation doesn't exist.

        PR 9 gap-fill: voice realtime proxies call this on every final
        transcript so the conversation list shows voice sessions next
        to chat threads — not just live-only events that disappear on
        reconnect. The ``source`` field carries the channel id
        (``voice_realtime_openai``, ``voice_realtime_gemini``) so the
        UI can render a small badge on voice threads.
        """
        existing = await self.conversation_get(conversation_id) or {}
        messages = list(existing.get("messages", []) or [])
        messages.append({
            "id": f"m_{int(time.time() * 1000)}_{len(messages)}",
            "role": role,
            "content": content,
            "source": source,
            "ts": time.time(),
        })
        return await self.conversation_save(
            conversation_id, messages, title=title or existing.get("title", ""),
        )

    # Hard ceiling on one page of conversation metadata. A caller asking
    # for more gets this; ``conversation_page`` reports the real total
    # alongside so a UI can page rather than silently lose rows past the
    # cut, which is what "fetch with no limit" used to do at 50.
    CONVERSATION_PAGE_MAX = 200

    @staticmethod
    def _conversation_search_clause(query: str) -> tuple[str, list]:
        """Build the WHERE fragment for a thread search.

        Matches title, preview and the stored message blob so searching
        for a word the user said three turns in finds the thread. The
        blob scan is a LIKE over ``messages_json``: this is a
        single-user local store where the table is a few thousand rows
        at most, and ``conversations`` has no FTS index to lean on.
        """
        needle = (query or "").strip()
        if not needle:
            return "", []
        # Escape the LIKE metacharacters so a literal '%' typed into the
        # search box matches a '%' and not "everything".
        escaped = (
            needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        like = f"%{escaped}%"
        return (
            " WHERE (title LIKE ? ESCAPE '\\'"
            " OR preview LIKE ? ESCAPE '\\'"
            " OR messages_json LIKE ? ESCAPE '\\')",
            [like, like, like],
        )

    async def conversation_page(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        query: str = "",
    ) -> dict:
        """One page of conversation metadata plus the unfiltered total.

        Returns ``{"items": [...], "total": int, "limit": int,
        "offset": int, "has_more": bool}``. ``total`` is the count of
        rows MATCHING the query, so a client can render "51 threads"
        and page through all of them instead of stopping at whatever
        the first request happened to return.

        Pinned threads sort first, then most-recently-updated.
        """
        limit = max(1, min(int(limit or 25), self.CONVERSATION_PAGE_MAX))
        offset = max(0, int(offset or 0))
        where, params = self._conversation_search_clause(query)
        conn = await self._conn()
        try:
            async with conn.execute(
                f"SELECT COUNT(*) FROM conversations{where}", params,
            ) as cur:
                total_row = await cur.fetchone()
            async with conn.execute(
                "SELECT id, title, preview, message_count, created_at, updated_at, pinned, title_custom "
                f"FROM conversations{where} ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        total = int(total_row[0]) if total_row else 0
        return {
            "items": [self._conversation_row(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    @staticmethod
    def _conversation_row(r) -> dict:
        return {
            "id": r[0],
            "title": r[1],
            "preview": r[2],
            "message_count": r[3],
            "created_at": r[4],
            "updated_at": r[5],
            "pinned": bool(r[6]),
            "title_custom": bool(r[7]),
        }

    async def conversation_list(self, limit: int = 50) -> list[dict]:
        """List recent conversations (metadata only)."""
        page = await self.conversation_page(limit=limit, offset=0)
        return page["items"]

    async def conversation_get(self, conversation_id: str) -> dict | None:
        """Load a full conversation with messages."""
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT id, title, preview, messages_json, message_count, created_at, updated_at, "
                "pinned, title_custom FROM conversations WHERE id = ?",
                (conversation_id,),
            ) as cur:
                row = await cur.fetchone()
        finally:
            await self._release(conn)
        if not row:
            return None
        return {
            "id": row[0], "title": row[1], "preview": row[2],
            "messages": json.loads(row[3]) if row[3] else [],
            "message_count": row[4], "created_at": row[5], "updated_at": row[6],
            "pinned": bool(row[7]), "title_custom": bool(row[8]),
        }

    async def conversation_delete(self, conversation_id: str) -> bool:
        conn = await self._conn()
        try:
            await conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            await conn.commit()
        finally:
            await self._release(conn)
        # ``conversations`` is in ``SyncEngine._SYNC_ALLOWED_TABLES``, so a
        # peer is permitted to act on this row, and deleting a
        # conversation is a privacy action a user expects to reach every
        # brain they own. It was not logged: the audit of 2026-08-12 found
        # 16,184 WAL operations of which 0 were deletes, on any table.
        # (No insert flow replicates conversations yet, so today this is a
        # no-op on the peer; it is logged anyway because the invariant
        # under test is "every delete on a syncable table is logged", and
        # an unlogged delete is invisible the moment that flow lands.)
        await self._log_sync_async(
            "conversations", "delete", conversation_id, {"id": conversation_id},
        )
        return True

    async def snapshot_session(
        self,
        *,
        session_id: str,
        history: list[dict],
        label: str = "",
        branch_name: str = "main",
        source_snapshot_id: str = "",
    ) -> dict:
        snapshot_id = str(uuid4())[:12]
        now = time.time()
        working = list(self._working.get(session_id, deque()))
        conn = await self._conn()
        try:
            await conn.execute(
                """
                INSERT INTO session_snapshots
                (id, session_id, branch_name, label, working_json, history_json, source_snapshot_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    session_id,
                    branch_name or "main",
                    label or "",
                    json.dumps(working),
                    json.dumps(history[-200:]),
                    source_snapshot_id or None,
                    now,
                ),
            )
            await conn.commit()
        finally:
            await self._release(conn)
        return {
            "snapshot_id": snapshot_id,
            "session_id": session_id,
            "branch_name": branch_name or "main",
            "label": label or "",
            "created_at": now,
            "working_count": len(working),
            "history_count": len(history),
            "source_snapshot_id": source_snapshot_id or None,
        }

    async def list_snapshots(
        self,
        *,
        session_id: str = "",
        branch_name: str = "",
        limit: int = 50,
    ) -> list[dict]:
        lim = max(1, min(limit, 200))
        conn = await self._conn()
        try:
            if session_id and branch_name:
                sql = """
                    SELECT id, session_id, branch_name, label, source_snapshot_id, created_at
                    FROM session_snapshots
                    WHERE session_id = ? AND branch_name = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                params = (session_id, branch_name, lim)
            elif session_id:
                sql = """
                    SELECT id, session_id, branch_name, label, source_snapshot_id, created_at
                    FROM session_snapshots
                    WHERE session_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                params = (session_id, lim)
            elif branch_name:
                sql = """
                    SELECT id, session_id, branch_name, label, source_snapshot_id, created_at
                    FROM session_snapshots
                    WHERE branch_name = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                params = (branch_name, lim)
            else:
                sql = """
                    SELECT id, session_id, branch_name, label, source_snapshot_id, created_at
                    FROM session_snapshots
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                params = (lim,)
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            {
                "snapshot_id": r["id"],
                "session_id": r["session_id"],
                "branch_name": r["branch_name"],
                "label": r["label"],
                "source_snapshot_id": r["source_snapshot_id"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def get_snapshot(self, snapshot_id: str) -> Optional[dict]:
        conn = await self._conn()
        try:
            async with conn.execute(
                """
                SELECT id, session_id, branch_name, label, working_json, history_json, source_snapshot_id, created_at
                FROM session_snapshots
                WHERE id = ?
                """,
                (snapshot_id,),
            ) as cur:
                row = await cur.fetchone()
        finally:
            await self._release(conn)
        if not row:
            return None
        return {
            "snapshot_id": row["id"],
            "session_id": row["session_id"],
            "branch_name": row["branch_name"],
            "label": row["label"],
            "working": json.loads(row["working_json"] or "[]"),
            "history": json.loads(row["history_json"] or "[]"),
            "source_snapshot_id": row["source_snapshot_id"],
            "created_at": row["created_at"],
        }

    # ─────────────────────────────────────────────
    # Tier 2: Episodic Memory (with embeddings)
    # ─────────────────────────────────────────────

    async def episode_save(
        self,
        session_id: str,
        event_type: str,
        summary: str,
        detail: str = "",
        emotions: list[str] | None = None,
        location: str = "",
        participants: list[str] | None = None,
        importance: float = 0.5,
        created_at: float | None = None,
    ) -> dict:
        eid = str(uuid4())[:12]
        # ``created_at`` is when the thing HAPPENED, which is not always
        # when it was written. An ambient transcript queues on the phone
        # while the brain is off and normally arrives hours or days late;
        # timeline recall filters on created_at and nothing else
        # (skills/impl/timeline_fusion.py:192), so without this a
        # conversation from yesterday does not appear when the user asks
        # about yesterday.
        #
        # Safe for CRDT ordering: the HLC that drives last-write-wins is
        # minted from the local clock in SyncEngine._build_operation
        # (memory/sync.py:644) and never reads this value, so backdating
        # moves the datum without reordering the write.
        now = created_at if created_at is not None else time.time()
        emotions = emotions or []
        participants = participants or []

        # Sync log first so we know the HLC string and can persist it
        # into ``episodes.hlc_string`` in the same INSERT — that gives
        # the receiving side a stable comparator for D12 LWW.
        # AUDIT-r14 finding 14 fix: previously this was a sync sqlite3
        # commit on the event loop, which the slow-callback monitor
        # caught at >100ms on slow disks. Off-loaded to a worker thread.
        hlc = await self._log_sync_async("episodes", "insert", eid, {
            "id": eid, "session_id": session_id, "event_type": event_type,
            "summary": summary, "detail": detail, "importance": importance, "created_at": now,
        })

        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO episodes
                   (id, session_id, event_type, summary, detail, emotions, location, participants, importance, created_at, hlc_string)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (eid, session_id, event_type, summary, detail,
                 json.dumps(emotions), location, json.dumps(participants), importance, now, hlc),
            )
            await conn.commit()
        finally:
            await self._release(conn)

        text = f"{summary}\n{detail}".strip()
        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            self._embed_queue.enqueue(
                chunk_id=f"{eid}_c{i}", text=chunk,
                source_table="episodes", source_id=eid,
                chunk_index=i, db_path=self.db_path,
            )

        # AUDIT-r14 finding 14 fix: AboutMe extractor previously ran
        # synchronously here (regex matching + N sqlite3 INSERTs per
        # match) on the chat hot path. Schedule it as a fire-and-forget
        # background task so episode_save returns as soon as the WAL
        # commit lands; the extractor's failures are logged but never
        # surfaced — they're best-effort UX.
        # NOT on speech the operator did not type.
        #
        # Every AboutMe extractor pattern is first person: "I prefer X",
        # "I live in X", "My wife <Name>", "I work as X". On a chat
        # episode that "I" is the operator, which is the whole premise.
        # On an ambient_conversation episode it is whoever was talking,
        # and the operator is frequently not the one talking. A
        # colleague saying "I prefer tea", a stranger saying "my wife
        # Sarah", someone saying where they live, all became facts about
        # the OPERATOR at 0.5 confidence, and ideas_engine then asked
        # them to confirm it.
        #
        # Two things wrong with that, and the second is worse. The
        # profile fills with other people's preferences; and third
        # parties' families and home towns get filed under the
        # operator's identity by a device they did not know was
        # listening. Recording a conversation is not consent to mine the
        # other speaker for a personal profile.
        #
        # The transcript is still summarized, still searchable, still
        # produces commitments. It just does not silently rewrite who
        # the operator is.
        about_me_eligible = event_type not in _NO_SELF_MODEL_EVENT_TYPES
        if not about_me_eligible and text:
            logger.debug(
                "AboutMe extraction skipped for event_type=%r: the speech is "
                "not necessarily the operator's", event_type,
            )
        if self._about_me_store is not None and text and about_me_eligible:
            extractor = getattr(
                self._about_me_store, "extract_from_text_async", None
            )

            async def _run_extractor() -> None:
                try:
                    if extractor is not None:
                        await extractor(text)
                    else:
                        # Fall through to the sync extractor on a worker
                        # thread for stores that don't yet have the
                        # async variant (older test doubles).
                        await asyncio.to_thread(
                            self._about_me_store.extract_from_text, text
                        )
                except Exception as exc:
                    logger.debug("AboutMe auto-extractor failed silently: %s", exc)

            try:
                task = asyncio.create_task(_run_extractor())
                # Hold a strong reference so the GC doesn't yank the
                # task mid-flight (asyncio only keeps weak refs); the
                # discard callback removes the entry once the task
                # resolves so the set doesn't grow unboundedly.
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            except RuntimeError:
                # No running loop (synchronous test instantiation that
                # somehow ended up here) — run inline as a last resort.
                try:
                    self._about_me_store.extract_from_text(text)
                except Exception as exc:
                    logger.debug("AboutMe auto-extractor failed silently: %s", exc)

        return {"id": eid, "event_type": event_type, "summary": summary, "created_at": now}

    def _centered_docs(self, blobs, dim, min_chunks=None):
        """Build the centred document matrix. Nothing here uses the query.

        Returns ``(docs, live, centroid)`` or None. Split out of
        _centered_similarity so the result can be cached: it is the same
        matrix for every query and it cost 9.1ms to rebuild on the
        reporter's 11,613-chunk store, against 0.35ms for the mat-vec that
        actually consumes the query.

        Embeddings occupy a narrow cone rather than the whole sphere, so
        every vector here shares a large common component and raw cosine
        measures mostly that. Subtracting the corpus mean from both sides
        leaves the part that actually distinguishes documents. This is the
        standard remedy for anisotropy and it is what makes a relevance
        threshold possible at all: see the block comment on
        _CENTERED_SEMANTIC_FLOOR for the measurements on the store where
        this was found.
        """
        # min_chunks is lowered by callers that already hold a centre and only
        # need a few rows scored against it, rather than deriving one.
        floor_n = _MIN_CHUNKS_FOR_CENTERING if min_chunks is None else min_chunks
        if len(blobs) < floor_n:
            return None
        mat = np.frombuffer(b"".join(blobs), dtype=np.float32)
        mat = mat.reshape(len(blobs), -1)
        if mat.shape[1] != dim:
            return None

        # Zero vectors are real: four of this store's chunks are all-zero
        # from embedding failures. They must not drag the mean, and they
        # can never be a hit, so they are scored -1 rather than dropped,
        # which would break the caller's positional zip.
        norms = np.linalg.norm(mat, axis=1)
        live = norms > 0
        if int(live.sum()) < floor_n:
            return None

        unit = np.zeros_like(mat)
        unit[live] = mat[live] / norms[live][:, None]

        centroid = self._centroid
        # Only the full-corpus call may define the centre. A caller that
        # passes min_chunks is scoring a handful of rows the index already
        # chose, and deriving a "corpus mean" from fifteen documents would
        # subtract those documents from themselves and score everything
        # near zero.
        may_derive = min_chunks is None
        stale = (
            centroid is None
            or centroid.shape[0] != unit.shape[1]
            # Rebuild once the corpus has moved by more than 5 percent, so
            # a growing store does not keep a stale centre while a single
            # new episode does not trigger a recompute.
            or abs(len(blobs) - self._centroid_n) > max(50, self._centroid_n * 0.05)
        )
        if stale and may_derive:
            centroid = unit[live].mean(axis=0)
            self._centroid = centroid
            self._centroid_n = len(blobs)
        elif centroid is None or centroid.shape[0] != unit.shape[1]:
            return None

        # In place, so ``unit`` IS ``docs``. The old ``unit - centroid``
        # allocated a second full matrix (17.8 MB on the reporter's store)
        # only to drop the first one a line later. Same float32 arithmetic,
        # same result, one allocation.
        unit -= centroid
        docs = unit
        dn = np.linalg.norm(docs, axis=1)
        dn[dn == 0] = 1.0
        docs /= dn[:, None]
        return docs, live, centroid

    @staticmethod
    def _score_centered(docs, live, centroid, query_vec):
        """Score a prepared centred matrix against one query. One mat-vec.

        Returns scores comparable to _CENTERED_SEMANTIC_FLOOR, or None when
        the query itself cannot be centred, in which case the caller falls
        back to raw cosine and the old floor.
        """
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        if q.shape[0] != centroid.shape[0]:
            return None
        qn = np.linalg.norm(q)
        if qn == 0:
            return None
        qc = (q / qn) - centroid
        qcn = np.linalg.norm(qc)
        if qcn == 0:
            return None
        qc /= qcn

        scores = docs @ qc
        scores[~live] = -1.0
        return scores

    def _centered_similarity(self, query_vec, blobs, min_chunks=None):
        """Cosine against the corpus with its shared direction removed.

        Returns per-blob scores comparable to _CENTERED_SEMANTIC_FLOOR, or
        None when the corpus is too small for a mean vector to mean anything,
        in which case the caller falls back to raw cosine and the old floor.

        Uncached: every call rebuilds the matrix. Kept for the small-row
        callers (the sqlite-vec path re-scores the handful of ids the index
        returned, where there is nothing worth caching). The full-corpus
        scan goes through :meth:`_centered_corpus` instead.
        """
        try:
            q = np.asarray(query_vec, dtype=np.float32).ravel()
            built = self._centered_docs(blobs, int(q.shape[0]), min_chunks)
            if built is None:
                return None
            docs, live, centroid = built
            return self._score_centered(docs, live, centroid, query_vec)
        except Exception as exc:
            # Falling back to raw cosine is worse but not wrong, and a broken
            # relevance floor must not take out search entirely.
            logger.warning("Centered similarity unavailable, using raw: %s", exc)
            return None

    async def _corpus_fingerprint(self, conn, source_table: str = "episodes"):
        """Cheap change detector for the embedding corpus.

        Returns a tuple that differs whenever the corpus may have changed,
        and is stable while it has not. This is what lets the centred matrix
        be cached instead of rebuilt per query, so it has to be both cheap
        and honest.

        Cheap. Measured on the reporter's store, this machine:

            SELECT COUNT(*), MAX(rowid) ... WHERE source_table=?   0.39 ms
            (for comparison) SELECT every embedding BLOB          23.3 ms

        The COUNT is 0.39ms only because ``idx_chunks_source`` covers it.
        Adding ``AND embedding IS NOT NULL`` makes it read every table row
        and costs 16ms, which would eat most of what the cache saves, so
        the predicate is deliberately left off: the count is used to detect
        change, never as the number of usable vectors.

        Honest, per writer. Both values are read from SQLite rather than
        from process state, so a write from any connection or any other
        process counts:

        * INSERT of a new chunk moves COUNT and MAX(rowid).
        * INSERT OR REPLACE of an existing chunk (the embed queue, at
          ``memory/embeddings.py``) deletes and reinserts, taking a fresh
          rowid, so MAX(rowid) moves.
        * DELETE (``memory/decay.py``) moves COUNT.
        * UPDATE of an embedding in place moves NEITHER, and is therefore
          not detected. The only such writer is ``feral memory reembed``
          (``cli/memory_cmd.py``), which runs in its own process and already
          ends by printing "Restart the brain to pick it up." That contract
          predates this cache; the cache does not weaken it.

        ``PRAGMA data_version`` was tried here and rejected. It reports
        commits by other connections on ANY table, and ``_bump_access``
        commits an ``UPDATE episodes`` after every single search, so it
        changed on essentially every query and the cache never hit.
        """
        try:
            async with conn.execute(
                "SELECT COUNT(*), MAX(rowid) FROM memory_chunks "
                "WHERE source_table = ?",
                (source_table,),
            ) as cur:
                row = await cur.fetchone()
            count = int(row[0] or 0) if row else 0
            max_rowid = int(row[1] or 0) if row else 0
        except Exception as exc:
            # A fingerprint that cannot be read must never produce a stale
            # answer, so return a value that can never equal a cached one.
            logger.debug("Corpus fingerprint unavailable: %s", exc)
            self._corpus_epoch += 1
            return (source_table, -1, -1, self._corpus_epoch)
        return (source_table, count, max_rowid, 0)

    async def _centered_corpus(self, conn, source_table: str = "episodes"):
        """The centred matrix for the whole corpus, built at most once per
        change to it.

        Returns a :class:`_CenteredCorpus`, or None when centring is not
        possible (corpus too small, mixed dimensions, no embeddings), in
        which case the caller falls back to raw cosine and the old floor.
        """
        fingerprint = await self._corpus_fingerprint(conn, source_table)
        cached = self._corpus_cache
        if cached is not None and cached.fingerprint == fingerprint:
            return cached

        if fingerprint[1] < _MIN_CHUNKS_FOR_CENTERING and fingerprint[1] >= 0:
            # Cannot possibly clear the centring floor, because the count is
            # taken without the NOT NULL predicate and so is an upper bound
            # on the number of usable vectors. Skip the 23ms fetch entirely.
            self._corpus_cache = None
            return None

        async with conn.execute(
            "SELECT source_id, embedding FROM memory_chunks "
            "WHERE source_table = ? AND embedding IS NOT NULL",
            (source_table,),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            self._corpus_cache = None
            return None

        source_ids = [r["source_id"] for r in rows]
        blobs = [r["embedding"] for r in rows]
        dim = len(blobs[0]) // 4
        try:
            built = self._centered_docs(blobs, dim)
        except Exception as exc:
            logger.warning("Centered similarity unavailable, using raw: %s", exc)
            built = None
        # Release the blob list and the buffer ``np.frombuffer`` viewed before
        # the caller does anything else, so peak RSS during a rebuild is not
        # the matrix plus a second copy of it as bytes.
        del blobs, rows
        if built is None:
            self._corpus_cache = None
            return None

        docs, live, centroid = built
        entry = _CenteredCorpus(
            source_ids=source_ids,
            docs=docs,
            live=live,
            centroid=centroid,
            fingerprint=fingerprint,
        )
        self._centroid_fingerprint = fingerprint
        if docs.nbytes <= _CORPUS_CACHE_MAX_BYTES:
            self._corpus_cache = entry
        else:
            # Used for this query, then dropped. See _CORPUS_CACHE_MAX_BYTES.
            self._corpus_cache = None
            logger.info(
                "Corpus matrix is %.0f MB (%d chunks), above the %.0f MB cache "
                "cap, so it is rebuilt per query. Installing sqlite-vec would "
                "keep the vectors on disk instead.",
                docs.nbytes / 1e6, len(source_ids),
                _CORPUS_CACHE_MAX_BYTES / 1e6,
            )
        return entry

    async def _ensure_centroid(self, conn, source_table: str = "episodes") -> bool:
        """Make ``self._centroid`` reflect the current corpus. Returns False
        when the corpus cannot be centred at all.

        For callers that need the centre but not the matrix. When the corpus
        fingerprint is unchanged the centre provably has not moved, so this
        is two cheap queries and nothing else. When it has changed, the
        matrix is built exactly as before and dropped on return.
        """
        fingerprint = await self._corpus_fingerprint(conn, source_table)
        if self._centroid is not None and self._centroid_fingerprint == fingerprint:
            return True
        if 0 <= fingerprint[1] < _MIN_CHUNKS_FOR_CENTERING:
            return False

        async with conn.execute(
            "SELECT embedding FROM memory_chunks "
            "WHERE source_table = ? AND embedding IS NOT NULL",
            (source_table,),
        ) as cur:
            blobs = [r["embedding"] for r in await cur.fetchall()]
        if not blobs:
            return False
        try:
            built = self._centered_docs(blobs, len(blobs[0]) // 4)
        except Exception as exc:
            logger.warning("Centered similarity unavailable, using raw: %s", exc)
            return False
        if built is None:
            return False
        self._centroid_fingerprint = fingerprint
        return True

    async def _centered_filter(self, conn, query_vec, chunk_ids):
        """Centered scores for specific chunk ids, keyed by id.

        The sqlite-vec index ranks by raw cosine, which is fine because the
        shared component is near-constant so it barely perturbs the order.
        What raw cosine cannot do is answer "is this good enough to return",
        so the returned handful is re-scored here against the corpus centre.
        """
        if not chunk_ids:
            return {}
        try:
            # The centre is refreshed only when the corpus has actually
            # changed. This used to re-read every embedding in the store and
            # rebuild the whole matrix on EVERY indexed query (23.3ms + 9.1ms
            # on the reporter's 11,613-chunk store) purely to recompute a mean
            # vector that had not moved. The matrix is deliberately NOT
            # retained here: keeping the corpus out of RAM is the reason to
            # run an index at all.
            centred = await self._ensure_centroid(conn)

            placeholders = ",".join("?" for _ in chunk_ids)
            async with conn.execute(
                f"SELECT id, embedding FROM memory_chunks WHERE id IN ({placeholders})",
                tuple(chunk_ids),
            ) as cur:
                rows = await cur.fetchall()
        except Exception as exc:
            logger.warning("Centered re-scoring failed, keeping raw hits: %s", exc)
            return {cid: 1.0 for cid in chunk_ids}

        if not centred:
            return {cid: 1.0 for cid in chunk_ids}

        ids = [r["id"] for r in rows]
        # Score only the handful the index returned against that centre.
        scores = self._centered_similarity(
            query_vec, [r["embedding"] for r in rows], min_chunks=1,
        )
        if scores is None:
            return {cid: 1.0 for cid in chunk_ids}
        return {
            cid: float(s)
            for cid, s in zip(ids, scores)
            if float(s) > _CENTERED_SEMANTIC_FLOOR
        }

    async def episode_search_hybrid(
        self,
        query: str,
        limit: int = 10,
        *,
        include_forgotten: bool = False,
        fts_mode: str = FTS_STRICT,
    ) -> list[dict]:
        """Hybrid search: FTS5 text + vector similarity fused by
        Reciprocal Rank Fusion, plus a bounded recency prior.

        ``relevance_score`` is ``rrf_norm * Σ 1/(RRF_K + position)`` over
        the legs that returned anything, in [0, 1], plus at most
        :data:`RECENCY_PRIOR_WEIGHT` for a pristine, brand-new memory.
        Scores are comparable within a query, not across queries.

        Uses sqlite-vec indexed search when available, numpy fallback otherwise.

        Excludes episodes whose ``forgotten_at`` is set by default. Set
        ``include_forgotten=True`` for admin / recall-eligibility
        queries that need to see the full set.

        Every returned episode is access-tracked: a fire-and-forget
        background task bumps ``last_accessed_at`` + ``access_count``
        so the decay sweep gives recently-rehearsed items a boost.
        """
        # The exclusion filter is applied in three places (FTS join, raw
        # episode lookup, and the in-memory cache fallback) so a row
        # cannot leak in through any code path even when one branch
        # short-circuits.
        forgotten_clause = "" if include_forgotten else " AND e.forgotten_at IS NULL"
        conn = await self._conn()
        try:
            fts_results = {}
            # Quote the utterance into a valid FTS5 expression. Passing
            # raw text meant "don't", "what's", "C++", "AI/ML" and
            # "(urgent)" all raised a syntax error into the swallowing
            # ``except`` below, so the text leg contributed nothing for
            # a large and entirely ordinary class of queries.
            match_expr = fts5_match_query(query, mode=fts_mode)
            if match_expr:
                try:
                    async with conn.execute(
                        f"""SELECT e.id, e.session_id, e.event_type, e.summary, e.detail,
                                  e.emotions, e.location, e.importance, e.created_at,
                                  e.decay_factor, e.forgotten_at, e.last_accessed_at,
                                  e.access_count, rank
                           FROM episodes_fts f JOIN episodes e ON f.rowid = e.rowid
                           WHERE episodes_fts MATCH ?{forgotten_clause}
                           ORDER BY rank LIMIT ?""",
                        (match_expr, limit * 3),
                    ) as cur:
                        rows = await cur.fetchall()
                    # The query is ``ORDER BY rank``, so row order is
                    # already the BM25 ordering, best first. We record
                    # the *position* and never the score: FTS5's rank is
                    # negative-is-better, and every attempt to turn it
                    # into a positive score in this file has got the sign
                    # backwards. Position carries the sign for us.
                    for pos, r in enumerate(rows, start=1):
                        fts_results[r["id"]] = {
                            **self._episode_row_to_dict(r),
                            "fts_pos": pos,
                        }
                except Exception as exc:
                    # Never silent: a dead text leg halves recall and the
                    # old bare ``pass`` is why nobody noticed for months.
                    logger.warning(
                        "Episode FTS leg failed for %r (expr=%r): %s",
                        query, match_expr, exc,
                    )

            vec_results = {}
            try:
                query_vec = await self._embedder.embed(query)

                if self._vec_index.indexed:
                    hits = await self._vec_index.search_cosine(query_vec, limit=limit * 3)
                    # Rank order from the index is usable, but its raw cosines
                    # cannot be thresholded (see _relevance_floor). Re-score
                    # the returned handful against the corpus centre.
                    keep = await self._centered_filter(
                        conn, query_vec, [cid for cid, _ in hits],
                    )
                    for chunk_id, sim in hits:
                        if chunk_id not in keep:
                            continue
                        sim = keep[chunk_id]
                        eid = chunk_id.rsplit("_c", 1)[0]
                        if eid not in vec_results or sim > vec_results[eid]["vec_score"]:
                            vec_results[eid] = {"id": eid, "vec_score": sim}
                else:
                    # The centred matrix is built once and reused until the
                    # corpus changes; only the mat-vec below depends on the
                    # query. Rebuilding it per query cost 32.4ms of the 39ms
                    # this whole call took on the reporter's store, against
                    # 0.35ms for the mat-vec. See _CORPUS_CACHE_MAX_BYTES.
                    corpus = await self._centered_corpus(conn)
                    sims = None
                    if corpus is not None:
                        sims = self._score_centered(
                            corpus.docs, corpus.live, corpus.centroid, query_vec,
                        )
                    if sims is not None:
                        source_ids = corpus.source_ids
                        floor = _CENTERED_SEMANTIC_FLOOR
                    else:
                        # Corpus too small to centre, or a query that cannot
                        # be centred. Raw cosine over the blobs, old floor.
                        # One blocked matmul instead of a Python loop of
                        # blob_to_vec + cosine_similarity. See
                        # cosine_similarity_bulk for the measured numbers
                        # (286ms -> 23ms at 100k chunks).
                        async with conn.execute(
                            "SELECT source_id, embedding FROM memory_chunks "
                            "WHERE source_table = 'episodes' AND embedding IS NOT NULL"
                        ) as cur:
                            chunks = await cur.fetchall()
                        source_ids = [c["source_id"] for c in chunks]
                        sims = cosine_similarity_bulk(
                            query_vec, [c["embedding"] for c in chunks],
                        )
                        floor = _RAW_SEMANTIC_FLOOR
                    for eid, sim in zip(source_ids, sims):
                        sim = float(sim)
                        if sim > floor and (eid not in vec_results or sim > vec_results[eid]["vec_score"]):
                            vec_results[eid] = {"id": eid, "vec_score": sim}
            except EmbeddingDimensionMismatch as exc:
                # Not transient, and not recoverable by retrying: the
                # stored vectors were written by a different embedder
                # than the one now configured, so every vector query in
                # this process will fail the same way. Logged loudly and
                # recorded, because the symptom is an empty result set
                # that is indistinguishable from "nothing matched".
                self._vector_leg_error = str(exc)
                logger.warning(
                    "Vector search is DISABLED: %s. Stored embeddings were "
                    "written by a different provider than the one now "
                    "configured, so semantic recall returns nothing and "
                    "search has silently degraded to keyword-only. Re-embed "
                    "with `feral memory reembed`, or set FERAL_EMBED_PROVIDER "
                    "back to the provider that wrote them.",
                    exc,
                )
            except Exception as e:
                # Same reasoning as the FTS leg above: a dead vector leg
                # halves recall, and logging it at debug is why this one
                # went unnoticed while the text leg's lesson was already
                # written down twenty lines up.
                self._vector_leg_error = str(e)
                logger.warning("Episode vector leg failed for %r: %s", query, e)

            all_ids = set(fts_results.keys()) | set(vec_results.keys())
            episode_cache = {}
            if all_ids - set(fts_results.keys()):
                missing = all_ids - set(fts_results.keys())
                placeholders = ",".join("?" for _ in missing)
                async with conn.execute(
                    f"SELECT * FROM episodes "
                    f"WHERE id IN ({placeholders}){forgotten_clause.replace('e.', '')}",
                    list(missing),
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    episode_cache[r["id"]] = self._episode_row_to_dict(r)
        finally:
            await self._release(conn)
        now = time.time()

        # ── Reciprocal Rank Fusion ──────────────────────────────────
        # Each leg is reduced to an ordering. The FTS leg is already in
        # BM25 order; the vector leg sorts by descending cosine. Only
        # the positions are fused. See the RRF_K comment at the top of
        # this module for why scores are deliberately discarded.
        fts_order = sorted(fts_results, key=lambda e: fts_results[e]["fts_pos"])
        vec_order = sorted(
            vec_results, key=lambda e: vec_results[e]["vec_score"], reverse=True
        )
        active_legs = [order for order in (fts_order, vec_order) if order]
        rrf: dict[str, float] = {}
        for order in active_legs:
            for pos, eid in enumerate(order, start=1):
                rrf[eid] = rrf.get(eid, 0.0) + 1.0 / (RRF_K + pos)
        # Positive per-query constant: rescales onto [0, 1] for the MMR
        # rerank without perturbing the fused ordering.
        rrf_norm = (RRF_K + 1) / len(active_legs) if active_legs else 0.0

        merged = []
        for eid in all_ids:
            info = fts_results.get(eid) or episode_cache.get(eid)
            if not info:
                continue

            # Recency prior: bounded, additive, and multiplied by
            # retention rather than divided into the rate. ``decay_factor``
            # is retention strength in (0, 1], where 1.0 is a pristine
            # memory and 0.05 is about to be forgotten, so it belongs on
            # the score, where a faded memory scores lower. Putting it in
            # the exponent (the old code) inverted that completely.
            hours_since = max(0.0, (now - info.get("created_at", now)) / 3600.0)
            retention = info.get("decay_factor", 1.0)
            recency = retention * math.exp(-DEFAULT_DECAY_RATE * hours_since)

            info.pop("fts_pos", None)
            info["relevance_score"] = (
                rrf_norm * rrf.get(eid, 0.0) + RECENCY_PRIOR_WEIGHT * recency
            )
            merged.append(info)

        merged.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        # Near-duplicate suppression runs BEFORE the MMR rerank, and on
        # the candidate pool rather than on the truncated result set, so
        # MMR still has a surplus to diversify over and so the filter
        # also applies when the pool is smaller than ``limit`` (the case
        # MMR returns early on). See
        # ``_suppress_near_duplicate_episodes`` for the measurements.
        #
        # ``max_kept`` caps how deep the scan goes. MMR can only prefer
        # a deeper candidate over a shallower one when their relevance
        # differs by less than its own penalty ceiling
        # (0.3 * 0.5 / 0.7 = 0.214), so representatives far below the
        # head can never be selected anyway, and scanning for them costs
        # seconds on the unbounded numpy-fallback pool.
        merged = self._suppress_near_duplicate_episodes(
            merged, max_kept=max(limit * 5, 50),
        )
        results = self._mmr_rerank_episodes(merged, limit)
        self._track_access([r["id"] for r in results])
        return results

    async def episode_search(
        self,
        query: str,
        limit: int = 10,
        *,
        include_forgotten: bool = False,
        fts_mode: str = FTS_STRICT,
    ) -> list[dict]:
        """FTS-only episode search (backward compat). Honours the
        same ``forgotten_at`` filter + access tracking as
        :meth:`episode_search_hybrid`."""
        forgotten_clause = "" if include_forgotten else " AND e.forgotten_at IS NULL"
        like_clause = "" if include_forgotten else " AND forgotten_at IS NULL"
        # Same quoting as the hybrid path: without it, a query holding an
        # apostrophe or a "+" fell through to the LIKE branch below on
        # every call, silently trading BM25 ordering for substring order.
        match_expr = fts5_match_query(query, mode=fts_mode)
        conn = await self._conn()
        try:
            try:
                if not match_expr:
                    raise ValueError("no indexable term in query")
                async with conn.execute(
                    f"""SELECT e.* FROM episodes_fts f
                       JOIN episodes e ON f.rowid = e.rowid
                       WHERE episodes_fts MATCH ?{forgotten_clause}
                       ORDER BY rank LIMIT ?""",
                    (match_expr, limit),
                ) as cur:
                    rows = await cur.fetchall()
            except Exception as exc:
                # The LIKE fallback is deliberate (brand-new DB with no
                # FTS triggers yet), but it must be visible when it fires.
                logger.debug(
                    "Episode FTS search fell back to LIKE for %r: %s", query, exc
                )
                async with conn.execute(
                    f"""SELECT * FROM episodes
                       WHERE (summary LIKE ? OR detail LIKE ?){like_clause}
                       ORDER BY created_at DESC LIMIT ?""",
                    (f"%{query}%", f"%{query}%", limit),
                ) as cur:
                    rows = await cur.fetchall()
        finally:
            await self._release(conn)
        out = [self._episode_row_to_dict(r) for r in rows]
        self._track_access([r["id"] for r in out])
        return out

    async def episode_recent(
        self,
        limit: int = 10,
        session_id: str = None,
        *,
        include_forgotten: bool = False,
    ) -> list[dict]:
        """Recent episodes by ``created_at`` (newest first). Honours
        the same ``forgotten_at`` filter + access tracking as the
        other episode read paths."""
        filter_clause = "" if include_forgotten else " AND forgotten_at IS NULL"
        conn = await self._conn()
        try:
            if session_id:
                async with conn.execute(
                    f"SELECT * FROM episodes WHERE session_id = ?{filter_clause} "
                    "ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                # Strip the leading " AND " when this is the only condition.
                where = "WHERE forgotten_at IS NULL " if not include_forgotten else ""
                async with conn.execute(
                    f"SELECT * FROM episodes {where}ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ) as cur:
                    rows = await cur.fetchall()
        finally:
            await self._release(conn)
        out = [self._episode_row_to_dict(r) for r in rows]
        self._track_access([r["id"] for r in out])
        return out

    # ── D11 access tracking ─────────────────────────────────────────────

    def _track_access(self, episode_ids: list[str]) -> None:
        """Fire-and-forget access bump for episodes just returned to a
        caller.

        Bumping ``last_accessed_at`` + ``access_count`` is the input to
        the decay sweep's ``access_boost`` term — rehearsed items
        decay slower. Doing the UPDATE inline would add a transaction
        round-trip to every search; instead we kick a background
        task per call. The task references are kept on
        ``self._access_tasks`` so the GC doesn't tear them down
        mid-write (asyncio's documented "fire-and-forget" hazard).

        Empty input is a no-op. Exceptions inside the task are
        swallowed with a debug log because access tracking is a
        heuristic; losing one update to a database-lock race must
        never surface as a chat-handler error.
        """
        if not episode_ids:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (sync call from a test that bypassed
            # async). Silently skip — the caller is not in a context
            # where fire-and-forget makes sense.
            return
        task = loop.create_task(self._bump_access(list(episode_ids)))
        if not hasattr(self, "_access_tasks"):
            self._access_tasks: set[asyncio.Task] = set()
        self._access_tasks.add(task)
        task.add_done_callback(self._access_tasks.discard)

    async def _bump_access(self, episode_ids: list[str]) -> None:
        now = time.time()
        conn = await self._conn()
        try:
            placeholders = ",".join("?" * len(episode_ids))
            await conn.execute(
                f"UPDATE episodes SET last_accessed_at = ?, "
                f"access_count = COALESCE(access_count, 0) + 1 "
                f"WHERE id IN ({placeholders})",
                [now, *episode_ids],
            )
            await conn.commit()
        except Exception as exc:
            logger.debug("episode access tracking lost an update: %s", exc)
        finally:
            await self._release(conn)

    @staticmethod
    def _episode_row_to_dict(row) -> dict:
        # Use ``keys()`` to detect the v2026.5.34 columns so old-shape
        # rows (test fixtures, fresh fts joins that didn't select them)
        # still round-trip correctly.
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        out = {
            "id": row["id"],
            "session_id": row["session_id"],
            "event_type": row["event_type"],
            "summary": row["summary"],
            "detail": row["detail"],
            "emotions": json.loads(row["emotions"]) if isinstance(row["emotions"], str) else row["emotions"],
            "location": row["location"],
            "participants": (
                json.loads(row["participants"])
                if "participants" in keys and isinstance(row["participants"], str)
                else (row["participants"] if "participants" in keys else [])
            ),
            "importance": row["importance"],
            "created_at": row["created_at"],
            "decay_factor": row["decay_factor"],
        }
        if "last_accessed_at" in keys:
            out["last_accessed_at"] = row["last_accessed_at"] or 0.0
        if "access_count" in keys:
            out["access_count"] = row["access_count"] or 0
        if "forgotten_at" in keys:
            out["forgotten_at"] = row["forgotten_at"]
        if "hlc_string" in keys:
            out["hlc_string"] = row["hlc_string"] or ""
        return out

    # ── Near-duplicate suppression ──────────────────────────────────
    #
    # Why this exists: on a real store (12,296 episodes, this user's
    # ~/.feral/memory.db) a background routine wrote the same handful of
    # robot commands thousands of times. 843 episodes are the literal
    # string "set the CuteBot lights to green for 2 seconds, then turn
    # them off", 820 are "flash red on the CuteBot", 246 are "CuteBot:
    # set_lights (r=0, g=0, b=0)". Measured before this pass existed,
    # ``episode_search_hybrid("coffee machine upkeep", limit=5)``
    # returned FIVE copies of that same lights sentence: the whole
    # result set was one memory, repeated, and anything actually about
    # the query was pushed off the end.
    #
    # ``_mmr_rerank_episodes`` does not catch this. It measures
    # diversity only by ``session_id`` with a fixed 0.3 * 0.5 = 0.15
    # penalty, and it returns early (no rerank at all) whenever the
    # candidate pool is not larger than ``limit``. Both failure modes
    # are live here: the 843 lights episodes all sit in ONE session, so
    # the penalty applies uniformly and never reorders them past a
    # weaker but different memory, while the repeated chat greetings
    # ("Hi" appears 26 times across 21 distinct sessions) look
    # maximally diverse to a session-based penalty.
    #
    # Threshold, measured rather than guessed. Similarity is token-set
    # Jaccard over the lowercased alphanumeric tokens of the episode
    # summary. On the corpus above:
    #
    #   * unrelated pairs (random cross-template, n=19,796):
    #     p50=0.128, p95=0.333, p99=0.500
    #   * pairs that CO-OCCUR in the top-50 of one real query, i.e. the
    #     topically related hard case this pass actually sees
    #     (n=6,969): p50=0.058, p90=0.323, p99=0.600
    #   * genuine variants of one sentence (same text once digits are
    #     masked, differing only in a parameter, n=164): min 0.826
    #
    # So there is an empty band between ~0.60 and ~0.826, and 0.80 sits
    # inside it: above the 99th percentile of every "different memory"
    # population, below the floor of every observed "same sentence,
    # different number" pair. At 0.85 we start MISSING real duplicates
    # (31.7% of those variants score below it); at 0.60 the suppression
    # rate on co-occurring unrelated pairs triples (1.03% vs 0.29%).
    # Every one of the 20 co-occurring pairs still suppressed at 0.80
    # was inspected and every one is a genuine restatement of the same
    # memory ("A developer working at a computer workstation with
    # multiple code editor windows..." twice, reworded by the captioner)
    # that the digit-masking label had simply failed to group.
    #
    # Only the summary is compared, never ``detail``: for chat episodes
    # ``detail`` is a constant JSON envelope ({"source": "phone_surface",
    # "mode": ..., "channel": "chat", ...}) that is byte-identical across
    # completely unrelated messages, so folding it in drags every short
    # chat turn toward every other one. Measured on 3,444 chat-episode
    # pairs that have DIFFERENT summaries: 1 pair (0.03%) is suppressed
    # at 0.80 on the summary alone, 84 pairs (2.4%) are suppressed once
    # ``detail`` is concatenated in. "??" and "What's my heart rate"
    # score 0.000 on the summary and 0.292 with the envelope attached.
    NEAR_DUPLICATE_JACCARD = 0.80

    _DEDUP_TOKEN_RE = re.compile(r"[a-z0-9]+")

    @classmethod
    def _dedup_tokens(cls, episode: dict) -> frozenset[str]:
        """Token set used for near-duplicate comparison. Summary first,
        falling back to ``detail`` only when a row has no summary at all
        (otherwise the row would look identical to every other
        summary-less row)."""
        text = (episode.get("summary") or "").strip()
        if not text:
            text = str(episode.get("detail") or "")
        return frozenset(cls._DEDUP_TOKEN_RE.findall(text.lower()))

    @classmethod
    def _suppress_near_duplicate_episodes(
        cls,
        results: list[dict],
        threshold: float | None = None,
        max_kept: int | None = None,
    ) -> list[dict]:
        """Drop results that restate a higher-scoring result.

        ``results`` must already be sorted best-first; the first member
        of each near-duplicate cluster is therefore the highest-scoring
        one and is the representative that survives. Nothing is written:
        this is a read-path filter, the suppressed episodes stay in the
        store untouched and are still returned by any query where they
        are the best answer.

        The survivor carries ``duplicates_suppressed``, the number of
        results it absorbed from the scanned prefix, so a caller can
        tell "one memory" from "one memory that happened 843 times"
        instead of silently losing that.

        ``max_kept`` bounds the work, and it is not a nicety. Cost is
        O(kept x scanned) frozenset intersections, and the candidate
        pool is NOT the ``limit * 3`` per leg that the indexed path
        produces: when ``_vec_index.indexed`` is False the vector leg
        above scans every embedded chunk and admits everything over
        cosine 0.25, with no top-k cut at all. On the reporter's store
        that is 8,581 candidates for a ``limit=5`` query. Timed on that
        exact pool, this pass costs 9,424ms unbounded and 4.3ms at
        ``max_kept=50``, for byte-identical top-5 output. Stopping once
        ``max_kept`` distinct representatives are in hand still hands
        the MMR rerank several times ``limit`` to diversify over.
        """
        cutoff = cls.NEAR_DUPLICATE_JACCARD if threshold is None else threshold
        kept: list[dict] = []
        kept_tokens: list[frozenset[str]] = []
        for ep in results:
            if max_kept is not None and len(kept) >= max_kept:
                break
            tokens = cls._dedup_tokens(ep)
            duplicate_of = None
            for i, seen in enumerate(kept_tokens):
                if not tokens and not seen:
                    duplicate_of = i
                    break
                inter = len(tokens & seen)
                if not inter:
                    continue
                union = len(tokens) + len(seen) - inter
                if union and inter / union >= cutoff:
                    duplicate_of = i
                    break
            if duplicate_of is None:
                kept.append(ep)
                kept_tokens.append(tokens)
            else:
                rep = kept[duplicate_of]
                rep["duplicates_suppressed"] = rep.get("duplicates_suppressed", 0) + 1
        return kept

    @staticmethod
    def _mmr_rerank_episodes(results: list[dict], limit: int, diversity: float = 0.3) -> list[dict]:
        if len(results) <= limit:
            return results
        selected = [results[0]]
        remaining = results[1:]
        while len(selected) < limit and remaining:
            best_idx = 0
            best_score = -999
            for i, cand in enumerate(remaining):
                relevance = cand.get("relevance_score", 0)
                max_overlap = max(
                    (0.5 if c.get("session_id") == cand.get("session_id") else 0.0)
                    for c in selected
                )
                mmr = (1.0 - diversity) * relevance - diversity * max_overlap
                if mmr > best_score:
                    best_score = mmr
                    best_idx = i
            selected.append(remaining.pop(best_idx))
        return selected

    # ─────────────────────────────────────────────
    # Tier 3: Semantic Memory — Unified Knowledge Graph (F1, v2026.5.35)
    #
    # The flat triple API (``knowledge_store/query/search/about``) is
    # preserved as-is at the caller level. When ``settings.memory.kg
    # .unified`` is true (default) the implementations route through
    # the entity-relation KG instead of the flat ``knowledge`` table:
    #
    #   * ``knowledge_store(s, p, o)`` becomes
    #     ``kg.add_relation(s, p, o)`` plus a "scalar-predicate" cleanup
    #     that deletes any pre-existing ``(s, p, *)`` relation so the
    #     flat-table "one row per (subject, predicate)" semantic is
    #     preserved. (The KG natively allows multi-target relations;
    #     the bridge layer enforces upsert.)
    #   * ``knowledge_query/search/about`` JOIN ``entities`` ×
    #     ``relations`` and return triple-shaped dicts so existing
    #     callers don't change.
    #
    # When ``unified`` is false the implementations stay on the flat
    # table (legacy path, kept for chaos/recovery and the deprecation
    # ramp).
    # ─────────────────────────────────────────────

    def _kg_unified_enabled(self) -> bool:
        """Cheap, cached lookup of the F1 feature flag.

        Reads ``settings.memory.kg.unified`` once per call; the loader
        is itself cached so this stays sub-microsecond. Honours an
        explicit ``False`` to disable, defaults to ``True`` for new
        installs.
        """
        if not self._kg:
            return False
        try:
            from config.loader import load_settings
            settings = load_settings()
            kg_cfg = (settings.get("memory") or {}).get("kg") or {}
            return bool(kg_cfg.get("unified", True))
        except Exception:
            return False

    async def knowledge_store(
        self,
        subject: str,
        predicate: str,
        obj: str,
        confidence: float = 1.0,
        source: str = "user",
    ) -> dict:
        if self._kg_unified_enabled():
            return await self._knowledge_store_unified(
                subject, predicate, obj, confidence, source,
            )
        return await self._knowledge_store_flat(
            subject, predicate, obj, confidence, source,
        )

    async def _knowledge_store_unified(
        self, subject: str, predicate: str, obj: str,
        confidence: float, source: str,
    ) -> dict:
        """F1 KG-routed write with *scalar-predicate* semantics.

        Flat triples upsert on ``(subject, predicate)``: one object
        per (s, p) pair, latest write wins. The native KG stores
        ``(source, relation_type, target)`` and allows multiple
        targets per source (e.g. ``user works_at FERAL`` AND
        ``user works_at Stripe`` both true). The bridge needs the
        former semantic but the underlying table is the latter.

        We solve this by computing a *scalar* relation id from
        ``(source_id, relation_type)`` only — independent of the
        target. Two writes of ``(user, color, *)`` get the same row
        id; ``INSERT OR REPLACE`` makes the second write replace
        the first locally, and two brains writing the same scalar
        predicate at different HLC values converge under D12 LWW
        because they agree on the row id without coordinating.

        KG-native writes via ``kg.add_relation`` keep their full
        ``(source, relation, target)`` id and multi-target
        semantic — only the ``knowledge_store`` bridge surface is
        scalar.
        """
        from memory.knowledge_graph import _stable_kg_id
        # Use add_entity to create/merge entities (it handles
        # embedding-based dedup + KG-native HLC logging).
        src = await self._kg.add_entity(subject, "thing")
        tgt = await self._kg.add_entity(obj, "thing")

        rid = _stable_kg_id("scalar", src["id"], predicate)
        now = time.time()

        hlc = self._log_sync("relations", "insert", rid, {
            "id": rid,
            "source_id": src["id"], "relation_type": predicate,
            "target_id": tgt["id"], "confidence": confidence,
            "evidence_text": source[:1000], "created_at": now,
        })

        conn = await self._conn()
        try:
            # INSERT OR REPLACE collapses the two-write upsert into
            # one row; LWW arrival ordering on the wire is handled
            # by ``_apply_to_memory`` (it compares on hlc_string and
            # short-circuits stale arrivals).
            await conn.execute(
                """INSERT OR REPLACE INTO relations
                   (id, source_id, relation_type, target_id, confidence,
                    evidence_text, source_origin, hlc_string,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'bridge', ?, ?, ?)""",
                (rid, src["id"], predicate, tgt["id"], confidence,
                 source[:1000], hlc, now, now),
            )
            await conn.commit()
        finally:
            await self._release(conn)

        return {
            "id": rid,
            "subject": subject, "predicate": predicate, "object": obj,
        }

    async def _knowledge_store_flat(
        self, subject: str, predicate: str, obj: str,
        confidence: float, source: str,
    ) -> dict:
        """Legacy flat-triple write path. Kept for the
        ``memory.kg.unified=false`` chaos/recovery setting and for
        brains that haven't run the F1 migration yet."""
        conn = await self._conn()
        now = time.time()
        try:
            async with conn.execute(
                "SELECT id FROM knowledge WHERE subject = ? AND predicate = ?",
                (subject, predicate),
            ) as cur:
                existing = await cur.fetchone()

            if existing:
                kid = existing[0]
                hlc = self._log_sync("knowledge", "insert", kid, {
                    "id": kid, "subject": subject, "predicate": predicate,
                    "object": obj, "confidence": confidence, "source": source,
                    "created_at": now,
                })
                await conn.execute(
                    "UPDATE knowledge SET object = ?, confidence = ?, source = ?, updated_at = ?, hlc_string = ? WHERE id = ?",
                    (obj, confidence, source, now, hlc, kid),
                )
            else:
                kid = _stable_knowledge_id(subject, predicate)
                hlc = self._log_sync("knowledge", "insert", kid, {
                    "id": kid, "subject": subject, "predicate": predicate,
                    "object": obj, "confidence": confidence, "source": source,
                    "created_at": now,
                })
                await conn.execute(
                    """INSERT INTO knowledge (id, subject, predicate, object, confidence, source, created_at, updated_at, hlc_string)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (kid, subject, predicate, obj, confidence, source, now, now, hlc),
                )

            await conn.commit()
        finally:
            await self._release(conn)
        return {"id": kid, "subject": subject, "predicate": predicate, "object": obj}

    async def knowledge_query(self, subject: str = "", predicate: str = "", limit: int = 20) -> list[dict]:
        if self._kg_unified_enabled():
            return await self._knowledge_query_unified(subject, predicate, limit)
        return await self._knowledge_query_flat(subject, predicate, limit)

    async def _knowledge_query_unified(
        self, subject: str, predicate: str, limit: int,
    ) -> list[dict]:
        conn = await self._conn()
        try:
            conditions, params = [], []
            if subject:
                conditions.append("e_src.name = ?")
                params.append(subject)
            if predicate:
                conditions.append("r.relation_type = ?")
                params.append(predicate)
            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            async with conn.execute(
                f"""SELECT r.id, e_src.name AS subject, r.relation_type AS predicate,
                           e_tgt.name AS object, r.confidence, r.evidence_text AS source,
                           r.updated_at
                    FROM relations r
                    JOIN entities e_src ON r.source_id = e_src.id
                    JOIN entities e_tgt ON r.target_id = e_tgt.id
                    {where}
                    ORDER BY r.updated_at DESC LIMIT ?""",
                (*params, limit),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            {"id": r["id"], "subject": r["subject"], "predicate": r["predicate"],
             "object": r["object"], "confidence": r["confidence"], "source": r["source"],
             "updated_at": r["updated_at"]}
            for r in rows
        ]

    async def _knowledge_query_flat(
        self, subject: str, predicate: str, limit: int,
    ) -> list[dict]:
        conn = await self._conn()
        try:
            conditions, params = [], []
            if subject:
                conditions.append("subject = ?")
                params.append(subject)
            if predicate:
                conditions.append("predicate = ?")
                params.append(predicate)
            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            async with conn.execute(
                f"SELECT * FROM knowledge {where} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            {"id": r["id"], "subject": r["subject"], "predicate": r["predicate"],
             "object": r["object"], "confidence": r["confidence"], "source": r["source"],
             "updated_at": r["updated_at"]}
            for r in rows
        ]

    async def knowledge_search(
        self, query: str, limit: int = 10, *, fts_mode: str = FTS_STRICT,
    ) -> list[dict]:
        if self._kg_unified_enabled():
            return await self._knowledge_search_unified(query, limit, fts_mode=fts_mode)
        return await self._knowledge_search_flat(query, limit, fts_mode=fts_mode)

    async def _knowledge_search_unified(
        self, query: str, limit: int, *, fts_mode: str = FTS_STRICT,
    ) -> list[dict]:
        """KG-routed search: hit ``entities_fts`` for matching entity
        names (subject OR object), then expand each match into the
        relations it participates in."""
        # Quoted here, at the SQL boundary, for the same reason as the
        # episode paths: an unquoted apostrophe or operator character
        # raised straight into the LIKE fallback below.
        match_expr = fts5_match_query(query, mode=fts_mode)
        conn = await self._conn()
        try:
            try:
                if not match_expr:
                    raise ValueError("no indexable term in query")
                async with conn.execute(
                    """SELECT e.id FROM entities_fts f
                       JOIN entities e ON f.rowid = e.rowid
                       WHERE entities_fts MATCH ? LIMIT ?""",
                    (match_expr, max(limit * 4, 20)),
                ) as cur:
                    entity_ids = [r["id"] for r in await cur.fetchall()]
            except Exception as exc:
                # FTS unavailable: fall back to LIKE on names.
                logger.debug("entities FTS search fell back to LIKE: %s", exc)
                async with conn.execute(
                    "SELECT id FROM entities WHERE name LIKE ? LIMIT ?",
                    (f"%{query}%", max(limit * 4, 20)),
                ) as cur:
                    entity_ids = [r["id"] for r in await cur.fetchall()]
            if not entity_ids:
                return []
            placeholders = ",".join("?" * len(entity_ids))
            async with conn.execute(
                f"""SELECT r.id, e_src.name AS subject, r.relation_type AS predicate,
                           e_tgt.name AS object, r.confidence
                    FROM relations r
                    JOIN entities e_src ON r.source_id = e_src.id
                    JOIN entities e_tgt ON r.target_id = e_tgt.id
                    WHERE r.source_id IN ({placeholders})
                       OR r.target_id IN ({placeholders})
                    ORDER BY r.updated_at DESC LIMIT ?""",
                (*entity_ids, *entity_ids, limit),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            {"id": r["id"], "subject": r["subject"], "predicate": r["predicate"],
             "object": r["object"], "confidence": r["confidence"]}
            for r in rows
        ]

    async def _knowledge_search_flat(
        self, query: str, limit: int, *, fts_mode: str = FTS_STRICT,
    ) -> list[dict]:
        match_expr = fts5_match_query(query, mode=fts_mode)
        conn = await self._conn()
        try:
            try:
                if not match_expr:
                    raise ValueError("no indexable term in query")
                async with conn.execute(
                    """SELECT k.* FROM knowledge_fts f
                       JOIN knowledge k ON f.rowid = k.rowid
                       WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (match_expr, limit),
                ) as cur:
                    rows = await cur.fetchall()
            except Exception as exc:
                logger.debug("knowledge FTS search fell back to LIKE: %s", exc)
                async with conn.execute(
                    """SELECT * FROM knowledge WHERE subject LIKE ? OR object LIKE ?
                       ORDER BY updated_at DESC LIMIT ?""",
                    (f"%{query}%", f"%{query}%", limit),
                ) as cur:
                    rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            {"id": r["id"], "subject": r["subject"], "predicate": r["predicate"],
             "object": r["object"], "confidence": r["confidence"]}
            for r in rows
        ]

    async def knowledge_about(self, entity: str, limit: int = 20) -> list[dict]:
        if self._kg_unified_enabled():
            return await self._knowledge_about_unified(entity, limit)
        return await self._knowledge_about_flat(entity, limit)

    async def _knowledge_about_unified(
        self, entity: str, limit: int,
    ) -> list[dict]:
        conn = await self._conn()
        try:
            async with conn.execute(
                """SELECT r.id, e_src.name AS subject, r.relation_type AS predicate,
                          e_tgt.name AS object, r.confidence
                   FROM relations r
                   JOIN entities e_src ON r.source_id = e_src.id
                   JOIN entities e_tgt ON r.target_id = e_tgt.id
                   WHERE e_src.name = ? OR e_tgt.name = ?
                   ORDER BY r.confidence DESC, r.updated_at DESC LIMIT ?""",
                (entity, entity, limit),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            {"subject": r["subject"], "predicate": r["predicate"], "object": r["object"],
             "confidence": r["confidence"]}
            for r in rows
        ]

    async def _knowledge_about_flat(
        self, entity: str, limit: int,
    ) -> list[dict]:
        conn = await self._conn()
        try:
            async with conn.execute(
                """SELECT * FROM knowledge WHERE subject = ? OR object = ?
                   ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                (entity, entity, limit),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            {"subject": r["subject"], "predicate": r["predicate"], "object": r["object"],
             "confidence": r["confidence"]}
            for r in rows
        ]

    async def migrate_knowledge_to_kg(
        self, *, batch_size: int = 200, mark_as_deprecated: bool = True,
    ) -> dict:
        """F1 bulk migration: port unmigrated rows from the flat
        ``knowledge`` table into the unified KG, then optionally
        rename the flat table to ``knowledge__deprecated`` so the
        legacy reader paths see no rows.

        Idempotent. Re-runs only port rows whose ``kg_migrated_at``
        is null/zero; rows already ported (from a prior boot) are
        skipped. Returns ``{"ported": N, "skipped": M, "deprecated":
        true/false}``.
        """
        if not self._kg:
            return {"ported": 0, "skipped": 0, "deprecated": False, "reason": "no_kg"}

        ported = 0
        skipped = 0

        # Check whether the flat table still exists. After
        # ``mark_as_deprecated`` fires once the table is renamed to
        # ``knowledge__deprecated`` and ``_init_db`` recreates an
        # empty ``knowledge`` on next boot; a second migration call
        # then finds the empty table, has zero unmigrated rows, and
        # skips the rename (because ``knowledge__deprecated``
        # already exists). Surface that state cleanly.
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge'"
            ) as cur:
                has_flat = (await cur.fetchone()) is not None
        finally:
            await self._release(conn)
        if not has_flat:
            return {"ported": 0, "skipped": 0, "deprecated": False, "reason": "no_flat_table"}

        # Pull batches via the pooled connection, RELEASE it before
        # calling ``kg.add_relation`` (KG opens its own connection;
        # holding a pooled write transaction here would deadlock on
        # SQLite's reserved lock under WAL contention), then reopen
        # to mark the rows ported.
        while True:
            conn = await self._conn()
            try:
                async with conn.execute(
                    """SELECT id, subject, predicate, object, confidence, source
                       FROM knowledge
                       WHERE COALESCE(kg_migrated_at, 0) = 0
                       LIMIT ?""",
                    (batch_size,),
                ) as cur:
                    batch = [dict(row) for row in await cur.fetchall()]
            finally:
                await self._release(conn)
            if not batch:
                break

            now = time.time()
            for row in batch:
                try:
                    await self._kg.add_relation(
                        source_name=row["subject"],
                        relation_type=row["predicate"],
                        target_name=row["object"],
                        confidence=row["confidence"] or 1.0,
                        evidence=row["source"] or "migration",
                    )
                    ported += 1
                except Exception as exc:
                    logger.warning(
                        "F1 migration: skipping row id=%s subject=%s: %s",
                        row["id"], row["subject"], exc,
                    )
                    skipped += 1
                    continue

                # Mark the row as ported in its own short
                # transaction so KG's connection isn't fighting our
                # write lock.
                mark_conn = await self._conn()
                try:
                    await mark_conn.execute(
                        "UPDATE knowledge SET kg_migrated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                    await mark_conn.commit()
                finally:
                    await self._release(mark_conn)

        deprecated = False
        if mark_as_deprecated:
            conn = await self._conn()
            try:
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge__deprecated'"
                ) as cur:
                    already_deprecated = await cur.fetchone()
                if not already_deprecated:
                    async with conn.execute(
                        "SELECT COUNT(*) FROM knowledge WHERE COALESCE(kg_migrated_at, 0) = 0"
                    ) as cur:
                        unmigrated = (await cur.fetchone())[0]
                    if unmigrated == 0:
                        await conn.execute(
                            "ALTER TABLE knowledge RENAME TO knowledge__deprecated"
                        )
                        # SQLite rewrites the FTS triggers to follow the
                        # renamed table, so knowledge_fts would go on being
                        # maintained from the deprecated rows while the
                        # recreated `knowledge` table got no triggers at all
                        # (_init_db's CREATE TRIGGER IF NOT EXISTS sees the
                        # names already taken). Drop them here, and empty the
                        # index they filled, so the next boot rebinds both to
                        # the live table. _repair_knowledge_fts fixes stores
                        # that already went through the old code path.
                        for trigger in (
                            "knowledge_ai", "knowledge_ad", "knowledge_fts_update",
                        ):
                            await conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                        await conn.execute("DELETE FROM knowledge_fts")
                        await conn.commit()
                        deprecated = True
                        logger.info(
                            "F1: knowledge table renamed to knowledge__deprecated "
                            "(ported=%d)", ported,
                        )
            finally:
                await self._release(conn)
        return {"ported": ported, "skipped": skipped, "deprecated": deprecated}

    # ─────────────────────────────────────────────
    # Tier 4: Execution Log
    # ─────────────────────────────────────────────

    async def log_execution(
        self, session_id: str, skill_id: str, endpoint_id: str, args: dict,
        result_status: str, result_summary: str = "", latency_ms: float = 0,
    ) -> str:
        eid = str(uuid4())[:12]
        now = time.time()
        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO execution_log
                   (id, session_id, skill_id, endpoint_id, args, result_status, result_summary, latency_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (eid, session_id, skill_id, endpoint_id, json.dumps(args)[:2000],
                 result_status, result_summary[:500], latency_ms, now),
            )
            await conn.commit()
        finally:
            await self._release(conn)
        return eid

    async def log_feedback(self, execution_id: str, feedback: str):
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE execution_log SET user_feedback = ? WHERE id = ?",
                (feedback[:500], execution_id),
            )
            await conn.commit()
        finally:
            await self._release(conn)

    async def log_recent(self, skill_id: str = "", limit: int = 20) -> list[dict]:
        conn = await self._conn()
        try:
            if skill_id:
                async with conn.execute(
                    "SELECT * FROM execution_log WHERE skill_id = ? ORDER BY created_at DESC LIMIT ?",
                    (skill_id, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    "SELECT * FROM execution_log ORDER BY created_at DESC LIMIT ?", (limit,),
                ) as cur:
                    rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [dict(r) for r in rows]

    async def log_success_rate(self, skill_id: str) -> dict:
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT COUNT(*) FROM execution_log WHERE skill_id = ?", (skill_id,),
            ) as cur:
                total = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COUNT(*) FROM execution_log WHERE skill_id = ? AND result_status = 'success'",
                (skill_id,),
            ) as cur:
                successes = (await cur.fetchone())[0]
        finally:
            await self._release(conn)
        return {"skill_id": skill_id, "total_executions": total, "successes": successes,
                "rate": successes / total if total > 0 else 0.0}

    # ─────────────────────────────────────────────
    # Unified Context Builder (for LLM injection)
    # ─────────────────────────────────────────────

    async def build_context_for_llm(
        self,
        session_id: str,
        query: str = "",
        max_tokens_budget: int = 2000,
        memory_filter: str = "",
    ) -> str:
        return await context_build_context_for_llm_async(
            self,
            session_id=session_id,
            query=query,
            max_tokens_budget=max_tokens_budget,
            memory_filter=memory_filter,
        )

    async def build_context_for_llm_async(
        self,
        session_id: str,
        query: str = "",
        max_tokens_budget: int = 2000,
        memory_filter: str = "",
    ) -> str:
        """Back-compat alias for :meth:`build_context_for_llm`. Pre-r12
        deployments call ``build_context_for_llm_async`` explicitly; now
        both names are async and route to the same implementation."""
        return await self.build_context_for_llm(
            session_id, query=query, max_tokens_budget=max_tokens_budget, memory_filter=memory_filter,
        )

    # ─────────────────────────────────────────────
    # Compaction (multi-stage summarization)
    # ─────────────────────────────────────────────

    async def compact_session(self, session_id: str, history: list[dict], llm=None,
                              preserve_last_n: int = 3, max_summary_chars: int = 16000) -> dict:
        return await context_compact_session(
            self,
            session_id=session_id,
            history=history,
            llm=llm,
            preserve_last_n=preserve_last_n,
            max_summary_chars=max_summary_chars,
        )

    async def _llm_summarize(self, messages: list[dict], llm, max_chars: int) -> str:
        return await context_llm_summarize(messages=messages, llm=llm, max_chars=max_chars)

    @staticmethod
    def _heuristic_summarize(messages: list[dict]) -> str:
        return context_heuristic_summarize(messages)

    # ─────────────────────────────────────────────
    # Unified Hybrid Search
    # ─────────────────────────────────────────────

    async def search_all(self, query: str, limit: int = 10) -> list[dict]:
        return await context_search_all(self, query=query, limit=limit)

    # ─────────────────────────────────────────────
    # Memory Wiki (durable markdown knowledge surface)
    # ─────────────────────────────────────────────

    @staticmethod
    def _wiki_slug(value: str) -> str:
        return helper_wiki_slug(value)

    async def wiki_upsert_page(
        self,
        *,
        page_id: str,
        title: str,
        kind: str,
        body_markdown: str,
        source_refs: list[dict] | None = None,
    ) -> dict:
        return await helper_wiki_upsert_page(
            self,
            page_id=page_id,
            title=title,
            kind=kind,
            body_markdown=body_markdown,
            source_refs=source_refs,
        )

    async def wiki_get_page(self, page_id: str) -> Optional[dict]:
        return await helper_wiki_get_page(self, page_id=page_id)

    async def wiki_list_pages(self, *, query: str = "", kind: str = "", limit: int = 50) -> list[dict]:
        return await helper_wiki_list_pages(self, query=query, kind=kind, limit=limit)

    async def wiki_stats(self) -> dict:
        return await helper_wiki_stats(self)

    async def wiki_compile(
        self,
        *,
        notes_limit: int = 200,
        episodes_limit: int = 200,
        knowledge_limit: int = 400,
    ) -> dict:
        return await helper_wiki_compile(
            self,
            notes_limit=notes_limit,
            episodes_limit=episodes_limit,
            knowledge_limit=knowledge_limit,
        )

    # ─────────────────────────────────────────────
    # Legacy Notes API (backward compat)
    # ─────────────────────────────────────────────

    async def save(self, content: str, tags: list[str] = None, importance: str = "normal", source: str = "user") -> dict:
        return await save_note(self, content=content, tags=tags, importance=importance, source=source)

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        return await search_notes(self, query=query, limit=limit)

    async def list_recent(self, limit: int = 10) -> list[dict]:
        return await list_recent_notes(self, limit=limit)

    async def delete(self, note_id: str) -> bool:
        return await delete_note(self, note_id=note_id)

    async def count(self) -> int:
        return await count_notes(self)

    # Stats path budgets + cache TTL.
    #
    # The 2.5s safety budget stays as the *last-resort* timeout: if
    # something pathological happens (the dedicated read connection
    # can't open AND the writer pool is fully wedged), the dashboard
    # still gets a degraded payload instead of hanging. In steady
    # state it must never trip — the cache + read-only connection
    # together keep the COUNT round off the writer-contention surface.
    #
    # ``_STATS_CACHE_TTL_S`` controls the short-TTL cache. Episode /
    # note / knowledge counts don't change second-to-second
    # meaningfully, so a 15s window collapses ~15 dashboard polls
    # into a single COUNT round-trip and kills the steady-state
    # SQL load from /api/dashboard.
    _STATS_TOTAL_BUDGET_S = 2.5
    _STATS_CONN_BUDGET_S = 1.0
    _STATS_CACHE_TTL_S = 15.0

    def _live_stats_overlay(self) -> dict:
        """Cheap in-RAM signals layered onto a cached payload.

        ``active_working_sessions`` and ``embed_queue_pending`` are
        zero-cost to read (no SQLite round-trip), and they're the
        stats fields most likely to drift second-to-second. Pulling
        them fresh on every cache hit keeps the dashboard's "things
        the brain is doing right now" tiles honest without paying
        for the COUNT(*) sweep.
        """
        return {
            "active_working_sessions": len(self._working),
            "embed_queue_pending": getattr(self._embed_queue, "pending", 0),
        }

    def _degraded_stats_payload(self) -> dict:
        return {
            "ok": False,
            "reason": "stats_timeout",
            "notes": 0,
            "episodes": 0,
            "knowledge_triples": 0,
            "execution_logs": 0,
            "wiki_pages": 0,
            "session_snapshots": 0,
            "active_working_sessions": len(self._working),
            "embedded_chunks": 0,
            "vec_index_count": 0,
            "vec_index_mode": self._backend_id + " (degraded — stats timeout)",
            "embedding_provider": self._embedder.provider_name,
            "embed_queue_pending": getattr(self._embed_queue, "pending", 0),
            "knowledge_graph": {"entities": 0, "relations": 0},
        }

    async def _acquire_stats_conn(self):
        """Acquire a connection for stats COUNT queries.

        Prefers the dedicated read-only connection so stats never
        queues behind the writer pool. Falls back to the pool only
        when the read-only connection can't be opened (e.g. a test
        fixture that doesn't expose ``_get_stats_read_conn``, or an
        aiosqlite build without URI support).

        Returns ``(conn, release)`` where ``release`` is an async
        callable. For the read-only connection ``release`` is a
        no-op — the connection is held for the lifetime of the
        store.
        """
        get_read = getattr(self, "_get_stats_read_conn", None)
        if get_read is not None:
            try:
                conn = await asyncio.wait_for(
                    get_read(), timeout=self._STATS_CONN_BUDGET_S,
                )
            except asyncio.TimeoutError:
                raise
            except Exception as exc:
                logger.debug(
                    "memory.stats: read-only connection unavailable "
                    "(%s); falling back to writer pool", exc,
                )
            else:
                async def _noop_release() -> None:
                    return None
                return conn, _noop_release

        conn = await asyncio.wait_for(
            self._conn(), timeout=self._STATS_CONN_BUDGET_S,
        )

        async def _release_pool() -> None:
            await self._release(conn)

        return conn, _release_pool

    async def stats(self) -> dict:
        # ── Cache path ──
        # Episode / note / knowledge counts don't change at the
        # dashboard's poll cadence (~1Hz). Serving rapid polls from a
        # short-TTL cache collapses N polls into one COUNT round and
        # is the structural fix for the v2026.5.41 stats-timeout
        # flood: in steady state the SQL path runs at most once per
        # ``_STATS_CACHE_TTL_S``, so background services holding the
        # writer pool can't starve it any more.
        ttl = self._STATS_CACHE_TTL_S
        now = time.time()
        cached = getattr(self, "_stats_cache", None)
        cached_at = getattr(self, "_stats_cache_at", 0.0)
        if cached is not None and (now - cached_at) < ttl:
            return {
                **cached,
                **self._live_stats_overlay(),
                "from_cache": True,
                "cache_age_s": round(now - cached_at, 3),
            }

        # Coalesce concurrent stats() calls so a thundering herd of
        # dashboard requests still triggers exactly one COUNT round.
        lock = getattr(self, "_stats_cache_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            try:
                self._stats_cache_lock = lock
            except Exception:
                pass
        async with lock:
            now = time.time()
            cached = getattr(self, "_stats_cache", None)
            cached_at = getattr(self, "_stats_cache_at", 0.0)
            if cached is not None and (now - cached_at) < ttl:
                return {
                    **cached,
                    **self._live_stats_overlay(),
                    "from_cache": True,
                    "cache_age_s": round(now - cached_at, 3),
                }
            return await self._compute_and_cache_stats()

    async def _compute_and_cache_stats(self) -> dict:
        async def _read_counts() -> dict:
            conn, release = await self._acquire_stats_conn()
            try:
                async with conn.execute("SELECT COUNT(*) FROM notes") as cur:
                    notes_count = (await cur.fetchone())[0]
                async with conn.execute("SELECT COUNT(*) FROM episodes") as cur:
                    episodes_count = (await cur.fetchone())[0]
                # F1 (v2026.5.35) — when the unified KG is on, the canonical
                # knowledge surface is ``relations``. Count from there so
                # ``stats["knowledge_triples"]`` keeps meaning "how many
                # facts does the brain know" across the flat→KG cutover.
                knowledge_table = "knowledge"
                if self._kg_unified_enabled():
                    knowledge_table = "relations"
                async with conn.execute(f"SELECT COUNT(*) FROM {knowledge_table}") as cur:
                    knowledge_count = (await cur.fetchone())[0]
                async with conn.execute("SELECT COUNT(*) FROM execution_log") as cur:
                    exec_count = (await cur.fetchone())[0]
                async with conn.execute("SELECT COUNT(*) FROM wiki_pages") as cur:
                    wiki_count = (await cur.fetchone())[0]
                async with conn.execute("SELECT COUNT(*) FROM session_snapshots") as cur:
                    snapshot_count = (await cur.fetchone())[0]
                try:
                    async with conn.execute("SELECT COUNT(*) FROM memory_chunks") as cur:
                        chunk_count = (await cur.fetchone())[0]
                except Exception:
                    chunk_count = 0
            finally:
                await release()
            return {
                "notes": notes_count,
                "episodes": episodes_count,
                "knowledge_triples": knowledge_count,
                "execution_logs": exec_count,
                "wiki_pages": wiki_count,
                "session_snapshots": snapshot_count,
                "embedded_chunks": chunk_count,
            }

        try:
            counts = await asyncio.wait_for(
                _read_counts(), timeout=self._STATS_TOTAL_BUDGET_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "memory.stats: aiosqlite COUNT queries exceeded the "
                "%.1fs safety budget — even the dedicated read-only "
                "connection couldn't complete in time. Returning a "
                "degraded payload so the dashboard stays responsive.",
                self._STATS_TOTAL_BUDGET_S,
            )
            return self._degraded_stats_payload()

        working_sessions = len(self._working)
        kg_stats = self._kg.stats() if self._kg else {"entities": 0, "relations": 0}

        vec_count = 0
        try:
            vec_count = await asyncio.wait_for(
                self._vec_index.count(), timeout=0.5,
            )
        except asyncio.TimeoutError:
            logger.debug("memory.stats: vec_index.count() exceeded 0.5s budget; reporting 0")
        except Exception as exc:
            logger.debug("vec_index.count() failed: %s", exc)

        payload = {
            "ok": True,
            "notes": counts["notes"],
            "episodes": counts["episodes"],
            "knowledge_triples": counts["knowledge_triples"],
            "execution_logs": counts["execution_logs"],
            "wiki_pages": counts["wiki_pages"],
            "session_snapshots": counts["session_snapshots"],
            "active_working_sessions": working_sessions,
            "embedded_chunks": counts["embedded_chunks"],
            "vec_index_count": vec_count,
            # "(numpy scan)", not "(degraded)". Both paths are full scans
            # (sqlite-vec 0.1.9 builds no ANN index) and the numpy one is the
            # faster of the two at every corpus size measured, so the old
            # word named a defect that is not there. See
            # memory.embeddings.cosine_similarity_bulk for the numbers.
            "vec_index_mode": self._backend_id + (" (indexed)" if self._vec_index.indexed else " (numpy scan)"),
            "embedding_provider": self._embedder.provider_name,
            "embed_queue_pending": getattr(self._embed_queue, "pending", 0),
            "knowledge_graph": kg_stats,
        }

        # Only cache the healthy payload. Degraded payloads must
        # NOT poison the cache — the next caller should retry the
        # COUNTs immediately so a transient pool stall doesn't lock
        # the dashboard into "0 episodes" for a full TTL window.
        try:
            self._stats_cache = dict(payload)
            self._stats_cache_at = time.time()
        except Exception:
            pass
        return payload
