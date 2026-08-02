"""Pins for the vectorized vector-scan path (``cosine_similarity_bulk``).

The numpy brute-force scan is the DEFAULT retrieval path on any interpreter
that cannot load the sqlite-vec extension (pyenv on macOS builds
``enable_load_extension`` out by default), and it used to run as a Python
per-row loop of ``blob_to_vec`` + ``cosine_similarity``. Measured on this
machine at 384 dims: 286ms per query at 100k chunks, versus 23ms for the
blocked matmul this module tests.

The speedup is only worth anything if the answers are unchanged, so these
tests pin three things the fast path could plausibly break:

1. identical scores and identical ranking versus the per-row loop,
2. the loud, throttled :class:`EmbeddingDimensionMismatch` on a
   wrong-width row (the scan callers all swallow exceptions into
   ``logger.debug``, so a silent wrong answer is invisible),
3. ragged input. ``np.frombuffer(b"".join(blobs))`` over MIXED widths does
   not fail, it returns confident garbage, and mixed widths are exactly
   what a provider switch leaves in the table.
"""
from __future__ import annotations

import numpy as np
import pytest

from memory.embeddings import (
    LOCAL_DIM,
    OPENAI_DIM,
    EmbeddingDimensionMismatch,
    blob_to_vec,
    cosine_similarity,
    cosine_similarity_bulk,
    vec_to_blob,
)


def _blobs(mat: np.ndarray) -> list[bytes]:
    return [row.tobytes() for row in mat]


def _scalar_scores(query: np.ndarray, blobs: list[bytes]) -> list[float]:
    """Exactly what the four call sites used to do, row by row."""
    return [cosine_similarity(query, blob_to_vec(b)) for b in blobs]


# ──────────────────────────────────────────────────────────────────
# Equivalence with the per-row loop it replaces
# ──────────────────────────────────────────────────────────────────


class TestScalarEquivalence:

    def test_scores_and_ranking_match_the_per_row_loop(self):
        """Same random set, scored both ways: scores agree to float
        tolerance and the ranking is the same, which is the only thing the
        callers actually consume.

        The ranking assertion is 'the score at each rank position is the
        same', not 'the row at each rank position is the same'. Both paths
        run in float32 and accumulate in a different order (sdot per row vs
        one sgemv over the block), so scores can differ by ~5e-8. Measured
        over 2000 random 384-dim rows that is enough to swap two rows whose
        scores are within 1e-8 of each other, and pinning exact index
        equality would pin float32 accumulation order, not behaviour.
        """
        rng = np.random.default_rng(20260731)
        mat = rng.standard_normal((2000, LOCAL_DIM)).astype(np.float32)
        query = rng.standard_normal(LOCAL_DIM).astype(np.float32)
        blobs = _blobs(mat)

        expected = np.asarray(_scalar_scores(query, blobs), dtype=np.float64)
        actual = cosine_similarity_bulk(query, blobs).astype(np.float64)

        assert np.allclose(actual, expected, rtol=1e-5, atol=1e-6)
        order_expected = np.argsort(-expected, kind="stable")
        order_actual = np.argsort(-actual, kind="stable")
        assert np.abs(
            expected[order_expected] - expected[order_actual]
        ).max() < 1e-6

    def test_crosses_the_internal_block_boundary(self):
        """The implementation decodes in blocks of _BULK_SCAN_BLOCK_ROWS.
        A row count that is not a multiple of the block size exercises the
        short final block and the per-block output slicing."""
        import memory.embeddings as emb

        n = emb._BULK_SCAN_BLOCK_ROWS * 2 + 37
        rng = np.random.default_rng(7)
        mat = rng.standard_normal((n, 16)).astype(np.float32)
        query = rng.standard_normal(16).astype(np.float32)
        blobs = _blobs(mat)

        actual = cosine_similarity_bulk(query, blobs)
        assert actual.shape == (n,)
        assert np.allclose(
            actual.astype(np.float64),
            np.asarray(_scalar_scores(query, blobs), dtype=np.float64),
            rtol=1e-5, atol=1e-6,
        )

    def test_identical_vector_scores_one(self):
        vec = np.arange(LOCAL_DIM, dtype=np.float32) + 1.0
        out = cosine_similarity_bulk(vec, [vec_to_blob(vec)])
        assert abs(float(out[0]) - 1.0) < 1e-5

    def test_empty_input_returns_empty_array(self):
        out = cosine_similarity_bulk(np.ones(LOCAL_DIM, dtype=np.float32), [])
        assert out.shape == (0,)

    def test_result_is_writable(self):
        """The decode uses np.frombuffer, which yields a READ-ONLY view.
        Callers must never receive one of those: an in-place normalisation
        or a sort on the returned array would raise ValueError."""
        rng = np.random.default_rng(3)
        mat = rng.standard_normal((5, 8)).astype(np.float32)
        out = cosine_similarity_bulk(mat[0], _blobs(mat))
        assert out.flags.writeable
        out.sort()  # would raise on a read-only buffer


# ──────────────────────────────────────────────────────────────────
# Zero-norm rows
# ──────────────────────────────────────────────────────────────────


class TestZeroNorm:
    """An all-zeros embedding is reachable (a degraded provider that wrote a
    blank vector). The scalar path returns 0.0 for it. NaN here would poison
    every ranking downstream, since NaN compares False against every
    threshold and sorts unpredictably."""

    def test_zero_row_scores_zero_not_nan(self):
        query = np.ones(8, dtype=np.float32)
        zeros = np.zeros(8, dtype=np.float32)
        good = np.arange(8, dtype=np.float32) + 1.0
        out = cosine_similarity_bulk(query, [vec_to_blob(zeros), vec_to_blob(good)])
        assert float(out[0]) == 0.0
        assert not np.isnan(out).any()
        assert abs(float(out[1]) - cosine_similarity(query, good)) < 1e-6

    def test_zero_query_scores_all_zero(self):
        query = np.zeros(8, dtype=np.float32)
        rng = np.random.default_rng(11)
        mat = rng.standard_normal((4, 8)).astype(np.float32)
        out = cosine_similarity_bulk(query, _blobs(mat))
        assert np.all(out == 0.0)
        assert not np.isnan(out).any()

    def test_zero_row_emits_no_divide_warning(self):
        query = np.ones(8, dtype=np.float32)
        blobs = [vec_to_blob(np.zeros(8, dtype=np.float32))] * 4
        with np.errstate(all="raise"):
            out = cosine_similarity_bulk(query, blobs)
        assert np.all(out == 0.0)


# ──────────────────────────────────────────────────────────────────
# Dimension mismatch must stay loud
# ──────────────────────────────────────────────────────────────────


class TestDimensionMismatch:

    def test_uniform_wrong_width_raises(self):
        """Every stored row written by the other provider. This is the
        common shape of the bug: set FERAL_EMBED_PROVIDER=openai over a
        table of 384-dim vectors and every row is 1536-dim."""
        query = np.ones(LOCAL_DIM, dtype=np.float32)
        blobs = [vec_to_blob(np.ones(OPENAI_DIM, dtype=np.float32))] * 3
        with pytest.raises(EmbeddingDimensionMismatch):
            cosine_similarity_bulk(query, blobs)

    def test_mismatch_error_is_a_valueerror(self):
        assert issubclass(EmbeddingDimensionMismatch, ValueError)

    def test_mismatch_is_logged_at_error_level(self, caplog):
        import memory.embeddings as emb

        emb._REPORTED_DIM_MISMATCHES.clear()
        query = np.ones(21, dtype=np.float32)
        blobs = [vec_to_blob(np.ones(23, dtype=np.float32))]
        with caplog.at_level("ERROR", logger="feral.memory.embeddings"):
            with pytest.raises(EmbeddingDimensionMismatch):
                cosine_similarity_bulk(query, blobs)
        assert any("embedding_dimension_mismatch" in r.message for r in caplog.records)
        assert any("query_dim=21 stored_dim=23" in r.message for r in caplog.records)

    def test_mismatch_logs_once_per_pair_across_many_calls(self, caplog):
        """One call now covers what used to be one row, so the throttle has
        to survive the conversion: a scan repeated per query must not
        re-emit the ERROR."""
        import memory.embeddings as emb

        emb._REPORTED_DIM_MISMATCHES.clear()
        query = np.ones(31, dtype=np.float32)
        blobs = [vec_to_blob(np.ones(33, dtype=np.float32))] * 10
        with caplog.at_level("ERROR", logger="feral.memory.embeddings"):
            for _ in range(50):
                with pytest.raises(EmbeddingDimensionMismatch):
                    cosine_similarity_bulk(query, blobs)
        assert sum(
            "embedding_dimension_mismatch" in r.message for r in caplog.records
        ) == 1

    def test_throttle_is_shared_with_the_scalar_path(self):
        """Both paths report into one set, so a pair the scalar path already
        reported is not re-reported by the bulk path (and vice versa)."""
        import memory.embeddings as emb

        emb._REPORTED_DIM_MISMATCHES.clear()
        with pytest.raises(EmbeddingDimensionMismatch):
            cosine_similarity(
                np.ones(41, dtype=np.float32), np.ones(43, dtype=np.float32)
            )
        assert (41, 43) in emb._REPORTED_DIM_MISMATCHES
        with pytest.raises(EmbeddingDimensionMismatch):
            cosine_similarity_bulk(
                np.ones(41, dtype=np.float32),
                [vec_to_blob(np.ones(43, dtype=np.float32))],
            )
        assert emb._REPORTED_DIM_MISMATCHES == {(41, 43)}


# ──────────────────────────────────────────────────────────────────
# Ragged (mixed-width) input
# ──────────────────────────────────────────────────────────────────


class TestRaggedInput:
    """A provider switch leaves the table holding BOTH widths, because there
    is no re-embedding migration. The naive vectorization silently succeeds
    on that input, which is worse than the loop it replaced."""

    def test_mixed_widths_raise_rather_than_scoring_garbage(self):
        query = np.ones(LOCAL_DIM, dtype=np.float32)
        blobs = [
            vec_to_blob(np.ones(LOCAL_DIM, dtype=np.float32)),
            vec_to_blob(np.ones(OPENAI_DIM, dtype=np.float32)),   # other provider
            vec_to_blob(np.ones(LOCAL_DIM, dtype=np.float32)),
        ]
        with pytest.raises(EmbeddingDimensionMismatch):
            cosine_similarity_bulk(query, blobs)

    def test_naive_join_would_have_returned_garbage(self):
        """Demonstrates the trap being guarded against, so the guard is not
        removed later as redundant: joining mixed widths yields a buffer
        that reshapes cleanly and scores nonsense."""
        rows, dim = 4, 8
        good = [vec_to_blob(np.ones(dim, dtype=np.float32)) for _ in range(rows - 2)]
        ragged = good + [
            vec_to_blob(np.ones(dim * 2, dtype=np.float32)),
            b"",  # truncated write: total byte count still adds up
        ]
        buf = b"".join(ragged)
        assert len(buf) == rows * dim * 4  # a total-bytes check would pass
        naive = np.frombuffer(buf, dtype=np.float32).reshape(rows, dim)
        assert naive.shape == (rows, dim)  # ... and reshape would too

        with pytest.raises(EmbeddingDimensionMismatch):
            cosine_similarity_bulk(np.ones(dim, dtype=np.float32), ragged)

    def test_first_offending_row_is_the_one_reported(self):
        """Matches the per-row loop, which raised on the first bad row it
        reached, so the reported (query_dim, stored_dim) pair is unchanged."""
        import memory.embeddings as emb

        emb._REPORTED_DIM_MISMATCHES.clear()
        query = np.ones(10, dtype=np.float32)
        blobs = [
            vec_to_blob(np.ones(10, dtype=np.float32)),
            vec_to_blob(np.ones(12, dtype=np.float32)),   # first bad row
            vec_to_blob(np.ones(14, dtype=np.float32)),
        ]
        with pytest.raises(EmbeddingDimensionMismatch) as excinfo:
            cosine_similarity_bulk(query, blobs)
        assert "12" in str(excinfo.value)
        assert emb._REPORTED_DIM_MISMATCHES == {(10, 12)}

    def test_non_multiple_of_four_width_still_raises_valueerror(self):
        """A truncated blob is not a clean dimension. The old path raised a
        plain ValueError out of np.frombuffer here; this raises the
        ValueError subclass, so every caller's handling is unchanged."""
        query = np.ones(8, dtype=np.float32)
        with pytest.raises(ValueError):
            cosine_similarity_bulk(query, [b"\x00" * 30])
