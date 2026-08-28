"""Compaction must not hold the session lock across the model call.

`_maybe_auto_compact` schedules a background task, so the turn that
triggers compaction returns immediately. That made it look like
consolidation was off the hot path. It was not: the background body took
`_get_session_lock(session_id)` and held it for the whole summarization,
and `handle_command` / `handle_command_stream` take that same
per-session lock for the entire turn.

So the operator's NEXT message waited for consolidation to finish. On a
local model that is tens of seconds, every time the turn threshold is
crossed. The stall was not removed, it was moved one turn later, which
is exactly why it was hard to see.

Summarization does not need the lock. It reads a list it was handed and
returns a new one. Only the swap needs exclusivity. These tests pin both
halves: the lock is free while the model works, and turns that arrive
during that window are not dropped by the swap.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.orchestrator import Orchestrator  # noqa: E402


SLOW_SUMMARY = 0.30  # stands in for a multi-second local generation


def _orchestrator(compact_impl):
    """A bare orchestrator with just the fields _maybe_auto_compact touches."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.conversation_history = {}
    orch._turns_since_compaction = {}
    orch._compaction_inflight = {}
    # F6: the deadline and idle clocks the trigger ladder reads.
    orch._pending_since = {}
    orch._session_last_turn_at = {}
    orch._session_locks = {}
    orch._background_tasks = set()
    orch._last_turn_at = 0.0
    orch.llm = MagicMock()
    orch.memory = MagicMock()
    orch.memory.compact_session = AsyncMock(side_effect=compact_impl)
    orch._track_background_task = lambda t: orch._background_tasks.add(t)
    return orch


def _lock(orch, session_id):
    return orch._get_session_lock(session_id)


@pytest.mark.asyncio
async def test_session_lock_is_free_while_the_model_summarizes():
    """The whole point: another turn can take the lock mid-compaction."""
    lock_seen_free = asyncio.Event()

    async def slow_compact(session_id, history, llm=None):
        await asyncio.sleep(SLOW_SUMMARY)
        return {"compacted": True, "history": [{"role": "system", "content": "[summary]"}]}

    orch = _orchestrator(slow_compact)
    sid = "s1"
    orch.conversation_history[sid] = [
        {"role": "user", "content": f"turn {i}"} for i in range(40)
    ]
    orch._turns_since_compaction[sid] = 999  # force the threshold

    orch._maybe_auto_compact(sid)
    await asyncio.sleep(0.05)  # let the task reach the model call

    async def competing_turn():
        # This is what handle_command does for a whole turn.
        t0 = time.monotonic()
        async with _lock(orch, sid):
            waited = time.monotonic() - t0
        lock_seen_free.set()
        return waited

    waited = await asyncio.wait_for(competing_turn(), timeout=SLOW_SUMMARY * 2)
    assert lock_seen_free.is_set()
    assert waited < SLOW_SUMMARY / 2, (
        f"the next turn waited {waited:.3f}s for compaction to finish; the "
        "session lock is being held across the model call again"
    )

    await asyncio.gather(*list(orch._background_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_turns_arriving_during_compaction_are_not_dropped():
    """The reason the lock was there in the first place must still hold."""
    started = asyncio.Event()

    async def slow_compact(session_id, history, llm=None):
        started.set()
        await asyncio.sleep(SLOW_SUMMARY)
        return {"compacted": True, "history": [{"role": "system", "content": "[summary]"}]}

    orch = _orchestrator(slow_compact)
    sid = "s2"
    orch.conversation_history[sid] = [
        {"role": "user", "content": f"old {i}"} for i in range(30)
    ]
    orch._turns_since_compaction[sid] = 999

    orch._maybe_auto_compact(sid)
    await started.wait()

    # A real turn lands while the model is still working.
    async with _lock(orch, sid):
        orch.conversation_history[sid].append({"role": "user", "content": "MIDFLIGHT"})
        orch.conversation_history[sid].append({"role": "assistant", "content": "ack"})

    await asyncio.gather(*list(orch._background_tasks), return_exceptions=True)

    final = orch.conversation_history[sid]
    contents = [m.get("content") for m in final]
    assert "MIDFLIGHT" in contents, (
        f"a turn that arrived during compaction was dropped by the swap: {contents}"
    )
    assert "ack" in contents
    assert contents[0] == "[summary]", "compacted prefix should lead the transcript"


@pytest.mark.asyncio
async def test_shrinking_transcript_leaves_live_history_alone():
    """If positions stop meaning anything, do not corrupt the transcript."""
    started = asyncio.Event()

    async def slow_compact(session_id, history, llm=None):
        started.set()
        await asyncio.sleep(SLOW_SUMMARY)
        return {"compacted": True, "history": [{"role": "system", "content": "[summary]"}]}

    orch = _orchestrator(slow_compact)
    sid = "s3"
    orch.conversation_history[sid] = [
        {"role": "user", "content": f"old {i}"} for i in range(30)
    ]
    orch._turns_since_compaction[sid] = 999

    orch._maybe_auto_compact(sid)
    await started.wait()

    # Something reset the session mid-flight.
    async with _lock(orch, sid):
        orch.conversation_history[sid] = [{"role": "user", "content": "fresh start"}]

    await asyncio.gather(*list(orch._background_tasks), return_exceptions=True)

    contents = [m.get("content") for m in orch.conversation_history[sid]]
    assert contents == ["fresh start"], (
        f"a shrunken transcript was overwritten by a stale compaction: {contents}"
    )
    # The counter still resets; the episode was written regardless.
    assert orch._turns_since_compaction[sid] == 0
