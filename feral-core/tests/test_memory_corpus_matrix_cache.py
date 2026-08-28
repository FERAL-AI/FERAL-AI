"""Pins for the cached corpus matrix on the numpy vector-search path.

Why this exists
---------------
On any interpreter that cannot load the sqlite-vec extension, every
``episode_search_hybrid`` call took the full-scan branch, and that branch
used to rebuild its entire float32 matrix from SQLite BLOBs on EVERY
query: one ``SELECT`` of all embeddings, one ``b"".join``, one
``np.frombuffer``, one unit-normalisation and one centring pass.

Measured on a copy of the reporter's real store (11,613 episode chunks,
384 dims) before the cache:

    SELECT all embeddings      23.3 ms
    _centered_similarity        9.1 ms
    ------------------------------------
    vector leg                 32.4 ms   per query, every query

None of that work depends on the query. The only query-dependent step is
the final mat-vec, which measures 0.35 ms on the same data.

These tests assert the *number of assemblies*, not latency. A latency
assertion is flaky on a shared machine; the call count is exact.
"""
from __future__ import annotations

import time

import aiosqlite
import numpy as np
import pytest

from memory.embeddings import vec_to_blob
from memory.store import _MIN_CHUNKS_FOR_CENTERING, MemoryStore

pytestmark = pytest.mark.asyncio

DIM = 16
N_CHUNKS = _MIN_CHUNKS_FOR_CENTERING + 60


class _NoOpVecIndex:
    """``indexed`` False, so the store takes the numpy full-scan branch."""

    backend_id = "noop"
    indexed = False

    async def search_similarity(self, query_vec, limit=20):
        return []

    def upsert(self, chunk_id, embedding):
        pass

    def count(self):
        return 0


class _StubEmbedder:
    """Deterministic vectors with a real angular spread, so the centred
    scores are not all identical and a ranking actually exists."""

    provider_name = "stub_local"
    degraded = False
    dimension = DIM

    def _vec_for(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.normal(size=DIM).astype(np.float32)
        # Push everything into a narrow cone, which is the anisotropy that
        # makes centring necessary in the first place.
        v[0] += 3.0
        return v / np.linalg.norm(v)

    async def embed(self, text: str) -> np.ndarray:
        return self._vec_for(text)

    def embed_sync(self, text: str):
        return self._vec_for(text)


async def _seed(db_path: str, n: int, *, start: int = 0) -> None:
    emb = _StubEmbedder()
    now = time.time()
    conn = await aiosqlite.connect(db_path)
    try:
        for i in range(start, start + n):
            eid = f"ep-{i}"
            await conn.execute(
                "INSERT OR REPLACE INTO episodes "
                "(id, session_id, event_type, summary, detail, importance, created_at) "
                "VALUES (?, 's', 'note', ?, '', 0.5, ?)",
                (eid, f"episode number {i} about topic {i % 7}", now),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO memory_chunks "
                "(id, source_table, source_id, chunk_index, text_content, embedding, created_at) "
                "VALUES (?, 'episodes', ?, 0, ?, ?, ?)",
                (
                    f"{eid}_c0",
                    eid,
                    f"episode number {i} about topic {i % 7}",
                    vec_to_blob(emb._vec_for(f"body {i} topic {i % 7}")),
                    now,
                ),
            )
        await conn.commit()
    finally:
        await conn.close()


@pytest.fixture
async def store(tmp_path):
    db = str(tmp_path / "corpus_cache.db")
    s = MemoryStore(db_path=db, vec_index=_NoOpVecIndex())
    s._embedder = _StubEmbedder()
    await _seed(db, N_CHUNKS)
    yield s
    s.close()


class _AssemblyCounter:
    """Counts every rebuild of the corpus matrix from stored BLOBs.

    ``np.frombuffer`` inside ``memory.store`` is only ever handed the
    joined embedding buffer, so one call is one full assembly. Counting
    it works identically before and after the fix, which is what makes
    this test able to fail against the unfixed source for the right
    reason rather than with an ``AttributeError``.
    """

    def __init__(self, monkeypatch):
        self.n = 0
        real = np.frombuffer

        def counting(buf, *a, **kw):
            self.n += 1
            return real(buf, *a, **kw)

        monkeypatch.setattr("memory.store.np.frombuffer", counting)


async def test_corpus_matrix_is_assembled_once_across_repeated_queries(
    store, monkeypatch
):
    """Three queries against an unchanged corpus must assemble the
    matrix once, not three times. This is the whole fix."""
    counter = _AssemblyCounter(monkeypatch)

    for q in ("topic 3", "topic 5", "something else entirely"):
        await store.episode_search_hybrid(q, limit=5)

    assert counter.n == 1, (
        f"corpus matrix rebuilt {counter.n} times for 3 queries; "
        "it does not depend on the query and must be assembled once"
    )


async def test_cached_matrix_is_the_same_object_on_the_second_query(store):
    """Cache identity, not just call count: the second query must score
    against the very same array object, so nothing is being copied."""
    await store.episode_search_hybrid("topic 1", limit=5)
    first = getattr(store, "_corpus_cache", None)
    assert first is not None, "nothing was cached after the first query"

    await store.episode_search_hybrid("topic 2", limit=5)
    assert store._corpus_cache is first
    assert store._corpus_cache.docs is first.docs


async def test_cache_is_invalidated_when_the_corpus_grows(store, monkeypatch):
    """A new chunk must force a rebuild. A cache that never invalidates
    would silently stop returning new memories, which is worse than the
    latency it saves."""
    counter = _AssemblyCounter(monkeypatch)
    await store.episode_search_hybrid("topic 3", limit=5)
    assert counter.n == 1

    await _seed(store.db_path, 5, start=10_000)

    await store.episode_search_hybrid("topic 3", limit=5)
    assert counter.n == 2, "corpus grew but the cached matrix was reused"


async def test_new_episode_is_findable_immediately_after_being_added(store):
    """End-to-end statement of the same invariant, in user terms."""
    await store.episode_search_hybrid("topic 3", limit=5)

    emb = _StubEmbedder()
    conn = await aiosqlite.connect(store.db_path)
    try:
        await conn.execute(
            "INSERT INTO episodes (id, session_id, event_type, summary, detail, "
            "importance, created_at) VALUES ('ep-new', 's', 'note', "
            "'zebra quasar mnemonic', '', 0.9, ?)",
            (time.time(),),
        )
        await conn.execute(
            "INSERT INTO memory_chunks (id, source_table, source_id, chunk_index, "
            "text_content, embedding, created_at) VALUES "
            "('ep-new_c0', 'episodes', 'ep-new', 0, 'zebra quasar mnemonic', ?, ?)",
            (vec_to_blob(emb._vec_for("zebra quasar mnemonic")), time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()

    results = await store.episode_search_hybrid("zebra quasar mnemonic", limit=5)
    assert "ep-new" in [r["id"] for r in results]


async def test_cached_queries_return_what_a_cold_store_returns(store, tmp_path):
    """Results must not move. A warm store and a store that has never
    queried before must produce the same rows in the same order."""
    queries = ["topic 3", "topic 6", "episode number 42", "asdfgh zxcvbn"]

    warm = []
    for q in queries:
        await store.episode_search_hybrid(q, limit=8)  # warm the cache
    for q in queries:
        warm.append(await store.episode_search_hybrid(q, limit=8))

    cold_results = []
    for q in queries:
        cold = MemoryStore(db_path=store.db_path, vec_index=_NoOpVecIndex())
        cold._embedder = _StubEmbedder()
        cold_results.append(await cold.episode_search_hybrid(q, limit=8))
        cold.close()

    for q, w, c in zip(queries, warm, cold_results):
        assert [r["id"] for r in w] == [r["id"] for r in c], f"ranking moved for {q!r}"
        # 6 decimals, not exact: relevance_score carries a recency prior
        # computed from ``time.time()``, so the warm and cold runs differ in
        # the 10th decimal purely because they ran a few milliseconds apart.
        # The vector leg's own bit-exactness is proved separately, on a copy
        # of the real store, and recorded in AUDIT-FIXES.md.
        assert [
            round(r["relevance_score"], 6) for r in w
        ] == [round(r["relevance_score"], 6) for r in c], f"scores moved for {q!r}"
