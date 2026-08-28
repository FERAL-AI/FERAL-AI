"""What the decay tier actually does, as opposed to what its knob names
suggest.

Three defects, all found by measuring a store rather than by reading the
call sites:

  D1  ``episode_recent`` bumped ``access_count`` on rows selected purely
      by ``created_at``. ``access_count`` is the rehearsal term in the
      decay formula, so a machine-driven recency read (the context
      builder runs one on every turn, ``GET /internal/episodes/recent``
      runs one per poll) made the newest N episodes decay-resistant
      regardless of whether a human ever read them.

  D2  With the shipped defaults a default-importance episode falls under
      ``forget_threshold`` at about 74 days and disappears from every
      default read path, while ``retention_days: 365`` sits next to it
      in ``settings.json``. 365 governs the hard delete that happens
      AFTER the row is already invisible, so the effective lifetime is
      ~74 days visible plus ~365 days recoverable, not 365 visible.

  D3  ``MemoryDecayService.recall`` wrote ``decay_factor = 1.0``, which
      the next hourly sweep recomputed from (age, idle, access) and
      overwrote. The value a caller read straight after a recall was not
      the value the row held an hour later.

Everything here asserts behaviour: what a read path returns, what a
column holds after a sweep, what the service reports.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from memory.decay import DecayConfig, MemoryDecayService, compute_decay
from memory.store import MemoryStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "decay-semantics.db"))
    try:
        yield s
    finally:
        await s.aclose()


async def _drain_access_tasks(store) -> None:
    """``_track_access`` is fire-and-forget. Await whatever it kicked so
    the assertion sees the settled value rather than a race."""
    for _ in range(3):
        tasks = list(getattr(store, "_access_tasks", ()) or ())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


async def _access_counts(store) -> dict[str, int]:
    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT summary, COALESCE(access_count, 0) AS n FROM episodes"
        ) as cur:
            return {r["summary"]: int(r["n"]) for r in await cur.fetchall()}
    finally:
        await store._release(conn)


# ── D1: a recency-only read must not rehearse ───────────────────────


async def test_episode_recent_does_not_rehearse(store):
    """``episode_recent`` orders by ``created_at`` and applies no
    relevance whatsoever. Counting that as a rehearsal means the decay
    of the newest N episodes is a function of how often a dashboard
    polls, which is not a property of the memory."""
    await store.episode_save(session_id="s", event_type="x", summary="alpha episode")
    await store.episode_save(session_id="s", event_type="x", summary="beta episode")

    for _ in range(3):
        await store.episode_recent(limit=10)
    await _drain_access_tasks(store)

    counts = await _access_counts(store)
    assert counts == {"alpha episode": 0, "beta episode": 0}, (
        f"a recency-only read rehearsed the newest episodes: {counts}"
    )


async def test_episode_recent_by_session_does_not_rehearse(store):
    """The ``session_id`` branch is the one the context builder calls on
    every turn, so it is the one that actually runs in production."""
    await store.episode_save(session_id="s1", event_type="x", summary="in session")

    for _ in range(3):
        await store.episode_recent(limit=10, session_id="s1")
    await _drain_access_tasks(store)

    counts = await _access_counts(store)
    assert counts == {"in session": 0}, f"session recency read rehearsed: {counts}"


async def test_relevance_driven_search_still_rehearses(store):
    """The other half. Access tracking exists so a memory a human keeps
    retrieving decays slower; removing it from the relevance path would
    delete the feature rather than fix the defect."""
    await store.episode_save(
        session_id="s", event_type="x", summary="kayak trip to the fjord"
    )
    await store.episode_save(session_id="s", event_type="x", summary="unrelated filler")

    hits = await store.episode_search_hybrid("kayak fjord", limit=5)
    assert hits, "the search returned nothing, so this proves nothing"
    await _drain_access_tasks(store)

    counts = await _access_counts(store)
    assert counts["kayak trip to the fjord"] >= 1, (
        f"a relevance-driven retrieval stopped rehearsing: {counts}"
    )


async def test_polling_recent_does_not_make_new_episodes_immortal(store):
    """The end-to-end consequence. Two equally old, equally unimportant
    episodes; one of them sat in the window a poller kept reading. After
    a sweep they must have the same retention."""
    real_now = time.time()
    await store.episode_save(session_id="s", event_type="x", summary="polled")
    await store.episode_save(session_id="s", event_type="x", summary="ignored")

    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = 0, access_count = 0",
            (real_now - 40 * 86400.0,),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    # 50 polls of the recency endpoint, the shape of a UI on a timer.
    for _ in range(50):
        await store.episode_recent(limit=1, session_id="s")
    await _drain_access_tasks(store)

    await MemoryDecayService(store, DecayConfig(enabled=True)).run_once()

    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT summary, decay_factor FROM episodes"
        ) as cur:
            factors = {r["summary"]: float(r["decay_factor"]) for r in await cur.fetchall()}
    finally:
        await store._release(conn)

    assert factors["polled"] == pytest.approx(factors["ignored"], abs=1e-9), (
        "polling /internal/episodes/recent changed how fast a memory decays: "
        f"{factors}"
    )


# ── D2: the real visibility horizon, and reporting it ───────────────


def test_forget_horizon_matches_the_measured_crossing():
    """``forget_horizon_days`` must agree with the formula it summarises,
    not with a number somebody typed once."""
    from memory.decay import forget_horizon_days

    cfg = DecayConfig()
    horizon = forget_horizon_days(cfg)
    assert 70.0 < horizon < 80.0, horizon

    now = 1_700_000_000.0

    def factor_at(days: float) -> float:
        return compute_decay(
            now=now,
            created_at=now - days * 86400.0,
            last_accessed_at=0.0,
            access_count=0,
            importance=0.5,
            decay_rate=cfg.decay_rate,
            access_boost_factor=cfg.access_boost_factor,
        )

    assert factor_at(horizon - 1.0) > cfg.forget_threshold
    assert factor_at(horizon + 1.0) < cfg.forget_threshold


def test_forget_horizon_moves_with_importance_and_rate():
    from memory.decay import forget_horizon_days

    cfg = DecayConfig()
    assert forget_horizon_days(cfg, importance=1.0) > forget_horizon_days(cfg)
    assert forget_horizon_days(cfg, importance=0.1) < forget_horizon_days(cfg)
    slower = DecayConfig(decay_rate=0.0005)
    assert forget_horizon_days(slower) > forget_horizon_days(cfg) * 1.9


async def test_stats_reports_the_visibility_horizon_next_to_retention(store):
    """``retention_days: 365`` is the only lifetime number the operator
    surface used to carry, and it is not the one that decides when a
    memory stops being findable. Report both."""
    stats = await MemoryDecayService(store, DecayConfig(enabled=True)).stats()
    assert stats["retention_days"] == 365.0
    assert "forget_horizon_days" in stats, sorted(stats)
    assert 70.0 < stats["forget_horizon_days"] < 80.0, stats["forget_horizon_days"]
    assert stats["forget_horizon_days"] < stats["retention_days"], (
        "the horizon is meant to expose that visibility ends long before "
        "retention does"
    )


async def test_ninety_day_old_episode_is_invisible_under_default_config(store):
    """The behaviour the horizon describes, measured on a store: at the
    shipped defaults a 90-day-old default-importance episode is gone from
    every default read path well inside ``retention_days``."""
    real_now = time.time()
    await store.episode_save(
        session_id="s", event_type="x", summary="kayak trip to the fjord"
    )
    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = 0, access_count = 0",
            (real_now - 90 * 86400.0,),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    await MemoryDecayService(store, DecayConfig(enabled=True)).run_once()

    assert await store.episode_recent(limit=10) == []
    assert await store.episode_search("kayak", limit=10) == []
    assert await store.episode_search_hybrid("kayak fjord", limit=10) == []
    # ... but still on disk, and still recoverable, which is the part
    # ``retention_days`` actually governs.
    assert await store.episode_recent(limit=10, include_forgotten=True)


# ── D3: recall must not write a value the sweep will contradict ─────


async def test_recall_decay_factor_survives_the_next_sweep(store):
    """``recall`` used to write ``decay_factor = 1.0`` unconditionally.
    The sweep recomputes from (age, idle, access, importance) and
    overwrote it within the hour, so the value a caller saw right after
    a recall was never the value the row kept."""
    real_now = time.time()
    await store.episode_save(session_id="s", event_type="x", summary="recalled one")
    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = 0, access_count = 0",
            (real_now - 30 * 86400.0,),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    service = MemoryDecayService(store, DecayConfig(enabled=True))
    eid = await _first_id(store)
    await service.forget(eid)
    await service.recall(eid)

    after_recall = await _decay_factor(store)
    await service.run_once()
    after_sweep = await _decay_factor(store)

    assert after_recall == pytest.approx(after_sweep, abs=1e-4), (
        f"recall reported decay_factor={after_recall:.4f} and the very next "
        f"sweep wrote {after_sweep:.4f}"
    )


async def test_recall_reports_whether_the_row_will_fade_again(store):
    """A recall of a memory too old to survive the formula is undone on
    the next sweep. The caller has to be able to tell."""
    real_now = time.time()
    await store.episode_save(session_id="s", event_type="x", summary="ancient")
    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET created_at = ?, last_accessed_at = 0, access_count = 0",
            (real_now - 400 * 86400.0,),
        )
        await conn.commit()
    finally:
        await store._release(conn)

    service = MemoryDecayService(store, DecayConfig(enabled=True))
    eid = await _first_id(store)
    await service.forget(eid)
    result = await service.recall(eid)

    assert result["ok"] is True
    assert result.get("refades") is True, result
    # And it does: the sweep puts it straight back.
    await service.run_once()
    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT forgotten_at FROM episodes WHERE id = ?", (eid,)
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store._release(conn)
    assert row["forgotten_at"] is not None, (
        "the test's premise is wrong: the sweep did not re-forget it"
    )


async def test_recall_of_a_recent_episode_does_not_refade(store):
    await store.episode_save(session_id="s", event_type="x", summary="fresh")
    service = MemoryDecayService(store, DecayConfig(enabled=True))
    eid = await _first_id(store)
    await service.forget(eid)
    result = await service.recall(eid)
    assert result["refades"] is False, result
    await service.run_once()
    assert [r["summary"] for r in await store.episode_recent(limit=10)] == ["fresh"]


async def _first_id(store) -> str:
    conn = await store._conn()
    try:
        async with conn.execute("SELECT id FROM episodes LIMIT 1") as cur:
            return (await cur.fetchone())["id"]
    finally:
        await store._release(conn)


async def _decay_factor(store) -> float:
    conn = await store._conn()
    try:
        async with conn.execute("SELECT decay_factor FROM episodes LIMIT 1") as cur:
            return float((await cur.fetchone())["decay_factor"])
    finally:
        await store._release(conn)
