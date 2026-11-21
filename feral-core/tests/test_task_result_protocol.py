"""Canonical durable task delivery is exact, bounded and recovery-only."""
import pytest
from pydantic import ValidationError
from models.protocol import AudioResponsePayload, NodeAckPayload, TASK_RESULT_CONTRACT, TaskAcceptedPayload, TaskReceiptsGetPayload, TaskReceiptsPayload, TaskResultContractPayload, TaskResultGetPayload, TaskResultPayload, VoiceResponsePayload, parse_message

REQUEST = "12e7619d-e93f-4f97-a350-78c4cdb2699a"
DELIVERY = "e8bc05d1-a865-4b27-a6d6-1da9e3469cd7"
IDENTITY = {"contract_version": 1, "request_id": REQUEST, "session_id": "conversation-a", "task_id": "flow-a"}
ACCEPTED = {**IDENTITY, "durable": True, "replayed": False, "action_outcome": "not_asserted"}
RESULT = {**ACCEPTED, "delivery_id": DELIVERY, "processing_outcome": "completed", "report": "Local draft saved.", "report_truncated": False}


@pytest.mark.parametrize("kind,cls,payload", [
    ("task_result_get", TaskResultGetPayload, IDENTITY),
    ("task_accepted", TaskAcceptedPayload, ACCEPTED),
    ("task_result", TaskResultPayload, RESULT),
])
def test_registered_exact_task_contract(kind, cls, payload):
    _, parsed = parse_message({"type": kind, "payload": payload})
    assert isinstance(parsed, cls)
    assert parsed.model_dump(exclude_none=True) == payload


@pytest.mark.parametrize("key,value", [
    ("contract_version", True), ("contract_version", "1"), ("contract_version", 2),
    ("request_id", REQUEST.upper()), ("request_id", 12), ("task_id", "flow\nsecret"),
    ("session_id", " conversation-a"), ("durable", 1), ("durable", False),
    ("replayed", "false"), ("report_truncated", 0), ("report", 1),
    ("report", "x" * 8193), ("delivery_id", DELIVERY.upper()),
    ("processing_outcome", "accepted"), ("action_outcome", "verified"),
    ("recovery_request_id", "bad"), ("extra", "unexpected"),
])
def test_task_terminal_refuses_coercion_unbounded_or_ambiguous_contract(key, value):
    with pytest.raises(ValidationError):
        parse_message({"type": "task_result", "payload": {**RESULT, key: value}})


def test_unknown_outcome_stays_unknown():
    with pytest.raises(ValidationError):
        TaskResultPayload(**{**RESULT, "processing_outcome": "outcome_unknown"})
    payload = TaskResultPayload(**{**RESULT, "processing_outcome": "outcome_unknown", "action_outcome": "unknown"})
    assert payload.action_outcome == "unknown"


def test_recovery_has_no_goal_or_resume_authority():
    for extra in ("goal", "resume", "approved", "owner_verified"):
        with pytest.raises(ValidationError):
            TaskResultGetPayload(**{**IDENTITY, extra: True})


def test_admission_cannot_claim_completion():
    with pytest.raises(ValidationError):
        TaskAcceptedPayload(**{**ACCEPTED, "processing_outcome": "completed"})


def test_audio_identity_is_optional_for_legacy_and_exact_when_present():
    legacy = AudioResponsePayload().model_dump()
    assert "stream_id" not in legacy and "response_id" not in legacy
    identified = AudioResponsePayload(stream_id="stream-a", response_id="resp_a").model_dump()
    assert identified["stream_id"] == "stream-a" and identified["response_id"] == "resp_a"
    for field in ("stream_id", "response_id"):
        for value in (1, "", " a", "x\nprivate"):
            with pytest.raises(ValidationError):
                AudioResponsePayload(**{field: value})


def test_lost_admission_discovery_is_identity_only_and_bounded():
    query = TaskReceiptsGetPayload(contract_version=1, session_id="conversation-a")
    assert query.limit == 16
    payload = {"contract_version": 1, "session_id": "conversation-a", "recovery_request_id": DELIVERY,
               "receipts": [{**ACCEPTED, "replayed": True}], "next_cursor": "flow-a"}
    _, parsed = parse_message({"type": "task_receipts", "payload": payload})
    assert isinstance(parsed, TaskReceiptsPayload)
    assert parsed.receipts[0].request_id == REQUEST
    for changed in (
        {"receipts": [ACCEPTED]}, {"session_id": "other"},
        {"receipts": payload["receipts"] * 2}, {"next_cursor": "other-flow"},
        {"receipts": []}, {"recovery_request_id": DELIVERY.upper()},
        {"goal": "do work"},
    ):
        with pytest.raises(ValidationError):
            TaskReceiptsPayload(**{**payload, **changed})


@pytest.mark.parametrize("change", [{"limit": 17}, {"limit": 0}, {"limit": True}, {"limit": "16"},
                                    {"contract_version": True}, {"older_than_task_id": " flow"},
                                    {"session_id": "other\n"}, {"resume": True}, {"goal": "do work"}])
def test_discovery_cannot_coerce_expand_or_resume(change):
    with pytest.raises(ValidationError):
        TaskReceiptsGetPayload(**{"contract_version": 1, "session_id": "conversation-a", **change})


def test_response_start_is_actual_exact_identity_before_audio():
    payload = {"contract_version": 1, "stream_id": "stream-a", "response_id": "resp_a", "state": "created"}
    _, parsed = parse_message({"type": "voice_response", "payload": payload})
    assert isinstance(parsed, VoiceResponsePayload)
    assert parsed.model_dump(exclude_none=True) == payload
    for change in ({"contract_version": True}, {"response_id": ""}, {"stream_id": 1},
                   {"state": "speaking"}, {"response_id": " resp_a"}, {"response_id": "resp\nprivate"}):
        with pytest.raises(ValidationError):
            VoiceResponsePayload(**{**payload, **change})


def test_task_recovery_requires_explicit_exact_capability():
    assert NodeAckPayload().task_result_contract is None
    ack = NodeAckPayload(task_result_contract=TASK_RESULT_CONTRACT)
    assert ack.task_result_contract.model_dump() == TASK_RESULT_CONTRACT
    for key in TASK_RESULT_CONTRACT:
        for value in (0, "true", False):
            with pytest.raises(ValidationError):
                TaskResultContractPayload(**{**TASK_RESULT_CONTRACT, key: value})
    with pytest.raises(ValidationError):
        TaskResultContractPayload(**{**TASK_RESULT_CONTRACT, "contract_version": True})
