"""PR 2 (v2026.5.34) F2 acceptance — real session compaction.

These tests pin the contract laid out in the master plan:

  1. ``compact_session`` no longer just edits the in-memory transcript
     — it persists a real episode row whose ``event_type`` is
     ``"session_compaction"`` and whose detail body contains the
     summary plus a structured metadata block (time_range,
     key_entities, source_turn_ids).
  2. The promoted episode is queryable through the normal
     ``episode_search`` / ``episode_recent`` APIs (i.e. the row is a
     first-class memory, not a side-table).
  3. The promotion is opt-out — ``promote_to_episode=False`` returns
     a successful compaction with ``episode_id=None`` and writes no
     episode row.
  4. The compaction is idempotent in shape: running it twice on the
     same history produces two distinct episodes (each compaction is
     a real event), but both share identical participants and
     summary metadata. (We don't dedupe — episodes are events.)
  5. Short histories below the preserve_last_n+2 threshold short-
     circuit (no episode, ``compacted=False``).
  6. ``participants`` is the de-duped set of message ``role``s from
     the summarisable window.
  7. ``time_range`` reflects the min/max of ``meta.created_at`` on
     the summarisable messages when present.
  8. ``source_turn_ids`` matches the indices (or message ids) of the
     summarisable window.
"""

from __future__ import annotations

import json
import time

import pytest

from memory.context_builder import compact_session
from memory.store import MemoryStore


def _make_history(n: int, *, with_ts: bool = False, with_ids: bool = False) -> list[dict]:
    base = time.time() - n
    out = []
    for i in range(n):
        msg = {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}: " + ("word " * 12).strip(),
        }
        if with_ts:
            msg["meta"] = {"created_at": base + i}
        if with_ids:
            msg["id"] = f"m{i:03d}"
        out.append(msg)
    return out


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "compact.db"))
    try:
        yield s
    finally:
        await s.aclose()


# ── 1. promotion writes a real episode row ────────────────────────────


@pytest.mark.asyncio
async def test_compaction_promotes_to_episode(store):
    history = _make_history(12, with_ts=True, with_ids=True)
    result = await compact_session(
        store, session_id="sess-1", history=history, llm=None,
        preserve_last_n=3,
    )

    assert result["compacted"] is True
    assert result.get("episode_id"), "F2 must promote a real episode"
    episode_id = result["episode_id"]

    recent = await store.episode_recent(limit=5)
    promoted = [e for e in recent if e["id"] == episode_id]
    assert promoted, "promoted episode should be visible via episode_recent"
    row = promoted[0]
    assert row["event_type"] == "session_compaction"
    assert row["session_id"] == "sess-1"

    # The detail body carries the metadata block.
    detail = row["detail"] or ""
    assert "<!-- compaction-metadata" in detail
    meta_block = detail.split("<!-- compaction-metadata", 1)[1]
    meta_json = meta_block.split("-->", 1)[0].strip()
    extras = json.loads(meta_json)
    assert "time_range" in extras and len(extras["time_range"]) == 2
    assert isinstance(extras.get("source_turn_ids"), list)
    assert len(extras["source_turn_ids"]) == 9  # 12 - preserve_last_n=3


# ── 2. promoted episode is searchable like any other ──────────────────


@pytest.mark.asyncio
async def test_promoted_episode_is_searchable(store):
    history = _make_history(15, with_ts=True)
    # Inject a distinctive token the summarizer will keep.
    history[2]["content"] = "the magic distinctive token MEMORANDUM"
    await compact_session(store, "sess-2", history, llm=None, preserve_last_n=3)

    hits = await store.episode_search("MEMORANDUM", limit=10)
    matched = [h for h in hits if h["event_type"] == "session_compaction"]
    assert matched, "the compaction episode should be retrievable by the search index"


# ── 3. promote_to_episode=False opts out ──────────────────────────────


@pytest.mark.asyncio
async def test_promote_to_episode_false(store):
    history = _make_history(12)
    before = len(await store.episode_recent(limit=100))
    result = await compact_session(
        store, "sess-3", history, llm=None,
        preserve_last_n=3, promote_to_episode=False,
    )
    after = len(await store.episode_recent(limit=100))
    assert result["compacted"] is True
    assert result["episode_id"] is None
    assert after == before, "no episode should land when opt-out is set"


# ── 4. each compaction is a fresh event (no dedupe) ───────────────────


@pytest.mark.asyncio
async def test_each_compaction_is_a_fresh_event(store):
    history = _make_history(12)
    r1 = await compact_session(store, "sess-4", history, preserve_last_n=3)
    r2 = await compact_session(store, "sess-4", history, preserve_last_n=3)
    assert r1["episode_id"] and r2["episode_id"]
    assert r1["episode_id"] != r2["episode_id"]


# ── 5. short history short-circuits ────────────────────────────────────


@pytest.mark.asyncio
async def test_short_history_short_circuits(store):
    history = _make_history(4)
    result = await compact_session(store, "sess-5", history, preserve_last_n=3)
    assert result["compacted"] is False
    assert result["reason"] == "too_short"
    # No episode landed.
    eps = await store.episode_recent(limit=10)
    assert not [e for e in eps if e["event_type"] == "session_compaction"]


# ── 6. participants are the de-duped roles ────────────────────────────


@pytest.mark.asyncio
async def test_participants_are_deduped_roles(store):
    history = _make_history(10)
    result = await compact_session(store, "sess-6", history, preserve_last_n=3)
    episode_id = result["episode_id"]
    eps = await store.episode_recent(limit=20)
    row = next(e for e in eps if e["id"] == episode_id)
    participants = row.get("participants") or []
    assert set(participants) == {"user", "assistant"}


# ── 7. time_range honours meta.created_at ─────────────────────────────


@pytest.mark.asyncio
async def test_time_range_from_meta(store):
    history = _make_history(10, with_ts=True)
    summarizable = history[:-3]
    expected_min = summarizable[0]["meta"]["created_at"]
    expected_max = summarizable[-1]["meta"]["created_at"]
    result = await compact_session(store, "sess-7", history, preserve_last_n=3)

    eps = await store.episode_recent(limit=5)
    row = next(e for e in eps if e["id"] == result["episode_id"])
    extras_block = row["detail"].split("<!-- compaction-metadata", 1)[1]
    extras = json.loads(extras_block.split("-->", 1)[0].strip())
    tr = extras["time_range"]
    assert abs(tr[0] - expected_min) < 0.01
    assert abs(tr[1] - expected_max) < 0.01


# ── 8. source_turn_ids honours message ids when present ───────────────


@pytest.mark.asyncio
async def test_source_turn_ids_use_message_ids(store):
    history = _make_history(10, with_ids=True)
    result = await compact_session(store, "sess-8", history, preserve_last_n=3)
    eps = await store.episode_recent(limit=5)
    row = next(e for e in eps if e["id"] == result["episode_id"])
    extras = json.loads(row["detail"].split("<!-- compaction-metadata", 1)[1].split("-->", 1)[0].strip())
    ids = extras["source_turn_ids"]
    # Summarizable window is history[:-3] → m000..m006
    assert ids == [f"m{i:03d}" for i in range(7)]
