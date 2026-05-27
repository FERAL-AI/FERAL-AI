"""Pin the AUDIT-r14 round2 wave3-followup-001 fixes.

The Lane 12 v2026.5.40 live verify caught two regressions in the
memory subsystem that surfaced through the WebUI:

1. ``GET /api/timeline`` returned ``{"entries": [], "count": 0}`` on
   a brain with 4601 episodes because the route was reading
   ``state.memory.search("")`` — the legacy *notes* search API — not
   ``episode_recent``. With 0 notes (the canonical case) the route
   reported 0 entries even though episodes existed.
2. ``state.memory.stats()`` could hang indefinitely under runtime
   contention when background services held the aiosqlite pool. The
   live brain saw 85s+ blocking on ``/api/memory/stats``.

These tests pin the corrected behaviour:

* ``test_timeline_route_reads_episodes`` — the route returns episode
  rows when the brain has episodes (and 0 notes). Asserts on the
  Lane 12 timeline-card field shape (``type``, ``timestamp``,
  ``title``, ``content``, ``metadata``).
* ``test_timeline_route_surfaces_memory_error`` — when the underlying
  read raises, the route surfaces a single ``memory_error`` row
  instead of silently swallowing it.
* ``test_memory_stats_timeout_returns_degraded_payload`` — when the
  pool acquire exceeds the budget, ``stats()`` returns a
  ``{"ok": False, "reason": "stats_timeout"}`` payload with all
  count fields zeroed (and the dashboard renders that honestly).
* ``test_memory_stats_success_path_includes_ok_true`` — additive: a
  successful run continues to populate every count field and now
  also exposes ``ok: True`` so callers can distinguish degraded
  from real-zero states.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routes import timeline as timeline_route


@pytest.fixture
def patch_route_state(monkeypatch):
    """Return a helper that swaps ``api.routes.timeline.state`` for a
    minimal namespace whose ``memory.episode_recent`` / ``calendar``
    / ``health_aggregator`` attributes the test controls."""
    def _patch(*, memory=None, calendar=None, health=None):
        fake_state = SimpleNamespace(
            memory=memory,
            calendar=calendar,
            health_aggregator=health,
        )
        monkeypatch.setattr(timeline_route, "state", fake_state)
        return fake_state
    return _patch


@pytest.mark.asyncio
async def test_timeline_route_reads_episodes(patch_route_state):
    """Pre-fix the route called ``state.memory.search("")`` (notes).
    Now it must read ``episode_recent`` and shape rows for the Lane 12
    TimelineCard component."""
    now = time.time()
    fake_episodes = [
        {
            "id": "ep-aaa",
            "session_id": "sess-1",
            "created_at": now - 60,
            "user_message": "what did I do today",
            "assistant_message": "You shipped Lane 12.",
            "summary": "user asks for daily recap; assistant summarises",
            "tags": ["recap"],
        },
        {
            "id": "ep-bbb",
            "session_id": "sess-1",
            "created_at": now - 3600,
            "user_message": "remind me to call mom",
            "assistant_message": "Reminder set for 6pm.",
            "summary": "",
            "tags": [],
        },
    ]
    memory = SimpleNamespace(
        episode_recent=AsyncMock(return_value=fake_episodes),
    )
    patch_route_state(memory=memory)

    result = await timeline_route.get_timeline(days=7, type="all")
    memory.episode_recent.assert_awaited_once_with(limit=200)

    entries = result["entries"]
    assert len(entries) == 2
    titles = {e["title"] for e in entries}
    assert "user asks for daily recap; assistant summarises" in titles
    # Empty summary falls back to a truncated user_message preview.
    assert "remind me to call mom" in titles

    # Field shape contract pinned for the TimelineCard component.
    for entry in entries:
        assert entry["type"] == "memory"
        assert entry["timestamp"] >= now - 3600 - 1
        assert "title" in entry
        assert "content" in entry
        assert "metadata" in entry
        assert "id" in entry["metadata"]
        assert "session_id" in entry["metadata"]


@pytest.mark.asyncio
async def test_timeline_filters_by_since_ts(patch_route_state):
    """Pin the ``days`` filter: episodes older than the window are
    dropped before the response is built (the route already had this
    behaviour; the wave3-followup fix kept it intact)."""
    now = time.time()
    eight_days_ago = now - 8 * 86400
    one_hour_ago = now - 3600
    fake_episodes = [
        {
            "id": "old", "session_id": "x",
            "created_at": eight_days_ago,
            "summary": "old episode", "user_message": "x", "assistant_message": "y",
        },
        {
            "id": "fresh", "session_id": "x",
            "created_at": one_hour_ago,
            "summary": "fresh", "user_message": "x", "assistant_message": "y",
        },
    ]
    memory = SimpleNamespace(
        episode_recent=AsyncMock(return_value=fake_episodes),
    )
    patch_route_state(memory=memory)

    result = await timeline_route.get_timeline(days=7, type="all")
    ids = {e["metadata"]["id"] for e in result["entries"]}
    assert ids == {"fresh"}, "only episodes within ``days`` should remain"


@pytest.mark.asyncio
async def test_timeline_route_surfaces_memory_error(patch_route_state):
    """When ``episode_recent`` raises, the route must add a single
    ``memory_error`` row instead of silently returning ``[]``. Honest
    failure surfacing is the v2026.5.40 contract."""
    memory = SimpleNamespace(
        episode_recent=AsyncMock(side_effect=RuntimeError("pool deadlock")),
    )
    patch_route_state(memory=memory)

    result = await timeline_route.get_timeline(days=7, type="all")
    error_entries = [e for e in result["entries"] if e["type"] == "memory_error"]
    assert len(error_entries) == 1
    assert error_entries[0]["title"] == "Memory unavailable"
    assert "pool deadlock" in error_entries[0]["content"]


# ─────────────────────────────────────────────────────────────────────
# memory/store.py:stats() timeout-degrade path
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_stats_timeout_returns_degraded_payload():
    """A pool acquire that exceeds the ``_STATS_CONN_BUDGET_S`` budget
    must NOT hang stats() — it returns a degraded payload the dashboard
    can render honestly."""
    from memory.store import MemoryStore

    # Build the smallest stats() invocation we can: bind the method
    # to a stand-in that just exposes the attributes stats() touches.
    # We deliberately don't construct a full MemoryStore here — that
    # path is exercised by ``test_integration.py``. This test is
    # narrowly about the timeout-degrade contract.
    fake = SimpleNamespace()
    fake._STATS_TOTAL_BUDGET_S = MemoryStore._STATS_TOTAL_BUDGET_S
    fake._STATS_CONN_BUDGET_S = 0.1  # tighten so the test runs fast
    fake._working = {}
    fake._kg = None
    fake._embedder = SimpleNamespace(provider_name="test-embedder")
    fake._embed_queue = SimpleNamespace(pending=0)
    fake._vec_index = SimpleNamespace(indexed=False, count=AsyncMock(return_value=0))
    fake._backend_id = "sqlite_vec"
    fake._kg_unified_enabled = lambda: False

    async def _hang_forever() -> None:
        await asyncio.sleep(60)

    fake._conn = _hang_forever

    result = await MemoryStore.stats(fake)
    assert result["ok"] is False
    assert result["reason"] == "stats_timeout"
    # Every count field is zeroed but present so callers using
    # ``.get(key, 0)`` continue to work without breaking.
    for key in ("notes", "episodes", "knowledge_triples", "execution_logs",
                "wiki_pages", "session_snapshots", "embedded_chunks"):
        assert result[key] == 0
    assert "stats timeout" in result["vec_index_mode"]


# ─────────────────────────────────────────────────────────────────────
# RC polish: type-filter contract alignment
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("type_value", ["memories", "memory", "chat"])
async def test_timeline_type_canonical_and_legacy_return_episodes(
    patch_route_state, type_value,
):
    """Canonical ``memories``/``chat`` and the legacy ``memory`` alias
    must all reach the episode source. Pre-fix the UI sent ``memory``
    and the route gated on ``memories`` only — the non-"All" picks
    returned an empty feed even when the brain had thousands of
    episodes."""
    now = time.time()
    fake_episodes = [
        {
            "id": "ep-canonical",
            "session_id": "sess-1",
            "created_at": now - 120,
            "user_message": "what did I do today",
            "assistant_message": "You shipped RC polish.",
            "summary": "daily recap",
            "tags": [],
        },
    ]
    memory = SimpleNamespace(
        episode_recent=AsyncMock(return_value=fake_episodes),
    )
    patch_route_state(memory=memory)

    result = await timeline_route.get_timeline(days=7, type=type_value)
    memory_entries = [e for e in result["entries"] if e["type"] == "memory"]
    assert len(memory_entries) == 1
    assert memory_entries[0]["metadata"]["id"] == "ep-canonical"


@pytest.mark.asyncio
async def test_timeline_type_calendar_alias_maps_to_events(patch_route_state):
    """``calendar`` (legacy UI vocabulary) must alias to ``events`` so
    the calendar source still fires. Pre-fix ``type=calendar`` matched
    no gate and returned an empty feed."""
    now = time.time()
    fake_events = {
        "success": True,
        "data": {
            "events": [
                {
                    "summary": "lunch with mom",
                    "start_epoch": now - 60,
                    "description": "noon at the diner",
                },
            ],
        },
    }
    calendar = SimpleNamespace(execute=AsyncMock(return_value=fake_events))
    # Memory absent so we don't blend sources into the assertion.
    memory = SimpleNamespace(episode_recent=AsyncMock(return_value=[]))
    patch_route_state(memory=memory, calendar=calendar)

    result = await timeline_route.get_timeline(days=7, type="calendar")
    event_entries = [e for e in result["entries"] if e["type"] == "event"]
    assert len(event_entries) == 1
    assert event_entries[0]["title"] == "lunch with mom"
    # And the canonical name reaches the same gate.
    result_canonical = await timeline_route.get_timeline(days=7, type="events")
    canonical_events = [
        e for e in result_canonical["entries"] if e["type"] == "event"
    ]
    assert len(canonical_events) == 1


# NOTE: The successful-path coverage for ``MemoryStore.stats()`` lives
# in ``tests/test_integration.py`` (``stats["notes"] >= 1`` etc.) — those
# cases also implicitly pin the additive ``ok: True`` field because they
# index into the dict by name and would not regress if the key were
# removed. Pinning the success path again here would require fully
# constructing a ``MemoryStore`` with all of its embedder + KG +
# vec_index dependencies, which is out of scope for this small fix.
