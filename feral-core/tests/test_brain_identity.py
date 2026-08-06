"""A brain identity that can be proved, not just asserted.

``meta.brain_id`` is a uuid: a label anyone can copy into a QR code.
``relay_id`` is derived from an Ed25519 public key, so claiming one
means holding the corresponding private key. That distinction is what
lets the relay hand a brain a certificate for a name without being able
to impersonate it.
"""

from __future__ import annotations

import base64
import re

import pytest

from security import brain_identity


@pytest.fixture(autouse=True)
def _clear_cache():
    brain_identity._reset_cache()
    yield
    brain_identity._reset_cache()


class TestRelayIdDerivation:
    """Pure. No vault, no filesystem."""

    def test_is_a_valid_dns_label(self):
        rid = brain_identity.derive_relay_id(b"\x01" * 32)
        assert re.fullmatch(r"[a-z2-7]{32}", rid), rid

    def test_has_no_padding_and_no_hyphens(self):
        rid = brain_identity.derive_relay_id(b"\x07" * 32)
        assert "=" not in rid and "-" not in rid

    def test_fits_the_dns_label_limit(self):
        """63 octets per label. 32 leaves room and keeps QR density low."""
        assert len(brain_identity.derive_relay_id(b"\x01" * 32)) <= 63

    def test_is_deterministic(self):
        a = brain_identity.derive_relay_id(b"\x02" * 32)
        b = brain_identity.derive_relay_id(b"\x02" * 32)
        assert a == b

    def test_differs_per_key(self):
        a = brain_identity.derive_relay_id(b"\x03" * 32)
        b = brain_identity.derive_relay_id(b"\x04" * 32)
        assert a != b

    def test_a_one_bit_change_changes_the_whole_id(self):
        """It is a hash, not a truncation of the key."""
        a = brain_identity.derive_relay_id(bytes([0] * 32))
        b = brain_identity.derive_relay_id(bytes([1] + [0] * 31))
        assert a != b
        differing = sum(1 for x, y in zip(a, b) if x != y)
        assert differing > len(a) // 3, (a, b)


class TestSignAndVerify:
    """Exercised against generated keys directly, so these do not need a
    vault and cannot be broken by one being locked."""

    @staticmethod
    def _keypair():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        key = ed25519.Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return key, base64.b64encode(pub).decode("ascii")

    def test_a_valid_signature_verifies(self):
        key, pub_b64 = self._keypair()
        payload = b'{"relay_id":"abc","ts":1}'
        sig = base64.b64encode(key.sign(payload)).decode("ascii")
        assert brain_identity.verify(payload, sig, pub_b64) is True

    def test_a_tampered_payload_fails(self):
        key, pub_b64 = self._keypair()
        sig = base64.b64encode(key.sign(b"original")).decode("ascii")
        assert brain_identity.verify(b"tampered", sig, pub_b64) is False

    def test_a_signature_from_another_key_fails(self):
        _, pub_b64 = self._keypair()
        other, _ = self._keypair()
        payload = b"payload"
        sig = base64.b64encode(other.sign(payload)).decode("ascii")
        assert brain_identity.verify(payload, sig, pub_b64) is False

    @pytest.mark.parametrize(
        "sig, pub",
        [
            ("not-base64!!", "also-not-base64!!"),
            ("", ""),
            ("aGVsbG8=", "aGVsbG8="),  # valid base64, wrong lengths
        ],
    )
    def test_malformed_input_returns_false_rather_than_raising(self, sig, pub):
        """A caller must not be able to mistake an exception path for
        success, so verify never raises."""
        assert brain_identity.verify(b"payload", sig, pub) is False


class TestRelayIdIsBoundToTheKey:
    def test_a_persisted_id_that_disagrees_with_the_key_is_corrected(self, monkeypatch):
        """The key is authoritative. A brain must not drift into a
        different identity because a settings value was edited."""
        monkeypatch.setattr(
            brain_identity, "public_key_bytes", lambda: b"\x09" * 32
        )
        expected = brain_identity.derive_relay_id(b"\x09" * 32)

        class _Config:
            def __init__(self):
                self.data = {"meta": {"relay_id": "somethingelsewrittenbyhand"}}

            def get(self, section, key, default=None):
                return self.data.get(section, {}).get(key, default)

            def update_settings(self, section, key, value):
                self.data.setdefault(section, {})[key] = value

        cfg = _Config()
        assert brain_identity.relay_id(config=cfg) == expected
        assert cfg.data["meta"]["relay_id"] == expected

    def test_absent_persisted_id_is_recomputed_not_regenerated(self, monkeypatch):
        monkeypatch.setattr(
            brain_identity, "public_key_bytes", lambda: b"\x0a" * 32
        )
        expected = brain_identity.derive_relay_id(b"\x0a" * 32)

        class _Config:
            def __init__(self):
                self.data = {}

            def get(self, section, key, default=None):
                return self.data.get(section, {}).get(key, default)

            def update_settings(self, section, key, value):
                self.data.setdefault(section, {})[key] = value

        cfg = _Config()
        assert brain_identity.relay_id(config=cfg) == expected


def test_an_unreadable_key_refuses_rather_than_minting_a_new_one(monkeypatch):
    """Silently regenerating would change the relay_id and orphan any
    certificate already issued for the old one."""
    class _Vault:
        def get(self, *a, **kw):
            return "this is not valid base64 for a key !!!"

    monkeypatch.setattr(brain_identity, "_vault", lambda: _Vault())
    with pytest.raises(brain_identity.BrainIdentityUnavailable):
        brain_identity._load_private_key()


def test_brain_id_docstring_no_longer_claims_an_unenforced_check():
    """It described phone clients comparing brain_id to refuse a
    re-pair. No such check exists, and an unsigned uuid could not
    support one."""
    import inspect

    from config.loader import ConfigLoader

    doc = inspect.getdoc(ConfigLoader.brain_id) or ""
    # The text still quotes the old claim, deliberately, in order to
    # refute it. What must be present is the refutation and the pointer
    # to the identity that can actually be verified.
    assert "not a credential" in doc
    assert "No such check" in doc
    assert "brain_identity" in doc
