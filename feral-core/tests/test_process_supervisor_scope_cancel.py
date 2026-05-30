"""scope_cancel kills every matching child within 200ms.

Contract: scope-cancel composes with  scope_key concept. Spec:
spawn 5 children with scope_key="batch-A",
1 child with scope_key="batch-B"; call scope_cancel("batch-A");
assert all 5 dead within 200ms; assert the survivor still running.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from process.supervisor import create_process_supervisor


pytestmark = pytest.mark.no_auto_feral_home


async def test_scope_cancel_kills_matching_children_under_200ms() -> None:
    supervisor = create_process_supervisor()

    batch_a = [
        await supervisor.run(["sleep", "30"], scope_key="batch-A")
        for _ in range(5)
    ]
    survivor = await supervisor.run(["sleep", "30"], scope_key="batch-B")

    # Sanity: all six are active before the cancel.
    active_before = await supervisor.registry.list_active()
    assert len(active_before) == 6

    started = time.monotonic()
    cancelled = await supervisor.scope_cancel("batch-A")
    assert cancelled == 5

    # Wait for every batch-A handle to actually finalize. ``sleep``
    # respects SIGTERM immediately so this resolves in a few ms; the
    # 0.5s ``asyncio.wait_for`` is a defensive ceiling, not the
    # measurement — the real assertion is the 200ms budget below.
    await asyncio.wait_for(
        asyncio.gather(*(h.wait() for h in batch_a)),
        timeout=0.5,
    )
    elapsed_ms = (time.monotonic() - started) * 1000.0
    assert elapsed_ms < 200.0, (
        f"scope_cancel took {elapsed_ms:.1f}ms for 5 children, "
        "expected <200ms"
    )

    for h in batch_a:
        record = await supervisor.registry.get(h.run_id)
        assert record is not None
        assert record.finished_at is not None
        assert record.kill_reason == "manual_cancel"
        assert record.exit_code is not None and record.exit_code < 0

    # The batch-B survivor must still be running and untouched.
    survivor_record = await supervisor.registry.get(survivor.run_id)
    assert survivor_record is not None
    assert survivor_record.finished_at is None
    assert survivor_record.kill_reason is None

    active_after = await supervisor.registry.list_active()
    assert len(active_after) == 1
    assert active_after[0].run_id == survivor.run_id
    assert active_after[0].scope_key == "batch-B"

    # list_by_scope reflects the cancellation but still returns the
    # finalized records.
    by_scope_a = await supervisor.registry.list_by_scope("batch-A")
    assert len(by_scope_a) == 5
    assert all(r.finished_at is not None for r in by_scope_a)
    by_scope_b = await supervisor.registry.list_by_scope("batch-B")
    assert len(by_scope_b) == 1
    assert by_scope_b[0].finished_at is None

    # Clean up the survivor so the test does not leak a 30s sleep.
    survivor.cancel()
    await survivor.wait()


async def test_scope_cancel_with_empty_or_unknown_key_is_noop() -> None:
    supervisor = create_process_supervisor()
    handle = await supervisor.run(["sleep", "10"], scope_key="batch-A")

    assert await supervisor.scope_cancel("") == 0
    assert await supervisor.scope_cancel("   ") == 0
    assert await supervisor.scope_cancel("batch-Z") == 0

    record = await supervisor.registry.get(handle.run_id)
    assert record is not None and record.finished_at is None

    handle.cancel()
    await handle.wait()
