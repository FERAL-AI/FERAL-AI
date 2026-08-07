"""Near-identical results must not occupy multiple slots.

Incident: on a real store (12,296 episodes) a scheduled routine had
written the same robot command thousands of times: 843 rows reading
"set the CuteBot lights to green for 2 seconds, then turn them off",
820 reading "flash red on the CuteBot". Measured against a copy of that
store before the fix, ``episode_search_hybrid("coffee machine upkeep",
limit=5)`` returned five copies of the same lights sentence, so the
entire result set was one memory repeated and nothing about the query
survived to be shown.

``_mmr_rerank_episodes`` did not help. It penalises only shared
``session_id`` (all 843 repeats live in ONE session, so the penalty is
uniform and never reorders them) and it returns early without reranking
at all when the candidate pool is not larger than ``limit``.

The threshold justification lives next to
``MemoryStore.NEAR_DUPLICATE_JACCARD``; these tests pin the behaviour,
including that de-duplication is a READ-path filter which never touches
stored rows.
"""
from __future__ import annotations

import os
import tempfile
import time

import aiosqlite
import pytest

from memory.store import MemoryStore

pytestmark = pytest.mark.asyncio


async def _seed(path, rows):
    conn = await aiosqlite.connect(path)
    try:
        for eid, session_id, summary, detail in rows:
            await conn.execute(
                "INSERT INTO episodes (id, session_id, event_type, summary, "
                "detail, emotions, location, importance, created_at, "
                "decay_factor) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid, session_id, "note", summary, detail, "[]", "", 0.5,
                 time.time(), 1.0),
            )
        await conn.commit()
    finally:
        await conn.close()


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    MemoryStore(db_path=path)  # boot DDL
    yield path
    os.unlink(path)


# ── Unit level: the suppression pass itself ──────────────────────────


def _r(eid, summary, score, **extra):
    return {"id": eid, "summary": summary, "detail": "", "session_id": "s",
            "relevance_score": score, **extra}


async def test_identical_text_collapses_to_the_highest_scorer():
    ranked = [
        _r("a", "flash red on the CuteBot", 0.98),
        _r("b", "flash red on the CuteBot", 0.97),
        _r("c", "flash red on the CuteBot", 0.96),
        _r("d", "remind me to call the landlord", 0.40),
    ]
    kept = MemoryStore._suppress_near_duplicate_episodes(ranked)

    assert [e["id"] for e in kept] == ["a", "d"]
    # The survivor is the highest-scoring member of its cluster and says
    # how many it absorbed, so "one memory" stays distinguishable from
    # "one memory that happened 843 times".
    assert kept[0]["relevance_score"] == 0.98
    assert kept[0]["duplicates_suppressed"] == 2
    assert "duplicates_suppressed" not in kept[1]


async def test_parameter_variants_of_one_sentence_are_near_identical():
    """The real corpus repeats a template with a changing number
    ("obstacle 5.0cm ahead" / "9.0cm ahead"). Those score 0.826+ on the
    measured token-Jaccard and must collapse."""
    ranked = [
        _r("a", "The CuteBot robot detected an obstacle 5.0cm ahead and stopped.", 0.9),
        _r("b", "The CuteBot robot detected an obstacle 9.0cm ahead and stopped.", 0.8),
    ]
    kept = MemoryStore._suppress_near_duplicate_episodes(ranked)

    assert [e["id"] for e in kept] == ["a"]


async def test_distinct_memories_about_one_topic_all_survive():
    """The failure mode to avoid: this must not become a topic filter.
    These four all concern the robot and none restates another."""
    ranked = [
        _r("a", "flash red on the CuteBot", 0.9),
        _r("b", "The CuteBot detected an obstacle and stopped", 0.8),
        _r("c", "pair the CuteBot over bluetooth from my phone", 0.7),
        _r("d", "the CuteBot firmware build script failed on line 40", 0.6),
    ]
    kept = MemoryStore._suppress_near_duplicate_episodes(ranked)

    assert [e["id"] for e in kept] == ["a", "b", "c", "d"]


async def test_detail_envelope_is_not_compared():
    """Chat episodes share a byte-identical JSON ``detail`` envelope.
    Folding it into the comparison suppressed 2.4% of genuinely
    different chat pairs in the measurement, so only the summary counts.
    """
    envelope = ('{"source": "phone_surface", "mode": "phone_surface", '
                '"channel": "chat", "reply_mode": "stream"}')
    ranked = [
        dict(_r("a", "??", 0.9), detail=envelope),
        dict(_r("b", "What's my heart rate", 0.8), detail=envelope),
    ]
    kept = MemoryStore._suppress_near_duplicate_episodes(ranked)

    assert [e["id"] for e in kept] == ["a", "b"]


async def test_summaryless_rows_fall_back_to_detail():
    ranked = [
        _r("a", "", 0.9, detail="CuteBot: set_lights (r=0, g=0, b=0)"),
        _r("b", "", 0.8, detail="CuteBot: set_lights (r=0, g=0, b=0)"),
        _r("c", "", 0.7, detail="battery at 12 percent, charging"),
    ]
    kept = MemoryStore._suppress_near_duplicate_episodes(ranked)

    assert [e["id"] for e in kept] == ["a", "c"]


async def test_max_kept_bounds_the_scan():
    """Without a bound this pass is O(kept x scanned), and the pool it
    receives is not small: with ``indexed=False`` the vector leg admits
    every chunk over cosine 0.25 with no top-k cut, which on the
    reporter's store is 8,581 candidates for a limit=5 query (9.4s
    unbounded vs 4.3ms at max_kept=50, same top 5)."""
    ranked = [_r(str(i), f"distinct memory number {i}", 1.0 - i / 10000)
              for i in range(5000)]

    kept = MemoryStore._suppress_near_duplicate_episodes(ranked, max_kept=50)

    assert len(kept) == 50
    assert [e["id"] for e in kept] == [str(i) for i in range(50)]


async def test_ranked_order_is_preserved():
    """Suppression removes, it never reorders: MMR runs after this and
    relies on the list still being best-first."""
    ranked = [_r(str(i), f"distinct memory number {i}", 1.0 - i / 100)
              for i in range(10)]
    kept = MemoryStore._suppress_near_duplicate_episodes(ranked)

    assert [e["id"] for e in kept] == [e["id"] for e in ranked]


# ── End to end through episode_search_hybrid ─────────────────────────


async def test_hybrid_search_does_not_return_three_copies(db_path):
    """The reported symptom, reproduced in miniature: a routine writes
    the same command many times inside ONE session (which is why the
    session-based MMR penalty never separated them)."""
    repeats = [
        (f"rep{i}", "routine-11",
         "set the CuteBot lights to green for 2 seconds, then turn them off",
         '{"source": "cron", "routine_id": 11}')
        for i in range(12)
    ]
    others = [
        ("real1", "chat-1", "the espresso machine needs descaling every month",
         "note about the coffee machine"),
        ("real2", "chat-2", "ordered new filters for the coffee grinder", ""),
    ]
    await _seed(db_path, repeats + others)

    store = MemoryStore(db_path=db_path)
    try:
        results = await store.episode_search_hybrid("cutebot lights", limit=5)
    finally:
        await store.aclose()

    summaries = [r["summary"] for r in results]
    assert len(summaries) == len(set(summaries)), (
        f"hybrid search returned duplicate texts: {summaries}"
    )
    lights = [s for s in summaries if s.startswith("set the CuteBot lights")]
    assert len(lights) == 1, f"the repeated command took {len(lights)} slots"


async def test_dedup_frees_slots_for_other_memories(db_path):
    """Suppression is only worth doing if the freed slots get used."""
    repeats = [
        (f"rep{i}", "routine-11", "flash red on the CuteBot",
         '{"source": "cron", "routine_id": 12}')
        for i in range(20)
    ]
    others = [
        ("o1", "chat-1", "pair the CuteBot over bluetooth from my phone", ""),
        ("o2", "chat-2", "the CuteBot firmware build failed", ""),
        ("o3", "chat-3", "CuteBot obstacle sensor reads 5cm", ""),
    ]
    await _seed(db_path, repeats + others)

    store = MemoryStore(db_path=db_path)
    try:
        results = await store.episode_search_hybrid("cutebot", limit=4)
    finally:
        await store.aclose()

    ids = {r["id"] for r in results}
    assert len(ids & {"o1", "o2", "o3"}) >= 2, (
        f"the repeated command still crowded out the distinct memories: {ids}"
    )


async def test_dedup_never_deletes_stored_rows(db_path):
    """Read-path only. Every suppressed episode must still be in the
    table afterwards, and must still be findable when it is the best
    answer available."""
    repeats = [
        (f"rep{i}", "routine-11", "flash red on the CuteBot",
         '{"source": "cron", "routine_id": 12}')
        for i in range(15)
    ]
    await _seed(db_path, repeats)

    store = MemoryStore(db_path=db_path)
    try:
        await store.episode_search_hybrid("flash red", limit=5)
    finally:
        await store.aclose()

    conn = await aiosqlite.connect(db_path)
    try:
        async with conn.execute("SELECT COUNT(*) FROM episodes") as cur:
            (count,) = await cur.fetchone()
    finally:
        await conn.close()

    assert count == 15, "de-duplication must not remove stored episodes"
