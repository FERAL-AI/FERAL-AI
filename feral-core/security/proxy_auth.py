"""Fail-closed authentication for a trusted reverse proxy.

This module deliberately does not inspect ``X-Forwarded-For``.  The caller
must pass the socket peer address reported by the ASGI server; forwarded
headers are untrusted input unless a separate, explicitly trusted proxy has
already rewritten the connection metadata.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlsplit


class ProxyAuthError(ValueError):
    """Raised when proxy authentication is disabled or configuration fails."""


@dataclass(frozen=True)
class ProxyAuthConfig:
    """Explicit configuration for proxy-authenticated browser access."""

    enabled: bool = False
    trusted_proxies: tuple[str, ...] = ()
    shared_secret: str = ""
    secret_header: str = "X-FERAL-Proxy-Secret"
    identity_header: str = "X-FERAL-Proxy-User"
    groups_header: str = "X-FERAL-Proxy-Groups"
    allowed_users: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    def validated(self) -> "ProxyAuthConfig":
        """Validate all security-critical settings, failing closed."""
        if not self.enabled:
            raise ProxyAuthError("proxy authentication is disabled")
        if not self.shared_secret:
            raise ProxyAuthError("proxy authentication secret is missing")
        if not self.trusted_proxies:
            raise ProxyAuthError("trusted proxy list is missing")
        for value in self.trusted_proxies:
            try:
                if "/" in value:
                    ipaddress.ip_network(value, strict=False)
                else:
                    ipaddress.ip_address(value)
            except ValueError as exc:
                raise ProxyAuthError(f"invalid trusted proxy entry: {value!r}") from exc
        for name in (self.secret_header, self.identity_header, self.groups_header):
            if not name or name.lower() in {"authorization", "cookie", "host", "origin"}:
                raise ProxyAuthError("invalid proxy-auth header configuration")
        if not self.allowed_origins:
            raise ProxyAuthError("allowed origin list is missing")
        if any(not _valid_origin(origin) for origin in self.allowed_origins):
            raise ProxyAuthError("invalid canonical allowed origin")
        return self


@dataclass(frozen=True)
class ProxyIdentity:
    """The authenticated identity supplied by the trusted proxy."""

    user: str
    groups: tuple[str, ...]
    authenticated: bool = True
    source: str = "trusted-proxy"


def config_from_env(env: Optional[Mapping[str, str]] = None) -> ProxyAuthConfig:
    """Build configuration from environment without silently enabling auth."""
    values = os.environ if env is None else env
    enabled = values.get("FERAL_PROXY_AUTH_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    def split(key: str) -> tuple[str, ...]:
        return tuple(x.strip() for x in values.get(key, "").split(",") if x.strip())
    return ProxyAuthConfig(
        enabled=enabled,
        trusted_proxies=split("FERAL_PROXY_AUTH_TRUSTED_PROXIES"),
        shared_secret=values.get("FERAL_PROXY_AUTH_SECRET", ""),
        secret_header=values.get("FERAL_PROXY_AUTH_SECRET_HEADER", "X-FERAL-Proxy-Secret"),
        identity_header=values.get("FERAL_PROXY_AUTH_IDENTITY_HEADER", "X-FERAL-Proxy-User"),
        groups_header=values.get("FERAL_PROXY_AUTH_GROUPS_HEADER", "X-FERAL-Proxy-Groups"),
        allowed_users=split("FERAL_PROXY_AUTH_ALLOWED_USERS"),
        allowed_groups=split("FERAL_PROXY_AUTH_ALLOWED_GROUPS"),
        allowed_origins=split("FERAL_PROXY_AUTH_ALLOWED_ORIGINS"),
    )


def authenticate_proxy(
    config: ProxyAuthConfig,
    *,
    socket_client_ip: str,
    headers: Mapping[str, str],
) -> ProxyIdentity:
    """Authenticate a request from the socket peer, never from X-Forwarded-For."""
    config.validated()
    if not _ip_trusted(socket_client_ip, config.trusted_proxies):
        raise ProxyAuthError("socket peer is not a trusted proxy")
    supplied = _header(headers, config.secret_header)
    if not supplied or not hmac.compare_digest(supplied, config.shared_secret):
        raise ProxyAuthError("proxy shared secret is invalid")
    user = _header(headers, config.identity_header).strip()
    if not user:
        raise ProxyAuthError("proxy identity is missing")
    groups = tuple(
        dict.fromkeys(
            x.strip()
            for x in _header(headers, config.groups_header).split(",")
            if x.strip()
        )
    )
    if config.allowed_users and user not in config.allowed_users:
        raise ProxyAuthError("proxy identity is not allowed")
    if config.allowed_groups and not set(groups).intersection(config.allowed_groups):
        raise ProxyAuthError("proxy groups are not allowed")
    return ProxyIdentity(user=user, groups=groups)


def authorize_browser_origin(
    config: ProxyAuthConfig,
    *,
    headers: Mapping[str, str],
    method: str = "GET",
    websocket: bool = False,
) -> None:
    """Reject cross-site proxy-auth requests and require Origin when needed.

    Safe non-browser requests may omit ``Origin``. If a browser supplies one,
    it must exactly match a configured canonical origin. Unsafe HTTP methods
    and WebSocket handshakes require an allowed Origin, fail-closed.
    """
    config.validated()
    origin = _header(headers, "Origin").strip()
    required = websocket or method.upper() not in {"GET", "HEAD", "OPTIONS"}
    if not origin:
        if required:
            raise ProxyAuthError("allowed Origin is required")
        return
    if origin not in config.allowed_origins:
        raise ProxyAuthError("Origin is not allowed")


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _ip_trusted(peer: str, entries: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for entry in entries:
        try:
            if "/" in entry:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            elif address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            return False
    return False


def _valid_origin(origin: str) -> bool:
    parsed = urlsplit(origin)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )
