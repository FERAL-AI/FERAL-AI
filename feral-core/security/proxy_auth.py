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
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlsplit


_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_RESERVED_PROXY_HEADERS = {
    "authorization",
    "connection",
    "cookie",
    "forwarded",
    "host",
    "origin",
    "proxy-authorization",
    "sec-fetch-site",
    "transfer-encoding",
    "x-forwarded-for",
    "x-original-uri",
    "x-original-url",
}
_MAX_SECRET_LENGTH = 4096
_MAX_IDENTITY_LENGTH = 512
_MAX_GROUP_HEADER_LENGTH = 16384
_MAX_GROUP_COUNT = 256


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
    groups_separator: str = "|"
    allowed_users: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    def validated(self) -> "ProxyAuthConfig":
        """Validate all security-critical settings, failing closed."""
        if not self.enabled:
            raise ProxyAuthError("proxy authentication is disabled")
        if (
            not self.shared_secret.strip()
            or len(self.shared_secret) > _MAX_SECRET_LENGTH
            or _has_control_character(self.shared_secret)
        ):
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
        header_names = (self.secret_header, self.identity_header, self.groups_header)
        for name in header_names:
            if (
                not name
                or not _HEADER_NAME_RE.fullmatch(name)
                or name.lower() in _RESERVED_PROXY_HEADERS
            ):
                raise ProxyAuthError("invalid proxy-auth header configuration")
        if len({name.lower() for name in header_names}) != len(header_names):
            raise ProxyAuthError("proxy-auth headers must be distinct")
        if (
            len(self.groups_separator) != 1
            or self.groups_separator.isspace()
            or not self.groups_separator.isprintable()
        ):
            raise ProxyAuthError("invalid proxy group separator")
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
        groups_separator=values.get("FERAL_PROXY_AUTH_GROUPS_SEPARATOR", "|"),
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
    if (
        not user
        or len(user) > _MAX_IDENTITY_LENGTH
        or _has_control_character(user)
    ):
        raise ProxyAuthError("proxy identity is missing or invalid")
    raw_groups = _header(headers, config.groups_header)
    if len(raw_groups) > _MAX_GROUP_HEADER_LENGTH:
        raise ProxyAuthError("proxy groups assertion is too large")
    groups = tuple(
        dict.fromkeys(
            x.strip()
            for x in raw_groups.split(config.groups_separator)
            if x.strip()
        )
    )
    if len(groups) > _MAX_GROUP_COUNT or any(
        len(group) > _MAX_IDENTITY_LENGTH or _has_control_character(group)
        for group in groups
    ):
        raise ProxyAuthError("proxy groups assertion is invalid")
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
    fetch_site = _header(headers, "Sec-Fetch-Site").strip().lower()
    fetch_mode = _header(headers, "Sec-Fetch-Mode").strip().lower()
    fetch_dest = _header(headers, "Sec-Fetch-Dest").strip().lower()
    required = websocket or method.upper() not in {"GET", "HEAD", "OPTIONS"}
    safe_top_level_navigation = (
        not websocket
        and not required
        and fetch_mode == "navigate"
        and fetch_dest == "document"
    )
    if fetch_site == "cross-site" and not safe_top_level_navigation:
        raise ProxyAuthError("cross-site browser request is not allowed")

    origin = _header(headers, "Origin").strip()
    if not origin:
        if required:
            raise ProxyAuthError("allowed Origin is required")
        return
    canonical_origin = _canonical_origin(origin)
    allowed = {_canonical_origin(value) for value in config.allowed_origins}
    if canonical_origin is None or canonical_origin not in allowed:
        raise ProxyAuthError("Origin is not allowed")


def _header(headers: Mapping[str, str], name: str) -> str:
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = [str(value) for value in getlist(name)]
        if len(values) > 1:
            raise ProxyAuthError(f"duplicate proxy-auth header: {name}")
        return values[0] if values else ""

    wanted = name.lower()
    values = []
    for key, value in headers.items():
        if str(key).lower() == wanted:
            values.append(str(value))
    if len(values) > 1:
        raise ProxyAuthError(f"duplicate proxy-auth header: {name}")
    return values[0] if values else ""


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
    return _canonical_origin(origin) is not None


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _canonical_origin(origin: str) -> Optional[str]:
    """Return a browser-style origin, or ``None`` for malformed input."""
    if not origin or origin != origin.strip():
        return None
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    raw_host = parsed.netloc.rsplit("@", 1)[-1]
    if raw_host.endswith(":"):
        return None
    if any(
        ord(character) < 33 or ord(character) == 127 or character in "/?#@%"
        for character in hostname
    ):
        return None
    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port == (443 if scheme == "https" else 80):
        port = None
    suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{hostname}{suffix}"
