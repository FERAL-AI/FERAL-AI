"""What the vector-index similarity score actually is, per backend.

The default ``sqlite_vec`` backend creates its ``vec0`` table without
``distance_metric=cosine``, so sqlite-vec answers with an L2 distance
and the adapter returns ``1.0 - L2``. On unit vectors that is a
monotone decreasing function of the cosine, so RANKING is right and
MAGNITUDE is not: a true cosine of 0.84 arrives as 0.4343.

That was survivable only because nothing thresholds the raw value.
Applying a cosine floor to it silently kills recall, which already
happened once and was caught by measurement rather than by review. The
method used to be called ``search_cosine`` on every backend, which is
what made the mistake reasonable to make.

These tests pin the real semantics numerically so the next person to
add a floor discovers the scale from a failing assertion rather than
from a support ticket. They are deliberately arithmetic, not
code-shape: the point is the number that comes back, not the name of
the function that returns it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from memory.vector_index_backends.sqlite_vec import SQLiteVecIndex


def _unit(v) -> np.ndarray:
    arr = np.asarray(v, dtype=np.float32)
    return arr / float(np.linalg.norm(arr))


def _l2_score_for(cosine: float) -> float:
    """What ``1.0 - L2`` is for two unit vectors at this cosine."""
    return 1.0 - math.sqrt(max(2.0 - 2.0 * cosine, 0.0))


@pytest.fixture
def index(tmp_path):
    idx = SQLiteVecIndex(dim=3, db_path=str(tmp_path / "vec.db"))
    if not idx.indexed:
        pytest.skip(
            "sqlite-vec extension not available on this host; these tests "
            "measure what the vec0 table actually returns"
        )
    return idx


@pytest.mark.asyncio
async def test_sqlite_vec_score_is_one_minus_l2_not_a_cosine(index):
    """The load-bearing measurement. Two unit vectors 45 degrees apart
    have cosine 0.7071; the backend reports 0.2346."""
    query = _unit([1.0, 0.0, 0.0])
    theta = math.pi / 4
    doc = _unit([math.cos(theta), math.sin(theta), 0.0])
    await index.upsert("doc", doc)

    hits = await index.search_similarity(query, limit=1)
    assert hits, "search returned nothing"
    _, score = hits[0]

    true_cosine = float(np.dot(query, doc))
    assert true_cosine == pytest.approx(0.7071, abs=1e-3)
    assert score == pytest.approx(_l2_score_for(true_cosine), abs=1e-3)
    assert score == pytest.approx(0.2346, abs=1e-3)
    assert abs(score - true_cosine) > 0.4, (
        f"score {score} is close enough to the cosine {true_cosine} that "
        "this test is not measuring what it claims"
    )
    await index.close()


@pytest.mark.asyncio
async def test_score_goes_negative_for_merely_unrelated_vectors(index):
    """Orthogonal unit vectors are cosine 0.0. The backend reports
    -0.4142, so the value is not even confined to [0, 1] and cannot be
    read as a similarity fraction."""
    query = _unit([1.0, 0.0, 0.0])
    await index.upsert("orthogonal", _unit([0.0, 1.0, 0.0]))

    hits = await index.search_similarity(query, limit=1)
    _, score = hits[0]
    assert score == pytest.approx(1.0 - math.sqrt(2.0), abs=1e-3)
    assert score < 0.0, score
    await index.close()


@pytest.mark.asyncio
async def test_a_cosine_floor_on_this_score_destroys_recall(index):
    """The trap, executed. A 0.5 floor is a mild cosine floor that every
    one of these documents clears easily, and applied to the returned
    score it silently drops two of the three.

    In general ``1 - L2 >= t`` is ``cos >= 1 - (1-t)**2 / 2``, so a 0.5
    floor is really a cosine floor of 0.875 and a 0.25 floor (the one
    ``memory/notes_legacy.py`` uses) is really 0.71875."""
    query = _unit([1.0, 0.0, 0.0])
    wanted = {}
    for name, cosine in (("near", 0.95), ("mid", 0.84), ("far", 0.7071)):
        doc = _unit([cosine, math.sqrt(1.0 - cosine**2), 0.0])
        await index.upsert(name, doc)
        wanted[name] = cosine

    hits = await index.search_similarity(query, limit=5)
    scores = {cid: s for cid, s in hits}
    assert set(scores) == set(wanted), scores

    # Every true cosine clears the floor comfortably.
    assert all(c >= 0.5 for c in wanted.values())
    # The reported score does not, for two of the three.
    survivors = sorted(cid for cid, s in scores.items() if s >= 0.5)
    assert survivors == ["near"], (
        f"expected a 0.5 floor to drop mid and far; scores were {scores}"
    )
    # The implied cosine floor, stated as a number.
    implied = 1.0 - (1.0 - 0.5) ** 2 / 2.0
    assert implied == pytest.approx(0.875)
    assert scores["mid"] < 0.5 < 0.84, scores
    await index.close()


@pytest.mark.asyncio
async def test_ranking_is_still_monotone_in_cosine(index):
    """Why this was survivable. The transform is monotone on unit
    vectors, so ordering is correct and the search results were never
    wrong, only the magnitudes."""
    query = _unit([1.0, 0.0, 0.0])
    for name, cosine in (("best", 0.99), ("middle", 0.8), ("worst", 0.2)):
        doc = _unit([cosine, math.sqrt(1.0 - cosine**2), 0.0])
        await index.upsert(name, doc)

    hits = await index.search_similarity(query, limit=5)
    assert [cid for cid, _ in hits] == ["best", "middle", "worst"], hits
    await index.close()


@pytest.mark.asyncio
async def test_default_backend_does_not_offer_a_method_called_search_cosine(index):
    """The name was the trap. A backend that returns ``1 - L2`` must not
    advertise a cosine, because the next person to add a floor will read
    the name and not the implementation."""
    assert not hasattr(index, "search_cosine"), (
        "sqlite_vec still exposes search_cosine, whose return value is not "
        "a cosine"
    )
    assert hasattr(index, "search_similarity")
    await index.close()


@pytest.mark.asyncio
async def test_notes_vector_floor_agrees_between_indexed_and_numpy_paths(tmp_path):
    """The trap was not hypothetical: ``memory/notes_legacy.py`` gates
    the notes vector leg on ``_NOTES_VEC_MIN_SIM = 0.25``, documented as
    a cosine floor, and applied it to the index score on the indexed
    branch and to a real cosine on the numpy branch.

    ``1 - L2 >= 0.25`` is ``cos >= 0.71875``, so a note at cosine 0.5
    was returned when sqlite-vec was unavailable and dropped when it was
    available. Whether a note is findable must not depend on whether the
    interpreter was built with loadable SQLite extensions.
    """
    import time as _time

    import aiosqlite

    from memory.embeddings import vec_to_blob
    from memory.notes_legacy import _vec_results_for_notes
    from memory.store import MemoryStore

    class _Backend:
        backend_id = "stub"

        def __init__(self, indexed, vectors):
            self.indexed = indexed
            self._vectors = vectors

        async def count(self):
            return len(self._vectors)

        async def upsert(self, chunk_id, embedding):
            return None

        async def upsert_batch(self, items):
            return None

        async def delete(self, chunk_id):
            return None

        async def search(self, query_vec, limit=20):
            # Faithful vec0 emulation: L2 distance, nearest first.
            rows = [
                (cid, float(np.linalg.norm(np.asarray(v) - np.asarray(query_vec))))
                for cid, v in self._vectors.items()
            ]
            rows.sort(key=lambda r: r[1])
            return rows[:limit]

        async def search_similarity(self, query_vec, limit=20):
            return [(cid, 1.0 - d) for cid, d in await self.search(query_vec, limit)]

        async def close(self):
            return None

    query = _unit([1.0, 0.0, 0.0, 0.0])
    # True cosine 0.5: comfortably over the documented 0.25 floor, and
    # comfortably under the 0.71875 the index score implies.
    mid = _unit([0.5, math.sqrt(0.75), 0.0, 0.0])
    vectors = {"note_n1_c0": mid}

    db = str(tmp_path / "notes.db")
    MemoryStore(db_path=db, vec_index=_Backend(False, vectors))
    conn = await aiosqlite.connect(db)
    try:
        await conn.execute(
            "INSERT OR REPLACE INTO memory_chunks "
            "(id, source_table, source_id, chunk_index, text_content, embedding, created_at) "
            "VALUES ('note_n1_c0', 'notes', 'n1', 0, 'body', ?, ?)",
            (vec_to_blob(mid), _time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()

    async def _leg(indexed: bool):
        store = MemoryStore(db_path=db, vec_index=_Backend(indexed, vectors))
        conn = await store._conn()
        try:
            return await _vec_results_for_notes(
                store, conn, query, candidate_factor=3, limit=5
            )
        finally:
            await store._release(conn)
            await store.aclose()

    numpy_path = await _leg(False)
    indexed_path = await _leg(True)

    assert set(numpy_path) == {"n1"}, (
        f"premise broken: cosine 0.5 should clear the 0.25 floor: {numpy_path}"
    )
    assert set(indexed_path) == set(numpy_path), (
        "the indexed path applied the cosine floor to a 1-L2 score and lost "
        f"the note: indexed={indexed_path} numpy={numpy_path}"
    )
    assert indexed_path["n1"]["vec_score"] == pytest.approx(0.5, abs=1e-3), (
        "the indexed path must report a real cosine, not the raw index "
        f"score: {indexed_path}"
    )


def test_third_party_backend_with_only_search_cosine_still_loads(tmp_path, monkeypatch):
    """Community backends implement the Protocol from outside this repo
    and are registered by module path, so the rename cannot be allowed
    to break them at query time. ``load_vector_index`` adapts the old
    name and says so."""
    import sys
    import types

    from memory.vector_index_backends.base import load_vector_index, register_backend

    module = types.ModuleType("feral_test_legacy_backend")

    class LegacyBackend:
        backend_id = "legacy_thirdparty"
        indexed = True

        async def count(self):
            return 1

        async def upsert(self, chunk_id, embedding):
            return None

        async def upsert_batch(self, items):
            return None

        async def delete(self, chunk_id):
            return None

        async def search(self, query_vec, limit=20):
            return [("x", 0.25)]

        async def search_cosine(self, query_vec, limit=20):
            return [("x", 0.75)]

        async def close(self):
            return None

    module.create = lambda *, dim, **cfg: LegacyBackend()
    monkeypatch.setitem(sys.modules, "feral_test_legacy_backend", module)
    register_backend("legacy_thirdparty", "feral_test_legacy_backend")

    backend = load_vector_index("legacy_thirdparty", dim=3)
    assert hasattr(backend, "search_similarity"), (
        "the loader did not adapt a legacy backend's search_cosine"
    )

    import asyncio

    hits = asyncio.run(backend.search_similarity(np.zeros(3, dtype=np.float32), limit=1))
    assert hits == [("x", 0.75)], hits
