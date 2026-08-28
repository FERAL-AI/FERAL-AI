"""PR 8: intent_gate — slot extraction + memory self-fill + impact
scoring + targeted clarification.

What it does
------------
Before the orchestrator hands a user message to the LLM and the safety
classifier, the gate makes one cheap, deterministic pass to figure out:

1. **Which slots does the implied action need?** (target, count,
   recipient, file path, timeframe, …)
2. **Can the runtime self-fill any of them from memory?** (e.g. the
   user said "send Mom the photo" — pull "Mom" from AboutMe contacts.)
3. **What's the impact score of doing this without confirmation?**
   (e.g. "delete it" with no target = high impact, must clarify.)
4. **What clarification question should we surface?** — exactly one,
   pointed at the highest-impact missing slot, with up to three
   memory-grounded suggestions.

The gate is intentionally rule-based and explainable. It is *not* an
LLM. It runs in the request path on every text command, so we can't
afford a model call here; an LLM clarification can still happen
downstream after the gate decides "go" vs "ask".

Truthfulness contract
---------------------
* If a slot value is taken from memory, the resulting
  :class:`IntentGateDecision` records the source so the UI can show
  "I used 'Mom = +1-555-…' from your contacts" *before* the message
  is sent.
* The gate never silently swallows a missing high-impact slot. Either
  the slot is filled (with provenance) or the verdict is ``"ask"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class IntentVerdict(str, Enum):
    PROCEED = "proceed"          # nothing missing or impact is low
    PROCEED_WITH_FILL = "proceed_with_fill"  # missing slots filled from memory
    ASK = "ask"                  # high-impact slot missing; clarify


@dataclass
class SlotFill:
    """One slot value the gate either extracted or self-filled."""

    name: str
    value: str
    source: str  # "user", "memory:<tier>", "default"
    confidence: float = 1.0


@dataclass
class IntentGateDecision:
    """Output of :func:`gate_intent`. Always carries an explanation."""

    verdict: IntentVerdict
    action_hint: str               # rough action verb the gate detected
    impact: str                    # low / medium / high
    slots: list[SlotFill] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)
    question: str = ""             # populated when verdict == ASK
    suggestions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


# ── Action vocabulary ────────────────────────────────────────────────


_DESTRUCTIVE_VERBS = {
    "delete", "remove", "rm", "wipe", "erase",
    "uninstall", "drop", "purge", "kill",
}
_SEND_VERBS = {"send", "email", "text", "message", "post", "share", "dm"}
_WRITE_VERBS = {"write", "save", "create", "open"}
_NAV_VERBS = {"open", "go to", "navigate", "browse"}


def _detect_action(text: str) -> tuple[str, str]:
    """Return ``(action_hint, impact)``.

    Impact ladder:
    * destructive verb     -> high
    * send/dm/email/text   -> medium
    * write/save/create    -> low (the canonical write-grant flow
      enforces its own approval, so we don't double-gate here)
    * navigation/read      -> low
    * fallback             -> low
    """
    if not text:
        return ("", "low")
    lowered = text.lower()
    words = re.findall(r"[a-z']+", lowered)
    head = set(words[:6]) if words else set()

    if head & _DESTRUCTIVE_VERBS or any(v in lowered for v in _DESTRUCTIVE_VERBS):
        # Soft-anchor: still need at least one destructive verb.
        if any(v in head for v in _DESTRUCTIVE_VERBS) or any(
            f" {v} " in f" {lowered} " for v in _DESTRUCTIVE_VERBS
        ):
            return ("destructive", "high")

    if head & _SEND_VERBS:
        return ("send", "medium")
    if head & _WRITE_VERBS:
        return ("write", "low")
    if head & _NAV_VERBS:
        return ("navigate", "low")
    return ("", "low")


# ── Slot extraction ──────────────────────────────────────────────────


_TARGET_PRONOUNS = {"it", "that", "this", "them", "those", "these"}
_PROPER_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")
_QUOTED_RE = re.compile(r"['\"]([^'\"]{1,80})['\"]")
_PATH_RE = re.compile(r"(?:~|/)[\w./-]+")
_DATETIME_HINT_RE = re.compile(
    r"\b(today|tomorrow|tonight|yesterday|now|in \d+\s+\w+|at \d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.IGNORECASE,
)


def _extract_target(text: str, action_hint: str) -> tuple[Optional[str], list[str]]:
    """Return ``(value_or_None, evidence)``.

    Order of preference:
    1. A quoted string (``"the budget plan"``).
    2. An explicit filesystem path.
    3. A proper-noun recipient for send/* actions (``Mom``, ``John``).
    4. None — and surface the pronoun if one is present so the
       clarification question can be specific.
    """
    evidence: list[str] = []

    q = _QUOTED_RE.search(text)
    if q:
        evidence.append(f"quoted-target='{q.group(1)}'")
        return (q.group(1), evidence)

    p = _PATH_RE.search(text)
    if p:
        evidence.append(f"path-target='{p.group(0)}'")
        return (p.group(0), evidence)

    if action_hint in {"send"}:
        for m in _PROPER_NAME_RE.finditer(text):
            evidence.append(f"name-target='{m.group(1)}'")
            return (m.group(1), evidence)

    lowered = text.lower()
    for pronoun in _TARGET_PRONOUNS:
        if re.search(rf"\b{re.escape(pronoun)}\b", lowered):
            evidence.append(f"pronoun-only='{pronoun}'")
            return (None, evidence)
    return (None, evidence)


# ── Memory self-fill ─────────────────────────────────────────────────


async def _self_fill_from_memory(
    *,
    text: str,
    action_hint: str,
    missing: str,
    retriever,
) -> Optional[SlotFill]:
    """Ask the MemoryRetriever for the missing slot.

    Returns None if memory has no hit; otherwise returns a
    :class:`SlotFill` with ``source="memory:<tier>"`` so the UI can
    show provenance."""
    if retriever is None:
        return None
    try:
        result = await retriever.retrieve(text, top_k=3)
    except Exception:
        return None
    if not result.records:
        return None
    top = result.records[0]
    # Don't self-fill below a sanity floor — anything weaker than a
    # token overlap of 0.15 is noise.
    if top.base_score < 0.15:
        return None
    return SlotFill(
        name=missing,
        value=top.content[:120],
        source=f"memory:{top.tier}",
        confidence=min(1.0, top.score),
    )


# ── Public entry point ───────────────────────────────────────────────


async def gate_intent(text: str, *, retriever=None) -> IntentGateDecision:
    """Single-shot deterministic gate.

    Args:
        text: raw user message.
        retriever: optional :class:`memory.retriever.MemoryRetriever`.
            When provided, the gate may self-fill missing high-impact
            slots from memory.

    Returns:
        :class:`IntentGateDecision` — always populated, never raises.
    """
    if not text or not text.strip():
        return IntentGateDecision(
            verdict=IntentVerdict.PROCEED,
            action_hint="",
            impact="low",
            evidence=["empty input"],
        )

    action_hint, impact = _detect_action(text)
    target, target_evidence = _extract_target(text, action_hint)

    slots: list[SlotFill] = []
    missing_slots: list[str] = []
    evidence = list(target_evidence)

    if target:
        slots.append(SlotFill(name="target", value=target, source="user"))

    needs_target = action_hint in {"destructive", "send"}
    if needs_target and target is None:
        # Try to self-fill from memory before clarifying. If memory has
        # a single confident hit we proceed-with-fill; otherwise we ask.
        fill = await _self_fill_from_memory(
            text=text, action_hint=action_hint, missing="target", retriever=retriever,
        )
        if fill is not None:
            slots.append(fill)
            evidence.append(f"self_filled target from {fill.source}")
        else:
            missing_slots.append("target")

    suggestions: list[str] = []
    question = ""

    if missing_slots and impact == "high":
        verdict = IntentVerdict.ASK
        if retriever is not None:
            try:
                hits = (await retriever.retrieve(text, top_k=3)).top(3)
                suggestions = [h.content[:120] for h in hits if h.content]
            except Exception:
                suggestions = []
        question = (
            "Which item do you mean? I need an explicit target before I "
            "perform this destructive action."
        )
    elif missing_slots and impact == "medium":
        # Medium-impact ambiguity also asks, but the question is softer
        # and we offer up to three memory-grounded suggestions.
        verdict = IntentVerdict.ASK
        if retriever is not None:
            try:
                hits = (await retriever.retrieve(text, top_k=3)).top(3)
                suggestions = [h.content[:120] for h in hits if h.content]
            except Exception:
                suggestions = []
        question = "Who should I send this to?" if action_hint == "send" else (
            "Could you tell me which one you mean?"
        )
    elif slots and any(s.source.startswith("memory:") for s in slots):
        verdict = IntentVerdict.PROCEED_WITH_FILL
    else:
        verdict = IntentVerdict.PROCEED

    return IntentGateDecision(
        verdict=verdict,
        action_hint=action_hint,
        impact=impact,
        slots=slots,
        missing_slots=missing_slots,
        question=question,
        suggestions=suggestions,
        evidence=evidence,
    )


__all__ = [
    "IntentGateDecision",
    "IntentVerdict",
    "SlotFill",
    "gate_intent",
]
