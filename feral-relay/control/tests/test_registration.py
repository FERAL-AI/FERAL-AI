"""Registration is the whole trust model, so it gets adversarial tests.

There are no accounts. A brain's claim to a relay id rests entirely on
the id being derived from a key it can sign with. Each test below is one
way that could be subverted.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from feral_relay_cp.identity import PINNED_VECTOR, derive_relay_id
from feral_relay_cp.registration import (
    MAX_CLOCK_SKEW_SECONDS,
    BrainRecord,
    RegistrationError,
    canonical_payload,
    register,
)


class MemoryStore:
    def __init__(self):
        self.brains: dict[str, BrainRecord] = {}
        self.nonces: set[tuple[str, str]] = set()

    def get(self, relay_id):
        return self.brains.get(relay_id)

    def put(self, record):
        self.brains[record.relay_id] = record

    def seen_nonce(self, relay_id, nonce):
        return (relay_id, nonce) in self.nonces

    def remember_nonce(self, relay_id, nonce, expires_at):
        self.nonces.add((relay_id, nonce))


def _keypair():
    key = ed25519.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return key, pub, base64.b64encode(pub).decode("ascii")


def _signed_body(key, pub, pub_b64, *, relay_id=None, ts=None, nonce="n-1"):
    body = {
        "relay_id": relay_id or derive_relay_id(pub),
        "public_key": pub_b64,
        "ts": time.time() if ts is None else ts,
        "nonce": nonce,
    }
    body["signature"] = base64.b64encode(
        key.sign(canonical_payload(body))
    ).decode("ascii")
    return body


class TestTheDerivationIsPinned:
    def test_matches_the_shared_vector(self):
        """Both sides of the wire pin this. If the brain's derivation
        changes and this does not, ids stop agreeing and the binding
        between an id and a key stops meaning anything."""
        pub, expected = PINNED_VECTOR
        assert derive_relay_id(pub) == expected


class TestHonestRegistration:
    def test_a_correctly_signed_first_registration_succeeds(self):
        key, pub, pub_b64 = _keypair()
        store = MemoryStore()
        record = register(_signed_body(key, pub, pub_b64), store)
        assert record.relay_id == derive_relay_id(pub)
        assert record.public_key_b64 == pub_b64

    def test_re_registering_with_the_same_key_is_allowed(self):
        """A brain that restarts must be able to say hello again."""
        key, pub, pub_b64 = _keypair()
        store = MemoryStore()
        first = register(_signed_body(key, pub, pub_b64, nonce="a"), store)
        second = register(_signed_body(key, pub, pub_b64, nonce="b"), store)
        assert second.relay_id == first.relay_id
        # created_at is preserved; last_seen_at moves.
        assert second.created_at == first.created_at
        assert second.last_seen_at >= first.last_seen_at


class TestForgeryIsRefused:
    def test_claiming_someone_elses_id_with_your_own_key_is_refused(self):
        """The attack the derivation check exists to stop.

        A valid signature only proves you hold *a* key. Without the
        derivation check, signing a body that names the victim's
        relay_id would register the attacker's key against it.
        """
        _, victim_pub, _ = _keypair()
        attacker, attacker_pub, attacker_pub_b64 = _keypair()
        victim_id = derive_relay_id(victim_pub)

        body = _signed_body(
            attacker, attacker_pub, attacker_pub_b64, relay_id=victim_id
        )
        with pytest.raises(RegistrationError) as exc:
            register(body, MemoryStore())
        assert exc.value.code == "relay_id_mismatch"

    def test_a_body_signed_by_a_different_key_is_refused(self):
        _, pub, pub_b64 = _keypair()
        other, _, _ = _keypair()
        body = {
            "relay_id": derive_relay_id(pub),
            "public_key": pub_b64,
            "ts": time.time(),
            "nonce": "n",
        }
        body["signature"] = base64.b64encode(
            other.sign(canonical_payload(body))
        ).decode("ascii")

        with pytest.raises(RegistrationError) as exc:
            register(body, MemoryStore())
        assert exc.value.code == "bad_signature"

    def test_tampering_with_a_signed_field_is_refused(self):
        key, pub, pub_b64 = _keypair()
        body = _signed_body(key, pub, pub_b64)
        body["ts"] = time.time() + 1  # inside the window, but not signed for
        with pytest.raises(RegistrationError) as exc:
            register(body, MemoryStore())
        assert exc.value.code == "bad_signature"

    def test_every_signed_field_is_actually_covered(self):
        """A field in the request that is not in canonical_payload is
        unauthenticated. Assert the set, so adding one to the request
        without adding it here fails loudly."""
        key, pub, pub_b64 = _keypair()
        body = _signed_body(key, pub, pub_b64)
        signed = json.loads(canonical_payload(body))
        assert set(signed) == {"relay_id", "public_key", "ts", "nonce"}

    def test_rebinding_an_id_to_another_key_is_refused(self):
        key, pub, pub_b64 = _keypair()
        store = MemoryStore()
        register(_signed_body(key, pub, pub_b64, nonce="a"), store)

        # Forge a stored record so the rebinding branch is reachable
        # even though the derivation check would normally prevent it.
        other, _, other_b64 = _keypair()
        store.brains[derive_relay_id(pub)] = BrainRecord(
            relay_id=derive_relay_id(pub),
            public_key_b64=other_b64,
            created_at=1.0,
            last_seen_at=1.0,
        )
        with pytest.raises(RegistrationError) as exc:
            register(_signed_body(key, pub, pub_b64, nonce="c"), store)
        assert exc.value.code == "already_bound"


class TestReplay:
    def test_a_replayed_request_is_refused(self):
        key, pub, pub_b64 = _keypair()
        store = MemoryStore()
        body = _signed_body(key, pub, pub_b64, nonce="same")
        register(body, store)
        with pytest.raises(RegistrationError) as exc:
            register(dict(body), store)
        assert exc.value.code == "replayed_nonce"

    def test_a_request_from_too_far_in_the_past_is_refused(self):
        key, pub, pub_b64 = _keypair()
        old = time.time() - (MAX_CLOCK_SKEW_SECONDS + 60)
        with pytest.raises(RegistrationError) as exc:
            register(_signed_body(key, pub, pub_b64, ts=old), MemoryStore())
        assert exc.value.code == "stale_request"

    def test_a_request_from_the_future_is_refused(self):
        """Symmetric, so a client with a fast clock cannot mint requests
        that stay valid for hours."""
        key, pub, pub_b64 = _keypair()
        future = time.time() + (MAX_CLOCK_SKEW_SECONDS + 60)
        with pytest.raises(RegistrationError) as exc:
            register(_signed_body(key, pub, pub_b64, ts=future), MemoryStore())
        assert exc.value.code == "stale_request"

    def test_modest_clock_skew_is_tolerated(self):
        """A laptop that has been asleep should still register."""
        key, pub, pub_b64 = _keypair()
        skewed = time.time() - (MAX_CLOCK_SKEW_SECONDS - 30)
        record = register(_signed_body(key, pub, pub_b64, ts=skewed), MemoryStore())
        assert record.relay_id


class TestMalformedInput:
    @pytest.mark.parametrize(
        "field", ["relay_id", "public_key", "ts", "nonce", "signature"]
    )
    def test_a_missing_field_is_refused(self, field):
        key, pub, pub_b64 = _keypair()
        body = _signed_body(key, pub, pub_b64)
        body.pop(field)
        with pytest.raises(RegistrationError) as exc:
            register(body, MemoryStore())
        assert exc.value.code == "missing_field"

    def test_a_public_key_of_the_wrong_length_is_refused(self):
        key, pub, pub_b64 = _keypair()
        body = _signed_body(key, pub, pub_b64)
        body["public_key"] = base64.b64encode(b"too short").decode("ascii")
        with pytest.raises(RegistrationError):
            register(body, MemoryStore())

    def test_a_non_numeric_timestamp_is_refused(self):
        key, pub, pub_b64 = _keypair()
        body = _signed_body(key, pub, pub_b64)
        body["ts"] = "yesterday"
        with pytest.raises(RegistrationError) as exc:
            register(body, MemoryStore())
        assert exc.value.code == "bad_timestamp"
