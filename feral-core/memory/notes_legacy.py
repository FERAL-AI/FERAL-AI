"""Legacy notes API. Async-native since v2026.5.33 (Option C refactor).

These functions are dispatched to from :class:`memory.store.MemoryStore`'s
back-compat methods (``save``, ``search``, ``list_recent``, ``delete``,
``count``). They take the store as their first argument so they can
share its db_path, embed queue, and sync engine without inheriting from
it.
"""

from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

import aiosqlite

from memory.embeddings import blob_to_vec, chunk_text, cosine_similarity
from memory.fts_query import fts5_match_query

logger = logging.getLogger("feral.memory.notes_legacy")


# Hybrid weighting mirrors :meth:`MemoryStore.episode_search_hybrid`: the
# FTS5 leg surfaces lexical hits, the vector leg surfaces semantic hits,
# and we trust the vector signal more (0.7) than the lexical one (0.3)
# when both are available. Values are intentionally duplicated rather
# than imported from store.py to avoid an import cycle (store imports
# this module).
_NOTES_TEXT_WEIGHT = 0.3
_NOTES_VECTOR_WEIGHT = 0.7

# Below this cosine similarity a vector hit is noise — the same gate
# episode_search_hybrid uses.
_NOTES_VEC_MIN_SIM = 0.25


async def save_note(
    store,
    content: str,
    tags: list[str] | None = None,
    importance: str = "normal",
    source: str = "user",
) -> dict:
    note_id = str(uuid4())[:8]
    now = time.time()
    tags = tags or []
    # Sync log first so the HLC string can land in the same INSERT —
    # required by D12 LWW on the receiving side.
    hlc = store._log_sync(
        "notes",
        "insert",
        note_id,
        {
            "id": note_id,
            "content": content,
            "tags": json.dumps(tags),
            "importance": importance,
            "source": source,
            "created_at": now,
        },
    )
    conn = await aiosqlite.connect(store.db_path)
    try:
        await conn.execute(
            "INSERT INTO notes (id, content, tags, importance, source, created_at, updated_at, hlc_string) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, content, json.dumps(tags), importance, source, now, now, hlc),
        )
        await conn.commit()
    finally:
        await conn.close()

    chunks = chunk_text(content)
    for i, chunk in enumerate(chunks):
        store._embed_queue.enqueue(
            chunk_id=f"note_{note_id}_c{i}",
            text=chunk,
            source_table="notes",
            source_id=note_id,
            chunk_index=i,
            db_path=store.db_path,
        )

    await store.knowledge_store(
        subject=f"note_{note_id}",
        predicate="says",
        obj=content[:300],
        source=f"notes:{note_id}",
    )
    return {
        "id": note_id,
        "content": content,
        "tags": tags,
        "importance": importance,
        "created_at": now,
        "status": "saved",
    }


def _embedder_has_semantic_signal(store) -> bool:
    """True when the store's embedder produces real semantic vectors.

    The hash fallback returns deterministic vectors per text but those
    vectors carry no semantic similarity signal — cosine similarity
    between two different inputs degenerates to noise. Whenever the
    primary provider is degraded the queue routes through that hash
    fallback too. In either case the hybrid path MUST skip the vector
    leg and fall back to FTS-only behaviour so we don't regress the
    pre-hybrid contract.
    """
    embedder = getattr(store, "_embedder", None)
    if embedder is None:
        return False
    if getattr(embedder, "degraded", False):
        return False
    provider = getattr(embedder, "provider_name", "none")
    return provider not in {"hash", "none"}


async def _fetch_note_rows_by_ids(conn, note_ids: list[str]) -> dict[str, dict]:
    """Hydrate {note_id: row_dict} for a set of ids returned by the
    vector leg but absent from the FTS hit set. Used to attach content
    + metadata to vector-only matches before the merge.
    """
    if not note_ids:
        return {}
    placeholders = ",".join("?" for _ in note_ids)
    async with conn.execute(
        f"SELECT id, content, tags, importance, created_at "
        f"FROM notes WHERE id IN ({placeholders})",
        note_ids,
    ) as cur:
        rows = await cur.fetchall()
    return {
        r["id"]: {
            "id": r["id"],
            "content": r["content"],
            "tags": json.loads(r["tags"]),
            "importance": r["importance"],
            "created_at": r["created_at"],
        }
        for r in rows
    }


async def _vec_results_for_notes(
    store, conn, query_vec, *, candidate_factor: int, limit: int
) -> dict[str, dict]:
    """Best-effort vector leg over the note chunks already persisted in
    the vector index / ``memory_chunks`` table.

    Returns ``{note_id: {"id": ..., "vec_score": cosine_similarity}}``.
    Empty dict on any failure or when no chunks pass the similarity
    floor — callers must treat the empty case as "skip vector blending"
    so FTS-only behaviour is preserved.
    """
    vec_results: dict[str, dict] = {}
    try:
        if store._vec_index.indexed:
            # Indexed nearest-neighbour: pull a wider slice than the
            # caller wants because the index is shared across every
            # source_table (notes, episodes, …) and we need to filter
            # to ``source_table='notes'`` after the fact. The
            # candidate_factor multiplier mirrors the episode hybrid
            # path's ``limit * 3`` and gives the post-filter merge
            # enough headroom to still return ``limit`` notes when the
            # head of the global index is dominated by episodes.
            hits = await store._vec_index.search_cosine(
                query_vec, limit=max(1, limit * candidate_factor)
            )
            if not hits:
                return vec_results
            sim_by_cid = {cid: float(sim) for cid, sim in hits}
            placeholders = ",".join("?" for _ in sim_by_cid)
            async with conn.execute(
                f"SELECT id, source_id FROM memory_chunks "
                f"WHERE source_table = 'notes' AND id IN ({placeholders})",
                list(sim_by_cid.keys()),
            ) as cur:
                rows = await cur.fetchall()
            for r in rows:
                sim = sim_by_cid.get(r["id"], 0.0)
                if sim < _NOTES_VEC_MIN_SIM:
                    continue
                nid = r["source_id"]
                if nid not in vec_results or sim > vec_results[nid]["vec_score"]:
                    vec_results[nid] = {"id": nid, "vec_score": sim}
        else:
            # Numpy fallback: full-table scan over note chunks. Same
            # shape as the episode_search_hybrid fallback path —
            # filter to ``source_table='notes'`` and skip rows whose
            # embedding wasn't persisted (provider was degraded /
            # skipped).
            async with conn.execute(
                "SELECT source_id, embedding FROM memory_chunks "
                "WHERE source_table = 'notes' AND embedding IS NOT NULL"
            ) as cur:
                chunks = await cur.fetchall()
            for c in chunks:
                evec = blob_to_vec(c["embedding"])
                sim = cosine_similarity(query_vec, evec)
                if sim < _NOTES_VEC_MIN_SIM:
                    continue
                nid = c["source_id"]
                if nid not in vec_results or sim > vec_results[nid]["vec_score"]:
                    vec_results[nid] = {"id": nid, "vec_score": float(sim)}
    except Exception as exc:
        logger.debug("notes vector leg failed: %s", exc)
        return {}
    return vec_results


async def search_notes(store, query: str, limit: int = 10) -> list[dict]:
    """Hybrid FTS5 + vector cosine search over notes.

    Mirrors :meth:`MemoryStore.episode_search_hybrid`:

    * FTS5 leg (weight 0.3) surfaces lexical hits via ``notes_fts``.
    * Vector leg (weight 0.7) surfaces semantic hits via the shared
      vector index (sqlite-vec or any registered ``VectorIndexBackend``)
      filtered to chunks whose ``memory_chunks.source_table = 'notes'``.
    * When sqlite-vec isn't loaded, the vector leg falls back to a
      numpy cosine scan over note chunks in ``memory_chunks`` — same
      shape the episode hybrid path uses.

    Graceful degradation (return shape unchanged in every case):

    * If the embedder has no real semantic signal (``hash`` / ``none``
      provider, or runtime-degraded), the vector leg is skipped and
      the function behaves like the legacy FTS-only ``search_notes``.
    * If the query embedding fails or the vector index has no note
      chunks, the vector leg contributes nothing and the FTS scores
      become the final ranking.
    * If both legs return empty (FTS5 raised because the table is
      brand new and has no triggers fired yet, or the query has no
      tokens), the function falls back to a ``LIKE`` substring scan
      so existing tests + dashboards still get a non-empty answer for
      simple inputs.

    Return shape (per row): ``id``, ``content``, ``tags`` (parsed
    list), ``importance``, ``created_at``, ``relevance_score``.
    Backward-compatible with the pre-hybrid signature.
    """
    conn = await store._conn()
    try:
        # ── FTS leg ─────────────────────────────────────────────────
        fts_results: dict[str, dict] = {}
        try:
            # Quoted, not raw: an unquoted "don't" / "C++" / "AI/ML"
            # raises an FTS5 syntax error, which dropped this leg
            # entirely and silently demoted the search to the LIKE
            # fallback further down.
            match_expr = fts5_match_query(query)
            if not match_expr:
                raise ValueError("no indexable term in query")
            async with conn.execute(
                """SELECT n.id, n.content, n.tags, n.importance, n.created_at, rank
                   FROM notes_fts f JOIN notes n ON f.rowid = n.rowid
                   WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?""",
                (match_expr, limit * 3),
            ) as cur:
                rows = await cur.fetchall()
            for r in rows:
                fts_results[r["id"]] = {
                    "id": r["id"],
                    "content": r["content"],
                    "tags": json.loads(r["tags"]),
                    "importance": r["importance"],
                    "created_at": r["created_at"],
                    "fts_score": 1.0 / (1.0 + abs(r["rank"])),
                }
        except Exception as exc:
            logger.debug("notes FTS leg failed: %s", exc)

        # ── Vector leg ─────────────────────────────────────────────
        vec_results: dict[str, dict] = {}
        if _embedder_has_semantic_signal(store):
            try:
                query_vec = await store._embedder.embed(query)
            except Exception as exc:
                logger.debug("notes query embedding failed: %s", exc)
                query_vec = None
            if query_vec is not None:
                vec_results = await _vec_results_for_notes(
                    store, conn, query_vec, candidate_factor=5, limit=limit,
                )

        # ── LIKE fallback when both legs returned nothing ───────────
        # Pre-hybrid behaviour returned a ``LIKE`` match on FTS5
        # exception; we keep that for the brand-new-DB / no-trigger
        # case to avoid regressing low-tech callers (the old
        # behaviour is what tests like ``test_taskflow`` rely on).
        if not fts_results and not vec_results:
            try:
                async with conn.execute(
                    "SELECT id, content, tags, importance, created_at "
                    "FROM notes WHERE content LIKE ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (f"%{query}%", limit),
                ) as cur:
                    rows = await cur.fetchall()
                for r in rows:
                    fts_results[r["id"]] = {
                        "id": r["id"],
                        "content": r["content"],
                        "tags": json.loads(r["tags"]),
                        "importance": r["importance"],
                        "created_at": r["created_at"],
                        "fts_score": 0.5,
                    }
            except Exception as exc:
                logger.debug("notes LIKE fallback failed: %s", exc)

        # Hydrate vector-only hits (notes the FTS leg missed) so we
        # can return their content/metadata. ``fts_results`` already
        # carries the row payload for FTS hits; only the vector-only
        # ids need a round-trip.
        all_ids = set(fts_results.keys()) | set(vec_results.keys())
        missing = [nid for nid in vec_results.keys() if nid not in fts_results]
        note_cache = await _fetch_note_rows_by_ids(conn, missing)
    finally:
        await store._release(conn)

    # ── Merge + score ───────────────────────────────────────────────
    merged: list[dict] = []
    have_vector_signal = bool(vec_results)
    for nid in all_ids:
        info = fts_results.get(nid) or note_cache.get(nid)
        if not info:
            continue
        fts_score = fts_results.get(nid, {}).get("fts_score", 0.0)
        vec_score = vec_results.get(nid, {}).get("vec_score", 0.0)
        if have_vector_signal:
            base_score = (
                _NOTES_TEXT_WEIGHT * fts_score
                + _NOTES_VECTOR_WEIGHT * vec_score
            )
        else:
            # Pre-hybrid contract: just the FTS / LIKE relevance.
            # Reusing ``abs`` matches the original ``abs(rank)`` cast
            # so callers comparing this to the legacy values see the
            # same magnitudes.
            base_score = abs(fts_score)
        merged.append(
            {
                "id": info["id"],
                "content": info["content"],
                "tags": info["tags"],
                "importance": info["importance"],
                "created_at": info["created_at"],
                "relevance_score": base_score,
            }
        )

    merged.sort(key=lambda x: x["relevance_score"], reverse=True)
    return merged[:limit]


async def list_recent_notes(store, limit: int = 10) -> list[dict]:
    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT id, content, tags, importance, created_at FROM notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    finally:
        # ``_release``, never ``close``: this connection belongs to the
        # pool. Closing it here destroyed one of the four pooled
        # connections per call, and since ``list_recent`` is on an
        # ordinary read path the brain deadlocked after four listings.
        await store._release(conn)
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "tags": json.loads(row["tags"]),
            "importance": row["importance"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def delete_note(store, note_id: str) -> bool:
    conn = await aiosqlite.connect(store.db_path)
    try:
        cursor = await conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        await conn.commit()
        deleted = cursor.rowcount > 0
    finally:
        await conn.close()
    return deleted


async def count_notes(store) -> int:
    conn = await aiosqlite.connect(store.db_path)
    try:
        async with conn.execute("SELECT COUNT(*) FROM notes") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0
    finally:
        await conn.close()
