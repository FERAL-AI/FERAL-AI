"""Tool-execution audit trail, written from the dispatch chokepoint.

Why this module exists
----------------------
``execution_log`` in ``~/.feral/memory.db`` stopped receiving rows on
2026-05-21 while the brain kept running until 2026-08-07. Nothing was
broken in ``MemoryStore.log_execution``: it still inserts correctly when
called. The row simply had exactly one writer, ``Orchestrator``, in its
two chat loops, and after 2026-05-21 the tool traffic moved to paths that
have no writer at all.

Measured on the live store: 206 rows total, last one at
``2026-05-21 15:16:47``. Over the same window ``episodes`` recorded 33
``event_type='tool'`` rows (2026-06-30 through 2026-08-06), every one of
them a real tool call made through ``voice/realtime_proxy.py``. Those
calls executed, they were narrated back to the user, and none of them
produced an audit row.

``SkillExecutor.execute`` is the one function every tool path reaches:
``agents/tool_runner.py``, ``agents/multi_agent.py``,
``agents/direct_execution.py``, ``mcp/server.py``,
``api/routes/tools.py``, ``voice/realtime_proxy.py`` and
``voice/gemini_realtime.py`` all arrive there. So the audit row is
written there, for the same reason the plan-mode gate moved there: a
record the caller has to opt into is missing from every caller that does
not know to opt in.

The orchestrator's own writes stay where they are, because the chat path
also dispatches tools that never reach the executor (``mcp_*`` tools,
``daemon_*`` commands, ``subagent__spawn_subagent``, and every refusal
that returns before dispatch). :func:`claimed_by_caller` is how the two
writers stay disjoint: the orchestrator claims the call, and the executor
sees the claim and does not write a second row.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

logger = logging.getLogger("feral.memory.execution_audit")

__all__ = [
    "claimed_by_caller",
    "caller_has_claimed",
    "record_execution",
    "status_of",
]

# Set for the duration of a tool call whose caller writes its own
# ``execution_log`` row. Read by ``SkillExecutor.execute``.
#
# A ContextVar rather than an attribute because the orchestrator runs its
# tool calls through ``asyncio.gather``, which copies the current context
# into each child task at creation time. Claiming before the gather
# therefore reaches every branch of the fan-out, and unclaiming cannot
# leak across an await into an unrelated session.
_CLAIMED: ContextVar[bool] = ContextVar("feral_execution_audit_claimed", default=False)

# One warning per reason per process. A tool loop that cannot reach the
# store must say so once, not on every call.
_warned: set[str] = set()
_warn_lock = threading.Lock()


def _warn_once(token: str, message: str, *args: Any) -> None:
    with _warn_lock:
        first = token not in _warned
        _warned.add(token)
    if first:
        logger.warning(message, *args)


@contextmanager
def claimed_by_caller() -> Iterator[None]:
    """Mark the enclosing block as writing its own audit row.

    Used by callers that log every tool call themselves, including the
    branches that never reach ``SkillExecutor``. Inside this block the
    executor skips its own write, so a claimed call produces exactly one
    row and an unclaimed one also produces exactly one.
    """
    token = _CLAIMED.set(True)
    try:
        yield
    finally:
        _CLAIMED.reset(token)


def caller_has_claimed() -> bool:
    """Whether an enclosing caller writes the audit row for this call."""
    return _CLAIMED.get()


def status_of(result: Any) -> str:
    """Classify a tool result envelope for ``execution_log.result_status``.

    ``pending_approval`` is deliberately its own status. It used to be
    recorded as ``failure`` because the envelope carries no ``success``
    key, which made the audit trail claim a tool had failed when what
    actually happened is that FERAL asked the operator a question. The
    live store holds three consecutive ``workspace_scripts__rerun`` rows
    with identical args, distinct ``request_id`` values and
    ``result_status='failure'``: the model was told its call failed and
    re-issued it, so one approval prompt became three.
    """
    if not isinstance(result, dict):
        return "success" if result else "failure"
    status = str(result.get("status") or "")
    if status == "pending_approval":
        return "pending_approval"
    if result.get("success") is True:
        return "success"
    # The hardware daemon acks asynchronously; the orchestrator has
    # treated this as success since the WS protocol landed.
    if status == "command_sent_to_hardware_daemon":
        return "success"
    return "failure"


def _resolve_memory() -> tuple[Optional[Any], Optional[str]]:
    """Return ``(memory_store, unavailable_reason)``.

    Reached through ``sys.modules`` rather than an import so ``memory``
    does not gain a dependency on ``api``, and so a process that never
    booted a brain (tests, the CLI, an embedder) has no store rather than
    a broken one. That case is normal and silent. ``api.state`` present
    with no store is not normal, and says so.
    """
    state_mod = sys.modules.get("api.state")
    if state_mod is None:
        return None, None
    state_obj = getattr(state_mod, "state", None)
    if state_obj is None:
        return None, None
    store = getattr(state_obj, "memory", None)
    if store is None:
        return None, "api.state is loaded but carries no memory store"
    if not hasattr(store, "log_execution"):
        return None, f"memory store {type(store).__name__} has no log_execution"
    return store, None


async def record_execution(
    *,
    session_id: str,
    tool_name: str,
    args: Any,
    result: Any,
    latency_ms: float = 0.0,
    surface: str = "",
) -> Optional[str]:
    """Write one ``execution_log`` row. Returns the row id, or None.

    Never raises: losing the audit record of a tool call must not turn a
    working tool call into a failed one. It does not fail quietly either
    - every reason for not writing is logged once per process, because a
    trail that stops without a log line is what produced the 2.5-month
    gap this module exists to close.
    """
    store, reason = _resolve_memory()
    if store is None:
        if reason:
            _warn_once(
                "no-store",
                "tool-execution audit disabled: %s. Tool calls will run "
                "but execution_log will not record them.",
                reason,
            )
        return None

    skill_id, _, endpoint_id = str(tool_name or "").partition("__")

    try:
        summary = json.dumps(result, default=str)
    except Exception:
        summary = repr(result)

    try:
        return await store.log_execution(
            session_id=session_id or "",
            skill_id=skill_id or str(tool_name or ""),
            endpoint_id=endpoint_id,
            args=args if isinstance(args, dict) else {"_args": repr(args)},
            result_status=status_of(result),
            result_summary=summary[:500],
            latency_ms=latency_ms,
        )
    except Exception as exc:
        # Warned rather than swallowed: the store is reachable and the
        # insert still failed, which is a real defect somewhere below.
        _warn_once(
            f"write-failed:{type(exc).__name__}",
            "tool-execution audit write failed (%s: %s); execution_log is "
            "incomplete from this point on. surface=%s tool=%s",
            type(exc).__name__, exc, surface or "unknown", tool_name,
        )
        return None
