"""Tool-iteration budget — unlimited by default, stopped by progress guards.

v2026.6.11: hardware execution is closed-loop (command → verify → diagnose
→ retry), so an arbitrary fixed iteration cap (formerly 20 in the
orchestrator, 4 in the multi-agent AgentWorker) could cut an agent off in
the middle of a legitimate task. The budget is now:

* ``agents.max_tool_iterations`` — persisted in ``~/.feral/settings.json``
  (same mechanism as ``security.autonomy_mode``) and settable via
  ``POST /api/config/update``. ``0``/unset = UNLIMITED (the default).
  The ``FERAL_MAX_ITERATIONS`` env var wins when set (ops pin).
* A NO-PROGRESS guard replaces the count cap: the loop stops issuing
  tools once the LLM repeats the exact same tool call with the same
  arguments and gets the same failing result twice in a row.
* A generous, configurable wall-clock backstop
  (``agents.tool_loop_max_seconds``, default 900s; env override
  ``FERAL_TOOL_LOOP_MAX_SECONDS``; 0 = disabled) catches pathological
  non-identical spins that the signature guard cannot see.

A loop always ends naturally when the LLM returns a final text answer
with no tool calls — that path is untouched.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional, Tuple

logger = logging.getLogger("feral.agents.budget")

DEFAULT_MAX_TOOL_ITERATIONS = 0  # 0 = unlimited
DEFAULT_TOOL_LOOP_MAX_SECONDS = 900.0

# Injected into the conversation when the no-progress guard trips so the
# model produces an honest final answer instead of a fourth identical call.
NO_PROGRESS_GUIDANCE = (
    "(LOOP GUARD: you issued the exact same tool call with the same "
    "arguments twice in a row and it failed identically both times. "
    "Tool access for this turn is now closed. Give the user your final "
    "answer: state plainly what you tried, what failed, and what they "
    "can do about it. Do NOT claim the action succeeded.)"
)


def _load_settings_safely() -> dict:
    try:
        from config.loader import load_settings
        return load_settings() or {}
    except Exception:
        return {}


def resolve_max_tool_iterations(settings: Optional[dict] = None) -> int:
    """Tool-loop iteration limit. 0 = unlimited (default).

    Precedence: ``FERAL_MAX_ITERATIONS`` env var →
    ``agents.max_tool_iterations`` in settings.json → unlimited.
    """
    env = os.environ.get("FERAL_MAX_ITERATIONS", "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            logger.warning("Ignoring non-integer FERAL_MAX_ITERATIONS=%r", env)
    if settings is None:
        settings = _load_settings_safely()
    raw = (settings.get("agents") or {}).get(
        "max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS
    )
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOOL_ITERATIONS


def resolve_tool_loop_max_seconds(settings: Optional[dict] = None) -> float:
    """Wall-clock backstop for a single tool loop. 0 = disabled."""
    env = os.environ.get("FERAL_TOOL_LOOP_MAX_SECONDS", "").strip()
    if env:
        try:
            return max(0.0, float(env))
        except ValueError:
            logger.warning("Ignoring non-numeric FERAL_TOOL_LOOP_MAX_SECONDS=%r", env)
    if settings is None:
        settings = _load_settings_safely()
    raw = (settings.get("agents") or {}).get(
        "tool_loop_max_seconds", DEFAULT_TOOL_LOOP_MAX_SECONDS
    )
    try:
        return max(0.0, float(raw or 0.0))
    except (TypeError, ValueError):
        return DEFAULT_TOOL_LOOP_MAX_SECONDS


def _stable_key(value: Any, limit: int = 4000) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)[:limit]
    except Exception:
        return repr(value)[:limit]


class NoProgressGuard:
    """Detects the no-progress signature: the exact same tool call with the
    same arguments producing the same failing result twice in a row.

    Successful calls (and any *different* call/args/result) reset the
    streak — repetition with changing results IS progress (e.g. polling).
    """

    def __init__(self) -> None:
        self._last_sig: Optional[Tuple[str, str, str]] = None
        self._streak = 0
        self.tripped = False

    def observe(self, tool_name: str, args: Any, success: bool, result: Any) -> bool:
        """Record one executed tool call; returns True when the guard trips."""
        if success:
            self._last_sig = None
            self._streak = 0
            return False
        sig = (str(tool_name), _stable_key(args), _stable_key(result))
        if sig == self._last_sig:
            self._streak += 1
        else:
            self._last_sig = sig
            self._streak = 1
        if self._streak >= 2:
            self.tripped = True
            logger.warning(
                "No-progress guard tripped: %s repeated %d× with identical "
                "args and identical failing result",
                tool_name, self._streak,
            )
            return True
        return False


class IterationBudget:
    """Governor for one agent tool loop: optional user-set iteration limit,
    generous wall-clock backstop, and the no-progress guard."""

    def __init__(self, max_iterations: int = 0, max_seconds: float = 0.0):
        self.max_iterations = max(0, int(max_iterations or 0))
        self.max_seconds = max(0.0, float(max_seconds or 0.0))
        self.iterations = 0
        self.guard = NoProgressGuard()
        self.stop_reason = ""
        self._started = time.monotonic()

    def start_iteration(self) -> bool:
        """Call at the top of each loop round; False = stop the loop.

        The first round is always allowed — the wall clock only gates
        CONTINUATION, so a turn that starts near the deadline still gets
        one LLM round instead of silently producing nothing.
        """
        if self.max_iterations and self.iterations >= self.max_iterations:
            self.stop_reason = (
                f"user-set max_tool_iterations={self.max_iterations} reached"
            )
            logger.warning("Tool loop stopped: %s", self.stop_reason)
            return False
        if (
            self.iterations > 0
            and self.max_seconds
            and (time.monotonic() - self._started) > self.max_seconds
        ):
            self.stop_reason = (
                f"wall-clock budget {self.max_seconds:.0f}s exhausted"
            )
            logger.warning("Tool loop stopped: %s", self.stop_reason)
            return False
        self.iterations += 1
        return True

    def observe_tool(self, tool_name: str, args: Any, success: bool, result: Any) -> bool:
        """Feed one tool execution; True = no-progress guard tripped."""
        tripped = self.guard.observe(tool_name, args, success, result)
        if tripped and not self.stop_reason:
            self.stop_reason = (
                "no progress: identical failing tool call repeated twice in a row"
            )
        return tripped
