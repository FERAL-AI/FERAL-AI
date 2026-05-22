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
import sqlite3
import time
from collections import deque
from typing import Optional
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
from memory.context_builder import (
    build_context_for_llm_async as context_build_context_for_llm_async,
    compact_session as context_compact_session,
    heuristic_summarize as context_heuristic_summarize,
    llm_summarize as context_llm_summarize,
    search_all as context_search_all,
)
from memory.embeddings import (
    EmbeddingProvider,
    EmbedQueue,
    chunk_text,
    blob_to_vec,
    cosine_similarity,
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

_SCHEMA_VERSION = 6  # v2026.5.34: D11 decay + D12 sync HLC columns


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

TEXT_WEIGHT = 0.3
VECTOR_WEIGHT = 0.7
DEFAULT_DECAY_RATE = 0.01


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

        self.db_path = db_path
        self._working: dict[str, deque[dict]] = {}
        self._working_max = 50
        self._working_max_sessions = 500
        self._sync_engine = None
        self._about_me_store = None
        self._embedder = EmbeddingProvider()
        self._kg = None
        # Lane 05 W3 (AUDIT-r14 finding 14): track fire-and-forget
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
            try:
                await self._release(conn)
            except Exception:
                pass

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
                await self._release(conn)
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
        once at construction time, before the event loop is even up."""
        conn = sqlite3.connect(self.db_path)
        try:
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

            conn.commit()
        finally:
            conn.close()

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
                preview = msg["content"][:120]
                break
        if not title and messages:
            for msg in messages:
                if msg.get("role") == "user" and msg.get("content"):
                    title = msg["content"][:80]
                    break
        title = title or "New conversation"

        conn = await self._conn()
        try:
            await conn.execute("""
                INSERT INTO conversations (id, title, preview, messages_json, message_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    preview = excluded.preview,
                    messages_json = excluded.messages_json,
                    message_count = excluded.message_count,
                    updated_at = excluded.updated_at
            """, (conversation_id, title, preview, json.dumps(messages[-500:]), len(messages), now, now))
            await conn.commit()
        finally:
            await self._release(conn)
        return {"id": conversation_id, "title": title, "message_count": len(messages), "updated_at": now}

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

    async def conversation_list(self, limit: int = 50) -> list[dict]:
        """List recent conversations (metadata only)."""
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT id, title, preview, message_count, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?",
                (min(limit, 200),),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            {"id": r[0], "title": r[1], "preview": r[2], "message_count": r[3], "created_at": r[4], "updated_at": r[5]}
            for r in rows
        ]

    async def conversation_get(self, conversation_id: str) -> dict | None:
        """Load a full conversation with messages."""
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT id, title, preview, messages_json, message_count, created_at, updated_at FROM conversations WHERE id = ?",
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
        }

    async def conversation_delete(self, conversation_id: str) -> bool:
        conn = await self._conn()
        try:
            await conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            await conn.commit()
        finally:
            await self._release(conn)
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
    ) -> dict:
        eid = str(uuid4())[:12]
        now = time.time()
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
        if self._about_me_store is not None and text:
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

    async def episode_search_hybrid(
        self,
        query: str,
        limit: int = 10,
        *,
        include_forgotten: bool = False,
    ) -> list[dict]:
        """Hybrid search: FTS5 text (0.3) + vector similarity (0.7) with temporal decay.

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
            try:
                async with conn.execute(
                    f"""SELECT e.id, e.session_id, e.event_type, e.summary, e.detail,
                              e.emotions, e.location, e.importance, e.created_at,
                              e.decay_factor, e.forgotten_at, e.last_accessed_at,
                              e.access_count, rank
                       FROM episodes_fts f JOIN episodes e ON f.rowid = e.rowid
                       WHERE episodes_fts MATCH ?{forgotten_clause}
                       ORDER BY rank LIMIT ?""",
                    (query, limit * 3),
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    fts_results[r["id"]] = {
                        **self._episode_row_to_dict(r),
                        "fts_score": 1.0 / (1.0 + abs(r["rank"])),
                    }
            except Exception:
                pass

            vec_results = {}
            try:
                query_vec = await self._embedder.embed(query)

                if self._vec_index.indexed:
                    hits = await self._vec_index.search_cosine(query_vec, limit=limit * 3)
                    for chunk_id, sim in hits:
                        if sim < 0.25:
                            continue
                        eid = chunk_id.rsplit("_c", 1)[0]
                        if eid not in vec_results or sim > vec_results[eid]["vec_score"]:
                            vec_results[eid] = {"id": eid, "vec_score": sim}
                else:
                    async with conn.execute(
                        "SELECT source_id, embedding FROM memory_chunks "
                        "WHERE source_table = 'episodes' AND embedding IS NOT NULL"
                    ) as cur:
                        chunks = await cur.fetchall()
                    for c in chunks:
                        evec = blob_to_vec(c["embedding"])
                        sim = cosine_similarity(query_vec, evec)
                        eid = c["source_id"]
                        if sim > 0.25 and (eid not in vec_results or sim > vec_results[eid]["vec_score"]):
                            vec_results[eid] = {"id": eid, "vec_score": sim}
            except Exception as e:
                logger.debug("Vector search failed: %s", e)

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
        merged = []
        for eid in all_ids:
            info = fts_results.get(eid) or episode_cache.get(eid)
            if not info:
                continue

            fts_score = fts_results.get(eid, {}).get("fts_score", 0)
            vec_score = vec_results.get(eid, {}).get("vec_score", 0)
            base_score = TEXT_WEIGHT * fts_score + VECTOR_WEIGHT * vec_score

            hours_since = (now - info.get("created_at", now)) / 3600.0
            decay = info.get("decay_factor", 1.0)
            temporal_factor = math.exp(-DEFAULT_DECAY_RATE * decay * hours_since)
            final_score = base_score * temporal_factor

            info["relevance_score"] = final_score
            merged.append(info)

        merged.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        results = self._mmr_rerank_episodes(merged, limit)
        self._track_access([r["id"] for r in results])
        return results

    async def episode_search(
        self,
        query: str,
        limit: int = 10,
        *,
        include_forgotten: bool = False,
    ) -> list[dict]:
        """FTS-only episode search (backward compat). Honours the
        same ``forgotten_at`` filter + access tracking as
        :meth:`episode_search_hybrid`."""
        forgotten_clause = "" if include_forgotten else " AND e.forgotten_at IS NULL"
        like_clause = "" if include_forgotten else " AND forgotten_at IS NULL"
        conn = await self._conn()
        try:
            try:
                async with conn.execute(
                    f"""SELECT e.* FROM episodes_fts f
                       JOIN episodes e ON f.rowid = e.rowid
                       WHERE episodes_fts MATCH ?{forgotten_clause}
                       ORDER BY rank LIMIT ?""",
                    (query, limit),
                ) as cur:
                    rows = await cur.fetchall()
            except Exception:
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

    async def knowledge_search(self, query: str, limit: int = 10) -> list[dict]:
        if self._kg_unified_enabled():
            return await self._knowledge_search_unified(query, limit)
        return await self._knowledge_search_flat(query, limit)

    async def _knowledge_search_unified(
        self, query: str, limit: int,
    ) -> list[dict]:
        """KG-routed search: hit ``entities_fts`` for matching entity
        names (subject OR object), then expand each match into the
        relations it participates in."""
        conn = await self._conn()
        try:
            try:
                async with conn.execute(
                    """SELECT e.id FROM entities_fts f
                       JOIN entities e ON f.rowid = e.rowid
                       WHERE entities_fts MATCH ? LIMIT ?""",
                    (query, max(limit * 4, 20)),
                ) as cur:
                    entity_ids = [r["id"] for r in await cur.fetchall()]
            except Exception:
                # FTS unavailable: fall back to LIKE on names.
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
        self, query: str, limit: int,
    ) -> list[dict]:
        conn = await self._conn()
        try:
            try:
                async with conn.execute(
                    """SELECT k.* FROM knowledge_fts f
                       JOIN knowledge k ON f.rowid = k.rowid
                       WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (query, limit),
                ) as cur:
                    rows = await cur.fetchall()
            except Exception:
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

    async def stats(self) -> dict:
        conn = await self._conn()
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
            await self._release(conn)
        working_sessions = len(self._working)

        kg_stats = self._kg.stats() if self._kg else {"entities": 0, "relations": 0}

        vec_count = 0
        try:
            vec_count = await self._vec_index.count()
        except Exception as exc:
            logger.debug("vec_index.count() failed: %s", exc)

        return {
            "notes": notes_count,
            "episodes": episodes_count,
            "knowledge_triples": knowledge_count,
            "execution_logs": exec_count,
            "wiki_pages": wiki_count,
            "session_snapshots": snapshot_count,
            "active_working_sessions": working_sessions,
            "embedded_chunks": chunk_count,
            "vec_index_count": vec_count,
            "vec_index_mode": self._backend_id + (" (indexed)" if self._vec_index.indexed else " (degraded)"),
            "embedding_provider": self._embedder.provider_name,
            "embed_queue_pending": self._embed_queue.pending,
            "knowledge_graph": kg_stats,
        }
