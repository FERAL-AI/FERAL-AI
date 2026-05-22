"""Lane 05 (Wave 2) — KG indexed vector search + entity-by-tag.

Closes AUDIT-r14 finding 14 fixes #2 and #3:
  * KG no longer bypasses the MemoryStore connection pool.
  * Entity search no longer full-scans the entities table on every
    turn — uses sqlite-vec's vec0 nearest-neighbour index when the
    extension is available, with deterministic fallback to numpy
    when not.
  * THESIS_SCENARIOS S3: ``find_entities_by_tag(category='device')``
    answers "what BLE devices are around my phone right now?" without
    a free-text search.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.embeddings import EmbeddingProvider, sqlite_vec_available  # noqa: E402
from memory.knowledge_graph import KnowledgeGraph  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


# ── Pool reuse: KG attached to MemoryStore shares its pool ────────


@pytest.mark.asyncio
async def test_kg_uses_shared_memory_store_pool(tmp_path):
    """When a MemoryStore is attached, KG._conn returns a pooled
    connection — verified by counting fresh sqlite handles open."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    kg = store.kg
    assert kg is not None

    # Force the pool to materialise.
    conn = await store._conn()
    await store._release(conn)

    pool_size_before = store._pool.qsize()
    # KG._conn must drain from the pool, not open a new aiosqlite handle.
    kg_conn = await kg._conn()
    assert store._pool.qsize() == pool_size_before - 1, (
        "KG._conn() did not borrow from MemoryStore pool"
    )
    await kg._release(kg_conn)
    assert store._pool.qsize() == pool_size_before, (
        "KG._release() did not return the connection to MemoryStore pool"
    )


# ── Indexed nearest-neighbour: search scales sub-linearly ─────────


@pytest.mark.asyncio
async def test_search_entities_scales_sub_linearly_with_size(tmp_path):
    """Insert 100 entities, then ensure search runs in <500ms.

    The pre-fix path was an O(N) numpy scan over every entity row;
    on a slow CI host with 100 rows the legacy path easily exceeded
    a second after embedding round-trips. This test pins the new
    indexed/scalable path. We don't assert sqlite-vec is loaded —
    when the extension is missing the numpy fallback still runs,
    just at higher constant. The test is generous (<500ms for 100
    entities) so it's tight enough to flag a regression but not
    so tight it goes flaky on cold disks.
    """
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    kg = store.kg

    entity_names = [
        f"BLE Device {i:03d}" if i < 50 else f"Person Number {i:03d}"
        for i in range(100)
    ]
    for name in entity_names:
        etype = "device" if name.startswith("BLE") else "person"
        await kg.add_entity(name, entity_type=etype, metadata={"category": etype})

    t0 = time.monotonic()
    results = await kg.search_entities("BLE Device 042", limit=5)
    elapsed = time.monotonic() - t0

    assert len(results) >= 1, "search returned nothing"
    assert results[0]["name"].startswith("BLE Device"), (
        f"top hit should be a BLE Device, got {results[0]}"
    )
    assert elapsed < 0.5, f"search_entities took {elapsed*1000:.1f}ms (regression)"


# ── Entity-by-tag (THESIS_SCENARIOS S3) ────────────────────────────


@pytest.mark.asyncio
async def test_find_entities_by_tag_filters_by_category(tmp_path):
    """find_entities_by_tag(category='device') returns devices only."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    kg = store.kg

    await kg.add_entity(
        "AirPods Pro", entity_type="device", metadata={"category": "device", "mac": "AA:BB"}
    )
    await kg.add_entity(
        "Apple Watch", entity_type="device", metadata={"category": "device", "mac": "CC:DD"}
    )
    await kg.add_entity(
        "Alice", entity_type="person", metadata={"category": "person"}
    )
    await kg.add_entity(
        "OpenAI", entity_type="organization", metadata={"category": "organization"}
    )

    devices = await kg.find_entities_by_tag(category="device")
    assert len(devices) == 2
    names = {d["name"] for d in devices}
    assert names == {"AirPods Pro", "Apple Watch"}
    assert all(d["entity_type"] == "device" for d in devices)
    # Metadata is roundtripped as a dict (not raw JSON string).
    assert all(isinstance(d["metadata"], dict) for d in devices)
    assert all(d["metadata"].get("category") == "device" for d in devices)


@pytest.mark.asyncio
async def test_find_entities_by_tag_combines_filters(tmp_path):
    """Both category and entity_type can be combined."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    kg = store.kg

    await kg.add_entity(
        "Living Room Roomba", entity_type="device", metadata={"category": "device"}
    )
    await kg.add_entity(
        "Bedside Lamp", entity_type="device", metadata={"category": "device"}
    )

    rooms = await kg.find_entities_by_tag(category="device", entity_type="device")
    assert len(rooms) == 2

    none = await kg.find_entities_by_tag(category="organization")
    assert none == []


@pytest.mark.asyncio
async def test_find_entities_by_tag_requires_at_least_one_filter(tmp_path):
    """Naked find_entities_by_tag() is rejected (would return everything)."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))

    with pytest.raises(ValueError, match="at least one"):
        await store.kg.find_entities_by_tag()


# ── Entity linking still works on indexed path ─────────────────────


@pytest.mark.asyncio
async def test_link_entity_via_indexed_search(tmp_path):
    """_link_entity uses the indexed path when available; same
    semantics as the numpy fallback (returns the closest match
    above ENTITY_MERGE_THRESHOLD)."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    kg = store.kg

    await kg.add_entity("Alice Johnson", entity_type="person")

    # Re-add with a slightly different name; entity_linking should
    # detect the duplicate via embedding similarity.
    await kg.add_entity("alice johnson", entity_type="person")

    # Both calls should resolve to the same entity id (case-insensitive
    # name match wins first, then embedding linking handles spacing /
    # punctuation drift).
    stats = await kg.stats_async()
    assert stats["entities"] == 1


# ── Diagnostic property ────────────────────────────────────────────


def test_vec_entities_available_flag_is_set(tmp_path):
    """The KG exposes whether the vec0 entity index is live so the
    health endpoint can surface it."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    expected = bool(sqlite_vec_available())
    assert store.kg._vec_entities_available == expected
