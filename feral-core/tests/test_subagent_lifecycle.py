"""subagent lifecycle — spawn → run → reap, and parent-cancel propagation.

Part of the canonical lifecycle test set: allowlist / cron-note /
lifecycle / model / scope.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agents import subagent_policy
from agents.subagent_spawner import (
    get_registry,
    register_runner,
    spawn_subsession,
)


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    subagent_policy.clear()
    get_registry().reset()
    register_runner(None)
    yield
    subagent_policy.clear()
    get_registry().reset()
    register_runner(None)


async def _hold_until_cancel_runner(*, cancel_event: asyncio.Event, **_):
    await cancel_event.wait()


@pytest.mark.asyncio
async def test_spawn_registers_running_child():
    register_runner(_hold_until_cancel_runner)
    child_id = await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")
    children = get_registry().children_of("parent-1")
    assert len(children) == 1
    assert children[0]["child_id"] == child_id
    assert not children[0]["task"].done()


@pytest.mark.asyncio
async def test_parent_completion_reaps_children():
    register_runner(_hold_until_cancel_runner)
    await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")
    await spawn_subsession("parent-1", "research", scope_key="alpha")
    assert len(get_registry().children_of("parent-1")) == 2

    cancelled = await get_registry().cancel_all_children("parent-1")
    assert cancelled == 2
    assert get_registry().children_of("parent-1") == []


@pytest.mark.asyncio
async def test_parent_cancel_propagates_within_200ms():
    register_runner(_hold_until_cancel_runner)
    await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")
    await spawn_subsession("parent-1", "research", scope_key="alpha")
    children = list(get_registry().children_of("parent-1"))

    started = time.monotonic()
    await asyncio.wait_for(
        get_registry().cancel_all_children("parent-1"), timeout=0.2
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 200, f"cancel took {elapsed_ms:.1f}ms"

    for c in children:
        assert c["task"].done()
        assert c["task"].cancelled() or c["task"].exception() is None


@pytest.mark.asyncio
async def test_orchestrator_session_lock_teardown_cancels_subsessions(monkeypatch):
    """The additive try/finally in handle_command must call the  cancel hook."""
    register_runner(_hold_until_cancel_runner)
    from agents.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._session_locks = {}
    cancel_calls: list[str] = []

    def fake_cancel(parent_id):
        cancel_calls.append(parent_id)

    monkeypatch.setattr(
        orch, "_w17_cancel_subsessions_nowait", fake_cancel, raising=False
    )

    async def _impl(session_id, text, context):
        return "ok"

    monkeypatch.setattr(orch, "_handle_command_impl", _impl, raising=False)

    result = await Orchestrator.handle_command(orch, "sess-A", "hi", None)
    assert result == "ok"
    assert cancel_calls == ["sess-A"]


@pytest.mark.asyncio
async def test_parent_kill_during_runner_propagates_within_200ms():
    """Even mid-flight (parent task cancelled), child must die fast."""
    register_runner(_hold_until_cancel_runner)

    async def parent_workload():
        await spawn_subsession("parent-2", "tool_runner", scope_key="alpha")
        await asyncio.sleep(10)

    parent_task = asyncio.create_task(parent_workload())
    await asyncio.sleep(0.05)
    children = list(get_registry().children_of("parent-2"))
    assert len(children) == 1

    parent_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent_task

    started = time.monotonic()
    await asyncio.wait_for(
        get_registry().cancel_all_children("parent-2"), timeout=0.2
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 200
    for c in children:
        assert c["task"].done()
