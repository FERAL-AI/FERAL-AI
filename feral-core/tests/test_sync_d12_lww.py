"""PR 2 (v2026.5.34) D12 acceptance — HLC LWW at materialization,
stable node_id, duplicate-id rejection.

Pins the contract from the master plan: arrival order on the wire
must NOT decide the winner; the (wall_ms, counter, node_id) tuple
on the row's ``hlc_string`` column does.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from memory.hlc import HLCTimestamp
from memory.store import MemoryStore
from memory.sync import (
    SyncEngine,
    SyncOperation,
    _parse_hlc,
    stable_node_id,
)


@pytest.fixture
async def pair(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    wal_a = tmp_path / "a_wal.db"
    wal_b = tmp_path / "b_wal.db"
    store_a = MemoryStore(db_path=str(db_a))
    store_b = MemoryStore(db_path=str(db_b))
    engine_a = SyncEngine(node_id="node-a", memory_store=store_a, db_path=str(wal_a))
    engine_b = SyncEngine(node_id="node-b", memory_store=store_b, db_path=str(wal_b))
    store_a.set_sync_engine(engine_a)
    store_b.set_sync_engine(engine_b)
    try:
        yield engine_a, engine_b, store_a, store_b
    finally:
        await store_a.aclose()
        await store_b.aclose()


# ── 1. LWW skips a stale arrival ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_lww_skips_stale_arrival(pair):
    """An older HLC arriving after a newer one must NOT overwrite."""
    engine_a, engine_b, store_a, store_b = pair

    # B already has a note at HLC (2000, 0, node-b).
    new_hlc = HLCTimestamp(wall_ms=2_000, counter=0, node_id="node-b").to_string()
    conn = await store_b._conn()
    try:
        await conn.execute(
            "INSERT INTO notes (id, content, tags, importance, source, "
            "created_at, updated_at, hlc_string) "
            "VALUES (?, 'newer content', '[]', 'normal', 'local', ?, ?, ?)",
            ("note1", time.time(), time.time(), new_hlc),
        )
        await conn.commit()
    finally:
        await store_b._release(conn)

    # Now A sends an older op for the same row.
    stale_op = SyncOperation(
        op_id="op-stale",
        table="notes",
        op_type="insert",
        row_id="note1",
        data={
            "id": "note1", "content": "older content", "tags": "[]",
            "importance": "normal", "source": "node-a",
            "created_at": time.time(),
        },
        hlc=HLCTimestamp(wall_ms=1_000, counter=0, node_id="node-a").to_string(),
        origin_node="node-a",
    )

    applied = await engine_b.apply_remote_changes([stale_op.to_dict()])
    assert applied == 0, "stale arrival must not materialize"

    conn = await store_b._conn()
    try:
        async with conn.execute(
            "SELECT content, hlc_string FROM notes WHERE id = 'note1'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store_b._release(conn)
    assert row["content"] == "newer content"
    assert row["hlc_string"] == new_hlc


# ── 2. LWW applies a strictly newer arrival ─────────────────────────────


@pytest.mark.asyncio
async def test_lww_applies_newer_arrival(pair):
    engine_a, engine_b, store_a, store_b = pair

    old_hlc = HLCTimestamp(wall_ms=1_000, counter=0, node_id="node-b").to_string()
    conn = await store_b._conn()
    try:
        await conn.execute(
            "INSERT INTO notes (id, content, tags, importance, source, "
            "created_at, updated_at, hlc_string) "
            "VALUES (?, 'old content', '[]', 'normal', 'local', ?, ?, ?)",
            ("note2", time.time(), time.time(), old_hlc),
        )
        await conn.commit()
    finally:
        await store_b._release(conn)

    new_hlc = HLCTimestamp(wall_ms=2_000, counter=0, node_id="node-a").to_string()
    fresh_op = SyncOperation(
        op_id="op-fresh", table="notes", op_type="insert", row_id="note2",
        data={
            "id": "note2", "content": "new content", "tags": "[]",
            "importance": "normal", "source": "node-a",
            "created_at": time.time(),
        },
        hlc=new_hlc, origin_node="node-a",
    )

    applied = await engine_b.apply_remote_changes([fresh_op.to_dict()])
    assert applied == 1

    conn = await store_b._conn()
    try:
        async with conn.execute(
            "SELECT content, hlc_string FROM notes WHERE id = 'note2'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store_b._release(conn)
    assert row["content"] == "new content"
    assert row["hlc_string"] == new_hlc


# ── 3. Arrival order doesn't matter — newest HLC always wins ────────────


@pytest.mark.asyncio
async def test_arrival_order_independence(pair):
    """Apply newer-then-older AND older-then-newer; both must converge."""
    engine_a, engine_b, store_a, store_b = pair

    older_hlc = HLCTimestamp(wall_ms=1_000, counter=0, node_id="node-a").to_string()
    newer_hlc = HLCTimestamp(wall_ms=2_000, counter=0, node_id="node-a").to_string()

    older = SyncOperation(
        op_id="o1", table="notes", op_type="insert", row_id="raceA",
        data={"id": "raceA", "content": "v1"}, hlc=older_hlc, origin_node="node-a",
    )
    newer = SyncOperation(
        op_id="o2", table="notes", op_type="insert", row_id="raceA",
        data={"id": "raceA", "content": "v2"}, hlc=newer_hlc, origin_node="node-a",
    )

    # newer first, then older
    await engine_b.apply_remote_changes([newer.to_dict()])
    await engine_b.apply_remote_changes([older.to_dict()])

    conn = await store_b._conn()
    try:
        async with conn.execute(
            "SELECT content, hlc_string FROM notes WHERE id = 'raceA'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store_b._release(conn)
    assert row["content"] == "v2"
    assert row["hlc_string"] == newer_hlc


# ── 4. LWW also gates deletes ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_lww_gates_delete(pair):
    """A delete with an older HLC than the latest write must not erase the row."""
    engine_a, engine_b, store_a, store_b = pair

    newer_hlc = HLCTimestamp(wall_ms=2_000, counter=0, node_id="node-b").to_string()
    conn = await store_b._conn()
    try:
        await conn.execute(
            "INSERT INTO notes (id, content, tags, importance, source, "
            "created_at, updated_at, hlc_string) "
            "VALUES ('alive', 'survives', '[]', 'normal', 'local', ?, ?, ?)",
            (time.time(), time.time(), newer_hlc),
        )
        await conn.commit()
    finally:
        await store_b._release(conn)

    stale_delete = SyncOperation(
        op_id="dop", table="notes", op_type="delete", row_id="alive", data={},
        hlc=HLCTimestamp(wall_ms=1_000, counter=0, node_id="node-a").to_string(),
        origin_node="node-a",
    )
    applied = await engine_b.apply_remote_changes([stale_delete.to_dict()])
    assert applied == 0

    conn = await store_b._conn()
    try:
        async with conn.execute(
            "SELECT content FROM notes WHERE id = 'alive'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store_b._release(conn)
    assert row is not None
    assert row["content"] == "survives"


# ── 5. Unknown table rejected ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_table_rejected(pair):
    engine_a, engine_b, store_a, store_b = pair

    op = SyncOperation(
        op_id="evil", table="users_admin", op_type="delete", row_id="root",
        data={}, hlc=HLCTimestamp(wall_ms=5_000, counter=0, node_id="x").to_string(),
        origin_node="evil",
    )
    applied = await engine_b.apply_remote_changes([op.to_dict()])
    assert applied == 0


# ── 6. Stable node_id round-trips ───────────────────────────────────────


def test_stable_node_id_round_trip(tmp_path):
    """First call writes the id; second call reads it back unchanged."""
    id1 = stable_node_id(data_home=tmp_path)
    id2 = stable_node_id(data_home=tmp_path)
    assert id1 == id2

    # Sanity: the id looks UUID-v7-shaped (timestamp-ms-prefix + hex nonce).
    assert "-" in id1
    head, tail = id1.split("-", 1)
    assert head.isdigit(), f"expected ms prefix, got {head!r}"
    assert all(c in "0123456789abcdef" for c in tail), f"expected hex nonce, got {tail!r}"


def test_stable_node_id_changes_per_brain(tmp_path):
    """Two separate data homes get distinct ids."""
    home_a = tmp_path / "brainA"
    home_b = tmp_path / "brainB"
    id_a = stable_node_id(data_home=home_a)
    id_b = stable_node_id(data_home=home_b)
    assert id_a != id_b


# ── 7. Local writes persist hlc_string ──────────────────────────────────


@pytest.mark.asyncio
async def test_local_write_persists_hlc(pair):
    """A local episode_save must populate hlc_string so the receiving
    side has a comparator when this op replicates over."""
    engine_a, engine_b, store_a, store_b = pair

    await store_a.episode_save(
        session_id="s1", event_type="x", summary="hello a", importance=0.5,
    )

    conn = await store_a._conn()
    try:
        async with conn.execute(
            "SELECT hlc_string FROM episodes WHERE summary = 'hello a'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store_a._release(conn)

    assert row is not None
    assert row["hlc_string"], "episode_save did not persist hlc_string"
    parsed = _parse_hlc(row["hlc_string"])
    assert parsed[0] > 0
    assert parsed[2] == "node-a"


# ── 8. End-to-end convergence + idempotent re-apply ─────────────────────


@pytest.mark.asyncio
async def test_end_to_end_two_brain_convergence(pair):
    """Two brains, conflicting writes on the same key — both converge
    to the same (newer-HLC) value."""
    engine_a, engine_b, store_a, store_b = pair

    # A writes first.
    await store_a.knowledge_store("user", "color", "blue")
    # B's write happens slightly later — its HLC will be larger.
    await asyncio.sleep(0.01)
    await store_b.knowledge_store("user", "color", "green")

    ops_a = engine_a.get_changes_since("")
    ops_b = engine_b.get_changes_since("")

    await engine_b.apply_remote_changes(ops_a)
    await engine_a.apply_remote_changes(ops_b)

    rows_a = await store_a.knowledge_query(subject="user", predicate="color")
    rows_b = await store_b.knowledge_query(subject="user", predicate="color")
    assert len(rows_a) == 1
    assert len(rows_b) == 1
    assert rows_a[0]["object"] == rows_b[0]["object"], (
        f"divergence: a={rows_a[0]['object']!r} b={rows_b[0]['object']!r}"
    )
    # B's write was later → green should be the survivor.
    assert rows_a[0]["object"] == "green"

    # Idempotency: re-applying must not change anything.
    again_a = await engine_a.apply_remote_changes(ops_b)
    again_b = await engine_b.apply_remote_changes(ops_a)
    # Re-applies should report 0 (every row already at the right HLC).
    assert again_a == 0
    assert again_b == 0
