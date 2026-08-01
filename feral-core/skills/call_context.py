"""Ambient tool-call identity for skill implementations.

Why this module exists
----------------------
``BaseSkill.execute(endpoint_id, args, vault)`` has no session identity.
``SkillExecutor._execute_inner`` calls it (``skills/executor.py:225``) with
only the endpoint id, the args dict and the vault, and the last layer that
still knows *who* is calling is ``ToolRunner.execute_tool_call_for_llm``
(``agents/tool_runner.py``), which drops ``session_id`` on the floor when it
hands off to the executor.

The reliability layer (read-before-edit tracking, checkpoints) is per
session and per turn, so that identity has to reach the implementation.
Threading a parameter through would change ``BaseSkill.execute``'s
signature and every third-party skill with it, so identity travels
out-of-band in a :class:`~contextvars.ContextVar`.

A contextvar is the right primitive here because:

* every dispatch path is ``async`` on a single event loop, so there is no
  thread-affinity problem;
* ``contextvars`` propagate into ``asyncio.Task``, so the children that
  ``ToolRunner.spawn_subagents`` creates inherit the parent's context and
  then rebind it with their own sub-session id;
* rebinding is stack-shaped (token-based reset), so a nested bind restores
  the outer context exactly.

Fail-open, deliberately
-----------------------
Callers that never bind (cron, taskflows, the REST tool surface, the voice
proxies) get :data:`UNBOUND`, whose ``session_id`` is empty. Consumers are
expected to log a warning and continue. This is a correctness aid, not a
security boundary: the boundary is ``security/sandbox_policy.py`` plus the
approval flow, and any guard keyed off this context is bypassable through
``coding_tools__bash`` running ``sed`` anyway. Failing closed would break
cron-driven and taskflow writes on day one for no security gain.

Kill switch
-----------
``FERAL_TOOL_CALL_CONTEXT=off`` (settings key ``coding.tool_call_context``)
makes :func:`current_context` and :func:`require_context` return
:data:`UNBOUND` for every caller, which is the same fail-open state an
unbound cron job already gets. See :func:`context_enabled`.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator, Optional
from uuid import uuid4

logger = logging.getLogger("feral.skills.call_context")

__all__ = [
    "ToolCallContext",
    "UNBOUND",
    "bind_context",
    "context_enabled",
    "current_context",
    "require_context",
    "new_turn_id",
]


@dataclass(frozen=True)
class ToolCallContext:
    """Identity of the tool call currently executing on this task.

    ``session_id``
        The orchestrator session. Empty string means "unbound"; see
        :data:`UNBOUND`.
    ``surface``
        Where the call came in from (``websocket``, ``http_api``,
        ``voice``, ...). Mirrors the value the safety resolver uses.
    ``tool_name``
        Fully-qualified ``skill__endpoint``.
    ``call_id``
        The LLM's tool-call id when it supplied one; unique per call.
    ``turn_id``
        Stable id for the user message this call belongs to. Every tool
        call made while answering one user message shares it, including
        calls made by spawned subagents. This is the grouping key
        ``skills/checkpoints.py`` reverts by.
    ``plan_mode``
        Whether the calling session is in plan mode. Informational only
        on this object: the enforcement point is
        ``ToolRunner.enforce_plan_mode``, which asks
        ``PlanModeState.is_active`` directly rather than reading this
        field. See ``agents/plan_mode.py``.
    """

    session_id: str = ""
    surface: str = ""
    tool_name: str = ""
    call_id: str = ""
    turn_id: str = ""
    plan_mode: bool = False

    @property
    def bound(self) -> bool:
        return bool(self.session_id)


UNBOUND = ToolCallContext()

_CURRENT: ContextVar[ToolCallContext] = ContextVar(
    "feral_tool_call_context", default=UNBOUND,
)

# One warning per feature per process is enough to get telemetry without
# turning an unbound cron loop into a log flood.
_warned: set[str] = set()
_warn_lock = threading.Lock()


def _warn_once(token: str, message: str, *args) -> None:
    """Log ``message`` the first time this process sees ``token``."""
    with _warn_lock:
        first = token not in _warned
        _warned.add(token)
    if first:
        logger.warning(message, *args)


def new_turn_id() -> str:
    """Mint a fresh turn id."""
    return f"turn_{uuid4().hex[:16]}"


def current_context() -> ToolCallContext:
    """Return the context bound to this task, or :data:`UNBOUND`.

    Returns :data:`UNBOUND` regardless of what is bound when
    :func:`context_enabled` is false. That is the whole of the kill
    switch: every consumer reads identity through this function or
    through :func:`require_context`, so refusing to hand identity out
    here is what makes ``FERAL_TOOL_CALL_CONTEXT=off`` mean what its
    name says.
    """
    if not context_enabled():
        return UNBOUND
    return _CURRENT.get()


def require_context(feature: str) -> ToolCallContext:
    """Return the current context, warning once per feature when unbound.

    Never raises. See the module docstring for why this fails open.
    """
    if not context_enabled():
        _warn_once(
            "kill-switch",
            "FERAL_TOOL_CALL_CONTEXT is off; per-session tool state "
            "(read-before-edit tracking, checkpoints, turn grouping) is "
            "disabled for every call path in this process.",
        )
        return UNBOUND
    ctx = _CURRENT.get()
    if ctx.bound:
        return ctx
    _warn_once(
        feature,
        "%s ran without a bound ToolCallContext; per-session state is "
        "disabled for this call path. Bind via "
        "skills.call_context.bind_context() at the dispatch site.",
        feature,
    )
    return ctx


@contextmanager
def bind_context(
    *,
    session_id: str = "",
    surface: str = "",
    tool_name: str = "",
    call_id: str = "",
    turn_id: str = "",
    plan_mode: Optional[bool] = None,
) -> Iterator[ToolCallContext]:
    """Bind tool-call identity for the duration of the block.

    Fields left at their defaults inherit from the enclosing context, so a
    nested bind that only sets ``tool_name`` keeps the outer session and
    turn. Reset uses the ``ContextVar`` token, which restores the previous
    value even when binds interleave across ``await`` points.
    """
    parent = _CURRENT.get()
    ctx = replace(
        parent,
        session_id=session_id or parent.session_id,
        surface=surface or parent.surface,
        tool_name=tool_name or parent.tool_name,
        call_id=call_id or parent.call_id,
        turn_id=turn_id or parent.turn_id,
        plan_mode=parent.plan_mode if plan_mode is None else bool(plan_mode),
    )
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def context_enabled() -> bool:
    """Is tool-call identity binding on? ``FERAL_TOOL_CALL_CONTEXT``.

    Default on. ``off`` / ``0`` / ``false`` / ``no`` make
    :func:`current_context` and :func:`require_context` return
    :data:`UNBOUND` no matter what is bound, which turns off every
    consumer of per-session tool state: read-before-edit tracking, the
    checkpoint turn grouping, and the plan-mode session lookups in
    ``skills/impl/plan.py`` and ``skills/impl/feral_workflows.py``.

    The escape hatch exists for the same reason
    ``security/exec_mode.unwrap_enabled`` does: the reliability layer
    keyed off this context can refuse a legitimate edit (a write the
    tracker believes was not preceded by a read), and an operator who
    hits that needs a way to keep working rather than a reason to stop
    using the coding tools. It is not a security control. The module
    docstring is explicit that this context is a correctness aid and
    that the boundary is ``security/sandbox_policy.py`` plus the
    approval flow, so switching it off widens no permission.

    Read on every call rather than cached, so a runtime write through
    ``ConfigLoader.update_settings`` (which re-exports the env var)
    takes effect on the next tool call rather than the next boot.
    """
    return os.environ.get("FERAL_TOOL_CALL_CONTEXT", "on").strip().lower() not in (
        "off", "0", "false", "no",
    )
