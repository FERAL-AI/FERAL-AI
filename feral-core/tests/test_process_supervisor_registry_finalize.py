"""registry finalize stamps finished_at + exit_code; clears active.

Contract: "finalize sets exited state". Spec: spawn → wait →
finalize; assert RunRecord has
finished_at + exit_code populated; assert list_active is empty after
finalize.
"""

from __future__ import annotations

import asyncio

import pytest

from process.supervisor import RunRecord, RunRegistry, create_process_supervisor


pytestmark = pytest.mark.no_auto_feral_home


async def test_spawn_wait_finalize_populates_record() -> None:
    supervisor = create_process_supervisor()

    handle = await supervisor.run(
        ["sh", "-c", "echo hello; exit 0"],
        scope_key="finalize-test",
    )

    # Pre-finalize: registry knows the run, finished_at is None.
    pre = await supervisor.registry.get(handle.run_id)
    assert pre is not None
    assert pre.finished_at is None
    assert pre.exit_code is None
    assert pre.kill_reason is None
    assert pre.scope_key == "finalize-test"
    assert pre.pid is not None and pre.pid > 0

    record = await handle.wait()

    # Post-finalize: every field the spec names is populated.
    assert record.finished_at is not None
    assert record.finished_at >= record.started_at
    assert record.exit_code == 0
    assert record.kill_reason == "exit"
    assert record.pid == pre.pid
    assert record.scope_key == "finalize-test"

    active = await supervisor.registry.list_active()
    assert active == []

    # The record survives in the registry (so callers can post-mortem).
    after = await supervisor.registry.get(handle.run_id)
    assert after is not None
    assert after.finished_at == record.finished_at
    assert after.exit_code == 0

    # stdout was captured.
    assert "hello" in handle.stdout


async def test_wait_for_finish_unblocks_on_finalize() -> None:
    """``RunRegistry.wait_for_finish`` resolves when finalize fires."""
    registry = RunRegistry()
    await registry.register(
        RunRecord(run_id="r1", pid=42, scope_key=None, started_at=0.0)
    )

    waiter = asyncio.create_task(registry.wait_for_finish("r1"))
    # Give the waiter a tick to actually start awaiting.
    await asyncio.sleep(0.01)
    assert not waiter.done()

    snap = await registry.finalize("r1", exit_code=0, kill_reason="exit")
    result = await asyncio.wait_for(waiter, timeout=1.0)

    assert result.exit_code == 0
    assert result.finished_at is not None
    assert result.finished_at == snap.finished_at


async def test_finalize_unknown_run_id_raises() -> None:
    registry = RunRegistry()
    with pytest.raises(KeyError):
        await registry.finalize("ghost", exit_code=0, kill_reason="exit")
