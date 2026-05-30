""" — vault legacy → encrypted migration tests.

Covers the migration triggered when ``~/.feral/credentials.json``
exists but ``credentials.enc`` does not:

  * plaintext credentials get re-encrypted to ``credentials.enc``
  * a backup is written to ``credentials.json.bak.legacy`` (chmod 0600)
  * the original ``credentials.json`` is unlinked
  * subsequent reads use ``credentials.enc`` and ignore ``.bak``
  * if both ``.enc`` and ``.json`` exist, ``.enc`` wins and a warning
    is logged
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

import security.vault as vault_mod
from security.vault import BlindVault


@pytest.fixture
def fake_keychain(monkeypatch):
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
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)
    return tmp_path


# ─────────────────────────────────────────────────────────────────────
# Migration on first read
# ─────────────────────────────────────────────────────────────────────


class TestPlaintextMigration:
    def test_seeds_get_re_encrypted(
        self, isolated_home: Path, fake_keychain, caplog
    ):
        legacy = isolated_home / "credentials.json"
        legacy.write_text(json.dumps({
            "OPENAI_API_KEY": "test-key-do-not-commit",
            "GROQ_API_KEY": "test-key-do-not-commit-2",
        }))
        os.chmod(legacy, 0o600)

        with caplog.at_level(logging.INFO, logger="feral.vault"):
            v = BlindVault(vault_path=str(legacy))

        # The encrypted file exists.
        enc = isolated_home / "credentials.enc"
        assert enc.exists()
        # The backup exists with chmod 0600.
        backup = isolated_home / "credentials.json.bak.legacy"
        assert backup.exists()
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        # Backup contents are byte-identical to the original.
        assert json.loads(backup.read_text()) == {
            "OPENAI_API_KEY": "test-key-do-not-commit",
            "GROQ_API_KEY": "test-key-do-not-commit-2",
        }
        # Original is gone.
        assert not legacy.exists()
        # The data round-trips.
        assert v.get_credential("OPENAI_API_KEY") == "test-key-do-not-commit"
        assert v.get_credential("GROQ_API_KEY") == "test-key-do-not-commit-2"
        # The well-known migration log line was emitted exactly once.
        migration_lines = [
            r for r in caplog.records
            if "vault.migrated_to_encrypted" in r.getMessage()
        ]
        assert len(migration_lines) == 1, (
            f"expected exactly one migration log line, got {migration_lines}"
        )

    def test_subsequent_reads_use_enc_and_ignore_backup(
        self, isolated_home: Path, fake_keychain
    ):
        legacy = isolated_home / "credentials.json"
        legacy.write_text(json.dumps({"OPENAI_API_KEY": "test-key-do-not-commit"}))

        # First open triggers migration.
        BlindVault(vault_path=str(legacy))

        # The .bak.legacy file is irrelevant on subsequent reads — even
        # if we corrupt it, the vault still works.
        backup = isolated_home / "credentials.json.bak.legacy"
        backup.write_text("{ corrupted backup }")

        v = BlindVault(vault_path=str(legacy))
        assert v.get_credential("OPENAI_API_KEY") == "test-key-do-not-commit"

    def test_namespaced_legacy_payload_is_preserved(
        self, isolated_home: Path, fake_keychain
    ):
        legacy = isolated_home / "credentials.json"
        # A user might have already namespaced their credentials by
        # hand-editing — the migration must preserve the structure.
        legacy.write_text(json.dumps({
            "credentials": {"OPENAI_API_KEY": "test-key-do-not-commit"},
            "publisher_keys": {"alice": "test-key-do-not-commit-2"},
        }))

        v = BlindVault(vault_path=str(legacy))
        assert v.get_credential("OPENAI_API_KEY") == "test-key-do-not-commit"
        assert v.get("publisher_keys", "alice") == "test-key-do-not-commit-2"

    def test_corrupt_legacy_json_is_moved_aside(
        self, isolated_home: Path, fake_keychain, caplog
    ):
        legacy = isolated_home / "credentials.json"
        legacy.write_text("{ this is not json")

        with caplog.at_level(logging.WARNING, logger="feral.vault"):
            v = BlindVault(vault_path=str(legacy))

        # The corrupt file was moved to .corrupt (legacy behaviour
        # preserved), the vault starts empty, and store still works.
        assert (isolated_home / "credentials.corrupt").exists()
        assert v.list_keys() == []
        v.set_credential("OPENAI_API_KEY", "test-key-do-not-commit")
        assert v.get_credential("OPENAI_API_KEY") == "test-key-do-not-commit"


# ─────────────────────────────────────────────────────────────────────
# Both files present
# ─────────────────────────────────────────────────────────────────────


class TestBothFilesPresent:
    def test_enc_wins_and_warning_logged(
        self, isolated_home: Path, fake_keychain, caplog
    ):
        # 1. Establish the encrypted vault with the "real" data.
        v1 = BlindVault(vault_path=str(isolated_home / "credentials.json"))
        v1.set_credential("OPENAI_API_KEY", "real-encrypted-value")

        # 2. Re-create credentials.json by hand with stale data — this
        # is what happens if a legacy writer (e.g. ConfigLoader.save_credentials,
        # see follow-ups) puts the file back after a migration boot.
        stale = isolated_home / "credentials.json"
        stale.write_text(json.dumps({"OPENAI_API_KEY": "stale-do-not-use"}))

        with caplog.at_level(logging.WARNING, logger="feral.vault"):
            v2 = BlindVault(vault_path=str(isolated_home / "credentials.json"))

        # The encrypted value wins.
        assert v2.get_credential("OPENAI_API_KEY") == "real-encrypted-value"
        # A warning was logged so operators see the situation.
        warning_lines = [
            r for r in caplog.records
            if "vault.both_files_present" in r.getMessage()
        ]
        assert warning_lines, (
            "expected vault.both_files_present warning when both .enc "
            "and .json are present"
        )


# ─────────────────────────────────────────────────────────────────────
# Migration is idempotent — second boot is a plain read, not another
# migration cycle.
# ─────────────────────────────────────────────────────────────────────


class TestMigrationIdempotency:
    def test_second_boot_does_not_re_migrate(
        self, isolated_home: Path, fake_keychain, caplog
    ):
        legacy = isolated_home / "credentials.json"
        legacy.write_text(json.dumps({"X": "test-key-do-not-commit"}))

        BlindVault(vault_path=str(legacy))
        # legacy is gone, .enc + .bak exist.

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="feral.vault"):
            v2 = BlindVault(vault_path=str(legacy))

        # Second boot must NOT emit the migration line.
        assert not [
            r for r in caplog.records
            if "vault.migrated_to_encrypted" in r.getMessage()
        ]
        assert v2.get_credential("X") == "test-key-do-not-commit"
