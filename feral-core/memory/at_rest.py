"""FERAL Memory — at-rest encryption envelope.

Whole-file AEAD encryption of ``~/.feral/memory.db`` while the brain is
stopped. The plaintext SQLite file is required at runtime (FTS5
virtual tables, sqlite-vec, embedding BLOBs, sync WAL materialisation
all need real DB pages), so the envelope is a one-shot operator
action: encrypt → brain offline → decrypt on next boot.

Crypto stack
------------
ChaCha20-Poly1305 with a 12-byte random nonce. The subkey is HKDF-
SHA256 derived from the vault master key so credentials.enc and
memory.db.enc share no key material directly — compromising one
ciphertext doesn't help an attacker recover the other.

  master_key (32B) ──HKDF-SHA256(info=b"feral-memory-v1")──▶ subkey (32B)
  payload  = nonce(12) || ChaCha20Poly1305(subkey).encrypt(
                            nonce, db_bytes, _AEAD_AAD)

On-disk artefacts
-----------------
  ~/.feral/memory.db                — plaintext SQLite (present while
                                       brain runs)
  ~/.feral/memory.db.enc            — authoritative ciphertext after
                                       ``feral memory encrypt``
  ~/.feral/memory.db.bak.plaintext  — chmod 0600 backup retained until
                                       operator removes it manually

Atomicity
---------
``.enc.new`` is written + fsynced, then ``os.replace`` swaps it into
place. The plaintext is renamed to ``.bak.plaintext`` only AFTER the
ciphertext is verified by a decrypt round-trip + ``PRAGMA
integrity_check`` on a temp file. On any exception after ``.enc.new``
is written, the temp file is unlinked and the original ``memory.db``
is left untouched.

Failure modes
-------------
``MemoryTamperedError`` is raised by :func:`decrypt_memory_db` and
:func:`ensure_plaintext_db` when AEAD verification fails — either the
ciphertext is tampered with or the master key in the OS keychain does
not match this file.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral.memory.at_rest")


# Domain separator so the memory subkey can never collide with the
# vault credentials ciphertext key, even if both files were encrypted
# under the same master.
_AEAD_AAD = b"feral-memory-v1"
_MEMORY_VERSION = 1
_HKDF_INFO = b"feral-memory-v1"
_NONCE_LEN = 12
_TAG_LEN = 16


class MemoryTamperedError(Exception):
    """Raised when the memory ciphertext fails AEAD verification."""


# ─────────────────────────────────────────────────────────────────────
# Lazy crypto imports (mirrors security.vault._cryptography pattern so
# ``import memory.at_rest`` stays sub-millisecond on a cold venv).
# ─────────────────────────────────────────────────────────────────────


def _cryptography():
    cached = getattr(_cryptography, "_cached", None)
    if cached is not None:
        return cached
    from cryptography.exceptions import InvalidTag as _IT
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305 as _CC
    _cryptography._cached = (_CC, _IT)  # type: ignore[attr-defined]
    return _cryptography._cached  # type: ignore[attr-defined]


def _hkdf_subkey(master: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 derive a domain-separated subkey from ``master``.

    Salt is left empty (RFC 5869 §3.1 — fine when the IKM is already
    high-entropy; the master key is a 32-byte random key from
    ``ChaCha20Poly1305.generate_key()``).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if not isinstance(master, (bytes, bytearray)) or len(master) < 16:
        raise ValueError(
            f"master key must be >=16 bytes of high-entropy material; "
            f"got {len(master) if isinstance(master, (bytes, bytearray)) else type(master).__name__}"
        )
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info)
    return hkdf.derive(bytes(master))


# ─────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────


def _enc_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".enc")


def _backup_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".bak.plaintext")


# ─────────────────────────────────────────────────────────────────────
# WAL checkpoint
# ─────────────────────────────────────────────────────────────────────


def _wal_checkpoint(db_path: Path) -> None:
    """Force a TRUNCATE checkpoint so the on-disk ``memory.db`` reflects
    every page that lives in ``memory.db-wal``.

    If the brain (or another process) holds the DB open this raises
    :class:`sqlite3.OperationalError`; the CLI gates ``feral memory
    encrypt`` on the brain not running so that path is the expected
    failure mode for "user tried to encrypt while serve is alive".
    """
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# Master-key resolver
# ─────────────────────────────────────────────────────────────────────


def _resolve_master_key(vault) -> bytes:
    """Pull the 32-byte master key out of a :class:`BlindVault`.

    The vault doesn't expose a public accessor (it's deliberately
    private to keep the credentials encryption surface minimal). We
    reach for ``_master_key()`` and treat any failure as a clear
    "vault could not be unlocked" error so the CLI can render an
    actionable message.
    """
    resolver = getattr(vault, "_master_key", None)
    if resolver is None or not callable(resolver):
        raise RuntimeError(
            "Vault object has no _master_key() resolver — incompatible "
            "vault implementation passed to memory.at_rest."
        )
    key = resolver()
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise RuntimeError(
            f"Vault master key is {len(key) if isinstance(key, (bytes, bytearray)) else '?'} "
            f"bytes; expected 32."
        )
    return bytes(key)


# ─────────────────────────────────────────────────────────────────────
# AEAD encrypt / decrypt of arbitrary bytes
# ─────────────────────────────────────────────────────────────────────


def _encrypt_bytes(master_key: bytes, plaintext: bytes) -> bytes:
    ChaCha20Poly1305, _ = _cryptography()
    subkey = _hkdf_subkey(master_key, _HKDF_INFO, length=32)
    nonce = os.urandom(_NONCE_LEN)
    ct = ChaCha20Poly1305(subkey).encrypt(nonce, plaintext, _AEAD_AAD)
    return nonce + ct


def _decrypt_bytes(master_key: bytes, raw: bytes) -> bytes:
    if len(raw) < _NONCE_LEN + _TAG_LEN:
        raise MemoryTamperedError(
            f"memory ciphertext is too short to contain a "
            f"ChaCha20-Poly1305 envelope ({len(raw)} bytes)."
        )
    ChaCha20Poly1305, InvalidTag = _cryptography()
    subkey = _hkdf_subkey(master_key, _HKDF_INFO, length=32)
    nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    try:
        return ChaCha20Poly1305(subkey).decrypt(nonce, ct, _AEAD_AAD)
    except InvalidTag as exc:
        raise MemoryTamperedError(
            "AEAD verification failed for memory ciphertext. Either the "
            "file is tampered with, OR the master key in the OS keychain "
            "does not match this file. Run `feral key recover` and "
            "supply the recovery code printed at first boot."
        ) from exc


# ─────────────────────────────────────────────────────────────────────
# Atomic write
# ─────────────────────────────────────────────────────────────────────


def _atomic_write_0600(path: Path, payload: bytes) -> None:
    """Stage to ``.new``, fsync, ``os.replace``, chmod 0600.

    On exception after ``.new`` is created, the staged file is
    unlinked so we never leave a dangling ``.enc.new`` behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("memory.at_rest.chmod_failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────
# Verification round-trip on a temp file
# ─────────────────────────────────────────────────────────────────────


def _verify_round_trip(master_key: bytes, ciphertext_path: Path) -> None:
    """Decrypt ``ciphertext_path`` to a temp file and run
    ``PRAGMA integrity_check``. Raises on any failure.

    The temp file is unlinked on success AND failure — the caller's
    plaintext backup is the durable copy.
    """
    raw = ciphertext_path.read_bytes()
    plaintext = _decrypt_bytes(master_key, raw)
    fd, tmp_name = tempfile.mkstemp(prefix=".feral-memdec-", suffix=".db")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(plaintext)
            f.flush()
            os.fsync(f.fileno())
        conn = sqlite3.connect(str(tmp_path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if not row or row[0] != "ok":
            raise MemoryTamperedError(
                f"Decrypted memory DB failed integrity_check: "
                f"{row[0] if row else '<no result>'}"
            )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def encrypt_memory_db(
    *,
    vault,
    db_path: Optional[Path] = None,
    shred_plaintext: bool = True,
    force: bool = False,
) -> dict:
    """Encrypt ``memory.db`` in place to ``memory.db.enc``.

    Workflow:

    1. WAL-checkpoint the live DB so ``memory.db`` reflects every
       page.
    2. Read the contiguous DB bytes.
    3. Derive HKDF subkey from the vault master key.
    4. AEAD-encrypt with a fresh 12-byte nonce.
    5. Atomically write ``memory.db.enc.new`` → fsync → ``os.replace``
       → chmod 0600.
    6. Decrypt round-trip + ``PRAGMA integrity_check`` on a temp file
       — refuses to remove the plaintext until this succeeds.
    7. Rename ``memory.db`` → ``memory.db.bak.plaintext`` (chmod 0600).
    8. If ``shred_plaintext`` and the backup is verified, unlink the
       backup.

    Returns ``{ciphertext_path, bytes, backup_path | None, plaintext_removed}``.
    """
    if db_path is None:
        from config.loader import feral_data_home
        db_path = feral_data_home() / "memory.db"
    else:
        db_path = Path(db_path)

    enc_path = _enc_path(db_path)
    backup_path = _backup_path(db_path)

    if enc_path.exists() and not force:
        raise FileExistsError(
            f"{enc_path} already exists. Re-run with --force to overwrite "
            f"the existing ciphertext (the previous .enc will be lost)."
        )

    if not db_path.exists():
        raise FileNotFoundError(
            f"No plaintext memory database at {db_path}. Run the brain "
            f"once to create it, or restore from backup before "
            f"encrypting."
        )

    master_key = _resolve_master_key(vault)

    _wal_checkpoint(db_path)

    plaintext = db_path.read_bytes()
    payload = _encrypt_bytes(master_key, plaintext)

    _atomic_write_0600(enc_path, payload)

    _verify_round_trip(master_key, enc_path)

    if backup_path.exists():
        try:
            backup_path.unlink()
        except OSError as exc:
            logger.warning("memory.at_rest.old_backup_unlink_failed: %s", exc)
    os.rename(db_path, backup_path)
    try:
        os.chmod(backup_path, 0o600)
    except OSError as exc:
        logger.warning("memory.at_rest.backup_chmod_failed: %s", exc)

    for sidecar in (".db-wal", ".db-shm"):
        side = db_path.with_name(db_path.name.replace(".db", sidecar))
        if side.exists():
            try:
                side.unlink()
            except OSError as exc:
                logger.warning(
                    "memory.at_rest.sidecar_unlink_failed: %s", exc
                )

    plaintext_removed = False
    if shred_plaintext:
        try:
            backup_path.unlink()
            plaintext_removed = True
        except OSError as exc:
            logger.warning(
                "memory.at_rest.shred_failed: %s — backup retained at %s",
                exc, backup_path,
            )

    return {
        "ciphertext_path": str(enc_path),
        "bytes": len(payload),
        "backup_path": None if plaintext_removed else str(backup_path),
        "plaintext_removed": plaintext_removed,
    }


def decrypt_memory_db(
    *,
    vault,
    db_path: Optional[Path] = None,
) -> Path:
    """Decrypt ``memory.db.enc`` to ``memory.db`` (atomically).

    Returns the path of the plaintext DB. Raises
    :class:`MemoryTamperedError` if AEAD verification fails or the
    decrypted DB does not pass ``PRAGMA integrity_check``.
    """
    if db_path is None:
        from config.loader import feral_data_home
        db_path = feral_data_home() / "memory.db"
    else:
        db_path = Path(db_path)

    enc_path = _enc_path(db_path)
    if not enc_path.exists():
        raise FileNotFoundError(
            f"No encrypted memory database at {enc_path}; nothing to "
            f"decrypt."
        )

    master_key = _resolve_master_key(vault)
    raw = enc_path.read_bytes()
    plaintext = _decrypt_bytes(master_key, raw)

    tmp = db_path.with_name(db_path.name + ".dec.new")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(tmp, "wb") as f:
            f.write(plaintext)
            f.flush()
            os.fsync(f.fileno())
        conn = sqlite3.connect(str(tmp))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if not row or row[0] != "ok":
            raise MemoryTamperedError(
                f"Decrypted memory DB at {tmp} failed integrity_check: "
                f"{row[0] if row else '<no result>'}"
            )
        os.replace(tmp, db_path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(db_path, 0o600)
    except OSError as exc:
        logger.warning("memory.at_rest.plaintext_chmod_failed: %s", exc)
    return db_path


def ensure_plaintext_db(
    *,
    vault,
    db_path: Path,
) -> Path:
    """Boot-path helper: make sure ``db_path`` exists as plaintext.

    Three cases:

    * ``memory.db`` exists → no-op, return path.
    * ``memory.db`` absent + ``memory.db.enc`` exists → decrypt then
      return path.
    * Both absent → no-op, return path (MemoryStore will create a
      fresh empty DB).

    When BOTH ``memory.db`` and ``memory.db.enc`` exist the plaintext
    wins (operator can run ``feral memory encrypt --force`` to rotate),
    and a warning is logged.
    """
    db_path = Path(db_path)
    enc_path = _enc_path(db_path)

    if db_path.exists():
        if enc_path.exists():
            logger.warning(
                "memory.at_rest.both_files_present: %s and %s both exist. "
                "Using the plaintext file; run `feral memory encrypt "
                "--force` to re-encrypt and discard the .enc.",
                db_path, enc_path,
            )
        return db_path

    if enc_path.exists():
        logger.info(
            "memory.at_rest.decrypting_on_boot: %s → %s",
            enc_path, db_path,
        )
        decrypt_memory_db(vault=vault, db_path=db_path)
        return db_path

    return db_path


def encryption_status(db_path: Path) -> dict:
    """Return a snapshot of memory-at-rest state for ``feral memory
    status`` / ``feral doctor`` consumers.

    Does NOT require the vault — purely a file-existence check.
    """
    db_path = Path(db_path)
    enc_path = _enc_path(db_path)
    backup_path = _backup_path(db_path)
    return {
        "encrypted_at_rest": enc_path.exists(),
        "ciphertext_path": str(enc_path) if enc_path.exists() else None,
        "plaintext_path": str(db_path),
        "plaintext_present": db_path.exists(),
        "backup_path": str(backup_path) if backup_path.exists() else None,
    }
