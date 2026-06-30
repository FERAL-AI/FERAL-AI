"""Lane 08 WS1 — ``episode_save`` is fire-and-forget on the orchestrator hot path.

AUDIT-r14 finding 20 + AUDIT-r13 finding 6.4 pinned the regression:
the orchestrator used to ``await self.memory.episode_save(...)`` BEFORE
``_route_prompt`` on every chat turn. Under SQLite pool contention that
write can take 50-500ms which the user perceives as the brain being
"hung" (no LLM activity, no stream_delta).

This module pins the fix:

  1. ``Orchestrator._save_episode_async`` returns immediately, never
     awaiting the SQLite write.

  2. ``handle_command`` / ``handle_command_stream`` finish their
     entry-block (the chunk before ``_route_prompt``) without holding
     the event loop for more than the slow-callback budget (50ms),
     even when ``episode_save`` is wedged for seconds.

  3. Failure inside ``episode_save`` is logged + counted, never
     propagated; the user-visible turn proceeds.

  4. The synthesized fire-and-forget task IS awaited by the test (the
     contract is "off the hot path", not "never written") so we prove
     the row eventually lands.
"""

from __future__ import annotations

import asyncio
import time
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


class TestSaveEpisodeAsync:
    """Direct API contract for the fire-and-forget helper."""

    def test_returns_none_when_memory_unwired(self) -> None:
        orch = _make_orchestrator(memory=None)
        # No running loop required for the None branch.
        assert orch._save_episode_async(
            session_id="s1",
            event_type="user_command",
            summary="hi",
            detail="{}",
        ) is None

    @pytest.mark.asyncio
    async def test_returns_task_and_writes_in_background(self) -> None:
        memory = MagicMock()
        memory.episode_save = AsyncMock(return_value={"event_type": "user_command"})
        orch = _make_orchestrator(memory=memory)

        task = orch._save_episode_async(
            session_id="sess-abc12345",
            event_type="user_command",
            summary="hello world",
            detail='{"a":1}',
        )
        assert task is not None
        assert isinstance(task, asyncio.Task)

        # The contract is fire-and-forget — the task is NOT yet done
        # at return time (it hasn't been scheduled by the loop yet).
        assert not task.done()

        await task
        memory.episode_save.assert_awaited_once_with(
            session_id="sess-abc12345",
            event_type="user_command",
            summary="hello world",
            detail='{"a":1}',
        )

    @pytest.mark.asyncio
    async def test_swallows_episode_save_failure(self, caplog) -> None:
        memory = MagicMock()
        memory.episode_save = AsyncMock(side_effect=RuntimeError("disk full"))
        orch = _make_orchestrator(memory=memory)

        task = orch._save_episode_async(
            session_id="s",
            event_type="user_command",
            summary="x",
            detail="{}",
        )
        assert task is not None

        with caplog.at_level("WARNING"):
            await task
        assert any("episode_save failed" in r.message for r in caplog.records)
        # Task completes cleanly — failure is swallowed.
        assert task.exception() is None

    @pytest.mark.asyncio
    async def test_threads_importance_when_provided(self) -> None:
        memory = MagicMock()
        memory.episode_save = AsyncMock(return_value={})
        orch = _make_orchestrator(memory=memory)

        task = orch._save_episode_async(
            session_id="s",
            event_type="proactive_alert",
            summary="HEALTH",
            importance=0.9,
        )
        assert task is not None
        await task
        kwargs = memory.episode_save.await_args.kwargs
        assert kwargs["importance"] == 0.9
        assert kwargs["event_type"] == "proactive_alert"

    @pytest.mark.asyncio
    async def test_omits_importance_when_not_provided(self) -> None:
        memory = MagicMock()
        memory.episode_save = AsyncMock(return_value={})
        orch = _make_orchestrator(memory=memory)

        task = orch._save_episode_async(
            session_id="s",
            event_type="user_command",
            summary="hi",
            detail="{}",
        )
        assert task is not None
        await task
        # No importance kwarg = upstream uses its own default. The
        # orchestrator must not synthesize one.
        assert "importance" not in memory.episode_save.await_args.kwargs

    @pytest.mark.asyncio
    async def test_drain_background_tasks_awaits_tracked_set(self) -> None:
        """CI-flake fix contract: every fire-and-forget save lands in
        ``self._background_tasks`` and the set self-cleans on
        completion. ``drain_background_tasks`` awaits the tracked
        gather instead of an ``all_tasks()`` sweep so tests can prove
        side-effects landed without a magic sleep.
        """
        memory = MagicMock()
        memory.episode_save = AsyncMock(return_value={})
        orch = _make_orchestrator(memory=memory)

        task = orch._save_episode_async(
            session_id="sess-track",
            event_type="user_command",
            summary="hi",
        )
        assert task is not None
        # Task is enrolled in the tracked set immediately.
        assert task in orch._background_tasks
        # Drain awaits it; the done-callback then discards it.
        await orch.drain_background_tasks(timeout=2.0)
        memory.episode_save.assert_awaited_once()
        assert orch._background_tasks == set()


class TestHotPathDoesNotBlock:
    """The user-visible regression: hot path return time must NOT
    depend on how long ``episode_save`` takes.
    """

    @pytest.mark.asyncio
    async def test_handle_command_entry_block_under_slow_callback_budget(
        self,
    ) -> None:
        """The pre-routing prelude (somatic + episode_save schedule)
        must finish well under the slow-callback budget even with a
        1-second ``episode_save``.
        """
        # Wedge episode_save with a real 1-second sleep so any awaited
        # path would visibly miss the budget. We use ``asyncio.sleep``
        # because the WS1 contract is "off the hot path", not "not
        # awaiting at all" — awaiting a 1s coroutine is fine when it's
        # a background task.

        async def slow_save(**_kwargs):
            await asyncio.sleep(1.0)
            return {"event_type": "user_command"}

        memory = MagicMock()
        memory.episode_save = AsyncMock(side_effect=slow_save)
        memory.working_push = MagicMock()
        memory.log_execution = AsyncMock()

        orch = _make_orchestrator(memory=memory)
        # No LLM, no skills — we want to exercise the entry prelude
        # then exit via the LLM-unavailable branch (``_direct_execute``
        # which we stub out so the test stays focused on WS1).
        orch.llm = MagicMock()
        orch.llm.available = False
        orch._direct_execute = AsyncMock(return_value=None)
        orch._maybe_handle_pending_tool_approval_text = AsyncMock(return_value=False)

        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = 0.05  # 50ms — alarms above this

        t0 = time.monotonic()
        await orch.handle_command(
            session_id="hot-12345678",
            text="What did I do yesterday?",
            context={"source": "test"},
        )
        elapsed = time.monotonic() - t0

        # Hot-path budget. Anything > 200ms means episode_save was
        # awaited (the slow_save sleeps 1.0s) — instant regression.
        assert elapsed < 0.2, (
            f"handle_command held the loop for {elapsed:.3f}s — "
            "episode_save must be fire-and-forget"
        )

        # Yield once so the just-scheduled background task gets to
        # enter its body and record the mock call. We're still well
        # inside the hot-path budget after this; the task body hits
        # the AsyncMock at first await, then suspends on slow_save's
        # 1s sleep — the call IS counted at that point even though
        # the side_effect hasn't returned yet.
        await asyncio.sleep(0)
        memory.episode_save.assert_called_once()

        # Deterministically await every fire-and-forget task the
        # orchestrator scheduled this turn (episode_save +
        # temporal-timeline side-channel). Bounded gather via the
        # orchestrator's tracked set — no ``all_tasks()`` sweep, no
        # ``asyncio.wait`` on tasks-we-don't-own, no magic sleep.
        await orch.drain_background_tasks(timeout=3.0)
        assert orch._background_tasks == set()

    @pytest.mark.asyncio
    async def test_stream_path_entry_block_under_slow_callback_budget(
        self,
    ) -> None:
        """Same contract for ``_handle_command_stream_impl`` — the
        second ``episode_save`` call site (lane 08's other patch
        point) must also be fire-and-forget.
        """

        async def slow_save(**_kwargs):
            await asyncio.sleep(1.0)
            return {"event_type": "user_command"}

        memory = MagicMock()
        memory.episode_save = AsyncMock(side_effect=slow_save)
        memory.working_push = MagicMock()
        memory.log_execution = AsyncMock()

        orch = _make_orchestrator(memory=memory)
        # Real stream path: llm.available + _streaming_enabled both
        # True. We force the stream to terminate on the first delta so
        # the test only exercises the entry prelude (which is what
        # WS1 targets) — no skill routing, no tool dispatch.

        async def fake_stream(messages, tools=None, **kwargs):
            yield {"type": "text_delta", "content": "ok"}
            yield {"type": "done"}

        orch.llm = MagicMock()
        orch.llm.available = True
        orch.llm.model_name = "test-model"
        orch.llm.chat_stream = fake_stream
        orch._maybe_handle_pending_tool_approval_text = AsyncMock(return_value=False)
        # Short-circuit routing so the prelude is the only meaningful
        # work — same intent as the non-stream test.
        orch._route_prompt = AsyncMock(return_value=[])
        orch._ensure_core_skills = lambda skills: skills

        t0 = time.monotonic()
        await orch.handle_command_stream(
            session_id="stream-12345678",
            text="hello",
            context={"source": "test"},
        )
        elapsed = time.monotonic() - t0

        # Stream-path budget. As long as ``episode_save`` is awaited
        # the test would block for the full slow_save sleep (1s).
        assert elapsed < 0.3, (
            f"handle_command_stream held the loop for {elapsed:.3f}s — "
            "episode_save must be fire-and-forget"
        )

        await asyncio.sleep(0)
        memory.episode_save.assert_called_once()

        await orch.drain_background_tasks(timeout=3.0)
        assert orch._background_tasks == set()
