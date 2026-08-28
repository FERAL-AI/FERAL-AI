"""Hybrid FTS5 + vector cosine retrieval for notes
(:func:`memory.notes_legacy.search_notes`).

Mirrors the contract proven by :meth:`MemoryStore.episode_search_hybrid`
for episodes:

* the vector leg surfaces semantically-relevant notes that the FTS leg
  would otherwise miss (different surface tokens, same meaning);
* the function degrades to the legacy FTS-only ranking when the
  embedder has no real semantic signal (hash fallback / no embedder /
  query embedding fails) and when the vector index has no note chunks;
* the public return shape (``id``, ``content``, ``tags``, ``importance``,
  ``created_at``, ``relevance_score``) is preserved 1:1, so existing
  callers (``search_all``, ``api/routes/memory.py``, ``digital_twin``,
  ``taskflow``, ``direct_execution``) keep working without touching
  their signatures.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Iterable

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiosqlite  # noqa: E402

from memory.embeddings import vec_to_blob  # noqa: E402
from memory.notes_legacy import search_notes  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


# ─────────────────────────────────────────────
# Helpers / stubs
# ─────────────────────────────────────────────


class _NoOpVecIndex:
    """Vector index that always reports ``indexed=False``.

    Forces :func:`search_notes` down its numpy-fallback branch (the
    one that scans ``memory_chunks`` directly), which lets the tests
    pin the FTS+vector merge logic with controlled embeddings without
    booting sqlite-vec or running the async embed queue.
    """

    backend_id = "noop"
    indexed = False

    def __init__(self) -> None:
        self.upserts: list[tuple[str, np.ndarray]] = []

    async def upsert(self, chunk_id: str, embedding: np.ndarray) -> None:
        self.upserts.append((chunk_id, np.asarray(embedding)))

    async def upsert_batch(self, items: Iterable[tuple[str, np.ndarray]]) -> None:
        for cid, vec in items:
            await self.upsert(cid, vec)

    async def delete(self, chunk_id: str) -> None:
        pass

    async def count(self) -> int:
        return 0

    async def search(self, query_vec: np.ndarray, limit: int = 20):
        return []

    async def search_similarity(self, query_vec: np.ndarray, limit: int = 20):
        return []

    def close(self) -> None:
        pass


class _IndexedVecIndex(_NoOpVecIndex):
    """Indexed variant: ``indexed=True`` and ``search_similarity`` returns
    a controlled list. Used to pin the indexed-path branch of
    :func:`search_notes` without needing sqlite-vec on the host.
    """

    indexed = True

    def __init__(self, hits: list[tuple[str, float]]) -> None:
        super().__init__()
        self._hits = list(hits)

    async def search_similarity(self, query_vec: np.ndarray, limit: int = 20):
        return self._hits[:limit]


class _SemanticStubEmbedder:
    """Tiny in-test embedder with a real bag-of-tokens semantic signal.

    The hash fallback in :class:`memory.embeddings.EmbeddingProvider`
    produces deterministic vectors per text but those vectors share
    no token-level structure — two different inputs generally have
    near-zero cosine similarity. We need a stub whose vectors actually
    correlate when the inputs share a topic, so the test can assert
    "vector leg surfaces a note that FTS missed".
    """

    provider_name = "stub_local"
    degraded = False
    dimension = 4

    _DIMS = ("alpha", "beta", "gamma", "delta")

    def _vec_for(self, text: str) -> np.ndarray:
        v = np.zeros(self.dimension, dtype=np.float32)
        lowered = (text or "").lower()
        for i, kw in enumerate(self._DIMS):
            if kw in lowered:
                v[i] += 1.0
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    async def embed(self, text: str) -> np.ndarray:
        return self._vec_for(text)

    def embed_sync(self, text: str):
        v = self._vec_for(text)
        if not np.any(v):
            return None
        return v


class _HashStubEmbedder:
    """Stub mimicking the hash fallback: ``provider_name='hash'`` so
    :func:`memory.notes_legacy._embedder_has_semantic_signal` returns
    ``False`` and the vector leg is skipped end-to-end."""

    provider_name = "hash"
    degraded = False
    dimension = 4

    async def embed(self, text: str) -> np.ndarray:
        # Should never be called when the gate works — return junk to
        # make accidental usage obvious.
        return np.ones(self.dimension, dtype=np.float32)

    def embed_sync(self, text: str):
        return None


async def _insert_note_chunk(
    db_path: str, *, chunk_id: str, source_id: str, vec: np.ndarray, text: str = ""
) -> None:
    """Manually populate ``memory_chunks`` so the hybrid path's vector
    leg has something to score against. The async embed queue is
    intentionally NOT started in these tests — bypassing it lets the
    test stamp deterministic vectors per chunk."""
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.execute(
            """INSERT OR REPLACE INTO memory_chunks
               (id, source_table, source_id, chunk_index, text_content, embedding, created_at)
               VALUES (?, 'notes', ?, 0, ?, ?, ?)""",
            (chunk_id, source_id, text, vec_to_blob(vec), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "notes_hybrid.db")
    s = MemoryStore(db_path=db, vec_index=_NoOpVecIndex())
    yield s


@pytest.fixture
def indexed_store(tmp_path):
    db = str(tmp_path / "notes_hybrid_indexed.db")

    def _factory(hits):
        return MemoryStore(db_path=db, vec_index=_IndexedVecIndex(hits))

    return _factory


# ─────────────────────────────────────────────
# Numpy fallback path (vec_index.indexed = False)
# ─────────────────────────────────────────────


async def test_hybrid_top_ranks_match_with_aligned_vector(store):
    """The note whose stored chunk vector aligns with the query vector
    AND whose content shares the FTS token must rank #1: it scores in
    BOTH legs of the blend."""
    store._embedder = _SemanticStubEmbedder()

    n1 = await store.save("alpha document")
    n2 = await store.save("beta document")

    # Stamp vectors so the query "alpha" embeds onto n1 and far from n2.
    # n2 stays orthogonal to the query and below the 0.25 vec floor —
    # so it falls out of the vector leg entirely. That matches the
    # episode hybrid's behaviour, which also drops sub-0.25 hits.
    await _insert_note_chunk(
        store.db_path,
        chunk_id=f"note_{n1['id']}_c0",
        source_id=n1["id"],
        vec=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
    )
    await _insert_note_chunk(
        store.db_path,
        chunk_id=f"note_{n2['id']}_c0",
        source_id=n2["id"],
        vec=np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
    )

    results = await search_notes(store, "alpha", limit=5)
    assert results, "search returned nothing"
    assert results[0]["id"] == n1["id"], (
        f"expected {n1['id']!r} top-ranked; got {results}"
    )


async def test_hybrid_pulls_in_purely_semantic_note_fts_misses(store):
    """A note that doesn't share any surface tokens with the query but
    whose vector aligns with the query vector MUST be retrieved through
    the vector leg alone."""
    store._embedder = _SemanticStubEmbedder()

    # The note's content has no 'alpha' token, but its stored embedding
    # is the alpha-direction. The query "alpha goal" has the alpha
    # token AND the alpha-direction vector — vector leg should fire.
    semantic_only = await save_with_no_fts_token(store, "this is the very topic", topic_dim=0)
    decoy = await store.save("beta only here")
    await _insert_note_chunk(
        store.db_path,
        chunk_id=f"note_{decoy['id']}_c0",
        source_id=decoy["id"],
        vec=np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
    )

    results = await search_notes(store, "alpha", limit=5)
    assert results, "search returned nothing"
    ids = [r["id"] for r in results]
    assert semantic_only["id"] in ids, (
        f"vector-only note missing from results: {results}"
    )


async def save_with_no_fts_token(store, content: str, *, topic_dim: int) -> dict:
    """Persist a note with content that has no 'alpha/beta/gamma/delta'
    token so FTS for those queries cannot hit it, then stamp its
    chunk embedding to the requested topic dimension."""
    note = await store.save(content)
    vec = np.zeros(_SemanticStubEmbedder.dimension, dtype=np.float32)
    vec[topic_dim] = 1.0
    await _insert_note_chunk(
        store.db_path,
        chunk_id=f"note_{note['id']}_c0",
        source_id=note["id"],
        vec=vec,
    )
    return note


# ─────────────────────────────────────────────
# Indexed path (vec_index.indexed = True) — chunk_id resolves through memory_chunks
# ─────────────────────────────────────────────


async def test_hybrid_indexed_path_resolves_chunk_id_via_memory_chunks(tmp_path):
    """When the vector index is indexed, ``search_similarity`` returns
    ``chunk_id`` -> similarity. The hybrid path must look those
    chunk_ids up in ``memory_chunks`` (filtered to ``source_table='notes'``)
    and resolve them to note ids before merging."""
    db = str(tmp_path / "indexed.db")
    # We'll build the store first with a no-op index just to populate
    # the schema, then swap in an indexed stub before searching.
    bootstrap = MemoryStore(db_path=db, vec_index=_NoOpVecIndex())
    bootstrap._embedder = _SemanticStubEmbedder()
    n1 = await bootstrap.save("hello world")
    n2 = await bootstrap.save("goodbye world")

    # Populate memory_chunks so the chunk_id -> note_id lookup resolves.
    await _insert_note_chunk(
        db,
        chunk_id=f"note_{n1['id']}_c0",
        source_id=n1["id"],
        vec=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
    )
    await _insert_note_chunk(
        db,
        chunk_id=f"note_{n2['id']}_c0",
        source_id=n2["id"],
        vec=np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
    )

    indexed = _IndexedVecIndex(
        hits=[
            (f"note_{n1['id']}_c0", 0.95),
            (f"note_{n2['id']}_c0", 0.30),
        ],
    )
    store = MemoryStore(db_path=db, vec_index=indexed)
    store._embedder = _SemanticStubEmbedder()

    results = await search_notes(store, "alpha", limit=5)
    ids = [r["id"] for r in results]
    assert n1["id"] in ids
    assert ids.index(n1["id"]) == 0, (
        f"vector top-ranked note must come first; got {results}"
    )


async def test_hybrid_indexed_path_filters_out_non_note_chunks(tmp_path):
    """Episode chunks (or any other source_table) MUST NOT bleed into
    the notes ranking even if the indexed search returns their
    chunk_ids in the global vec0 result set."""
    db = str(tmp_path / "filter.db")
    bootstrap = MemoryStore(db_path=db, vec_index=_NoOpVecIndex())
    bootstrap._embedder = _SemanticStubEmbedder()
    n1 = await bootstrap.save("hello world")

    await _insert_note_chunk(
        db,
        chunk_id=f"note_{n1['id']}_c0",
        source_id=n1["id"],
        vec=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
    )

    # Inject an episode chunk into memory_chunks so we can be sure the
    # source_table filter actually runs.
    conn = await aiosqlite.connect(db)
    try:
        await conn.execute(
            """INSERT INTO memory_chunks
               (id, source_table, source_id, chunk_index, text_content, embedding, created_at)
               VALUES (?, 'episodes', 'ep-1', 0, '', ?, ?)""",
            ("ep-1_c0", vec_to_blob(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()

    indexed = _IndexedVecIndex(
        hits=[
            ("ep-1_c0", 0.99),  # would-be top hit, but it's an episode
            (f"note_{n1['id']}_c0", 0.80),
        ],
    )
    store = MemoryStore(db_path=db, vec_index=indexed)
    store._embedder = _SemanticStubEmbedder()

    results = await search_notes(store, "alpha", limit=5)
    ids = [r["id"] for r in results]
    assert "ep-1" not in ids, "episode chunk leaked into notes ranking"
    assert n1["id"] in ids


# ─────────────────────────────────────────────
# Graceful degradation
# ─────────────────────────────────────────────


async def test_hybrid_degrades_to_fts_when_embedder_has_no_semantic_signal(store):
    """Hash-only / degraded embedder MUST skip the vector leg entirely.
    If we accidentally query the vector index in that case, the test's
    fake embedder would return junk vectors that could pollute the
    ranking."""
    store._embedder = _HashStubEmbedder()

    n1 = await store.save("milk and bread")
    n2 = await store.save("call mom about milk")
    n3 = await store.save("totally unrelated topic")

    # Drop a bogus chunk vector for n3 — if the gate is broken and
    # the hybrid path runs the vector leg, n3 would float to the top.
    await _insert_note_chunk(
        store.db_path,
        chunk_id=f"note_{n3['id']}_c0",
        source_id=n3["id"],
        vec=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
    )

    results = await search_notes(store, "milk", limit=5)
    ids = [r["id"] for r in results]
    # n1 and n2 share the FTS token "milk"; n3 must NOT outrank them.
    assert n1["id"] in ids
    assert n2["id"] in ids
    if n3["id"] in ids:
        assert ids.index(n3["id"]) > 1


async def test_hybrid_degrades_to_fts_when_vector_index_has_no_note_chunks(store):
    """No chunks for notes in ``memory_chunks`` (queue not started yet,
    or recently flushed) means the vector leg returns no hits — the
    function must still return FTS hits with the legacy magnitudes."""
    store._embedder = _SemanticStubEmbedder()

    n1 = await store.save("alpha alpha")
    await store.save("beta beta")

    results = await search_notes(store, "alpha", limit=5)
    ids = [r["id"] for r in results]
    assert n1["id"] == ids[0]
    # All hits must carry a positive relevance score even with the
    # vector leg dark — the FTS rank-derived score must propagate.
    assert all(r["relevance_score"] > 0 for r in results)


async def test_hybrid_degrades_when_embed_call_raises(store):
    """If the embedder is configured but ``embed()`` raises (transient
    network failure, etc.), the vector leg is silently dropped."""

    class _RaisingEmbedder(_SemanticStubEmbedder):
        async def embed(self, text):  # type: ignore[override]
            raise RuntimeError("upstream timeout")

    store._embedder = _RaisingEmbedder()

    n1 = await store.save("alpha alpha")
    results = await search_notes(store, "alpha", limit=5)
    assert results
    assert results[0]["id"] == n1["id"]


# ─────────────────────────────────────────────
# Backward-compat: shape + LIKE fallback
# ─────────────────────────────────────────────


async def test_hybrid_preserves_legacy_return_shape(store):
    """Existing callers (search_all, api/routes/memory, taskflow,
    digital_twin) destructure these exact keys. New keys are fine,
    but each must be present."""
    store._embedder = _HashStubEmbedder()
    await store.save("legacy shape", tags=["a"], importance="high")

    results = await search_notes(store, "legacy", limit=5)
    assert results
    row = results[0]
    for key in ("id", "content", "tags", "importance", "created_at", "relevance_score"):
        assert key in row, f"missing legacy key: {key}"
    assert isinstance(row["tags"], list)
    assert row["tags"] == ["a"]


async def test_hybrid_falls_through_to_like_for_brand_new_db(tmp_path):
    """Pre-hybrid behaviour: when FTS returns nothing AND the vector
    leg has nothing to say, the function still tries a substring
    LIKE so dashboards / smoke tests don't see an empty list for a
    plain query that the data has."""
    db = str(tmp_path / "fresh.db")
    s = MemoryStore(db_path=db, vec_index=_NoOpVecIndex())
    s._embedder = _HashStubEmbedder()

    # Save a single note. Don't run the embed queue — chunks are not
    # in memory_chunks, so the vector leg sees nothing. The FTS
    # trigger DID fire on insert, so a single-token query will hit;
    # use a multi-word phrase so FTS5 returns 0 (AND-of-tokens that
    # happens to fail) and force the LIKE branch.
    note = await s.save("Z9X-y substring-only-token never-FTS-matches")
    out = await search_notes(s, "Z9X-y substring-only-token", limit=5)
    assert any(r["id"] == note["id"] for r in out), (
        f"LIKE fallback did not surface the saved note: {out}"
    )


# ─────────────────────────────────────────────
# Integration with MemoryStore.search (the public surface)
# ─────────────────────────────────────────────


async def test_memory_store_search_routes_through_hybrid(store):
    """``MemoryStore.search()`` is the public entry point every caller
    uses. It delegates to ``search_notes`` — that delegation must
    return the new hybrid ranking, not a stale FTS-only path."""
    store._embedder = _SemanticStubEmbedder()
    n1 = await store.save("alpha alpha")
    n2 = await store.save("beta beta")

    await _insert_note_chunk(
        store.db_path,
        chunk_id=f"note_{n1['id']}_c0",
        source_id=n1["id"],
        vec=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
    )
    await _insert_note_chunk(
        store.db_path,
        chunk_id=f"note_{n2['id']}_c0",
        source_id=n2["id"],
        vec=np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
    )

    via_public = await store.search("alpha", limit=5)
    via_legacy = await search_notes(store, "alpha", limit=5)
    # Both surfaces must return the same ranking.
    assert [r["id"] for r in via_public] == [r["id"] for r in via_legacy]


# Guard against the pool-leak regression: search_notes used to call
# ``conn.close()`` on a pool connection, shrinking the pool by 1 every
# call. Confirm the pool is unchanged after a search.


async def test_hybrid_returns_pool_connection(store):
    store._embedder = _HashStubEmbedder()
    await store.save("hello pool")
    # Force the pool to materialise.
    c = await store._conn()
    await store._release(c)
    size_before = store._pool.qsize()
    await search_notes(store, "hello", limit=3)
    # Give async housekeeping a tick to finish.
    await asyncio.sleep(0)
    assert store._pool.qsize() == size_before, (
        "search_notes leaked / closed a pooled connection"
    )
