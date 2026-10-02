"""Deferred, explicit vault unlock without bootstrap/migration/recovery writes.

Passive construction/status never touches the keychain. This coordinator is
not wired into boot yet. Callers must gate dependent services with require_ready
and supply hydration/disable callbacks which do not start jobs before hydration
has completed. A timed-out OS operation remains single-flight until it returns.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import math
import threading
import time
from dataclasses import dataclass
from uuid import uuid4
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable


class VaultLockedRefusal(RuntimeError):
    code = "vault_locked"

    def __init__(self):
        super().__init__("Unlock the vault explicitly before using stored credentials.")


class VaultInitializationRefusal(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("Fresh initialization needs a current explicit review and no operation in progress.")


@dataclass(frozen=True)
class VaultInitializationReview:
    token: str
    generation: int
    created_uptime: float
    scope: str = "Create an empty encrypted vault with a random master key in the existing OS keychain. Existing keys and vault artifacts will not be replaced. macOS may ask for access. A successful key write followed by a disk failure can leave partial initialization; no automatic reset or deletion occurs."


class _Unavailable(RuntimeError):
    def __init__(self, code: str):
        self.code = code


def readonly_vault_factory(vault_path: str | Path | None = None):
    """Return a factory that authenticates existing ciphertext without writes.

    Unlike BlindVault construction, this refuses legacy/fresh boot rather than
    migrating or generating a key. It never consults a recovery environment
    value. Keychain get runs directly on the coordinator's one daemon worker;
    no nested timeout thread can outlive our single-flight bookkeeping.
    """
    def open_existing():
        from security.vault import BlindVault, KEYRING_SERVICE, KEYRING_USERNAME

        class ReadOnlyOpenedVault(BlindVault):
            _explicit_deferred_unlock = True
            def _master_key(self):
                if self._cached_master_key is not None:
                    return self._cached_master_key
                import keyring
                stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
                if not stored:
                    raise _Unavailable("key_unavailable")
                try:
                    key = base64.b64decode(stored, validate=True)
                except (ValueError, TypeError) as exc:
                    raise _Unavailable("key_invalid") from exc
                if len(key) != 32:
                    raise _Unavailable("key_invalid")
                self._cached_master_key = key
                return key

            def _load(self):
                if not self._enc_path.is_file():
                    raise _Unavailable("vault_uninitialized")
                # _decrypt_blob verifies the AEAD tag before returning data.
                self._data = self._normalise_namespaces(
                    self._decrypt_blob(self._enc_path.read_bytes())
                )

        return ReadOnlyOpenedVault(vault_path=str(vault_path) if vault_path is not None else None)

    return open_existing


async def _callback(callback: Callable, *args):
    value = callback(*args)
    if inspect.isawaitable(value):
        return await value
    return value


class VaultCoordinator:
    """Event-loop-owned readiness gate with one uncancellable OS worker.

    The injected factory must authenticate the complete ciphertext before
    returning. Hydration runs on the owning event loop after authentication;
    returning False is failure. Disable must remove dependent readiness on
    failure/lock. Do not expose the private vault before require_ready succeeds.
    """
    _MESSAGES = {
        "unlock_required": "Stored credentials are locked. Unlock explicitly to continue.",
        "unlock_pending": "The original unlock is still running. Another attempt will not start.",
        "key_unavailable": "The existing keychain key is unavailable. Check your existing keychain, then retry.",
        "key_invalid": "The stored key is invalid. No keychain entry or vault file was changed.",
        "vault_uninitialized": "No encrypted vault exists. Explicit secure setup is required; no key was generated.",
        "authentication_failed": "Vault authentication failed. No recovery, reset or file changes were attempted.",
        "unlock_failed": "Unlock failed. Private system details are withheld; dependent services remain disabled.",
        "hydration_failed": "Credentials could not be activated. Dependent services remain disabled.",
        "ready": "Encrypted vault authenticated and credential hydration completed.",
        "initialization_unsupported": "Secure initialization is unavailable for this storage backend; no fallback key storage is used.",
        "key_already_present": "An existing shared master key was found. It was not replaced; inspect the existing vault before proceeding.",
        "vault_artifacts_present": "Vault artifacts already exist. Fresh initialization refused to preserve them.",
        "initialization_partial": "The master key was added, but vault persistence failed. Initialization is partial; no key or file was reset or deleted.",
    }

    def __init__(self, *, vault_factory: Callable[[], Any] | None = None,
                 credential_hydrator: Callable[[Any], Any] | None = None,
                 disable_dependents: Callable[[], Any] | None = None,
                 fresh_initializer: Callable[[], Any] | None = None):
        self._factory = vault_factory or readonly_vault_factory()
        self._initializer = fresh_initializer
        self._initialization_reviews: dict[str, VaultInitializationReview] = {}
        self._unlock_reviews: dict[str, tuple[int, float, str, str]] = {}
        self._hydrate = credential_hydrator or (lambda vault: None)
        self._disable = disable_dependents or (lambda: None)
        self._vault = None
        self._state = "locked"
        self._code = "unlock_required"
        self._operation: asyncio.Task | None = None
        self._generation = 0
        self._loop = None

    def status(self) -> dict:
        # No filesystem, keychain, factory or credential callbacks here.
        return {"state": self._state, "code": self._code,
                "message": self._MESSAGES[self._code],
                "in_flight": self._operation is not None and not self._operation.done(),
                "credentials_available": self._state == "ready"}

    def require_ready(self):
        if self._state != "ready" or self._vault is None:
            raise VaultLockedRefusal()
        return self._vault

    async def lock(self):
        self._generation += 1
        self._initialization_reviews.clear()
        self._unlock_reviews.clear()
        self._vault = None
        self._state, self._code = "locked", "unlock_required"
        try:
            await _callback(self._disable)
        except Exception:
            # Readiness stays closed even if cleanup itself fails.
            self._state, self._code = "unavailable", "hydration_failed"

    def review_unlock(self) -> dict:
        if self._state == "ready" or (self._operation is not None and not self._operation.done()):
            raise VaultInitializationRefusal("operation_in_progress")
        token = str(uuid4())
        self._unlock_reviews[token] = (self._generation, time.monotonic(), self._state, self._code)
        return {"review_token": token, "expires_in_seconds": 300,
                "previous_state": self._state,
                "scope": "Unlock existing encrypted credentials using the existing OS keychain. The OS may ask for access. Successful authentication activates stored credentials locally and may restore the existing encrypted memory database to its plaintext working database. No keychain reset, recovery, fresh initialization, model request or federation startup is performed. Previously blocked full agent bootstrap remains a separate pending action."}

    def cancel_unlock_review(self, token: str):
        self._unlock_reviews.pop(token, None)

    async def confirm_unlock(self, token: str, *, timeout: float = 5.0) -> dict:
        held = self._unlock_reviews.pop(token, None)
        if held is None or held[0] != self._generation or time.monotonic() - held[1] > 300 or held[2:] != (self._state, self._code):
            raise VaultInitializationRefusal("review_expired")
        if self._operation is not None and not self._operation.done():
            raise VaultInitializationRefusal("operation_in_progress")
        self._unlock_reviews.clear()
        return await self.unlock(timeout=timeout)

    async def unlock(self, *, timeout: float = 5.0) -> dict:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
            raise ValueError("timeout must be finite and between zero and 120 seconds")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("VaultCoordinator must stay on its owning event loop")
        self._loop = loop
        if self._state == "ready":
            return self.status()
        if self._operation is None or self._operation.done():
            self._initialization_reviews.clear()
            self._unlock_reviews.clear()
            self._state, self._code = "unlocking", "unlock_pending"
            self._operation = loop.create_task(self._run(self._generation))
        # Shield both timeout and caller cancellation. Cancelling a thread's
        # waiter cannot cancel an OS keychain dialog or syscall.
        try:
            await asyncio.wait_for(asyncio.shield(self._operation), timeout)
        except asyncio.TimeoutError:
            pass
        return self.status()

    def review_initialization(self) -> VaultInitializationReview:
        # Review is passive: the fresh-artifact and keychain checks run only
        # after confirmation. Never inspect OS keys while constructing UI.
        if self._initializer is None:
            raise VaultInitializationRefusal("initialization_unsupported")
        if self._state == "ready" or (self._operation is not None and not self._operation.done()):
            raise VaultInitializationRefusal("operation_in_progress")
        review = VaultInitializationReview(str(uuid4()), self._generation, time.monotonic())
        self._initialization_reviews[review.token] = review
        return review

    def cancel_initialization(self, review: VaultInitializationReview):
        self._initialization_reviews.pop(review.token, None)

    async def initialize(self, review: VaultInitializationReview, *, timeout: float = 5.0) -> dict:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
            raise ValueError("timeout must be finite and between zero and 120 seconds")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("VaultCoordinator must stay on its owning event loop")
        self._loop = loop
        held = self._initialization_reviews.pop(review.token, None)
        if held is None or held != review or held.generation != self._generation or time.monotonic() - held.created_uptime > 300:
            raise VaultInitializationRefusal("review_expired")
        if self._initializer is None or self._state == "ready" or (self._operation is not None and not self._operation.done()):
            raise VaultInitializationRefusal("operation_in_progress")
        self._initialization_reviews.clear()
        self._state, self._code = "unlocking", "unlock_pending"
        self._operation = loop.create_task(self._run(self._generation, self._initializer))
        try:
            await asyncio.wait_for(asyncio.shield(self._operation), timeout)
        except asyncio.TimeoutError:
            pass
        return self.status()

    async def _run(self, generation: int, factory: Callable[[], Any] | None = None):
        result = Future()

        def worker():
            try:
                result.set_result((factory or self._factory)())
            except BaseException as exc:
                result.set_exception(exc)

        # Mark running before publishing the worker. Even event-loop shutdown
        # cannot cancel the concurrent Future while its OS call still runs.
        result.set_running_or_notify_cancel()
        threading.Thread(target=worker, name="vault-explicit-unlock", daemon=True).start()
        try:
            vault = await asyncio.wrap_future(result)
            if generation != self._generation:
                return
            if await _callback(self._hydrate, vault) is False:
                raise _Unavailable("hydration_failed")
            if generation != self._generation:
                # Lock happened during hydration; remove anything hydrated.
                await _callback(self._disable)
                return
            self._vault = vault
            self._state, self._code = "ready", "ready"
        except Exception as exc:
            if generation != self._generation:
                # A cancelled generation may have partially hydrated before
                # failing. Clear dependent state again after it has finished.
                try:
                    await _callback(self._disable)
                except Exception:
                    self._state, self._code = "unavailable", "hydration_failed"
                return
            self._vault = None
            from security.vault import VaultTamperedError, VaultFormatError
            if isinstance(exc, _Unavailable):
                code = exc.code
            elif isinstance(exc, (VaultTamperedError, VaultFormatError)):
                code = "authentication_failed"
            else:
                code = "unlock_failed"
            self._state, self._code = "unavailable", code
            try:
                await _callback(self._disable)
            except Exception:
                self._code = "hydration_failed"


def _macos_add_master_key_no_replace(encoded_key: str) -> None:
    """Explicit initialization only: add to an existing keychain, never update.

    Security.framework's add operation fails on a duplicate shared master key;
    keyring.set_password normally updates it and cannot provide that guarantee.
    """
    import ctypes
    import sys
    from security.vault import KEYRING_SERVICE, KEYRING_USERNAME, _macos_default_keychain_state
    if sys.platform != "darwin":
        raise _Unavailable("initialization_unsupported")
    status, exists = _macos_default_keychain_state()
    if status != 0 or not exists:
        raise _Unavailable("key_unavailable")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    copy_default = security.SecKeychainCopyDefault
    copy_default.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    copy_default.restype = ctypes.c_int32
    reference = ctypes.c_void_p()
    status = int(copy_default(ctypes.byref(reference)))
    if status != 0 or not reference.value:
        raise _Unavailable("key_unavailable")
    release = core.CFRelease
    release.argtypes = [ctypes.c_void_p]; release.restype = None
    add = security.SecKeychainAddGenericPassword
    add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                    ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                    ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    add.restype = ctypes.c_int32
    try:
        # Recheck the actual reference's path before Add. Passing a non-null
        # reference prevents Add from creating a missing default keychain.
        get_path = security.SecKeychainGetPath
        get_path.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p]
        get_path.restype = ctypes.c_int32
        path = ctypes.create_string_buffer(4096); length = ctypes.c_uint32(len(path))
        import os
        if get_path(reference, ctypes.byref(length), path) != 0 or not path.value or not Path(os.fsdecode(path.value)).is_file():
            raise _Unavailable("key_unavailable")
        service, user, password = KEYRING_SERVICE.encode(), KEYRING_USERNAME.encode(), encoded_key.encode("ascii")
        status = int(add(reference, len(service), service, len(user), user, len(password), password, None))
        if status == -25299:  # errSecDuplicateItem: preserve the existing key.
            raise _Unavailable("key_already_present")
        if status != 0:
            raise _Unavailable("key_unavailable")
    finally:
        release(reference)


def initialization_factory(vault_path: str | Path, *, add_master_key: Callable[[str], Any] | None = None):
    """Factory for a separately reviewed fresh empty-vault initialization.

    add_master_key must atomically add-if-absent, never replace an existing
    entry. Production defaults to the macOS Security add operation; Linux
    fails closed until an equivalent backend is supplied. After an OS key
    write, any disk failure is a partial result: we never delete/reset keys.
    Parent directory must already exist; all ancestors are opened nofollow.
    """
    def create_empty():
        import os
        import json
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from security.vault import _AEAD_AAD, _VAULT_VERSION
        target = Path(vault_path)
        if not target.is_absolute() or ".." in target.parts or target.suffix != ".json":
            raise _Unavailable("initialization_unsupported")
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in target.parent.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor); descriptor = child
            encrypted = target.with_suffix(".enc").name
            reserved = [target.name, encrypted, encrypted + ".prev", encrypted + ".new", target.name + ".bak.legacy"]
            def ensure_absent():
                for name in reserved:
                    try: os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError: continue
                    raise _Unavailable("vault_artifacts_present")
            ensure_absent()
            key = ChaCha20Poly1305.generate_key()
            nonce = os.urandom(12)
            payload = json.dumps({"version": _VAULT_VERSION, "data": {"credentials": {}}}, separators=(",", ":")).encode()
            encrypted_payload = ChaCha20Poly1305(key).encrypt(nonce, payload, _AEAD_AAD)
            # Prove generated ciphertext/key agreement before any key write.
            assert ChaCha20Poly1305(key).decrypt(nonce, encrypted_payload, _AEAD_AAD) == payload
            (add_master_key or _macos_add_master_key_no_replace)(base64.b64encode(key).decode("ascii"))
            try:
                ensure_absent()
                fd = os.open(encrypted, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
                with os.fdopen(fd, "w+b") as output:
                    blob = nonce + encrypted_payload
                    output.write(blob); output.flush(); os.fsync(output.fileno())
                    output.seek(0)
                    persisted = output.read(len(blob) + 1)
                    if persisted != blob or ChaCha20Poly1305(key).decrypt(persisted[:12], persisted[12:], _AEAD_AAD) != payload:
                        raise RuntimeError("Initialization readback mismatch")
                os.fsync(descriptor)
            except Exception as exc:
                # Never compensate with key deletion: another app/profile may
                # already depend on the added shared entry. Report partial.
                raise _Unavailable("initialization_partial") from exc
            # Authenticate the exact in-memory blob without a second keychain
            # read. Opening again later remains the normal read-only factory.
            from security.vault import BlindVault
            class NewlyInitializedVault(BlindVault):
                _explicit_deferred_unlock = True
                def _master_key(self): return key
                def _load(self):
                    self._data = self._normalise_namespaces(self._decrypt_blob(persisted, key=key))
            return NewlyInitializedVault(vault_path=str(target))
        finally:
            os.close(descriptor)
    return create_empty


class DeferredBootVaultFacade:
    """Truthy OAuth boot adapter: no plaintext fallback and no stored reads.

    Only boot loaders get neutral empty reads while locked; public credential
    routes must call coordinator.require_ready and return a typed refusal.
    Every write still requires authenticated readiness. Once ready, reads
    delegate to the authenticated vault without opening another keychain.
    """
    def __init__(self, coordinator: VaultCoordinator): self.coordinator = coordinator
    def retrieve(self, key, requester="executor"):
        if not self.coordinator.status()["credentials_available"]: return None
        return self.coordinator.require_ready().retrieve(key, requester=requester)
    def get(self, namespace, key, **kwargs):
        if not self.coordinator.status()["credentials_available"]: return None
        return self.coordinator.require_ready().get(namespace, key, **kwargs)
    def list_keys(self):
        if not self.coordinator.status()["credentials_available"]: return []
        return self.coordinator.require_ready().list_keys()
    def __getattr__(self, name):
        return getattr(self.coordinator.require_ready(), name)
