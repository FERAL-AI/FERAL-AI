"""FERAL Memory — at-rest encryption envelope.

Whole-file AEAD encryption of ``~/.feral/memory.db`` AND
``~/.feral/sync_wal.db`` while the brain is stopped. The plaintext
SQLite files are required at runtime (FTS5 virtual tables,
sqlite-vec, embedding BLOBs, sync WAL materialisation all need real DB
pages), so the envelope is a one-shot operator action: encrypt → brain
offline → decrypt on next boot.

Why the sync WAL is in here
---------------------------
``sync_wal.db`` stores the replication log as JSON row payloads, the
FULL note body / episode text of every local write, not just an id.
Until v2026.5.44 it sat outside this module entirely: two audits found
it at mode ``-rw-r--r--``, 12 MB on a live install, of which 6.24 MB
was plaintext memory content readable by any local user with a
read-only sqlite connection. So an operator who ran ``feral memory
encrypt`` ended up with ciphertext sitting next to a world-readable
plaintext copy of the same memories, and the feature did less than it
appeared to. Both files now go through the same envelope, and the
plaintext WAL is chmod 0600 every time it is opened.

Crypto stack
------------
ChaCha20-Poly1305 with a 12-byte random nonce. The subkey is HKDF-
SHA256 derived from the vault master key so credentials.enc and
memory.db.enc share no key material directly — compromising one
ciphertext doesn't help an attacker recover the other. The sync WAL
gets its own domain separator for the same reason, which also means a
``memory.db.enc`` can never be passed off as a ``sync_wal.db.enc``:
the AAD won't verify.

  master_key (32B) ──HKDF-SHA256(info=b"feral-memory-v1")──▶ subkey (32B)
  master_key (32B) ──HKDF-SHA256(info=b"feral-sync-wal-v1")▶ subkey (32B)
  payload  = nonce(12) || ChaCha20Poly1305(subkey).encrypt(
                            nonce, db_bytes, aad)

On-disk artefacts
-----------------
  ~/.feral/memory.db
      plaintext SQLite, present while the brain runs, chmod 0600
  ~/.feral/memory.db.enc
      authoritative ciphertext after ``feral memory encrypt``
  ~/.feral/memory.db.bak.plaintext
      chmod 0600 backup retained until the operator removes it
  ~/.feral/sync_wal.db
      plaintext replication log, present while the brain runs,
      chmod 0600
  ~/.feral/sync_wal.db.enc
      ciphertext of the same
  ~/.feral/sync_wal.db.bak.plaintext
      chmod 0600 backup

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

The boot path never propagates that. ``MemoryStore.__init__`` catches
it, warns, and carries on with whatever ``memory.db`` contains;
:func:`prepare_sync_wal_for_boot` takes the same position for the sync
WAL (see its docstring for why "warn and carry on" beats "refuse to
boot" here). In both cases the ``.enc`` is left untouched on disk so a
later ``feral key recover`` can still get the data back.
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
# Separate domain for sync_wal.db. Same master key, same cipher,
# different subkey + AAD, so the two ciphertexts can never be swapped
# for each other and a compromise of one buys nothing against the
# other.
_SYNC_WAL_AAD = b"feral-sync-wal-v1"
_SYNC_WAL_HKDF_INFO = b"feral-sync-wal-v1"
_SYNC_WAL_FILENAME = "sync_wal.db"
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


def _sidecar_paths(db_path: Path) -> tuple[Path, Path]:
    """The ``-wal`` / ``-shm`` files SQLite keeps beside ``db_path``.

    These hold committed-but-not-yet-checkpointed pages, i.e. the same
    plaintext rows as the main file, so anything that hardens or
    removes the DB has to cover them too.
    """
    return (
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    )


def sync_wal_path(db_path: Optional[Path] = None) -> Path:
    """Locate ``sync_wal.db``.

    Given a ``memory.db`` path, returns its sibling WAL (so a test that
    points the store at ``tmp_path`` gets the WAL from the same
    directory). With no argument, resolves through
    ``feral_data_home()`` exactly like ``SyncEngine`` does.
    """
    if db_path is None:
        from config.loader import feral_data_home
        return feral_data_home() / _SYNC_WAL_FILENAME
    return Path(db_path).with_name(_SYNC_WAL_FILENAME)


def harden_db_mode(db_path: Path) -> None:
    """chmod 0600 a plaintext DB and its SQLite sidecars.

    SQLite creates new files at 0666 & ~umask, which on a default
    install lands at 0644, world readable. Every other secret this
    repo writes is 0600 (``security/device_pairing.py``, the SDK
    node-key writers, the ``.enc`` files below), and a database of
    memory contents is no less sensitive than those. Applied on every
    open, not just creation, so installs that already have a 0644 file
    are repaired rather than left behind.

    Best-effort: a chmod failure (Windows, exotic filesystem, file
    owned by another user) is logged, never raised. Refusing to open
    the WAL over a chmod would be a worse outcome than a warning.
    """
    db_path = Path(db_path)
    for path in (db_path, *_sidecar_paths(db_path)):
        if not path.exists():
            continue
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning(
                "memory.at_rest.harden_mode_failed: %s (%s)", path, exc
            )


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


def _encrypt_bytes(
    master_key: bytes,
    plaintext: bytes,
    *,
    info: bytes = _HKDF_INFO,
    aad: bytes = _AEAD_AAD,
) -> bytes:
    ChaCha20Poly1305, _ = _cryptography()
    subkey = _hkdf_subkey(master_key, info, length=32)
    nonce = os.urandom(_NONCE_LEN)
    ct = ChaCha20Poly1305(subkey).encrypt(nonce, plaintext, aad)
    return nonce + ct


def _decrypt_bytes(
    master_key: bytes,
    raw: bytes,
    *,
    info: bytes = _HKDF_INFO,
    aad: bytes = _AEAD_AAD,
) -> bytes:
    if len(raw) < _NONCE_LEN + _TAG_LEN:
        raise MemoryTamperedError(
            f"memory ciphertext is too short to contain a "
            f"ChaCha20-Poly1305 envelope ({len(raw)} bytes)."
        )
    ChaCha20Poly1305, InvalidTag = _cryptography()
    subkey = _hkdf_subkey(master_key, info, length=32)
    nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    try:
        return ChaCha20Poly1305(subkey).decrypt(nonce, ct, aad)
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


def _verify_round_trip(
    master_key: bytes,
    ciphertext_path: Path,
    *,
    info: bytes = _HKDF_INFO,
    aad: bytes = _AEAD_AAD,
) -> None:
    """Decrypt ``ciphertext_path`` to a temp file and run
    ``PRAGMA integrity_check``. Raises on any failure.

    The temp file is unlinked on success AND failure — the caller's
    plaintext backup is the durable copy.
    """
    raw = ciphertext_path.read_bytes()
    plaintext = _decrypt_bytes(master_key, raw, info=info, aad=aad)
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


def _encrypt_db_file(
    *,
    vault,
    db_path: Path,
    shred_plaintext: bool,
    force: bool,
    info: bytes,
    aad: bytes,
) -> dict:
    """Shared body of :func:`encrypt_memory_db` and
    :func:`encrypt_sync_wal_db`, see the former for the step-by-step
    workflow. ``info`` / ``aad`` select the crypto domain so the two
    ciphertexts are not interchangeable.
    """
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
            f"No plaintext database at {db_path}. Run the brain "
            f"once to create it, or restore from backup before "
            f"encrypting."
        )

    master_key = _resolve_master_key(vault)

    _wal_checkpoint(db_path)

    plaintext = db_path.read_bytes()
    payload = _encrypt_bytes(master_key, plaintext, info=info, aad=aad)

    _atomic_write_0600(enc_path, payload)

    _verify_round_trip(master_key, enc_path, info=info, aad=aad)

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

    for side in _sidecar_paths(db_path):
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
                "memory.at_rest.shred_failed: %s - backup retained at %s",
                exc, backup_path,
            )

    return {
        "ciphertext_path": str(enc_path),
        "bytes": len(payload),
        "backup_path": None if plaintext_removed else str(backup_path),
        "plaintext_removed": plaintext_removed,
    }


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
      , refuses to remove the plaintext until this succeeds.
    7. Rename ``memory.db`` → ``memory.db.bak.plaintext`` (chmod 0600).
    8. If ``shred_plaintext`` and the backup is verified, unlink the
       backup.

    Returns ``{ciphertext_path, bytes, backup_path | None, plaintext_removed}``.

    Covers ``memory.db`` only. ``sync_wal.db`` is a separate file with
    its own crypto domain, see :func:`encrypt_sync_wal_db`, which
    ``feral memory encrypt`` runs as a second leg.
    """
    if db_path is None:
        from config.loader import feral_data_home
        db_path = feral_data_home() / "memory.db"
    return _encrypt_db_file(
        vault=vault,
        db_path=Path(db_path),
        shred_plaintext=shred_plaintext,
        force=force,
        info=_HKDF_INFO,
        aad=_AEAD_AAD,
    )


def encrypt_sync_wal_db(
    *,
    vault,
    wal_path: Optional[Path] = None,
    shred_plaintext: bool = True,
    force: bool = False,
) -> dict:
    """Encrypt ``sync_wal.db`` in place to ``sync_wal.db.enc``.

    Identical workflow, atomicity and verification to
    :func:`encrypt_memory_db`; only the HKDF info and the AEAD AAD
    differ.

    MIGRATION (existing installs)
    -----------------------------
    Every install that predates this function has a plaintext
    ``sync_wal.db``. The three options were: encrypt it in place, leave
    it alone, or discard it.

    We encrypt it in place. Discarding is the only option that loses
    data, the WAL is the replication log, and dropping it means peers
    silently never receive the operations that were pending in it, with
    no error anywhere to explain the gap. Leaving it alone is what the
    bug already did: the operator runs ``feral memory encrypt``, is told
    they are encrypted, and a plaintext copy of the same memories stays
    on disk. Encrypting in place preserves every operation and makes the
    claim true.

    The conversion is not silent and not implicit: it happens when the
    operator runs ``feral memory encrypt`` (brain stopped), the same
    action that encrypts ``memory.db``, and the plaintext is renamed to
    ``sync_wal.db.bak.plaintext`` only after a decrypt + integrity_check
    round trip on the ciphertext succeeds. With ``shred_plaintext``
    false that backup is left in place, chmod 0600, for the operator to
    delete once satisfied.

    An install that was ALREADY encrypted before this change has a
    ``memory.db.enc`` and a plaintext ``sync_wal.db``. Re-running
    ``feral memory encrypt`` covers it: the CLI skips the memory leg
    (nothing to encrypt) and runs this one.
    """
    if wal_path is None:
        wal_path = sync_wal_path()
    return _encrypt_db_file(
        vault=vault,
        db_path=Path(wal_path),
        shred_plaintext=shred_plaintext,
        force=force,
        info=_SYNC_WAL_HKDF_INFO,
        aad=_SYNC_WAL_AAD,
    )


def _decrypt_db_file(
    *,
    vault,
    db_path: Path,
    info: bytes,
    aad: bytes,
) -> Path:
    """Shared body of :func:`decrypt_memory_db` and
    :func:`decrypt_sync_wal_db`."""
    db_path = Path(db_path)
    enc_path = _enc_path(db_path)
    if not enc_path.exists():
        raise FileNotFoundError(
            f"No encrypted database at {enc_path}; nothing to "
            f"decrypt."
        )

    master_key = _resolve_master_key(vault)
    raw = enc_path.read_bytes()
    plaintext = _decrypt_bytes(master_key, raw, info=info, aad=aad)

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
    harden_db_mode(db_path)
    return db_path


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
    return _decrypt_db_file(
        vault=vault,
        db_path=Path(db_path),
        info=_HKDF_INFO,
        aad=_AEAD_AAD,
    )


def decrypt_sync_wal_db(
    *,
    vault,
    wal_path: Optional[Path] = None,
) -> Path:
    """Decrypt ``sync_wal.db.enc`` to ``sync_wal.db`` (atomically).

    Same contract as :func:`decrypt_memory_db`, different crypto
    domain.
    """
    if wal_path is None:
        wal_path = sync_wal_path()
    return _decrypt_db_file(
        vault=vault,
        db_path=Path(wal_path),
        info=_SYNC_WAL_HKDF_INFO,
        aad=_SYNC_WAL_AAD,
    )


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


def ensure_plaintext_sync_wal(
    *,
    vault,
    wal_path: Optional[Path] = None,
) -> Path:
    """Boot-path helper for ``sync_wal.db``. Mirrors
    :func:`ensure_plaintext_db` case for case.

    Raises on a failed decrypt. Callers on the brain boot path should
    use :func:`prepare_sync_wal_for_boot`, which owns the failure
    posture.
    """
    if wal_path is None:
        wal_path = sync_wal_path()
    wal_path = Path(wal_path)
    enc_path = _enc_path(wal_path)

    if wal_path.exists():
        if enc_path.exists():
            logger.warning(
                "memory.at_rest.both_wal_files_present: %s and %s both "
                "exist. Using the plaintext file; run `feral memory "
                "encrypt --force` to re-encrypt and discard the .enc.",
                wal_path, enc_path,
            )
        return wal_path

    if enc_path.exists():
        logger.info(
            "memory.at_rest.decrypting_sync_wal_on_boot: %s -> %s",
            enc_path, wal_path,
        )
        decrypt_sync_wal_db(vault=vault, wal_path=wal_path)
        return wal_path

    return wal_path


def prepare_sync_wal_for_boot(wal_path: Path) -> Path:
    """Everything ``SyncWAL`` needs done to its file before it opens
    it: decrypt a ``.enc`` if that is all there is, and chmod 0600
    whatever ends up on disk.

    Never raises. FAILURE POSTURE, and why it matches
    ``MemoryStore.__init__``'s for ``memory.db``:

    * The vault is only touched when a ``.enc`` exists and the
      plaintext does not, so a test or a fresh install never pays for
      keychain access.
    * If the key cannot be resolved (locked vault, keychain moved,
      wrong machine) we log at ERROR and return. ``SyncWAL`` then
      creates an empty WAL and the brain boots. Refusing to boot the
      whole brain because a replication log cannot be read would take
      the user's assistant offline over a subsystem that is optional
      by design, and ``MemoryStore`` already decided this question the
      same way for the much more important ``memory.db``.
    * The ciphertext is never deleted, moved or overwritten on this
      path. Whatever was in the WAL is still recoverable with
      ``feral key recover``; what is lost is replication of the ops
      that were pending, which peers reconcile from ``memory.db``
      anyway.
    * Because a fresh plaintext WAL then sits beside the ``.enc``,
      the next boot hits the both-files-present warning, and
      ``encryption_status`` reports ``sync_wal_plaintext_present``
      true. That is the signal that stops an operator believing they
      are fully encrypted while a plaintext file is on disk.
    """
    wal_path = Path(wal_path)
    enc_path = _enc_path(wal_path)

    if enc_path.exists() and wal_path.exists():
        # Both on disk. This is what a boot after a failed decrypt
        # looks like, and it is the state where an operator is most
        # likely to think they are encrypted while readable memory
        # content sits next to the ciphertext. Say so, every boot.
        logger.warning(
            "memory.at_rest.both_wal_files_present: %s and %s both exist. "
            "Using the plaintext file, which holds note and episode "
            "content in the clear. Run `feral memory encrypt --force` "
            "with the brain stopped to re-encrypt and discard the stale "
            ".enc.",
            wal_path, enc_path,
        )
    elif enc_path.exists():
        try:
            from security.vault import get_vault
            ensure_plaintext_sync_wal(vault=get_vault(), wal_path=wal_path)
        except Exception as exc:
            logger.error(
                "memory.at_rest.sync_wal_decrypt_failed: %s (%s). Starting "
                "with an EMPTY sync WAL; %s is untouched and still holds "
                "the previous replication log. Run `feral key recover` and "
                "restart to get it back.",
                wal_path, exc, enc_path,
            )
    return wal_path


def encryption_status(db_path: Path) -> dict:
    """Return a snapshot of memory-at-rest state for ``feral memory
    status`` / ``feral doctor`` consumers.

    Does NOT require the vault, purely a file-existence check.

    ``encrypted_at_rest`` stays scoped to ``memory.db`` so existing
    callers keep their meaning. The ``sync_wal_*`` keys are additive
    and exist so an operator can see the second half of the picture:
    a true ``encrypted_at_rest`` with a true
    ``sync_wal_plaintext_present`` means memory contents are still
    readable on disk through the replication log.
    """
    db_path = Path(db_path)
    enc_path = _enc_path(db_path)
    backup_path = _backup_path(db_path)
    wal_path = sync_wal_path(db_path)
    wal_enc_path = _enc_path(wal_path)
    wal_backup_path = _backup_path(wal_path)
    return {
        "encrypted_at_rest": enc_path.exists(),
        "ciphertext_path": str(enc_path) if enc_path.exists() else None,
        "plaintext_path": str(db_path),
        "plaintext_present": db_path.exists(),
        "backup_path": str(backup_path) if backup_path.exists() else None,
        "sync_wal_path": str(wal_path),
        "sync_wal_encrypted_at_rest": wal_enc_path.exists(),
        "sync_wal_ciphertext_path": (
            str(wal_enc_path) if wal_enc_path.exists() else None
        ),
        "sync_wal_plaintext_present": wal_path.exists(),
        "sync_wal_backup_path": (
            str(wal_backup_path) if wal_backup_path.exists() else None
        ),
    }
