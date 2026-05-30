"""overall (wall-clock) timeout kills the child within budget.

Contract: "enforces overall timeout". Spec: ``sleep 10`` with
overall_timeout=1 must die within
1.2s, the exit_code must show SIGTERM/SIGKILL (negative returncode
under asyncio convention), and the registry's kill_reason must be
``overall_timeout``.
"""

from __future__ import annotations

import time

import pytest

from process.supervisor import create_process_supervisor


pytestmark = pytest.mark.no_auto_feral_home


async def test_overall_timeout_kills_within_budget() -> None:
    supervisor = create_process_supervisor()
    started = time.monotonic()
    handle = await supervisor.run(
        ["sleep", "10"],
        overall_timeout_sec=1.0,
    )
    record = await handle.wait()
    elapsed = time.monotonic() - started

    # Budget: 1.0s timeout + 0.2s for SIGTERM delivery + asyncio
    # scheduling. ``sleep`` honours SIGTERM immediately so the real
    # number is closer to 1.0–1.05s on a quiet host.
    assert elapsed < 1.2, f"overall timeout took {elapsed:.3f}s, expected <1.2s"

    assert record.kill_reason == "overall_timeout"
    assert handle.kill_reason == "overall_timeout"
    # asyncio reports signal-terminated children as a negative
    # returncode (``-SIGTERM`` == -15, ``-SIGKILL`` == -9). Either is
    # acceptable per the spec.
    assert record.exit_code is not None
    assert record.exit_code < 0, (
        f"expected signal exit (negative rc); got exit_code={record.exit_code}"
    )
    assert record.finished_at is not None
    assert record.pid is not None and record.pid > 0

    active = await supervisor.registry.list_active()
    assert active == []
