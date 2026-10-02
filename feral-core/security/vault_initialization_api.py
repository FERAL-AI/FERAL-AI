"""Reviewed fresh-vault API controller, kept separate from passive unlock.

Not enabled by construction. Release integration must explicitly configure the
same coordinator's secure initializer and accept the signed macOS release gate.
No status/review operation queries a keychain or reads credential content.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from security.vault_coordinator import (
    VaultCoordinator,
    VaultInitializationRefusal,
    initialization_factory,
)


@dataclass(frozen=True)
class BoundVaultInitializer:
    """Immutable reviewed target around the existing secure initializer.

    Root passes this same object into VaultCoordinator(fresh_initializer=...).
    The controller can then verify its reviewed directory actually belongs to
    the callable being dispatched, without trusting a second bare path claim.
    """

    vault_path: Path
    add_master_key: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "vault_path", Path(self.vault_path))

    def __call__(self):
        return initialization_factory(
            self.vault_path, add_master_key=self.add_master_key
        )()


class VaultInitializationAPIRefusal(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _StorageSnapshot:
    directory_device: int
    directory_inode: int
    artifacts: tuple[bool, ...]


class VaultInitializationAPI:
    """One-use, fresh-storage-reviewed adapter around one coordinator.

    ``signed_release_accepted`` is a release integration gate, never a request
    parameter. ``atomic_platform_verified`` is reserved for a future verified
    non-macOS atomic add backend; merely injecting a keyring setter is not that.
    """

    def __init__(
        self,
        coordinator: VaultCoordinator,
        vault_path: str | Path,
        *,
        signed_release_accepted: bool = False,
        platform: str | None = None,
        atomic_platform_verified: bool = False,
    ):
        self.coordinator = coordinator
        self.vault_path = Path(vault_path)
        self.signed_release_accepted = signed_release_accepted is True
        self.platform = platform if platform is not None else sys.platform
        self.atomic_platform_verified = atomic_platform_verified is True
        self._reviews: dict[str, tuple[object, _StorageSnapshot, float, object]] = {}

    def _storage_snapshot(self):
        target = self.vault_path
        if (
            not target.is_absolute()
            or target.name != "credentials.json"
            or ".." in target.parts
        ):
            raise VaultInitializationAPIRefusal(
                "storage_unavailable",
                "The app-owned vault storage cannot be reviewed safely.",
            )
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in target.parent.parts[1:]:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            directory = os.fstat(descriptor)
            encrypted = target.with_suffix(".enc").name
            present = []
            for name in (
                target.name,
                encrypted,
                encrypted + ".prev",
                encrypted + ".new",
                target.name + ".bak.legacy",
            ):
                try:
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    present.append(True)
                except FileNotFoundError:
                    present.append(False)
            return _StorageSnapshot(directory.st_dev, directory.st_ino, tuple(present))
        finally:
            os.close(descriptor)

    def status(self) -> dict:
        coordinator = self.coordinator.status()
        artifacts = False
        try:
            snapshot = self._storage_snapshot()
            artifacts = any(snapshot.artifacts)
            storage_safe = True
        except (OSError, VaultInitializationAPIRefusal):
            storage_safe = False
        initializer = getattr(self.coordinator, "_initializer", None)
        configured = (
            isinstance(initializer, BoundVaultInitializer)
            and initializer.vault_path == self.vault_path
        )
        platform_supported = self.platform == "darwin" or self.atomic_platform_verified
        if not self.signed_release_accepted:
            code, message = (
                "release_acceptance_required",
                "Fresh keychain initialization awaits signed-app acceptance. Local use remains available without a vault.",
            )
        elif not platform_supported:
            code, message = (
                "platform_unsupported",
                "This platform has no verified secure add-if-absent key storage. No weaker fallback is used.",
            )
        elif not configured:
            code, message = (
                "initializer_not_configured",
                "Secure initialization is not configured for this agent. Local use does not require a vault.",
            )
        elif coordinator["in_flight"]:
            code, message = (
                "operation_in_progress",
                "An existing vault operation is still running. No second keychain operation will start.",
            )
        elif coordinator["credentials_available"]:
            code, message = (
                "vault_ready",
                "Credential storage is already authenticated. Fresh initialization is unnecessary.",
            )
        elif not storage_safe:
            code, message = (
                "storage_unavailable",
                "The app-owned vault directory cannot be reviewed safely. No keychain operation was attempted.",
            )
        elif coordinator["code"] == "initialization_partial":
            code, message = (
                "initialization_partial",
                "A key may have been added before vault persistence failed. No automatic key deletion or reset will be attempted.",
            )
        elif artifacts:
            code, message = (
                "vault_artifacts_present",
                "Existing vault artifacts were found. Initialization will not replace, migrate or reset them.",
            )
        elif coordinator["code"] == "key_already_present":
            code, message = (
                "key_already_present",
                "An existing shared master key must not be replaced. Inspect the existing vault instead.",
            )
        else:
            code, message = (
                "available",
                "A fresh empty encrypted vault can be reviewed. Existing OS master keys have not been queried; a duplicate key will be refused.",
            )
        return {
            "supported": code == "available",
            "code": code,
            "message": message,
            "in_flight": coordinator["in_flight"],
            "credential_storage_available": coordinator["credentials_available"],
            "existing_artifacts": artifacts,
            "requires_signed_acceptance": not self.signed_release_accepted,
            "local_use_requires_vault": False,
        }

    def review(self) -> dict:
        now = time.monotonic()
        for token, held in list(self._reviews.items()):
            if now - held[2] > 300:
                self.cancel(token)
        if len(self._reviews) >= 16:
            raise VaultInitializationAPIRefusal(
                "too_many_reviews",
                "Cancel an earlier initialization review before preparing another.",
            )
        status = self.status()
        if not status["supported"]:
            raise VaultInitializationAPIRefusal(status["code"], status["message"])
        snapshot = self._storage_snapshot()
        try:
            review = self.coordinator.review_initialization()
        except VaultInitializationRefusal as exc:
            raise VaultInitializationAPIRefusal(
                exc.code, "Vault state changed. Review initialization again."
            ) from None
        self._reviews[review.token] = (
            review,
            snapshot,
            time.monotonic(),
            self.coordinator._initializer,
        )
        return {
            "review_token": review.token,
            "expires_in_seconds": 300,
            "scope": review.scope
            + " No cloud key or model request is submitted. Local operation does not require completing this setup. Signed OS behavior remains a release acceptance responsibility.",
            "previous_status": status,
        }

    def cancel(self, token: str):
        held = self._reviews.pop(token, None)
        if held is not None:
            self.coordinator.cancel_initialization(held[0])

    async def confirm(self, token: str, *, timeout: float = 5.0) -> dict:
        held = self._reviews.pop(token, None)
        if held is None or time.monotonic() - held[2] > 300:
            if held is not None:
                self.coordinator.cancel_initialization(held[0])
            raise VaultInitializationAPIRefusal(
                "review_expired",
                "Initialization review expired or was already consumed.",
            )
        review, previous, _, initializer = held
        try:
            # Re-read the directory identity and every reserved filename
            # immediately before dispatch; absence claims are never immutable.
            status = self.status()
            if (
                not status["supported"]
                or self.coordinator._initializer is not initializer
                or self._storage_snapshot() != previous
            ):
                raise VaultInitializationAPIRefusal(
                    "storage_changed",
                    "Vault state or storage changed. Review again; no initialization was dispatched.",
                )
            await self.coordinator.initialize(review, timeout=timeout)
        except VaultInitializationRefusal as exc:
            raise VaultInitializationAPIRefusal(
                exc.code, "Vault state changed. Review initialization again."
            ) from None
        except OSError:
            raise VaultInitializationAPIRefusal(
                "storage_changed",
                "Vault storage could not be revalidated; no initialization was dispatched.",
            ) from None
        finally:
            # If preflight refuses, the coordinator's own token must also be
            # revoked. If already dispatched, cancellation cannot undo it.
            self.coordinator.cancel_initialization(review)
        vault = self.coordinator.status()
        return {
            "ok": vault["credentials_available"],
            "operation": "initialize",
            "review_token": token,
            "status": self.status(),
            "vault_status": vault,
        }
