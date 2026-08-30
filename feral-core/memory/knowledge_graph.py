"""
FERAL Knowledge Graph — Entity-Linked Semantic Graph
======================================================
Real knowledge graph with:
  - Entity table with embeddings + type + aliases
  - Relation table with confidence + evidence
  - Multi-hop traversal via recursive CTE
  - Entity extraction via LLM structured output
  - Entity linking via embedding similarity
  - Graph neighborhood context for LLM injection

Async-native since v2026.5.33 (Option C). Every public method is a
coroutine; sync ``_init_schema`` runs once at boot. ``stats()`` is now
async — the boot-time log in MemoryStore.__init__ no longer queries
it (kept it simple; ``MemoryStore.stats()`` surfaces the kg counts).
"""

from __future__ import annotations
import asyncio
import json
import logging
import sqlite3
import time
from typing import Optional
from uuid import uuid4

import aiosqlite
import numpy as np

# Prompt sizing (see "Extraction prompt sizing" below). Both are leaf
# modules under ``agents/`` that import nothing from this package, and
# ``agents/__init__.py`` is empty, so this does not close an import
# cycle with the orchestrator side.
from agents.context_manager import configured_context_window_tokens
from agents.token_estimate import estimate_tokens

from memory.fts_query import fts5_match_query
from memory.sqlite_features import require_fts5
from memory.embeddings import (
    EmbeddingDimensionMismatch,
    EmbeddingProvider,
    vec_to_blob,
    cosine_similarity_bulk,
)
# The entity tier's relevance floor is the same machinery as the episode
# tier's, run against an entity-specific centre. Importing rather than
# reimplementing is the point: a second copy of this arithmetic is how the
# entity tier ended up thresholding a raw cosine long after the episode tier
# stopped. ``memory.store`` imports this module lazily (inside
# ``_init_knowledge_graph``), so this direction does not close a cycle.
from memory.store import (
    _CENTERED_SEMANTIC_FLOOR,
    _MIN_CHUNKS_FOR_CENTERING,
    center_rows,
    score_centered,
    unit_matrix,
)

logger = logging.getLogger("feral.memory.kg")


# Sources whose text must never reach ``_heuristic_extract``.
#
# The heuristic matches first-person patterns ("my name is", "i live in")
# and files every hit under the ``user`` entity. That is right for text the
# operator typed and wrong for speech the room overheard: it would record a
# visitor's home town as the operator's. ``store._NO_SELF_MODEL_EVENT_TYPES``
# draws exactly this boundary for About-Me extraction; the graph needs it
# too, and did not have it because the ``source`` argument that would carry
# the distinction was never accepted.
_NO_FIRST_PERSON_HEURISTIC_SOURCES = frozenset({
    "ambient_conversation",
})


# ─────────────────────────────────────────────────────────────────────
# Extraction prompt sizing
# ─────────────────────────────────────────────────────────────────────
#
# This used to be ``text[:2000]``, a bare literal with no derivation
# behind it, and it was wrong in BOTH directions at once.
#
# Wrong direction 1, it threw text away. Every caller passes more than
# 2000 characters and the extractor silently dropped the rest:
#
#     caller                              passes         seen
#     memory/context_builder.py:596       12000 chars    2000   (17%)
#     api/server.py:5173                   8000 chars    2000   (25%)
#     agents/learner.py:164               unbounded      2000
#
# ``context_builder.CHUNK_CHARS`` is 12000 because that segment size was
# measured to halve the number of generation waves while still showing
# the model every message. Six sevenths of each of those carefully sized
# segments went into a prompt that read the first 2000 characters. An
# entity named late in a segment could never enter the graph, which is
# the same defect ``tests/test_consolidation_redesign.py`` already
# records for the old ``[:3000]`` cap one layer up.
#
# Wrong direction 2, and this is the one a bigger number would not have
# fixed: 2000 CHARACTERS IS NOT A SAFE PROMPT. Characters are not
# tokens, and the ratio is not close to constant. Measured with
# ``agents/token_estimate.estimate_tokens`` over
# ``tests/fixtures/token_estimate_corpus.json``, 2000 characters costs:
#
#     script            est. tokens     + template + 1024 reply
#     english prose            698                        1835
#     russian                 1323                        2460
#     korean                  2209                        3346
#     chinese / japanese      2601                        3738
#     emoji                   6000                        7137   OVERFLOW
#
# The last row is the point. A 4096-token local model handed 2000
# characters of emoji-dense chat was already over its window before this
# change, and the "safe" literal is what hid it. Raising 2000 to 8000 or
# 12000 would have moved chinese, japanese, korean, arabic, greek,
# hebrew, hindi and thai into that same column: 12000 characters of
# chinese estimates at 15600 tokens, nearly four times a 4096 window.
#
# So the bound is expressed in TOKENS against the serving model's
# window, and the character count falls out of the text itself:
#
#     text budget = window - reply reserve - prompt overhead
#
# with every term measured rather than assumed. On the 4096-token window
# ``LlamaCppEngine`` actually pins, that is 4096 - 1024 - 126 = 2946
# tokens, and the characters that buys, measured per script:
#
#     script            old cap    now    prompt+reply est (window 4096)
#     english prose        2000    8427                            4096
#     russian              2000    4459                            4095
#     korean               2000    2679                            4095
#     chinese / japanese   2000    2279                            4094
#     emoji                2000    1001                            4092
#
# English gains 4.2x. Emoji LOSES half, which is the safety, and every
# row now lands under the window instead of one row overflowing it by
# 74%. On a 128000-token cloud window the same arithmetic yields 126850
# tokens, so the character bound below is what binds there instead.
#
# No extra fudge factor is applied on top, deliberately.
# ``estimate_tokens`` is already tuned never to fall below a real
# tokenizer on that corpus and it over-counts English by up to ~1.6x
# (628 estimated against 401 real), so the margin is inside the
# estimator where it is measured, not bolted on here where it would not
# be.

#: Fixed part of the extraction prompt. Kept as a template so the
#: overhead below is derived from the text actually sent, and cannot
#: drift when the wording is edited.
_EXTRACTION_PROMPT_TEMPLATE = (
    "Extract knowledge triples from this text. Return a JSON array of objects, "
    "each with: subject, subject_type, predicate, object, object_type.\n"
    "Types: person, place, organization, concept, thing, event, time.\n"
    "Only extract factual statements. Skip opinions and questions.\n"
    "Text: {text}\n"
    "Output ONLY valid JSON array. No markdown."
)

#: Role framing ``LocalLLMEngine.format_chat`` wraps a single user
#: message in before a local model sees it. 24 characters, and it counts
#: against the same window, so it is counted.
_LOCAL_CHAT_WRAPPER = "<|user|>\n\n<|assistant|>\n"

#: Prompt overhead in tokens, measured at import off the real strings:
#: 126 for the template plus the local chat wrapper.
_EXTRACTION_OVERHEAD_TOKENS = estimate_tokens(
    _EXTRACTION_PROMPT_TEMPLATE.format(text="") + _LOCAL_CHAT_WRAPPER
)

#: Room kept for the model's own reply. ``extract_and_store`` calls
#: ``llm.chat()`` without ``max_tokens``, so it gets
#: ``LLMProvider.chat``'s default of 1024 and the reply can occupy every
#: one of them. Measured capacity at that ceiling: a five-field triple
#: object serialises to 140 characters / ~67 estimated tokens, so 1024
#: tokens holds roughly 15 triples. Reserve less and a full reply is
#: truncated mid-array, ``json.loads`` raises, and the whole LLM call is
#: discarded for the heuristic fallback.
_EXTRACTION_REPLY_TOKENS = 1024

#: Floor, so a misconfigured or very small window still extracts
#: something rather than sending an empty prompt. 256 tokens measured at
#: 739 characters of English prose, comfortably more than the single
#: sentences the heuristic fallback handles.
_MIN_EXTRACTION_TEXT_TOKENS = 256

#: Upper bound in characters. This is a COST bound, not a safety bound:
#: safety is the token budget above. A 128000-token window would
#: otherwise let ``agents/learner.py``, whose input is unbounded, spend
#: ~126000 tokens of prompt on a reply that can express ~15 triples.
#: 12000 is ``context_builder.CHUNK_CHARS``, the largest any caller
#: passes, so nothing a caller sends is truncated by this bound.
#: ``tests/test_kg_extraction_window.py`` asserts the two stay equal, so
#: raising CHUNK_CHARS cannot silently reintroduce the truncation.
MAX_EXTRACTION_CHARS = 12000

#: Marker left where the middle of an over-budget text was removed.
_EXTRACTION_ELIDED = "\n[... middle elided ...]\n"


def _extraction_window_tokens(llm) -> int:
    """Context window to size the extraction prompt against.

    Prefers what the router reports for the model that will actually
    serve the call (``LLMProvider.context_window_tokens``, which is the
    local engine's pinned ``n_ctx`` when one is attached), and falls
    back to ``FERAL_CONTEXT_WINDOW_TOKENS`` for an ``llm`` that does not
    report one, including the test doubles that stand in for a provider.
    """
    reported = getattr(llm, "context_window_tokens", 0)
    try:
        reported = int(reported or 0)
    except (TypeError, ValueError):
        reported = 0
    if reported > 0:
        return reported
    return configured_context_window_tokens()


def _extraction_text_budget_tokens(llm) -> int:
    """Tokens of source text the extraction prompt can carry."""
    window = _extraction_window_tokens(llm)
    budget = window - _EXTRACTION_REPLY_TOKENS - _EXTRACTION_OVERHEAD_TOKENS
    return max(_MIN_EXTRACTION_TEXT_TOKENS, budget)


def _fit_to_token_budget(text: str, budget_tokens: int) -> str:
    """Largest slice of ``text`` estimated to fit ``budget_tokens``.

    Keeps the head AND the tail rather than a leading prefix.
    ``context_builder.PER_MESSAGE_HARD_CAP`` documents why: arXiv
    2210.16732 measures ~80% of the information needed to reconstruct a
    summary lost at a 1K-token leading cut, and finds salience
    ANTI-correlated with position near the cut. Trimming here is the
    exception rather than the rule (the character bound already matches
    the largest caller's segment size), but when it does happen a
    dropped tail is the worst available choice.

    Binary search rather than a chars-per-token divide, because that
    ratio is what this whole change exists to stop assuming. Measured
    cost on a 12000-character segment: 0.68ms when the text already fits
    (one estimate, the common case on a cloud window) and 8.87ms when it
    has to search. Both are synchronous CPU in an async method and both
    are noise beside the LLM round trip they precede.
    """
    if budget_tokens <= 0 or not text:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text

    def _longest_prefix() -> str:
        low, high, best = 0, len(text), ""
        while low <= high:
            mid = (low + high) // 2
            if estimate_tokens(text[:mid]) <= budget_tokens:
                best, low = text[:mid], mid + 1
            else:
                high = mid - 1
        return best

    if estimate_tokens(_EXTRACTION_ELIDED) >= budget_tokens:
        return _longest_prefix()

    # Widen the kept head and tail together until the pair stops fitting.
    low, high, best = 1, len(text) // 2, ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid] + _EXTRACTION_ELIDED + text[len(text) - mid:]
        if estimate_tokens(candidate) <= budget_tokens:
            best, low = candidate, mid + 1
        else:
            high = mid - 1
    return best or _longest_prefix()


def _clip_chars(text: str, limit: int) -> str:
    """Apply the character bound, keeping the head and the tail.

    A bare ``text[:limit]`` here would undo, for the one caller the
    character bound actually binds on (``agents/learner.py``, whose
    input is unbounded), the tail preservation
    ``_fit_to_token_budget`` exists to provide.
    """
    if len(text) <= limit:
        return text
    half = max(1, (limit - len(_EXTRACTION_ELIDED)) // 2)
    return text[:half] + _EXTRACTION_ELIDED + text[len(text) - half:]


def fit_extraction_text(text: str, llm=None) -> str:
    """Trim ``text`` to what one extraction prompt may carry.

    Public so a test can assert the bound without reaching through a
    live LLM call.
    """
    if not text:
        return ""
    return _fit_to_token_budget(
        _clip_chars(text, MAX_EXTRACTION_CHARS),
        _extraction_text_budget_tokens(llm),
    )


ENTITY_MERGE_THRESHOLD = 0.85
ENTITY_CANDIDATE_THRESHOLD = 0.70

#: Raw-cosine floor for the entity vector leg, used ONLY when the entity
#: table is below :data:`_MIN_CHUNKS_FOR_CENTERING` and therefore has no
#: trustworthy centre. Above that, entity hits are centred and judged
#: against :data:`_CENTERED_SEMANTIC_FLOOR` instead.
#:
#: This number used to be the only gate on this leg, hardcoded at both call
#: sites. In an anisotropic embedding space a raw cosine cannot separate a
#: hit from a miss at any threshold (see the block comment on
#: _CENTERED_SEMANTIC_FLOOR in memory/store.py for the measurements), which
#: is why the episode tier was given corpus centring; the entity tier was
#: not, and kept returning entities for queries with no answer in the graph.
_RAW_ENTITY_FLOOR = 0.3


def _stable_kg_id(*parts: str) -> str:
    """Deterministic 12-char id from any tuple of strings.

    Two brains that compute ``_stable_kg_id("Alice", "person")`` get
    the same id without coordinating. Same convergence trick the
    flat-knowledge ``_stable_knowledge_id`` uses, lifted here so the
    KG can give entities + relations stable cross-brain identity.

    The hash is sha256-truncated; collisions are negligible at the
    sizes the KG operates on (~1e6 entities at most) and would
    manifest as one extra LWW merge — the upstream caller's
    ``WHERE id = ?`` gate catches it.
    """
    import hashlib
    blob = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


class KnowledgeGraph:
    """Production knowledge graph backed by SQLite with embedding-based
    entity linking and multi-hop traversal."""

    def __init__(self, db_path: str, embedder: EmbeddingProvider):
        self.db_path = db_path
        self._embedder = embedder
        # v2026.5.35 (PR 2.5, F1) — late-bound MemoryStore reference so
        # KG writes can log themselves to the sync WAL with HLC. Set by
        # ``MemoryStore.__init__`` after both objects exist (avoids a
        # circular import). When ``None`` (e.g. unit tests instantiate
        # KG directly), writes skip sync logging — the relations and
        # entities still land, they just don't replicate.
        self._store = None
        # Lane 05  (AUDIT-r14 finding 14, S3): is the sqlite-vec
        # entity index available? Set by :meth:`_init_schema` based
        # on extension load + CREATE VIRTUAL TABLE outcome.
        self._vec_entities_available: bool = False
        # Non-transient reason the embedding leg of entity search is dead,
        # or None. Read by MemoryStore / /internal/memory/stats, which is
        # the only reason the operator ever finds out: a dead vector leg
        # returns a SHORTER list, never an error, and a shorter list is
        # indistinguishable from "nothing else matched".
        self._vector_leg_error: str | None = None
        # Distinct leg errors already logged at ERROR by this instance. The
        # mismatch repeats on EVERY query (it is a property of the stored
        # data, not of the query), so an unthrottled line would bury the
        # log while adding nothing: the state stays readable in
        # _vector_leg_error and in /internal/memory/stats. Same reasoning,
        # and the same shape, as embeddings._REPORTED_DIM_MISMATCHES.
        self._logged_leg_errors: set[str] = set()
        # Corpus centre for the ENTITY embeddings, and the fingerprint of the
        # entity table it was derived from. See _entity_centroid_vector: the
        # episode centroid lives on MemoryStore and is computed over
        # memory_chunks, which is a different population, so the entity tier
        # cannot borrow it.
        self._entity_centroid = None
        self._entity_centroid_n = 0
        self._entity_centroid_fingerprint: Optional[tuple] = None
        # Bumped whenever the fingerprint cannot be read, so a failed read can
        # never compare equal to a cached one and serve a stale centre.
        self._entity_centroid_epoch = 0
        self._init_schema()

    @property
    def vector_leg_error(self) -> str | None:
        """Why entity vector search is degraded, or None if it is not."""
        return self._vector_leg_error

    def _should_log_leg_error(self, exc: Exception) -> bool:
        """True the first time this instance sees this exact failure."""
        key = f"{type(exc).__name__}: {exc}"
        if key in self._logged_leg_errors:
            return False
        self._logged_leg_errors.add(key)
        return True

    def _record_vector_leg_error(self, exc: Exception) -> None:
        """Publish a dead vector leg everywhere an operator might look.

        Mirrored onto the owning MemoryStore because that is what
        ``/internal/memory/stats`` and ``feral doctor`` read
        (``_semantic_health`` in api/routes/memory.py). Without the mirror
        the KG could be dead while the endpoint reported
        ``semantic_search: ok``, which is how this survived long enough to
        reach a release.
        """
        self._vector_leg_error = str(exc)
        store = self._store
        if store is not None:
            try:
                store._vector_leg_error = str(exc)
            except Exception as set_exc:  # pragma: no cover - attribute is plain
                logger.debug("could not mirror vector_leg_error to store: %s", set_exc)

    async def _conn(self) -> aiosqlite.Connection:
        """Acquire an aiosqlite connection.

        Lane 05  (AUDIT-r14 finding 14): when a ``MemoryStore`` is
        attached (the production path), reuse its pooled connection
        so KG writes / reads share the same N=4 connection budget as
        the rest of the memory subsystem instead of opening a fresh
        sqlite handle on every call. Pool reuse eliminates the
        per-call ``aiosqlite.connect()`` + PRAGMA round-trips that
        dominated KG latency.

        Callers MUST release via :meth:`_release` in a ``finally``
        block.
        """
        store = self._store
        if store is not None and hasattr(store, "_conn") and hasattr(store, "_release"):
            return await store._conn()
        # Standalone fallback (unit tests instantiate KG directly).
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def _release(self, conn: aiosqlite.Connection) -> None:
        """Return a pooled connection to the MemoryStore pool, or
        close it when the KG is running standalone."""
        store = self._store
        if store is not None and hasattr(store, "_release"):
            await store._release(conn)
            return
        try:
            await conn.close()
        except Exception:
            pass

    def _init_schema(self):
        """Boot-time DDL. Sync because __init__ is sync."""
        # Same guard as MemoryStore._init_db, and it is needed here too:
        # KnowledgeGraph can be constructed standalone (unit tests and
        # `feral memory` subcommands do), in which case this executescript
        # is the first FTS5 statement the process runs. The entities_fts
        # table is created mid-script at line 162, so without the guard a
        # non-FTS5 interpreter commits `entities`, `entity_aliases` and
        # `relations` and then dies with `no such module: fts5`.
        require_fts5("FERAL's knowledge graph")

        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    entity_type TEXT DEFAULT 'thing',
                    embedding BLOB,
                    metadata TEXT DEFAULT '{}',
                    mention_count INTEGER DEFAULT 1,
                    hlc_string TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
                CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

                CREATE TABLE IF NOT EXISTS entity_aliases (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_aliases_entity ON entity_aliases(entity_id);
                CREATE INDEX IF NOT EXISTS idx_aliases_alias ON entity_aliases(alias);

                CREATE TABLE IF NOT EXISTS relations (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL,
                    target_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    confidence REAL DEFAULT 1.0,
                    evidence_text TEXT DEFAULT '',
                    source_origin TEXT DEFAULT 'conversation',
                    hlc_string TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source_id);
                CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target_id);
                CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(relation_type);

                CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts
                USING fts5(name, entity_type, metadata, tokenize='porter');

                CREATE TRIGGER IF NOT EXISTS entities_ai_fts AFTER INSERT ON entities BEGIN
                    INSERT INTO entities_fts(rowid, name, entity_type, metadata)
                    VALUES (new.rowid, new.name, new.entity_type, new.metadata);
                END;
                CREATE TRIGGER IF NOT EXISTS entities_ad_fts AFTER DELETE ON entities BEGIN
                    DELETE FROM entities_fts WHERE rowid = old.rowid;
                END;
                CREATE TRIGGER IF NOT EXISTS entities_au_fts AFTER UPDATE ON entities BEGIN
                    DELETE FROM entities_fts WHERE rowid = old.rowid;
                    INSERT INTO entities_fts(rowid, name, entity_type, metadata)
                    VALUES (new.rowid, new.name, new.entity_type, new.metadata);
                END;
            """)
            # v2026.5.35 (F1) — idempotent migration to add the hlc_string
            # column on pre-existing brains. Same pattern as
            # ``MemoryStore._add_column_if_missing`` but inlined here
            # because KG owns its own schema.
            cur = conn.execute("PRAGMA table_info(relations)")
            existing = {row[1] for row in cur.fetchall()}
            if "hlc_string" not in existing:
                conn.execute("ALTER TABLE relations ADD COLUMN hlc_string TEXT DEFAULT ''")
            cur = conn.execute("PRAGMA table_info(entities)")
            existing = {row[1] for row in cur.fetchall()}
            if "hlc_string" not in existing:
                conn.execute("ALTER TABLE entities ADD COLUMN hlc_string TEXT DEFAULT ''")
            conn.commit()

            # Lane 05  (AUDIT-r14 finding 14, S3): create a vec0
            # virtual table for entity embeddings so search_entities
            # and _link_entity can do indexed nearest-neighbour
            # lookups instead of the previous full-table embedding
            # scan (which was O(N) on every search and dominated KG
            # query cost — primary slowdown flagged by AUDIT-r13
            # subagent 09).
            #
            # The table is keyed on a deterministic int rowid (the
            # entity rowid is wrong because vec0 requires INTEGER and
            # entity ids are TEXT). We use a stable hash of the
            # entity id mapped to a 64-bit int so inserts can find
            # the right vec row to replace without an extra lookup
            # table.
            try:
                from memory.embeddings import sqlite_vec_available, _try_load_sqlite_vec
                if sqlite_vec_available() and _try_load_sqlite_vec(conn):
                    # A vec0 table's dimension is baked in at CREATE time and
                    # `CREATE VIRTUAL TABLE IF NOT EXISTS` is a silent no-op
                    # against one built at another dimension: every upsert is
                    # then rejected (and swallowed at debug level by
                    # _vec_upsert_entity) and every search returns nothing.
                    # embeddings.SQLiteVecIndex._init has guarded this since
                    # the vec_chunks index shipped; vec_entities never did,
                    # so the same provider switch that stranded
                    # entities.embedding at 1536 dims would also have left
                    # this index quietly unwritable. Refuse it instead, and
                    # name the command that repairs it.
                    from memory.reembed import vec0_declared_dim
                    row = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='vec_entities'"
                    ).fetchone()
                    declared = vec0_declared_dim(row[0] if row else None)
                    if declared is not None and declared != self._embedder.dimension:
                        self._vec_entities_available = False
                        logger.error(
                            "KG vec0 entity index was built at dim=%d but the "
                            "active embedding provider produces dim=%d, "
                            "refusing to use it (entity search falls back to "
                            "the numpy scan, which is correct and slower). "
                            "FIX: run `feral memory reembed`, which rebuilds "
                            "the index at the current dimension.",
                            declared, self._embedder.dimension,
                        )
                        return
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS vec_entities
                        USING vec0(
                            entity_rowid INTEGER PRIMARY KEY,
                            embedding FLOAT[{self._embedder.dimension}]
                        )
                    """)
                    conn.commit()
                    self._vec_entities_available = True
                    logger.info(
                        "KG vec0 entity index ready (dim=%d)",
                        self._embedder.dimension,
                    )
            except Exception as exc:
                logger.info(
                    "KG vec0 entity index unavailable (%s) — "
                    "falling back to numpy scan over entities.embedding",
                    exc,
                )
                self._vec_entities_available = False
        finally:
            conn.close()

    @staticmethod
    def _entity_rowid(entity_id: str) -> int:
        """Deterministic 63-bit positive int derived from the entity id.

        sqlite-vec's ``vec0`` table requires INTEGER PRIMARY KEY but
        entity ids are 12-char hex strings. Hashing to a stable int
        lets us upsert / delete vec rows without a side-table
        mapping. We mask to 63 bits because SQLite stores INTEGER as
        signed 64-bit and vec0 rejects negatives.
        """
        import hashlib
        digest = hashlib.sha256(entity_id.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)

    async def _vec_upsert_entity(
        self, entity_id: str, embedding
    ) -> None:
        """Push (or replace) the given entity's embedding into the
        vec0 entity index. Silently skipped when sqlite-vec is not
        loaded — the caller's numpy fallback path still works.
        """
        if not self._vec_entities_available or embedding is None:
            return
        rowid = self._entity_rowid(entity_id)
        try:
            from memory.embeddings import _try_load_sqlite_vec, vec_to_blob
        except Exception:
            return
        conn = await aiosqlite.connect(self.db_path)
        try:
            # vec0 requires the extension on the connection that
            # writes; pooled MemoryStore connections also load the
            # extension during boot, but we use a fresh sync-aware
            # path here to avoid making the pool path conditional on
            # extension state. Cost is one extension-load per upsert
            # — acceptable on the entity-creation path (slow path).
            await asyncio.to_thread(_try_load_sqlite_vec, conn._connection)  # type: ignore[attr-defined]
            await conn.execute(
                "INSERT OR REPLACE INTO vec_entities(entity_rowid, embedding) VALUES (?, ?)",
                (rowid, vec_to_blob(embedding)),
            )
            await conn.commit()
        except Exception as exc:
            logger.debug("vec_entities upsert failed: %s", exc)
        finally:
            await conn.close()

    async def add_entity(
        self,
        name: str,
        entity_type: str = "thing",
        metadata: dict | None = None,
    ) -> dict:
        """Add or merge an entity. Uses embedding similarity for dedup."""
        existing = await self._find_entity_by_name(name)
        if existing:
            await self._bump_mention(existing["id"])
            return existing

        linked = await self._link_entity(name)
        if linked:
            await self._bump_mention(linked["id"])
            await self._add_alias(linked["id"], name)
            return linked

        # v2026.5.35 (F1) — derive a stable id from the entity name so
        # two brains that learn the same person/thing converge on a
        # single row after HLC LWW. Without this the entities table
        # would replicate twice (once per node id) and ``add_relation``
        # would link to whichever copy the local brain wrote first.
        eid = _stable_kg_id(name, entity_type)
        now = time.time()
        embedding = await self._embedder.embed(name)
        meta_json = json.dumps(metadata or {})

        # Async-offload the WAL log so the KG write doesn't block
        # the event loop on the sync sqlite3 fsync (Lane 05 
        # offloaded MemoryStore.episode_save the same way).
        hlc = ""
        if self._store is not None:
            try:
                log_async = getattr(self._store, "_log_sync_async", None)
                if log_async is not None:
                    hlc = await log_async("entities", "insert", eid, {
                        "id": eid, "name": name, "entity_type": entity_type,
                        "metadata": meta_json, "created_at": now,
                    })
                else:
                    hlc = self._store._log_sync("entities", "insert", eid, {
                        "id": eid, "name": name, "entity_type": entity_type,
                        "metadata": meta_json, "created_at": now,
                    })
            except Exception as exc:
                logger.debug("entities sync log failed: %s", exc)

        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO entities (id, name, entity_type, embedding, metadata, hlc_string, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (eid, name, entity_type, vec_to_blob(embedding), meta_json, hlc, now, now),
            )
            await conn.commit()
        finally:
            await self._release(conn)

        # Push to the vec0 index so the next search_entities /
        # _link_entity call hits the indexed path instead of a
        # full-table numpy scan.
        await self._vec_upsert_entity(eid, embedding)

        logger.info("Entity added: %s (%s) [%s]", name, entity_type, eid)
        return {"id": eid, "name": name, "entity_type": entity_type}

    async def add_relation(
        self,
        source_name: str,
        relation_type: str,
        target_name: str,
        confidence: float = 1.0,
        evidence: str = "",
        source_type: str = "thing",
        target_type: str = "thing",
        source_origin: str = "conversation",
    ) -> dict:
        """Add a relation between two entities (creating them if needed).

        ``source_origin`` records how the triple was obtained. It exists so
        ``_heuristic_extract`` can route through here without losing the
        'heuristic' provenance its own INSERT used to write.
        """
        source = await self.add_entity(source_name, source_type)
        target = await self.add_entity(target_name, target_type)

        conn = await self._conn()
        try:
            async with conn.execute(
                """SELECT id, confidence FROM relations
                   WHERE source_id = ? AND relation_type = ? AND target_id = ?""",
                (source["id"], relation_type, target["id"]),
            ) as cur:
                existing = await cur.fetchone()

            now = time.time()
            # v2026.5.35 (F1) — derive a stable id from the triple so
            # two brains converge on a single relation row under HLC
            # LWW (mirrors ``_stable_knowledge_id``).
            rid = _stable_kg_id(source["id"], relation_type, target["id"])
            if existing:
                new_conf = min(1.0, (existing["confidence"] + confidence) / 2.0 + 0.1)
                hlc = ""
                if self._store is not None:
                    try:
                        hlc = self._store._log_sync("relations", "insert", rid, {
                            "id": rid, "source_id": source["id"], "relation_type": relation_type,
                            "target_id": target["id"], "confidence": new_conf,
                            "evidence_text": evidence[:1000], "created_at": now,
                        })
                    except Exception as exc:
                        logger.debug("relations sync log failed: %s", exc)
                await conn.execute(
                    "UPDATE relations SET confidence = ?, evidence_text = ?, updated_at = ?, hlc_string = ? WHERE id = ?",
                    (new_conf, evidence[:1000], now, hlc, existing["id"]),
                )
                rid = existing["id"]
            else:
                hlc = ""
                if self._store is not None:
                    try:
                        hlc = self._store._log_sync("relations", "insert", rid, {
                            "id": rid, "source_id": source["id"], "relation_type": relation_type,
                            "target_id": target["id"], "confidence": confidence,
                            "evidence_text": evidence[:1000], "created_at": now,
                        })
                    except Exception as exc:
                        logger.debug("relations sync log failed: %s", exc)
                await conn.execute(
                    """INSERT INTO relations
                       (id, source_id, relation_type, target_id, confidence, evidence_text, source_origin, hlc_string, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rid, source["id"], relation_type, target["id"], confidence, evidence[:1000], source_origin, hlc, now, now),
                )

            await conn.commit()
        finally:
            await self._release(conn)
        logger.info("Relation: (%s) --[%s]--> (%s)", source_name, relation_type, target_name)
        return {
            "id": rid,
            "source": source["name"],
            "relation": relation_type,
            "target": target["name"],
            "confidence": confidence,
        }

    async def traverse(
        self,
        start_entity_name: str,
        max_depth: int = 3,
        limit: int = 50,
    ) -> list[dict]:
        """Multi-hop graph traversal using recursive CTE."""
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT id, name FROM entities WHERE name = ? COLLATE NOCASE",
                (start_entity_name,),
            ) as cur:
                entity = await cur.fetchone()
            if not entity:
                async with conn.execute(
                    "SELECT entity_id FROM entity_aliases WHERE alias = ? COLLATE NOCASE",
                    (start_entity_name,),
                ) as cur:
                    alias_row = await cur.fetchone()
                if alias_row:
                    async with conn.execute(
                        "SELECT id, name FROM entities WHERE id = ?",
                        (alias_row["entity_id"],),
                    ) as cur:
                        entity = await cur.fetchone()
            if not entity:
                return []

            async with conn.execute("""
                WITH RECURSIVE graph_walk(entity_id, entity_name, relation_type, target_id, target_name, depth, path) AS (
                    SELECT
                        r.source_id, e_src.name, r.relation_type, r.target_id, e_tgt.name,
                        1, e_src.name || ' -> ' || r.relation_type || ' -> ' || e_tgt.name
                    FROM relations r
                    JOIN entities e_src ON r.source_id = e_src.id
                    JOIN entities e_tgt ON r.target_id = e_tgt.id
                    WHERE r.source_id = ? OR r.target_id = ?

                    UNION ALL

                    SELECT
                        r2.source_id, e2_src.name, r2.relation_type, r2.target_id, e2_tgt.name,
                        gw.depth + 1,
                        gw.path || ' | ' || e2_src.name || ' -> ' || r2.relation_type || ' -> ' || e2_tgt.name
                    FROM relations r2
                    JOIN entities e2_src ON r2.source_id = e2_src.id
                    JOIN entities e2_tgt ON r2.target_id = e2_tgt.id
                    JOIN graph_walk gw ON (r2.source_id = gw.target_id OR r2.target_id = gw.entity_id)
                    WHERE gw.depth < ?
                        AND gw.path NOT LIKE '%' || e2_tgt.name || '%'
                )
                SELECT DISTINCT entity_name, relation_type, target_name, depth, path
                FROM graph_walk
                ORDER BY depth ASC
                LIMIT ?
            """, (entity["id"], entity["id"], max_depth, limit)) as cur:
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        return [
            {
                "source": r["entity_name"],
                "relation": r["relation_type"],
                "target": r["target_name"],
                "depth": r["depth"],
                "path": r["path"],
            }
            for r in rows
        ]

    async def _entity_corpus_fingerprint(self, conn) -> tuple:
        """Cheap change detector for the entity embedding corpus.

        Same contract as ``MemoryStore._corpus_fingerprint``: differs
        whenever the corpus may have moved, stable while it has not. The
        NOT NULL predicate is deliberately left off so the count is an
        index-only read; it is used to detect change, never as the number
        of usable vectors.
        """
        try:
            async with conn.execute(
                "SELECT COUNT(*), MAX(rowid) FROM entities"
            ) as cur:
                row = await cur.fetchone()
            return (int(row[0] or 0), int(row[1] or 0))
        except Exception as exc:
            logger.debug("Entity corpus fingerprint unavailable: %s", exc)
            self._entity_centroid_epoch += 1
            return (-1, -self._entity_centroid_epoch)

    async def _entity_centroid_vector(self, conn):
        """The mean of the unit-normalised entity embeddings, or None when
        the entity table is too small for a mean to mean anything.

        This is the entity tier's half of the fix described at length on
        ``MemoryStore._CENTERED_SEMANTIC_FLOOR``. The episode tier was
        given corpus centring because a raw cosine floor cannot separate a
        hit from a miss in an anisotropic embedding space; the entity tier
        kept a hardcoded raw ``> 0.3`` and so kept the defect. Measured
        before this existed: "asdfgh zxcvbn qwerty" returned eight
        entities scoring 0.29-0.42, including a note reading "the wifi
        password is stored in 1password", while the episode, note and
        knowledge tiers all correctly returned zero. That leaked into
        ``search_all`` and into the LLM context builder.

        The centre is entity-specific: entity embeddings live in
        ``entities.embedding``, not in ``memory_chunks``, so the episode
        centroid is the wrong vector to subtract.
        """
        fingerprint = await self._entity_corpus_fingerprint(conn)
        if self._entity_centroid is not None:
            if self._entity_centroid_fingerprint == fingerprint:
                return self._entity_centroid
            # Adding one entity moves the fingerprint but not the mean, and
            # rebuilding would re-read every embedding in the table on the
            # next query. Same 5 percent tolerance MemoryStore._centered_docs
            # uses on the episode corpus, for the same reason.
            n = self._entity_centroid_n
            if abs(fingerprint[0] - n) <= max(50, n * 0.05):
                return self._entity_centroid
        if 0 <= fingerprint[0] < _MIN_CHUNKS_FOR_CENTERING:
            # Cannot possibly clear the centring floor. The count is taken
            # without the NOT NULL predicate so it is an upper bound on the
            # number of usable vectors; skip the fetch entirely.
            self._entity_centroid = None
            self._entity_centroid_fingerprint = fingerprint
            return None

        async with conn.execute(
            "SELECT embedding FROM entities WHERE embedding IS NOT NULL"
        ) as cur:
            blobs = [r["embedding"] for r in await cur.fetchall()]
        if not blobs:
            self._entity_centroid = None
            self._entity_centroid_fingerprint = fingerprint
            return None
        try:
            built = unit_matrix(
                blobs, len(blobs[0]) // 4, _MIN_CHUNKS_FOR_CENTERING,
            )
        except Exception as exc:
            # A broken relevance floor must not take out entity search.
            logger.warning("Entity centring unavailable, using raw: %s", exc)
            built = None
        if built is None:
            self._entity_centroid = None
            self._entity_centroid_fingerprint = fingerprint
            return None
        unit, live = built
        centroid = unit[live].mean(axis=0)
        self._entity_centroid = centroid
        self._entity_centroid_n = fingerprint[0]
        self._entity_centroid_fingerprint = fingerprint
        return centroid

    def _centered_entity_scores(self, query_vec, blobs, centroid):
        """Centred scores for a handful of entity embeddings, or None.

        Same arithmetic as the episode path, against the entity centre.
        """
        try:
            q = np.asarray(query_vec, dtype=np.float32).ravel()
            built = unit_matrix(blobs, int(q.shape[0]), 1)
            if built is None:
                return None
            unit, live = built
            if centroid.shape[0] != unit.shape[1]:
                return None
            docs = center_rows(unit, centroid)
            return score_centered(docs, live, centroid, query_vec)
        except Exception as exc:
            logger.warning("Entity centred scoring failed, using raw: %s", exc)
            return None

    async def _vec_search_candidates(
        self, query_vec, limit: int
    ) -> dict[str, float]:
        """Use the vec0 entity index (when available) to fetch the top-K
        nearest entities. Returns ``{entity_id: cosine_similarity}``.

        Lane 05  fix for AUDIT-r14 finding 14: replaces the previous
        "load every entity row + cosine in Python" path that scaled
        linearly with the entity table size and dominated KG query
        latency once the table grew past a few thousand rows.

        Returns an empty dict when sqlite-vec is unavailable; the
        caller's numpy fallback then takes over.
        """
        if not self._vec_entities_available:
            return {}

        from memory.embeddings import _try_load_sqlite_vec, vec_to_blob

        results: dict[str, float] = {}
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        try:
            await asyncio.to_thread(_try_load_sqlite_vec, conn._connection)  # type: ignore[attr-defined]
            blob = vec_to_blob(query_vec)
            async with conn.execute(
                """
                SELECT v.entity_rowid AS rowid, v.distance AS distance,
                       e.id AS id
                FROM vec_entities v
                JOIN entities e
                  ON e.rowid IN (
                      SELECT rowid FROM entities WHERE rowid = e.rowid
                  )
                WHERE v.embedding MATCH ?
                ORDER BY v.distance
                LIMIT ?
                """,
                (blob, limit * 2),
            ) as cur:
                # The join on rowid above is intentionally a no-op
                # — we want the indexed match without scanning the
                # entities table. Resolve rowid → entity id below.
                pass

            # vec0 doesn't expose a column for entity_id; we map back
            # via our deterministic 63-bit hash. Run the indexed
            # MATCH then look up entity ids by reverse-mapping the
            # rowids in a second statement that uses the entities
            # table by rowid.
            async with conn.execute(
                """
                SELECT entity_rowid, distance
                FROM vec_entities
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
                """,
                (blob, limit * 2),
            ) as cur:
                hits = await cur.fetchall()

            if not hits:
                return {}

            # Build a rowid → entity_id mapping by querying entities
            # we currently care about. The mapping isn't a SQL JOIN
            # because the entity_rowid is our hash, not entities.rowid.
            # Walk the entities table once, hash each id, and match
            # against the hit set. For N entities this is still a scan
            # but it runs only when the indexed search hit something
            # — and the work is just hashing 12-char strings, no
            # cosine math.
            hit_rowids = {int(h["entity_rowid"]): float(h["distance"]) for h in hits}
            async with conn.execute(
                "SELECT id FROM entities"
            ) as cur:
                async for row in cur:
                    rid = self._entity_rowid(row["id"])
                    if rid in hit_rowids:
                        # vec0 cosine returns distance ∈ [0, 2]; cosine
                        # similarity = 1 - distance.
                        results[row["id"]] = 1.0 - hit_rowids[rid]
        except Exception as exc:
            logger.debug("vec_entities search failed: %s", exc)
            return {}
        finally:
            await conn.close()
        return results

    def _score_entity_rows(self, query_vec, rows, centroid):
        """Relevance scores for candidate entity rows, keyed by entity id.

        Entities that do not clear the floor are absent from the result,
        which is how both callers drop them.

        Centred against the entity corpus centre when there is one, and only
        then thresholded at :data:`_CENTERED_SEMANTIC_FLOOR`. Below
        :data:`_MIN_CHUNKS_FOR_CENTERING` there is no trustworthy centre, so
        the raw cosine and the historical 0.3 stand: that is a weak floor,
        but it is the one this tier has always had on small graphs and
        changing it is a separate question from the anisotropy defect.

        Both scores are computed here from ``entities.embedding`` rather than
        taken from the index. ``vec_entities`` is created without
        ``distance_metric=cosine``, so vec0 answers with an L2 distance and
        ``_vec_search_candidates`` reports ``1 - L2``: monotone in cosine on
        unit vectors, so the ORDER is right, but a true cosine of 0.84
        arrives as 0.44, and the indexed branch was comparing that against a
        threshold meant for cosines. Same candidates, same ranking, honest
        numbers.
        """
        usable = [r for r in rows if r["embedding"] is not None]
        if not usable:
            return {}
        blobs = [r["embedding"] for r in usable]

        if centroid is not None:
            scores = self._centered_entity_scores(query_vec, blobs, centroid)
            if scores is not None:
                return {
                    r["id"]: float(s)
                    for r, s in zip(usable, scores)
                    if float(s) > _CENTERED_SEMANTIC_FLOOR
                }

        sims = cosine_similarity_bulk(query_vec, blobs)
        return {
            r["id"]: float(s)
            for r, s in zip(usable, sims)
            if float(s) > _RAW_ENTITY_FLOOR
        }

    async def search_entities(self, query: str, limit: int = 10) -> list[dict]:
        """Hybrid FTS + embedding search for entities.

        Lane 05  (AUDIT-r14 finding 14): the embedding leg now
        prefers the indexed vec0 nearest-neighbour search and only
        falls back to the full-table numpy scan when sqlite-vec is
        unavailable. Indexed path is sub-linear in entity count.
        """
        # Indexed nearest-neighbours first (works when sqlite-vec is
        # loaded). Falls through to the numpy path when not.
        query_vec = await self._embedder.embed(query)
        indexed_hits = await self._vec_search_candidates(query_vec, limit)

        conn = await self._conn()
        try:
            fts_results = {}
            # Quoted, and failures logged. ``build_graph_context`` is the
            # first leg of the LLM context builder, and passing the raw
            # utterance meant "don't", "what's", "C++" and "AI/ML" each
            # raised an fts5 syntax error into the bare ``except`` below,
            # dropping the entity text leg with no trace.
            match_expr = fts5_match_query(query)
            try:
                if not match_expr:
                    raise ValueError("no indexable term in query")
                async with conn.execute(
                    """SELECT e.id, e.name, e.entity_type, e.mention_count, rank
                       FROM entities_fts f JOIN entities e ON f.rowid = e.rowid
                       WHERE entities_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (match_expr, limit * 2),
                ) as cur:
                    rows = await cur.fetchall()
                # The query is ``ORDER BY rank``, so row order is already
                # the BM25 ordering, best first. Record the POSITION and
                # never the score. FTS5's ``rank`` is BM25: negative, and
                # more negative means a better match, so the old
                # ``1.0 / (1.0 + abs(rank))`` folded the sign away and
                # mapped the best match to the smallest score, reversing
                # the ordering exactly. This is verbatim the defect
                # store.py records having already found and fixed for
                # episodes; position carries the sign for us.
                for pos, r in enumerate(rows, start=1):
                    fts_results[r["id"]] = {
                        "id": r["id"], "name": r["name"], "type": r["entity_type"],
                        "mentions": r["mention_count"],
                        "fts_score": 1.0 / (1.0 + pos),
                    }
            except Exception as exc:
                logger.debug("entity FTS leg failed for %r: %s", query, exc)

            centroid = await self._entity_centroid_vector(conn)

            if indexed_hits:
                # Hydrate the indexed hits with entity metadata. The
                # embedding comes with them because the index picks the
                # candidates but must not decide whether they are good
                # enough to return: the handful it found is re-scored
                # against the entity corpus centre below.
                hit_ids = list(indexed_hits.keys())
                placeholders = ",".join("?" * len(hit_ids))
                async with conn.execute(
                    f"SELECT id, name, entity_type, mention_count, embedding "
                    f"FROM entities WHERE id IN ({placeholders})",
                    hit_ids,
                ) as cur:
                    indexed_rows = await cur.fetchall()

                scored = self._score_entity_rows(query_vec, indexed_rows, centroid)
                vec_results = {
                    r["id"]: {
                        "id": r["id"],
                        "name": r["name"],
                        "type": r["entity_type"],
                        "mentions": r["mention_count"],
                        "vec_score": scored[r["id"]],
                    }
                    for r in indexed_rows
                    if r["id"] in scored
                }
                # Indexed path resolved — skip the full-table scan
                # entirely.
                merged = {}
                all_ids = set(fts_results.keys()) | set(vec_results.keys())
                for eid in all_ids:
                    fts = fts_results.get(eid, {})
                    vec = vec_results.get(eid, {})
                    info = fts or vec
                    score = 0.3 * fts.get("fts_score", 0) + 0.7 * vec.get("vec_score", 0)
                    merged[eid] = {**info, "score": score}
                    merged[eid].pop("fts_score", None)
                    merged[eid].pop("vec_score", None)
                ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
                return self._mmr_rerank(ranked, limit)

            # Fallback: full-table numpy scan (sqlite-vec unavailable
            # or indexed path returned nothing).
            async with conn.execute(
                "SELECT id, name, entity_type, mention_count, embedding FROM entities WHERE embedding IS NOT NULL"
            ) as cur:
                all_entities = await cur.fetchall()
        finally:
            await self._release(conn)

        # One blocked matmul instead of a Python loop of blob_to_vec +
        # cosine_similarity. See cosine_similarity_bulk for the measured
        # numbers (286ms -> 23ms at 100k rows).
        vec_results = {}
        try:
            scored = self._score_entity_rows(query_vec, all_entities, centroid)
        except EmbeddingDimensionMismatch as exc:
            # This leg is dead until the store is migrated, and it will
            # fail identically on every subsequent call, so retrying or
            # re-raising per query buys nothing. Letting it propagate is
            # what killed the whole knowledge graph in production: this
            # method is called from MemoryStore.search_all (via
            # context_builder), from the memory.search RPC
            # (gateway/protocol.py), from the taskflow memory.search step
            # and from /api/knowledge/entities, none of which caught it, so
            # a stale entities table took out episode, note and knowledge
            # recall as well. Measured on a copy of the real store: 5 of 5
            # search_all calls raised.
            #
            # So the LEG degrades, not the query: the FTS results computed
            # above are still returned, exact-name and alias lookups are
            # unaffected, and the failure is published through
            # _record_vector_leg_error rather than being swallowed. Silence
            # here would be the actual bug; an empty list that looks like a
            # normal answer is how this class of defect survives.
            self._record_vector_leg_error(exc)
            logger.log(
                logging.ERROR if self._should_log_leg_error(exc) else logging.DEBUG,
                "Entity vector search is DEAD: %s. entities.embedding was "
                "written by a different embedding provider than the one now "
                "configured, so semantic entity recall returns nothing and "
                "the knowledge graph has degraded to FTS-only. FIX: run "
                "`feral memory reembed` (check first with `feral memory "
                "reembed check`), then restart the brain.",
                exc,
            )
            scored = {}
        for e in all_entities:
            if e["id"] in scored:
                vec_results[e["id"]] = {
                    "id": e["id"], "name": e["name"], "type": e["entity_type"],
                    "mentions": e["mention_count"],
                    "vec_score": scored[e["id"]],
                }

        merged = {}
        all_ids = set(fts_results.keys()) | set(vec_results.keys())
        for eid in all_ids:
            fts = fts_results.get(eid, {})
            vec = vec_results.get(eid, {})
            info = fts or vec
            score = 0.3 * fts.get("fts_score", 0) + 0.7 * vec.get("vec_score", 0)
            merged[eid] = {**info, "score": score}
            merged[eid].pop("fts_score", None)
            merged[eid].pop("vec_score", None)

        ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
        return self._mmr_rerank(ranked, limit)

    async def get_entity_neighborhood(self, entity_name: str, depth: int = 1) -> dict:
        """Get all relations for an entity and its immediate neighbors."""
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT id, name, entity_type, metadata, mention_count FROM entities WHERE name = ? COLLATE NOCASE",
                (entity_name,),
            ) as cur:
                entity = await cur.fetchone()
            if not entity:
                return {}

            async with conn.execute(
                """SELECT r.*, e_src.name as source_name, e_tgt.name as target_name
                   FROM relations r
                   JOIN entities e_src ON r.source_id = e_src.id
                   JOIN entities e_tgt ON r.target_id = e_tgt.id
                   WHERE r.source_id = ? OR r.target_id = ?
                   ORDER BY r.confidence DESC""",
                (entity["id"], entity["id"]),
            ) as cur:
                relations = await cur.fetchall()

            async with conn.execute(
                "SELECT alias FROM entity_aliases WHERE entity_id = ?",
                (entity["id"],),
            ) as cur:
                aliases = await cur.fetchall()
        finally:
            await self._release(conn)

        return {
            "entity": {
                "id": entity["id"],
                "name": entity["name"],
                "type": entity["entity_type"],
                "mentions": entity["mention_count"],
                "metadata": json.loads(entity["metadata"] or "{}"),
                "aliases": [a["alias"] for a in aliases],
            },
            "relations": [
                {
                    "source": r["source_name"],
                    "relation": r["relation_type"],
                    "target": r["target_name"],
                    "confidence": r["confidence"],
                }
                for r in relations
            ],
        }

    async def build_graph_context(self, query: str, max_chars: int = 2000) -> str:
        """Build a graph context string for LLM injection."""
        entities = await self.search_entities(query, limit=5)
        if not entities:
            return ""

        lines = ["## Knowledge Graph"]
        chars = 0
        for e in entities:
            neighborhood = await self.get_entity_neighborhood(e["name"])
            if not neighborhood:
                continue
            ent = neighborhood["entity"]
            header = f"\n### {ent['name']} ({ent['type']})"
            if ent.get("aliases"):
                header += f" aka {', '.join(ent['aliases'])}"
            lines.append(header)
            chars += len(header)

            for rel in neighborhood["relations"][:10]:
                line = f"- {rel['source']} --[{rel['relation']}]--> {rel['target']} (conf: {rel['confidence']:.1f})"
                if chars + len(line) > max_chars:
                    break
                lines.append(line)
                chars += len(line)

            if chars > max_chars:
                break

        return "\n".join(lines) if len(lines) > 1 else ""

    async def extract_and_store(
        self, text: str, llm=None, source: str | None = None
    ) -> list[dict]:
        """Extract entities and relations from text via LLM, then store them.

        ``source`` names where the text came from. ``api/server.py`` has
        always passed it and this signature has never accepted it, so
        every ambient conversation raised TypeError into a debug-level
        handler and contributed nothing to the graph.

        It is not decoration. The heuristic fallback below matches
        first-person patterns ("my name is", "i live in") and files them
        under the ``user`` entity. That is correct for something the
        operator typed and wrong for speech the room merely overheard,
        which is the same trust boundary ``store._NO_SELF_MODEL_EVENT_TYPES``
        draws for About-Me extraction. So for ambient text the heuristic
        is refused rather than allowed to attribute a stranger's home
        town to the operator; an LLM pass, which sees whose words these
        are, still runs.
        """
        if not llm or not llm.available:
            if source in _NO_FIRST_PERSON_HEURISTIC_SOURCES:
                logger.debug(
                    "heuristic extraction skipped for source=%r: first-person "
                    "patterns would be attributed to the operator", source,
                )
                return []
            return await self._heuristic_extract(text)

        # Sized against the serving model's context window rather than
        # clipped at a fixed character count. See "Extraction prompt
        # sizing" at the top of this module for the measurements.
        fitted = fit_extraction_text(text, llm)
        if len(fitted) < len(text):
            logger.debug(
                "extraction text trimmed from %d to %d chars for a "
                "%d-token window", len(text), len(fitted),
                _extraction_window_tokens(llm),
            )
        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(text=fitted)

        try:
            response = await llm.chat([{"role": "user", "content": prompt}], tools=None)
            raw_text, _ = llm.extract_response(response)
            cleaned = raw_text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            triples = json.loads(cleaned)
        except Exception as e:
            logger.warning("LLM extraction failed, using heuristic: %s", e)
            return await self._heuristic_extract(text)

        stored = []
        for t in triples:
            if not isinstance(t, dict):
                continue
            subj = t.get("subject", "").strip()
            pred = t.get("predicate", "").strip()
            obj = t.get("object", "").strip()
            if not all([subj, pred, obj]):
                continue
            rel = await self.add_relation(
                source_name=subj,
                relation_type=pred,
                target_name=obj,
                evidence=text[:500],
                source_type=t.get("subject_type", "thing"),
                target_type=t.get("object_type", "thing"),
            )
            stored.append(rel)
        return stored

    async def _heuristic_extract(self, text: str) -> list[dict]:
        """Pattern-based extraction when LLM is unavailable. Async-native.

        Writes through :meth:`add_relation` rather than INSERTing directly.
        The direct INSERT used a fresh ``uuid4()`` and had no existence
        check, while ``add_relation`` dedups on
        ``(source_id, relation_type, target_id)``, derives a stable id from
        the triple so two brains converge, and blends confidence on a
        repeat. Measured before this change: three extractions of the same
        sentence produced three rows per predicate. Entities deduped fine
        (this loop did check for an existing name), relations did not, and
        this path runs whenever the LLM is unavailable OR its JSON fails to
        parse, so a degraded local model grew the relations table linearly.
        """
        import re
        patterns = [
            (r"(?:my name is|i am|i'm)\s+(\w+)", "user", "is_named", "person"),
            (r"i (?:live|reside) (?:in|at)\s+(.+?)(?:\.|,|$)", "user", "lives_in", "place"),
            (r"i (?:work|am employed) (?:at|for)\s+(.+?)(?:\.|,|$)", "user", "works_at", "organization"),
            (r"i (?:like|love|enjoy)\s+(.+?)(?:\.|,|$)", "user", "likes", "thing"),
            (r"(?:my (?:wife|husband|partner) is|i'm married to)\s+(\w+)", "user", "partner_is", "person"),
            (r"i (?:study|studied) (?:at|in)\s+(.+?)(?:\.|,|$)", "user", "studied_at", "organization"),
        ]
        results = []
        for pattern, subject, predicate, obj_type in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            obj = match.group(1).strip()
            if not obj or len(obj) < 2:
                continue
            try:
                rel = await self.add_relation(
                    source_name=subject,
                    relation_type=predicate,
                    target_name=obj,
                    confidence=0.8,
                    evidence=text[:200],
                    source_type="person",
                    target_type=obj_type,
                    source_origin="heuristic",
                )
            except Exception as exc:
                # One unstorable triple must not lose the others. Logged,
                # never swallowed: a heuristic extraction that silently
                # stored nothing is indistinguishable from text with no
                # facts in it.
                logger.warning(
                    "heuristic extraction could not store (%s)-[%s]->(%s): %s",
                    subject, predicate, obj, exc,
                )
                continue
            results.append(rel)
        return results

    def stats(self) -> dict:
        """Synchronous stats query — kept sync because MemoryStore.__init__
        and a few legacy callers expect a non-coroutine return. Uses
        stdlib sqlite3 on a short-lived connection so it doesn't block
        any running event loop materially (one-shot call, microseconds).
        For async callers, prefer :meth:`stats_async`."""
        conn = sqlite3.connect(self.db_path)
        try:
            entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            relation_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            alias_count = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
        finally:
            conn.close()
        return {
            "entities": entity_count,
            "relations": relation_count,
            "aliases": alias_count,
        }

    async def stats_async(self) -> dict:
        """Async variant of :meth:`stats` for async callers."""
        conn = await self._conn()
        try:
            async with conn.execute("SELECT COUNT(*) FROM entities") as cur:
                entity_count = (await cur.fetchone())[0]
            async with conn.execute("SELECT COUNT(*) FROM relations") as cur:
                relation_count = (await cur.fetchone())[0]
            async with conn.execute("SELECT COUNT(*) FROM entity_aliases") as cur:
                alias_count = (await cur.fetchone())[0]
        finally:
            await self._release(conn)
        return {
            "entities": entity_count,
            "relations": relation_count,
            "aliases": alias_count,
        }

    async def _find_entity_by_name(self, name: str) -> Optional[dict]:
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT id, name, entity_type FROM entities WHERE name = ? COLLATE NOCASE",
                (name,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                async with conn.execute(
                    "SELECT entity_id FROM entity_aliases WHERE alias = ? COLLATE NOCASE",
                    (name,),
                ) as cur:
                    alias_row = await cur.fetchone()
                if alias_row:
                    async with conn.execute(
                        "SELECT id, name, entity_type FROM entities WHERE id = ?",
                        (alias_row["entity_id"],),
                    ) as cur:
                        row = await cur.fetchone()
        finally:
            await self._release(conn)
        if row:
            return {"id": row["id"], "name": row["name"], "entity_type": row["entity_type"]}
        return None

    async def _link_entity(self, name: str) -> Optional[dict]:
        """Find an existing entity with similar embedding (entity linking).

        Lane 05 : prefer the indexed vec0 nearest-neighbour search
        when sqlite-vec is loaded — only the top-K candidates are
        re-scored. Falls back to the legacy full-table numpy scan
        when the index is unavailable so behaviour is identical on
        hosts without the extension.
        """
        if not self._embedder.available:
            return None
        name_vec = await self._embedder.embed(name)

        # Indexed path: ask vec0 for the nearest few candidates and
        # only score those.
        indexed = await self._vec_search_candidates(name_vec, limit=5)
        if indexed:
            best_id, best_sim = max(indexed.items(), key=lambda kv: kv[1])
            if best_sim >= ENTITY_MERGE_THRESHOLD:
                conn = await self._conn()
                try:
                    async with conn.execute(
                        "SELECT id, name, entity_type FROM entities WHERE id = ?",
                        (best_id,),
                    ) as cur:
                        row = await cur.fetchone()
                finally:
                    await self._release(conn)
                if row:
                    logger.info(
                        "Entity linked (indexed): %r -> %r (sim=%.3f)",
                        name, row["name"], best_sim,
                    )
                    return {
                        "id": row["id"],
                        "name": row["name"],
                        "entity_type": row["entity_type"],
                    }
            return None

        # Fallback: full-table scan.
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT id, name, entity_type, embedding FROM entities WHERE embedding IS NOT NULL"
            ) as cur:
                all_entities = await cur.fetchall()
        finally:
            await self._release(conn)

        # One blocked matmul instead of a Python loop of blob_to_vec +
        # cosine_similarity. See cosine_similarity_bulk for the measured
        # numbers (286ms -> 23ms at 100k rows). argmax returns the FIRST
        # maximum, which is the row the strictly-greater-than loop kept.
        best_match = None
        best_sim = 0.0
        try:
            sims = cosine_similarity_bulk(
                name_vec, [e["embedding"] for e in all_entities]
            )
        except EmbeddingDimensionMismatch as exc:
            # WRITE path, not a read path: this runs inside add_entity, so
            # propagating would make every KG write raise, and knowledge
            # ingest (knowledge_store -> add_entity) would stop entirely
            # while the store waited for a migration. Fuzzy linking is an
            # optimisation on top of the exact name / alias lookup that
            # add_entity already did, so losing it costs near-duplicate
            # entities ("Feral" vs "FERAL AI"), not correctness. Recorded
            # loudly for the same reason as the read path.
            self._record_vector_leg_error(exc)
            logger.log(
                logging.ERROR if self._should_log_leg_error(exc) else logging.DEBUG,
                "Entity linking degraded to exact-name matching: %s. "
                "New entities may duplicate existing ones until "
                "`feral memory reembed` is run.",
                exc,
            )
            return None
        if sims.size:
            top = int(np.argmax(sims))
            if float(sims[top]) > best_sim:
                best_sim = float(sims[top])
                best_match = all_entities[top]

        if best_match and best_sim >= ENTITY_MERGE_THRESHOLD:
            logger.info(
                "Entity linked: %r -> %r (sim=%.3f)",
                name, best_match["name"], best_sim,
            )
            return {"id": best_match["id"], "name": best_match["name"], "entity_type": best_match["entity_type"]}
        return None

    async def find_entities_by_tag(
        self,
        *,
        category: str = "",
        entity_type: str = "",
        limit: int = 50,
    ) -> list[dict]:
        """Lookup entities by metadata category and/or entity_type.

        Lane 05  (THESIS_SCENARIOS S3): the orchestrator needs to
        answer "what BLE devices are around my phone right now?"
        without doing a full-text search over the KG. Devices land
        in the entities table with ``metadata.category == 'device'``
        (the iOS BLE ingest in Wave 3 Lane 11 writes this), so a
        targeted JSON1 lookup over ``entities.metadata`` plus the
        existing ``entity_type`` index is the right primitive.

        Both filters are optional but at least one must be provided.

        Returns ``[{id, name, entity_type, metadata, mention_count,
        last_seen_at}]`` ordered by most-recently-mentioned first.
        """
        if not category and not entity_type:
            raise ValueError(
                "find_entities_by_tag requires at least one of category= or entity_type="
            )

        clauses: list[str] = []
        params: list = []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if category:
            # SQLite's JSON1 ``json_extract`` is enabled by default
            # in modern sqlite3 builds; if the build was compiled
            # without it the query raises and we fall back to a LIKE
            # match on the raw metadata JSON text. The fallback is
            # noisier (matches "category" appearing anywhere) but
            # keeps the call functional on stripped-down builds.
            clauses.append(
                "(json_extract(metadata, '$.category') = ?"
                " OR metadata LIKE ?)"
            )
            params.append(category)
            params.append(f'%"category": "{category}"%')

        where_sql = " AND ".join(clauses) if clauses else "1=1"
        sql = (
            "SELECT id, name, entity_type, metadata, mention_count, updated_at "
            f"FROM entities WHERE {where_sql} "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(int(limit))

        conn = await self._conn()
        try:
            try:
                async with conn.execute(sql, params) as cur:
                    rows = await cur.fetchall()
            except aiosqlite.OperationalError as exc:
                # JSON1 missing — fall through to a LIKE-only query
                # (already covered by the second clause above; this
                # path catches the case where SQLite errored before
                # OR-evaluation).
                logger.debug(
                    "find_entities_by_tag JSON1 path failed (%s) — "
                    "retrying with LIKE-only filter", exc,
                )
                fallback_sql = (
                    "SELECT id, name, entity_type, metadata, mention_count, updated_at "
                    "FROM entities WHERE metadata LIKE ? "
                    "ORDER BY updated_at DESC LIMIT ?"
                )
                async with conn.execute(
                    fallback_sql,
                    (f'%"category": "{category}"%', int(limit)),
                ) as cur:
                    rows = await cur.fetchall()
        finally:
            await self._release(conn)

        return [
            {
                "id": r["id"],
                "name": r["name"],
                "entity_type": r["entity_type"],
                "metadata": json.loads(r["metadata"] or "{}"),
                "mention_count": r["mention_count"],
                "last_seen_at": r["updated_at"],
            }
            for r in rows
        ]

    async def _bump_mention(self, entity_id: str):
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE entities SET mention_count = mention_count + 1, updated_at = ? WHERE id = ?",
                (time.time(), entity_id),
            )
            await conn.commit()
        finally:
            await self._release(conn)

    async def _add_alias(self, entity_id: str, alias: str):
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT id FROM entity_aliases WHERE entity_id = ? AND alias = ? COLLATE NOCASE",
                (entity_id, alias),
            ) as cur:
                existing = await cur.fetchone()
            if not existing:
                await conn.execute(
                    "INSERT INTO entity_aliases (id, entity_id, alias, created_at) VALUES (?, ?, ?, ?)",
                    (str(uuid4())[:12], entity_id, alias, time.time()),
                )
                await conn.commit()
        finally:
            await self._release(conn)

    @staticmethod
    def _mmr_rerank(results: list[dict], limit: int, diversity: float = 0.3) -> list[dict]:
        """Maximal Marginal Relevance reranking for diversity."""
        if len(results) <= limit:
            return results
        selected = [results[0]]
        candidates = results[1:]
        while len(selected) < limit and candidates:
            best_idx = 0
            best_mmr = -1.0
            for i, cand in enumerate(candidates):
                relevance = cand.get("score", 0)
                max_sim = max(
                    (1.0 if c["name"].lower() == cand["name"].lower() else 0.0)
                    for c in selected
                )
                mmr = (1.0 - diversity) * relevance - diversity * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = i
            selected.append(candidates.pop(best_idx))
        return selected
