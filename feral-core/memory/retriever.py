"""PR 8: Unified MemoryRetriever — one ranked, explainable view across
every memory tier we already maintain.

Why this module exists
----------------------
The orchestrator and intent compiler each had their own ad-hoc memory
lookup logic. Each one decided independently which tiers to query, how
to rank results, and whether to fall back when a tier was unavailable.
That made it impossible to answer "what does the agent actually
remember about X right now?" in one place, and it made it equally
impossible to reason about retrieval quality.

This module owns the cross-tier query and returns a single ranked
list of :class:`MemoryRecord` instances. Each record carries its
provenance (which tier it came from, the raw row, the score, and the
ranking signal) so the UI can show the user *why* the agent surfaced
this memory. No silent guesses.

Tiers (queried in this order; gracefully degrades when a tier is
missing)
* notes (``MemoryStore.search``)
* episodes (``MemoryStore.episode_recent`` + substring filter)
* knowledge graph (``MemoryStore.knowledge_query`` on subject)
* execution log (``MemoryStore.log_recent``)
* about-me / identity loader (when wired)

Ranking
-------
We combine a per-tier *base score* with a maximal-marginal-relevance
(MMR) diversity pass to keep the top-k from being dominated by
near-duplicates. The MMR weight (``diversity_lambda``) is tunable and
defaults to ``0.5`` — equal weight on relevance and novelty, which is
the common default in literature.

Provenance
----------
Every record exposes:

* ``tier`` — string id (notes, episode, knowledge, log, about_me)
* ``record_id`` — best-effort stable id from the underlying tier
* ``content`` — the textual content surfaced to the LLM
* ``score`` — final ranked score in ``[0, 1]``
* ``base_score`` — pre-MMR relevance
* ``raw`` — the underlying dict from the tier (for the UI)
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import numpy as np

logger = logging.getLogger("feral.memory.retriever")


_WORD_RE = re.compile(r"[\w']+")

# Hybrid weighting matches :meth:`MemoryStore.episode_search_hybrid` so
# the cross-tier retriever speaks the same blend the episode tier does
# when both legs are available. When the embedding leg degrades to no
# signal (no embedder, hash provider, OpenAI/async-only, or per-call
# failure) the lexical leg owns the score 1:1 — pre-PR behaviour.
_TEXT_WEIGHT = 0.3
_VECTOR_WEIGHT = 0.7


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {t.lower() for t in _WORD_RE.findall(text) if len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity guarded against zero-norm vectors. Local copy
    to keep the retriever import-light (avoids pulling the rest of
    ``memory.embeddings`` for callers that don't need it).
    """
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


@dataclass
class MemoryRecord:
    """One ranked, explainable memory hit."""

    tier: str
    record_id: str
    content: str
    score: float
    base_score: float
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class RetrievalResult:
    """Ranked retrieval result with explainable provenance."""

    query: str
    records: list[MemoryRecord] = field(default_factory=list)
    skipped_tiers: dict[str, str] = field(default_factory=dict)

    def top(self, k: int) -> list[MemoryRecord]:
        return self.records[:max(0, k)]

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "records": [r.as_dict() for r in self.records],
            "skipped_tiers": dict(self.skipped_tiers),
        }


class MemoryRetriever:
    """Cross-tier retriever with MMR-style diversity ranking."""

    def __init__(
        self,
        memory: Any,
        *,
        about_me: Any = None,
        identity_loader: Any = None,
        diversity_lambda: float = 0.5,
        per_tier_limit: int = 10,
    ) -> None:
        self._memory = memory
        self._about_me = about_me
        self._identity_loader = identity_loader
        self._diversity_lambda = max(0.0, min(1.0, diversity_lambda))
        self._per_tier_limit = max(1, per_tier_limit)

    # ── Public API ────────────────────────────────────────────────

    def retrieve(self, query: str, *, top_k: int = 8) -> RetrievalResult:
        result = RetrievalResult(query=query)
        if not query or not query.strip():
            return result

        query_tokens = _tokenize(query)
        # Best-effort sync embedding for the semantic leg. ``None``
        # means "no semantic signal available" and every collector
        # falls through to lexical-only — preserves pre-embedding
        # behaviour 1:1 when the embedder is hash-only / absent /
        # OpenAI-only / degraded.
        query_vec = self._query_embedding(query)
        candidates: list[MemoryRecord] = []

        for tier_fn in (
            self._collect_notes,
            self._collect_episodes,
            self._collect_knowledge,
            self._collect_execution_log,
            self._collect_about_me,
        ):
            try:
                candidates.extend(tier_fn(query, query_tokens, query_vec))
            except Exception as exc:
                logger.warning("retriever tier %s skipped: %s", tier_fn.__name__, exc)
                result.skipped_tiers[tier_fn.__name__.replace("_collect_", "")] = str(exc)

        if not candidates:
            return result

        # Deduplicate by (tier, record_id): a single underlying row
        # surfaced via two code paths must not appear twice.
        seen: dict[tuple[str, str], MemoryRecord] = {}
        for rec in candidates:
            key = (rec.tier, rec.record_id)
            existing = seen.get(key)
            if existing is None or rec.base_score > existing.base_score:
                seen[key] = rec
        candidates = list(seen.values())

        result.records = self._rank_with_mmr(query_tokens, candidates, top_k)
        return result

    # ── Tier collectors ───────────────────────────────────────────

    def _collect_notes(
        self,
        query: str,
        query_tokens: set[str],
        query_vec: Optional[np.ndarray],
    ) -> list[MemoryRecord]:
        rows = self._safe_call(self._memory, "search", query, self._per_tier_limit) or []
        out: list[MemoryRecord] = []
        for row in rows:
            content = row.get("content") or row.get("text") or ""
            score = self._blend_score(query_tokens, query_vec, content)
            out.append(MemoryRecord(
                tier="notes",
                record_id=str(row.get("id") or row.get("note_id") or content[:32]),
                content=content,
                score=score,
                base_score=score,
                raw=dict(row),
            ))
        return out

    def _collect_episodes(
        self,
        query: str,
        query_tokens: set[str],
        query_vec: Optional[np.ndarray],
    ) -> list[MemoryRecord]:
        rows = self._safe_call(self._memory, "episode_recent", self._per_tier_limit * 2) or []
        out: list[MemoryRecord] = []
        for row in rows:
            content = (
                row.get("summary")
                or row.get("description")
                or row.get("title")
                or row.get("content")
                or ""
            )
            score = self._blend_score(query_tokens, query_vec, content)
            if score == 0.0:
                continue
            out.append(MemoryRecord(
                tier="episode",
                record_id=str(row.get("id") or row.get("episode_id") or content[:32]),
                content=content,
                score=score,
                base_score=score,
                raw=dict(row),
            ))
        return out[: self._per_tier_limit]

    def _collect_knowledge(
        self,
        query: str,
        query_tokens: set[str],
        query_vec: Optional[np.ndarray],
    ) -> list[MemoryRecord]:
        out: list[MemoryRecord] = []
        for token in sorted(query_tokens, key=len, reverse=True)[:4]:
            rows = self._safe_call(
                self._memory, "knowledge_query", token, "", self._per_tier_limit
            ) or []
            for row in rows:
                content = (
                    f"{row.get('subject', '')} {row.get('predicate', '')} "
                    f"{row.get('object', '')}".strip()
                )
                score = self._blend_score(query_tokens, query_vec, content)
                if score == 0.0:
                    continue
                out.append(MemoryRecord(
                    tier="knowledge",
                    record_id=str(row.get("id") or content[:32]),
                    content=content,
                    score=score,
                    base_score=score,
                    raw=dict(row),
                ))
        return out

    def _collect_execution_log(
        self,
        query: str,
        query_tokens: set[str],
        query_vec: Optional[np.ndarray],
    ) -> list[MemoryRecord]:
        rows = self._safe_call(self._memory, "log_recent", "", self._per_tier_limit * 2) or []
        out: list[MemoryRecord] = []
        for row in rows:
            content = (
                row.get("description")
                or row.get("summary")
                or f"{row.get('skill_id','')}/{row.get('endpoint_id','')}"
            )
            score = self._blend_score(query_tokens, query_vec, content)
            if score == 0.0:
                continue
            out.append(MemoryRecord(
                tier="log",
                record_id=str(row.get("id") or content[:32]),
                content=content,
                score=score,
                base_score=score,
                raw=dict(row),
            ))
        return out[: self._per_tier_limit]

    def _collect_about_me(
        self,
        query: str,
        query_tokens: set[str],
        query_vec: Optional[np.ndarray],
    ) -> list[MemoryRecord]:
        text = ""
        for src in (self._about_me, self._identity_loader):
            if src is None:
                continue
            for attr in ("snapshot", "describe", "to_dict"):
                fn = getattr(src, attr, None)
                if callable(fn):
                    try:
                        out = fn()
                    except Exception:
                        continue
                    if isinstance(out, dict):
                        text = " ".join(str(v) for v in out.values())
                    elif isinstance(out, str):
                        text = out
                    break
            if text:
                break
        if not text:
            return []
        score = self._blend_score(query_tokens, query_vec, text)
        if score == 0.0:
            return []
        return [MemoryRecord(
            tier="about_me",
            record_id="profile",
            content=text,
            score=score,
            base_score=score,
            raw={"text": text},
        )]

    # ── Ranking ───────────────────────────────────────────────────

    def _rank_with_mmr(
        self,
        query_tokens: set[str],
        candidates: list[MemoryRecord],
        top_k: int,
    ) -> list[MemoryRecord]:
        if top_k <= 0 or not candidates:
            return []

        lam = self._diversity_lambda
        remaining = sorted(candidates, key=lambda r: r.base_score, reverse=True)
        selected: list[MemoryRecord] = []

        while remaining and len(selected) < top_k:
            best_idx = 0
            best_value = float("-inf")
            for idx, cand in enumerate(remaining):
                if not selected:
                    mmr = cand.base_score
                else:
                    max_sim = max(
                        _jaccard(_tokenize(cand.content), _tokenize(s.content))
                        for s in selected
                    )
                    mmr = lam * cand.base_score - (1.0 - lam) * max_sim
                if mmr > best_value:
                    best_value = mmr
                    best_idx = idx
            chosen = remaining.pop(best_idx)
            chosen.score = max(0.0, min(1.0, best_value))
            selected.append(chosen)
        return selected

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _lexical_score(query_tokens: set[str], content: str) -> float:
        if not query_tokens or not content:
            return 0.0
        return _jaccard(query_tokens, _tokenize(content))

    def _query_embedding(self, query: str) -> Optional[np.ndarray]:
        """Return a real semantic embedding for ``query`` or ``None``.

        ``None`` means "no embedding leg this turn — every collector
        must score with the lexical leg only". This is the single
        graceful-degradation gate for the whole semantic path:

        * memory has no ``embedder`` (test stubs / non-MemoryStore
          callers) → ``None``
        * embedder doesn't expose ``embed_sync`` (older provider) →
          ``None``
        * ``embed_sync`` returns ``None`` (hash provider, OpenAI-only,
          provider degraded) → ``None``
        * any unexpected exception → ``None``
        """
        embedder = self._get_embedder()
        if embedder is None:
            return None
        embed_sync = getattr(embedder, "embed_sync", None)
        if embed_sync is None:
            return None
        try:
            vec = embed_sync(query)
        except Exception:
            return None
        if vec is None:
            return None
        try:
            return np.asarray(vec, dtype=np.float32)
        except Exception:
            return None

    def _content_embedding(self, content: str) -> Optional[np.ndarray]:
        """Per-candidate embedding for the semantic leg. Same gates as
        :meth:`_query_embedding` so a transient failure on a single
        document drops that document's semantic score to 0 rather than
        breaking the whole retrieve call."""
        if not content:
            return None
        embedder = self._get_embedder()
        if embedder is None:
            return None
        embed_sync = getattr(embedder, "embed_sync", None)
        if embed_sync is None:
            return None
        try:
            vec = embed_sync(content[:2000])
        except Exception:
            return None
        if vec is None:
            return None
        try:
            return np.asarray(vec, dtype=np.float32)
        except Exception:
            return None

    def _get_embedder(self) -> Any:
        """Resolve the embedder attached to the underlying memory.

        Looks for the public ``embedder`` property first (the
        documented :class:`MemoryStore` surface) and falls back to the
        private ``_embedder`` attribute so tests / older stores still
        work."""
        embedder = getattr(self._memory, "embedder", None)
        if embedder is not None:
            return embedder
        return getattr(self._memory, "_embedder", None)

    def _blend_score(
        self,
        query_tokens: set[str],
        query_vec: Optional[np.ndarray],
        content: str,
    ) -> float:
        """Combined lexical + semantic score in ``[0, 1]``.

        When ``query_vec`` is ``None`` the function returns the
        lexical score 1:1 — so callers that previously compared
        scores against ``0.0`` to filter "no overlap" still see the
        same boundary. When the embedder is present and produces a
        real vector, the score is the same convex combination
        :meth:`MemoryStore.episode_search_hybrid` uses (0.3 lex +
        0.7 vec).
        """
        lex = self._lexical_score(query_tokens, content)
        if query_vec is None:
            return lex
        cvec = self._content_embedding(content)
        if cvec is None or cvec.shape != query_vec.shape:
            # No semantic signal for this candidate — fall back to
            # lexical so we don't penalise it for the embedder
            # transiently failing on this row.
            return lex
        sem = max(0.0, _cosine(query_vec, cvec))
        return _TEXT_WEIGHT * lex + _VECTOR_WEIGHT * sem

    @staticmethod
    def _safe_call(target: Any, method: str, *args, **kwargs):
        fn = getattr(target, method, None)
        if fn is None:
            return None
        return fn(*args, **kwargs)


__all__ = ["MemoryRecord", "MemoryRetriever", "RetrievalResult"]
