"""scope_key cancellation semantics.

Scope contract (part of the canonical lifecycle test set:
allowlist / cron-note / lifecycle / model / scope):
* siblings sharing a scope_key die together
* default cancel_all kills every child regardless of scope_key
  (the "all-children-tied" default)
* explicit per-scope cancel spares siblings on a different scope_key
  (the user-opt-out path)
"""

from __future__ import annotations

import asyncio

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


async def _hold(*, cancel_event: asyncio.Event, **_):
    await cancel_event.wait()


@pytest.mark.asyncio
async def test_matching_scope_keys_cancelled_together():
    register_runner(_hold)
    a = await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")
    b = await spawn_subsession("parent-1", "research", scope_key="alpha")
    children = {c["child_id"]: c for c in get_registry().children_of("parent-1")}

    cancelled = await get_registry().cancel_children("parent-1", scope_key="alpha")
    assert cancelled == 2

    assert children[a]["task"].done()
    assert children[b]["task"].done()
    assert get_registry().children_of("parent-1") == []


@pytest.mark.asyncio
async def test_default_cancel_kills_every_child_regardless_of_scope():
    """All-children-tied default: cancel_all_children kills siblings on any scope."""
    register_runner(_hold)
    await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")
    await spawn_subsession("parent-1", "research", scope_key="beta")
    children = list(get_registry().children_of("parent-1"))
    assert len(children) == 2

    cancelled = await get_registry().cancel_all_children("parent-1")
    assert cancelled == 2
    for c in children:
        assert c["task"].done()


@pytest.mark.asyncio
async def test_explicit_scope_cancel_spares_other_scopes():
    """User opts out of all-tied by cancelling a single scope_key."""
    register_runner(_hold)
    a_id = await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")
    b_id = await spawn_subsession("parent-1", "research", scope_key="beta")
    children = {c["child_id"]: c for c in get_registry().children_of("parent-1")}

    cancelled = await get_registry().cancel_children("parent-1", scope_key="alpha")
    assert cancelled == 1
    assert children[a_id]["task"].done()
    assert not children[b_id]["task"].done()

    survivors = get_registry().children_of("parent-1")
    assert len(survivors) == 1
    assert survivors[0]["child_id"] == b_id

    await get_registry().cancel_all_children("parent-1")
    assert children[b_id]["task"].done()


@pytest.mark.asyncio
async def test_unrelated_parent_children_unaffected():
    """Cancellation must not leak across parent_ids."""
    register_runner(_hold)
    await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")
    other = await spawn_subsession("parent-2", "tool_runner", scope_key="alpha")

    await get_registry().cancel_all_children("parent-1")

    survivors = get_registry().children_of("parent-2")
    assert len(survivors) == 1
    assert survivors[0]["child_id"] == other
    assert not survivors[0]["task"].done()

    await get_registry().cancel_all_children("parent-2")
