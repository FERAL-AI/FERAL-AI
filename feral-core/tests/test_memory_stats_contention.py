"""Pin the steady-state stats() contention fix.

Background — operator-observed bug: with ~5k episodes and the brain's
background services holding pool connections (sync scheduler, decay
sweeper, proactive engine, learner, cron, screen loop), every
``/api/memory/stats`` poll queued the COUNT(*) round behind the
writers and tripped the 2.5s safety budget. The dashboard then
flooded the brain log with ``stats: aiosqlite COUNT queries exceeded
the 2.5s budget`` warnings every 1-2s and rendered a permanently
degraded "0 episodes" payload.

These tests pin the structural fix:

1. A short-TTL cache on :meth:`MemoryStore.stats` collapses rapid
   dashboard polls into a single COUNT round; the second call inside
   the TTL must come from cache and must NOT re-acquire a SQLite
   connection.
2. After the TTL elapses the cache must refresh — a fresh COUNT runs
   and the new payload replaces the cached one.
3. With WAL + the dedicated read-only stats connection, a held
   write transaction on the writer pool must NOT block stats():
   the COUNT round still completes promptly inside the safety
   budget.
"""
from __future__ import annotations

import asyncio

import pytest

from memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    """A real MemoryStore with a temporary on-disk SQLite DB.

    Construction stays lightweight — the embedder + sqlite-vec backend
    spin up but don't make network calls in the test environment.
    """
    s = MemoryStore(db_path=str(tmp_path / "stats_contention.db"))
    yield s
    # Best-effort drain so leftover background fire-and-forget tasks
    # don't bleed into the next test's event loop.
    try:
        asyncio.get_event_loop().run_until_complete(s.aclose())
    except Exception:
        pass


@pytest.mark.asyncio
async def test_stats_served_from_cache_within_ttl(store, monkeypatch):
    """Two rapid stats() calls inside the TTL window must run the
    SQLite COUNT round exactly once.

    The dashboard polls /api/memory/stats ~1Hz; without the cache
    that drove ~1 COUNT round per second under load. The cache
    collapses the herd into one round per TTL.
    """
    # Seed something cheap so the COUNTs have non-zero values to pin.
    await store.save("contention probe note", tags=["probe"])

    call_count = {"n": 0}
    real_compute = store._compute_and_cache_stats

    async def _counting_compute() -> dict:
        call_count["n"] += 1
        return await real_compute()

    monkeypatch.setattr(store, "_compute_and_cache_stats", _counting_compute)

    first = await store.stats()
    second = await store.stats()
    third = await store.stats()

    assert first["ok"] is True
    assert first["notes"] >= 1

    # First call computes; the next two must come from cache.
    assert call_count["n"] == 1, (
        f"expected exactly one COUNT round across three rapid stats() "
        f"calls, got {call_count['n']}"
    )
    # Cached payloads expose the cache marker so a debugging operator
    # can tell at a glance which calls hit the cache.
    assert second.get("from_cache") is True
    assert third.get("from_cache") is True
    # Cached calls still preserve every count field — the UI's
    # ``Number(t.x ?? 0)`` parsing must keep seeing the same shape.
    assert second["notes"] == first["notes"]
    assert second["episodes"] == first["episodes"]
    assert second["knowledge_triples"] == first["knowledge_triples"]


@pytest.mark.asyncio
async def test_stats_cache_refreshes_after_ttl(store, monkeypatch):
    """After ``_STATS_CACHE_TTL_S`` elapses, the next stats() call
    must trigger a fresh COUNT round."""
    monkeypatch.setattr(store, "_STATS_CACHE_TTL_S", 0.05)

    call_count = {"n": 0}
    real_compute = store._compute_and_cache_stats

    async def _counting_compute() -> dict:
        call_count["n"] += 1
        return await real_compute()

    monkeypatch.setattr(store, "_compute_and_cache_stats", _counting_compute)

    first = await store.stats()
    assert call_count["n"] == 1

    # Inside the TTL: cached.
    second = await store.stats()
    assert call_count["n"] == 1
    assert second.get("from_cache") is True

    # After the TTL: fresh COUNT.
    await asyncio.sleep(0.08)
    third = await store.stats()
    assert call_count["n"] == 2
    # The fresh payload is the canonical (non-cached) shape.
    assert third.get("from_cache") is not True
    assert third["ok"] is True


@pytest.mark.asyncio
async def test_stats_read_does_not_block_on_writer(store):
    """Hold every connection in the writer pool against a long-running
    transaction; stats() must still complete promptly.

    Pre-fix, the COUNT round acquired a pool connection and would
    queue behind the writers, tripping the 2.5s budget. With the
    dedicated read-only connection (separate from the pool, opened
    in WAL mode), the COUNTs no longer compete with writers and
    return well inside the safety budget even when the pool is
    fully claimed.
    """
    # Prime the cache fields with one real call so we observe the
    # uncached path next time (cache won't short-circuit).
    store._stats_cache = None
    store._stats_cache_at = 0.0

    # Force the writer pool to fill so we know we're testing the
    # contention path, not just the quiet path.
    held: list = []
    try:
        for _ in range(store._pool_size):
            held.append(await store._conn())

        # Open and hold a write transaction on one of the pool
        # connections — this is the worst case the operator observes,
        # where a background service is mid-INSERT and SQLite has
        # taken the reserved/exclusive lock.
        await held[0].execute("BEGIN IMMEDIATE")
        await held[0].execute(
            "INSERT INTO notes (id, content, tags, importance, source, "
            "created_at, updated_at) VALUES (?, ?, '[]', 'normal', "
            "'test', 0, 0)",
            ("blocker", "writer holds the pool"),
        )

        result = await asyncio.wait_for(store.stats(), timeout=2.0)

        assert result["ok"] is True, (
            "stats() must NOT return a degraded payload while the "
            "writer pool is held — the dedicated read-only "
            "connection should bypass pool contention entirely."
        )
        # The held INSERT is uncommitted, so the read sees the
        # pre-blocker state of ``notes``. We don't care about the
        # exact count — just that the read completed promptly.
        assert "notes" in result
        assert "episodes" in result
    finally:
        # Roll back so the held connection is reusable on aclose().
        try:
            await held[0].execute("ROLLBACK")
        except Exception:
            pass
        for c in held:
            try:
                store._pool.put_nowait(c)
            except Exception:
                pass
