"""A brain identity you can actually prove.

``meta.brain_id`` is a uuid4. It names a brain and proves nothing: a
uuid is a label anyone can copy into a QR code, and the loader's own
docstring described phone clients "refusing to re-pair against a
different brain by comparing the QR's brain_id" — a check that could not
work, because nothing signs the value and the transport could not
deliver it. That claim is now corrected at the source.

This module adds the second identity the relay needs, and the one a
client could genuinely verify:

* an **Ed25519 keypair**, generated once at first use, private half
  stored in the existing BlindVault so it is encrypted at rest with the
  rest of the brain's secrets;
* a **relay_id**, derived from the public key rather than chosen, so it
  cannot be claimed by anyone who does not hold the private key.

``relay_id = base32_lower(sha256(pubkey)[:20])`` gives 32 characters
from ``[a-z2-7]``: a valid DNS label with no padding and no hyphens, so
it drops straight into ``<relay_id>.relay.feral.sh``. Truncating SHA-256
to 160 bits leaves ~80 bits of collision resistance, which is ample for
a namespace whose entries are also bound to a public key on first
registration.

The private key never leaves this machine. The control plane learns the
public key and issues certificates for a name derived from it; it can
neither impersonate the brain nor decrypt its traffic. That property is
what keeps the local-first claim honest once a relay exists, so it is
enforced here rather than documented elsewhere.

``brain_id`` is unchanged and still emitted, because the shipped iOS
build parses it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional

logger = logging.getLogger("feral.security.brain_identity")

VAULT_NAMESPACE = "brain_identity"
VAULT_KEY = "ed25519_private_key"

#: Bytes of the SHA-256 digest kept for the relay id. 20 bytes encodes
#: to exactly 32 base32 characters with no padding.
_RELAY_ID_BYTES = 20

_cached_relay_id: Optional[str] = None


class BrainIdentityUnavailable(RuntimeError):
    """The keypair could not be loaded or created.

    Raised rather than returning a placeholder. An identity that
    silently degrades is worse than one that is absent: callers would
    register a brain under an id it cannot later prove it owns.
    """


def _ed25519():
    """Import lazily so a brain without ``cryptography`` still boots."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise BrainIdentityUnavailable(
            "the `cryptography` package is required for brain identity"
        ) from exc
    return ed25519


def _serialization():
    from cryptography.hazmat.primitives import serialization

    return serialization


def _vault():
    from security.vault import BlindVault

    return BlindVault()


def derive_relay_id(public_key_bytes: bytes) -> str:
    """The DNS label for a public key. Derived, never chosen.

    Deterministic, so a brain that loses its persisted ``meta.relay_id``
    recomputes the same value from its key rather than silently becoming
    a different brain.
    """
    digest = hashlib.sha256(public_key_bytes).digest()[:_RELAY_ID_BYTES]
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def _load_private_key():
    """Return the stored private key, or None when there is not one."""
    try:
        stored = _vault().get(VAULT_NAMESPACE, VAULT_KEY, requester="brain_identity")
    except Exception as exc:
        raise BrainIdentityUnavailable(f"vault unavailable: {exc}") from exc
    if not stored:
        return None
    try:
        raw = base64.b64decode(stored)
        return _ed25519().Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:
        raise BrainIdentityUnavailable(
            "the stored brain identity key is unreadable. Refusing to mint a "
            "replacement, because that would silently change this brain's "
            "relay_id and orphan any certificate issued for it."
        ) from exc


def _create_private_key():
    """Generate and persist a new keypair. First boot only."""
    key = _ed25519().Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        encoding=_serialization().Encoding.Raw,
        format=_serialization().PrivateFormat.Raw,
        encryption_algorithm=_serialization().NoEncryption(),
    )
    try:
        _vault().put(
            VAULT_NAMESPACE,
            VAULT_KEY,
            base64.b64encode(raw).decode("ascii"),
            stored_by="brain_identity",
        )
    except Exception as exc:
        raise BrainIdentityUnavailable(f"could not persist identity: {exc}") from exc
    logger.info("brain_identity: generated a new Ed25519 identity")
    return key


def private_key():
    """The brain's signing key, creating one on first use."""
    return _load_private_key() or _create_private_key()


def public_key_bytes() -> bytes:
    return private_key().public_key().public_bytes(
        encoding=_serialization().Encoding.Raw,
        format=_serialization().PublicFormat.Raw,
    )


def public_key_b64() -> str:
    """The public half, base64, for registration and verification."""
    return base64.b64encode(public_key_bytes()).decode("ascii")


def relay_id(config=None) -> str:
    """This brain's relay id, persisted at ``meta.relay_id``.

    Recomputed from the key when absent, so the persisted value is a
    cache rather than the source of truth. A brain cannot drift into a
    different identity by losing a settings key.
    """
    global _cached_relay_id
    if _cached_relay_id:
        return _cached_relay_id

    derived = derive_relay_id(public_key_bytes())

    if config is None:
        try:
            from api.state import state

            config = getattr(state, "config", None)
        except Exception:  # pragma: no cover - defensive
            config = None

    if config is not None:
        try:
            persisted = (config.get("meta", "relay_id", "") or "").strip()
            if persisted and persisted != derived:
                logger.warning(
                    "brain_identity: persisted relay_id %r does not match the "
                    "one derived from this brain's key (%r). The key is "
                    "authoritative; correcting the persisted value.",
                    persisted, derived,
                )
            if persisted != derived:
                config.update_settings("meta", "relay_id", derived)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("brain_identity: could not persist relay_id: %s", exc)

    _cached_relay_id = derived
    return derived


def sign(payload: bytes) -> str:
    """Sign bytes with the brain's key. Returns base64."""
    return base64.b64encode(private_key().sign(payload)).decode("ascii")


def verify(payload: bytes, signature_b64: str, public_key_b64_value: str) -> bool:
    """Check a signature against a public key. Never raises.

    Returns ``False`` on a bad signature, malformed base64, or a
    malformed key, so callers cannot accidentally treat an exception
    path as success.
    """
    try:
        pub = _ed25519().Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64_value)
        )
        pub.verify(base64.b64decode(signature_b64), payload)
        return True
    except Exception:
        return False


def _reset_cache() -> None:
    """Test seam. Registered in the shared-state resetters."""
    global _cached_relay_id
    _cached_relay_id = None
