"""Pure caller-session syntax shared by policy consumers and durable context.

Validation does not authenticate a caller or grant execution authority. It
preserves the exact identity; callers must separately bind and authorize it.
"""

from __future__ import annotations

import unicodedata


class SessionIdentityValidationError(ValueError):
    """Redacted invalid session identity, without caller-supplied content."""


def validate_session_id(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 1024 or value.strip() != value or any(
        unicodedata.category(char) == "Cc" for char in value
    ):
        raise SessionIdentityValidationError("Invalid session identity")
