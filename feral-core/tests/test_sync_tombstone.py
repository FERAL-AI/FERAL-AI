"""Deleted rows must stay deleted: tombstone semantics for federated sync.

Regression pin for the resurrection defect: ``_apply_to_memory``'s
delete branch used to run a bare ``DELETE FROM {table} WHERE id = ?``
and record nothing. The LWW gate one block above it is written as::

    if existing_row is not None and remote_tuple <= existing_tuple:
        return False

so once the row is gone, ``existing_row`` is ``None``, the gate is
skipped entirely, and ANY later-arriving insert for that id, however
old its HLC, is materialised. A delete therefore survived only until
the next peer replayed the original insert, which is exactly what
``get_changes_since`` does on every handshake: it selects on HLC alone
and a peer that never saw the delete still holds the insert forever.

The fuzz suite could not see this: ``tests/test_sync_fuzz.py``
asserts convergence over ``_final_state``, which is derived from the
sync WAL op log, never from the materialised ``notes`` / ``knowledge``
tables. Both nodes converge on the same set of WAL ops no matter what
happened to the rows, so the assertions passed while the rows were
wrong.
"""

from __future__ import annotations

import time

import pytest

from memory.hlc import HLCTimestamp
from memory.store import MemoryStore
from memory.sync import SyncEngine, SyncOperation


def _hlc(wall_ms: int, node: str = "node-a", counter: int = 0) -> str:
    return HLCTimestamp(wall_ms=wall_ms, counter=counter, node_id=node).to_string()


def _insert_op(row_id: str, content: str, hlc: str, op_id: str) -> dict:
    return SyncOperation(
        op_id=op_id,
        table="notes",
        op_type="insert",
        row_id=row_id,
        data={
            "id": row_id,
            "content": content,
            "tags": "[]",
            "importance": "normal",
            "source": "node-a",
            "created_at": time.time(),
        },
        hlc=hlc,
        origin_node="node-a",
    ).to_dict()


def _delete_op(row_id: str, hlc: str, op_id: str, origin: str = "node-b") -> dict:
    return SyncOperation(
        op_id=op_id,
        table="notes",
        op_type="delete",
        row_id=row_id,
        data={"id": row_id},
        hlc=hlc,
        origin_node=origin,
    ).to_dict()


async def _note_count(store: MemoryStore, row_id: str) -> int:
    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM notes WHERE id = ?", (row_id,)
        ) as cur:
            return (await cur.fetchone())["n"]
    finally:
        await store._release(conn)


@pytest.fixture
async def engine(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "mem.db"))
    eng = SyncEngine(
        node_id="node-local",
        memory_store=store,
        db_path=str(tmp_path / "wal.db"),
    )
    store.set_sync_engine(eng)
    try:
        yield eng, store
    finally:
        await store.aclose()


# ── 1. The core defect ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_insert_after_delete_does_not_resurrect(engine):
    """insert(t=1000) → delete(t=2000) → insert(t=1000) replayed.

    The replayed insert is strictly older than the delete, so the row
    must stay gone. Pre-fix this re-inserted the note.
    """
    eng, store = engine

    ins = _insert_op("note-1", "secret", _hlc(1_000), "op-ins")
    await eng.apply_remote_changes([ins])
    assert await _note_count(store, "note-1") == 1

    await eng.apply_remote_changes([_delete_op("note-1", _hlc(2_000), "op-del")])
    assert await _note_count(store, "note-1") == 0

    # A peer that never saw the delete replays the original insert.
    applied = await eng.apply_remote_changes([ins])

    assert await _note_count(store, "note-1") == 0, (
        "deleted note was resurrected by a stale insert replay"
    )
    assert applied == 0, "stale insert against a tombstone must not count as applied"


# ── 2. Same defect via a realistic 3-node topology ──────────────────────


@pytest.mark.asyncio
async def test_three_node_delete_survives_stale_peer_replay(tmp_path):
    """A writes, B deletes, C (offline during the delete) re-sends A's
    insert to B. B must not resurrect the row.
    """
    stores = {}
    engines = {}
    for name in ("a", "b", "c"):
        st = MemoryStore(db_path=str(tmp_path / f"{name}.db"))
        en = SyncEngine(
            node_id=f"node-{name}", memory_store=st, db_path=str(tmp_path / f"{name}_wal.db")
        )
        st.set_sync_engine(en)
        stores[name] = st
        engines[name] = en

    try:
        # A creates the note and ships it to B and C.
        ins = _insert_op("shared", "private thought", _hlc(1_000), "op-a-ins")
        engines["a"]._wal.append(SyncOperation.from_dict(ins))
        await engines["b"].apply_remote_changes([ins])
        await engines["c"].apply_remote_changes([ins])
        assert await _note_count(stores["b"], "shared") == 1
        assert await _note_count(stores["c"], "shared") == 1

        # B deletes it. C is offline and never hears about it.
        dele = _delete_op("shared", _hlc(2_000, "node-b"), "op-b-del")
        await engines["b"].apply_remote_changes([dele])
        assert await _note_count(stores["b"], "shared") == 0

        # C comes back and replays everything it has, which still
        # includes A's original insert.
        c_ops = engines["c"].get_changes_since("0:0:")
        assert any(o["op_type"] == "insert" and o["row_id"] == "shared" for o in c_ops)
        await engines["b"].apply_remote_changes(c_ops)

        assert await _note_count(stores["b"], "shared") == 0, (
            "B resurrected a note it deleted, from a peer that never saw the delete"
        )
    finally:
        for st in stores.values():
            await st.aclose()


# ── 3. A locally-deleted row must also stay deleted ─────────────────────


@pytest.mark.asyncio
async def test_locally_deleted_note_is_not_resurrected_by_peer(engine):
    """The local delete path (``notes_legacy.delete_note``) must record a
    tombstone too, otherwise a peer replay resurrects it on this node.
    """
    eng, store = engine
    from memory.notes_legacy import save_note, delete_note

    note = await save_note(store, "local secret", tags=[])
    note_id = note["id"] if isinstance(note, dict) else note
    assert await _note_count(store, note_id) == 1

    assert await delete_note(store, note_id) is True
    assert await _note_count(store, note_id) == 0

    # A peer replays its copy of the insert, minted before the delete.
    stale = _insert_op(note_id, "local secret", _hlc(1), "op-peer-replay")
    await eng.apply_remote_changes([stale])

    assert await _note_count(store, note_id) == 0, (
        "a locally deleted note was resurrected by a peer replay"
    )


# ── 4. Guard against over-fixing: a genuine re-create must land ─────────


@pytest.mark.asyncio
async def test_newer_insert_after_delete_still_applies(engine):
    """A tombstone is not a permanent ban. An insert with an HLC newer
    than the delete is a legitimate re-creation and must materialise.
    """
    eng, store = engine

    await eng.apply_remote_changes([_insert_op("note-2", "v1", _hlc(1_000), "i1")])
    await eng.apply_remote_changes([_delete_op("note-2", _hlc(2_000), "d1")])
    assert await _note_count(store, "note-2") == 0

    applied = await eng.apply_remote_changes(
        [_insert_op("note-2", "v2", _hlc(3_000), "i2")]
    )
    assert applied == 1
    assert await _note_count(store, "note-2") == 1

    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT content FROM notes WHERE id = 'note-2'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store._release(conn)
    assert row["content"] == "v2"

    # ...and the now-superseded tombstone must be gone, so a second
    # re-create does not have to fight a stale gate.
    tombstones = await store.tombstone_count()
    assert tombstones == 0, "tombstone must be cleared once the row is re-created"


# ── 5. Delete LWW still holds: a stale delete cannot erase a newer row ──


@pytest.mark.asyncio
async def test_stale_delete_does_not_erase_newer_row(engine):
    eng, store = engine

    await eng.apply_remote_changes([_insert_op("note-3", "current", _hlc(5_000), "i3")])
    applied = await eng.apply_remote_changes([_delete_op("note-3", _hlc(1_000), "d3")])

    assert applied == 0
    assert await _note_count(store, "note-3") == 1


# ── 6. GC: tombstones are prunable, and the horizon is respected ────────


@pytest.mark.asyncio
async def test_prune_tombstones_drops_only_expired(engine):
    eng, store = engine

    await eng.apply_remote_changes([_insert_op("old", "x", _hlc(1_000), "i-old")])
    await eng.apply_remote_changes([_delete_op("old", _hlc(2_000), "d-old")])
    await eng.apply_remote_changes([_insert_op("new", "y", _hlc(1_000), "i-new")])
    await eng.apply_remote_changes([_delete_op("new", _hlc(2_000), "d-new")])
    assert await store.tombstone_count() == 2

    # Age the first tombstone past the horizon.
    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE sync_tombstones SET deleted_at = ? WHERE row_id = 'old'",
            (time.time() - 400 * 86400,),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    pruned = await store.prune_tombstones(max_age_seconds=90 * 86400)
    assert pruned == 1
    assert await store.tombstone_count() == 1

    # And the documented cost of GC: past the horizon, a stale replay
    # from a very long-offline peer CAN resurrect. Pinned so the
    # trade-off is visible in the suite rather than only in a comment.
    await eng.apply_remote_changes([_insert_op("old", "x", _hlc(1_000), "i-old")])
    assert await _note_count(store, "old") == 1
    assert await _note_count(store, "new") == 0


# ── 7. The GC is actually wired to something that runs ──────────────────


@pytest.mark.asyncio
async def test_scheduler_tick_prunes_tombstones_and_rate_limits(engine, monkeypatch):
    """A GC nobody calls is not a GC. The scheduler cadence tick is the
    only periodic sync-owned callback, so pin that it prunes, and that
    it does not prune on every 30-second tick.
    """
    import memory.sync_scheduler as sched_mod

    eng, store = engine
    scheduler = sched_mod.SyncScheduler(eng)

    calls: list[float] = []
    real_prune = store.prune_tombstones

    async def _counting_prune(*args, **kwargs):
        calls.append(time.time())
        return await real_prune(*args, **kwargs)

    monkeypatch.setattr(store, "prune_tombstones", _counting_prune)

    await scheduler._tick()
    assert len(calls) == 1, "cadence tick must prune tombstones"

    await scheduler._tick()
    assert len(calls) == 1, "GC must be rate-limited, not run on every tick"

    scheduler._last_tombstone_gc -= sched_mod.TOMBSTONE_GC_INTERVAL_SECONDS + 1
    await scheduler._tick()
    assert len(calls) == 2
