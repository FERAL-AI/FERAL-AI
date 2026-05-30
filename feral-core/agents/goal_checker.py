"""PR 7: GoalChecker — deterministic done/blocked/continue verdict for
long-horizon work ( subsessions, TaskFlows, IntentCompiler plans).

Design
------
The goal checker is *not* an LLM. It's a small, auditable rules engine
that an autonomy loop calls between micro-steps to decide whether to:

* ``done``     — the user-visible objective is satisfied and the loop
                should stop.
* ``blocked``  — progress is stuck on something the runtime cannot
                resolve on its own (a permission grant, a missing
                credential, a captcha, a destructive confirmation),
                so the loop should surface a remediation card and
                yield control to the operator.
* ``continue`` — neither of the above; the loop may take another
                micro-step.

The decision is derived from a typed ``GoalContext`` so we can unit-test
the matrix exhaustively. Higher layers may *also* prompt an LLM for a
free-form verdict, but the LLM's answer is never trusted on its own:
the checker enforces the safety floor that "blocked" beats "continue"
whenever a permission/error signal is present, and "done" beats
"continue" only when the criteria explicitly evaluate True.

This module is intentionally framework-agnostic. It does not import
asyncio, FastAPI, or the orchestrator. The wiring layer (route /
runtime) builds the GoalContext from its own state and calls
``check_goal``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class GoalVerdict(str, Enum):
    DONE = "done"
    BLOCKED = "blocked"
    CONTINUE = "continue"


@dataclass(frozen=True)
class GoalDecision:
    """Output of :func:`check_goal`.

    Attributes:
        verdict: One of :class:`GoalVerdict`.
        reason:  Operator-readable short string (≤120 chars).
        evidence: List of fact strings backing the verdict; surfaced
            verbatim in the status endpoint so the user can see *why*
            the runtime stopped or kept going. Never contains secrets.
        remediation: Present only when ``verdict == BLOCKED``; tells
            the UI the exact next step the operator must take.
    """

    verdict: GoalVerdict
    reason: str
    evidence: list[str] = field(default_factory=list)
    remediation: str = ""


# A criterion is a tuple of (label, predicate_fn). Predicates take the
# GoalContext and return True/False; the label is surfaced as evidence.
GoalCriterion = tuple[str, Callable[["GoalContext"], bool]]


@dataclass
class GoalContext:
    """Snapshot of the runtime state the checker reasons about.

    Build it once per loop iteration. Mutating fields between iterations
    is fine — the dataclass exists for ergonomics, not immutability.
    """

    goal_text: str = ""
    iteration: int = 0
    max_iterations: int = 50
    elapsed_seconds: float = 0.0
    max_seconds: float = 0.0  # 0 = no wall-clock cap
    success_criteria: list[GoalCriterion] = field(default_factory=list)
    pending_approval: bool = False
    pending_approval_tool: str = ""
    pending_approval_reason: str = ""
    permission_needed: bool = False
    permission_target: str = ""
    permission_setup_step: str = ""
    last_error: str = ""
    consecutive_failures: int = 0
    max_consecutive_failures: int = 3
    progress_delta: float = 0.0  # positive = forward progress this step
    stall_threshold: int = 5  # iterations without progress before BLOCKED
    iterations_since_progress: int = 0


def check_goal(ctx: GoalContext) -> GoalDecision:
    """Apply the rules engine in priority order.

    The order is load-bearing: "blocked" outranks "done" outranks
    "continue", because a permission/error signal must always reach the
    operator even if the criteria happen to evaluate True (the runtime
    may have *partially* satisfied the goal while a side-step is now
    blocked, and forcing DONE would hide that).
    """
    # ── BLOCKED: permission needed ─────────────────────────────────
    if ctx.permission_needed:
        return GoalDecision(
            verdict=GoalVerdict.BLOCKED,
            reason=f"Permission required: {ctx.permission_target or 'unspecified'}",
            evidence=[f"permission_needed target={ctx.permission_target}"],
            remediation=ctx.permission_setup_step or (
                "Grant the required permission via `feral grant` or the "
                "Settings → Grants panel, then resume the task."
            ),
        )

    # ── BLOCKED: pending operator approval ─────────────────────────
    if ctx.pending_approval:
        return GoalDecision(
            verdict=GoalVerdict.BLOCKED,
            reason=f"Awaiting approval for {ctx.pending_approval_tool or 'tool call'}",
            evidence=[
                f"pending_approval tool={ctx.pending_approval_tool} "
                f"reason={ctx.pending_approval_reason}"
            ],
            remediation=(
                "Approve or deny the pending action in the chat permission "
                "card. The loop will resume on approval."
            ),
        )

    # ── BLOCKED: too many consecutive failures ─────────────────────
    if ctx.consecutive_failures >= ctx.max_consecutive_failures:
        return GoalDecision(
            verdict=GoalVerdict.BLOCKED,
            reason=(
                f"{ctx.consecutive_failures} consecutive failures "
                f"(limit {ctx.max_consecutive_failures}); last error: "
                f"{(ctx.last_error or 'unknown')[:80]}"
            ),
            evidence=[
                f"consecutive_failures={ctx.consecutive_failures}",
                f"last_error={ctx.last_error[:120]}",
            ],
            remediation=(
                "Inspect the last_error in the trace, fix the root cause, "
                "or steer the subsession with a corrected instruction."
            ),
        )

    # ── BLOCKED: budget exhausted ──────────────────────────────────
    if ctx.iteration >= ctx.max_iterations:
        return GoalDecision(
            verdict=GoalVerdict.BLOCKED,
            reason=f"Iteration budget exhausted ({ctx.iteration}/{ctx.max_iterations})",
            evidence=[f"iteration={ctx.iteration}", f"max_iterations={ctx.max_iterations}"],
            remediation=(
                "Raise the iteration budget for this run, or break the "
                "goal into smaller objectives."
            ),
        )

    if ctx.max_seconds > 0 and ctx.elapsed_seconds >= ctx.max_seconds:
        return GoalDecision(
            verdict=GoalVerdict.BLOCKED,
            reason=f"Wall-clock budget exhausted ({ctx.elapsed_seconds:.1f}s/{ctx.max_seconds:.1f}s)",
            evidence=[f"elapsed_seconds={ctx.elapsed_seconds:.1f}"],
            remediation="Extend the wall-clock budget or split the work.",
        )

    # ── BLOCKED: stalled (no progress) ─────────────────────────────
    if (
        ctx.stall_threshold > 0
        and ctx.iterations_since_progress >= ctx.stall_threshold
        and ctx.progress_delta <= 0
    ):
        return GoalDecision(
            verdict=GoalVerdict.BLOCKED,
            reason=f"Stalled — {ctx.iterations_since_progress} iterations without progress",
            evidence=[
                f"iterations_since_progress={ctx.iterations_since_progress}",
                f"stall_threshold={ctx.stall_threshold}",
            ],
            remediation=(
                "Steer the subsession with new context, switch tools, or "
                "ask the user a targeted clarification question."
            ),
        )

    # ── DONE: all explicit success criteria pass ───────────────────
    if ctx.success_criteria:
        passed: list[str] = []
        failed: list[str] = []
        for label, predicate in ctx.success_criteria:
            try:
                ok = bool(predicate(ctx))
            except Exception as exc:  # pragma: no cover - defensive
                failed.append(f"{label}: predicate raised {exc!s}")
                continue
            (passed if ok else failed).append(label)
        if not failed:
            return GoalDecision(
                verdict=GoalVerdict.DONE,
                reason="All success criteria satisfied",
                evidence=[f"criterion_passed: {label}" for label in passed],
            )

    # ── CONTINUE (default) ─────────────────────────────────────────
    return GoalDecision(
        verdict=GoalVerdict.CONTINUE,
        reason="Progress in motion; no terminal signal yet",
        evidence=[
            f"iteration={ctx.iteration}",
            f"progress_delta={ctx.progress_delta}",
        ],
    )


__all__ = [
    "GoalCriterion",
    "GoalContext",
    "GoalDecision",
    "GoalVerdict",
    "check_goal",
]
