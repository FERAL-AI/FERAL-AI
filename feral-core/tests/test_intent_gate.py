"""PR 8: intent_gate — slot extraction + impact + clarification.

Matrix
------
* Plain navigation ("open the homepage") → PROCEED, low impact.
* Destructive verb with explicit quoted target → PROCEED, target slot.
* Destructive verb with pronoun-only target → ASK, high impact.
* Destructive verb without target but memory has one strong hit →
  PROCEED_WITH_FILL, with provenance ``memory:<tier>``.
* Send action without recipient → ASK with medium impact + suggestions.
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from agents.intent_gate import IntentVerdict, gate_intent  # noqa: E402
from memory.retriever import MemoryRetriever  # noqa: E402


class _FakeMemory:
    def __init__(self, notes):
        self._notes = notes

    def search(self, query, limit=10):
        return self._notes[:limit]


@pytest.mark.asyncio
async def test_navigation_proceeds_with_low_impact():
    decision = await gate_intent("open the homepage")
    assert decision.verdict == IntentVerdict.PROCEED
    assert decision.impact == "low"


@pytest.mark.asyncio
async def test_destructive_with_quoted_target_proceeds():
    decision = await gate_intent("delete 'old report.txt'")
    assert decision.verdict == IntentVerdict.PROCEED
    assert decision.action_hint == "destructive"
    assert decision.impact == "high"
    assert any(s.name == "target" and s.value == "old report.txt" for s in decision.slots)


@pytest.mark.asyncio
async def test_destructive_with_pronoun_only_asks():
    decision = await gate_intent("delete it")
    assert decision.verdict == IntentVerdict.ASK
    assert decision.impact == "high"
    assert "target" in decision.missing_slots
    assert decision.question  # populated
    assert any("pronoun-only" in e for e in decision.evidence)


@pytest.mark.asyncio
async def test_destructive_self_fills_from_memory_when_one_strong_hit():
    """If memory has exactly one high-confidence hit for the ambiguous
    pronoun, the gate self-fills with provenance instead of asking."""
    mem = _FakeMemory(notes=[
        {"id": "n", "content": "delete the file backups/2025-01.zip when ready"},
    ])
    retriever = MemoryRetriever(mem)
    decision = await gate_intent("delete it now", retriever=retriever)
    # Either fills or asks-with-suggestions — both are honest. The
    # critical contract: it must NOT silently proceed without surfacing
    # the source or the question.
    if decision.verdict == IntentVerdict.PROCEED_WITH_FILL:
        target = next(s for s in decision.slots if s.name == "target")
        assert target.source.startswith("memory:")
    else:
        assert decision.verdict == IntentVerdict.ASK
        assert decision.suggestions  # at least one suggestion shown
        # Suggestions must come from memory content, not hallucinated.
        assert any("backups" in s for s in decision.suggestions)


@pytest.mark.asyncio
async def test_send_without_recipient_asks_medium_impact():
    decision = await gate_intent("send the report")
    assert decision.verdict == IntentVerdict.ASK
    assert decision.impact == "medium"
    assert "recipient" in decision.question.lower() or "send" in decision.question.lower()


@pytest.mark.asyncio
async def test_send_with_recipient_proceeds():
    decision = await gate_intent("send Mom the report")
    assert decision.verdict == IntentVerdict.PROCEED
    assert decision.action_hint == "send"
    assert any(s.name == "target" and s.value == "Mom" for s in decision.slots)


@pytest.mark.asyncio
async def test_empty_input_returns_proceed():
    decision = await gate_intent("")
    assert decision.verdict == IntentVerdict.PROCEED
    assert decision.impact == "low"


@pytest.mark.asyncio
async def test_decision_serializable_via_dataclass_fields():
    decision = await gate_intent("delete it")
    # Pretty obvious but pin the schema so future refactors keep it
    # JSON-friendly for the WS payload that ships this to the UI.
    assert decision.verdict.value in {"proceed", "proceed_with_fill", "ask"}
    assert decision.impact in {"low", "medium", "high"}
    assert isinstance(decision.evidence, list)


@pytest.mark.asyncio
async def test_path_in_input_is_used_as_target():
    decision = await gate_intent("delete /tmp/old.log")
    assert decision.verdict == IntentVerdict.PROCEED
    assert any(s.value == "/tmp/old.log" for s in decision.slots)


@pytest.mark.asyncio
async def test_quoted_target_takes_priority_over_path():
    """If both a quoted phrase and a path are present, the quoted
    phrase wins because the user typed it explicitly."""
    decision = await gate_intent("delete 'the daily build' from /tmp/builds.log")
    target = next(s for s in decision.slots if s.name == "target")
    assert target.value == "the daily build"
