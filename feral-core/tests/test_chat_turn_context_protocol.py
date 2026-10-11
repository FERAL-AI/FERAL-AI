"""Committed context is exact historical evidence, not an optional loose hint."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.protocol import ChatTurnTerminalPayload, FeralMessage, parse_message


def checkpoint():
    return {"contract_version": 1, "session_id": "context-thread",
            "generation": str(uuid4()), "revision": 2, "attempt_id": str(uuid4()),
            "durable": True}


def terminal(context=None, outcome="completed"):
    payload = {"request_id": str(uuid4()), "turn_id": str(uuid4()),
               "session_id": "context-thread", "processing_outcome": outcome,
               "final_text": "Synthetic verified response"}
    if context is not None:
        payload["context_checkpoint"] = context
    return payload


@pytest.mark.parametrize("outcome", ["completed", "awaiting_approval", "refused"])
def test_real_parser_retains_exact_historical_commit(outcome):
    context = checkpoint()
    frame = FeralMessage(type="chat_turn_terminal", payload=terminal(context, outcome))
    _, parsed = parse_message(frame.model_dump())
    assert parsed.context_checkpoint.model_dump() == context
    assert parsed.model_dump()["context_checkpoint"] == context


def test_legacy_terminal_shape_does_not_gain_null_checkpoint():
    parsed = ChatTurnTerminalPayload(**terminal())
    assert parsed.context_checkpoint is None
    assert "context_checkpoint" not in parsed.model_dump()


@pytest.mark.parametrize("field", list(checkpoint()))
def test_partial_commit_is_refused(field):
    value = checkpoint()
    del value[field]
    with pytest.raises(ValidationError):
        ChatTurnTerminalPayload(**terminal(value))


@pytest.mark.parametrize("field,invalid", [
    ("contract_version", True), ("contract_version", "1"), ("contract_version", 1.0),
    ("contract_version", 2), ("durable", 1), ("durable", "true"), ("durable", False),
    ("revision", True), ("revision", "2"), ("revision", 2.0),
    ("revision", 0), ("revision", 2**63 - 1),
    ("generation", "A" * 36), ("generation", str(uuid4()).upper()),
    ("attempt_id", "invalid"), ("attempt_id", str(uuid4()).replace("-", "")),
    ("session_id", "other-thread"), ("session_id", " context-thread"),
    ("session_id", "context-thread\u0085"), ("session_id", ""),
])
def test_noncanonical_or_foreign_commit_is_refused(field, invalid):
    value = checkpoint()
    value[field] = invalid
    with pytest.raises(ValidationError):
        ChatTurnTerminalPayload(**terminal(value))


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "outcome_unknown", "unavailable", "budget_exceeded"])
def test_unverified_outcome_cannot_carry_commit_certification(outcome):
    with pytest.raises(ValidationError):
        ChatTurnTerminalPayload(**terminal(checkpoint(), outcome))


def test_commit_rejects_unknown_fields():
    value = {**checkpoint(), "current_playback_authority": True}
    with pytest.raises(ValidationError):
        ChatTurnTerminalPayload(**terminal(value))
