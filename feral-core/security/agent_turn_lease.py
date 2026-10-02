"""Explicit generation leases for reviewed encrypted-native agent turns.

Not an OS/vault accessor. Optional locked vaults on fresh plaintext profiles do
not require a lease. Revocation prevents new dispatch; already-started external
operations can remain uncertain and are never described as rolled back.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


class AgentTurnRevoked(asyncio.CancelledError):
    pass


@dataclass(frozen=True)
class AgentTurnLease:
    state: object = field(repr=False)
    generation: int


_lease = ContextVar("feral_reviewed_agent_turn_lease", default=None)


def lease_required(state):
    controller = getattr(state, "agent_bootstrap_controller", None)
    return getattr(state, "_native_vault_deferred", False) is True and (
        getattr(state, "_native_bootstrap_required", False) is True
        or getattr(controller, "_active_binding", None) is not None
        or getattr(controller, "_completed", None) is not None
    )


def capture_agent_lease(state):
    return (
        AgentTurnLease(state, getattr(state, "_native_agent_turn_generation", 0))
        if lease_required(state)
        else None
    )


def guard_agent_dispatch(attached=None):
    current = _lease.get()
    lease = current if current is not None else attached
    if lease is None:
        return
    if not isinstance(lease, AgentTurnLease):
        raise AgentTurnRevoked("Agent authorization lease is unavailable")
    state = lease.state
    if (
        lease.generation != getattr(state, "_native_agent_turn_generation", 0)
        or getattr(state, "_native_bootstrap_required", False) is True
        or getattr(state, "orchestrator", None) is None
        or getattr(state, "memory", None) is None
    ):
        raise AgentTurnRevoked(
            "Agent authorization was revoked; no new tool dispatch is allowed"
        )
    if attached is not None and (
        not isinstance(attached, AgentTurnLease)
        or attached.state is not state
        or attached.generation != lease.generation
    ):
        raise AgentTurnRevoked("Tool runner belongs to another agent generation")


@contextmanager
def bind_agent_turn(state):
    token = _lease.set(capture_agent_lease(state))
    try:
        guard_agent_dispatch()
        yield
    finally:
        _lease.reset(token)


def attach_agent_dispatch_lease(state):
    runner = getattr(getattr(state, "orchestrator", None), "tool_runner", None)
    if runner is not None:
        runner._native_agent_dispatch_lease = capture_agent_lease(state)


def spawn_agent_turn(state, coro):
    # Capture before scheduling; a lock between create_task and first instruction
    # must not turn an old coroutine into a fresh authorized turn.
    captured = capture_agent_lease(state)
    registry = getattr(state, "_native_agent_turn_tasks", None)
    if registry is None:
        registry = state._native_agent_turn_tasks = set()

    async def run():
        token = _lease.set(captured)
        started = False
        try:
            guard_agent_dispatch()
            started = True
            return await coro
        finally:
            _lease.reset(token)
            if not started:
                close = getattr(coro, "close", None)
                if callable(close):
                    close()

    task = asyncio.create_task(run())
    registry.add(task)
    task.add_done_callback(registry.discard)

    # If cancelled before run's first instruction, its finally cannot close the
    # supplied coroutine. Keep an idempotent done cleanup to prevent leaked work.
    def close_unstarted(_):
        close = getattr(coro, "close", None)
        if callable(close):
            close()

    task.add_done_callback(close_unstarted)
    return task


def invalidate_agent_turns(state):
    if not lease_required(state):
        return ()
    state._native_agent_turn_generation = (
        getattr(state, "_native_agent_turn_generation", 0) + 1
    )
    current = asyncio.current_task()
    tasks = tuple(
        task
        for task in getattr(state, "_native_agent_turn_tasks", ())
        if not task.done()
    )
    for task in tasks:
        # A turn that calls lock must finish the lock response itself. Keep it
        # in drain handles so its retained memory cannot be closed underneath it.
        if task is not current:
            task.cancel()
    return tasks


async def drain_agent_turns(tasks, timeout=5):
    held = set(tasks)
    if asyncio.current_task() in held:
        return False
    if not held:
        return True
    done, pending = await asyncio.wait(held, timeout=max(0, min(float(timeout), 30)))
    for task in done:
        if not task.cancelled():
            task.exception()  # Consume private failures without printing details.
    return not pending
