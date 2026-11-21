"""episode_recent filters by type and time in SQL, before the LIMIT.

Screen-capture episodes are about 90% of a day's rows. "Newest N, then keep
the conversation ones" returns almost nothing, so the digest endpoint needs
the filter applied by the query itself. Defaults must not change for the
existing callers (timeline, briefing, context builder, workspace).
"""
from __future__ import annotations

import time

import pytest

from memory.store import MemoryStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "recent-filters.db"))
    try:
        yield s
    finally:
        await s.aclose()


async def _seed(store):
    now = time.time()
    for i in range(30):
        await store.episode_save("screen_loop", "screen_general", f"window title {i}", created_at=now - i)
    await store.episode_save("primary", "user_command", "is my cutebot connected", created_at=now - 100)
    await store.episode_save("voice-phone", "assistant_reply", "The CuteBot is not connected.", created_at=now - 90)
    await store.episode_save("primary", "user_command", "what did I do last week", created_at=now - 10 * 86400)
    return now


async def test_type_filter_applies_before_the_limit(store):
    await _seed(store)
    rows = await store.episode_recent(limit=5, event_types=("user_command", "assistant_reply"))
    assert [r["summary"] for r in rows] == [
        "The CuteBot is not connected.",
        "is my cutebot connected",
        "what did I do last week",
    ]


async def test_since_excludes_older_episodes(store):
    now = await _seed(store)
    rows = await store.episode_recent(
        limit=50, event_types=("user_command", "assistant_reply"), since=now - 86400,
    )
    assert "what did I do last week" not in [r["summary"] for r in rows]
    assert len(rows) == 2


async def test_session_and_type_compose(store):
    await _seed(store)
    rows = await store.episode_recent(limit=50, session_id="primary", event_types=("user_command",))
    assert {r["session_id"] for r in rows} == {"primary"}
    assert len(rows) == 2


async def test_defaults_are_unchanged(store):
    await _seed(store)
    rows = await store.episode_recent(limit=3)
    assert len(rows) == 3
    assert all(r["event_type"] == "screen_general" for r in rows)
    created = [r["created_at"] for r in rows]
    assert created == sorted(created, reverse=True)
