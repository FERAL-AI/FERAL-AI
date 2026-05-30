"""steer-failure clears the suppression flag.

If the supervisor's ``steer`` decision raises mid-spawn, the parent's
announce-suppression flag MUST be cleared before the exception
propagates. Silent swallowing is forbidden by  doctrine.
"""

from __future__ import annotations

import asyncio

import pytest

from agents import subagent_policy
from agents.subagent_spawner import (
    get_registry,
    register_runner,
    register_supervisor,
    spawn_subsession,
)


pytestmark = pytest.mark.no_auto_feral_home


async def _hold(*, cancel_event: asyncio.Event, **_):
    await cancel_event.wait()


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    subagent_policy.clear()
    get_registry().reset()
    register_runner(_hold)
    register_supervisor(None)
    yield
    subagent_policy.clear()
    get_registry().reset()
    register_runner(None)
    register_supervisor(None)


@pytest.mark.asyncio
async def test_steer_failure_clears_suppression_and_propagates():
    """Hook raises → suppression flag cleared → exception NOT swallowed."""
    parent_id = "parent-1"
    child_id = await spawn_subsession(parent_id, "tool_runner", scope_key="alpha")

    raised = []

    async def steer_hook(**kwargs):
        raised.append(kwargs)
        raise RuntimeError("dispatch failed")

    reg = get_registry()
    with pytest.raises(RuntimeError, match="dispatch failed"):
        await reg.steer_subsession(parent_id, child_id, "go", steer_hook=steer_hook)

    assert len(raised) == 1
    assert reg.is_suppressed(parent_id, child_id) is False

    success_calls = []

    async def success_hook(**kwargs):
        success_calls.append(kwargs)
        return {"ok": True}

    result = await reg.steer_subsession(
        parent_id, child_id, "retry", steer_hook=success_hook
    )
    assert result == {"ok": True}
    assert len(success_calls) == 1

    await reg.cancel_all_children(parent_id)


@pytest.mark.asyncio
async def test_successful_steer_leaves_suppression_set():
    """A successful steer keeps the suppression on ( contract)."""
    parent_id = "parent-2"
    child_id = await spawn_subsession(parent_id, "tool_runner", scope_key="alpha")

    async def hook(**kwargs):
        return "ok"

    reg = get_registry()
    result = await reg.steer_subsession(parent_id, child_id, "go", steer_hook=hook)
    assert result == "ok"
    assert reg.is_suppressed(parent_id, child_id) is True

    await reg.cancel_all_children(parent_id)


@pytest.mark.asyncio
async def test_missing_hook_raises_without_silent_swallow():
    parent_id = "parent-3"
    child_id = await spawn_subsession(parent_id, "tool_runner", scope_key="alpha")

    reg = get_registry()
    with pytest.raises(RuntimeError, match="no steer hook"):
        await reg.steer_subsession(parent_id, child_id, "go")
    assert reg.is_suppressed(parent_id, child_id) is False

    await reg.cancel_all_children(parent_id)


@pytest.mark.asyncio
async def test_unknown_child_raises_keyerror():
    reg = get_registry()
    with pytest.raises(KeyError):
        await reg.steer_subsession("ghost", "missing", "go", steer_hook=lambda **_: None)
