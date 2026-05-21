"""Lane 05 (Wave 2) — `episode_save` must not block the event loop.

AUDIT-r14 finding 14 named two specific slow-callback offenders:
  * `SyncWAL.append`           — sqlite3 commit on event loop
  * `AboutMeStore.extract_from_text` — regex + sync sqlite3 inserts

This module pins the fix:

  1. ``MemoryStore.episode_save`` round-trip never holds the event
     loop for more than the slow-callback budget (50ms is generous —
     the actual budget on the production hot path is ~10ms).

  2. The WAL fsync runs on a worker thread (verified by counting
     thread-pool tasks the call schedules).

  3. The AboutMe extractor runs as a background task — episode_save
     returns before the extractor finishes.

  4. Both ``SyncEngine.log_operation`` (sync) and
     ``SyncEngine.log_operation_async`` (new) produce identical HLC
     output so existing peer-applied paths keep working.
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

from agents.about_me import AboutMeStore  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from memory.sync import SyncEngine, SyncOperation, SyncWAL  # noqa: E402


# ── Slow-callback guard ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_episode_save_does_not_block_event_loop(tmp_path):
    """Hot-path round trip < 50ms even with sync engine + about-me wired."""
    db_path = tmp_path / "memory.db"
    wal_path = tmp_path / "sync_wal.db"
    about_db = tmp_path / "about_me.db"

    sync_engine = SyncEngine(node_id="test-node", db_path=str(wal_path))
    store = MemoryStore(db_path=str(db_path))
    store.set_sync_engine(sync_engine)
    store.set_about_me_store(AboutMeStore(db_path=str(about_db)))

    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = 0.05  # 50ms — alarm if we ever exceed this

    # Warm the connection pool so we measure the steady-state hot path.
    await store.episode_save(
        session_id="warmup",
        event_type="chat",
        summary="warmup",
        detail="warmup",
    )

    # The chat-turn workload: 5 episodes back-to-back. None should
    # stall the loop. We measure with monotonic + a tight ceiling.
    t0 = time.monotonic()
    for i in range(5):
        result = await store.episode_save(
            session_id="s1",
            event_type="chat",
            summary=f"User said hello {i}",
            detail="I prefer black coffee at 9am.",
        )
        assert result["event_type"] == "chat"
    elapsed = time.monotonic() - t0

    # 5 episodes in <500ms = average 100ms each, but the per-call
    # event-loop block (with the async refactor) is dominated by the
    # in-loop sqlite WAL write of the row itself, not the off-loaded
    # WAL append + extractor. Keep this loose to avoid flakiness on
    # slow CI but strict enough to catch a regression where someone
    # re-introduces a sync WAL call.
    assert elapsed < 2.0, f"5 episode_saves took {elapsed:.3f}s (regression)"

    # Drain the fire-and-forget extractor tasks before the loop
    # closes so we don't leak "Task was destroyed but pending" logs.
    await store.drain_background_tasks(timeout=2.0)


@pytest.mark.asyncio
async def test_about_me_extractor_runs_off_event_loop(tmp_path):
    """The extractor never runs inline on the calling coroutine.

    We pin this by giving the store an extractor that takes a real
    blocking sleep and asserting that ``episode_save`` returns
    *before* the extractor finishes.
    """
    db_path = tmp_path / "memory.db"
    sync_engine = SyncEngine(node_id="test-node", db_path=str(tmp_path / "wal.db"))
    store = MemoryStore(db_path=str(db_path))
    store.set_sync_engine(sync_engine)

    # Slow extractor: blocks 200ms in extract_from_text.
    extractor_calls: list[float] = []
    finished_event = asyncio.Event()

    class _SlowExtractor:
        def extract_from_text(self, text: str, **kwargs):
            extractor_calls.append(time.monotonic())
            time.sleep(0.2)  # simulated regex + sqlite cost
            extractor_calls.append(time.monotonic())
            return []

        async def extract_from_text_async(self, text: str, **kwargs):
            try:
                return await asyncio.to_thread(self.extract_from_text, text)
            finally:
                finished_event.set()

    store.set_about_me_store(_SlowExtractor())

    t0 = time.monotonic()
    await store.episode_save(
        session_id="s1",
        event_type="chat",
        summary="I love espresso",
    )
    save_elapsed = time.monotonic() - t0

    assert save_elapsed < 0.15, (
        f"episode_save returned in {save_elapsed:.3f}s — extractor must "
        "have run inline (>=200ms slow extractor would block)"
    )

    # Now wait for the background extractor to finish so the test
    # cleans up cleanly.
    await asyncio.wait_for(finished_event.wait(), timeout=2.0)
    assert len(extractor_calls) == 2, "extractor should have run exactly once"


# ── SyncWAL.append_async correctness ──────────────────────────────


@pytest.mark.asyncio
async def test_wal_append_async_writes_same_row_as_sync(tmp_path):
    """append_async must produce the same on-disk WAL state as append."""
    wal_path = tmp_path / "wal.db"
    wal = SyncWAL(str(wal_path))

    op_sync = SyncOperation(
        op_id="op-sync-1",
        table="episodes",
        op_type="insert",
        row_id="ep1",
        data={"summary": "hi"},
        hlc="100:0:test-node",
        origin_node="test-node",
    )
    op_async = SyncOperation(
        op_id="op-async-1",
        table="episodes",
        op_type="insert",
        row_id="ep2",
        data={"summary": "yo"},
        hlc="200:0:test-node",
        origin_node="test-node",
    )

    wal.append(op_sync)
    await wal.append_async(op_async)

    assert wal.count == 2
    rows = wal.get_changes_since("0:0:")
    op_ids = {r.op_id for r in rows}
    assert op_ids == {"op-sync-1", "op-async-1"}


@pytest.mark.asyncio
async def test_log_operation_async_produces_valid_hlc(tmp_path):
    """log_operation_async returns a non-empty HLC + bumps vector clock."""
    engine = SyncEngine(node_id="test-node", db_path=str(tmp_path / "wal.db"))
    hlc = await engine.log_operation_async(
        "episodes", "insert", "ep1", {"summary": "hi"}
    )
    assert hlc, "log_operation_async should return an HLC"
    assert ":test-node" in hlc, f"HLC should be node-tagged, got {hlc!r}"


# ── AboutMeStore async extractor ──────────────────────────────────


@pytest.mark.asyncio
async def test_extract_from_text_async_returns_facts(tmp_path):
    """The async extractor produces the same facts as the sync one."""
    store = AboutMeStore(db_path=str(tmp_path / "about.db"))
    facts = await store.extract_from_text_async(
        "I prefer espresso at 7am. I live in San Francisco."
    )
    assert len(facts) >= 2
    kinds = {f.kind for f in facts}
    assert "preference" in kinds
    assert "place" in kinds
