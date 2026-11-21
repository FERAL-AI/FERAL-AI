"""Shared syntax preserves the durable identity contract across skill boundaries."""

import unicodedata

import pytest

from memory.runtime_session_checkpoint import (
    CheckpointValidationError,
    validate_session_id as validate_checkpoint_session,
)
from security.session_identity import SessionIdentityValidationError, validate_session_id


def legacy_checkpoint_validator(value):
    """Pre-extraction contract oracle; intentionally independent of the helper."""
    if not isinstance(value, str) or not value or len(value) > 1024 or value.strip() != value or any(
        unicodedata.category(char) == "Cc" for char in value
    ):
        raise CheckpointValidationError("Invalid checkpoint session identity")


@pytest.mark.parametrize("value", [
    "owner", "a b", "界" * 1024, "🦊" * 1024, "a\u200db", "\u200bowner",
    "", " leading", "trailing ", "\u00a0owner", "owner\u00a0", "x" * 1025,
    None, True, False, 1, {}, [], b"owner",
] + ["a" + chr(code) + "b" for code in range(160)
     if unicodedata.category(chr(code)) == "Cc"])
def test_shared_and_checkpoint_validation_match_legacy_exactly(value):
    try:
        legacy_checkpoint_validator(value)
    except CheckpointValidationError:
        with pytest.raises(SessionIdentityValidationError, match="^Invalid session identity$"):
            validate_session_id(value)
        with pytest.raises(CheckpointValidationError, match="^Invalid checkpoint session identity$") as caught:
            validate_checkpoint_session(value)
        assert caught.value.__cause__ is None
    else:
        assert validate_session_id(value) is None
        assert validate_checkpoint_session(value) is None


def test_syntax_validation_never_coerces_identity_or_contains_private_input():
    class StringLike:
        def __str__(self):
            raise AssertionError("Identity must not be coerced")

    with pytest.raises(SessionIdentityValidationError):
        validate_session_id(StringLike())
    private = "PRIVATE_SYNTHETIC_OWNER\n"
    for validator, error_type in [
        (validate_session_id, SessionIdentityValidationError),
        (validate_checkpoint_session, CheckpointValidationError),
    ]:
        with pytest.raises(error_type) as failure:
            validator(private)
        assert private not in str(failure.value)
