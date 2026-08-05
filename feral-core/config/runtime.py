"""
FERAL Runtime Contract helpers.

One place for listen/public URL and local service defaults so app, CLI, and
desktop wrappers do not drift across hardcoded localhost/port assumptions.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


def _int_env(*keys: str, default: int) -> int:
    for key in keys:
        value = os.getenv(key)
        if value is None:
            continue
        try:
            return int(value)
        except ValueError:
            continue
    return default


def hydrate_brain_runtime_env() -> None:
    """Mirror persisted network settings into ``os.environ`` at CLI boot.

    The setup wizard's ``apply_lan()`` / ``apply_localhost()`` paths set
    ``FERAL_BIND_HOST`` in-process, and launchd forwards ``FERAL_*`` from
    the operator's shell when installing the service. A fresh terminal
    running ``feral serve`` only consulted ``settings.json`` at bind time
    via :func:`brain_bind_host`, while subsystems that read
    ``os.environ`` directly (mDNS advertisement, sync engine port
    defaults, pairing URL builders that fall back to env) could boot with
    stale or missing values. ``feral start --foreground`` and
    ``feral serve`` both call this once before spawning uvicorn so the
    two entrypoints share the same runtime contract.
    """
    bind = brain_bind_host()
    if bind:
        os.environ.setdefault("FERAL_BIND_HOST", bind)
    os.environ.setdefault("FERAL_PORT", str(brain_port()))
    if brain_tls_enabled():
        os.environ.setdefault("FERAL_TLS", "1")


def brain_bind_host() -> str:
    """Resolve the host the brain binds to.

    Precedence: ``FERAL_HOST`` env > ``FERAL_BIND_HOST`` env >
    persisted ``network.bind_host`` in ``~/.feral/settings.json`` (the
    new wizard's network step writes this when the operator picks the
    LAN profile) > loopback-only default (``127.0.0.1``). The settings
    file is only consulted when neither env var is set so existing
    deployments that pin the host via systemd/docker keep their
    behaviour verbatim.
    """
    env = os.getenv("FERAL_HOST") or os.getenv("FERAL_BIND_HOST")
    if env:
        return env
    persisted = _settings_get("network", "bind_host")
    if isinstance(persisted, str) and persisted:
        return persisted
    return "127.0.0.1"


# The host the *running* listener actually bound, recorded once by
# ``cli.main._spawn_brain_server``. ``brain_bind_host()`` answers "what
# would the next boot bind", which is a different question and the one
# that made the reported bug invisible: settings said "local" while the
# live process was still on loopback, and nothing compared the two.
_BOUND_HOST: str | None = None


def record_bound_host(host: str) -> None:
    """Record what the live listener bound. Called once, at serve time.

    Deliberately takes the value passed to uvicorn rather than re-reading
    config, because the whole point is to capture reality rather than
    intent.
    """
    global _BOUND_HOST
    _BOUND_HOST = host


def bound_host() -> str | None:
    """The live listener's bind host, or ``None`` if nothing is serving.

    ``None`` means "no listener in this process" (a CLI invocation, a
    test), not "unknown". Callers treat it as "nothing to restart".
    """
    return _BOUND_HOST


def _reset_bound_host() -> None:
    """Test seam. Registered in ``tests/conftest.py`` shared-state resets."""
    global _BOUND_HOST
    _BOUND_HOST = None


def _settings_get(*path: str) -> object | None:
    """Best-effort read of a nested value from ``~/.feral/settings.json``.

    Returns ``None`` on any failure (file missing, bad JSON, key absent).
    Never raises — callers fall through to defaults. Centralised here so
    ``brain_port`` / ``brain_tls_enabled`` / ``brain_bind_host`` use the
    same parse code path.
    """
    try:
        from config.loader import feral_home  # local import to avoid cycle at module load
        import json as _json

        path_to_settings = feral_home() / "settings.json"
        if not path_to_settings.exists():
            return None
        data = _json.loads(path_to_settings.read_text())
        cursor: object = data
        for key in path:
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(key)
            if cursor is None:
                return None
        return cursor
    except Exception:
        return None


def brain_port() -> int:
    """Resolve the brain HTTP listen port.

    Precedence: ``FERAL_PORT`` env > ``FERAL_BRAIN_PORT`` env >
    persisted ``network.port`` in ``~/.feral/settings.json`` (written
    by the setup wizard's network step) > ``9090``. Env still wins so
    ops with the brain inside docker / systemd can pin the port
    without touching the wizard.
    """
    env = _int_env("FERAL_PORT", "FERAL_BRAIN_PORT", default=-1)
    if env != -1:
        return env
    persisted = _settings_get("network", "port")
    if isinstance(persisted, int) and 1 <= persisted <= 65535:
        return persisted
    if isinstance(persisted, str) and persisted.isdigit():
        n = int(persisted)
        if 1 <= n <= 65535:
            return n
    return 9090


def brain_public_scheme() -> str:
    return os.getenv("FERAL_PUBLIC_SCHEME", "http")


def brain_public_host() -> str:
    return (
        os.getenv("FERAL_PUBLIC_HOST")
        or os.getenv("FERAL_BRAIN_HOST")
        or "localhost"
    )


def brain_public_port() -> int:
    return _int_env("FERAL_PUBLIC_PORT", "FERAL_BRAIN_PORT", "FERAL_PORT", default=9090)


def brain_public_base_url() -> str:
    explicit = os.getenv("FERAL_PUBLIC_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    scheme = brain_public_scheme()
    host = brain_public_host()
    port = brain_public_port()
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


def ws_base_url() -> str:
    parsed = urlparse(brain_public_base_url())
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    suffix = f":{parsed.port}" if parsed.port else ""
    return f"{ws_scheme}://{parsed.hostname}{suffix}"


def market_registry_url() -> str:
    """Resolve the marketplace registry URL (with API path).

    Default points at the production registry so a fresh install can
    browse + install community items without configuration. Old builds
    defaulted to ``http://localhost:8080/api/v1`` which was a vestige
    of local-registry development and surprised every user who didn't
    have one running.
    """
    return os.getenv("FERAL_MARKETPLACE_URL", "https://registry.feral.sh/api/v1")


def brain_tls_enabled() -> bool:
    """Resolve whether the brain should serve over TLS.

    Precedence: ``FERAL_TLS`` env > persisted ``network.tls`` in
    ``~/.feral/settings.json`` > ``False``. The env path keeps ops
    in charge for systemd / docker deployments; the wizard path
    lets a user enable TLS once and have every subsequent
    ``feral start`` honour it without re-typing the flag.
    """
    raw = os.getenv("FERAL_TLS")
    if raw is not None and raw != "":
        return raw.lower() in ("1", "true", "yes")
    persisted = _settings_get("network", "tls")
    if isinstance(persisted, bool):
        return persisted
    if isinstance(persisted, str):
        return persisted.lower() in ("1", "true", "yes")
    return False


def brain_tls_cert() -> str:
    return os.getenv("FERAL_TLS_CERT", str(Path.home() / ".feral" / "tls" / "cert.pem"))


def brain_tls_key() -> str:
    return os.getenv("FERAL_TLS_KEY", str(Path.home() / ".feral" / "tls" / "key.pem"))


def ollama_base_url() -> str:
    return os.getenv("FERAL_OLLAMA_BASE_URL", "http://localhost:11434")


def ollama_openai_base_url() -> str:
    base = ollama_base_url().rstrip("/")
    return f"{base}/v1"
