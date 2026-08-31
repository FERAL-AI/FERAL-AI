"""Clock-drift guards on the HLC and the sync apply path.

Pins the failure this fixes: HLC's correctness bound |l - pt| <= epsilon
is *assumed*, not enforced (Kulkarni et al., Corollary 1), and Theorem 2
(l.f >= pt.f) means an adopted far-future timestamp is never walked
back. Before this guard, one peer with a wrong clock would pin every
other node's logical clock in the future and win every last-writer-wins
comparison from then on.

The gate has to reject the *operation*, not just decline to advance the
clock, because the LWW comparison in ``_apply_to_memory`` reads
``op.hlc`` directly.
"""

from __future__ import annotations

import time

import pytest

from memory.hlc import (
    ClockDriftRejection,
    HLCTimestamp,
    HybridLogicalClock,
)
from memory.store import MemoryStore
from memory.sync import SyncEngine, SyncOperation

DAY_MS = 24 * 60 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── HLC unit level ──────────────────────────────────────────────────────


class TestDriftGuard:
    def test_far_future_remote_is_not_adopted(self):
        """The 2038 case: a wildly-ahead peer must not pin our clock."""
        hlc = HybridLogicalClock("node-a")
        poison = HLCTimestamp(wall_ms=_now_ms() + 3650 * DAY_MS, counter=0,
                              node_id="node-evil")

        out = hlc.receive(poison)

        assert hlc.drift_rejections == 1
        assert hlc.last_rejection == poison
        # Our clock stayed anchored to physical time, not the poison.
        assert out.wall_ms < poison.wall_ms
        assert abs(out.wall_ms - _now_ms()) < 60_000

    def test_clock_still_usable_after_rejection(self):
        """Rejection must not wedge the clock — later events still tick."""
        hlc = HybridLogicalClock("node-a")
        hlc.receive(HLCTimestamp(_now_ms() + 3650 * DAY_MS, 0, "node-evil"))

        t1 = hlc.now()
        t2 = hlc.now()
        assert t2.to_tuple() > t1.to_tuple()
        assert abs(t2.wall_ms - _now_ms()) < 60_000

    def test_modest_skew_is_still_adopted(self):
        """The bound is loose on purpose — ordinary skew must pass."""
        hlc = HybridLogicalClock("node-a", max_drift_ms=300_000)
        ahead = HLCTimestamp(wall_ms=_now_ms() + 5_000, counter=7,
                             node_id="node-b")

        out = hlc.receive(ahead)

        assert hlc.drift_rejections == 0
        assert out.wall_ms == ahead.wall_ms
        assert out.counter == ahead.counter + 1

    def test_past_timestamps_are_never_rejected(self):
        """Only future drift is dangerous; stale ops must still apply."""
        hlc = HybridLogicalClock("node-a")
        old = HLCTimestamp(wall_ms=1_000, counter=0, node_id="node-b")

        hlc.receive(old)

        assert hlc.drift_rejections == 0

    def test_drift_boundary_is_inclusive(self):
        hlc = HybridLogicalClock("node-a", max_drift_ms=10_000)
        pt = _now_ms()
        assert hlc.is_within_drift(HLCTimestamp(pt + 9_000, 0, "b"), physical_ms=pt)
        assert hlc.is_within_drift(HLCTimestamp(pt + 10_000, 0, "b"), physical_ms=pt)
        assert not hlc.is_within_drift(HLCTimestamp(pt + 10_001, 0, "b"), physical_ms=pt)

    def test_receive_strict_raises(self):
        hlc = HybridLogicalClock("node-a", max_drift_ms=1_000)
        poison = HLCTimestamp(_now_ms() + DAY_MS, 0, "node-evil")

        with pytest.raises(ClockDriftRejection):
            hlc.receive_strict(poison)

    def test_self_stabilizes_from_a_poisoned_state(self):
        """A clock already in the future (restored state, pre-fix data)
        must reset to physical time rather than stay stuck."""
        hlc = HybridLogicalClock("node-a", max_drift_ms=60_000)
        hlc._wall_ms = _now_ms() + 3650 * DAY_MS  # simulate prior poisoning
        hlc._counter = 42

        ts = hlc.now()

        assert hlc.stabilizations == 1
        assert abs(ts.wall_ms - _now_ms()) < 60_000

    def test_counter_overflow_stabilizes(self):
        hlc = HybridLogicalClock("node-a", max_counter=100)
        hlc._wall_ms = _now_ms()
        hlc._counter = 101

        hlc.now()

        assert hlc.stabilizations == 1
        # Reset to (physical, 0), then the local tick for *this* event
        # bumps the counter to 1 because physical has not advanced past
        # wall_ms yet. The point is that it came back down from 101.
        assert hlc._counter <= 1

    def test_health_reports_counters(self):
        hlc = HybridLogicalClock("node-a")
        hlc.receive(HLCTimestamp(_now_ms() + 3650 * DAY_MS, 0, "node-evil"))

        health = hlc.health
        assert health["drift_rejections"] == 1
        assert health["node_id"] == "node-a"
        assert "node-evil" in health["last_rejection"]


# ── sync apply path ─────────────────────────────────────────────────────


@pytest.fixture
async def engine(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "a.db"))
    eng = SyncEngine(node_id="node-a", memory_store=store,
                     db_path=str(tmp_path / "a_wal.db"))
    store.set_sync_engine(eng)
    try:
        yield eng, store
    finally:
        await store.aclose()


def _op(op_id: str, hlc: str, content: str, row_id: str = "note1") -> dict:
    return SyncOperation(
        op_id=op_id,
        table="notes",
        op_type="insert",
        row_id=row_id,
        data={
            "id": row_id, "content": content, "tags": "[]",
            "importance": "normal", "source": "node-b",
            "created_at": time.time(),
        },
        hlc=hlc,
        origin_node="node-b",
    ).to_dict()


@pytest.mark.asyncio
async def test_far_future_op_does_not_win_lww(engine):
    """The whole point. A poisoned op must not land, and must not
    become the row version that every later honest write loses to."""
    eng, store = engine

    poison_hlc = HLCTimestamp(_now_ms() + 3650 * DAY_MS, 0, "node-b").to_string()
    applied = await eng.apply_remote_changes([_op("op-poison", poison_hlc, "poison")])

    assert applied == 0
    assert eng._hlc_drift_rejections == 1

    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT content FROM notes WHERE id = 'note1'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store._release(conn)
    assert row is None, "poisoned op must not materialize"


@pytest.mark.asyncio
async def test_honest_op_still_applies_after_poison(engine):
    """A rejected op must not raise the bar for honest peers."""
    eng, store = engine

    poison_hlc = HLCTimestamp(_now_ms() + 3650 * DAY_MS, 0, "node-b").to_string()
    await eng.apply_remote_changes([_op("op-poison", poison_hlc, "poison")])

    good_hlc = HLCTimestamp(_now_ms(), 0, "node-b").to_string()
    applied = await eng.apply_remote_changes([_op("op-good", good_hlc, "honest")])

    assert applied == 1
    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT content FROM notes WHERE id = 'note1'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store._release(conn)
    assert row["content"] == "honest"


@pytest.mark.asyncio
async def test_malformed_hlc_does_not_abort_the_batch(engine):
    """One bad op must not drop the good ops behind it in the batch."""
    eng, store = engine

    good_hlc = HLCTimestamp(_now_ms(), 0, "node-b").to_string()
    batch = [
        _op("op-bad", "not-a-timestamp", "junk", row_id="note-bad"),
        _op("op-good", good_hlc, "survivor", row_id="note-good"),
    ]

    applied = await eng.apply_remote_changes(batch)

    assert applied == 1
    assert eng._hlc_malformed_rejections == 1

    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT content FROM notes WHERE id = 'note-good'"
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store._release(conn)
    assert row["content"] == "survivor"


@pytest.mark.asyncio
async def test_stats_surface_drift_counters(engine):
    eng, _store = engine
    poison_hlc = HLCTimestamp(_now_ms() + 3650 * DAY_MS, 0, "node-b").to_string()
    await eng.apply_remote_changes([_op("op-poison", poison_hlc, "poison")])

    stats = eng.stats
    assert stats["hlc_drift_rejections"] == 1
    assert stats["clock"]["max_drift_ms"] > 0
