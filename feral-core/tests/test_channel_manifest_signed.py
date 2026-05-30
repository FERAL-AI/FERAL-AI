""" — signed-manifest verification path through  Ed25519 signer.

The contract here is the dial: ``allow_unsigned`` is the developer
convenience knob; a present-but-tampered signature is ALWAYS fatal,
regardless of that knob. Tests assert both arms of the dial.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path

import pytest

from channels.loader import (
    discover_bundled,
    load_with_verification,
)
from channels.manifest import (
    ManifestSignatureError,
    assert_signature,
    load_manifest_dict,
    sign_manifest,
    verify_signature,
)
from genui.manifest_signing import generate_keypair


def _bundled_telegram_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "channels"
        / "telegram"
        / "feral-channel.manifest.json"
    )


def _unsigned_manifest() -> dict:
    return {
        "id": "telegram",
        "providers": ["telegram"],
        "providerAuthEnvVars": {"telegram": ["FERAL_TELEGRAM_BOT_TOKEN"]},
        "capabilities": {"messagingProvider": True},
    }


class TestBundledTelegramSignature:
    def test_bundled_telegram_manifest_verifies(self) -> None:
        manifests = discover_bundled()
        telegram = next(m for m in manifests if m.id == "telegram")
        ok, reason = verify_signature(telegram)
        assert ok, f"bundled telegram manifest failed: {reason}"

    def test_load_with_verification_accepts_bundled(self) -> None:
        registry = load_with_verification(allow_unsigned=False)
        assert "telegram" in registry

    def test_one_byte_tamper_is_rejected(self, tmp_path: Path) -> None:
        # Copy the bundled telegram manifest, flip a single character in
        # the value (NOT the signature itself) — verification must
        # fail because canonical_json no longer matches.
        src = _bundled_telegram_path()
        data = json.loads(src.read_text(encoding="utf-8"))
        data["providerAuthEnvVars"]["telegram"] = ["FERAL_TELEGRAM_BOT_TOKEX"]  # one byte tamper

        # Drop the tampered manifest in an isolated bundle dir + verify.
        (tmp_path / "telegram").mkdir()
        (tmp_path / "telegram" / "feral-channel.manifest.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
        with pytest.raises(ManifestSignatureError) as exc:
            load_with_verification(root=tmp_path, allow_unsigned=False)
        assert "signature_mismatch" in str(exc.value)


class TestSigningRoundTrip:
    def test_round_trip_sign_then_verify(self) -> None:
        priv, _ = generate_keypair()
        signed = sign_manifest(_unsigned_manifest(), priv, public_key_id="test-key")
        m = load_manifest_dict(signed)
        ok, reason = verify_signature(m)
        assert ok, reason
        assert m.signature["publicKeyId"] == "test-key"
        assert m.signature["algo"] == "ed25519"

    def test_signing_does_not_mutate_input(self) -> None:
        priv, _ = generate_keypair()
        original = _unsigned_manifest()
        snapshot = json.dumps(original, sort_keys=True)
        signed = sign_manifest(original, priv)
        assert json.dumps(original, sort_keys=True) == snapshot
        assert "signature" in signed
        assert "signature" not in original

    def test_tamper_after_signing_breaks_verification(self) -> None:
        priv, _ = generate_keypair()
        signed = sign_manifest(_unsigned_manifest(), priv)
        signed["providerAuthEnvVars"]["telegram"] = ["DIFFERENT_VAR"]
        m = load_manifest_dict(signed)
        ok, reason = verify_signature(m)
        assert ok is False
        assert reason == "signature_mismatch"

    def test_signature_byte_flip_breaks_verification(self) -> None:
        priv, _ = generate_keypair()
        signed = sign_manifest(_unsigned_manifest(), priv)
        sig_b = bytearray(base64.b64decode(signed["signature"]["signature"]))
        sig_b[0] ^= 0x01  # flip one bit
        signed["signature"]["signature"] = base64.b64encode(bytes(sig_b)).decode("ascii")
        m = load_manifest_dict(signed)
        ok, reason = verify_signature(m)
        assert ok is False
        # PyNaCl reports this as a signature_mismatch (BadSignatureError).
        assert reason == "signature_mismatch"

    def test_assert_signature_raises_on_failure(self) -> None:
        priv, _ = generate_keypair()
        signed = sign_manifest(_unsigned_manifest(), priv)
        signed["signature"]["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        m = load_manifest_dict(signed)
        with pytest.raises(ManifestSignatureError):
            assert_signature(m)

    def test_public_key_provider_pin(self) -> None:
        priv, pub = generate_keypair()
        pub_b64 = base64.b64encode(pub).decode("ascii")
        signed = sign_manifest(_unsigned_manifest(), priv, public_key_id="pinned")
        m = load_manifest_dict(signed)

        # Correct pin → ok.
        ok, reason = verify_signature(m, public_key_provider=lambda kid: pub_b64)
        assert ok, reason

        # Wrong pin → key_mismatch ( wire-contract reason).
        wrong_b64 = base64.b64encode(b"\x00" * 32).decode("ascii")
        ok, reason = verify_signature(m, public_key_provider=lambda kid: wrong_b64)
        assert ok is False
        assert reason == "key_mismatch"


class TestVerificationDial:
    def test_unsigned_rejected_by_default(self, tmp_path: Path) -> None:
        (tmp_path / "telegram").mkdir()
        (tmp_path / "telegram" / "feral-channel.manifest.json").write_text(
            json.dumps(_unsigned_manifest()), encoding="utf-8"
        )
        with pytest.raises(ManifestSignatureError) as exc:
            load_with_verification(root=tmp_path, allow_unsigned=False)
        assert "unsigned" in str(exc.value)

    def test_unsigned_allowed_when_flag_set(self, tmp_path: Path) -> None:
        (tmp_path / "telegram").mkdir()
        (tmp_path / "telegram" / "feral-channel.manifest.json").write_text(
            json.dumps(_unsigned_manifest()), encoding="utf-8"
        )
        registry = load_with_verification(root=tmp_path, allow_unsigned=True)
        assert "telegram" in registry

    def test_tampered_signature_fatal_even_with_allow_unsigned(self, tmp_path: Path) -> None:
        # Sign cleanly, then corrupt the signature payload — even with
        # allow_unsigned=True the loader must refuse.
        priv, _ = generate_keypair()
        signed = sign_manifest(_unsigned_manifest(), priv)
        signed["signature"]["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")

        (tmp_path / "telegram").mkdir()
        (tmp_path / "telegram" / "feral-channel.manifest.json").write_text(
            json.dumps(signed), encoding="utf-8"
        )
        with pytest.raises(ManifestSignatureError):
            load_with_verification(root=tmp_path, allow_unsigned=True)

    def test_unknown_signature_alg_rejected(self) -> None:
        priv, _ = generate_keypair()
        signed = sign_manifest(_unsigned_manifest(), priv)
        # Patch the in-memory dict before validation so we hit the
        # *schema* layer's algo check (not a verifier path).
        signed["signature"]["algo"] = "rsa-pss"
        with pytest.raises(Exception):
            load_manifest_dict(signed)
