"""Exercise negotiated voice frames through the canonical wire parser."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.protocol import parse_message


SID = "managed-voice-protocol"
ATTEMPT, REQUEST, TURN, GENERATION, COMMIT = (str(uuid4()) for _ in range(5))
IDENTITY = {"managed_chained_voice_version": 1,
            "voice_attempt_version": 1, "voice_attempt_id": ATTEMPT}
REVIEW = {"context_generation": GENERATION, "context_revision": 3}
CHECKPOINT = {"contract_version": 1, "session_id": SID,
              "generation": GENERATION, "revision": 3,
              "attempt_id": COMMIT, "durable": True}


def parse(kind, payload, session_id=SID):
    return parse_message({"type": kind, "session_id": session_id,
                          "payload": payload})[1]


@pytest.mark.parametrize("kind", ["voice_utterance_begin", "voice_utterance_finish"])
def test_utterance_review_survives_canonical_parse(kind):
    wire = {**IDENTITY, **REVIEW, "request_id": REQUEST}
    assert parse(kind, wire).model_dump() == wire


@pytest.mark.parametrize("kind,payload", [
    ("voice_config", {"mode": "chained", "provider": "configured", **REVIEW}),
    ("voice_config_ack", {"mode": "chained", "status": "configured",
                          "context_checkpoint": CHECKPOINT}),
    ("voice_utterance_begin_ack", {"status": "collecting", "request_id": REQUEST,
                                   "context_checkpoint": CHECKPOINT}),
    ("voice_utterance_finish_ack", {"status": "submitted_processing",
                                    "request_id": REQUEST, "task_accepted": False}),
    ("transcript", {"role": "assistant", "text": "Done", "request_id": REQUEST,
                     "turn_id": TURN, "context_checkpoint": CHECKPOINT}),
    ("audio_chunk", {"data_b64": "AAA=", "encoding": "pcm16", "request_id": REQUEST,
                      "turn_id": TURN, "context_checkpoint": CHECKPOINT}),
    ("voice_state", {"state": "processing", "request_id": REQUEST,
                      "context_checkpoint": CHECKPOINT}),
])
def test_negotiated_metadata_round_trip(kind, payload):
    wire = {**IDENTITY, **payload}
    result = parse(kind, wire).model_dump()
    assert all(result[key] == value for key, value in wire.items())


@pytest.mark.parametrize("field,value", [
    ("managed_chained_voice_version", True),
    ("managed_chained_voice_version", "1"),
    ("managed_chained_voice_version", None),
    ("voice_attempt_id", ATTEMPT.upper()),
    ("request_id", "not-a-request"),
    ("context_generation", GENERATION.upper()),
    ("context_revision", True), ("context_revision", "3"),
    ("context_revision", 0), ("context_revision", 2**63 - 1),
])
def test_malformed_review_is_refused_before_dispatch(field, value):
    with pytest.raises(ValidationError):
        parse("voice_utterance_begin", {**IDENTITY, **REVIEW,
                                        "request_id": REQUEST, field: value})


@pytest.mark.parametrize("remove", ["request_id", "context_generation",
                                   "context_revision", "voice_attempt_id"])
def test_missing_utterance_identity_is_refused(remove):
    wire = {**IDENTITY, **REVIEW, "request_id": REQUEST}
    wire.pop(remove)
    with pytest.raises(ValidationError):
        parse("voice_utterance_begin", wire)


@pytest.mark.parametrize("field,value", [
    ("durable", False), ("durable", 1), ("revision", True),
    ("attempt_id", "missing"), ("contract_version", True),
    ("unknown", "not-a-checkpoint-field"),
])
def test_uncommitted_or_malformed_checkpoint_is_refused(field, value):
    with pytest.raises(ValidationError):
        parse("voice_utterance_begin_ack", {
            **IDENTITY, "request_id": REQUEST, "status": "collecting",
            "context_checkpoint": {**CHECKPOINT, field: value}})


def test_other_session_checkpoint_is_refused():
    with pytest.raises(ValueError, match="another session"):
        parse("voice_utterance_begin_ack", {
            **IDENTITY, "request_id": REQUEST, "status": "collecting",
            "context_checkpoint": CHECKPOINT}, session_id="different-session")


@pytest.mark.parametrize("value", [True, 0, "false", None])
def test_finish_ack_cannot_claim_task_acceptance(value):
    with pytest.raises(ValidationError):
        parse("voice_utterance_finish_ack", {**IDENTITY, "request_id": REQUEST,
            "status": "submitted_processing", "task_accepted": value})


def test_configured_ack_requires_managed_checkpoint():
    with pytest.raises(ValidationError):
        parse("voice_config_ack", {"mode": "chained", "status": "configured"})


def test_client_cannot_present_committed_task_authority_as_utterance():
    with pytest.raises(ValidationError):
        parse("voice_utterance_finish", {**IDENTITY, **REVIEW,
            "request_id": REQUEST, "turn_id": TURN, "context_checkpoint": CHECKPOINT})


def test_legacy_frames_keep_previous_shape_and_ignore_managed_metadata():
    legacy = parse("audio_chunk", {"data_b64": "AAA="}).model_dump()
    noisy = parse("audio_chunk", {"data_b64": "AAA=", "request_id": "ignored",
        "turn_id": "ignored", "context_checkpoint": "ignored"}).model_dump()
    assert noisy == legacy
    assert not (set(IDENTITY) | set(REVIEW) | {"request_id", "turn_id",
                                             "context_checkpoint"}) & legacy.keys()
    assert parse("voice_config_ack", {"status": "ok"}).model_dump() == {
        "mode": "", "provider": "", "status": "ok", "code": "", "message": ""}


def test_task_stop_receipt_survives_canonical_parse():
    receipt = {"cancel_requested": False, "status": "acceptance_unconfirmed"}
    result = parse("voice_config_ack", {**IDENTITY, "status": "ok",
                                       "task_cancellation": receipt}).model_dump()
    assert result["task_cancellation"] == receipt


@pytest.mark.parametrize("kind", ["voice_utterance_begin_ack", "voice_utterance_finish_ack"])
def test_negative_ack_preserves_request_without_task_authority(kind):
    wire = {**IDENTITY, "request_id": REQUEST, "status": "error",
            "code": "managed_voice_context_superseded", "retry_safe": False}
    assert parse(kind, wire).model_dump() == wire


@pytest.mark.parametrize("value", [True, 0, None])
def test_negative_ack_does_not_grant_automatic_replay(value):
    with pytest.raises(ValidationError):
        parse("voice_utterance_begin_ack", {**IDENTITY, "request_id": REQUEST,
            "status": "error", "code": "unconfirmed", "retry_safe": value})
