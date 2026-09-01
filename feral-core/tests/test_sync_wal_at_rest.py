"""``sync_wal.db`` must live inside the at-rest envelope.

Two separate audits found ``~/.feral/sync_wal.db`` sitting OUTSIDE the
encryption envelope that ``feral memory encrypt`` advertises:

* ``memory/at_rest.py`` encrypted ``memory.db`` only. It cleaned up the
  ``memory.db-wal`` / ``memory.db-shm`` sidecars and never looked at
  ``sync_wal.db``.
* On a live install the file was mode ``-rw-r--r--`` (world readable),
  12 MB, holding 6.24 MB of plaintext JSON row payloads — note bodies
  and episode text, readable with a read-only sqlite connection by any
  local user.

So after ``feral memory encrypt`` the ciphertext sat next to a
world-readable plaintext copy of the same memories.

These tests pin both halves of the fix:

  1. the plaintext WAL is chmod 0600 the moment it is opened, and
  2. ``feral memory encrypt`` leaves no readable plaintext of a secret
     that was written through the store, while the sync state itself
     survives the round trip.
"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

import security.vault as vault_mod
from security.vault import BlindVault

# A string that only ever enters the store through a note body. If it
# shows up in bytes on disk after encryption, the envelope has a hole.
CANARY = "vermilion-ptarmigan-42-SECRET-PAYLOAD"


# ─────────────────────────────────────────────────────────────────────
# Fixtures — mirror tests/test_memory_encrypt.py so this file can be
# invoked on its own.
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_keychain(monkeypatch):
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        vault_mod, "_keyring_get_password",
        lambda service, username: store.get((service, username)),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_set_password",
        lambda service, username, password: store.__setitem__(
            (service, username), password
        ),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_delete_password",
        lambda service, username: store.pop((service, username), None),
    )
    return store


@pytest.fixture
def feral_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)
    import config.loader as _cl
    monkeypatch.setattr(_cl, "feral_data_home", lambda: tmp_path)
    vault_mod.reset_vault()
    return tmp_path


@pytest.fixture
def seeded_vault(feral_home, fake_keychain) -> BlindVault:
    vault = BlindVault(vault_path=str(feral_home / "credentials.json"))
    # Force the master key to materialise in the fake keychain.
    vault.set_credential("__bootstrap__", "x")
    vault.remove("__bootstrap__")
    return vault


def _bytes_on_disk(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _files_holding(directory: Path, needle: bytes) -> list[str]:
    """Every file under ``directory`` whose raw bytes contain ``needle``."""
    hits = []
    for candidate in sorted(directory.rglob("*")):
        if not candidate.is_file():
            continue
        try:
            if needle in candidate.read_bytes():
                hits.append(candidate.name)
        except OSError:
            continue
    return hits


# ─────────────────────────────────────────────────────────────────────
# (1) File mode
# ─────────────────────────────────────────────────────────────────────


def test_sync_wal_is_created_mode_0600(tmp_path: Path):
    """0644 on a file of memory contents is wrong regardless of
    encryption. 0600 is what the rest of the repo applies (see
    ``security/device_pairing.py`` and the SDK node-key writers)."""
    from memory.sync import SyncWAL

    wal_path = tmp_path / "sync_wal.db"
    SyncWAL(str(wal_path))

    mode = stat.S_IMODE(wal_path.stat().st_mode)
    assert mode == 0o600, (
        f"sync_wal.db is mode {mode:04o}; it holds plaintext row payloads "
        f"and must be 0600"
    )


def test_sync_wal_mode_is_repaired_on_an_existing_install(tmp_path: Path):
    """An install that predates the fix already has a 0644 file on
    disk. Opening it must repair the mode, not just get new files
    right."""
    from memory.sync import SyncWAL

    wal_path = tmp_path / "sync_wal.db"
    conn = sqlite3.connect(str(wal_path))
    conn.close()
    wal_path.chmod(0o644)

    SyncWAL(str(wal_path))

    assert stat.S_IMODE(wal_path.stat().st_mode) == 0o600


# ─────────────────────────────────────────────────────────────────────
# (2) No plaintext survives ``feral memory encrypt``
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_encrypt_leaves_no_plaintext_wal_payload_on_disk(
    seeded_vault: BlindVault, feral_home: Path,
):
    from memory.at_rest import encrypt_memory_db, encrypt_sync_wal_db
    from memory.store import MemoryStore
    from memory.sync import SyncEngine

    db_path = feral_home / "memory.db"
    wal_path = feral_home / "sync_wal.db"

    store = MemoryStore(db_path=str(db_path))
    engine = SyncEngine("node-under-test", memory_store=store,
                        db_path=str(wal_path))
    store._sync_engine = engine
    try:
        await store.save(f"the note body carries {CANARY}", tags=["audit"])
    finally:
        await store.drain_background_tasks()
        await store.aclose()

    # Pre-condition: the WAL really does hold the payload in plaintext
    # while the brain is running. If this ever stops being true the
    # test below is vacuous.
    assert CANARY.encode() in _bytes_on_disk(wal_path), (
        "fixture is not exercising the WAL row payload path"
    )

    encrypt_memory_db(vault=seeded_vault, db_path=db_path,
                      shred_plaintext=True)
    encrypt_sync_wal_db(vault=seeded_vault, wal_path=wal_path,
                        shred_plaintext=True)

    leaking = _files_holding(feral_home, CANARY.encode())
    assert leaking == [], (
        f"plaintext memory contents readable after encrypt, in: {leaking}"
    )
    assert (feral_home / "sync_wal.db.enc").exists()


# ─────────────────────────────────────────────────────────────────────
# (3) Migration — an existing install must not lose sync state
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_encrypted_wal_round_trips_without_losing_operations(
    seeded_vault: BlindVault, feral_home: Path,
):
    from memory.at_rest import encrypt_sync_wal_db, ensure_plaintext_sync_wal
    from memory.store import MemoryStore
    from memory.sync import SyncEngine

    db_path = feral_home / "memory.db"
    wal_path = feral_home / "sync_wal.db"

    store = MemoryStore(db_path=str(db_path))
    engine = SyncEngine("node-under-test", memory_store=store,
                        db_path=str(wal_path))
    store._sync_engine = engine
    try:
        await store.save(f"round trip {CANARY}", tags=["audit"])
        before = engine._wal.count
    finally:
        await store.drain_background_tasks()
        await store.aclose()
    assert before > 0

    encrypt_sync_wal_db(vault=seeded_vault, wal_path=wal_path,
                        shred_plaintext=True)
    assert not wal_path.exists()

    ensure_plaintext_sync_wal(vault=seeded_vault, wal_path=wal_path)
    assert wal_path.exists(), "decrypt-on-boot did not restore sync_wal.db"
    assert stat.S_IMODE(wal_path.stat().st_mode) == 0o600

    conn = sqlite3.connect(str(wal_path))
    try:
        after = conn.execute("SELECT COUNT(*) FROM sync_wal").fetchone()[0]
        payloads = " ".join(
            row[0] for row in conn.execute("SELECT data FROM sync_wal")
        )
    finally:
        conn.close()
    assert after == before, "sync operations were lost across the envelope"
    assert CANARY in payloads


@pytest.mark.asyncio
async def test_sync_wal_decrypts_on_boot_through_the_wal_constructor(
    seeded_vault: BlindVault, feral_home: Path,
):
    """The boot path has to work the way ``memory.db``'s does: opening
    the WAL with only a ``.enc`` on disk must transparently restore
    it."""
    from memory.at_rest import encrypt_sync_wal_db
    from memory.sync import SyncWAL

    wal_path = feral_home / "sync_wal.db"
    SyncWAL(str(wal_path))
    conn = sqlite3.connect(str(wal_path))
    try:
        conn.execute(
            "INSERT INTO sync_wal (op_id, table_name, op_type, row_id, data,"
            " hlc, origin_node, timestamp) VALUES (?,?,?,?,?,?,?,?)",
            ("op-1", "notes", "insert", "row-1",
             f'{{"body": "{CANARY}"}}', "1:1:n", "node-a", 1.0),
        )
        conn.commit()
    finally:
        conn.close()

    encrypt_sync_wal_db(vault=seeded_vault, wal_path=wal_path,
                        shred_plaintext=True)
    assert not wal_path.exists()

    wal = SyncWAL(str(wal_path))
    assert wal.count == 1, "boot did not decrypt sync_wal.db.enc"
    assert stat.S_IMODE(wal_path.stat().st_mode) == 0o600


# ─────────────────────────────────────────────────────────────────────
# (4) ``feral memory encrypt`` runs both legs
# ─────────────────────────────────────────────────────────────────────


def test_cli_encrypt_covers_both_files(
    seeded_vault: BlindVault, feral_home: Path, monkeypatch, capsys,
):
    from cli import memory_cmd
    from memory.sync import SyncWAL

    monkeypatch.setattr(memory_cmd, "_brain_health_ok", lambda timeout=1.0: False)
    monkeypatch.setattr("security.vault.get_vault", lambda: seeded_vault)
    monkeypatch.setattr(memory_cmd, "_save_settings", lambda settings: None)

    db_path = feral_home / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.commit()
    conn.close()
    SyncWAL(str(feral_home / "sync_wal.db"))

    class _Flags:
        force = False
        no_shred = False

    memory_cmd.cmd_memory("encrypt", None, flags=_Flags())

    assert (feral_home / "memory.db.enc").exists()
    assert (feral_home / "sync_wal.db.enc").exists()
    assert not (feral_home / "sync_wal.db").exists()


def test_cli_encrypt_covers_a_wal_on_an_already_encrypted_install(
    seeded_vault: BlindVault, feral_home: Path, monkeypatch, capsys,
):
    """The install that predates this fix: memory.db.enc exists, the
    plaintext memory.db is long gone, and sync_wal.db is still
    readable. Re-running encrypt has to fix that rather than exiting on
    'no plaintext memory database'."""
    from cli import memory_cmd
    from memory.at_rest import encrypt_memory_db
    from memory.sync import SyncWAL

    monkeypatch.setattr(memory_cmd, "_brain_health_ok", lambda timeout=1.0: False)
    monkeypatch.setattr("security.vault.get_vault", lambda: seeded_vault)
    monkeypatch.setattr(memory_cmd, "_save_settings", lambda settings: None)

    db_path = feral_home / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.commit()
    conn.close()
    encrypt_memory_db(vault=seeded_vault, db_path=db_path,
                      shred_plaintext=True)
    SyncWAL(str(feral_home / "sync_wal.db"))

    class _Flags:
        force = False
        no_shred = False

    memory_cmd.cmd_memory("encrypt", None, flags=_Flags())

    out = capsys.readouterr().out
    assert "memory.db: already encrypted" in out
    assert (feral_home / "sync_wal.db.enc").exists()
    assert not (feral_home / "sync_wal.db").exists()


# ─────────────────────────────────────────────────────────────────────
# (5) Failure posture — key unavailable at boot
# ─────────────────────────────────────────────────────────────────────


def test_unreadable_ciphertext_does_not_block_boot_or_destroy_it(
    seeded_vault: BlindVault, feral_home: Path, monkeypatch,
):
    """Matches ``MemoryStore.__init__``'s posture for ``memory.db``:
    warn, carry on with an empty database, and never delete the
    ciphertext the operator may still recover."""
    from memory.at_rest import encrypt_sync_wal_db
    from memory.sync import SyncWAL

    wal_path = feral_home / "sync_wal.db"
    SyncWAL(str(wal_path))
    encrypt_sync_wal_db(vault=seeded_vault, wal_path=wal_path,
                        shred_plaintext=True)
    enc_path = feral_home / "sync_wal.db.enc"
    ciphertext_before = enc_path.read_bytes()

    # Vault cannot be unlocked at boot.
    import memory.at_rest as at_rest_mod

    def _boom(_vault):
        raise RuntimeError("vault locked")

    monkeypatch.setattr(at_rest_mod, "_resolve_master_key", _boom)

    wal = SyncWAL(str(wal_path))          # must NOT raise
    assert wal.count == 0
    assert enc_path.read_bytes() == ciphertext_before, (
        "the unreadable ciphertext must be preserved for recovery"
    )
