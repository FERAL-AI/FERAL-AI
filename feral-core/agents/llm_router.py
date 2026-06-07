"""Adaptive prompt → tier routing for the interactive chat path.

A small, dependency-free decision layer that estimates the *difficulty*
of a turn and maps it to a model tier (``cheap`` / ``balanced`` /
``premium``). It is deliberately a heuristic classifier — no extra model
call on the hot path — plus a cost-headroom downshift.

The tier is turned into a concrete ``(provider, model)`` by
:meth:`LLMProvider.route_call`, which — for the auto (non-operator-
override) path — only ever picks a cheaper/stronger model *within the
user's already-configured provider*. That invariant is what makes
adaptive routing safe to enable by default: it can never silently route
a turn to a provider the operator has no key for, and any miss falls
back through the normal failover chain to the primary model.

Design notes (grounded in mid-2026 routing practice):

* The classifier is intentionally *conservative about going cheap* —
  only clearly trivial turns (greetings, acks, very short asks) are
  downshifted, so perceived quality never regresses on substantive work.
* ``premium`` requires a real signal (code, math, multi-step reasoning,
  long context). Tool-calling turns never go ``cheap`` — agentic turns
  want a competent model.
* Escalation (a "verifier-gated cascade"): when a turn produces an
  empty / refusal / plan-only answer, the orchestrator bumps the tier
  one step and retries, so a wrong cheap answer is recovered by a
  stronger model rather than shipped.
"""

from __future__ import annotations

import re
from typing import Final

CHEAP: Final[str] = "cheap"
BALANCED: Final[str] = "balanced"
PREMIUM: Final[str] = "premium"

TIER_ORDER: Final[tuple[str, ...]] = (CHEAP, BALANCED, PREMIUM)

# Substantive-reasoning markers. A single hit nudges toward at least
# balanced; two or more (or a hit alongside tools/length) means premium.
_PREMIUM_KEYWORDS: Final[tuple[str, ...]] = (
    "debug", "refactor", "architect", "architecture", "design",
    "prove", "derive", "derivation", "theorem", "analyze", "analysis",
    "optimize", "optimization", "algorithm", "complexity", "trade-off",
    "tradeoff", "compare", "evaluate", "strategy", "step by step",
    "step-by-step", "multi-step", "explain why", "root cause",
    "why does", "why is", "how does", "how would", "implement",
    "write code", "stack trace", "traceback", "regex", "sql query",
    "reason about", "think through", "walk me through", "plan for",
    "pros and cons", "edge case", "edge cases",
)

# Clearly-trivial openers/acks. Matched as the whole message or as a
# leading token so "hi there" is cheap but "high availability" is not.
_TRIVIAL_PHRASES: Final[tuple[str, ...]] = (
    "hi", "hello", "hey", "yo", "sup", "thanks", "thank you", "ty",
    "ok", "okay", "k", "cool", "nice", "great", "got it", "gotcha",
    "good morning", "good night", "good evening", "yes", "no", "yep",
    "nope", "yeah", "sure", "lol", "haha", "morning",
)

_CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"```|\bdef \w+\(|\bclass \w+\b|=>|;\s*\n|\{\s*\n|</?\w+>|\bimport \w+",
)
_MATH_RE: Final[re.Pattern[str]] = re.compile(
    r"[∫∑√≈≤≥∂πθ]|\d+\s*[\^]\s*\d+|\b\d+\s*/\s*\d+\b|\bx\s*=\s*|\bsolve\b",
)
_ENUMERATED_RE: Final[re.Pattern[str]] = re.compile(r"\n\s*(?:\d+[.)]|[-*])\s+")


def escalate(tier: str) -> str:
    """Return the next-stronger tier, clamped at ``premium``."""
    try:
        idx = TIER_ORDER.index(tier)
    except ValueError:
        return BALANCED
    return TIER_ORDER[min(idx + 1, len(TIER_ORDER) - 1)]


def _looks_trivial(text_lower: str, length: int) -> bool:
    if length <= 24:
        return True
    if length > 140:
        return False
    for phrase in _TRIVIAL_PHRASES:
        if text_lower == phrase:
            return True
        if text_lower.startswith(phrase + " ") or text_lower.startswith(phrase + ","):
            # Only trivial if the whole thing stays short and question-free.
            if length <= 60 and "?" not in text_lower:
                return True
    return False


def classify_difficulty(
    text: str,
    *,
    has_tools: bool = False,
    has_vision: bool = False,
    context_chars: int = 0,
) -> str:
    """Estimate a turn's difficulty and return a tier.

    Args:
        text: The user's message text for this turn.
        has_tools: Whether tools are offered to the model this turn
            (an agentic turn — never routed ``cheap``).
        has_vision: Whether an image/frame is attached this turn.
        context_chars: Approximate size of the conversation context
            being sent, used as a "this is a heavy turn" signal.

    Returns:
        One of :data:`CHEAP` / :data:`BALANCED` / :data:`PREMIUM`.
    """
    raw = (text or "").strip()
    text_lower = raw.lower()
    length = len(raw)

    premium_hits = sum(1 for kw in _PREMIUM_KEYWORDS if kw in text_lower)
    has_code = bool(_CODE_RE.search(raw))
    has_math = bool(_MATH_RE.search(raw))
    enumerated = bool(_ENUMERATED_RE.search(raw))
    long_turn = length > 1500 or context_chars > 12000

    # Vision turns that also carry reasoning load want the frontier model.
    if has_vision and (has_tools or premium_hits or length > 400):
        return PREMIUM

    # Strong evidence of hard reasoning → premium.
    if has_code or has_math or long_turn or premium_hits >= 2:
        return PREMIUM
    if premium_hits == 1 and (has_tools or enumerated or length > 600):
        return PREMIUM

    # Any single reasoning marker, an enumerated multi-part ask, or an
    # agentic (tool) turn deserves the operator's full model.
    if premium_hits >= 1 or enumerated or has_tools:
        return BALANCED

    # Only clearly trivial, tool-free, question-free turns go cheap.
    if not has_vision and _looks_trivial(text_lower, length):
        return CHEAP

    return BALANCED


def apply_cost_downshift(
    tier: str,
    *,
    headroom_ratio: float,
    tight_ratio: float,
) -> str:
    """Drop one tier when daily-budget headroom is tight.

    Mirrors the failover layer's "prefer cheapest when headroom is low"
    rule, but at the *tier* level: under budget pressure a ``premium``
    turn becomes ``balanced`` and a ``balanced`` turn becomes ``cheap``.
    A no-op when budgeting is disabled (``headroom_ratio`` defaults to
    ``1.0`` upstream) or headroom is healthy.
    """
    if tight_ratio <= 0.0:
        return tier
    if headroom_ratio > tight_ratio:
        return tier
    try:
        idx = TIER_ORDER.index(tier)
    except ValueError:
        return tier
    return TIER_ORDER[max(idx - 1, 0)]
