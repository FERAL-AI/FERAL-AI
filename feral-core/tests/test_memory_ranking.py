"""Regression tests for hybrid episode ranking.

Incident (v2026.7.x): ``episode_search_hybrid`` blended
``0.3 * fts_score + 0.7 * vec_score`` and scaled the result by an
exponential temporal factor. Four independent sign/scale errors all
pushed the ranking the wrong way:

a) ``fts_score = 1/(1 + abs(rank))``. FTS5's ``rank`` is BM25:
   negative, and more negative means a better match, so ``abs()``
   reversed the ordering outright.
b) ``decay_factor`` sat in the *exponent*. It is retention strength in
   (0, 1], so a nearly-forgotten memory got a smaller effective decay
   rate and therefore ranked higher.
c) The rate was 0.01/hour (a 69-hour half-life) against
   ``decay.DecayConfig.decay_rate``'s 0.001, two constants for one
   concept, 10x apart, which let fresh noise outrank old signal.
d) The raw utterance went straight into ``MATCH ?`` with failures
   swallowed by ``except Exception: pass``, so every query containing
   an apostrophe or an FTS5 operator character silently contributed
   nothing from the text leg.

Scoring is now Reciprocal Rank Fusion over the two legs' *orderings*
plus a bounded additive recency prior. These tests pin the four
directional properties, not the exact scores: RRF's absolute values are
an implementation detail, but "the better match ranks higher" is the
contract.
"""
import os
import tempfile
import time

import aiosqlite
import pytest

from memory.decay import DecayConfig
from memory.fts_query import fts5_match_query
from memory.store import DEFAULT_DECAY_RATE, MemoryStore

HOUR = 3600.0


async def _seed(path, rows):
    """Insert episodes directly so created_at / decay_factor are exact.

    ``episode_save`` stamps its own timestamps; these tests need to
    place rows at specific ages and retention levels.
    """
    conn = await aiosqlite.connect(path)
    try:
        for eid, summary, detail, created_at, decay_factor in rows:
            await conn.execute(
                "INSERT INTO episodes (id, session_id, event_type, summary, "
                "detail, emotions, location, importance, created_at, "
                "decay_factor) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid, eid, "note", summary, detail, "[]", "", 0.5,
                 created_at, decay_factor),
            )
        await conn.commit()
    finally:
        await conn.close()


def _distractors(count, now):
    """Rows that never match the query.

    BM25's IDF term collapses toward zero when every row matches, which
    flattens ``rank`` to ~1e-6 and hides ordering bugs. Real corpora
    have non-matching rows; these supply them.
    """
    return [
        (f"d{i}", f"unrelated subject {i}", f"nothing relevant here {i}", now, 1.0)
        for i in range(count)
    ]


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    MemoryStore(db_path=path)  # runs the boot DDL
    yield path
    os.unlink(path)


async def test_bm25_ordering_is_not_inverted(db_path):
    """(a) The strongest lexical match must rank first.

    e1..e4 mention "wallet" with strictly decreasing term frequency
    relative to document length, so BM25 orders them e1 > e2 > e3 > e4.
    The ``abs(rank)`` bug produced exactly the reverse.
    """
    now = time.time()
    await _seed(db_path, [
        ("e1", "wallet", "wallet wallet wallet wallet wallet wallet", now, 1.0),
        ("e2", "wallet", "wallet wallet wallet", now, 1.0),
        ("e3", "wallet", "wallet", now, 1.0),
        ("e4", "wallet " + "unrelated filler prose " * 40,
         "more filler prose " * 40, now, 1.0),
    ] + _distractors(24, now))

    store = MemoryStore(db_path=db_path)
    try:
        results = await store.episode_search_hybrid("wallet", limit=4)
    finally:
        await store.aclose()

    assert [r["id"] for r in results] == ["e1", "e2", "e3", "e4"], (
        "BM25 ordering inverted: FTS5 rank is negative-is-better and the "
        "ranking must not fold that sign away"
    )


async def test_healthy_memory_outranks_nearly_forgotten_one(db_path):
    """(b) ``decay_factor`` is retention: higher must rank higher.

    Two rows, identical text and identical age, differing only in
    retention. The bug put ``decay_factor`` in the exponent, so the
    nearly-forgotten row decayed *slower* and won by ~423x.
    """
    now = time.time()
    age = now - 30 * 24 * HOUR
    await _seed(db_path, [
        ("healthy", "wallet", "wallet", age, 0.90),
        ("forgotten", "wallet", "wallet", age, 0.06),
    ] + _distractors(24, now))

    store = MemoryStore(db_path=db_path)
    try:
        results = await store.episode_search_hybrid("wallet", limit=4)
    finally:
        await store.aclose()

    scores = {r["id"]: r["relevance_score"] for r in results}
    assert scores["healthy"] > scores["forgotten"], (
        f"retention inverted: healthy={scores['healthy']} "
        f"forgotten={scores['forgotten']}"
    )


async def test_strong_old_match_beats_weak_fresh_match(db_path):
    """(c) Recency is a tiebreaker, not the dominant term.

    A tight 14-day-old match must beat a bloated one-hour-old match.
    With a 69-hour half-life and a multiplicative temporal factor, age
    swamped relevance and the fresh noise won.
    """
    now = time.time()
    await _seed(db_path, [
        ("strong_old", "wallet wallet wallet", "wallet wallet wallet",
         now - 14 * 24 * HOUR, 1.0),
        ("weak_fresh", "wallet " + "noise " * 200, "noise " * 200,
         now - HOUR, 1.0),
    ] + _distractors(24, now))

    store = MemoryStore(db_path=db_path)
    try:
        results = await store.episode_search_hybrid("wallet", limit=4)
    finally:
        await store.aclose()

    assert results[0]["id"] == "strong_old", (
        "a one-hour-old weak match outranked a strong 14-day-old one; the "
        "recency prior is no longer bounded"
    )


@pytest.mark.parametrize("query,expected_id", [
    ("don't", "q1"),
    ("what's", "q2"),
    ("C++", "q3"),
    ("AI/ML", "q4"),
    ("(urgent) ping", "q5"),
])
async def test_fts_operator_characters_still_reach_the_text_leg(
    db_path, query, expected_id
):
    """(d) Ordinary queries must not silently kill the text leg.

    Each of these raises ``fts5: syntax error`` when passed to MATCH
    unquoted. The old bare ``except Exception: pass`` turned that into
    an empty result set with no log line.
    """
    now = time.time()
    await _seed(db_path, [
        ("q1", "I don't know where the wallet is", "don't", now, 1.0),
        ("q2", "what's the plan for friday", "what's", now, 1.0),
        ("q3", "C++ build broke again", "C++ toolchain", now, 1.0),
        ("q4", "AI/ML pipeline notes", "AI/ML", now, 1.0),
        ("q5", "(urgent) ping the oncall", "urgent ping", now, 1.0),
    ])

    store = MemoryStore(db_path=db_path)
    try:
        results = await store.episode_search_hybrid(query, limit=5)
    finally:
        await store.aclose()

    assert expected_id in [r["id"] for r in results], (
        f"query {query!r} returned {[r['id'] for r in results]}; the FTS "
        f"leg was dropped by a syntax error"
    )


async def test_decay_rate_is_reconciled_with_the_decay_service():
    """(c) One concept, one constant.

    ``store.DEFAULT_DECAY_RATE`` (search-time recency) and
    ``decay.DecayConfig.decay_rate`` (the sweep that writes
    ``decay_factor``) model the same Ebbinghaus curve over the same
    rows. They were 0.01 and 0.001, a 10x disagreement about how fast
    memory fades. This test exists so they cannot drift apart again.
    """
    assert DEFAULT_DECAY_RATE == DecayConfig.decay_rate, (
        f"store.DEFAULT_DECAY_RATE={DEFAULT_DECAY_RATE} disagrees with "
        f"decay.DecayConfig.decay_rate={DecayConfig.decay_rate}"
    )


@pytest.mark.parametrize("raw", [
    "don't", "what's", "C++", "AI/ML", "(urgent)", 'say "hi"',
    "a-b", "50%", "foo*", "NEAR(x y)", "back\\slash",
])
async def test_fts5_match_query_output_is_always_valid(db_path, raw):
    """The sanitizer's contract: whatever goes in, FTS5 accepts it.

    Exercised against a real FTS5 table rather than by inspecting the
    string, because the only authority on FTS5 syntax is FTS5.
    """
    expr = fts5_match_query(raw)
    assert expr, f"{raw!r} produced an empty expression"
    conn = await aiosqlite.connect(db_path)
    try:
        # Must not raise. Whether it matches anything is irrelevant here.
        async with conn.execute(
            "SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH ? LIMIT 1",
            (expr,),
        ) as cur:
            await cur.fetchall()
    finally:
        await conn.close()


@pytest.mark.parametrize("query", ["don't", "C++", "AI/ML", "(urgent)"])
async def test_wiki_search_does_not_500_on_operator_characters(db_path, query):
    """``wiki_list_pages`` had no guard around its MATCH at all.

    Unlike the episode/note paths, which swallowed the syntax error,
    this one let ``sqlite3.OperationalError`` propagate straight out of
    ``GET /api/wiki/pages?q=...`` as a 500.
    """
    store = MemoryStore(db_path=db_path)
    try:
        await store.wiki_upsert_page(
            page_id="p1", title="C++ and AI/ML notes", kind="note",
            body_markdown="I don't know. (urgent)",
        )
        pages = await store.wiki_list_pages(query=query, limit=10)
    finally:
        await store.aclose()
    assert [p["id"] for p in pages] == ["p1"]


def test_fts5_match_query_drops_terms_with_no_indexable_content():
    """Punctuation-only input tokenizes to nothing.

    FTS5 rejects an empty phrase, so these must be dropped rather than
    quoted. Callers treat ``""`` as "no text query" and skip the MATCH.
    """
    assert fts5_match_query("+++") == ""
    assert fts5_match_query("   ") == ""
    assert fts5_match_query("") == ""
    # A real term alongside noise survives.
    assert fts5_match_query("+++ wallet") == '"wallet"'
