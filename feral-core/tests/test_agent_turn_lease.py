"""Revocation races without a vault, network, device or background agent."""

import asyncio
import inspect
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock

import pytest
from security.agent_turn_lease import (
    AgentTurnRevoked,
    attach_agent_dispatch_lease,
    bind_agent_turn,
    drain_agent_turns,
    guard_agent_dispatch,
    invalidate_agent_turns,
    spawn_agent_turn,
)
from agents.tool_runner import ToolRunner


def encrypted():
    runner = ToolRunner.__new__(ToolRunner)
    runner._native_agent_dispatch_lease = None
    state = NS(
        _native_vault_deferred=True,
        _native_bootstrap_required=False,
        agent_bootstrap_controller=NS(_completed=object(), _active_binding=None),
        orchestrator=NS(tool_runner=runner),
        memory=object(),
    )
    attach_agent_dispatch_lease(state)
    return state, runner


@pytest.mark.asyncio
async def test_registered_running_turn_is_cancelled_and_drained():
    state, _ = encrypted()
    entered = asyncio.Event()

    async def operation():
        entered.set()
        await asyncio.Event().wait()

    task = spawn_agent_turn(state, operation())
    await entered.wait()
    handles = invalidate_agent_turns(state)
    assert handles == (task,)
    assert await drain_agent_turns(handles, 0.1)
    assert task.cancelled()
    await asyncio.sleep(0)
    assert not state._native_agent_turn_tasks


@pytest.mark.asyncio
async def test_lock_before_first_task_instruction_never_runs_or_leaks_coro():
    state, _ = encrypted()
    ran = []

    async def operation():
        ran.append(True)

    coro = operation()
    task = spawn_agent_turn(state, coro)
    assert await drain_agent_turns(invalidate_agent_turns(state), 0.1)
    await asyncio.sleep(0)
    assert task.cancelled() and not ran
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_cancellation_suppressing_turn_cannot_dispatch_and_is_not_drained():
    state, runner = encrypted()
    entered, release = asyncio.Event(), asyncio.Event()
    prevented = []

    async def operation():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            with pytest.raises(AgentTurnRevoked):
                runner._guard_agent_lease()
            prevented.append(True)
            await release.wait()

    task = spawn_agent_turn(state, operation())
    await entered.wait()
    handles = invalidate_agent_turns(state)
    assert not await drain_agent_turns(handles, 0.001)
    assert prevented == [True]
    release.set()
    assert await drain_agent_turns(handles, 0.1)
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_old_runner_stays_revoked_when_state_is_restored_and_new_runner_attached():
    state, old = encrypted()
    invalidate_agent_turns(state)
    state.memory = object()
    fresh = ToolRunner.__new__(ToolRunner)
    state.orchestrator = NS(tool_runner=fresh)
    attach_agent_dispatch_lease(state)
    fresh._guard_agent_lease()
    with pytest.raises(AgentTurnRevoked):
        old._guard_agent_lease()
    with bind_agent_turn(state):
        fresh._guard_agent_lease()
        with pytest.raises(AgentTurnRevoked):
            old._guard_agent_lease()


@pytest.mark.asyncio
async def test_optional_plaintext_locked_vault_does_not_cancel_or_block_local_turn():
    state = NS(
        _native_vault_deferred=True,
        _native_bootstrap_required=False,
        agent_bootstrap_controller=NS(_completed=None, _active_binding=None),
        orchestrator=object(),
        memory=object(),
    )
    release = asyncio.Event()

    async def operation():
        guard_agent_dispatch()
        await release.wait()
        return "local works"

    task = spawn_agent_turn(state, operation())
    assert invalidate_agent_turns(state) == ()
    assert not task.cancelled()
    release.set()
    assert await task == "local works"


def test_reviewed_startup_attachment_fences_dispatch_until_commit():
    state, runner = encrypted()
    state._native_bootstrap_required = True
    attach_agent_dispatch_lease(state)
    with pytest.raises(AgentTurnRevoked):
        runner._guard_agent_lease()
    state._native_bootstrap_required = False
    runner._guard_agent_lease()


@pytest.mark.asyncio
async def test_actual_runner_entrypoints_refuse_before_policy_or_dispatch():
    state, runner = encrypted()
    runner.policy_for = Mock(side_effect=AssertionError("must not resolve"))
    runner._orch = NS(
        daemons={"phone": NS(send_json=AsyncMock())}, executor=AsyncMock()
    )
    invalidate_agent_turns(state)
    with pytest.raises(AgentTurnRevoked):
        runner.enforce_safety("read", {})
    for call in (
        runner.execute_daemon_command("sid", "phone", "action", {}),
        runner.execute_daemon_command_with_ack("sid", "phone", "action", {}),
        runner.execute_capability_action("sid", "phone.action", {}),
        runner.execute_tool_call_for_llm("sid", {}, []),
        runner.execute_tool_call("sid", {}, []),
        runner.spawn_subagents("sid", {}),
    ):
        with pytest.raises(AgentTurnRevoked):
            await call
    runner.policy_for.assert_not_called()
    runner._orch.daemons["phone"].send_json.assert_not_called()
    runner._orch.executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_turn_that_calls_lock_is_retained_for_cleanup_without_self_cancel():
    state, runner = encrypted()
    captured, release = asyncio.Event(), asyncio.Event()
    handles = []

    async def operation():
        handles.extend(invalidate_agent_turns(state))
        assert asyncio.current_task() in handles
        assert not await drain_agent_turns(handles, 0.01)
        with pytest.raises(AgentTurnRevoked):
            runner._guard_agent_lease()
        captured.set()
        await release.wait()
        return "lock response"

    task = spawn_agent_turn(state, operation())
    await captured.wait()
    assert not await drain_agent_turns(handles, 0.001)
    release.set()
    assert await task == "lock response"
    assert await drain_agent_turns(handles, 0.1)


@pytest.mark.asyncio
async def test_cancel_during_daemon_send_releases_ack_without_manufactured_result(
    monkeypatch,
):
    state, runner = encrypted()
    started = asyncio.Event()

    async def send(frame):
        started.set()
        await asyncio.Event().wait()

    runner._orch = NS(daemons={"phone": NS(send_json=send)})
    runner._pending_daemon_acks, runner._daemon_session_map = {}, {}
    monkeypatch.setattr(
        "agents.tool_runner.build_action_request",
        lambda *a, **kw: NS(allowed=True, frame={"action": "fixture"}),
    )
    task = spawn_agent_turn(
        state, runner.execute_daemon_command_with_ack("sid", "phone", "fixture", {})
    )
    await started.wait()
    assert len(runner._pending_daemon_acks) == 1
    assert await drain_agent_turns(invalidate_agent_turns(state), 0.1)
    assert task.cancelled()
    assert not runner._pending_daemon_acks and not runner._daemon_session_map
