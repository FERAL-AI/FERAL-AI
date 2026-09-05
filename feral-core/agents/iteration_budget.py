"""Tool-iteration budget — unlimited by default, stopped by progress guards.

v2026.6.11: hardware execution is closed-loop (command → verify → diagnose
→ retry), so an arbitrary fixed iteration cap (formerly 20 in the
orchestrator, 4 in the multi-agent AgentWorker) could cut an agent off in
the middle of a legitimate task. The budget is now:

* ``agents.max_tool_iterations`` — persisted in ``~/.feral/settings.json``
  (same mechanism as ``security.autonomy_mode``) and settable via
  ``POST /api/config/update``. ``0``/unset = UNLIMITED (the default).
  The ``FERAL_MAX_ITERATIONS`` env var wins when set (ops pin).
* A NO-PROGRESS guard replaces the count cap (see below).
* A generous, configurable wall-clock backstop
  (``agents.tool_loop_max_seconds``, default 900s; env override
  ``FERAL_TOOL_LOOP_MAX_SECONDS``; 0 = disabled) catches pathological
  non-identical spins that the signature guard cannot see.

A loop always ends naturally when the LLM returns a final text answer
with no tool calls — that path is untouched.

v2026.7.30 — the guard was too eager and its response was too blunt.
It tripped after **two** identical failing calls and the orchestrator
answered by setting ``tools = None`` for the rest of the turn. So an agent
ten steps into a real task that hit one unavailable tool twice (a daemon
that had not finished booting, a rate-limited API) lost its ENTIRE
toolset — every unrelated read, search and edit tool with it — and had to
apologise instead of routing around the failure. Two transient failures
should not disarm the agent.

The guard is now two-level, and both levels are configurable
(``agents.no_progress_warn_threshold`` / ``agents.no_progress_stop_threshold``
in settings.json; ``FERAL_NO_PROGRESS_WARN_THRESHOLD`` /
``FERAL_NO_PROGRESS_STOP_THRESHOLD`` env pins):

* ``GUARD_WARN`` after ``warn_threshold`` (default 4) repetitions of the
  exact same call **with the same failing result**. Tools stay available;
  the model is told to change approach. Repetition with a *changing*
  result is still progress (polling) and never counts.
* ``GUARD_STOP`` after ``stop_threshold`` (default 8) consecutive failures
  of the same call+args regardless of whether the failure body changes.
  This looser signature is what actually terminates a spin: once
  ``ToolRunner.register_tool_attempt`` starts hard-blocking a repeated
  call it returns an envelope carrying an incrementing ``anti_loop_streak``
  counter, so the failing body is never byte-identical and the strict
  signature alone would spin until the wall clock. Only at ``GUARD_STOP``
  is the toolset withdrawn for a final honest answer.

v2026.9.5: both of those key on ``(tool, args)`` and reset when the args
change, and the WARN text tells the model to "change the arguments". An
agent that takes the advice therefore resets the counter meant to stop
it. Measured: routine-10 called ``cutebot__set_lights`` 46 times against
a disconnected robot, walking rgb 255,0,0 then 254, 253, 252, each answered
with the same 503 "CuteBot is not connected", and neither streak ever
reached its threshold.

* A third streak counts PRECONDITION failures per tool and ignores args
  entirely (``agents.unavailable_tool_threshold``, default 3;
  ``FERAL_UNAVAILABLE_TOOL_THRESHOLD`` env pin). No value of any argument
  connects a robot, authorises an OAuth app or installs Docker, so after
  three the tool is dropped from the list passed to the next round and the
  model is told once, by name, why. Every OTHER tool stays available: this
  withdraws one broken capability, not the agent's hands.
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

# Two-level no-progress thresholds. See the module docstring for why the
# warn level is no longer 2 and why stopping uses a looser signature.
DEFAULT_NO_PROGRESS_WARN_THRESHOLD = 4
DEFAULT_NO_PROGRESS_STOP_THRESHOLD = 8

# Third guard, and the only one that survives an agent varying its
# arguments. Both streaks above key on ``(tool, args)`` and reset the
# moment the args change, while the WARN text they produce tells the
# model to "change the arguments", so an agent that follows the advice
# resets the counter that was supposed to stop it.
#
# routine-10 did exactly that on the operator's brain: 46 calls to
# ``cutebot__set_lights`` walking rgb 255,0,0 then 254, 253, 252 against
# a robot that was not plugged in, every one answered with the same
# deterministic 503 "CuteBot is not connected". Not a loop by either
# existing definition, and 46 calls of pure loss.
#
# A precondition failure is not an argument problem. No value of any
# argument connects a robot, authorises an OAuth app or installs Docker,
# so this streak counts per TOOL and ignores args entirely.
DEFAULT_UNAVAILABLE_TOOL_THRESHOLD = 3

# Status codes that mean "the precondition for this tool is absent":
# 503 unavailable (no daemon, no sandbox, nothing configured),
# 412 precondition failed, 424 failed dependency, 401/403 not authorised.
# Deliberately NOT 429 (rate limited, waiting helps) or 5xx generally (a
# transient server fault may well clear).
PRECONDITION_STATUS_CODES = frozenset({401, 403, 412, 424, 503})

# ...and the phrases integrations use when they answer without one. Kept
# alongside the codes because ten integration modules return
# ``{"success": False, "error": ...}`` with no status_code at all (see
# the comment in ``skills/executor.py`` about the flat ``.get(..., 200)``
# that used to stamp HTTP 200 on every one of them).
PRECONDITION_ERROR_PHRASES = (
    "not connected",
    "not configured",
    "not installed",
    "not authorised",
    "not authorized",
    "no live websocket",
    "undeliverable frame",
    "sandbox required",
    "sandbox unavailable",
    "no usb robot",
    "requires authentication",
)

# Guard levels, ordered by severity. Falsy "" means "no problem", so a
# caller that only cares whether something tripped can still use a plain
# truth test.
GUARD_OK = ""
GUARD_WARN = "warn"
GUARD_STOP = "stop"

# Level 1. Tools stay available on purpose: the agent may be ten useful
# steps into a task and just needs to route around ONE broken tool.
NO_PROGRESS_WARNING = (
    "(LOOP GUARD: you have issued the exact same tool call with the same "
    "arguments several times in a row and it failed identically every "
    "time. Repeating it again will not help. Your other tools still work "
    "— change the arguments, use a different tool, or tell the user what "
    "is broken. Do NOT claim the action succeeded.)"
)

# Level 2. Injected when the toolset is actually withdrawn, so the model
# produces an honest final answer instead of another identical call.
NO_PROGRESS_GUIDANCE = (
    "(LOOP GUARD: the same failing tool call has now been repeated well "
    "past the warning and nothing has changed. Tool access for this turn "
    "is now closed. Give the user your final answer: state plainly what "
    "you tried, what failed, and what they can do about it. Do NOT claim "
    "the action succeeded.)"
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


def _resolve_int_setting(
    env_var: str, settings_key: str, default: int, settings: Optional[dict] = None,
) -> int:
    """Shared ``env → settings.json → default`` resolution for the guard
    thresholds. Mirrors ``resolve_max_tool_iterations``; kept generic so the
    two thresholds do not duplicate the same twelve lines."""
    env = os.environ.get(env_var, "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            logger.warning("Ignoring non-integer %s=%r", env_var, env)
    if settings is None:
        settings = _load_settings_safely()
    raw = (settings.get("agents") or {}).get(settings_key, default)
    try:
        return max(1, int(raw or default))
    except (TypeError, ValueError):
        return default


def resolve_no_progress_warn_threshold(settings: Optional[dict] = None) -> int:
    """Identical failing repetitions before the model is warned."""
    return _resolve_int_setting(
        "FERAL_NO_PROGRESS_WARN_THRESHOLD", "no_progress_warn_threshold",
        DEFAULT_NO_PROGRESS_WARN_THRESHOLD, settings,
    )


def resolve_no_progress_stop_threshold(settings: Optional[dict] = None) -> int:
    """Consecutive failures of the same call+args before tools are withdrawn."""
    return _resolve_int_setting(
        "FERAL_NO_PROGRESS_STOP_THRESHOLD", "no_progress_stop_threshold",
        DEFAULT_NO_PROGRESS_STOP_THRESHOLD, settings,
    )


def resolve_unavailable_tool_threshold(settings: Optional[dict] = None) -> int:
    """Precondition failures of one tool before it stops being offered."""
    return _resolve_int_setting(
        "FERAL_UNAVAILABLE_TOOL_THRESHOLD", "unavailable_tool_threshold",
        DEFAULT_UNAVAILABLE_TOOL_THRESHOLD, settings,
    )


def _stable_key(value: Any, limit: int = 4000) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)[:limit]
    except Exception:
        return repr(value)[:limit]


def precondition_failure_reason(result: Any) -> str:
    """The reason this result is a missing precondition, or "".

    Answers "no argument will fix this", which is a different question
    from "did this fail". A 404 from a search, a 500 from a flaky API and
    a rejected parameter are all failures worth retrying differently; a
    503 saying the robot is not plugged in is not.
    """
    if not isinstance(result, dict):
        return ""
    if result.get("success"):
        return ""
    error = str(result.get("error") or "").strip()
    try:
        status = int(result.get("status_code") or 0)
    except (TypeError, ValueError):
        status = 0
    if status in PRECONDITION_STATUS_CODES:
        return error or f"the tool answered HTTP {status}"
    lowered = error.lower()
    for phrase in PRECONDITION_ERROR_PHRASES:
        if phrase in lowered:
            return error
    return ""


def unavailable_tool_notice(tool_name: str, reason: str) -> str:
    """The one message the model gets when a tool is withdrawn."""
    return (
        f"(TOOL UNAVAILABLE: `{tool_name}` has failed its precondition "
        f"repeatedly and has been removed from your tools for the rest of "
        f"this turn. Reason: {reason} No arguments will change that, so do "
        f"not try different values. Use another tool or tell the user what "
        f"is missing. Do NOT claim the action succeeded.)"
    )


def withdrawn_tool_refusal(tool_name: str, reason: str) -> dict:
    """Envelope for a call to a tool that was already withdrawn.

    Removing a tool from the list is advisory: a model can name a tool it
    was not given, and on the multi-agent path nothing else stops it.
    ``ToolRunner`` has its own hard anti-loop block for the orchestrator
    path; this is the same idea in the shape the executor returns, so a
    withdrawn tool is withdrawn on both.
    """
    return {
        "success": False,
        "status_code": 503,
        "data": None,
        "error": (
            f"{tool_name} was withdrawn for this turn after repeated "
            f"precondition failures ({reason}). It was not called."
        ),
    }


def drop_unavailable_tools(
    tools: Optional[list], unavailable: dict[str, str],
) -> Optional[list]:
    """Remove withdrawn tools from a tool list, preserving order.

    Shared by the orchestrator and the multi-agent worker so the two
    cannot drift; ``agents/multi_agent.py`` calls ``SkillExecutor``
    directly and never reaches ``ToolRunner``'s anti-loop block, which is
    exactly how routine-10 got 46 calls in.
    """
    if not tools or not unavailable:
        return tools
    from agents.tool_list import tool_name_from_def

    kept = [t for t in tools if tool_name_from_def(t) not in unavailable]
    return kept or None


class NoProgressGuard:
    """Two-level detector for a stuck tool loop.

    Strict streak — same tool, same args, byte-identical failing result —
    drives ``GUARD_WARN``. Loose streak — same tool, same args, still
    failing, body irrelevant — drives ``GUARD_STOP``; it is the level that
    actually terminates the loop, because the anti-loop block envelope
    carries an incrementing counter and is therefore never byte-identical.

    Successful calls, and any call with different args, reset both streaks:
    repetition with changing results IS progress (e.g. polling).
    """

    def __init__(
        self,
        warn_threshold: Optional[int] = None,
        stop_threshold: Optional[int] = None,
        unavailable_threshold: Optional[int] = None,
    ) -> None:
        self.unavailable_threshold = (
            resolve_unavailable_tool_threshold()
            if unavailable_threshold is None
            else max(1, int(unavailable_threshold))
        )
        #: tool name -> consecutive precondition failures, args ignored.
        self._precondition_streaks: dict[str, int] = {}
        #: tool name -> reason, for every tool withdrawn this turn.
        self.unavailable_tools: dict[str, str] = {}
        #: Withdrawals the caller has not yet told the model about.
        self._unannounced: list[tuple[str, str]] = []
        self.warn_threshold = (
            resolve_no_progress_warn_threshold()
            if warn_threshold is None else max(1, int(warn_threshold))
        )
        self.stop_threshold = (
            resolve_no_progress_stop_threshold()
            if stop_threshold is None else max(1, int(stop_threshold))
        )
        # A stop that would fire before the warning is a misconfiguration;
        # keep the ordering so the model always gets its warning round.
        self.stop_threshold = max(self.stop_threshold, self.warn_threshold)
        self._last_sig: Optional[Tuple[str, str, str]] = None
        self._last_call: Optional[Tuple[str, str]] = None
        self._streak = 0
        self._call_streak = 0
        self.tripped = False
        self.level = GUARD_OK

    def take_unannounced_unavailable(self) -> list[tuple[str, str]]:
        """Drain the tools withdrawn since the last call.

        Drained rather than read so the caller tells the model once per
        withdrawal. The tool stays in :attr:`unavailable_tools`, which is
        what the tool-list filter reads on every subsequent round.
        """
        pending, self._unannounced = self._unannounced, []
        return pending

    def _observe_precondition(self, tool_name: str, success: bool, result: Any) -> None:
        """Track the args-independent precondition streak for one tool."""
        name = str(tool_name)
        if success:
            self._precondition_streaks.pop(name, None)
            return
        reason = precondition_failure_reason(result)
        if not reason:
            # A failure that arguments might fix. The two streaks above
            # own that case; this one must not claim it.
            self._precondition_streaks.pop(name, None)
            return
        streak = self._precondition_streaks.get(name, 0) + 1
        self._precondition_streaks[name] = streak
        if streak < self.unavailable_threshold or name in self.unavailable_tools:
            return
        self.unavailable_tools[name] = reason
        self._unannounced.append((name, reason))
        logger.warning(
            "Tool withdrawn for this turn: %s failed its precondition %d× "
            "regardless of arguments (%s). It will not be offered again "
            "this turn.",
            name, streak, reason,
        )

    def observe(self, tool_name: str, args: Any, success: bool, result: Any) -> str:
        """Record one executed tool call; returns ``GUARD_OK`` / ``GUARD_WARN``
        / ``GUARD_STOP``. Falsy when nothing is wrong.

        The returned level is unchanged by the precondition streak: that
        one withdraws a single tool rather than ending the loop, and the
        caller reads it from :attr:`unavailable_tools` and
        :meth:`take_unannounced_unavailable`.
        """
        self._observe_precondition(tool_name, success, result)

        if success:
            self._last_sig = None
            self._last_call = None
            self._streak = 0
            self._call_streak = 0
            self.level = GUARD_OK
            return GUARD_OK

        call = (str(tool_name), _stable_key(args))
        if call == self._last_call:
            self._call_streak += 1
        else:
            self._last_call = call
            self._call_streak = 1

        sig = (call[0], call[1], _stable_key(result))
        if sig == self._last_sig:
            self._streak += 1
        else:
            self._last_sig = sig
            self._streak = 1

        if self._call_streak >= self.stop_threshold:
            self.tripped = True
            self.level = GUARD_STOP
            logger.warning(
                "No-progress guard STOP: %s failed %d× in a row with identical "
                "args; withdrawing tools for a final answer",
                tool_name, self._call_streak,
            )
            return GUARD_STOP

        if self._streak >= self.warn_threshold:
            self.tripped = True
            self.level = GUARD_WARN
            logger.warning(
                "No-progress guard WARN: %s repeated %d× with identical args "
                "and identical failing result; tools stay available",
                tool_name, self._streak,
            )
            return GUARD_WARN

        self.level = GUARD_OK
        return GUARD_OK


class IterationBudget:
    """Governor for one agent tool loop: optional user-set iteration limit,
    generous wall-clock backstop, and the no-progress guard."""

    def __init__(
        self,
        max_iterations: int = 0,
        max_seconds: float = 0.0,
        warn_threshold: Optional[int] = None,
        stop_threshold: Optional[int] = None,
        unavailable_threshold: Optional[int] = None,
    ):
        self.max_iterations = max(0, int(max_iterations or 0))
        self.max_seconds = max(0.0, float(max_seconds or 0.0))
        self.iterations = 0
        self.guard = NoProgressGuard(
            warn_threshold, stop_threshold, unavailable_threshold,
        )
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

    def observe_tool(self, tool_name: str, args: Any, success: bool, result: Any) -> str:
        """Feed one tool execution; returns the guard level (``GUARD_OK`` /
        ``GUARD_WARN`` / ``GUARD_STOP``). Only ``GUARD_STOP`` justifies
        withdrawing the toolset — see the module docstring."""
        level = self.guard.observe(tool_name, args, success, result)
        if level == GUARD_STOP and not self.stop_reason:
            self.stop_reason = (
                "no progress: the same failing tool call was repeated "
                f"{self.guard.stop_threshold}× in a row"
            )
        return level

    # ── Withdrawn tools (precondition streak) ────────────────────────
    #
    # Thin pass-throughs so a caller holding an IterationBudget does not
    # have to know the guard exists. Both loops that run tools use these.

    @property
    def unavailable_tools(self) -> dict[str, str]:
        """Tools withdrawn for the rest of this turn -> why."""
        return self.guard.unavailable_tools

    def take_unannounced_unavailable(self) -> list[tuple[str, str]]:
        """Withdrawals the model has not been told about yet."""
        return self.guard.take_unannounced_unavailable()

    def filter_tools(self, tools: Optional[list]) -> Optional[list]:
        """``tools`` minus anything withdrawn this turn."""
        return drop_unavailable_tools(tools, self.guard.unavailable_tools)
