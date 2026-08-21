"""Remove the plaintext credential backup once the vault provably has the keys.

``BlindVault._migrate_from_plaintext`` encrypts ``credentials.json`` into
``credentials.enc`` and copies the original to
``credentials.json.bak.legacy`` at mode 0600. That copy is a deliberate
safety net for a one-way, unrecoverable encryption step: FERAL has no
escrow, so losing the keychain entry and the recovery code means the
vault cannot be opened and the operator must re-enter every credential.

Nothing ever removes the copy. Grep the tree: ``bak.legacy`` appears only
inside vault.py, where it is written. So the safety net is permanent, and
on a live install it was found four months later still holding six usable
provider keys in plaintext.

This is the second half of that migration, and it is deliberately timid:

* if the vault cannot be opened, do nothing, because the backup is then
  the only copy and deleting it would destroy the credentials outright
* if any key in the backup is missing from the vault, do nothing and name
  the keys, because a partial migration is exactly when the backup earns
  its keep
* only when every key is present and readable is the plaintext removed

Re-running is harmless: an absent backup is a no-op.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("feral.migrations")

# Namespaces the vault may hold a provider key under. Checked in order.
_NAMESPACES = ("default", "providers", "user")


def _backup_path() -> Path:
    from config.loader import feral_home
    return feral_home() / "credentials.json.bak.legacy"


def _read_backup(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"backup is unreadable, leaving it alone: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("backup is not a JSON object, leaving it alone")
    return data


def _vault_has(vault, key: str) -> bool:
    """True when the vault can return a non-empty value for *key*."""
    for namespace in _NAMESPACES:
        try:
            if vault.get(namespace, key, requester="migration"):
                return True
        except Exception:
            continue
    try:
        return bool(vault.retrieve(key, requester="migration"))
    except Exception:
        return False


# This is a sweep, not a one-time step, so the runner never marks it
# applied. It runs before state.init(), and the file it deletes is
# written LATER IN THE SAME BOOT by BlindVault._migrate_from_plaintext,
# lazily, at first unlock. Marked applied on a fresh install it would
# find nothing, be recorded as done, and never run again, leaving the
# plaintext credentials it exists to remove on disk forever.
RECURRING = True


def migrate():
    path = _backup_path()
    if not path.exists():
        return ("no legacy credential backup present", False)

    stored = _read_backup(path)
    if not stored:
        path.unlink()
        return ("removed an empty legacy credential backup", True)

    try:
        from security.vault import BlindVault
        vault = BlindVault()
    except Exception as exc:
        # The backup is the only copy. Say so and stop.
        raise RuntimeError(
            f"vault unavailable ({exc}); keeping {path.name}, which is "
            "currently the only copy of those credentials"
        ) from exc

    missing = [key for key in sorted(stored) if not _vault_has(vault, key)]
    if missing:
        raise RuntimeError(
            f"{len(missing)} of {len(stored)} keys are not readable from the "
            f"vault ({', '.join(missing)}); keeping {path.name} until they are"
        )

    path.unlink()
    logger.info(
        "removed %s after confirming all %d keys are readable from the vault",
        path.name, len(stored),
    )
    return (
        f"removed {path.name} after confirming all {len(stored)} keys "
        "are readable from the encrypted vault",
        True,
    )
