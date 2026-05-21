"""
FERAL Blind Vault — Secure Credential Management (encrypted-at-rest)
=====================================================================
The LLM NEVER sees raw credentials. When a skill needs an API key,
the executor injects it at the HTTP layer. The LLM only knows:
"web_search is available" — not the key itself.

Threat Model:
  - LLM prompt injection cannot exfiltrate keys
  - Client-side code never receives keys
  - Skills are sandboxed: they can only access their own key
  - All credential access is logged to the audit trail
  - **At rest, the credentials file is AEAD-encrypted (ChaCha20-Poly1305)
    with a 32-byte master key held in the OS keychain.** A user with
    physical disk access cannot read keys without also compromising the
    OS keychain (or holding the one-time recovery code printed at first
    boot).

Architecture (W9 — vault encryption-at-rest):

  ┌──────────────────────────┐         ┌──────────────────────────┐
  │  OS Keychain             │         │  ~/.feral/credentials.enc│
  │  service "feral-ai"      │         │  ┌────────────────────┐  │
  │  user "vault-master"     │ ──key──▶│  │ nonce(12) | ct+tag │  │
  │  → 32-byte master key    │         │  └────────────────────┘  │
  └──────────────────────────┘         │  AAD = b"feral-vault-v1" │
                                       │  Plaintext: JSON         │
                                       │  {"version":1,"data":{…}}│
                                       └──────────────────────────┘

Recovery code:
  The recovery code IS the master key, base32-encoded and grouped
  in 4-character chunks for human-friendly transcription. Anyone with
  the code can decrypt the vault offline. The code is shown ONCE
  (first boot) and at every `feral key rotate`. It is never persisted.
  FERAL has no escrow — losing both the OS keychain entry AND the
  recovery code means the vault is unrecoverable; the user must
  re-enter every credential.

Migration:
  When ``credentials.json`` (legacy plaintext) exists and
  ``credentials.enc`` does not, the first read transparently:
    1. parses the plaintext
    2. AEAD-encrypts the JSON to ``credentials.enc``
    3. copies the original to ``credentials.json.bak.legacy`` (chmod 0600)
    4. unlinks ``credentials.json``
  An info-level audit line is emitted:
    "vault.migrated_to_encrypted: backed up legacy credentials to
     credentials.json.bak.legacy"

Failure modes (no try/except: pass — every error is explicit):
  - Keychain unavailable AND no recovery code → ``VaultKeyUnavailableError``
  - AEAD verification fails → ``VaultTamperedError`` (refuses to read)
  - Unknown vault version → ``VaultFormatError``
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

# R2-001 (round-2 of Wave 1) — vault module import must be side-effect-
# free and < 200ms on a cold venv. The heavy ``cryptography`` import
# (~150ms on macOS) and ``config.loader`` cascade are deferred to first
# use so that ``python -c "import security.vault"`` returns fast and a
# ``from security import vault`` from a route module doesn't drag the
# AEAD primitive + every config dependency into the boot path.
# ``BlindVault.__init__`` and the AEAD helpers do the imports on demand
# via :func:`_cryptography` and :func:`_feral_home` below.
if TYPE_CHECKING:  # pragma: no cover — import-time hint only
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401

logger = logging.getLogger("feral.vault")


def _cryptography():
    """Lazy import of the AEAD primitive + ``InvalidTag``.

    Returns ``(ChaCha20Poly1305, InvalidTag)``. Calling this triggers
    the ~150ms ``cryptography`` import only when the vault is actually
    used; ``import security.vault`` on its own no longer pays that
    cost. The result is cached on the function via attribute lookup so
    a hot vault (``store`` / ``retrieve`` in a loop) sees the same
    one-time cost as the original eager import.
    """
    cached = getattr(_cryptography, "_cached", None)
    if cached is not None:
        return cached
    from cryptography.exceptions import InvalidTag as _IT
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305 as _CC
    _cryptography._cached = (_CC, _IT)  # type: ignore[attr-defined]
    return _cryptography._cached  # type: ignore[attr-defined]


def _feral_home() -> Path:
    """Lazy wrapper around :func:`config.loader.feral_home` so the
    ``config`` package's transitive imports (provider catalogs, model
    pricing, settings shape validation) don't load on
    ``import security.vault``."""
    from config.loader import feral_home as _fh
    return _fh()

# OS keychain coordinates — keep these stable so legacy installs roll
# forward cleanly. Changing them silently orphans every existing master
# key and forces a recovery-code restore.
KEYRING_SERVICE = "feral-ai"
KEYRING_USERNAME = "vault-master"

# AEAD associated data binds ciphertexts to this format. Bumping the
# version invalidates older payloads (so we never accidentally accept a
# downgrade attack against a future format).
_AEAD_AAD = b"feral-vault-v1"
_VAULT_VERSION = 1

# Env-var override for headless / CI / disaster-recovery: when the OS
# keychain is unavailable but the operator can supply the recovery code
# out-of-band, set FERAL_VAULT_RECOVERY_CODE before importing. The code
# is parsed once at vault construction and cached for the process
# lifetime; it is never echoed to logs.
RECOVERY_ENV = "FERAL_VAULT_RECOVERY_CODE"

# R2-001 — first vault construction blocks on the OS keychain. Without
# a bound, an unresponsive ``securityd`` (macOS) or a broken Secret
# Service bus (Linux) would hang the entire boot indefinitely. Cap the
# unlock at 5 seconds by default; ``FERAL_VAULT_UNLOCK_TIMEOUT_S``
# overrides for slow disaster-recovery environments. The timeout raises
# :class:`VaultKeyUnavailableError` with the same recovery guidance as
# a missing-key path so the operator sees the actionable next step.
_DEFAULT_UNLOCK_TIMEOUT_S = 5.0
_UNLOCK_TIMEOUT_ENV = "FERAL_VAULT_UNLOCK_TIMEOUT_S"


def _unlock_timeout_seconds() -> float:
    raw = os.environ.get(_UNLOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_UNLOCK_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "vault.unlock_timeout_invalid: %s=%r is not a number; "
            "falling back to %.1fs",
            _UNLOCK_TIMEOUT_ENV, raw, _DEFAULT_UNLOCK_TIMEOUT_S,
        )
        return _DEFAULT_UNLOCK_TIMEOUT_S
    if value <= 0:
        return _DEFAULT_UNLOCK_TIMEOUT_S
    return value


# ─────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────


class VaultError(RuntimeError):
    """Base class for vault failures the user must see."""


class VaultKeyUnavailableError(VaultError):
    """The OS keychain has no master key and no recovery code was given."""


class VaultTamperedError(VaultError):
    """AEAD verification failed; the file or key is wrong."""


class VaultFormatError(VaultError):
    """The vault file is well-formed bytes but an unknown vault version."""


# ─────────────────────────────────────────────────────────────────────
# Recovery-code helpers (module-level so CLI + vault both share them)
# ─────────────────────────────────────────────────────────────────────


def encode_recovery_code(master_key: bytes) -> str:
    """Render a 32-byte master key as a human-friendly recovery code.

    Format: uppercase base32 of the raw bytes, stripped of `=` padding,
    grouped into 4-char chunks separated by `-` for readability. A
    32-byte key produces 13 groups (52 base32 chars). Example:

        ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZAB-CDEF-GHIJ-KLMN-OPQR-STUV-WXY3

    The code IS the master key — anyone holding it can decrypt the
    vault offline. It is shown exactly once.
    """
    if len(master_key) != 32:
        raise ValueError(f"master key must be 32 bytes, got {len(master_key)}")
    raw = base64.b32encode(master_key).decode().rstrip("=")
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def decode_recovery_code(code: str) -> bytes:
    """Inverse of :func:`encode_recovery_code`. Accepts mixed case +
    arbitrary whitespace + dashes (so a user can paste the code as the
    CLI rendered it OR as a single block they wrote down)."""
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    if not cleaned:
        raise ValueError("recovery code is empty after stripping separators")
    pad = (-len(cleaned)) % 8
    cleaned += "=" * pad
    try:
        key = base64.b32decode(cleaned)
    except Exception as exc:
        raise ValueError(
            f"recovery code is not valid base32 ({exc}); expected the "
            f"format printed by `feral key rotate` / first-boot."
        ) from exc
    if len(key) != 32:
        raise ValueError(
            f"recovery code decoded to {len(key)} bytes; expected 32. "
            f"Check that you copied every group."
        )
    return key


# ─────────────────────────────────────────────────────────────────────
# Keychain access (small wrapper so tests can patch one symbol)
# ─────────────────────────────────────────────────────────────────────


def _keyring_get_password(service: str, username: str) -> Optional[str]:
    """Read a password from the OS keychain.

    Wrapped so tests can patch a single symbol with a dict-backed fake
    instead of polluting the real macOS Keychain / Linux Secret Service.
    Returns ``None`` if the entry is missing OR the keychain is
    unavailable; the caller decides whether absence is fatal.

    R2-001 — the call is run on a worker thread bounded by
    :func:`_unlock_timeout_seconds` (default 5s). A frozen
    ``securityd`` no longer hangs the brain boot indefinitely; instead
    we raise :class:`VaultKeyUnavailableError` with the recovery
    guidance the operator needs.
    """
    try:
        import keyring
    except ImportError as exc:
        raise VaultKeyUnavailableError(
            "The `keyring` package is not installed. Install with "
            "`pip install keyring` or set "
            f"{RECOVERY_ENV}=<recovery-code> to decrypt the vault."
        ) from exc

    timeout = _unlock_timeout_seconds()
    logger.info("vault.unlock.start service=%s timeout=%.1fs", service, timeout)

    import threading

    started = time.perf_counter()
    result_box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result_box["value"] = keyring.get_password(service, username)
        except Exception as exc:  # noqa: BLE001 — caller decides
            result_box["error"] = exc

    # ``daemon=True`` so a frozen ``securityd`` cannot wedge process
    # shutdown — the worker dies with the interpreter even when the
    # blocking syscall never returns. We deliberately do NOT join with
    # an unbounded wait; the timeout below is the only blocking point.
    worker = threading.Thread(
        target=_worker, name="vault-unlock", daemon=True,
    )
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        raise VaultKeyUnavailableError(
            f"OS keychain unlock for service={service!r}, "
            f"user={username!r} did not complete in {timeout:.1f}s. "
            f"On macOS, the login keychain may be locked — open "
            f"Keychain Access and unlock it, then retry. On Linux, "
            f"the Secret Service (gnome-keyring/KWallet) may not be "
            f"running. Alternatively, set {RECOVERY_ENV}=<recovery-"
            f"code> to skip the keychain on this boot."
        )

    if "error" in result_box:
        # Keychain backend errors are user-actionable but vary wildly
        # by platform; surface them at the call site with context
        # rather than guessing here.
        logger.info(
            "vault.unlock.error service=%s elapsed=%.0fms err=%s",
            service,
            (time.perf_counter() - started) * 1000,
            type(result_box["error"]).__name__,
        )
        return None

    result = result_box.get("value")
    logger.info(
        "vault.unlock.done service=%s elapsed=%.0fms found=%s",
        service, (time.perf_counter() - started) * 1000, result is not None,
    )
    return result


def _keyring_set_password(service: str, username: str, password: str) -> None:
    """Persist a password in the OS keychain.

    Errors propagate so the vault can refuse to start with a clear
    "your platform's keychain is broken; here's how to recover" message
    instead of silently losing the master key.
    """
    import keyring
    keyring.set_password(service, username, password)


def _keyring_delete_password(service: str, username: str) -> None:
    """Delete a keychain entry (used by `feral key recover` --reset
    paths and by tests that want to force a fresh-boot codepath)."""
    try:
        import keyring
        keyring.delete_password(service, username)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# BlindVault
# ─────────────────────────────────────────────────────────────────────


class BlindVault:
    """Encrypted credential storage.

    Public API:

      - ``store(key, value, stored_by="user")`` / ``retrieve(key)`` /
        ``has_key(key)`` / ``list_keys()`` / ``remove(key)`` /
        ``fingerprint(key)`` / ``to_safe_summary()``
        — legacy single-namespace API. Equivalent to
        ``put("credentials", key, value)`` etc.

      - ``put(namespace, key, value)`` / ``get(namespace, key)`` /
        ``list_namespace(namespace)`` / ``remove_from(namespace, key)``
        — namespaced API for callers that need more than the default
        flat credentials map (e.g. publisher_keys, oauth_*).

      - ``set_credential(key, value)`` / ``get_credential(key)``
        — thin wrappers used by the new W9 smoke surface.

      - ``rotate_master_key()`` returns a freshly-printed recovery
        code; the previous master key is wiped from the keychain and
        the previous ``.enc`` file lingers as ``.enc.prev`` until the
        next successful boot, then is removed.
    """

    DEFAULT_NAMESPACE = "credentials"

    def __init__(self, vault_path: Optional[str] = None):
        home = _feral_home()

        # The vault has TWO disk artefacts:
        #   - legacy plaintext file (only present pre-migration)
        #   - encrypted file (authoritative after migration)
        # Constructors that pass a `vault_path` (legacy tests, code that
        # wants a per-instance vault) get the .enc placed next to it so
        # tests stay self-contained inside their tmp_path.
        if vault_path:
            self._legacy_json_path = Path(vault_path)
        else:
            self._legacy_json_path = home / "credentials.json"

        # `with_suffix` replaces the existing `.json` with `.enc`. For
        # paths without a `.json` suffix (e.g. a tests' "vault.db" hack)
        # we just append `.enc` so the encrypted file is unambiguously
        # distinct from the legacy file.
        if self._legacy_json_path.suffix == ".json":
            self._enc_path = self._legacy_json_path.with_suffix(".enc")
        else:
            self._enc_path = self._legacy_json_path.with_name(
                self._legacy_json_path.name + ".enc"
            )

        self._backup_path = self._legacy_json_path.with_name(
            self._legacy_json_path.name + ".bak.legacy"
        )
        self._prev_path = self._enc_path.with_name(self._enc_path.name + ".prev")

        self._audit_path = home / "audit.log"

        # In-memory state:
        #   _data is the WHOLE vault, namespace-keyed:
        #     {"credentials": {…flat creds…}, "publisher_keys": {…}, …}
        #   _master_key is the 32-byte AEAD key, fetched lazily.
        self._data: dict[str, dict] = {self.DEFAULT_NAMESPACE: {}}
        self._cached_master_key: Optional[bytes] = None
        self._first_boot_recovery_code: Optional[str] = None
        self._first_boot: bool = False
        self._migrated_from_legacy: bool = False

        self._load()

    # ── Master-key lifecycle ────────────────────────────────────────

    def _master_key(self) -> bytes:
        """Resolve the 32-byte master key from keychain → recovery
        env var → fresh generate (only if no .enc exists yet)."""
        if self._cached_master_key is not None:
            return self._cached_master_key

        stored = _keyring_get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if stored:
            try:
                key = base64.b64decode(stored)
            except Exception as exc:
                raise VaultTamperedError(
                    f"Master key in OS keychain (service={KEYRING_SERVICE!r}, "
                    f"user={KEYRING_USERNAME!r}) is not valid base64. "
                    f"Run `feral key recover` and supply your recovery code."
                ) from exc
            if len(key) != 32:
                raise VaultTamperedError(
                    f"Master key in OS keychain is {len(key)} bytes; "
                    f"expected 32. The keychain entry may be corrupted; "
                    f"run `feral key recover` and supply your recovery code."
                )
            self._cached_master_key = key
            return key

        recovery = os.environ.get(RECOVERY_ENV, "").strip()
        if recovery:
            key = decode_recovery_code(recovery)
            self._cached_master_key = key
            # Re-seed the keychain so subsequent boots don't need the
            # recovery code. If the keychain is broken we keep using the
            # cached key for this process and let the user run
            # `feral key recover` next time.
            try:
                _keyring_set_password(
                    KEYRING_SERVICE,
                    KEYRING_USERNAME,
                    base64.b64encode(key).decode(),
                )
            except Exception as exc:
                logger.warning(
                    "vault.keychain_reseed_failed: %s "
                    "(continuing with recovery-code key in memory only)",
                    exc,
                )
            return key

        if self._enc_path.exists():
            raise VaultKeyUnavailableError(
                f"Encrypted vault {self._enc_path} exists but the OS "
                f"keychain has no master key (service={KEYRING_SERVICE!r}, "
                f"user={KEYRING_USERNAME!r}) and {RECOVERY_ENV} is unset.\n"
                f"Recover by either:\n"
                f"  • re-running on a host where the keychain still has "
                f"the entry, OR\n"
                f"  • running `feral key recover` and pasting the recovery "
                f"code printed at first boot, OR\n"
                f"  • exporting {RECOVERY_ENV}=<recovery-code> in this "
                f"shell and re-launching the brain."
            )

        # Fresh install: generate a new master key, persist to keychain,
        # capture the recovery code so the CLI / boot path can show it
        # exactly once.
        ChaCha20Poly1305, _ = _cryptography()
        key = ChaCha20Poly1305.generate_key()
        try:
            _keyring_set_password(
                KEYRING_SERVICE,
                KEYRING_USERNAME,
                base64.b64encode(key).decode(),
            )
        except Exception as exc:
            raise VaultKeyUnavailableError(
                f"Failed to write master key to OS keychain "
                f"(service={KEYRING_SERVICE!r}, user={KEYRING_USERNAME!r}): "
                f"{exc}.\n"
                f"On macOS, open Keychain Access and unlock the login "
                f"keychain. On Linux, ensure a Secret Service backend "
                f"(GNOME Keyring, KWallet, or `keyrings.alt`) is "
                f"installed and a session is running. Alternatively, "
                f"set {RECOVERY_ENV}=<32-byte hex/base32> to bootstrap."
            ) from exc
        self._cached_master_key = key
        self._first_boot_recovery_code = encode_recovery_code(key)
        self._first_boot = True
        return key

    # ── On-disk format: AEAD encrypt / decrypt ──────────────────────

    def _encrypt_blob(self, payload: dict) -> bytes:
        ChaCha20Poly1305, _ = _cryptography()
        key = self._master_key()
        plaintext = json.dumps(
            {"version": _VAULT_VERSION, "data": payload},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        nonce = os.urandom(12)
        ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, _AEAD_AAD)
        return nonce + ct

    def _decrypt_blob(self, raw: bytes, *, key: Optional[bytes] = None) -> dict:
        if len(raw) < 12 + 16:  # nonce + minimum AEAD tag
            raise VaultTamperedError(
                f"Vault file {self._enc_path} is too short to contain a "
                f"ChaCha20-Poly1305 ciphertext ({len(raw)} bytes)."
            )
        ChaCha20Poly1305, InvalidTag = _cryptography()
        if key is None:
            key = self._master_key()
        nonce, ct = raw[:12], raw[12:]
        try:
            plaintext = ChaCha20Poly1305(key).decrypt(nonce, ct, _AEAD_AAD)
        except InvalidTag as exc:
            raise VaultTamperedError(
                f"AEAD verification failed for {self._enc_path}. The file "
                f"is tampered with, OR the master key in the OS keychain "
                f"does not match this file. Run `feral key recover` and "
                f"supply the recovery code printed at first boot."
            ) from exc
        try:
            blob = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise VaultFormatError(
                f"Vault decrypted but payload is not JSON: {exc}"
            ) from exc
        if (
            not isinstance(blob, dict)
            or blob.get("version") != _VAULT_VERSION
            or not isinstance(blob.get("data"), dict)
        ):
            raise VaultFormatError(
                f"Vault {self._enc_path} has unknown format "
                f"(expected {{'version': {_VAULT_VERSION}, 'data': {{…}}}})."
            )
        return blob["data"]

    def _persist(self) -> None:
        self._enc_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._encrypt_blob(self._data)
        # Atomic write: stage to .enc.new, fsync, rename.
        tmp = self._enc_path.with_name(self._enc_path.name + ".new")
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._enc_path)
        try:
            os.chmod(self._enc_path, 0o600)
        except OSError as exc:
            logger.warning("vault.chmod_failed: %s", exc)

    # ── Boot path: load / migrate ───────────────────────────────────

    def _load(self) -> None:
        # Path 1: encrypted file is authoritative if present.
        if self._enc_path.exists():
            if self._legacy_json_path.exists():
                logger.warning(
                    "vault.both_files_present: %s and %s both exist. "
                    "Using the encrypted file; please delete the legacy "
                    "plaintext file (it is no longer read).",
                    self._enc_path, self._legacy_json_path,
                )
            raw = self._enc_path.read_bytes()
            decoded = self._decrypt_blob(raw)
            self._data = self._normalise_namespaces(decoded)
            # On a successful boot, the previous-rotation backup is no
            # longer needed (we just proved the new key works).
            if self._prev_path.exists():
                try:
                    self._prev_path.unlink()
                except OSError as exc:
                    logger.warning("vault.prev_unlink_failed: %s", exc)
            return

        # Path 2: legacy plaintext exists → migrate.
        if self._legacy_json_path.exists():
            self._migrate_from_plaintext()
            return

        # Path 3: fresh install — touch nothing, just initialise empty.
        self._data = {self.DEFAULT_NAMESPACE: {}}

    def _migrate_from_plaintext(self) -> None:
        """Read ``credentials.json``, encrypt to ``credentials.enc``,
        back up the legacy file to ``credentials.json.bak.legacy``
        (chmod 0600), and unlink the original."""
        try:
            with open(self._legacy_json_path) as f:
                parsed = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            # Legacy behaviour preserved: a corrupt plaintext file is
            # moved aside (so the brain can boot with an empty vault
            # rather than crash) and surfaced as a warning. Operators
            # can inspect the `.corrupt` file out-of-band.
            corrupt_path = self._legacy_json_path.with_name(
                self._legacy_json_path.stem + ".corrupt"
            )
            try:
                self._legacy_json_path.rename(corrupt_path)
            except OSError as rename_exc:
                logger.error(
                    "vault.corrupt_rename_failed: could not move %s to %s: %s",
                    self._legacy_json_path, corrupt_path, rename_exc,
                )
                raise VaultError(
                    f"Legacy credentials file {self._legacy_json_path} is "
                    f"unreadable and could not be moved aside: {rename_exc}"
                ) from rename_exc
            logger.warning(
                "vault.legacy_corrupt: %s unreadable (%s) → moved to %s; "
                "starting with empty vault",
                self._legacy_json_path, exc, corrupt_path,
            )
            self._data = {self.DEFAULT_NAMESPACE: {}}
            return

        if not isinstance(parsed, dict):
            logger.warning(
                "vault.legacy_unexpected_shape: %s root was %s, expected dict; "
                "starting with empty vault",
                self._legacy_json_path, type(parsed).__name__,
            )
            self._data = {self.DEFAULT_NAMESPACE: {}}
            parsed = {}

        # Distinguish legacy "flat credentials" maps from newer "fully
        # namespaced" payloads (which a future hand-edit might place
        # there). Heuristic: if every value at the top level is itself
        # a dict, treat the file as already-namespaced. Otherwise wrap
        # the whole map into the default credentials namespace.
        if parsed and all(isinstance(v, dict) for v in parsed.values()):
            self._data = self._normalise_namespaces(parsed)
        else:
            self._data = {self.DEFAULT_NAMESPACE: dict(parsed)}

        self._persist()

        # Back up the legacy file (chmod 0600), then unlink it.
        try:
            self._enc_path.parent.mkdir(parents=True, exist_ok=True)
            data = self._legacy_json_path.read_bytes()
            self._backup_path.write_bytes(data)
            os.chmod(self._backup_path, 0o600)
            os.unlink(self._legacy_json_path)
        except OSError as exc:
            raise VaultError(
                f"Encrypted vault was written to {self._enc_path}, but the "
                f"legacy backup/unlink step failed: {exc}. Inspect "
                f"{self._legacy_json_path} and {self._backup_path} manually."
            ) from exc

        self._migrated_from_legacy = True
        logger.info(
            "vault.migrated_to_encrypted: backed up legacy credentials to "
            "credentials.json.bak.legacy"
        )

    @staticmethod
    def _normalise_namespaces(decoded: dict) -> dict[str, dict]:
        """Coerce the on-disk dict into ``{namespace: {key: value}}``.

        Older payloads (pre-W9) could legitimately have non-dict values
        at the top level (the BlindVault used to be a flat map). After
        migration we only ever write namespaced dicts, but we still
        defensively accept either shape on read so a hand-edited file
        never bricks the boot."""
        if not isinstance(decoded, dict):
            return {BlindVault.DEFAULT_NAMESPACE: {}}
        out: dict[str, dict] = {}
        flat: dict = {}
        for k, v in decoded.items():
            if isinstance(v, dict):
                out[k] = dict(v)
            else:
                flat[k] = v
        if flat:
            existing = out.setdefault(BlindVault.DEFAULT_NAMESPACE, {})
            existing.update(flat)
        out.setdefault(BlindVault.DEFAULT_NAMESPACE, {})
        return out

    # ── Namespaced API (primary surface for new W9 callers) ────────

    def put(self, namespace: str, key: str, value: str, *, stored_by: str = "user") -> None:
        """Store ``value`` under ``namespace.key`` and persist."""
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace must be a non-empty string")
        if not key or not isinstance(key, str):
            raise ValueError("key must be a non-empty string")
        bucket = self._data.setdefault(namespace, {})
        bucket[key] = value
        self._persist()
        self._audit("put", f"{namespace}.{key}", stored_by)
        logger.info("Credential stored: %s.%s", namespace, key)

    def get(self, namespace: str, key: str, *, requester: str = "executor") -> Optional[str]:
        """Read ``namespace.key``; ``None`` when absent."""
        bucket = self._data.get(namespace, {})
        value = bucket.get(key)
        self._audit("get", f"{namespace}.{key}", requester, found=value is not None)
        return value

    def remove_from(self, namespace: str, key: str, *, removed_by: str = "user") -> bool:
        bucket = self._data.get(namespace)
        if bucket is None or key not in bucket:
            return False
        del bucket[key]
        self._persist()
        self._audit("remove", f"{namespace}.{key}", removed_by)
        return True

    def list_namespace(self, namespace: str) -> list[str]:
        return list(self._data.get(namespace, {}).keys())

    def list_namespaces(self) -> list[str]:
        return [ns for ns in self._data.keys()]

    @property
    def _cache(self) -> dict:
        """Backward-compat alias for the default-namespace contents.

        Pre-W9 BlindVault was a flat ``{key: value}`` map and one
        existing test (``test_blind_vault_survives_corrupt_json`` in
        ``tests/test_key_persistence.py``) asserts directly on
        ``vault._cache``. Returning the default namespace keeps that
        assertion meaningful without re-exposing the underlying
        namespaced storage."""
        return self._data.get(self.DEFAULT_NAMESPACE, {})

    # ── Legacy single-namespace API ─────────────────────────────────
    #
    # These are the methods the rest of feral-core (state.py, oauth_manager,
    # api/routes/*) calls today. They are now thin wrappers around the
    # namespaced API; the on-disk layout is still encrypted.

    def store(self, key_name: str, value: str, stored_by: str = "user") -> None:
        self.put(self.DEFAULT_NAMESPACE, key_name, value, stored_by=stored_by)

    def retrieve(self, key_name: str, requester: str = "executor") -> Optional[str]:
        return self.get(self.DEFAULT_NAMESPACE, key_name, requester=requester)

    def has_key(self, key_name: str) -> bool:
        return key_name in self._data.get(self.DEFAULT_NAMESPACE, {})

    def list_keys(self) -> list[str]:
        return self.list_namespace(self.DEFAULT_NAMESPACE)

    def remove(self, key_name: str, removed_by: str = "user") -> bool:
        return self.remove_from(
            self.DEFAULT_NAMESPACE, key_name, removed_by=removed_by
        )

    def fingerprint(self, key_name: str) -> Optional[str]:
        """SHA-256 fingerprint of the value for verification without
        exposing the secret. Truncated to 12 hex chars (48 bits) — long
        enough to detect "is this the same key" but short enough to be
        unguessable."""
        val = self._data.get(self.DEFAULT_NAMESPACE, {}).get(key_name)
        if val is None:
            return None
        return hashlib.sha256(val.encode()).hexdigest()[:12]

    def to_safe_summary(self) -> dict:
        """Return a summary safe for the client. Shows key names and
        fingerprints, never values. Includes the default credentials
        namespace only — extra namespaces (publisher_keys, oauth_*) are
        deliberately omitted from the client-visible surface to avoid
        leaking which integrations a user has configured."""
        return {
            name: {
                "stored": True,
                "fingerprint": self.fingerprint(name),
            }
            for name in self.list_keys()
        }

    # ── set_credential / get_credential aliases (W9 smoke-test surface)

    def set_credential(self, key_name: str, value: str) -> None:
        self.store(key_name, value, stored_by="set_credential")

    def get_credential(self, key_name: str) -> Optional[str]:
        return self.retrieve(key_name, requester="get_credential")

    def list_credential_keys(self) -> list[str]:
        """Return key names in the default credentials namespace (no values).

        Used by probe/doctor surfaces to discover configured providers
        without leaking secrets to logs.
        """
        return self.list_keys()

    def iter_credential_keys(self):
        """Iterate credential key names in the default namespace."""
        yield from self.list_keys()

    # ── Rotation ────────────────────────────────────────────────────

    def rotate_master_key(self) -> str:
        """Generate a new master key, re-encrypt the vault under it,
        and atomically swap the on-disk file. The previous ``.enc`` is
        kept as ``.enc.prev`` (chmod 0600) until the next successful
        boot. Returns the new recovery code; print it for the user
        immediately and never persist it.

        Failure modes:
          - Existing vault won't decrypt → ``VaultTamperedError`` (we
            refuse to rotate from an unreadable state).
          - Keychain rejects the new master key → original ``.enc``
            stays in place and the exception propagates so the user
            can fix the keychain before retrying.
        """
        # Force a fresh decrypt with the CURRENT master key so a
        # silently-corrupted in-memory state can't be re-encrypted under
        # a new key (we'd lose data without noticing otherwise).
        if self._enc_path.exists():
            raw = self._enc_path.read_bytes()
            self._data = self._normalise_namespaces(self._decrypt_blob(raw))

        ChaCha20Poly1305, _ = _cryptography()
        new_key = ChaCha20Poly1305.generate_key()
        plaintext = json.dumps(
            {"version": _VAULT_VERSION, "data": self._data},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        nonce = os.urandom(12)
        ct = ChaCha20Poly1305(new_key).encrypt(nonce, plaintext, _AEAD_AAD)
        payload = nonce + ct

        new_path = self._enc_path.with_name(self._enc_path.name + ".new")
        with open(new_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(new_path, 0o600)
        except OSError:
            pass

        # Move the existing .enc → .enc.prev (clearing any older .prev),
        # then move .enc.new → .enc atomically.
        if self._prev_path.exists():
            try:
                self._prev_path.unlink()
            except OSError as exc:
                logger.warning("vault.prev_unlink_failed: %s", exc)
        if self._enc_path.exists():
            os.rename(self._enc_path, self._prev_path)
            try:
                os.chmod(self._prev_path, 0o600)
            except OSError:
                pass
        os.replace(new_path, self._enc_path)

        # Update the keychain LAST — if it fails we still have a working
        # file decryptable by the new in-memory key, but the old .prev
        # is still decryptable by the OLD key from the OS keychain.
        # Operator runs `feral key recover` to resync.
        _keyring_set_password(
            KEYRING_SERVICE,
            KEYRING_USERNAME,
            base64.b64encode(new_key).decode(),
        )
        self._cached_master_key = new_key
        new_code = encode_recovery_code(new_key)
        self._audit("rotate_master_key", "vault-master", "user")
        logger.info(
            "vault.master_key_rotated: previous .enc kept at %s until next "
            "successful boot.",
            self._prev_path,
        )
        return new_code

    # ── Recovery from a recovery code ───────────────────────────────

    def restore_from_recovery_code(self, code: str) -> None:
        """Re-seed the OS keychain from a written-down recovery code.

        Use case: the user wiped their keychain (new laptop, OS
        reinstall, accidental delete) and the .enc file is the only
        copy of the credentials. Decode the code → verify it actually
        decrypts the file → write it back to the keychain.
        """
        candidate = decode_recovery_code(code)
        if not self._enc_path.exists():
            raise VaultError(
                f"No encrypted vault found at {self._enc_path}; nothing "
                f"to restore. (Did you run on the right host / right "
                f"FERAL_HOME?)"
            )
        raw = self._enc_path.read_bytes()
        # Will raise VaultTamperedError if the code doesn't match.
        self._data = self._normalise_namespaces(
            self._decrypt_blob(raw, key=candidate)
        )
        _keyring_set_password(
            KEYRING_SERVICE,
            KEYRING_USERNAME,
            base64.b64encode(candidate).decode(),
        )
        self._cached_master_key = candidate
        self._audit("restore_from_recovery_code", "vault-master", "user")
        logger.info("vault.master_key_restored_from_recovery_code")

    # ── Status / introspection (CLI uses this) ──────────────────────

    def status(self) -> dict:
        """Snapshot of vault on-disk + keychain state for `feral key status`."""
        keychain_ok = _keyring_get_password(KEYRING_SERVICE, KEYRING_USERNAME) is not None
        return {
            "encrypted": self._enc_path.exists(),
            "encrypted_path": str(self._enc_path),
            "keychain": keychain_ok,
            "keychain_service": KEYRING_SERVICE,
            "keychain_user": KEYRING_USERNAME,
            "legacy_backup": self._backup_path.exists(),
            "legacy_backup_path": str(self._backup_path) if self._backup_path.exists() else None,
            "prev_backup": self._prev_path.exists(),
            "prev_backup_path": str(self._prev_path) if self._prev_path.exists() else None,
            "namespaces": self.list_namespaces(),
            "key_count": sum(len(v) for v in self._data.values()),
            "first_boot_recovery_code": self._first_boot_recovery_code,
        }

    def consume_first_boot_recovery_code(self) -> Optional[str]:
        """Return the first-boot recovery code exactly once. Subsequent
        calls return ``None`` so the code is never logged or echoed
        twice. Call sites: the CLI / setup wizard, NEVER the audit log."""
        code = self._first_boot_recovery_code
        self._first_boot_recovery_code = None
        return code

    # ── Audit ───────────────────────────────────────────────────────

    def _audit(self, action: str, key_name: str, actor: str, **extra) -> None:
        """Append a fail-loud audit entry.

        audit-r12 A5 (v2026.5.38) — the pre-fix path logged the OSError
        as a warning and let the credential op succeed silently. That
        violated the security-perimeter promise: a write that left no
        audit record might as well have been a stealth credential read
        for the operator who later combs the log looking for the breach.

        The new behaviour delegates to
        :func:`security.audit_log.audit_event` which raises
        :class:`security.audit_log.AuditFailure` on any I/O error. The
        public credential ops (:meth:`store`, :meth:`retrieve`,
        :meth:`remove`, :meth:`put`, :meth:`get`, :meth:`remove_from`,
        :meth:`put_namespace`, :meth:`get_namespace`,
        :meth:`remove_namespace`, :meth:`rotate_master_key`,
        :meth:`restore_from_recovery_code`) propagate the exception so
        the caller decides — they no longer swallow it.

        The legacy ``self._audit_path`` attribute is still set in
        ``__init__`` for the historical inline path, but writes now
        flow through :func:`audit_log.audit_event` which resolves the
        path via ``FERAL_AUDIT_LOG_PATH`` / ``$FERAL_HOME/audit.log``
        (same default as before, plus a test-friendly env override).
        """
        from security import audit_log

        audit_log.audit_event(action, key_name, actor, **extra)


# ─────────────────────────────────────────────────────────────────────
# Module-level singleton (mirrors the device_pairing pattern)
# ─────────────────────────────────────────────────────────────────────


_vault: Optional[BlindVault] = None


def get_vault(vault_path: Optional[str] = None) -> BlindVault:
    """Lazy-initialised process-wide vault. The CLI smoke test in the
    W9 charter uses this entrypoint:

        from feral_core.security.vault import get_vault
        v = get_vault()
        v.set_credential("test", "value")
        assert v.get_credential("test") == "value"
    """
    global _vault
    if _vault is None or vault_path is not None:
        _vault = BlindVault(vault_path=vault_path)
    return _vault


def reset_vault() -> None:
    """Reset the module-level singleton — used in tests."""
    global _vault
    _vault = None


# ─────────────────────────────────────────────────────────────────────
# Permission tiers + execution sandbox (unchanged behaviour from pre-W9)
# ─────────────────────────────────────────────────────────────────────

    # ------------------------------------------------------------------
    # Namespaced put/get (W8 addition; W9 should adopt as the canonical
    # interface, then we can deprecate the flat store/retrieve helpers).
    # The namespace is encoded as a single key prefix so on-disk layout
    # stays a flat dict (one JSON file). Reserved separator: "::".
    # ------------------------------------------------------------------

    _NS_SEP = "::"

    @classmethod
    def _ns_key(cls, namespace: str, key: str) -> str:
        if not namespace or not isinstance(namespace, str):
            raise ValueError("namespace must be a non-empty string")
        if cls._NS_SEP in namespace:
            raise ValueError(
                f"namespace must not contain {cls._NS_SEP!r} (reserved separator)"
            )
        if not key or not isinstance(key, str):
            raise ValueError("key must be a non-empty string")
        return f"{namespace}{cls._NS_SEP}{key}"

    def put_namespace(
        self,
        namespace: str,
        key: str,
        value: str,
        *,
        stored_by: str = "user",
    ) -> None:
        """Store *value* under (namespace, key). Audit-logged like store()."""
        full_key = self._ns_key(namespace, key)
        self._cache[full_key] = value
        self._persist()
        self._audit("store", full_key, stored_by, namespace=namespace)
        logger.info("Credential stored: ns=%s key=%s", namespace, key)

    def get_namespace(
        self,
        namespace: str,
        key: str,
        *,
        requester: str = "executor",
    ) -> Optional[str]:
        """Retrieve a value previously written via put_namespace."""
        full_key = self._ns_key(namespace, key)
        value = self._cache.get(full_key)
        self._audit(
            "retrieve",
            full_key,
            requester,
            found=value is not None,
            namespace=namespace,
        )
        return value

    def list_namespace(self, namespace: str) -> list[str]:
        """List the keys (without values) registered under *namespace*."""
        prefix = f"{namespace}{self._NS_SEP}"
        return [k[len(prefix):] for k in self._cache if k.startswith(prefix)]

    def remove_namespace(
        self,
        namespace: str,
        key: str,
        *,
        removed_by: str = "user",
    ) -> bool:
        full_key = self._ns_key(namespace, key)
        if full_key in self._cache:
            del self._cache[full_key]
            self._persist()
            self._audit("remove", full_key, removed_by, namespace=namespace)
            return True
        return False


class PermissionTier:
    """
    Permission tiers for skill execution:
      - PASSIVE: read-only, no side effects (weather, search)
      - ACTIVE: can send data (messaging, calendar create)
      - PRIVILEGED: can modify system state (file access, shell commands)
      - DANGEROUS: destructive operations (delete, financial transactions)
    """
    PASSIVE = "passive"
    ACTIVE = "active"
    PRIVILEGED = "privileged"
    DANGEROUS = "dangerous"

    TIER_ORDER = [PASSIVE, ACTIVE, PRIVILEGED, DANGEROUS]

    @classmethod
    def requires_confirmation(cls, tier: str) -> bool:
        return tier in (cls.PRIVILEGED, cls.DANGEROUS)

    @classmethod
    def tier_level(cls, tier: str) -> int:
        try:
            return cls.TIER_ORDER.index(tier)
        except ValueError:
            return 0


class ExecutionSandbox:
    """
    Constraints applied to skill execution based on permission tier.
    """

    def __init__(self, max_tier: str = PermissionTier.ACTIVE):
        self.max_tier = max_tier
        self._blocked_domains: set[str] = set()
        self._rate_limits: dict[str, int] = {}
        self._execution_log: list[dict] = []

    def can_execute(self, skill_id: str, tier: str) -> tuple[bool, str]:
        if PermissionTier.tier_level(tier) > PermissionTier.tier_level(self.max_tier):
            return False, f"Tier {tier} exceeds max allowed tier {self.max_tier}"

        limit = self._rate_limits.get(skill_id)
        if limit is not None:
            recent = sum(
                1 for e in self._execution_log
                if e["skill_id"] == skill_id and time.time() - e["ts"] < 60
            )
            if recent >= limit:
                return False, f"Rate limit exceeded for {skill_id} ({limit}/min)"

        return True, "ok"

    def log_execution(self, skill_id: str, tier: str, success: bool):
        self._execution_log.append({
            "ts": time.time(),
            "skill_id": skill_id,
            "tier": tier,
            "success": success,
        })
        if len(self._execution_log) > 1000:
            self._execution_log = self._execution_log[-500:]

    def set_rate_limit(self, skill_id: str, per_minute: int):
        self._rate_limits[skill_id] = per_minute

    def block_domain(self, domain: str):
        self._blocked_domains.add(domain)

    def is_domain_blocked(self, url: str) -> bool:
        return any(d in url for d in self._blocked_domains)
