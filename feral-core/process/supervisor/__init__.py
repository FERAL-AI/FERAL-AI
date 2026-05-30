"""W18: process supervisor (overall + no-output timeouts, scope-cancel).

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

The abstraction ships READY for W23/voice/Codex CLI/Claude Code CLI
integrations but is intentionally NOT wired into ``agents/orchestrator``
in this PR (no callers yet).
"""

from .registry import RunRecord, RunRegistry
from .supervisor import ProcessSupervisor, RunHandle, create_process_supervisor

__all__ = [
    "ProcessSupervisor",
    "RunHandle",
    "RunRecord",
    "RunRegistry",
    "create_process_supervisor",
]
