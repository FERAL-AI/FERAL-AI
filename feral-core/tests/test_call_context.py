"""Contract for the ambient tool-call context (skills/call_context.py).

Session identity is not a parameter of ``BaseSkill.execute``; it travels
on a contextvar bound by ``ToolRunner``. These tests pin the two
properties the reliability layer depends on: it survives ``asyncio.Task``
boundaries (so ``spawn_subagents`` children inherit and can rebind), and
it fails open rather than raising when nothing bound it.
"""

from __future__ import annotations

import asyncio

from skills.call_context import (
    UNBOUND,
    bind_context,
    current_context,
    new_turn_id,
    require_context,
)


def test_unbound_by_default():
    ctx = current_context()
    assert ctx == UNBOUND
    assert ctx.session_id == ""
    assert ctx.bound is False


def test_require_context_fails_open_and_does_not_raise():
    """The guards built on this are a correctness aid, not a security
    boundary (that is SandboxPolicy plus approvals, and anything here is
    bypassable via bash + sed anyway). Failing closed would break
    cron-driven and taskflow writes on day one."""
    ctx = require_context("unit-test-feature")
    assert ctx.session_id == ""
    assert ctx.bound is False


def test_bind_and_reset_are_stack_shaped():
    with bind_context(session_id="s1", surface="websocket", turn_id="t1"):
        assert current_context().session_id == "s1"
        with bind_context(session_id="s2"):
            assert current_context().session_id == "s2"
            # Unset fields inherit from the enclosing bind.
            assert current_context().turn_id == "t1"
            assert current_context().surface == "websocket"
        assert current_context().session_id == "s1"
    assert current_context() == UNBOUND


def test_bind_resets_even_when_the_body_raises():
    try:
        with bind_context(session_id="s1"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert current_context() == UNBOUND


async def test_context_propagates_into_spawned_tasks():
    """This is why a contextvar is the right primitive: every dispatch
    path is async on one loop, and contextvars are copied into each
    ``asyncio.Task``, so subagent workers inherit the parent's identity
    without any plumbing."""
    seen: list[str] = []

    async def child(name: str):
        seen.append(current_context().session_id)
        with bind_context(session_id=f"parent:sub:{name}"):
            await asyncio.sleep(0)
            seen.append(current_context().session_id)

    with bind_context(session_id="parent", turn_id="t9"):
        await asyncio.gather(child("1"), child("2"))
        # The children's rebinds did not leak back out.
        assert current_context().session_id == "parent"

    assert seen.count("parent") == 2
    assert "parent:sub:1" in seen and "parent:sub:2" in seen


async def test_concurrent_binds_do_not_bleed_across_tasks():
    results: dict[str, str] = {}

    async def worker(sid: str):
        with bind_context(session_id=sid, turn_id=f"turn-{sid}"):
            await asyncio.sleep(0.01)
            results[sid] = current_context().turn_id

    await asyncio.gather(*(worker(f"s{i}") for i in range(8)))
    assert results == {f"s{i}": f"turn-s{i}" for i in range(8)}


def test_turn_ids_are_unique():
    assert new_turn_id() != new_turn_id()
