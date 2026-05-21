"""W9 — vault encryption-at-rest tests.

Covers:
  * write/read round-trip with a temp keyring (dict-backed fake patched
    onto :mod:`security.vault`'s keychain wrapper — never the real
    macOS Keychain / Linux Secret Service)
  * AEAD tamper detection: a single byte flip in ``credentials.enc``
    must raise :class:`security.vault.VaultTamperedError` instead of
    silently zero-filling the cache
  * rotation: the previous master key no longer decrypts the new file,
    the new master key (and its freshly-printed recovery code) does
"""

from __future__ import annotations

from pathlib import Path

import pytest

import security.vault as vault_mod
from security.vault import (
    BlindVault,
    VaultTamperedError,
    encode_recovery_code,
    decode_recovery_code,
    KEYRING_SERVICE,
    KEYRING_USERNAME,
)


# ─────────────────────────────────────────────────────────────────────
# Fake keychain — dict-backed so tests never touch the real OS keychain
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_keychain(monkeypatch):
    """Replace the keychain wrapper with an in-memory dict.

    Returns the dict so tests can assert on stored values + simulate
    "keychain wiped" by clearing it.
    """
    store: dict[tuple[str, str], str] = {}

    def fake_get(service, username):
        return store.get((service, username))

    def fake_set(service, username, password):
        store[(service, username)] = password

    def fake_delete(service, username):
        store.pop((service, username), None)

    monkeypatch.setattr(vault_mod, "_keyring_get_password", fake_get)
    monkeypatch.setattr(vault_mod, "_keyring_set_password", fake_set)
    monkeypatch.setattr(vault_mod, "_keyring_delete_password", fake_delete)
    return store


@pytest.fixture
def vault(tmp_path: Path, fake_keychain, monkeypatch) -> BlindVault:
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)
    return BlindVault(vault_path=str(tmp_path / "credentials.json"))


# ─────────────────────────────────────────────────────────────────────
# Round-trip + on-disk shape
# ─────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_set_credential_round_trip(self, vault: BlindVault, tmp_path: Path):
        vault.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        assert vault.get_credential("OPENAI_API_KEY") == "test-key-do-not-commit"

        # Encrypted file exists and does NOT contain the plaintext.
        enc_path = tmp_path / "credentials.enc"
        assert enc_path.exists(), "credentials.enc was not created"
        raw = enc_path.read_bytes()
        assert b"test-key-do-not-commit" not in raw, (
            "plaintext leaked into the encrypted file — encryption is "
            "not actually being applied"
        )
        # Smallest plausible AEAD payload: 12-byte nonce + 16-byte tag.
        assert len(raw) >= 28

    def test_namespaced_put_get(self, vault: BlindVault):
        vault.put("publisher_keys", "alice", "test-key-do-not-commit")
        vault.put("publisher_keys", "bob", "test-key-do-not-commit-2")
        assert vault.get("publisher_keys", "alice") == "test-key-do-not-commit"
        assert vault.get("publisher_keys", "bob") == "test-key-do-not-commit-2"
        # Default credentials namespace stays isolated.
        assert vault.get_credential("alice") is None
        assert sorted(vault.list_namespace("publisher_keys")) == ["alice", "bob"]
        assert "publisher_keys" in vault.list_namespaces()
        assert "credentials" in vault.list_namespaces()

    def test_legacy_store_retrieve_still_work(self, vault: BlindVault):
        vault.store("svc_a", "secret-one", stored_by="test")
        vault.store("svc_b", "secret-two", stored_by="test")
        assert vault.retrieve("svc_a") == "secret-one"
        assert vault.retrieve("svc_b") == "secret-two"
        assert sorted(vault.list_keys()) == ["svc_a", "svc_b"]
        assert vault.has_key("svc_a") is True
        assert vault.has_key("nope") is False
        assert vault.fingerprint("svc_a") is not None
        assert len(vault.fingerprint("svc_a")) == 12

    def test_persisted_state_survives_reopen(
        self, tmp_path: Path, fake_keychain, monkeypatch
    ):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)

        v1 = BlindVault(vault_path=str(tmp_path / "credentials.json"))
        v1.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        v1.put("publisher_keys", "alice", "another-test-key")

        v2 = BlindVault(vault_path=str(tmp_path / "credentials.json"))
        assert v2.get_credential("OPENAI_API_KEY") == "test-key-do-not-commit"
        assert v2.get("publisher_keys", "alice") == "another-test-key"

    def test_first_boot_emits_recovery_code(
        self, tmp_path: Path, fake_keychain, monkeypatch
    ):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)

        v = BlindVault(vault_path=str(tmp_path / "credentials.json"))
        # Trigger a write so the master key actually gets generated.
        v.set_credential("trigger", "test-key-do-not-commit")
        code = v.consume_first_boot_recovery_code()
        assert code is not None and code, (
            "first boot must emit a recovery code"
        )
        assert v.consume_first_boot_recovery_code() is None, (
            "recovery code must be one-shot — second consume returns None"
        )
        # Recovery code round-trips back to the master key.
        decoded = decode_recovery_code(code)
        assert len(decoded) == 32
        # And the keychain holds the same key (base64-encoded).
        import base64
        stored = fake_keychain[(KEYRING_SERVICE, KEYRING_USERNAME)]
        assert base64.b64decode(stored) == decoded


# ─────────────────────────────────────────────────────────────────────
# Tamper detection
# ─────────────────────────────────────────────────────────────────────


class TestTamperDetection:
    def test_byte_flip_raises_vault_tampered_error(
        self, vault: BlindVault, tmp_path: Path, fake_keychain, monkeypatch
    ):
        vault.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        enc_path = tmp_path / "credentials.enc"
        raw = bytearray(enc_path.read_bytes())
        # Flip the last byte (inside the AEAD tag).
        raw[-1] ^= 0x01
        enc_path.write_bytes(raw)

        with pytest.raises(VaultTamperedError) as exc:
            BlindVault(vault_path=str(tmp_path / "credentials.json"))
        assert "AEAD verification failed" in str(exc.value)

    def test_truncated_file_raises_vault_tampered_error(
        self, vault: BlindVault, tmp_path: Path
    ):
        vault.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        enc_path = tmp_path / "credentials.enc"
        # Truncate to below the minimum AEAD payload size.
        enc_path.write_bytes(b"\x00" * 10)

        with pytest.raises(VaultTamperedError):
            BlindVault(vault_path=str(tmp_path / "credentials.json"))

    def test_wrong_master_key_in_keychain_raises(
        self, vault: BlindVault, tmp_path: Path, fake_keychain
    ):
        vault.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")

        # Replace the keychain entry with a different valid 32-byte key.
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        wrong_key = ChaCha20Poly1305.generate_key()
        fake_keychain[(KEYRING_SERVICE, KEYRING_USERNAME)] = base64.b64encode(
            wrong_key
        ).decode()

        with pytest.raises(VaultTamperedError):
            BlindVault(vault_path=str(tmp_path / "credentials.json"))


# ─────────────────────────────────────────────────────────────────────
# Rotation
# ─────────────────────────────────────────────────────────────────────


class TestRotation:
    def test_rotate_emits_new_code_and_changes_keychain(
        self, vault: BlindVault, tmp_path: Path, fake_keychain
    ):
        import base64

        vault.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        # Drop the first-boot code so the test only sees the rotation
        # code (the contract is "shown ONCE per occasion").
        vault.consume_first_boot_recovery_code()

        old_key_b64 = fake_keychain[(KEYRING_SERVICE, KEYRING_USERNAME)]
        old_key = base64.b64decode(old_key_b64)

        new_code = vault.rotate_master_key()
        assert new_code, "rotation must return a recovery code"
        new_key = decode_recovery_code(new_code)
        assert new_key != old_key, "rotation must produce a different key"

        # Keychain now holds the new key.
        new_keychain_entry = base64.b64decode(
            fake_keychain[(KEYRING_SERVICE, KEYRING_USERNAME)]
        )
        assert new_keychain_entry == new_key

        # .enc.prev exists with mode 0600.
        prev = tmp_path / "credentials.enc.prev"
        assert prev.exists()
        import stat
        assert stat.S_IMODE(prev.stat().st_mode) == 0o600

    def test_old_master_key_no_longer_decrypts_after_rotate(
        self, vault: BlindVault, tmp_path: Path, fake_keychain
    ):
        import base64

        vault.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        vault.consume_first_boot_recovery_code()

        old_key_b64 = fake_keychain[(KEYRING_SERVICE, KEYRING_USERNAME)]
        new_code = vault.rotate_master_key()
        new_key = decode_recovery_code(new_code)

        # Force the keychain to ONLY hold the OLD key — try to open the
        # new vault with the old key. Must raise VaultTamperedError.
        fake_keychain[(KEYRING_SERVICE, KEYRING_USERNAME)] = old_key_b64
        with pytest.raises(VaultTamperedError):
            BlindVault(vault_path=str(tmp_path / "credentials.json"))

        # Restoring the new key (e.g. what restore_from_recovery_code
        # would do) makes the file readable again.
        fake_keychain[(KEYRING_SERVICE, KEYRING_USERNAME)] = base64.b64encode(
            new_key
        ).decode()
        v3 = BlindVault(vault_path=str(tmp_path / "credentials.json"))
        assert v3.get_credential("OPENAI_API_KEY") == "test-key-do-not-commit"

    def test_recovery_code_round_trip_after_rotate(
        self, vault: BlindVault, tmp_path: Path, fake_keychain
    ):
        import base64
        vault.set_credential("foo", "test-key-do-not-commit")
        new_code = vault.rotate_master_key()

        decoded = decode_recovery_code(new_code)
        # Encoding it back must yield the same printable form (modulo
        # case + whitespace which decode_recovery_code normalises).
        assert encode_recovery_code(decoded) == new_code

        # Sanity: the keychain holds exactly that key.
        stored = base64.b64decode(
            fake_keychain[(KEYRING_SERVICE, KEYRING_USERNAME)]
        )
        assert stored == decoded

    def test_prev_file_cleared_after_successful_boot(
        self, vault: BlindVault, tmp_path: Path, fake_keychain
    ):
        vault.set_credential("foo", "test-key-do-not-commit")
        vault.rotate_master_key()
        prev = tmp_path / "credentials.enc.prev"
        assert prev.exists()
        # Reopening the vault constitutes a "successful boot" → .prev
        # is cleaned up.
        BlindVault(vault_path=str(tmp_path / "credentials.json"))
        assert not prev.exists(), (
            "credentials.enc.prev must be removed after the next "
            "successful boot"
        )


# ─────────────────────────────────────────────────────────────────────
# Restore from recovery code
# ─────────────────────────────────────────────────────────────────────


class TestRestoreFromRecoveryCode:
    def test_recover_from_wiped_keychain(
        self, vault: BlindVault, tmp_path: Path, fake_keychain
    ):
        vault.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        code = vault.consume_first_boot_recovery_code()
        assert code

        # Wipe the keychain entirely → the next boot would normally
        # raise VaultKeyUnavailableError. Instead the user supplies the
        # recovery code via env var.
        fake_keychain.clear()
        # Fresh BlindVault with the recovery code in env recovers and
        # re-seeds the keychain.
        import os
        os.environ["FERAL_VAULT_RECOVERY_CODE"] = code
        try:
            v2 = BlindVault(vault_path=str(tmp_path / "credentials.json"))
            assert v2.get_credential("OPENAI_API_KEY") == "test-key-do-not-commit"
            # Keychain re-seeded.
            assert (KEYRING_SERVICE, KEYRING_USERNAME) in fake_keychain
        finally:
            os.environ.pop("FERAL_VAULT_RECOVERY_CODE", None)

    def test_wrong_recovery_code_raises(
        self, vault: BlindVault, tmp_path: Path, fake_keychain
    ):
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

        vault.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        wrong_code = encode_recovery_code(ChaCha20Poly1305.generate_key())

        with pytest.raises(VaultTamperedError):
            vault.restore_from_recovery_code(wrong_code)


# ─────────────────────────────────────────────────────────────────────
# ConfigLoader integration — vault as credential authority
# ─────────────────────────────────────────────────────────────────────


class TestConfigLoaderVaultIntegration:
    def test_loader_reads_credentials_from_vault(
        self, tmp_path: Path, fake_keychain, monkeypatch
    ):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        vault = BlindVault(vault_path=str(tmp_path / "credentials.json"))
        vault.set_credential("OPENAI_API_KEY", "sk-from-vault")
        vault.set_credential("ANTHROPIC_API_KEY", "sk-ant-from-vault")

        from config.loader import ConfigLoader

        loader = ConfigLoader(project_dir=str(tmp_path))
        loader.user_home = tmp_path
        loader.discover()

        assert loader.get_credential("OPENAI_API_KEY") == "sk-from-vault"
        assert loader.get_credential("ANTHROPIC_API_KEY") == "sk-ant-from-vault"

    def test_loader_env_overrides_vault(
        self, tmp_path: Path, fake_keychain, monkeypatch
    ):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")

        vault = BlindVault(vault_path=str(tmp_path / "credentials.json"))
        vault.set_credential("OPENAI_API_KEY", "sk-from-vault")

        from config.loader import ConfigLoader

        loader = ConfigLoader(project_dir=str(tmp_path))
        loader.user_home = tmp_path
        loader.discover()

        assert loader.get_credential("OPENAI_API_KEY") == "sk-from-env"

    def test_loader_warns_on_legacy_plaintext_without_reading(
        self, tmp_path: Path, fake_keychain, monkeypatch, caplog
    ):
        import json
        import logging

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        legacy = tmp_path / "credentials.json"
        legacy.write_text(json.dumps({"OPENAI_API_KEY": "sk-legacy-plain"}))

        from config.loader import ConfigLoader

        with caplog.at_level(logging.WARNING, logger="feral.config"):
            loader = ConfigLoader(project_dir=str(tmp_path))
            loader.user_home = tmp_path
            loader.discover()

        assert any(
            "Deprecated plaintext credentials.json" in rec.message
            for rec in caplog.records
        )
        # Vault migration should have moved legacy into encrypted store.
        assert (tmp_path / "credentials.enc").exists()
        assert loader.get_credential("OPENAI_API_KEY") == "sk-legacy-plain"
