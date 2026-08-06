"""Binding a relay id to the key that is entitled to it.

There are no accounts here, and that is the design rather than a gap. A
brain proves who it is by signing with a key only it holds, and the id
it claims is *derived* from that key, so the control plane never has to
decide whether a claim is legitimate: it recomputes the id and either
the arithmetic agrees or the request is a forgery.

What the control plane must get right is narrow and worth stating:

1. **The signature must verify against the key in the request.** Not
   against a stored key, because on a first registration there is none.
2. **The id must derive from that same key.** Without this check, a
   valid signature over a body claiming someone else's id would be
   accepted, and an attacker could register `victim-id` with their own
   key. This is the check that makes the whole scheme work.
3. **First registration binds permanently.** A later request for the
   same id signed by a different key is refused. Rotating a key means
   becoming a different brain, which is the honest outcome: the id is
   the key.
4. **A replayed request is refused.** The signature is over a body that
   includes a timestamp and a nonce, so capturing one and resending it
   later must not re-register or re-bind anything.

None of this requires an email address, a password, or a session.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from .identity import derive_relay_id, verify_signature

#: How far out of step with us a brain's clock may be. Wide enough for a
#: laptop that has been asleep and a little NTP drift, narrow enough
#: that a captured request stops being useful quickly.
MAX_CLOCK_SKEW_SECONDS = 300


class RegistrationError(Exception):
    """A registration that must not be honoured, and why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class BrainRecord:
    relay_id: str
    public_key_b64: str
    created_at: float
    last_seen_at: float


class BrainStore(Protocol):
    """What registration needs from storage, and nothing more.

    Kept this narrow so the logic is testable without a database and so
    swapping SQLite for anything else is not a rewrite.
    """

    def get(self, relay_id: str) -> Optional[BrainRecord]: ...

    def put(self, record: BrainRecord) -> None: ...

    def seen_nonce(self, relay_id: str, nonce: str) -> bool: ...

    def remember_nonce(self, relay_id: str, nonce: str, expires_at: float) -> None: ...


def canonical_payload(body: dict) -> bytes:
    """The exact bytes a brain signs and the control plane verifies.

    Sorted keys and no whitespace, so two implementations cannot
    disagree about the encoding and produce a signature that verifies on
    one side and not the other. Only the fields that are signed appear
    here: adding a field to the request without adding it here would
    leave it unauthenticated, which is how signed protocols quietly stop
    protecting anything.
    """
    return json.dumps(
        {
            "relay_id": body["relay_id"],
            "public_key": body["public_key"],
            "ts": body["ts"],
            "nonce": body["nonce"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def register(body: dict, store: BrainStore, *, now: Optional[float] = None) -> BrainRecord:
    """Register or re-register a brain. Raises on anything suspect."""
    now = time.time() if now is None else now

    for field in ("relay_id", "public_key", "ts", "nonce", "signature"):
        if not body.get(field):
            raise RegistrationError("missing_field", f"{field} is required")

    relay_id = str(body["relay_id"]).strip().lower()
    public_key_b64 = str(body["public_key"]).strip()
    nonce = str(body["nonce"]).strip()

    try:
        ts = float(body["ts"])
    except (TypeError, ValueError):
        raise RegistrationError("bad_timestamp", "ts must be a number")

    # Replay window first: it is the cheapest check and it bounds how
    # long a captured request stays interesting.
    if abs(now - ts) > MAX_CLOCK_SKEW_SECONDS:
        raise RegistrationError(
            "stale_request",
            f"ts is more than {MAX_CLOCK_SKEW_SECONDS}s from our clock",
        )

    if store.seen_nonce(relay_id, nonce):
        raise RegistrationError("replayed_nonce", "this request has already been used")

    # The signature is over the body, so verify before trusting any of
    # it, including the public key it carries.
    if not verify_signature(canonical_payload(body), body["signature"], public_key_b64):
        raise RegistrationError("bad_signature", "signature does not verify")

    # The check that makes the scheme work. A valid signature only
    # proves the sender holds *a* key; this proves the id they are
    # claiming is the one that key is entitled to.
    try:
        expected = derive_relay_id(_decode_key(public_key_b64))
    except Exception:
        raise RegistrationError("bad_public_key", "public_key is not a valid Ed25519 key")

    if relay_id != expected:
        raise RegistrationError(
            "relay_id_mismatch",
            "relay_id is not derived from this public key",
        )

    existing = store.get(relay_id)
    if existing is not None and existing.public_key_b64 != public_key_b64:
        # Unreachable while derivation holds, since a different key
        # derives a different id. Kept because it is the invariant we
        # actually care about, and a future change to the derivation
        # must not silently turn rebinding into a supported operation.
        raise RegistrationError(
            "already_bound",
            "this relay_id is bound to a different key",
        )

    record = BrainRecord(
        relay_id=relay_id,
        public_key_b64=public_key_b64,
        created_at=existing.created_at if existing else now,
        last_seen_at=now,
    )
    store.put(record)
    store.remember_nonce(relay_id, nonce, now + MAX_CLOCK_SKEW_SECONDS * 2)
    return record


def _decode_key(public_key_b64: str) -> bytes:
    import base64

    raw = base64.b64decode(public_key_b64, validate=True)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return raw
