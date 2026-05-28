"""Pin the v2026.5.43 fused-timeline contract (S1 thesis closer).

The orchestrator dispatches ``notes_memory__fused_timeline`` for any
natural-language temporal-recall query. The fusion combines
episodes + notes + knowledge graph + calendar + health into a
single chronological card, gracefully skipping any source that
isn't configured. These tests pin:

* ``test_parses_yesterday_window`` — natural-language label →
  midnight-to-midnight range for the previous day.
* ``test_returns_episodes_and_notes_for_yesterday`` — fusion picks
  up rows from multiple memory sources within the window and drops
  rows outside it.
* ``test_gracefully_degrades_when_calendar_unconfigured`` —
  ``state.calendar`` absent → ``degraded_sources`` entry, episodes
  + notes still surface.
* ``test_per_source_cap_50`` — even with thousands of episodes
  in-window, no source exceeds the cap.
* ``test_screen_loop_always_degraded_v1`` — pinning the v1.0
  reality: ScreenLoop frame query API doesn't exist yet, so the
  source is always listed as degraded.
* ``test_entries_sorted_chronologically`` — final list is by
  ascending timestamp regardless of source-merge order.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.impl.timeline_fusion import (  # noqa: E402
    DEFAULT_PER_SOURCE_LIMIT,
    parse_window,
    timeline_fusion,
)


def _midnight(d: datetime) -> datetime:
    return datetime(d.year, d.month, d.day)


@pytest.fixture
def fixed_now():
    # Mid-afternoon on a known weekday — avoids DST edge weirdness
    # in the human-readable assertions.
    return datetime(2026, 5, 27, 15, 30, 0)  # Wed


# ─────────────────────────────────────────────────────────────────────
# parse_window
# ─────────────────────────────────────────────────────────────────────


def test_parses_yesterday_window(fixed_now):
    w = parse_window("yesterday", now=fixed_now)
    yest = fixed_now - timedelta(days=1)
    assert w["from_ts"] == _midnight(yest).timestamp()
    assert w["to_ts"] == _midnight(fixed_now).timestamp()
    assert w["label"] == "yesterday"


def test_parses_morning_window(fixed_now):
    w = parse_window("morning", now=fixed_now)
    today = _midnight(fixed_now)
    assert w["from_ts"] == (today + timedelta(hours=6)).timestamp()
    assert w["to_ts"] == (today + timedelta(hours=12)).timestamp()


def test_parses_last_tuesday(fixed_now):
    # fixed_now is Wednesday — "last tuesday" is the previous day.
    w = parse_window("last_tuesday", now=fixed_now)
    expected = _midnight(fixed_now) - timedelta(days=1)
    assert w["from_ts"] == expected.timestamp()
    assert w["to_ts"] == (expected + timedelta(days=1)).timestamp()


def test_explicit_from_to_overrides_label(fixed_now):
    f = (_midnight(fixed_now) - timedelta(days=5)).timestamp()
    t = _midnight(fixed_now).timestamp()
    w = parse_window("yesterday", from_ts=f, to_ts=t, now=fixed_now)
    assert w["from_ts"] == f
    assert w["to_ts"] == t


def test_unknown_label_defaults_to_yesterday(fixed_now):
    w = parse_window("zzzz_unknown", now=fixed_now)
    assert w["label"] == "yesterday"


# ─────────────────────────────────────────────────────────────────────
# timeline_fusion — core fan-out
# ─────────────────────────────────────────────────────────────────────


def _seed_episodes_around(now: datetime):
    """Two episodes yesterday + one 5 days ago. Mixed timestamps so the
    sort assertion has something to verify."""
    y_morning = (_midnight(now) - timedelta(days=1) + timedelta(hours=9)).timestamp()
    y_afternoon = (_midnight(now) - timedelta(days=1) + timedelta(hours=14)).timestamp()
    five_days_ago = (now - timedelta(days=5)).timestamp()
    return [
        {
            "id": "ep-old",
            "session_id": "s1",
            "created_at": five_days_ago,
            "summary": "ancient episode",
            "user_message": "irrelevant",
            "assistant_message": "—",
        },
        {
            "id": "ep-y2",
            "session_id": "s1",
            "created_at": y_afternoon,
            "summary": "yesterday afternoon",
            "user_message": "what should I do",
            "assistant_message": "shipped the timeline closer",
        },
        {
            "id": "ep-y1",
            "session_id": "s1",
            "created_at": y_morning,
            "summary": "",
            "user_message": "what time is standup",
            "assistant_message": "9am",
        },
    ]


def _seed_notes_around(now: datetime):
    y_noon = (_midnight(now) - timedelta(days=1) + timedelta(hours=12)).timestamp()
    return [
        {
            "id": "note-y1",
            "created_at": y_noon,
            "content": "yesterday note: pick up groceries",
            "tags": ["todo"],
            "importance": "normal",
        },
        {
            "id": "note-old",
            "created_at": (now - timedelta(days=14)).timestamp(),
            "content": "two weeks old",
            "tags": [],
            "importance": "low",
        },
    ]


def _build_memory(now: datetime):
    return SimpleNamespace(
        episode_recent=AsyncMock(return_value=_seed_episodes_around(now)),
        list_recent=AsyncMock(return_value=_seed_notes_around(now)),
    )


@pytest.mark.asyncio
async def test_returns_episodes_and_notes_for_yesterday(fixed_now):
    memory = _build_memory(fixed_now)
    result = await timeline_fusion(
        query="what did I do yesterday?",
        memory=memory,
        calendar=None,
        health_aggregator=None,
        window_label="yesterday",
        now=fixed_now,
    )
    entries = result["entries"]
    sources = {e["source"] for e in entries}
    ids = {e["metadata"].get("id") for e in entries if isinstance(e.get("metadata"), dict)}

    assert "episode" in sources
    assert "note" in sources
    assert "ep-y1" in ids and "ep-y2" in ids and "note-y1" in ids
    assert "ep-old" not in ids and "note-old" not in ids
    assert result["window"]["label"] == "yesterday"
    assert "episode" in result["sources_queried"]
    assert "note" in result["sources_queried"]


@pytest.mark.asyncio
async def test_entries_sorted_chronologically(fixed_now):
    memory = _build_memory(fixed_now)
    result = await timeline_fusion(
        query="recap",
        memory=memory,
        window_label="yesterday",
        now=fixed_now,
    )
    timestamps = [e["timestamp"] for e in result["entries"]]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_gracefully_degrades_when_calendar_unconfigured(fixed_now):
    memory = _build_memory(fixed_now)
    result = await timeline_fusion(
        query="yesterday",
        memory=memory,
        calendar=None,  # explicitly absent
        health_aggregator=None,
        window_label="yesterday",
        now=fixed_now,
    )
    degraded_sources = {d["source"] for d in result["degraded_sources"]}
    assert "calendar" in degraded_sources
    cal_reason = next(d["reason"] for d in result["degraded_sources"] if d["source"] == "calendar")
    assert cal_reason == "no_token"

    # Episodes / notes still surface.
    assert any(e["source"] == "episode" for e in result["entries"])
    assert any(e["source"] == "note" for e in result["entries"])


@pytest.mark.asyncio
async def test_gracefully_degrades_when_health_unconfigured(fixed_now):
    memory = _build_memory(fixed_now)
    result = await timeline_fusion(
        query="yesterday",
        memory=memory,
        health_aggregator=None,
        window_label="yesterday",
        now=fixed_now,
    )
    degraded = {d["source"] for d in result["degraded_sources"]}
    assert "health" in degraded
    health_reason = next(d["reason"] for d in result["degraded_sources"] if d["source"] == "health")
    assert health_reason == "no_provider"


@pytest.mark.asyncio
async def test_screen_loop_always_degraded_v1(fixed_now):
    """v1.0 reality: ScreenLoop has no range-query API. Pin that the
    source is always surfaced as degraded so the WebUI keeps an
    honest chip."""
    memory = _build_memory(fixed_now)
    result = await timeline_fusion(
        query="yesterday",
        memory=memory,
        window_label="yesterday",
        now=fixed_now,
    )
    assert {"source": "screen_loop", "reason": "no_query_api"} in result["degraded_sources"]


@pytest.mark.asyncio
async def test_per_source_cap_50(fixed_now):
    """Even with thousands of yesterday-stamped episodes in-window,
    no source exceeds ``per_source_limit``."""
    base_ts = (_midnight(fixed_now) - timedelta(days=1) + timedelta(hours=10)).timestamp()
    flood = [
        {
            "id": f"ep-{i}",
            "session_id": "s",
            "created_at": base_ts + i,  # 1s apart, still inside the day
            "summary": f"ep{i}",
            "user_message": "x",
            "assistant_message": "y",
        }
        for i in range(200)
    ]
    memory = SimpleNamespace(
        episode_recent=AsyncMock(return_value=flood),
        list_recent=AsyncMock(return_value=[]),
    )
    result = await timeline_fusion(
        query="yesterday",
        memory=memory,
        window_label="yesterday",
        now=fixed_now,
        per_source_limit=DEFAULT_PER_SOURCE_LIMIT,
    )
    ep_entries = [e for e in result["entries"] if e["source"] == "episode"]
    assert len(ep_entries) == DEFAULT_PER_SOURCE_LIMIT


@pytest.mark.asyncio
async def test_calendar_integration_consumed_when_present(fixed_now):
    memory = _build_memory(fixed_now)
    y_noon_ts = (_midnight(fixed_now) - timedelta(days=1) + timedelta(hours=12)).timestamp()
    calendar = SimpleNamespace(
        execute=AsyncMock(
            return_value={
                "success": True,
                "data": {
                    "events": [
                        {
                            "id": "cal-1",
                            "summary": "Lunch with mom",
                            "description": "noon at the diner",
                            "start_epoch": y_noon_ts,
                        },
                    ],
                },
            },
        ),
    )
    result = await timeline_fusion(
        query="yesterday",
        memory=memory,
        calendar=calendar,
        window_label="yesterday",
        now=fixed_now,
    )
    cal_entries = [e for e in result["entries"] if e["source"] == "calendar"]
    assert len(cal_entries) == 1
    assert cal_entries[0]["title"] == "Lunch with mom"

    # The calendar source is NOT in degraded_sources because the call succeeded.
    degraded = {d["source"] for d in result["degraded_sources"]}
    assert "calendar" not in degraded


@pytest.mark.asyncio
async def test_no_memory_emits_degraded_chips_but_does_not_crash(fixed_now):
    result = await timeline_fusion(
        query="yesterday",
        memory=None,
        window_label="yesterday",
        now=fixed_now,
    )
    sources_degraded = {d["source"] for d in result["degraded_sources"]}
    # Episodes, notes, knowledge are all gated on memory.
    assert {"episode", "note", "knowledge"}.issubset(sources_degraded)
    assert result["entries"] == []


@pytest.mark.asyncio
async def test_query_passed_through_to_result(fixed_now):
    memory = _build_memory(fixed_now)
    result = await timeline_fusion(
        query="what did I do yesterday?",
        memory=memory,
        window_label="yesterday",
        now=fixed_now,
    )
    assert result["query"] == "what did I do yesterday?"
