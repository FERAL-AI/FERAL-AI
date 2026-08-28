"""B9 — the primary-thread snapshot debounce dropped turns permanently.

``SessionSnapshotStore.save`` opened with

    if not force and (now - self._last_save_ts) < 2.5:
        return False

which is a leading-edge-only debounce: the first call in a window
writes, every later call is discarded, and NOTHING is scheduled to write
the discarded state afterwards. It is not "the write is deferred", it is
"the write never happens". Measured against the unfixed store: five turns
in rapid succession returned True, False, False, False, False, leaving
one turn on disk while RAM held five.

The second half is the shutdown flush that ``BrainState.snapshot_primary_thread``
already documents:

    "Called from the orchestrator after each successful turn, and on
     FastAPI shutdown ... force=True bypasses debounce - used on shutdown
     to guarantee the last turn lands."

The FastAPI ``shutdown_event`` never called it. The only ``force=True``
in the tree was on WebSocket disconnect, primary session only, so every
surface without a WS attachment (CLI, headless, channels, cron) and every
SIGTERM / ``feral stop`` lost whatever the debounce had swallowed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.session_snapshot import SessionSnapshotStore  # noqa: E402


def _turns(n: int) -> list[dict]:
    return [{"role": "user", "content": f"turn-{i}"} for i in range(n)]


@pytest.fixture
def store(tmp_path):
    s = SessionSnapshotStore(tmp_path)
    # Production debounce is 2.5s; the defect is in the SHAPE of the
    # debounce, not its length, so shorten it to keep the test fast.
    s._min_save_interval_s = 0.05
    yield s
    s.close()


def _disk(store) -> dict:
    return json.loads(store.path.read_text())


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── trailing edge ────────────────────────────────────────────────────


def test_the_fifth_rapid_turn_reaches_disk(store):
    """The measured case. Five saves inside one debounce window: the
    last one must land, not be discarded."""
    for n in range(1, 6):
        store.save("primary", conversation_history=_turns(n))

    assert _wait_for(
        lambda: store.path.is_file() and len(
            _disk(store)["conversation_history"]
        ) == 5
    ), (
        "the snapshot still holds "
        f"{len(_disk(store)['conversation_history']) if store.path.is_file() else 0}"
        " turns; the debounced turns were dropped, not deferred"
    )


def test_the_trailing_save_carries_the_newest_state(store):
    """Not just "a write happened" - it must be the LATEST state, since
    the point is that the operator's last turn survives."""
    store.save("primary", conversation_history=_turns(1))
    store.save("primary", conversation_history=[{"role": "user", "content": "stale"}])
    store.save("primary", conversation_history=[{"role": "user", "content": "newest"}])

    assert _wait_for(
        lambda: store.path.is_file()
        and _disk(store)["conversation_history"] == [
            {"role": "user", "content": "newest"}
        ]
    )


def test_working_memory_half_also_lands(store):
    """Both lists the snapshot carries, not only the transcript."""
    store.save("primary", conversation_history=_turns(1), working_memory=[{"a": 1}])
    store.save("primary", conversation_history=_turns(2), working_memory=[{"a": 2}])

    assert _wait_for(
        lambda: store.path.is_file() and _disk(store)["working_memory"] == [{"a": 2}]
    )


def test_a_forced_save_still_writes_immediately(store):
    store.save("primary", conversation_history=_turns(1))
    assert store.save("primary", conversation_history=_turns(9), force=True) is True
    assert len(_disk(store)["conversation_history"]) == 9


def test_debounce_still_suppresses_the_synchronous_write(store):
    """The debounce must still do its job: a hot chat loop is not allowed
    to become IO-bound. The second call still returns False (no inline
    write); it just no longer LOSES the state."""
    assert store.save("primary", conversation_history=_turns(1)) is True
    assert store.save("primary", conversation_history=_turns(2)) is False


def test_close_flushes_a_pending_save(store):
    """``close()`` is the deterministic drain: whatever the debounce is
    holding must hit disk before the process goes away."""
    store.save("primary", conversation_history=_turns(1))
    store.save("primary", conversation_history=_turns(7))
    store.close()
    assert len(_disk(store)["conversation_history"]) == 7


def test_close_is_idempotent(store):
    store.save("primary", conversation_history=_turns(1))
    store.close()
    store.close()


# ── the shutdown force-save the docstring already promises ───────────


async def test_fastapi_shutdown_force_saves_the_primary_thread(monkeypatch):
    """``shutdown_event`` must call ``snapshot_primary_thread(force=True)``.

    Without it, every SIGTERM / ``feral stop`` / container stop loses
    whatever the debounce had swallowed, on every surface that has no
    WebSocket to disconnect.
    """
    from api import server as api_server
    from api.state import BrainState

    fake = MagicMock(spec=BrainState)
    fake.shutdown_background_tasks = AsyncMock(return_value=0)
    for attr in (
        "proactive", "screen_loop", "cron_service", "channel_manager",
        "mqtt_bridge", "email_watcher", "memory", "orchestrator",
        "mcp_client", "taskflows", "sync_engine", "sync_scheduler",
        "memory_decay", "consciousness",
    ):
        setattr(fake, attr, None)
    fake.snapshot_primary_thread = MagicMock(return_value=True)

    monkeypatch.setattr(api_server, "state", fake)
    await api_server.shutdown_event()

    fake.snapshot_primary_thread.assert_called_once_with(force=True)


async def test_shutdown_snapshot_runs_before_the_memory_store_closes(monkeypatch):
    """Ordering is load-bearing: ``snapshot_primary_thread`` reads
    ``memory.working_get``, so a snapshot taken after ``memory.close()``
    would persist an empty working-memory half."""
    from api import server as api_server
    from api.state import BrainState

    order: list[str] = []

    fake = MagicMock(spec=BrainState)
    fake.shutdown_background_tasks = AsyncMock(return_value=0)
    for attr in (
        "proactive", "screen_loop", "cron_service", "channel_manager",
        "mqtt_bridge", "email_watcher", "orchestrator", "mcp_client",
        "taskflows", "sync_engine", "sync_scheduler", "memory_decay",
        "consciousness",
    ):
        setattr(fake, attr, None)
    memory = MagicMock()
    memory.close = MagicMock(side_effect=lambda: order.append("memory.close"))
    fake.memory = memory
    fake.snapshot_primary_thread = MagicMock(
        side_effect=lambda **kw: order.append("snapshot") or True
    )

    monkeypatch.setattr(api_server, "state", fake)
    await api_server.shutdown_event()

    assert order.index("snapshot") < order.index("memory.close")


async def test_a_raising_snapshot_does_not_abort_shutdown(monkeypatch):
    """Shutdown must still tear the rest down if the snapshot fails."""
    from api import server as api_server
    from api.state import BrainState

    fake = MagicMock(spec=BrainState)
    fake.shutdown_background_tasks = AsyncMock(return_value=0)
    for attr in (
        "proactive", "screen_loop", "cron_service", "channel_manager",
        "mqtt_bridge", "email_watcher", "orchestrator", "mcp_client",
        "taskflows", "sync_engine", "sync_scheduler", "memory_decay",
        "consciousness",
    ):
        setattr(fake, attr, None)
    closed: list[str] = []
    memory = MagicMock()
    memory.close = MagicMock(side_effect=lambda: closed.append("closed"))
    fake.memory = memory
    fake.snapshot_primary_thread = MagicMock(side_effect=RuntimeError("disk full"))

    monkeypatch.setattr(api_server, "state", fake)
    await api_server.shutdown_event()

    assert closed == ["closed"]
