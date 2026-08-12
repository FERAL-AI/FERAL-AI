"""Strict postMessage schema for AppSurface iframe → host messages.

The AppSurface iframe sandbox (see roadmap §3.3 #2) lets the GenUI
surface emit ``postMessage`` events to its parent (FERAL host page).
Without a tight schema, a compromised app could spam the host with
arbitrary objects and hope something slips through into FERAL's own
handlers.

We pin three things here:

* The message envelope (:class:`AppMessage`) — strict pydantic v2
  model, ``extra="forbid"`` so unexpected fields are rejected before
  the message ever reaches FERAL's reducer.
* The enum of allowed ``type`` values (:class:`AppMessageType`).
  Anything outside this enum is dropped.
* The maximum payload size (:data:`MAX_PAYLOAD_BYTES`). 64 KiB matches
  the upper bound the brain's ui_event hot path is willing to accept;
  anything larger is denied here so the iframe can't DoS the host
  channel. It is measured by :func:`payload_size_bytes`, and *what* it
  measures is load-bearing, see that function.

The TypeScript mirror at ``feral-client-v2/src/pages/AppSurface.types.ts``
must stay in lockstep — there's a comment in both files reminding
maintainers to update both halves together. The Python side is the
authoritative schema for backend parsers (registry / brain replay);
the TS side is what the host actually runs to drop malformed events
before dispatch.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


__all__ = [
    "AppMessage",
    "AppMessageType",
    "AppMessageError",
    "MAX_PAYLOAD_BYTES",
    "payload_size_bytes",
    "validate_app_message",
]


MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KiB hard cap — see module docstring.


def payload_size_bytes(payload: dict[str, Any]) -> int:
    """Size of ``payload`` in UTF-8 bytes of its compact JSON encoding.

    This is the *one* quantity ``MAX_PAYLOAD_BYTES`` is measured in, and it is
    mirrored by ``measurePayloadBytes`` in
    ``feral-client-v2/src/pages/AppSurface.types.ts``. Every keyword below is
    load-bearing, because the stdlib defaults made the two sides disagree by 6x:

    * ``ensure_ascii=False``. The default ``True`` emits ``\\uXXXX`` escapes,
      six bytes for a BMP character and twelve for an astral-plane one.
      ``{"a": "中" * 11000}`` measured 66009 and was refused by the brain while
      the browser measured 11008 UTF-16 units and let it through.
    * ``separators=(",", ":")``. The default is ``(", ", ": ")``, two extra
      bytes per key that ``JSON.stringify`` never emits. On its own that was
      enough to refuse a pure-ASCII payload of exactly 65536 bytes, which
      measured 65537.
    * ``errors="backslashreplace"``. ``json.loads('{"a": "\\ud800"}')`` yields
      a lone surrogate, and with ``ensure_ascii=False`` encoding one raises
      UnicodeEncodeError. That is a ValidationError-shaped ValueError, so the
      validator would have reported an acceptable payload as unserialisable.
      JavaScript's well-formed ``JSON.stringify`` re-escapes lone surrogates to
      the same six ASCII characters ``backslashreplace`` produces, so the two
      sides land on the same count instead of one of them erroring.

    Raises ``ValueError`` if the payload cannot be JSON-encoded at all.
    """
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", "backslashreplace")
    )


class AppMessageType(str, Enum):
    """Allowed AppMessage types. Add cases here, never inline literals."""

    REQUEST_DATA = "request_data"
    SUBMIT_FORM = "submit_form"
    NAVIGATE = "navigate"
    CLOSE = "close"


class AppMessageError(ValueError):
    """Raised by :func:`validate_app_message` for any malformed input."""


class AppMessage(BaseModel):
    """The strict envelope for app→host postMessage events.

    Fields:
      * ``type`` — one of :class:`AppMessageType`. ``Literal`` would be
        sufficient too, but the enum lets the TS mirror import the
        same names symbolically.
      * ``payload`` — opaque dict. We don't attempt schema-level
        validation of payload contents here — that's the host's job
        per message type. We do enforce the *size* of the payload.
      * ``message_id`` — caller-supplied correlation id. Required so
        the host can ack/reject specific messages without ambiguity.
      * ``signed_with_key_id`` — the publisher key id whose Ed25519
        signature gated the install. We carry it on every message so
        the host can verify the iframe wasn't swapped at runtime.

    Pydantic v2 ``extra="forbid"`` means an attacker can't smuggle in
    side-channel fields hoping the host might forward them.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    type: AppMessageType = Field(
        ...,
        description="Allowed message kind; see AppMessageType.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-type opaque payload. Size-capped by the validator.",
    )
    message_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Caller-correlation id; the host echoes this in acks.",
    )
    signed_with_key_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Key id from the SignedManifest that gated install.",
    )

    @field_validator("payload")
    @classmethod
    def _payload_within_bounds(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("payload must be a dict")
        try:
            size = payload_size_bytes(v)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError(f"payload must be JSON-serialisable: {exc}") from exc
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"payload too large: {size} bytes "
                f"(max {MAX_PAYLOAD_BYTES})"
            )
        return v


def validate_app_message(raw: Any) -> Optional[AppMessage]:
    """Best-effort validation that NEVER raises.

    Mirrors the host-side TS guard: returns ``None`` for any malformed
    input, so the host can drop the event silently without crashing
    its message loop. Callers that need the failure reason can call
    :class:`AppMessage` directly and catch ``ValidationError``.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return AppMessage.model_validate(raw)
    except ValidationError:
        return None
    except Exception:  # pragma: no cover — pydantic only raises ValidationError
        return None
