"""B7: session eviction must drop the STALEST session, not the shortest.

``Orchestrator._evict_stale_sessions`` is named for staleness and its
docstring promised "evict oldest conversation sessions". It sorted by
``len(self.conversation_history[sid])`` and deleted the head of that
list, i.e. the sessions with the FEWEST rows. A session with few rows is
usually the one that just started, so the cap evicted the session the
operator is talking to right now and kept five abandoned ones.

The dict grows in the first place because ``on_session_disconnect`` has
exactly one caller, the WebSocket handler in ``api/server.py``. Channel
sessions (``channel_{type}_{user_id}``), cron sessions (``routine-{id}``)
and plain REST turns never disconnect, so they accumulate until the cap
fires. That makes the cap the ONLY cleanup those surfaces get, and it was
picking the wrong victim.

These tests assert the behaviour: with the cap exceeded, the fresh
session survives and the abandoned ones go.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.orchestrator import Orchestrator  # noqa: E402


def _bare_orchestrator(max_sessions: int = 5) -> Orchestrator:
    """An Orchestrator shell holding only the session bookkeeping these
    tests touch. Constructing a real one boots the whole agent stack."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.conversation_history = {}
    orch._conversation_max_sessions = max_sessions
    orch._conversation_max_per_session = 200
    orch._session_locks = {}
    orch._session_surfaces = {}
    # The consolidation ladder's clocks. Eviction clears these too:
    # the background tick iterates them, so a stale entry would be a
    # compaction retried forever against a transcript that is gone.
    orch._turns_since_compaction = {}
    orch._pending_since = {}
    orch._session_last_turn_at = {}
    orch._compaction_inflight = {}
    orch._tool_result_images = {}
    orch._tool_image_order = {}
    orch._tool_image_rounds = {}
    orch._session_last_active = {}
    return orch


def _seed(orch: Orchestrator, sid: str, rows: int, *, age_s: float) -> None:
    """Give ``sid`` a transcript of ``rows`` messages last touched
    ``age_s`` seconds ago."""
    orch.conversation_history[sid] = [
        {"role": "user", "content": f"{sid}-{i}"} for i in range(rows)
    ]
    orch._session_last_active[sid] = time.time() - age_s


def test_the_fresh_session_survives_and_the_stale_ones_go():
    """The measured case: five long-but-abandoned sessions plus one
    one-row session the operator is actively using."""
    orch = _bare_orchestrator(max_sessions=5)
    for i in range(5):
        _seed(orch, f"stale-{i}", rows=50, age_s=3600 + i)
    _seed(orch, "fresh", rows=1, age_s=0.0)

    orch._evict_stale_sessions()

    assert "fresh" in orch.conversation_history, (
        "the session the operator is talking to right now was evicted"
    )
    assert len(orch.conversation_history) == 5
    # The single oldest session is the one that had to go.
    assert "stale-4" not in orch.conversation_history


def test_eviction_order_is_oldest_first():
    """Several sessions over the cap: the N oldest go, in age order."""
    orch = _bare_orchestrator(max_sessions=2)
    _seed(orch, "oldest", rows=1, age_s=900)
    _seed(orch, "middle", rows=99, age_s=600)
    _seed(orch, "newer", rows=1, age_s=300)
    _seed(orch, "newest", rows=99, age_s=0)

    orch._evict_stale_sessions()

    assert set(orch.conversation_history) == {"newer", "newest"}


def test_eviction_drops_the_side_tables_too():
    """Whatever is evicted must take its locks, surfaces, images and
    activity stamp with it, or the caps leak."""
    orch = _bare_orchestrator(max_sessions=1)
    _seed(orch, "old", rows=5, age_s=1000)
    _seed(orch, "new", rows=5, age_s=0)
    orch._session_surfaces["old"] = "channel"
    orch._session_locks["old"] = object()
    orch._tool_result_images["old"] = {"call-1": {"images": ["x"]}}
    orch._tool_image_order["old"] = ["call-1"]
    orch._tool_image_rounds["old"] = {}

    orch._evict_stale_sessions()

    assert set(orch.conversation_history) == {"new"}
    assert "old" not in orch._session_surfaces
    assert "old" not in orch._session_locks
    assert "old" not in orch._tool_result_images
    assert "old" not in orch._session_last_active, (
        "the activity map must not outlive the sessions it describes"
    )


def test_a_session_with_no_stamp_is_treated_as_stale():
    """A session that never recorded activity (pre-upgrade snapshot,
    direct history poke) must not be treated as infinitely fresh and
    pin the cap forever."""
    orch = _bare_orchestrator(max_sessions=1)
    orch.conversation_history["unstamped"] = [{"role": "user", "content": "?"}]
    _seed(orch, "stamped", rows=1, age_s=5)

    orch._evict_stale_sessions()

    assert set(orch.conversation_history) == {"stamped"}


def test_under_the_cap_nothing_is_evicted():
    orch = _bare_orchestrator(max_sessions=5)
    for i in range(5):
        _seed(orch, f"s{i}", rows=1, age_s=i)
    orch._evict_stale_sessions()
    assert len(orch.conversation_history) == 5


# ── the activity stamp has to be written somewhere ──────────────────


def test_finalize_turn_records_activity():
    """``_finalize_turn`` is where a turn is committed, so it is where
    the session's last-activity stamp belongs. Without a write here the
    map is empty and the sort is meaningless."""
    orch = _bare_orchestrator(max_sessions=5)
    orch._active_turns = {}
    orch.learner = None
    orch._session_snapshot_hook = None
    orch._maybe_snapshot_primary = lambda sid: None
    orch._maybe_auto_compact = lambda sid: None
    orch._persist_assistant_rows = lambda sid, rows: None
    orch._turn_rows = lambda turn: [{"role": "assistant", "content": "hi"}]

    before = time.time()
    orch._finalize_turn("s-new", {"text": "hello"})

    assert "s-new" in orch._session_last_active
    assert orch._session_last_active["s-new"] >= before


async def test_voice_turn_records_activity():
    """A live-voice session is text-silent on ``_finalize_turn`` but is
    very much active; its rows land through ``_append_voice_row``."""
    import asyncio

    orch = _bare_orchestrator(max_sessions=5)
    orch._session_locks = {}
    orch._get_session_lock = lambda sid: orch._session_locks.setdefault(
        sid, asyncio.Lock()
    )

    before = time.time()
    await orch._append_voice_row("voice-1", "user", "turn the light on")

    assert orch._session_last_active.get("voice-1", 0) >= before


@pytest.mark.parametrize("attr", ["_session_last_active"])
def test_the_map_is_initialised_by_the_real_constructor(attr):
    """The shells above set it by hand; production must too."""
    import inspect

    source = inspect.getsource(Orchestrator.__init__)
    assert attr in source, f"{attr} is never initialised in __init__"
