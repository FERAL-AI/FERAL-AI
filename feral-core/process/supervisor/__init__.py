"""process supervisor (overall + no-output timeouts, scope-cancel).

The supervisor is FERAL's canonical POSIX-subprocess lifecycle manager:
a run has an overall wall-clock timeout, a no-output inactivity timeout,
and belongs to a scope that can cancel every in-flight run atomically.

Public surface::

    from process.supervisor import create_process_supervisor

    supervisor = create_process_supervisor()
    handle = await supervisor.run(
        ["sleep", "10"],
        scope_key="batch-A",
        overall_timeout_sec=1.0,
    )
    record = await handle.wait()

Callers in production today:

* ``skills/impl/coding_tools.py``, ``coding_tools__bash`` with
  ``run_in_background: true`` runs every background shell job through
  this supervisor (bounded output buffers, wall-clock timeout,
  ``scope_key`` = the FERAL session id so one call kills a session's
  whole job set).

Still unwired (ready, no caller): voice service restarts, Codex CLI /
Claude Code CLI integrations, ffmpeg pipelines.
"""

from .buffer import BoundedLineBuffer
from .registry import RunRecord, RunRegistry
from .supervisor import ProcessSupervisor, RunHandle, create_process_supervisor

__all__ = [
    "BoundedLineBuffer",
    "ProcessSupervisor",
    "RunHandle",
    "RunRecord",
    "RunRegistry",
    "create_process_supervisor",
]
