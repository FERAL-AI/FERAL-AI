"""A stale-dimension vector table must degrade loudly, never silently.

Why this exists
---------------
On this machine's real store, ``entities.embedding`` was left at 1536 dims
by a re-embed that only covered ``memory_chunks``. Two things then went
wrong, and they pull in opposite directions:

1. ``KnowledgeGraph.search_entities`` let ``EmbeddingDimensionMismatch``
   escape, and ``memory/context_builder.search_all`` had no handler, so
   ONE dead tier destroyed the episode, note and knowledge tiers that had
   all answered correctly. Measured against a copy of the real store
   before the fix: 5 of 5 ``search_all`` calls raised, at all three call
   sites (``gateway/protocol.py`` memory.search RPC, ``agents/taskflow.py``
   memory.search step, ``api/routes/memory.py``).

2. The obvious repair, ``except Exception: return []``, would be WORSE.
   An empty result set from a search function reads as "nothing matched",
   so a broken store looks like an empty one and nobody investigates.
   That is how this survived to a release.

The contract pinned here is the middle one: the failing LEG degrades, the
surviving tiers still answer, and the degradation is declared through an
ERROR log naming the fix, through ``store._vector_leg_error`` (which
``/internal/memory/stats`` already reports as
``semantic_search: degraded``) and through
``store.last_search_degradations``. If EVERY tier fails there is no
partial answer left, so the error propagates rather than being reported as
an empty store.
"""

from __future__ import annotations

import sqlite3
import time

import numpy as np
import pytest

from memory.embeddings import EmbeddingDimensionMismatch, vec_to_blob
from memory.store import MemoryStore

DIM = 8
STALE_DIM = 24

pytestmark = pytest.mark.asyncio


class _NoOpVecIndex:
    backend_id = "noop"
    indexed = False

    async def search_cosine(self, query_vec, limit=20):
        return []

    def upsert(self, chunk_id, embedding):
        pass

    def count(self):
        return 0


class _StubEmbedder:
    provider_name = "stub_local"
    active_provider = "stub_local"
    degrade_reason = None
    degraded = False
    available = True
    dimension = DIM

    def _vec_for(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.normal(size=DIM).astype(np.float32)
        return v / np.linalg.norm(v)

    async def embed(self, text: str) -> np.ndarray:
        return self._vec_for(text)

    def embed_sync(self, text: str):
        return self._vec_for(text)

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self._vec_for(t) for t in texts]


@pytest.fixture
async def store(tmp_path):
    """Entities stale at the old provider's width, everything else current."""
    db = str(tmp_path / "memory.db")
    s = MemoryStore(db_path=db, vec_index=_NoOpVecIndex())
    s._embedder = _StubEmbedder()
    if s._kg is not None:
        s._kg._embedder = _StubEmbedder()

    now = time.time()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO episodes "
            "(id, session_id, event_type, summary, detail, importance, created_at) "
            "VALUES ('ep1', 's', 'note', 'sailing across the atlantic', '', 0.5, ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO memory_chunks "
            "(id, source_table, source_id, chunk_index, text_content, embedding, created_at) "
            "VALUES ('ep1_c0', 'episodes', 'ep1', 0, 'sailing across the atlantic', ?, ?)",
            (vec_to_blob(np.ones(DIM, dtype=np.float32)), now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO notes (id, content, tags, importance, source, created_at, updated_at) "
            "VALUES ('n1', 'sailing notes', '[]', 0.5, 'user', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO entities "
            "(id, name, entity_type, embedding, metadata, mention_count, created_at, updated_at) "
            "VALUES ('ent1', 'sailing', 'thing', ?, '{}', 3, ?, ?)",
            (vec_to_blob(np.ones(STALE_DIM, dtype=np.float32)), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    yield s
    await s.aclose()


# ── search_all: isolate the tier, keep the answer ─────────────────────


async def test_search_all_survives_a_dead_entity_tier(store):
    """One stale table must not take out three healthy tiers."""
    results = await store.search_all("sailing", limit=5)
    tiers = {r.get("tier") for r in results}
    assert "episode" in tiers, "episode recall was collateral damage"
    assert results, "search_all returned nothing at all"


async def test_search_all_declares_the_degradation_it_survived(store):
    """Degrading quietly is the bug. The failure has to be readable.

    The dimension mismatch is handled one level down, inside the KG's
    vector leg, so ``search_all`` itself sees a successful (FTS-only)
    entity tier. The degradation is still published, through the field
    ``/internal/memory/stats`` turns into ``semantic_search: degraded``.
    """
    await store.search_all("sailing", limit=5)
    assert store._vector_leg_error, (
        "entity vector search died and nothing anywhere recorded it"
    )
    assert "dims" in store._vector_leg_error


async def test_a_tier_that_raises_is_recorded_and_the_rest_still_answer(store):
    """The aggregator's own contract, tested with a tier that raises all
    the way out: isolate it, record it, keep the other tiers."""

    async def _boom(*a, **kw):
        raise EmbeddingDimensionMismatch("query vector has 8 dims but stored vector has 24")

    store.episode_search_hybrid = _boom

    results = await store.search_all("sailing", limit=5)
    assert store.last_search_degradations, (
        "a tier failed and search_all reported a complete result set"
    )
    assert store.last_search_degradations[0]["tier"] == "episode"
    assert "EmbeddingDimensionMismatch" in store.last_search_degradations[0]["error"]
    assert {r.get("tier") for r in results} & {"note", "entity", "knowledge"}, (
        "one dead tier took out the others"
    )


async def test_the_error_names_the_command_that_fixes_it(store, caplog):
    with caplog.at_level("ERROR"):
        await store.search_all("sailing", limit=5)
    actionable = [r for r in caplog.records if "feral memory reembed" in r.getMessage()]
    assert actionable, (
        "no log line told the operator what to run; the messages were: "
        + " | ".join(r.getMessage()[:120] for r in caplog.records)
    )


async def test_search_all_propagates_when_every_tier_is_dead(store):
    """With nothing left to return, [] would be a lie about the store's
    contents rather than a reduced answer."""

    async def _boom(*a, **kw):
        raise EmbeddingDimensionMismatch("query vector has 8 dims but stored vector has 24")

    store.episode_search_hybrid = _boom
    store.search = _boom
    store.knowledge_search = _boom
    store._kg.search_entities = _boom

    with pytest.raises(RuntimeError) as excinfo:
        await store.search_all("sailing", limit=5)
    assert "every memory tier failed" in str(excinfo.value)


# ── the knowledge graph itself ────────────────────────────────────────


async def test_entity_search_still_answers_from_fts(store):
    """The text leg is unaffected by a dimension mismatch, so entity
    recall degrades rather than disappearing."""
    hits = await store._kg.search_entities("sailing", limit=5)
    assert [h["name"] for h in hits] == ["sailing"]


async def test_entity_search_publishes_its_dead_leg(store):
    await store._kg.search_entities("sailing", limit=5)
    assert store._kg.vector_leg_error
    assert "feral memory reembed" in store._kg.vector_leg_error or store._vector_leg_error
    assert store._vector_leg_error, "the KG failure never reached the store"


async def test_entity_ingest_is_not_blocked_by_a_stale_table(store):
    """``_link_entity`` runs inside ``add_entity``. Letting the mismatch
    propagate there would stop knowledge ingest entirely until somebody
    migrated the store."""
    created = await store._kg.add_entity("Ada Lovelace", "person")
    assert created["name"] == "Ada Lovelace"
    conn = sqlite3.connect(store.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE name = 'Ada Lovelace'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 1


# ── the three call sites the exception used to escape through ─────────


async def test_memory_search_rpc_still_answers(store, monkeypatch):
    """gateway/protocol.py: the RPC had no handler, so a stale entities
    table turned every device-side memory.search into a protocol error."""
    from gateway.protocol import MethodRegistry, register_core_methods

    class _State:
        memory = store

        def __getattr__(self, name):
            return None

    registry = MethodRegistry()
    register_core_methods(registry, _State())
    handler = registry.get("memory.search")
    result = await handler("sess-1", {"query": "sailing", "limit": 5}, None)
    assert result["results"], "memory.search RPC returned nothing"


async def test_taskflow_memory_search_step_still_completes(store, tmp_path):
    """agents/taskflow.py: the step is awaited inside the flow runner, so
    the mismatch failed the whole flow."""
    from agents.taskflow import TaskFlowRuntime

    runtime = TaskFlowRuntime(db_path=str(tmp_path / "flows.db"), memory_store=store)
    try:
        outcome = await runtime._execute_step(
            {"context": {}},
            {"step_type": "memory.search", "payload": {"query": "sailing", "limit": 5}},
        )
    finally:
        await runtime._http.aclose()
    assert outcome["status"] == "completed"
    assert outcome["results"]


async def test_knowledge_entities_endpoint_still_answers(store, monkeypatch):
    """api/routes/memory.py:559 catches Exception and returns
    ``{"entities": [], "error": ...}``: an error field nothing renders,
    over an empty list that reads as "no such entity"."""
    from api.routes import memory as memory_routes

    monkeypatch.setattr(memory_routes.state, "memory", store, raising=False)
    payload = await memory_routes.search_knowledge_entities(q="sailing", limit=5)
    assert payload.get("error") is None
    assert [e["name"] for e in payload["entities"]] == ["sailing"]


async def test_degraded_semantic_search_is_reported_by_the_stats_endpoint(store, monkeypatch):
    """The wire field operators actually look at."""
    from api.routes import memory as memory_routes

    await store.search_all("sailing", limit=5)
    monkeypatch.setattr(memory_routes.state, "memory", store, raising=False)
    health = memory_routes._semantic_health()
    assert health["semantic_search"] == "degraded"
    assert health["vector_leg_error"]
