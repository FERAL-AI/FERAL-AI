import os
import tempfile
import time
import asyncio

import pytest
import pytest_asyncio

from agents.taskflow import TaskFlowRuntime
from memory.store import MemoryStore


@pytest_asyncio.fixture
async def runtime():
    fd_mem, mem_path = tempfile.mkstemp(suffix=".db")
    os.close(fd_mem)
    fd_flow, flow_path = tempfile.mkstemp(suffix=".db")
    os.close(fd_flow)

    store = MemoryStore(db_path=mem_path)
    taskflow = TaskFlowRuntime(db_path=flow_path, memory_store=store)
    await taskflow.start()
    yield taskflow, store
    await taskflow.stop()

    os.unlink(mem_path)
    os.unlink(flow_path)


# The runner is a poller: ``TaskFlowRuntime._runner_loop`` sleeps 1.0s
# whenever it finds no ready flow, so a freshly created flow waits up to
# a full second before it is even picked up. On top of that the first
# ``note.save`` in a process pays for the embedding backend warming up,
# which on a cold CI runner includes a model-fetch attempt that has to
# fail before the numpy fallback takes over.
#
# The old budgets (5s and 7s) were therefore racing the runtime's own
# poll granularity plus a network timeout, and lost on a loaded runner:
# `assert 'running' == 'completed'` on 2026-08-06, job 92525495791.
#
# The property under test is "the flow reaches completed and the note is
# stored", not "within five seconds". So the ceiling below is an
# anti-hang guard, deliberately far above the real cost (~1-2s locally),
# not a latency assertion. If a flow ever genuinely needs 30s, that is a
# bug worth failing on. Raising this further is not the fix; making the
# runner event-driven would be.
_FLOW_COMPLETION_CEILING_SEC = 30.0


async def _await_flow_status(taskflow, flow_id, expected, *,
                             ceiling=_FLOW_COMPLETION_CEILING_SEC):
    """Poll until the flow reaches ``expected``; return the final record.

    Reports the last status actually seen when it times out, so a
    failure says which state the flow got stuck in instead of just
    re-printing the mismatch.
    """
    deadline = time.monotonic() + ceiling
    latest = None
    while time.monotonic() < deadline:
        latest = taskflow.get_flow(flow_id)
        if latest and latest["status"] == expected:
            return latest
        await asyncio.sleep(0.05)
    seen = latest["status"] if latest else "<flow disappeared>"
    raise AssertionError(
        f"flow {flow_id} was {seen!r}, never reached {expected!r} "
        f"within {ceiling}s"
    )


@pytest.mark.asyncio
async def test_taskflow_runs_steps_and_completes(runtime):
    taskflow, store = runtime
    flow = taskflow.create_flow(
        session_id="s1",
        title="simple flow",
        steps=[
            {"type": "noop"},
            {"type": "note.save", "content": "taskflow wrote this note"},
        ],
    )
    flow_id = flow["id"]

    await _await_flow_status(taskflow, flow_id, "completed")

    notes = await store.search("taskflow wrote this note", limit=5)
    assert len(notes) >= 1


@pytest.mark.asyncio
async def test_taskflow_waiting_step_resumes(runtime):
    taskflow, _ = runtime
    flow = taskflow.create_flow(
        session_id="s2",
        title="wait flow",
        steps=[
            {"type": "sleep", "seconds": 1},
            {"type": "noop"},
        ],
    )
    flow_id = flow["id"]

    # This flow contains a 1s `sleep` step, so it costs the runner's poll
    # interval twice: once to pick the flow up, once to resume it after
    # the wait. Same ceiling, same reasoning as above.
    await _await_flow_status(taskflow, flow_id, "completed")
