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

from memory.embeddings import (
    EmbeddingProvider,
    vec_to_blob,
    blob_to_vec,
    cosine_similarity,
)

logger = logging.getLogger("feral.memory.kg")

ENTITY_MERGE_THRESHOLD = 0.85
ENTITY_CANDIDATE_THRESHOLD = 0.70


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
        # Lane 05 W4 (AUDIT-r14 finding 14, S3): is the sqlite-vec
        # entity index available? Set by :meth:`_init_schema` based
        # on extension load + CREATE VIRTUAL TABLE outcome.
        self._vec_entities_available: bool = False
        self._init_schema()

    async def _conn(self) -> aiosqlite.Connection:
        """Acquire an aiosqlite connection.

        Lane 05 W4 (AUDIT-r14 finding 14): when a ``MemoryStore`` is
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

            # Lane 05 W4 (AUDIT-r14 finding 14, S3): create a vec0
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
        # the event loop on the sync sqlite3 fsync (Lane 05 W3
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
    ) -> dict:
        """Add a relation between two entities (creating them if needed)."""
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
                    (rid, source["id"], relation_type, target["id"], confidence, evidence[:1000], "conversation", hlc, now, now),
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

    async def _vec_search_candidates(
        self, query_vec, limit: int
    ) -> dict[str, float]:
        """Use the vec0 entity index (when available) to fetch the top-K
        nearest entities. Returns ``{entity_id: cosine_similarity}``.

        Lane 05 W4 fix for AUDIT-r14 finding 14: replaces the previous
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

    async def search_entities(self, query: str, limit: int = 10) -> list[dict]:
        """Hybrid FTS + embedding search for entities.

        Lane 05 W4 (AUDIT-r14 finding 14): the embedding leg now
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
            try:
                async with conn.execute(
                    """SELECT e.id, e.name, e.entity_type, e.mention_count, rank
                       FROM entities_fts f JOIN entities e ON f.rowid = e.rowid
                       WHERE entities_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (query, limit * 2),
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    fts_results[r["id"]] = {
                        "id": r["id"], "name": r["name"], "type": r["entity_type"],
                        "mentions": r["mention_count"],
                        "fts_score": 1.0 / (1.0 + abs(r["rank"])),
                    }
            except Exception:
                pass

            if indexed_hits:
                # Hydrate the indexed hits with entity metadata.
                hit_ids = list(indexed_hits.keys())
                placeholders = ",".join("?" * len(hit_ids))
                async with conn.execute(
                    f"SELECT id, name, entity_type, mention_count "
                    f"FROM entities WHERE id IN ({placeholders})",
                    hit_ids,
                ) as cur:
                    indexed_rows = await cur.fetchall()
                vec_results = {
                    r["id"]: {
                        "id": r["id"],
                        "name": r["name"],
                        "type": r["entity_type"],
                        "mentions": r["mention_count"],
                        "vec_score": indexed_hits[r["id"]],
                    }
                    for r in indexed_rows
                    if indexed_hits.get(r["id"], 0.0) > 0.3
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

        vec_results = {}
        for e in all_entities:
            evec = blob_to_vec(e["embedding"])
            sim = cosine_similarity(query_vec, evec)
            if sim > 0.3:
                vec_results[e["id"]] = {
                    "id": e["id"], "name": e["name"], "type": e["entity_type"],
                    "mentions": e["mention_count"],
                    "vec_score": sim,
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

    async def extract_and_store(self, text: str, llm=None) -> list[dict]:
        """Extract entities and relations from text via LLM, then store them."""
        if not llm or not llm.available:
            return await self._heuristic_extract(text)

        prompt = (
            "Extract knowledge triples from this text. Return a JSON array of objects, "
            "each with: subject, subject_type, predicate, object, object_type.\n"
            "Types: person, place, organization, concept, thing, event, time.\n"
            "Only extract factual statements. Skip opinions and questions.\n"
            f"Text: {text[:2000]}\n"
            "Output ONLY valid JSON array. No markdown."
        )

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
        """Pattern-based extraction when LLM is unavailable. Async-native."""
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
        now = time.time()
        conn = await self._conn()
        try:
            for pattern, subject, predicate, obj_type in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    obj = match.group(1).strip()
                    if not obj or len(obj) < 2:
                        continue
                    for ename, etype in [(subject, "person"), (obj, obj_type)]:
                        async with conn.execute(
                            "SELECT id FROM entities WHERE name = ? COLLATE NOCASE", (ename,)
                        ) as cur:
                            existing = await cur.fetchone()
                        if not existing:
                            eid = str(uuid4())[:12]
                            await conn.execute(
                                "INSERT INTO entities (id, name, entity_type, metadata, created_at, updated_at) VALUES (?, ?, ?, '{}', ?, ?)",
                                (eid, ename, etype, now, now),
                            )
                    async with conn.execute("SELECT id FROM entities WHERE name = ? COLLATE NOCASE", (subject,)) as cur:
                        src = await cur.fetchone()
                    async with conn.execute("SELECT id FROM entities WHERE name = ? COLLATE NOCASE", (obj,)) as cur:
                        tgt = await cur.fetchone()
                    if src and tgt:
                        rid = str(uuid4())[:12]
                        await conn.execute(
                            "INSERT INTO relations (id, source_id, relation_type, target_id, confidence, evidence_text, source_origin, created_at, updated_at) VALUES (?, ?, ?, ?, 0.8, ?, 'heuristic', ?, ?)",
                            (rid, src["id"], predicate, tgt["id"], text[:200], now, now),
                        )
                    results.append({"source": subject, "relation": predicate, "target": obj})
            await conn.commit()
        finally:
            await self._release(conn)
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

        Lane 05 W4: prefer the indexed vec0 nearest-neighbour search
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

        best_match = None
        best_sim = 0.0
        for e in all_entities:
            evec = blob_to_vec(e["embedding"])
            sim = cosine_similarity(name_vec, evec)
            if sim > best_sim:
                best_sim = sim
                best_match = e

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

        Lane 05 W4 (THESIS_SCENARIOS S3): the orchestrator needs to
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
