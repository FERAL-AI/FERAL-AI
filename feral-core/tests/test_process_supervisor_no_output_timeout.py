"""no-output (silent-hang) timeout kills the child within budget.

Contract: "no-output timeout". Spec: ``sleep 10``
emits nothing on stdout/stderr; with no_output_timeout=1, must die
within 1.2s with kill_reason=``no_output_timeout``.
"""

from __future__ import annotations

import time

import pytest

from process.supervisor import create_process_supervisor


pytestmark = pytest.mark.no_auto_feral_home


async def test_no_output_timeout_kills_within_budget() -> None:
    supervisor = create_process_supervisor()
    started = time.monotonic()
    handle = await supervisor.run(
        ["sleep", "10"],
        no_output_timeout_sec=1.0,
    )
    record = await handle.wait()
    elapsed = time.monotonic() - started

    assert elapsed < 1.2, (
        f"no-output timeout took {elapsed:.3f}s, expected <1.2s"
    )
    assert record.kill_reason == "no_output_timeout"
    assert handle.kill_reason == "no_output_timeout"
    assert record.exit_code is not None
    assert record.exit_code < 0, (
        f"expected signal exit (negative rc); got exit_code={record.exit_code}"
    )
    assert record.finished_at is not None
    # ``sleep`` produced no output; both buffers must be empty.
    assert handle.stdout == ""
    assert handle.stderr == ""


async def test_no_output_timeout_resets_on_output() -> None:
    """Output activity resets the silence timer (rolling-gap semantics).

    A child that emits a line every 0.3s with no_output_timeout=0.8s
    must NOT be killed — the touch-output-on-line semantics require
    the timer to restart on every line. This test would catch the
    regression where the watcher uses a fixed deadline instead of a
    rolling gap.
    """
    supervisor = create_process_supervisor()
    handle = await supervisor.run(
        [
            "sh",
            "-c",
            "for i in 1 2 3 4 5; do echo line$i; sleep 0.3; done",
        ],
        no_output_timeout_sec=0.8,
        overall_timeout_sec=5.0,
    )
    record = await handle.wait()

    assert record.kill_reason == "exit"
    assert record.exit_code == 0
    assert "line5" in handle.stdout
