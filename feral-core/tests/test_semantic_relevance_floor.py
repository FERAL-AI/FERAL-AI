"""Search must be able to answer "nothing here matches that".

The vector leg rejected results below a raw cosine of 0.25. On the store
where this was found, all 11,996 chunks cleared that floor for every
query, including "asdfgh zxcvbn qwerty", so search always returned its
full limit however irrelevant the corpus was to the question. There was
no "I don't know" state, which is worse than an empty result: a confident
wrong memory is harder to distrust than no memory.

The cause is anisotropy, a property of the embedding model rather than a
defect in this store. Sentence embeddings occupy a narrow cone instead of
spreading over the sphere. Measured on that corpus the mean vector had
norm 0.799 and the average chunk sat at cosine 0.799 from it, so every
comparison was dominated by a direction all documents share and raw
cosines landed near 0.53 whatever was asked. No absolute threshold can
work in that space, which is why the answer was not to pick a better
number: 0.25 was not too low, it was measuring the wrong thing.

Subtracting the corpus mean removes the shared component. Same corpus:

                                     raw max   centered max
    "CuteBot lights"       present     0.878        0.759
    "how does the relay work" absent   0.636        0.350
    "asdfgh zxcvbn qwerty"  nonsense   0.696        0.343

Raw, nonsense outscores a real question. Centered, hits and misses
separate into bands that do not overlap.
"""

from __future__ import annotations

import numpy as np
import pytest

from memory.store import (
    _CENTERED_SEMANTIC_FLOOR,
    _MIN_CHUNKS_FOR_CENTERING,
    _RAW_SEMANTIC_FLOOR,
    MemoryStore,
)


def _anisotropic_corpus(n=400, dim=64, seed=0):
    """Vectors sharing a strong common direction, like the real thing.

    Without the shared component this test would pass under the old code
    too, and would prove nothing about the bug.
    """
    rng = np.random.default_rng(seed)
    shared = rng.normal(size=dim)
    shared /= np.linalg.norm(shared)

    def _in_cone(r):
        noise = r.normal(size=dim)
        noise /= np.linalg.norm(noise)
        v = 0.9 * shared + 0.1 * noise
        return (v / np.linalg.norm(v)).astype(np.float32)

    return np.array([_in_cone(rng) for _ in range(n)]), shared


def _unrelated_query(dim=64, seed=99):
    """A query with no answer in the corpus.

    It must be built the same way the documents are. A real query is itself
    a sentence embedding, so it lies in the same narrow cone: that is
    precisely why raw cosine cannot separate it from a true match. Drawing
    it from open space instead would make the old floor look like it worked.
    """
    rng = np.random.default_rng(seed)
    shared = np.random.default_rng(0).normal(size=dim)
    shared /= np.linalg.norm(shared)
    noise = rng.normal(size=dim)
    noise /= np.linalg.norm(noise)
    v = 0.9 * shared + 0.1 * noise
    return (v / np.linalg.norm(v)).astype(np.float32)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "m.db"))


class TestTheCorpusIsActuallyAnisotropic:
    def test_the_fixture_reproduces_the_condition(self):
        """Guard the guard: if this stops holding, the tests below stop
        testing anything."""
        V, _ = _anisotropic_corpus()
        centroid = V.mean(axis=0)
        assert np.linalg.norm(centroid) > 0.7

        q = _unrelated_query(dim=V.shape[1])
        raw = V @ q
        # An unrelated query still scores far above the old floor on every
        # single document. That is the bug, reproduced.
        assert (raw > _RAW_SEMANTIC_FLOOR).mean() > 0.9


class TestCenteringSeparatesHitsFromMisses:
    def test_an_unrelated_query_scores_below_the_floor(self, store):
        V, _ = _anisotropic_corpus()
        blobs = [v.tobytes() for v in V]
        q = _unrelated_query(dim=V.shape[1])

        scores = store._centered_similarity(q, blobs)

        assert scores is not None
        assert scores.max() < _CENTERED_SEMANTIC_FLOOR, (
            f"unrelated query still scored {scores.max():.3f}"
        )

    def test_a_genuine_match_scores_above_the_floor(self, store):
        V, _ = _anisotropic_corpus()
        # Query is one stored document, so it must be retrievable.
        q = V[7].copy()
        blobs = [v.tobytes() for v in V]

        scores = store._centered_similarity(q, blobs)

        assert scores[7] > _CENTERED_SEMANTIC_FLOOR
        assert int(scores.argmax()) == 7


class TestDegenerateInputsAreSafe:
    def test_zero_vectors_can_never_be_returned(self, store):
        """Four chunks in the real store are all-zero from failed
        embeddings. They must not match, and must not drag the centre."""
        V, _ = _anisotropic_corpus()
        V[3] = np.zeros_like(V[3])
        blobs = [v.tobytes() for v in V]
        q = V[7].copy()

        scores = store._centered_similarity(q, blobs)

        assert scores[3] < _CENTERED_SEMANTIC_FLOOR
        assert len(scores) == len(blobs), "positional zip with rows broken"

    def test_a_small_corpus_falls_back_rather_than_centring(self, store):
        """Below the minimum the mean is whichever few documents exist, so
        centring would subtract noise. A new install must behave as before."""
        V, _ = _anisotropic_corpus(n=_MIN_CHUNKS_FOR_CENTERING - 1)
        assert store._centered_similarity(V[0], [v.tobytes() for v in V]) is None

    def test_a_dimension_mismatch_falls_back_instead_of_raising(self, store):
        V, _ = _anisotropic_corpus(dim=64)
        wrong = np.ones(32, dtype=np.float32)
        assert store._centered_similarity(wrong, [v.tobytes() for v in V]) is None


class TestTheCentreIsOnlyDerivedFromTheCorpus:
    def test_scoring_a_handful_does_not_redefine_the_centre(self, store):
        """The subtle one. _centered_filter scores the few rows the index
        returned, after the centre was built from everything. If that call
        were allowed to rebuild, it would derive a "corpus mean" from
        fifteen documents, subtract them from themselves, and score every
        real hit near zero."""
        V, _ = _anisotropic_corpus()
        blobs = [v.tobytes() for v in V]
        q = V[7].copy()

        store._centered_similarity(q, blobs)          # establishes the centre
        centre_before = store._centroid.copy()
        n_before = store._centroid_n

        few = [V[i].tobytes() for i in range(15)]
        scores = store._centered_similarity(q, few, min_chunks=1)

        assert np.array_equal(store._centroid, centre_before), "centre was rebuilt"
        assert store._centroid_n == n_before
        assert scores is not None and len(scores) == 15

    def test_a_small_set_without_an_established_centre_declines(self, store):
        V, _ = _anisotropic_corpus()
        few = [V[i].tobytes() for i in range(15)]
        assert store._centered_similarity(V[0], few, min_chunks=1) is None

    def test_the_centre_is_rebuilt_once_the_corpus_grows(self, store):
        V, _ = _anisotropic_corpus(n=400)
        store._centered_similarity(V[0], [v.tobytes() for v in V])
        assert store._centroid_n == 400

        bigger, _ = _anisotropic_corpus(n=800, seed=5)
        store._centered_similarity(bigger[0], [v.tobytes() for v in bigger])
        assert store._centroid_n == 800


class TestTheFloorsAreOrdered:
    def test_the_two_floors_are_not_interchangeable(self):
        """Centred scores are not on the same scale as raw cosines, and
        mixing the two constants up would silently disable the filter.

        The direction of the inequality carries no meaning: the two numbers
        live in different spaces. It is asserted only so a swap of the two
        constants fails here instead of in production. It reversed when
        _RAW_SEMANTIC_FLOOR was raised from 0.25 (which, as this module's
        docstring says, never rejected anything) to a value measured the
        same way 0.47 was, on the small corpora that constant actually
        governs. See the block comment in memory/store.py for that table.
        """
        assert _RAW_SEMANTIC_FLOOR > _CENTERED_SEMANTIC_FLOOR
        assert 0.0 < _CENTERED_SEMANTIC_FLOOR < 1.0
        assert 0.0 < _RAW_SEMANTIC_FLOOR < 1.0
