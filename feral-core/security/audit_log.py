"""
FERAL audit log — fail-loud credential audit trail.

audit-r12 A5 (v2026.5.38) — credential operations on
:class:`security.vault.BlindVault` write a JSON-Lines audit trail to
``~/.feral/audit.log``. The pre-fix path swallowed ``OSError`` on write
with a ``logger.warning(...)`` and let the credential op succeed
silently. That violates the security-perimeter promise: a write that
left no audit record might as well have been a stealth credential
read for the operator who later combs the log looking for the breach.

This module extracts the inline audit helper from ``vault.py`` into a
single fail-loud surface:

* :func:`audit_event` writes a JSON line. On any I/O failure it raises
  :class:`AuditFailure` so the caller can decide what to do (vault
  callers in v2026.5.38 propagate the exception up to the API caller
  rather than logging and continuing).
* Callers may use :func:`audit_event_or_raise` for the same behaviour
  with a slightly more explicit name in mixed code paths.
* :func:`recent_events` reads the tail for the REST surface and the
  CLI ``feral key status --audit``; on read failure it raises
  :class:`AuditFailure` so a dashboard query can surface "audit log
  unreadable" instead of "no events".

The audit log path is :func:`audit_log_path` and respects
``FERAL_AUDIT_LOG_PATH`` for tests (mirrors the existing ``FERAL_HOME``
convention without forcing the entire home tree into the temp dir).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("feral.audit_log")


class AuditFailure(RuntimeError):
    """The audit log could not be appended/read.

    Vault callers MUST NOT swallow this exception — the whole point of
    audit-r12 A5 is that a credential operation without an audit
    record is itself a security incident.
    """


def _feral_home() -> Path:
    return Path(os.environ.get("FERAL_HOME", str(Path.home() / ".feral")))


def audit_log_path() -> Path:
    """Resolve the audit-log path.

    Precedence: ``FERAL_AUDIT_LOG_PATH`` env > ``$FERAL_HOME/audit.log``
    > ``~/.feral/audit.log``. Tests prefer ``FERAL_AUDIT_LOG_PATH``
    when they want to isolate audit writes from the rest of the home
    directory.
    """
    explicit = os.environ.get("FERAL_AUDIT_LOG_PATH")
    if explicit:
        return Path(explicit)
    return _feral_home() / "audit.log"


def audit_event(
    action: str,
    key: str,
    actor: str,
    *,
    namespace: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Append a JSON-Lines audit record.

    The wire shape matches the pre-extraction inline ``BlindVault._audit``
    so existing consumers (REST ``GET /api/security/audit``, the
    WebUI v2 Settings → Audit log panel that reads ``e.key``,
    ``e.action``, ``e.actor``, ``e.ts``) keep working without any
    front-end change.

    Returns the dict that was written so callers can use the same
    payload for additional logging/telemetry without re-marshalling.

    Raises :class:`AuditFailure` on any I/O error (missing parent dir
    that can't be created, read-only filesystem, full disk, …).
    """
    if not action or not isinstance(action, str):
        raise AuditFailure("audit_event: action must be a non-empty string")
    if not key or not isinstance(key, str):
        raise AuditFailure("audit_event: key must be a non-empty string")
    if not actor or not isinstance(actor, str):
        raise AuditFailure("audit_event: actor must be a non-empty string")

    entry: dict[str, Any] = {
        "ts": time.time(),
        "action": action,
        "key": key,
        "actor": actor,
    }
    if namespace is not None:
        entry["namespace"] = namespace
    for k, value in extra.items():
        if k in entry:
            continue
        entry[k] = value

    path = audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AuditFailure(
            f"audit log parent {path.parent} could not be created: {exc}"
        ) from exc

    line = json.dumps(entry, separators=(",", ":")) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    except OSError as exc:
        raise AuditFailure(
            f"audit log write to {path} failed: {exc}"
        ) from exc

    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
    except OSError:
        pass

    return entry


def audit_event_or_raise(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias for :func:`audit_event` for callers that want the explicit
    "fail-loud" name at the call site."""
    return audit_event(*args, **kwargs)


def recent_events(limit: int = 100) -> list[dict[str, Any]]:
    """Return the last ``limit`` audit records (newest last).

    Returns an empty list when the audit log does not yet exist (no
    credentials have been written). Raises :class:`AuditFailure` when
    the log exists but cannot be read or parsed — callers should
    surface that to the operator instead of pretending the log is
    empty.
    """
    if limit <= 0:
        return []
    path = audit_log_path()
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditFailure(f"audit log read from {path} failed: {exc}") from exc

    lines = [ln for ln in text.splitlines() if ln.strip()]
    tail = lines[-limit:]
    out: list[dict[str, Any]] = []
    for ln in tail:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError as exc:
            raise AuditFailure(
                f"audit log {path} contains malformed JSON: {exc}"
            ) from exc
    return out


def _iter_events_raw(path: Path) -> Iterable[str]:
    """Test helper: stream raw lines (no parse). Importable but not
    exported; kept for tooling that doesn't want the parse cost."""
    if not path.exists():
        return iter(())
    return iter(path.read_text(encoding="utf-8").splitlines())
