"""FERAL ``security`` package — lazy facade.

R2-001 (round-2 of Wave 1) — historically this module eagerly imported
``fetch_guard`` (which transitively imports ``httpx`` at ~85ms) and
``vault`` + ``session_auth`` + ``device_pairing``. That made
``from security import vault`` cost ~95ms even when the caller only
wanted the type hint, and pushed ``python -c "import security.vault"``
over the 200ms gate enforced by CI.

The PEP-562 module-level ``__getattr__`` below preserves every existing
public name as a lazy attribute: callers that do ``from security
import safe_fetch`` still get the symbol they asked for, but only the
sub-modules they actually touch are imported. Pure ``import security``
or ``from security import vault`` now pays only the per-package cost.

If you add a new public re-export, append it to ``_LAZY`` so the
attribute resolves correctly. ``__all__`` mirrors the list so
``from security import *`` and IDE auto-completion keep working.
"""

from __future__ import annotations

from typing import Any

_LAZY: dict[str, tuple[str, str]] = {
    # vault
    "BlindVault": ("security.vault", "BlindVault"),
    "PermissionTier": ("security.vault", "PermissionTier"),
    "ExecutionSandbox": ("security.vault", "ExecutionSandbox"),
    "VaultError": ("security.vault", "VaultError"),
    "VaultKeyUnavailableError": ("security.vault", "VaultKeyUnavailableError"),
    "VaultTamperedError": ("security.vault", "VaultTamperedError"),
    "VaultFormatError": ("security.vault", "VaultFormatError"),
    "get_vault": ("security.vault", "get_vault"),
    "reset_vault": ("security.vault", "reset_vault"),
    "encode_recovery_code": ("security.vault", "encode_recovery_code"),
    "decode_recovery_code": ("security.vault", "decode_recovery_code"),
    # fetch_guard
    "safe_fetch": ("security.fetch_guard", "safe_fetch"),
    "validate_url": ("security.fetch_guard", "validate_url"),
    # session_auth
    "generate_session_token": ("security.session_auth", "generate_session_token"),
    "save_session_token": ("security.session_auth", "save_session_token"),
    "load_session_token": ("security.session_auth", "load_session_token"),
    "verify_session": ("security.session_auth", "verify_session"),
    # device_pairing
    "DevicePairingStore": ("security.device_pairing", "DevicePairingStore"),
    # audit_log (new in v2026.5.38)
    "audit_event": ("security.audit_log", "audit_event"),
    "audit_event_or_raise": ("security.audit_log", "audit_event_or_raise"),
    "recent_events": ("security.audit_log", "recent_events"),
    "audit_log_path": ("security.audit_log", "audit_log_path"),
    "AuditFailure": ("security.audit_log", "AuditFailure"),
}

__all__ = sorted(_LAZY.keys())


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'security' has no attribute {name!r}")
    module_name, attr_name = target
    from importlib import import_module
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return list(__all__) + list(globals().keys())
