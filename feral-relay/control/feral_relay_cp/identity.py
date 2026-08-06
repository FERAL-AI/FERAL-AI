"""Relay identity, from the control plane's side of the wire.

Deliberately imports nothing from ``feral-core``. The control plane is a
separate deployable that runs on infrastructure we operate, and a brain
runs on a user's laptop; coupling them at the source level would mean a
brain could not be older or newer than the relay it talks to.

The cost of that independence is a duplicated derivation, and a
duplicated derivation that silently drifts would be a security bug
rather than an inconvenience: a brain would compute one ``relay_id``,
the control plane would compute another, and the binding between an id
and a key would stop meaning anything.

So both sides pin the same test vector, :data:`PINNED_VECTOR`. If either
implementation changes, one of the two test suites fails. That is
cheaper than a shared package and louder than a comment.
"""

from __future__ import annotations

import base64
import hashlib

#: Bytes of the SHA-256 digest kept. 20 bytes is exactly 32 base32
#: characters with no padding, and leaves ~80 bits of collision
#: resistance in a namespace whose entries are additionally bound to a
#: public key on first registration.
RELAY_ID_BYTES = 20

#: A 32-byte public key of all 0x01 and the id it must derive to.
#: Asserted by tests on both sides of the wire. Do not change without
#: changing both.
PINNED_VECTOR = (b"\x01" * 32, "olgw5bbcyqd7w3ijq2ipceylpxwx5qxx")


def derive_relay_id(public_key_bytes: bytes) -> str:
    """The DNS label a public key is entitled to. Derived, never chosen."""
    digest = hashlib.sha256(public_key_bytes).digest()[:RELAY_ID_BYTES]
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def verify_signature(payload: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """Check an Ed25519 signature. Never raises.

    Returns ``False`` for a bad signature, malformed base64, or a
    malformed key, so a caller cannot mistake an exception path for a
    successful verification.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        pub = ed25519.Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64)
        )
        pub.verify(base64.b64decode(signature_b64), payload)
        return True
    except Exception:
        return False
