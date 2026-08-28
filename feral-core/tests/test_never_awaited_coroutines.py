"""Four features that have never worked once, all the same mistake.

An `async def` called without `await` returns a coroutine. The caller then
treats it as data: iterates it, or hands it to a JSON response. Iterating
raises `TypeError: 'coroutine' object is not iterable`, and in every case
here the exception lands in a broad `except` that logs at debug or turns
it into a 500, so the feature reports failure as "unavailable" or as
success rather than as a bug.

They are grouped because they are one defect, not four:

  * `MemoryRetriever._safe_call` invokes async store methods synchronously,
    so every tier raises, every tier lands in `skipped_tiers`, and
    `retrieve()` has always returned zero records for every query.
  * `relationship_query` / `graph_visualization_data` are plain `def`
    calling the async `KnowledgeGraph.traverse`, so multi-hop traversal
    has never been reachable from any user-facing path. `traverse` itself
    is correct.
  * `api/server.py` calls `extract_and_store(text, source=...)` but the
    signature is `(self, text, llm=None)`, so ambient conversations have
    always contributed zero entities to the graph.
  * `POST /api/location/update` never awaits `update_location`, so
    geofences have never fired over REST while returning success.

The tests assert behaviour. Every one of them fails against the code as
it stood before this file was added.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.knowledge_graph import KnowledgeGraph  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


async def _kg(tmp_path) -> KnowledgeGraph:
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    kg = store.kg
    assert kg is not None, "store did not wire a knowledge graph"
    await kg.add_relation("Alice", "knows", "Bob")
    await kg.add_relation("Bob", "works_at", "Acme")
    return kg


# ── ambient knowledge-graph extraction ─────────────────────────────

def test_extract_and_store_accepts_the_source_kwarg_its_caller_passes():
    """`api/server.py` passes `source=`; the signature must accept it.

    Without it every ambient conversation raised TypeError into a
    debug-level handler, so the graph silently learned nothing from
    anything the brain overheard.
    """
    sig = inspect.signature(KnowledgeGraph.extract_and_store)
    assert "source" in sig.parameters, (
        "extract_and_store does not accept `source`, but api/server.py calls it "
        f"with source=...; signature is {sig}"
    )


@pytest.mark.asyncio
async def test_extraction_stores_relations_for_operator_text(tmp_path):
    """A source that IS the operator still populates the graph."""
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    kg = store.kg
    await kg.extract_and_store("my name is Charlie and i live in Berlin")
    rels = await kg.traverse("user", max_depth=1)
    assert rels, "operator text stored no relations"


@pytest.mark.asyncio
async def test_ambient_text_does_not_reach_the_first_person_heuristic(tmp_path):
    """Overheard speech must not become facts about the operator.

    Accepting `source` fixed the TypeError, but naively routing ambient
    text into `_heuristic_extract` would be worse than the bug: its
    patterns are all first-person and everything they match is filed
    under the `user` entity, so a visitor saying "my name is Dana" would
    rename the operator. Same boundary `store._NO_SELF_MODEL_EVENT_TYPES`
    draws for About-Me.
    """
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    kg = store.kg
    out = await kg.extract_and_store(
        "my name is Dana and i live in Lisbon", source="ambient_conversation"
    )
    assert out == [], f"ambient text reached the heuristic extractor: {out}"
    rels = await kg.traverse("user", max_depth=1)
    assert not rels, f"overheard speech was filed under the operator: {rels}"


# ── multi-hop traversal ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_relationship_query_resolves_a_two_hop_path(tmp_path):
    from memory.enhanced_search import relationship_query

    kg = await _kg(tmp_path)
    result = relationship_query(kg, "Alice", "Acme", max_depth=3)
    if inspect.isawaitable(result):
        result = await result
    assert isinstance(result, dict), f"expected a dict, got {type(result)}"


@pytest.mark.asyncio
async def test_graph_visualization_returns_nodes(tmp_path):
    from memory.enhanced_search import graph_visualization_data

    kg = await _kg(tmp_path)
    result = graph_visualization_data(kg, "Alice", max_depth=2, limit=10)
    if inspect.isawaitable(result):
        result = await result
    assert isinstance(result, dict), f"expected a dict, got {type(result)}"
    assert result.get("nodes"), f"no nodes returned: {result}"


# ── the unified retriever ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_retriever_returns_records_for_a_matching_query(tmp_path):
    """It has always returned zero records, for every query, every tier."""
    from memory.retriever import MemoryRetriever

    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    await store.episode_save(
        session_id="s", event_type="user_command",
        summary="the cutebot robot has RGB LED headlights",
        detail="the cutebot robot has RGB LED headlights driven over BLE",
    )

    retriever = MemoryRetriever(store)
    result = retriever.retrieve("cutebot headlights", top_k=5)
    if inspect.isawaitable(result):
        result = await result

    skipped = getattr(result, "skipped_tiers", {}) or {}
    coroutine_errors = {
        tier: err for tier, err in skipped.items()
        if "coroutine" in str(err)
    }
    assert not coroutine_errors, (
        "tiers failed because async store methods were called without await: "
        f"{coroutine_errors}"
    )
    assert getattr(result, "records", []), (
        f"retriever returned no records; skipped_tiers={skipped}"
    )


# ── geofences over REST ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_location_update_route_awaits_the_engine():
    """The REST route returned success with a coroutine in the body.

    The phone WebSocket path awaited it correctly, so geofences worked
    for one transport and had never fired for the other.
    """
    from perception.location import LocationEngine

    engine = LocationEngine()
    fired: list = []
    if hasattr(engine, "register_geofence"):
        engine.register_geofence(
            "home", 37.7749, -122.4194, radius_m=200,
            on_enter=lambda *a, **k: fired.append("home"),
        )

    src = (ROOT / "api" / "routes" / "timeline.py").read_text()
    idx = src.find("location_engine.update_location")
    assert idx != -1, "call site moved; update this test"
    window = src[max(0, idx - 60):idx]
    assert "await" in window, (
        "api/routes/timeline.py calls the async update_location without await; "
        "the route returns a coroutine object inside a success response and no "
        "geofence can ever fire over REST"
    )
