"""``_emit_tool_result`` survives foreign-loop scheduling.

Regression: v2026.6.22 added ``device_action`` episode logging in
``Orchestrator._emit_tool_result``. The hook schedules ``episode_save``
as a fire-and-forget task via ``asyncio.create_task(...)``, which uses
whichever loop happens to be currently running. When the tool-result
hook is driven from a loop other than the one that owns the memory
store's aiosqlite pool — typical cron / routine paths spawn
``asyncio.new_event_loop()`` per job, and an embed of the brain into
another async host can re-route the realtime voice path — the runner
later awaits ``memory.episode_save`` → ``self._pool.get()`` which is
an ``asyncio.Queue`` bound to the owning loop. First contended ``get``
raises ::

    RuntimeError: <Queue at 0x... maxsize=4> is bound to a different event loop

The runner catches it and logs, so the user-facing tool still returns
its result; but the ``device_action`` provenance row silently never
lands and the brain's recall path correctly reports "I have no record
of that". The user perceives this as "the command isn't going through,
there's a misalignment in the event loop" — exactly the symptom that
flagged the bug on the live realtime voice path for ``cutebot__set_lights``.

These tests pin the fix:

  1. ``Orchestrator`` tracks an ``_owning_loop`` (BrainState seeds it
     from the FastAPI startup coroutine; tests can set it explicitly
     or rely on the lazy capture path).

  2. ``_save_episode_async`` notices when the current loop differs
     from the owning loop and hands the runner back to the owning
     loop via ``call_soon_threadsafe`` — so the runner's
     ``episode_save`` awaits land on the loop that owns the pool.

  3. After the foreign-loop ``_emit_tool_result`` returns and the
     owning loop drains, the ``device_action`` episode is persisted
     for both ``cutebot__set_lights`` and ``cutebot__drive`` (i.e. no
     loop-mismatch RuntimeError leaks, and the side-effect lands).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator


def _make_orchestrator(memory: Any) -> Orchestrator:
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    return Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=memory,
        vision_buffer=None,
        perception=None,
        learner=None,
    )


class _LoopBoundMemory:
    """A memory whose ``episode_save`` enforces the owning-loop contract.

    Mirrors the production failure: aiosqlite + ``asyncio.Queue`` raise
    ``RuntimeError`` the first time ``episode_save``'s internals try to
    wait on a primitive bound to a different loop. We don't need a real
    aiosqlite pool for the regression — emulating the cross-loop check
    is faithful to the failure mode and keeps the test hermetic and
    deterministic.
    """

    def __init__(self, owning_loop: asyncio.AbstractEventLoop):
        self._owning_loop = owning_loop
        self.calls: list[dict] = []
        self.loop_errors: list[str] = []

    async def episode_save(self, **kwargs):
        running = asyncio.get_running_loop()
        if running is not self._owning_loop:
            # This is what aiosqlite's pool queue raises in
            # production once the foreign-loop ``get`` has to wait.
            err = (
                f"<MemoryStore pool> is bound to a different event loop "
                f"(running={id(running):x} owning={id(self._owning_loop):x})"
            )
            self.loop_errors.append(err)
            raise RuntimeError(err)
        self.calls.append(kwargs)
        return {"id": f"ep-{len(self.calls)}"}


def _run_owning_loop_in_thread() -> tuple[
    asyncio.AbstractEventLoop, threading.Thread
]:
    """Start a fresh asyncio loop on a dedicated daemon thread.

    Returns the loop + thread so tests can ``call_soon_threadsafe`` /
    ``run_coroutine_threadsafe`` into it. ``stop_loop`` (below)
    shuts it down at test teardown.
    """
    loop = asyncio.new_event_loop()

    def _runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return loop, t


def _stop_loop(loop: asyncio.AbstractEventLoop, t: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


# ── Hermetic owning-loop fixtures ─────────────────────────────────────


@pytest.fixture
def owning_loop_pair():
    """Provide a (loop, thread) pair distinct from the asyncio.run
    loop pytest-asyncio spins up for the test function. The owning
    loop runs on a daemon thread; the test runs on the pytest-asyncio
    loop and pretends to be a 'foreign' caller scheduling background
    work.
    """
    loop, t = _run_owning_loop_in_thread()
    yield loop, t
    _stop_loop(loop, t)


# ── Repro: WITHOUT the fix, scheduling from a foreign loop loses the side-effect ──


@pytest.mark.asyncio
async def test_foreign_loop_emit_tool_result_routes_save_to_owning_loop(
    owning_loop_pair,
) -> None:
    """``_emit_tool_result`` driven from a foreign loop must still
    land the ``device_action`` episode on the owning loop's
    ``memory.episode_save`` without raising loop-mismatch.
    """
    owning_loop, _ = owning_loop_pair

    # Build the memory store and orchestrator on the OWNING loop so
    # ``_LoopBoundMemory`` sees that loop as canonical and the
    # orchestrator's ``set_owning_loop`` hook is called from the
    # same place BrainState would call it in production.
    async def _build_on_owning() -> Orchestrator:
        memory = _LoopBoundMemory(asyncio.get_running_loop())
        orch = _make_orchestrator(memory)
        orch.set_owning_loop(asyncio.get_running_loop())
        return orch

    orch = asyncio.run_coroutine_threadsafe(
        _build_on_owning(), owning_loop
    ).result(timeout=5)
    memory: _LoopBoundMemory = orch.memory  # type: ignore[assignment]

    # We are running on the pytest-asyncio loop — a DIFFERENT loop
    # from ``owning_loop``. This is exactly the situation a cron
    # daemon-thread routine (or an embed of the brain into a
    # third-party async host) creates when it calls back into the
    # orchestrator's tool-result hook.
    assert asyncio.get_running_loop() is not owning_loop

    await orch._emit_tool_result(
        session_id="sess-voice-realtime",
        tool_call={
            "name": "cutebot__set_lights",
            "args": {"r": 0, "g": 255, "b": 0},
        },
        result_data={"success": True, "data": {"verified": True}},
        latency_ms=12.0,
    )

    # Drain the owning loop's tracked background tasks from the
    # owning loop itself (which is where ``_save_episode_async``
    # routed the scheduling via ``call_soon_threadsafe``). The
    # drain must run on the same loop the tasks were created on,
    # so we wait for the call_soon trampoline to land first.
    await asyncio.sleep(0.05)

    async def _drain():
        await orch.drain_background_tasks(timeout=5.0)

    asyncio.run_coroutine_threadsafe(_drain(), owning_loop).result(timeout=10)

    # Contract: ZERO loop-mismatch errors and the device_action
    # episode landed on the owning loop's memory.
    assert memory.loop_errors == [], (
        "episode_save was scheduled on the wrong loop; saw: "
        f"{memory.loop_errors!r}"
    )
    assert len(memory.calls) == 1, (
        "device_action episode was not persisted (expected one call to "
        f"memory.episode_save, got {memory.calls!r})"
    )
    call = memory.calls[0]
    assert call["event_type"] == "device_action"
    assert call["session_id"] == "sess-voice-realtime"
    assert call["summary"].startswith("CuteBot: set_lights")
    assert "r=0" in call["summary"]
    assert "g=255" in call["summary"]
    assert "b=0" in call["summary"]


@pytest.mark.asyncio
async def test_drive_through_foreign_loop_also_lands(
    owning_loop_pair,
) -> None:
    """Sister test: confirms the cron/routine-style ``cutebot__drive``
    path — the one the user reported as ``NOT obviously failing`` in
    the live log — also lands its episode under the fix.
    """
    owning_loop, _ = owning_loop_pair

    async def _build_on_owning() -> Orchestrator:
        memory = _LoopBoundMemory(asyncio.get_running_loop())
        orch = _make_orchestrator(memory)
        orch.set_owning_loop(asyncio.get_running_loop())
        return orch

    orch = asyncio.run_coroutine_threadsafe(
        _build_on_owning(), owning_loop
    ).result(timeout=5)
    memory: _LoopBoundMemory = orch.memory  # type: ignore[assignment]

    assert asyncio.get_running_loop() is not owning_loop

    await orch._emit_tool_result(
        session_id="sess-routine-1",
        tool_call={
            "name": "cutebot__drive",
            "args": {"left": 50, "right": -50},
        },
        result_data={"success": True, "data": {"verified": True}},
        latency_ms=8.0,
    )
    await asyncio.sleep(0.05)

    async def _drain():
        await orch.drain_background_tasks(timeout=5.0)

    asyncio.run_coroutine_threadsafe(_drain(), owning_loop).result(timeout=10)

    assert memory.loop_errors == []
    assert len(memory.calls) == 1
    assert memory.calls[0]["summary"].startswith("CuteBot: drive")


@pytest.mark.asyncio
async def test_same_loop_path_still_creates_and_tracks_task() -> None:
    """Regression on the regression: the existing same-loop contract
    (``_save_episode_async`` returns a real ``asyncio.Task`` that
    tests can await directly) must NOT be broken by the foreign-loop
    branch.
    """
    memory = MagicMock()
    memory.episode_save = AsyncMock(return_value={"id": "ep-1"})
    orch = _make_orchestrator(memory)
    orch.set_owning_loop(asyncio.get_running_loop())

    task = orch._save_episode_async(
        session_id="sess-same-loop",
        event_type="device_action",
        summary="CuteBot: drive (left=10, right=10) — verified",
        detail="{}",
        importance=0.6,
    )
    assert task is not None
    assert isinstance(task, asyncio.Task)
    assert task in orch._background_tasks

    await task
    memory.episode_save.assert_awaited_once()
    assert task not in orch._background_tasks


@pytest.mark.asyncio
async def test_owning_loop_is_captured_lazily_when_unset() -> None:
    """When BrainState forgets to seed ``_owning_loop`` (the lazy
    path), the first call from inside a running loop pins it — and
    the same-loop branch keeps working without surprise.
    """
    memory = MagicMock()
    memory.episode_save = AsyncMock(return_value={"id": "ep-1"})
    orch = _make_orchestrator(memory)
    assert orch._owning_loop is None

    task = orch._save_episode_async(
        session_id="sess-lazy",
        event_type="device_action",
        summary="CuteBot: set_lights (r=255, g=0, b=0)",
    )
    assert task is not None
    assert orch._owning_loop is asyncio.get_running_loop()
    await task
    memory.episode_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_foreign_loop_emit_does_not_raise_when_owning_loop_dies(
) -> None:
    """If the owning loop is stopped/closed by the time a foreign
    caller arrives, the foreign branch must drop silently — never
    crash the tool path. The runner is best-effort provenance, not
    a user-visible side-channel.
    """
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()

    memory = MagicMock()
    memory.episode_save = AsyncMock(return_value={"id": "ep-1"})
    orch = _make_orchestrator(memory)
    orch.set_owning_loop(dead_loop)

    # Pytest-asyncio loop is the foreign loop here.
    out = orch._save_episode_async(
        session_id="sess-dead",
        event_type="device_action",
        summary="CuteBot: drive (left=1, right=1)",
    )
    assert out is None
    memory.episode_save.assert_not_called()


_ = pytest  # keep import-checker quiet
