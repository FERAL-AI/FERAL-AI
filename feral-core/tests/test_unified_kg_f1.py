"""PR 2.5 (v2026.5.35) F1 acceptance — unified knowledge graph.

Twelve tests that pin the contract from the master plan:

  1. ``knowledge_store`` routes writes through the KG when
     ``settings.memory.kg.unified`` is true (default). Entities and
     relations land in the KG tables; the relation gets a scalar id
     derived from ``(source_id, predicate)`` so the upsert is real.
  2. Two writes to the same ``(subject, predicate)`` with different
     objects collapse to a single row — preserves flat-triple upsert
     across the unified surface.
  3. ``knowledge_query(subject, predicate)`` reads from the unified
     KG JOIN view and returns triple-shaped dicts identical to the
     legacy flat-table API.
  4. ``knowledge_search(query)`` uses ``entities_fts`` and returns
     relations connected to matching entities.
  5. ``knowledge_about(entity)`` returns every relation where the
     entity is the source or the target.
  6. ``migrate_knowledge_to_kg`` ports legacy flat rows into the KG
     idempotently — running twice doesn't duplicate.
  7. After migration, the flat ``knowledge`` table is renamed to
     ``knowledge__deprecated`` so the legacy reader paths see no
     rows (only fires when every row has been ported).
  8. The migration is incremental — pre-existing rows with
     ``kg_migrated_at != 0`` are skipped on subsequent runs.
  9. Wiki compiler reads from the KG view when unified is on, so
     ``wiki_compile`` sees the same knowledge surface the rest of
     the brain does.
 10. Flipping ``settings.memory.kg.unified`` to false short-circuits
     to the legacy flat path (chaos/rollback safety net).
 11. KG-native writes via ``kg.add_relation`` preserve multi-target
     semantics — only the ``knowledge_store`` bridge enforces
     scalar upsert.
 12. Two-brain convergence on a scalar predicate — the deterministic
     relation id means both brains pick the strictly-newer HLC
     write without any extra coordination.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from memory.knowledge_graph import _stable_kg_id
from memory.store import MemoryStore
from memory.sync import SyncEngine


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "f1.db"))
    s.set_sync_engine(SyncEngine(
        node_id=f"node-{uuid.uuid4().hex[:6]}",
        memory_store=s,
        db_path=str(tmp_path / "f1.wal"),
    ))
    try:
        yield s
    finally:
        await s.aclose()


# ── 1. knowledge_store routes through KG ─────────────────────────────


@pytest.mark.asyncio
async def test_knowledge_store_routes_through_kg(store):
    result = await store.knowledge_store("user", "name_is", "Alice")
    assert result["subject"] == "user"
    assert result["object"] == "Alice"

    conn = await store._conn()
    try:
        async with conn.execute("SELECT COUNT(*) FROM entities") as cur:
            entity_count = (await cur.fetchone())[0]
        async with conn.execute("SELECT COUNT(*) FROM relations") as cur:
            rel_count = (await cur.fetchone())[0]
    finally:
        await store._release(conn)

    assert entity_count >= 2, "user + Alice entities must land in entities table"
    assert rel_count == 1, "one relation must land in relations table"


# ── 2. flat upsert preserved under the unified path ──────────────────


@pytest.mark.asyncio
async def test_unified_path_preserves_flat_upsert_semantic(store):
    await store.knowledge_store("user", "favorite_color", "blue")
    await store.knowledge_store("user", "favorite_color", "green")
    results = await store.knowledge_query(subject="user", predicate="favorite_color")
    assert len(results) == 1
    assert results[0]["object"] == "green"


# ── 3. knowledge_query returns triple-shape from KG ──────────────────


@pytest.mark.asyncio
async def test_knowledge_query_returns_triple_shape(store):
    await store.knowledge_store("user", "lives_in", "San Francisco")
    results = await store.knowledge_query(subject="user")
    assert len(results) == 1
    row = results[0]
    assert {"id", "subject", "predicate", "object", "confidence", "updated_at"}.issubset(row.keys())
    assert row["subject"] == "user"
    assert row["object"] == "San Francisco"


# ── 4. knowledge_search via entities_fts ─────────────────────────────


@pytest.mark.asyncio
async def test_knowledge_search_via_entities_fts(store):
    await store.knowledge_store("user", "lives_in", "San Francisco")
    await store.knowledge_store("user", "works_at", "FERAL")
    hits = await store.knowledge_search("San Francisco")
    assert any(h["object"] == "San Francisco" for h in hits)


# ── 5. knowledge_about surfaces source AND target rows ───────────────


@pytest.mark.asyncio
async def test_knowledge_about_source_or_target(store):
    await store.knowledge_store("user", "likes", "coffee")
    await store.knowledge_store("user", "dislikes", "spam")
    await store.knowledge_store("Alice", "knows", "user")
    about = await store.knowledge_about("user")
    # 3 relations touch "user": 2 as source, 1 as target.
    assert len(about) == 3


# ── 6. migration is idempotent ───────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    # Build a store with the unified flag OFF so we can seed the flat
    # table directly, then re-open with the flag ON to drive the
    # migration code path.
    import config.loader as cfg
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"memory": {"kg": {"unified": False}}}))
    monkey_home = tmp_path / "home"
    monkey_home.mkdir()
    (monkey_home / "settings.json").write_text(settings_path.read_text())

    original_feral_home = cfg.feral_home
    cfg.feral_home = lambda: monkey_home
    cfg.load_settings.cache_clear() if hasattr(cfg.load_settings, "cache_clear") else None

    try:
        s = MemoryStore(db_path=str(tmp_path / "mig.db"))
        s.set_sync_engine(SyncEngine(
            node_id="n1", memory_store=s, db_path=str(tmp_path / "mig.wal"),
        ))
        for i in range(5):
            await s.knowledge_store(f"s{i}", "rel", f"o{i}")
        # All 5 rows should be in the flat table now (unified=false).
        conn = await s._conn()
        try:
            async with conn.execute("SELECT COUNT(*) FROM knowledge") as cur:
                flat_count = (await cur.fetchone())[0]
        finally:
            await s._release(conn)
        assert flat_count == 5

        # Flip unified ON.
        (monkey_home / "settings.json").write_text(
            json.dumps({"memory": {"kg": {"unified": True}}})
        )
        cfg.load_settings.cache_clear() if hasattr(cfg.load_settings, "cache_clear") else None

        # Migrate.
        r1 = await s.migrate_knowledge_to_kg()
        assert r1["ported"] == 5

        # Idempotent — second run ports 0.
        r2 = await s.migrate_knowledge_to_kg()
        assert r2["ported"] == 0
        await s.aclose()
    finally:
        cfg.feral_home = original_feral_home
        cfg.load_settings.cache_clear() if hasattr(cfg.load_settings, "cache_clear") else None


# ── 7. flat table renamed to knowledge__deprecated after full port ──


@pytest.mark.asyncio
async def test_flat_table_renamed_after_full_port(tmp_path):
    import config.loader as cfg
    monkey_home = tmp_path / "home"
    monkey_home.mkdir()
    (monkey_home / "settings.json").write_text(
        json.dumps({"memory": {"kg": {"unified": False}}})
    )
    original_feral_home = cfg.feral_home
    cfg.feral_home = lambda: monkey_home
    cfg.load_settings.cache_clear() if hasattr(cfg.load_settings, "cache_clear") else None

    try:
        s = MemoryStore(db_path=str(tmp_path / "dep.db"))
        s.set_sync_engine(SyncEngine(
            node_id="n1", memory_store=s, db_path=str(tmp_path / "dep.wal"),
        ))
        await s.knowledge_store("a", "b", "c")

        # Flip unified ON before migration so the rename fires.
        (monkey_home / "settings.json").write_text(
            json.dumps({"memory": {"kg": {"unified": True}}})
        )
        cfg.load_settings.cache_clear() if hasattr(cfg.load_settings, "cache_clear") else None

        result = await s.migrate_knowledge_to_kg()
        assert result["deprecated"] is True

        conn = await s._conn()
        try:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge__deprecated'"
            ) as cur:
                row = await cur.fetchone()
        finally:
            await s._release(conn)
        assert row is not None
        await s.aclose()
    finally:
        cfg.feral_home = original_feral_home
        cfg.load_settings.cache_clear() if hasattr(cfg.load_settings, "cache_clear") else None


# ── 8. unified=false short-circuits to flat ──────────────────────────


@pytest.mark.asyncio
async def test_unified_false_uses_flat_path(tmp_path):
    import config.loader as cfg
    monkey_home = tmp_path / "home"
    monkey_home.mkdir()
    (monkey_home / "settings.json").write_text(
        json.dumps({"memory": {"kg": {"unified": False}}})
    )
    original_feral_home = cfg.feral_home
    cfg.feral_home = lambda: monkey_home
    cfg.load_settings.cache_clear() if hasattr(cfg.load_settings, "cache_clear") else None

    try:
        s = MemoryStore(db_path=str(tmp_path / "flat.db"))
        s.set_sync_engine(SyncEngine(
            node_id="n1", memory_store=s, db_path=str(tmp_path / "flat.wal"),
        ))
        assert s._kg_unified_enabled() is False

        await s.knowledge_store("user", "name", "Alice")
        # The write must land in the FLAT table when unified is off.
        conn = await s._conn()
        try:
            async with conn.execute("SELECT COUNT(*) FROM knowledge") as cur:
                flat_count = (await cur.fetchone())[0]
            async with conn.execute("SELECT COUNT(*) FROM relations") as cur:
                rel_count = (await cur.fetchone())[0]
        finally:
            await s._release(conn)
        assert flat_count == 1
        assert rel_count == 0
        await s.aclose()
    finally:
        cfg.feral_home = original_feral_home
        cfg.load_settings.cache_clear() if hasattr(cfg.load_settings, "cache_clear") else None


# ── 9. KG-native writes keep multi-target semantic ───────────────────


@pytest.mark.asyncio
async def test_kg_native_writes_preserve_multi_target(store):
    """``kg.add_relation`` directly is multi-target by design — only
    the ``knowledge_store`` bridge enforces scalar upsert. Two
    distinct ``works_at`` relations on the same source should
    coexist."""
    await store.kg.add_relation("user", "works_at", "FERAL")
    await store.kg.add_relation("user", "works_at", "Stripe")

    conn = await store._conn()
    try:
        async with conn.execute(
            """SELECT COUNT(*) FROM relations r
               JOIN entities e ON r.source_id = e.id
               WHERE e.name = 'user' AND r.relation_type = 'works_at'"""
        ) as cur:
            count = (await cur.fetchone())[0]
    finally:
        await store._release(conn)
    assert count == 2, "KG-native writes must keep multi-target semantic"


# ── 10. two-brain convergence on a scalar predicate ──────────────────


@pytest.mark.asyncio
async def test_two_brain_convergence_scalar_predicate(tmp_path):
    """The bridge writes via a scalar relation id derived from
    ``(source_id, predicate)`` only — two brains writing the same
    scalar predicate at different HLC values get the SAME relation
    id without coordinating, so LWW resolves it under D12.
    """
    store_a = MemoryStore(db_path=str(tmp_path / "a.db"))
    store_b = MemoryStore(db_path=str(tmp_path / "b.db"))
    sync_a = SyncEngine(node_id="a", memory_store=store_a, db_path=str(tmp_path / "a.wal"))
    sync_b = SyncEngine(node_id="b", memory_store=store_b, db_path=str(tmp_path / "b.wal"))
    store_a.set_sync_engine(sync_a)
    store_b.set_sync_engine(sync_b)

    try:
        await store_a.knowledge_store("user", "color", "blue")
        # Advance wall clock by 50ms so the HLC tuples cleanly order.
        import asyncio
        await asyncio.sleep(0.05)
        await store_b.knowledge_store("user", "color", "green")

        # Cross-sync: apply each peer's WAL to the other.
        a_ops = sync_a.get_changes_since("0:0:")
        b_ops = sync_b.get_changes_since("0:0:")
        await sync_a.apply_remote_changes(b_ops)
        await sync_b.apply_remote_changes(a_ops)

        rows_a = await store_a.knowledge_query(subject="user", predicate="color")
        rows_b = await store_b.knowledge_query(subject="user", predicate="color")
        assert len(rows_a) == 1
        assert len(rows_b) == 1
        assert rows_a[0]["object"] == rows_b[0]["object"], (
            f"divergence: a={rows_a[0]['object']!r} b={rows_b[0]['object']!r}"
        )
        # B's write was later in wall-clock time, so both must end up green.
        assert rows_a[0]["object"] == "green"
    finally:
        await store_a.aclose()
        await store_b.aclose()


# ── 11. stable_kg_id is deterministic ────────────────────────────────


def test_stable_kg_id_is_deterministic():
    a = _stable_kg_id("user", "thing")
    b = _stable_kg_id("user", "thing")
    c = _stable_kg_id("user", "person")
    assert a == b
    assert a != c


# ── 12. wiki_compile reads from KG view when unified is on ──────────


@pytest.mark.asyncio
async def test_wiki_compile_reads_from_kg(store):
    """``wiki_compile`` writes one wiki page per knowledge triple.
    Under F1 the source rows come from the unified KG, not the
    legacy flat table — so a triple written through
    ``knowledge_store`` (which goes to KG) must appear in
    ``wiki_pages`` after compile."""
    from memory.wiki import wiki_compile

    await store.knowledge_store("user", "loves", "espresso")
    summary = await wiki_compile(store)
    # The compiler groups triples by subject and emits one
    # ``entity`` page per subject. With one (user, loves, espresso)
    # triple landing through the unified KG, ``entity_pages`` must
    # be at least one — proves the JOIN in wiki.py picked the row
    # up from ``relations`` × ``entities`` instead of the empty
    # flat table.
    assert summary.get("entity_pages", 0) >= 1, (
        f"wiki_compile saw no KG rows: {summary}"
    )
    # And verify the page actually exists in the wiki store.
    pages = await store.wiki_list_pages(kind="entity", limit=10)
    assert any("user" in (p.get("title") or "").lower() for p in pages), (
        f"no 'user' entity page in wiki: {pages}"
    )
