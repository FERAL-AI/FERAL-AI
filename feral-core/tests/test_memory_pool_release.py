"""Regression tests for the MemoryStore connection-pool lifecycle.

Incident (v2026.7.x): ``memory/wiki.py`` and ``memory/notes_legacy.py``
took connections from ``MemoryStore``'s pool and returned them with
``await conn.close()`` instead of ``await store._release(conn)``. The
pool is filled exactly once at first use and never refills, so each of
those calls permanently destroyed one of the four connections. The
fifth call blocked forever inside ``asyncio.Queue.get()``, with no
timeout, no error and no log line, taking the whole memory subsystem
with it. ``list_recent`` is on an ordinary read path, so this was
reachable by simply using the brain.

Two further pool defects are pinned here:

* ``_release`` recursed into itself on ``QueueFull`` (measured 494
  frames, then a ``RecursionError`` swallowed by a broad ``except``),
  so the surplus connection was never closed and kept its file lock.
* ``refresh()`` opened its own ``aiosqlite`` connection and then
  ``_release``d it *into* the pool, grafting a foreign connection in.
  It runs on every inbound sync.

Every test here bounds its awaits with ``asyncio.wait_for`` so a
regression fails as a named ``TimeoutError`` instead of hanging the
suite until the CI job is cancelled.
"""
import asyncio
import os
import tempfile

import aiosqlite
import pytest

from memory.store import MemoryStore

# Generous enough that a loaded CI box never trips it, short enough
# that a real deadlock is reported in seconds.
CALL_TIMEOUT = 10.0


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = MemoryStore(db_path=path)
    yield s
    os.unlink(path)


def _read_paths(s):
    """Read paths that draw from the pool, keyed by name.

    Each entry is a zero-argument coroutine factory so the test can
    call it repeatedly.
    """
    return {
        "list_recent": lambda: s.list_recent(limit=5),
        "wiki_get_page": lambda: s.wiki_get_page("index"),
        "wiki_list_pages": lambda: s.wiki_list_pages(limit=5),
        "wiki_list_pages_query": lambda: s.wiki_list_pages(query="index", limit=5),
        "wiki_stats": lambda: s.wiki_stats(),
        "wiki_upsert_page": lambda: s.wiki_upsert_page(
            page_id="p1", title="t", kind="note", body_markdown="body"
        ),
        "wiki_compile": lambda: s.wiki_compile(
            notes_limit=2, episodes_limit=2, knowledge_limit=2
        ),
    }


@pytest.mark.parametrize("name", [
    "list_recent",
    "wiki_get_page",
    "wiki_list_pages",
    "wiki_list_pages_query",
    "wiki_stats",
    "wiki_upsert_page",
    "wiki_compile",
])
async def test_survives_more_calls_than_pool_size(store, name):
    """Calling any pooled read ``pool_size + 1`` times must still return.

    Before the fix each call leaked one connection, so call number
    ``pool_size + 1`` hung forever. One extra call past the pool size
    is the minimum that distinguishes "returns the connection" from
    "destroys the connection".
    """
    factory = _read_paths(store)[name]
    calls = store._pool_size + 1
    for i in range(calls):
        try:
            await asyncio.wait_for(factory(), timeout=CALL_TIMEOUT)
        except asyncio.TimeoutError:
            pytest.fail(
                f"{name} deadlocked on call {i + 1} of {calls}: the pool "
                f"({store._pool_size} connections) was exhausted, which means "
                f"a pooled connection was closed instead of released."
            )


async def test_pool_is_intact_after_repeated_reads(store):
    """The pool must hold exactly ``pool_size`` connections afterwards.

    A weaker version of this test (just "does it return?") would pass
    if a call closed a connection *and* something else opened a
    replacement. Assert the invariant directly.
    """
    for _ in range(store._pool_size * 3):
        await asyncio.wait_for(store.list_recent(limit=1), timeout=CALL_TIMEOUT)
        await asyncio.wait_for(store.wiki_stats(), timeout=CALL_TIMEOUT)
    assert store._pool is not None
    assert store._pool.qsize() == store._pool_size


async def test_release_closes_surplus_connection_without_recursing(store):
    """A release into a full pool closes the connection, no recursion.

    Before: ``_release`` called itself, hit the same full queue, and
    recursed ~494 frames until ``RecursionError``, which the broad
    ``except Exception`` swallowed, leaving the connection open and
    holding a SQLite file lock.
    """
    # Force the pool into existence and leave it full.
    conn = await store._conn()
    await store._release(conn)
    assert store._pool.qsize() == store._pool_size

    stray = await aiosqlite.connect(store.db_path)

    depth = {"max": 0, "current": 0}
    original = MemoryStore._release

    async def counting_release(self, c):
        depth["current"] += 1
        depth["max"] = max(depth["max"], depth["current"])
        try:
            return await original(self, c)
        finally:
            depth["current"] -= 1

    MemoryStore._release = counting_release
    try:
        await asyncio.wait_for(store._release(stray), timeout=CALL_TIMEOUT)
    finally:
        MemoryStore._release = original

    assert depth["max"] == 1, (
        f"_release recursed {depth['max']} frames deep on a full pool"
    )
    # The surplus connection must actually be closed, not leaked. An
    # aiosqlite connection runs a worker thread for its lifetime;
    # a live thread here means the file lock is still held.
    assert not stray._thread.is_alive(), (
        "surplus connection was not closed, it still holds a file lock"
    )
    # And the pool must be unchanged: no grafting, no shrinking.
    assert store._pool.qsize() == store._pool_size


async def test_refresh_does_not_graft_its_connection_into_the_pool(store):
    """``refresh()`` owns its connection and must close it.

    It opens a bare ``aiosqlite.connect`` (no pool row_factory, no
    PRAGMAs) and used to hand it to ``_release``, which either grew the
    pool past its configured size with a mis-configured connection or
    hit the recursion bug above. ``api/server.py`` calls this on every
    inbound sync, so it compounded once per peer message.
    """
    # Hold one connection out, so the pool has a free slot. This is the
    # state that exposes the bug: with a *full* pool the stray put
    # raises QueueFull and the damage shows up as the recursion above
    # instead, masking the graft.
    held = await store._conn()
    expected = store._pool_size - 1
    assert store._pool.qsize() == expected

    for _ in range(5):
        result = await asyncio.wait_for(store.refresh(), timeout=CALL_TIMEOUT)
        assert result["ok"] is True, result

    assert store._pool.qsize() == expected, (
        f"refresh() grafted its own connection into the pool: expected "
        f"{expected} pooled connections with one held out, found "
        f"{store._pool.qsize()}"
    )

    await store._release(held)
    assert store._pool.qsize() == store._pool_size

    # Every pooled connection must still be a real, correctly configured
    # one. A grafted connection is opened bare, with no ``aiosqlite.Row``
    # row_factory, so column-name access would break on it.
    for _ in range(store._pool_size):
        c = await asyncio.wait_for(store._conn(), timeout=CALL_TIMEOUT)
        async with c.execute("SELECT 1 AS one") as cur:
            row = await cur.fetchone()
            assert row["one"] == 1, "pooled connection lost its row_factory"
        await store._release(c)
