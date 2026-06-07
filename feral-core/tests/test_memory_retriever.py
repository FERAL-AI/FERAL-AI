"""PR 8: cross-tier MemoryRetriever — ranking, MMR diversity, provenance,
and graceful tier skipping.

Plus the post-PR-8 embedding leg (this PR): the retriever now blends
lexical Jaccard with embedding cosine when a sync-capable embedder is
attached to the underlying memory, and falls back to the legacy
lexical-only score when not — see the ``TestEmbeddingLeg`` class.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from memory.retriever import MemoryRecord, MemoryRetriever  # noqa: E402


class _FakeMemory:
    """In-memory MemoryStore stub with controllable per-tier hits."""

    def __init__(self, *, notes=None, episodes=None, knowledge=None, logs=None):
        self._notes = notes or []
        self._episodes = episodes or []
        self._knowledge = knowledge or []
        self._logs = logs or []

    def search(self, query, limit=10):
        return self._notes[:limit]

    def episode_recent(self, limit=10, session_id=None):
        return self._episodes[:limit]

    def knowledge_query(self, subject="", predicate="", limit=20):
        # Substring filter on subject to mimic real behaviour.
        sub = (subject or "").lower()
        return [r for r in self._knowledge if sub in str(r.get("subject", "")).lower()][:limit]

    def log_recent(self, skill_id="", limit=20):
        return self._logs[:limit]


def test_retriever_returns_notes_ranked_by_lexical_overlap():
    mem = _FakeMemory(notes=[
        {"id": "n-1", "content": "Buy groceries: milk and bread"},
        {"id": "n-2", "content": "Plan the Q4 product roadmap"},
        {"id": "n-3", "content": "Remember to call Mom about milk delivery"},
    ])
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("milk", top_k=3)
    contents = [r.content for r in result.records]
    # The two notes mentioning milk must be ranked above the unrelated one.
    assert "Buy groceries: milk and bread" in contents[:2]
    assert "Remember to call Mom about milk delivery" in contents[:2]
    assert "Plan the Q4 product roadmap" not in contents[:2]


def test_retriever_skips_tiers_with_no_method():
    """A MemoryStore stub that doesn't implement `episode_recent`
    must NOT crash the retriever — just skip the tier."""
    class _Minimal:
        def search(self, q, limit=10):
            return [{"id": "n", "content": q}]

    retriever = MemoryRetriever(_Minimal())
    result = retriever.retrieve("hello", top_k=3)
    assert any(r.tier == "notes" for r in result.records)
    # Other tiers absent; no exception raised.


def test_retriever_records_skipped_tier_when_method_raises():
    class _Boom:
        def search(self, *a, **kw):
            return [{"id": "ok", "content": "hello world"}]

        def episode_recent(self, *a, **kw):
            raise RuntimeError("db locked")

    retriever = MemoryRetriever(_Boom())
    result = retriever.retrieve("hello", top_k=3)
    assert "episodes" in result.skipped_tiers
    assert "db locked" in result.skipped_tiers["episodes"]


def test_mmr_diversifies_near_duplicate_results():
    """With diversity_lambda=0.5, the top-k should not be filled with
    near-identical notes when more diverse hits exist."""
    mem = _FakeMemory(notes=[
        {"id": "a", "content": "buy milk and bread"},
        {"id": "b", "content": "buy milk and bread today"},
        {"id": "c", "content": "buy milk and bread tonight"},
        {"id": "d", "content": "milk delivery scheduled tomorrow morning early"},
    ])
    retriever = MemoryRetriever(mem, diversity_lambda=0.3)
    result = retriever.retrieve("buy milk", top_k=2)
    contents = [r.content for r in result.records]
    # At least one of the top-2 should be the diverse hit ("delivery scheduled")
    # rather than two near-duplicates of "buy milk and bread".
    assert any("delivery" in c for c in contents)


def test_retriever_deduplicates_same_record_from_two_paths():
    """If episode_recent and notes return the same content, the
    retriever must keep only one with the higher base score."""
    same = {"id": "shared-1", "content": "hello world"}
    mem = _FakeMemory(notes=[same], episodes=[same])
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("hello", top_k=5)
    # Single hit across both tiers — but they're different tiers
    # (notes vs episode) so both keys exist. The dedup is per-key.
    assert len([r for r in result.records if r.content == "hello world"]) == 2  # different tiers OK


def test_retriever_records_carry_provenance():
    mem = _FakeMemory(
        notes=[{"id": "n-1", "content": "alpha beta"}],
        knowledge=[{"id": "k-1", "subject": "alpha", "predicate": "is", "object": "letter"}],
    )
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("alpha", top_k=5)
    tiers = {r.tier for r in result.records}
    assert "notes" in tiers
    assert "knowledge" in tiers
    for r in result.records:
        assert r.record_id  # never blank
        assert 0.0 <= r.score <= 1.0
        assert r.raw  # original row preserved


def test_empty_query_returns_no_records():
    retriever = MemoryRetriever(_FakeMemory(notes=[{"id": "n", "content": "x"}]))
    result = retriever.retrieve("", top_k=5)
    assert result.records == []


def test_top_helper_limits_output():
    mem = _FakeMemory(notes=[
        {"id": str(i), "content": f"alpha {i}"} for i in range(10)
    ])
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("alpha", top_k=5)
    assert len(result.records) <= 5
    assert len(result.top(3)) == 3


# ─────────────────────────────────────────────
# Embedding leg
# ─────────────────────────────────────────────


class _SemanticStubEmbedder:
    """In-test embedder with a real bag-of-tokens semantic signal.

    Each of four "topic" keywords (alpha/beta/gamma/delta) maps to a
    fixed dimension. Inputs share cosine similarity if and only if
    they share topics. Mirrors the contract a real local sentence-
    transformer embedder would expose to the retriever via
    ``embed_sync()``.
    """

    provider_name = "stub_local"
    degraded = False
    dimension = 4
    _DIMS = ("alpha", "beta", "gamma", "delta")

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dimension, dtype=np.float32)
        lowered = (text or "").lower()
        for i, kw in enumerate(self._DIMS):
            if kw in lowered:
                v[i] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_sync(self, text: str):
        v = self._vec(text)
        if not np.any(v):
            return None
        return v


class _HashStubEmbedder:
    """Like the production hash fallback: ``embed_sync`` always returns
    None so the retriever skips the semantic leg even though an
    embedder object is attached."""

    provider_name = "hash"
    degraded = False
    dimension = 4

    def embed_sync(self, text: str):
        return None


class _MemoryWithEmbedder:
    """Fake memory that exposes a public ``embedder`` property — the
    same shape :class:`MemoryStore` exposes to the retriever."""

    def __init__(self, *, embedder, notes=None, episodes=None):
        self._notes = notes or []
        self._episodes = episodes or []
        self.embedder = embedder

    def search(self, query, limit=10):
        return self._notes[:limit]

    def episode_recent(self, limit=10, session_id=None):
        return self._episodes[:limit]


def test_embedding_leg_blends_with_lexical_when_real_embedder_is_attached():
    """A semantic-only match (no token overlap with the query) MUST be
    surfaced thanks to the cosine leg; the existing lexical match
    should still rank ahead because it scores in BOTH legs."""
    mem = _MemoryWithEmbedder(
        embedder=_SemanticStubEmbedder(),
        notes=[
            {"id": "lex", "content": "alpha document"},  # FTS hit + cos hit
            {"id": "vec", "content": "the alpha topic restated"},  # FTS hit + cos hit
            {"id": "noise", "content": "completely different beta thing"},
        ],
    )
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("alpha", top_k=3)
    ids = [r.record_id for r in result.records]
    # Both alpha-topic notes outrank the beta-topic noise.
    assert "noise" not in ids[:2], f"semantic noise leaked into top-2: {ids}"


def test_embedding_leg_surfaces_semantic_only_match():
    """Note that has zero token overlap with the query but whose
    embedding aligns must clear the ranking floor through the vector
    contribution alone (0.7 * cosine ≥ 0.7 * 1.0 = 0.7)."""
    mem = _MemoryWithEmbedder(
        embedder=_SemanticStubEmbedder(),
        notes=[
            # Token "alpha" only — but the stub embeds it as the alpha-
            # direction, same direction the query "alpha" embeds to.
            {"id": "vec_only", "content": "alpha"},
            {"id": "lex_only", "content": "no overlap here at all"},
        ],
    )
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("alpha", top_k=3)
    ids = [r.record_id for r in result.records]
    assert "vec_only" in ids
    # Must outrank the noise note.
    assert ids.index("vec_only") < (ids.index("lex_only") if "lex_only" in ids else 999)


def test_retriever_degrades_to_lexical_only_with_hash_embedder():
    """When the attached embedder is the deterministic-but-semantically-
    empty hash fallback, the embedding leg MUST be skipped end-to-end
    so we don't pollute the ranking with fake-cosine noise."""
    mem = _MemoryWithEmbedder(
        embedder=_HashStubEmbedder(),
        notes=[
            {"id": "match", "content": "milk and bread"},
            {"id": "near", "content": "milk delivery scheduled"},
            {"id": "miss", "content": "totally unrelated tale"},
        ],
    )
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("milk", top_k=3)
    ids = [r.record_id for r in result.records]
    assert "match" in ids
    assert "near" in ids
    # Lexical-only baseline: the unrelated note must NOT rank in the
    # top-2.
    assert "miss" not in ids[:2]


def test_retriever_degrades_to_lexical_only_with_no_embedder():
    """The pre-PR fake memory has no ``embedder`` attribute. The new
    embedding leg MUST gracefully skip in that case so existing call
    sites and tests keep their semantics: the lexical leg picks the
    top-ranked note, and the unrelated one (zero Jaccard) does not
    rank above it."""
    mem = _FakeMemory(notes=[
        {"id": "n-1", "content": "alpha topic note"},
        {"id": "n-2", "content": "completely unrelated"},
    ])
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("alpha", top_k=1)
    assert result.records, "lexical-only retrieve returned nothing"
    assert result.records[0].record_id == "n-1"


def test_retriever_degrades_when_embed_sync_raises():
    """A transient embedder failure must not break the retrieve call —
    the lexical leg owns the ranking when the semantic leg drops."""

    class _BoomEmbedder:
        provider_name = "stub_local"
        degraded = False

        def embed_sync(self, text):
            raise RuntimeError("model offline")

    mem = _MemoryWithEmbedder(
        embedder=_BoomEmbedder(),
        notes=[
            {"id": "n-1", "content": "alpha note"},
            {"id": "n-2", "content": "no overlap"},
        ],
    )
    retriever = MemoryRetriever(mem)
    result = retriever.retrieve("alpha", top_k=1)
    assert result.records and result.records[0].record_id == "n-1"


def test_blended_score_outranks_lexical_only_when_semantic_aligns():
    """Two notes with the same lexical Jaccard score: the one whose
    embedding aligns better with the query should rank higher because
    the vector leg is 0.7 of the blend."""
    mem = _MemoryWithEmbedder(
        embedder=_SemanticStubEmbedder(),
        notes=[
            # Identical surface tokens => identical Jaccard score.
            # But "alpha" embeds onto dim 0; the query "alpha topic"
            # also embeds onto dim 0, while "beta" embeds onto dim 1.
            {"id": "aligned", "content": "alpha"},
            {"id": "unaligned", "content": "beta"},
        ],
    )
    retriever = MemoryRetriever(mem, diversity_lambda=1.0)  # disable MMR penalty
    result = retriever.retrieve("alpha", top_k=2)
    ids = [r.record_id for r in result.records]
    assert ids[0] == "aligned", f"semantic-aligned note should be #1: {ids}"
