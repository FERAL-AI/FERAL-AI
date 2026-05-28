"""v2026.5.43 W-B — ``feral memory encrypt`` regression tests.

Covers:
  * round-trip: encrypt → decrypt-on-boot → episodes still searchable
  * AEAD tamper detection: a single byte flip in ``memory.db.enc`` must
    raise :class:`memory.at_rest.MemoryTamperedError`
  * brain-running guard: CLI refuses to encrypt while the brain is up
  * doctor probe: ``feral doctor`` surfaces ``memory at-rest
    encryption: enabled`` when the .enc is present and the vault
    unlocks
  * doctor probe: ``feral doctor`` fails loud when the .enc is present
    but the vault cannot be unlocked
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import security.vault as vault_mod
from security.vault import BlindVault


# ─────────────────────────────────────────────────────────────────────
# Fixtures — mirror tests/test_vault_encryption.py so this test file
# is self-contained and can be invoked with just
# ``pytest tests/test_memory_encrypt.py``.
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_keychain(monkeypatch):
    """Replace the OS keychain wrapper with an in-memory dict so the
    tests never touch the real macOS Keychain / Linux Secret
    Service."""
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
def feral_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)
    # ``feral_data_home`` is XDG-driven, not FERAL_HOME-driven — point
    # it at the same tmp_path so the doctor probe finds the artefacts
    # we just wrote there.
    import config.loader as _cl
    monkeypatch.setattr(_cl, "feral_data_home", lambda: tmp_path)
    vault_mod.reset_vault()
    return tmp_path


@pytest.fixture
def seeded_vault(feral_home, fake_keychain) -> BlindVault:
    """Construct a BlindVault under ``$FERAL_HOME`` so its
    ``_master_key()`` resolves through the in-memory keychain."""
    return BlindVault(vault_path=str(feral_home / "credentials.json"))


# ─────────────────────────────────────────────────────────────────────
# (1) Round-trip — encrypt, decrypt-on-boot, episodes still searchable
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_encrypt_round_trip_returns_searchable_episodes(
    seeded_vault: BlindVault, feral_home: Path,
):
    from memory.at_rest import (
        encrypt_memory_db,
        ensure_plaintext_db,
        encryption_status,
    )
    from memory.store import MemoryStore

    db_path = feral_home / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    try:
        await store.episode_save(
            session_id="t-session",
            event_type="note",
            summary="canary episode — searchable after encrypt round-trip",
            detail="this row must come back through episode_search after "
            "decrypt-on-boot",
            importance=0.9,
        )
    finally:
        await store.aclose()

    # Force the master key to materialise so the keychain has it; the
    # vault generates it on first persist, which we trigger with a
    # one-off write.
    seeded_vault.set_credential("__bootstrap__", "x")
    seeded_vault.remove("__bootstrap__")

    result = encrypt_memory_db(
        vault=seeded_vault,
        db_path=db_path,
        shred_plaintext=True,
    )
    enc_path = db_path.with_name(db_path.name + ".enc")
    assert enc_path.exists(), "ciphertext file was not created"
    assert not db_path.exists(), (
        "plaintext memory.db should be gone after shred"
    )
    assert result["plaintext_removed"] is True
    assert result["backup_path"] is None
    assert result["ciphertext_path"] == str(enc_path)

    status = encryption_status(db_path)
    assert status["encrypted_at_rest"] is True
    assert status["plaintext_present"] is False

    ensure_plaintext_db(vault=seeded_vault, db_path=db_path)
    assert db_path.exists(), "decrypt-on-boot did not restore memory.db"

    store2 = MemoryStore(db_path=str(db_path))
    try:
        hits = await store2.episode_search(query="canary", limit=10)
        assert any(
            "canary" in (row.get("summary") or "") for row in hits
        ), f"expected the canary episode in {hits!r}"
    finally:
        await store2.aclose()


# ─────────────────────────────────────────────────────────────────────
# (2) Tamper detection
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tampered_ciphertext_raises_memory_tampered_error(
    seeded_vault: BlindVault, feral_home: Path,
):
    from memory.at_rest import (
        encrypt_memory_db,
        ensure_plaintext_db,
        MemoryTamperedError,
    )
    from memory.store import MemoryStore

    db_path = feral_home / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    try:
        await store.episode_save(
            session_id="t", event_type="note",
            summary="tamper-test row", detail="", importance=0.5,
        )
    finally:
        await store.aclose()

    seeded_vault.set_credential("__b__", "x")
    seeded_vault.remove("__b__")

    encrypt_memory_db(vault=seeded_vault, db_path=db_path, shred_plaintext=True)
    enc_path = db_path.with_name(db_path.name + ".enc")

    raw = bytearray(enc_path.read_bytes())
    # Flip a byte well inside the ciphertext body (past the 12-byte
    # nonce) so the AEAD tag check is what catches us.
    raw[len(raw) // 2] ^= 0x01
    enc_path.write_bytes(bytes(raw))

    with pytest.raises(MemoryTamperedError):
        ensure_plaintext_db(vault=seeded_vault, db_path=db_path)


# ─────────────────────────────────────────────────────────────────────
# (3) Brain-running guard
# ─────────────────────────────────────────────────────────────────────


def test_encrypt_refused_when_brain_running(
    seeded_vault: BlindVault, feral_home: Path, monkeypatch, capsys,
):
    from cli import memory_cmd

    # Pretend the brain's /health endpoint returns 200.
    monkeypatch.setattr(memory_cmd, "_brain_health_ok", lambda timeout=1.0: True)

    # Need a memory.db on disk so the encrypt path doesn't trip on
    # "no plaintext db" before it ever reaches the health probe.
    db_path = feral_home / "memory.db"
    db_path.write_bytes(b"SQLite stub")

    class _Flags:
        force = False
        no_shred = False

    with pytest.raises(SystemExit) as exc_info:
        memory_cmd.cmd_memory("encrypt", None, flags=_Flags())
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "stop the brain first" in captured.out.lower()


# ─────────────────────────────────────────────────────────────────────
# (4) Doctor — happy path renders the enabled row
# ─────────────────────────────────────────────────────────────────────


def test_doctor_reports_encrypted_at_rest_when_flag_set(
    seeded_vault: BlindVault, feral_home: Path, monkeypatch, capsys,
):
    import asyncio
    from memory.at_rest import encrypt_memory_db
    from memory.store import MemoryStore

    db_path = feral_home / "memory.db"

    async def _seed():
        store = MemoryStore(db_path=str(db_path))
        try:
            await store.episode_save(
                session_id="d", event_type="note",
                summary="doctor-happy", detail="", importance=0.5,
            )
        finally:
            await store.aclose()

    asyncio.run(_seed())

    seeded_vault.set_credential("__b__", "x")
    seeded_vault.remove("__b__")
    encrypt_memory_db(vault=seeded_vault, db_path=db_path, shred_plaintext=True)

    settings = feral_home / "settings.json"
    settings.write_text(json.dumps({"memory": {"encrypted_at_rest": True}}))

    enc_path = db_path.with_name(db_path.name + ".enc")
    assert enc_path.exists()

    from cli.main import cmd_doctor

    # Best-effort: cmd_doctor runs a LOT of probes (network/CDP/etc),
    # most of which are irrelevant. We just need to confirm the
    # encryption row renders. We swallow SystemExit so a non-zero
    # status from unrelated probes doesn't fail this test.
    try:
        cmd_doctor()
    except SystemExit:
        pass

    captured = capsys.readouterr()
    haystack = (captured.out + captured.err).lower()
    assert "memory at-rest encryption" in haystack
    assert "enabled" in haystack


# ─────────────────────────────────────────────────────────────────────
# (5) Doctor — vault-broken path fails loud
# ─────────────────────────────────────────────────────────────────────


def test_doctor_fails_loud_when_enc_present_but_vault_locked(
    seeded_vault: BlindVault, feral_home: Path, fake_keychain,
    monkeypatch, capsys,
):
    import asyncio
    from memory.at_rest import encrypt_memory_db
    from memory.store import MemoryStore

    db_path = feral_home / "memory.db"

    async def _seed():
        store = MemoryStore(db_path=str(db_path))
        try:
            await store.episode_save(
                session_id="d", event_type="note",
                summary="doctor-broken-vault", detail="", importance=0.5,
            )
        finally:
            await store.aclose()

    asyncio.run(_seed())

    seeded_vault.set_credential("__b__", "x")
    seeded_vault.remove("__b__")
    encrypt_memory_db(vault=seeded_vault, db_path=db_path, shred_plaintext=True)

    # Wipe the keychain + reset the vault singleton, then disable the
    # recovery-env fallback so _master_key() must fail.
    fake_keychain.clear()
    vault_mod.reset_vault()
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)

    from cli.main import cmd_doctor

    try:
        cmd_doctor()
    except SystemExit:
        pass

    captured = capsys.readouterr()
    haystack = (captured.out + captured.err).lower()
    # We want both: the row label rendered AND the actionable fix line.
    assert "memory at-rest encryption" in haystack
    assert "vault" in haystack
