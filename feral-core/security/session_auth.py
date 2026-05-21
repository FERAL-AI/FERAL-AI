"""
FERAL Session Authentication
==============================
Token-based authentication for WebSocket sessions (/v1/session).

Tokens are 32-char hex strings persisted in ~/.feral/session_token.
Localhost connections can optionally bypass auth (FERAL_LOCAL_BYPASS).

**audit-r12 A1 (shipped in v2026.5.38):** ``FERAL_LOCAL_BYPASS`` now
defaults to ``False``. Loopback (127.0.0.1 / ::1) continues to bypass
auth via the explicit loopback exception in the middleware so a fresh
``feral serve`` against the default ``127.0.0.1`` bind keeps working
without configuration. LAN / Tailscale / 0.0.0.0 binds now reject
unauthenticated requests by default. Opt back in for dev with
``FERAL_LOCAL_BYPASS=1``; boot will then emit a loud warning whenever
the brain is bound to a non-loopback address.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral.session_auth")


def _token_path() -> Path:
    home = os.environ.get("FERAL_HOME", str(Path.home() / ".feral"))
    return Path(home) / "session_token"


def generate_session_token() -> str:
    """Generate a cryptographically-secure 32-char hex token."""
    return secrets.token_hex(16)


def save_session_token(token: str) -> None:
    """Persist *token* to ~/.feral/session_token with 0600 permissions."""
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    logger.info("Session token saved to %s", path)


def load_session_token() -> Optional[str]:
    """Read the saved session token, or return ``None`` if absent."""
    path = _token_path()
    if not path.exists():
        return None
    text = path.read_text().strip()
    return text or None


def verify_session(token: str) -> bool:
    """Return ``True`` if *token* matches the persisted session token."""
    stored = load_session_token()
    if stored is None:
        return False
    return secrets.compare_digest(stored, token)


def session_auth_required() -> bool:
    """Decide whether incoming WebSocket sessions must authenticate.

    Auth is required when FERAL_SESSION_AUTH=true **or** a token file
    already exists on disk.
    """
    if os.environ.get("FERAL_SESSION_AUTH", "").lower() == "true":
        return True
    return _token_path().exists()


def is_localhost(host: str | None) -> bool:
    """Return ``True`` if *host* is a loopback address."""
    if not host:
        return False
    return host in ("127.0.0.1", "::1", "localhost")


def local_bypass_enabled() -> bool:
    """Whether localhost connections skip session auth (default ``False``).

    audit-r12 A1 — flipped to secure-by-default in v2026.5.38. The
    middleware separately permits loopback requests so the default
    127.0.0.1 bind still works without auth. Set
    ``FERAL_LOCAL_BYPASS=1`` (alongside ``FERAL_LOCAL_BYPASS=true`` /
    ``yes``) only in dev when running off-loopback (LAN/Tailscale)
    and you accept the trust posture.
    """
    return os.environ.get("FERAL_LOCAL_BYPASS", "false").lower() in ("true", "1", "yes")


def warn_if_unsafe_bypass(bind_host: str) -> Optional[str]:
    """Emit a loud warning when ``FERAL_LOCAL_BYPASS`` is on for a
    non-loopback bind.

    Returns the warning message that was logged (or ``None`` if the
    configuration is safe). Callers (boot path, ``feral doctor``)
    surface this so operators see the trust degradation immediately.
    """
    if not local_bypass_enabled():
        return None
    if is_localhost(bind_host) or bind_host in ("", None):
        return None
    msg = (
        "FERAL_LOCAL_BYPASS=1 with bind_host=%s — UNAUTHENTICATED ACCESS "
        "from every host that can reach this address. Set "
        "FERAL_LOCAL_BYPASS=0 (default) for production; this opt-in only "
        "makes sense on a trusted single-user dev box."
    ) % bind_host
    logger.warning(msg)
    return msg
