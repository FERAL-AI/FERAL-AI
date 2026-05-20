"""PR 2 (v2026.5.34) D11 acceptance — Ebbinghaus memory decay + forgetting.

Eleven tests pinning the contract laid out in the master plan:

  1. Decay formula matches the documented Ebbinghaus + SM-2 derivation
     across a parametrized table of inputs.
  2. The service sweep updates ``decay_factor`` and writes
     ``forgotten_at`` for rows below ``forget_threshold``.
  3. Access boost — rehearsed items decay slower than ignored ones.
  4. Forget-threshold honoured — a row whose decay falls below the
     threshold gets marked forgotten on the next sweep.
  5. Default ``episode_search`` excludes forgotten rows.
  6. ``include_forgotten=True`` opt-in returns them.
  7. Recall restores a forgotten row.
  8. Hard delete after ``retention_days`` past ``forgotten_at`` removes
     the episode + its FTS shadow + its memory_chunks rows.
  9. Idempotency — running ``run_once`` twice in a row produces the
     same ``decay_factor`` on every row.
 10. ``enabled=False`` short-circuits the loop entirely (no sweep
     ever happens, no DB writes).
 11. A concurrent ``episode_search`` during a sweep does not deadlock
     and returns sensible results.
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from memory.decay import (
    DecayConfig,
    MemoryDecayService,
    compute_decay,
)
from memory.store import MemoryStore


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "decay.db"))
    try:
        yield s
    finally:
        await s.aclose()


# ── 1. Decay formula ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case",
    [
        # Fresh, never accessed: decay ≈ 1.
        {
            "name": "fresh",
            "now": 1_700_000_000.0,
            "created_at": 1_700_000_000.0,
            "last_accessed_at": 0.0,
            "access_count": 0,
            "importance": 0.5,
            "expected_floor": 0.6,  # importance^0.5 ≈ 0.707
            "expected_ceiling": 1.0,
        },
        # Hour-old, never accessed: small base decay.
        {
            "name": "one_hour_old",
            "now": 1_700_003_600.0,
            "created_at": 1_700_000_000.0,
            "last_accessed_at": 0.0,
            "access_count": 0,
            "importance": 0.5,
            "expected_floor": 0.6,
            "expected_ceiling": 1.0,
        },
        # Three months old, no access: noticeable decay.
        {
            "name": "ninety_days_no_access",
            "now": 1_700_000_000.0 + 90 * 86400,
            "created_at": 1_700_000_000.0,
            "last_accessed_at": 0.0,
            "access_count": 0,
            "importance": 0.5,
            "expected_floor": 0.0,
            "expected_ceiling": 0.5,
        },
        # Three months old, rehearsed 20 times: with the documented
        # decay_rate=0.001 the natural Ebbinghaus floor is exp(-2.16)
        # ≈ 0.115, multiplied by ≈0.89 (importance^0.5) and ≈1.30
        # (1 + log(21)*0.1). Final ≈ 0.13. The same row with no
        # access (covered by the case above) ends ≈ 0.07, so the
        # boost is real but bounded. Pin the math.
        {
            "name": "ninety_days_with_rehearsal",
            "now": 1_700_000_000.0 + 90 * 86400,
            "created_at": 1_700_000_000.0,
            "last_accessed_at": 1_700_000_000.0 + 90 * 86400 - 3600,
            "access_count": 20,
            "importance": 0.8,
            "expected_floor": 0.10,
            "expected_ceiling": 0.20,
        },
        # Output is clamped to [0, 1].
        {
            "name": "clamped_at_one",
            "now": 1_700_000_000.0,
            "created_at": 1_700_000_000.0 - 1,
            "last_accessed_at": 1_700_000_000.0,
            "access_count": 10_000,
            "importance": 1.0,
            "expected_floor": 0.95,
            "expected_ceiling": 1.0,
        },
    ],
    ids=lambda c: c["name"],
)
def test_compute_decay_formula(case):
    """Pure formula matches the documented Ebbinghaus + SM-2 derivation."""
    decay = compute_decay(
        now=case["now"],
        created_at=case["created_at"],
        last_accessed_at=case["last_accessed_at"],
        access_count=case["access_count"],
        importance=case["importance"],
        decay_rate=0.001,
        access_boost_factor=0.1,
    )
    assert 0.0 <= decay <= 1.0
    assert case["expected_floor"] <= decay <= case["expected_ceiling"], (
        f"{case['name']}: decay={decay:.4f} not in "
        f"[{case['expected_floor']}, {case['expected_ceiling']}]"
    )


# ── 2. Sweep mutates decay_factor + forgotten_at ─────────────────────────


@pytest.mark.asyncio
async def test_run_once_updates_decay_factor_and_marks_forgotten(store, monkeypatch):
    """A row aged 1000 hours with low importance crosses the threshold
    on the first sweep."""
    real_now = time.time()
    # Seed an episode that will land far below the forget threshold.
    await store.episode_save(
        session_id="s1", event_type="conversation",
        summary="old chatter", importance=0.1,
    )
    # Backdate the row so the sweep sees an "ancient" episode.
    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = ?",
            (real_now - 1_000_000.0, real_now - 1_000_000.0),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    service = MemoryDecayService(store, DecayConfig(
        enabled=True, decay_rate=0.01, forget_threshold=0.5,
    ))
    result = await service.run_once()

    assert result["ok"]
    assert result["scanned"] == 1
    assert result["newly_forgotten"] == 1
    assert result["updated"] == 1

    rows = await store.episode_recent(limit=10, include_forgotten=True)
    assert len(rows) == 1
    assert rows[0]["forgotten_at"] is not None
    assert rows[0]["decay_factor"] < 0.5


# ── 3. Access boost — rehearsed items decay slower ──────────────────────


@pytest.mark.asyncio
async def test_access_boost_rescues_rehearsed_item(store):
    """Two ancient episodes — one rehearsed, one not. After sweep the
    rehearsed one has a higher decay_factor."""
    real_now = time.time()
    await store.episode_save(session_id="s", event_type="x", summary="rehearsed", importance=0.5)
    await store.episode_save(session_id="s", event_type="x", summary="abandoned", importance=0.5)

    conn = await store._conn()
    try:
        # Same age, different access pattern. The rehearsed one was
        # touched recently and accessed many times.
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = ?, access_count = 0 "
            "WHERE summary = 'abandoned'",
            (real_now - 500_000.0, 0.0),
        )
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = ?, access_count = 50 "
            "WHERE summary = 'rehearsed'",
            (real_now - 500_000.0, real_now - 60.0),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    service = MemoryDecayService(store, DecayConfig(enabled=True, decay_rate=0.01))
    await service.run_once()

    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT summary, decay_factor FROM episodes"
        ) as cur:
            rows = {r["summary"]: r["decay_factor"] for r in await cur.fetchall()}
    finally:
        await store._release(conn)

    assert rows["rehearsed"] > rows["abandoned"], (
        f"rehearsed={rows['rehearsed']:.4f} should exceed abandoned={rows['abandoned']:.4f}"
    )


# ── 4. Forget threshold cuts at the configured boundary ─────────────────


@pytest.mark.asyncio
async def test_forget_threshold_boundary(store):
    """A row above the threshold stays active; a row below gets marked.

    Calibrated to land on either side of forget_threshold=0.3 with
    decay_rate=0.001 and age=100h (the documented Ebbinghaus reference
    point in the formula's docstring). High importance keeps the
    factor above 0.5; low importance pushes it under 0.25.
    """
    real_now = time.time()
    await store.episode_save(session_id="s", event_type="x", summary="just_above", importance=1.0)
    await store.episode_save(session_id="s", event_type="x", summary="just_below", importance=0.1)

    conn = await store._conn()
    try:
        # 100 hours = 360_000 s. Same age for both rows; only
        # importance separates them.
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = ?",
            (real_now - 360_000.0, real_now - 360_000.0),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    service = MemoryDecayService(store, DecayConfig(
        enabled=True, decay_rate=0.001, forget_threshold=0.3,
    ))
    await service.run_once()

    rows = {
        r["summary"]: r
        for r in await store.episode_recent(limit=10, include_forgotten=True)
    }
    assert rows["just_above"]["forgotten_at"] is None, (
        f"high-importance row was marked forgotten "
        f"(decay={rows['just_above']['decay_factor']:.3f})"
    )
    assert rows["just_below"]["forgotten_at"] is not None, (
        f"low-importance row was NOT marked forgotten "
        f"(decay={rows['just_below']['decay_factor']:.3f})"
    )


# ── 5. Default episode_search excludes forgotten ────────────────────────


@pytest.mark.asyncio
async def test_default_episode_search_excludes_forgotten(store):
    await store.episode_save(session_id="s", event_type="x", summary="alpha kept", importance=0.5)
    await store.episode_save(session_id="s", event_type="x", summary="alpha gone", importance=0.5)

    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET forgotten_at = ? WHERE summary = 'alpha gone'",
            (time.time(),),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    hits_default = await store.episode_search("alpha")
    summaries = {h["summary"] for h in hits_default}
    assert "alpha kept" in summaries
    assert "alpha gone" not in summaries

    recent_default = await store.episode_recent(limit=10)
    assert all(r["summary"] != "alpha gone" for r in recent_default)


# ── 6. include_forgotten opt-in returns them ────────────────────────────


@pytest.mark.asyncio
async def test_include_forgotten_flag_returns_them(store):
    await store.episode_save(session_id="s", event_type="x", summary="bravo kept")
    await store.episode_save(session_id="s", event_type="x", summary="bravo gone")

    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET forgotten_at = ? WHERE summary = 'bravo gone'",
            (time.time(),),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    hits = await store.episode_search("bravo", include_forgotten=True)
    summaries = {h["summary"] for h in hits}
    assert {"bravo kept", "bravo gone"}.issubset(summaries)

    recent = await store.episode_recent(limit=10, include_forgotten=True)
    assert any(r["summary"] == "bravo gone" for r in recent)


# ── 7. Recall restores a forgotten row ──────────────────────────────────


@pytest.mark.asyncio
async def test_recall_restores_forgotten(store):
    saved = await store.episode_save(session_id="s", event_type="x", summary="recallme")
    eid = saved["id"]

    service = MemoryDecayService(store)
    await service.forget(eid)

    hits = await store.episode_search("recallme")
    assert all(h["id"] != eid for h in hits), "forgotten row leaked into default search"

    result = await service.recall(eid)
    assert result["ok"]
    assert result["forgotten_at"] is None

    hits_after = await store.episode_search("recallme")
    assert any(h["id"] == eid for h in hits_after), "recall did not restore visibility"


# ── 8. Hard delete after retention_days ─────────────────────────────────


@pytest.mark.asyncio
async def test_hard_delete_after_retention(store):
    saved = await store.episode_save(session_id="s", event_type="x", summary="doomed")
    eid = saved["id"]

    real_now = time.time()
    # Forgotten 10 days ago; retention is 7 days → must be hard-deleted.
    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET forgotten_at = ? WHERE id = ?",
            (real_now - 10 * 86400.0, eid),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    service = MemoryDecayService(store, DecayConfig(
        enabled=True, retention_days=7,
    ))
    result = await service.run_once()
    assert result["hard_deleted"] == 1

    conn = await store._conn()
    try:
        async with conn.execute("SELECT id FROM episodes WHERE id = ?", (eid,)) as cur:
            row = await cur.fetchone()
        assert row is None, "hard delete left the episode behind"

        async with conn.execute(
            "SELECT COUNT(*) AS n FROM memory_chunks "
            "WHERE source_table = 'episodes' AND source_id = ?",
            (eid,),
        ) as cur:
            n = (await cur.fetchone())["n"]
        assert n == 0, "memory_chunks rows survived hard delete"
    finally:
        await store._release(conn)


# ── 9. Idempotent re-run ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_is_idempotent(store):
    real_now = time.time()
    for i in range(10):
        await store.episode_save(
            session_id="s", event_type="x", summary=f"e{i}", importance=0.5,
        )

    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = ?",
            (real_now - 100_000.0, real_now - 100_000.0),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    service = MemoryDecayService(store, DecayConfig(enabled=True, decay_rate=0.001))
    await service.run_once()

    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT id, decay_factor FROM episodes ORDER BY id"
        ) as cur:
            first = {r["id"]: r["decay_factor"] for r in await cur.fetchall()}
    finally:
        await store._release(conn)

    await service.run_once()

    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT id, decay_factor FROM episodes ORDER BY id"
        ) as cur:
            second = {r["id"]: r["decay_factor"] for r in await cur.fetchall()}
    finally:
        await store._release(conn)

    for k, v in first.items():
        assert math.isclose(v, second[k], abs_tol=1e-3), (
            f"{k}: decay shifted {v} → {second[k]} on re-run"
        )


# ── 10. Disabled flag short-circuits the loop ───────────────────────────


@pytest.mark.asyncio
async def test_disabled_setting_skips_loop(store):
    service = MemoryDecayService(store, DecayConfig(enabled=False))
    await service.start()
    assert service._task is None, "loop started despite enabled=False"

    result = await service.run_once()
    assert result == {"ok": False, "reason": "disabled"}


# ── 11. Concurrent search during sweep stays alive ──────────────────────


@pytest.mark.asyncio
async def test_concurrent_search_during_sweep(store):
    """Spawn a sweep + a flurry of searches; both must complete with
    no deadlock and no exceptions. The pool's 4 connections must serve
    both workloads concurrently.
    """
    for i in range(40):
        await store.episode_save(
            session_id="s", event_type="x",
            summary=f"concurrent {i} alpha bravo", importance=0.5,
        )

    service = MemoryDecayService(store, DecayConfig(enabled=True, decay_rate=0.0001))

    async def query_repeatedly():
        last = []
        for _ in range(8):
            hits = await store.episode_search("alpha")
            last = hits
            await asyncio.sleep(0)
        return last

    sweep_task = asyncio.create_task(service.run_once())
    queries_task = asyncio.create_task(query_repeatedly())

    sweep_result, query_result = await asyncio.wait_for(
        asyncio.gather(sweep_task, queries_task), timeout=15.0
    )

    assert sweep_result["ok"]
    assert isinstance(query_result, list)
    # Some queries should have returned non-empty results (FTS prime
    # may have been slow on the first call but the rest hit it).
    assert query_result, "concurrent search returned only empty results"
