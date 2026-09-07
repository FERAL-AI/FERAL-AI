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


# ── The endpoint of the brain living in this FERAL_HOME ───────────
#
# ``brain_public_port`` consulted environment variables and nothing
# else, so every ``feral`` command addressed localhost:9090 no matter
# which FERAL_HOME it was pointed at. Running a second brain and
# administering it was therefore impossible without also exporting
# FERAL_PORT: the commands succeeded, against the WRONG brain, and said
# so cheerfully. Measured on 2026-09-07, ``FERAL_HOME=<B> feral sync
# peer scope grant <A> work`` granted the scope on A, to A itself, and
# left B with an empty roster. B then correctly refused every operation
# A sent it, and the feature looked broken while it was working.
#
# So a serving brain records where it is, in its own home, and the
# resolver below reads it. Env still wins, because an operator behind a
# proxy is describing something this file cannot observe.

_RUNTIME_STATE_FILE = "runtime.json"


def _runtime_state_path() -> "Path":
    from config.loader import feral_home  # local import: avoids a cycle
    return feral_home() / _RUNTIME_STATE_FILE


def record_runtime_endpoint(port: int) -> None:
    """Record the port this process is about to serve on. Never raises.

    Written at serve time into this brain's own FERAL_HOME, so a CLI
    pointed at that home can find it. Best effort: a brain that cannot
    write its own home has larger problems than CLI addressing, and
    failing to boot over it would be the wrong trade.
    """
    import json as _json
    import os as _os

    try:
        path = _runtime_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps({"port": int(port), "pid": _os.getpid()}))
        _os.replace(tmp, path)
    except Exception:
        pass


def running_brain_port() -> int | None:
    """The port the brain in this FERAL_HOME last served on, if known.

    ``None`` when nothing has served here, or when the record is
    unreadable or nonsensical. A stale record is not dangerous: the
    caller gets a connection refused, which is the same outcome as
    guessing 9090 at a home with no brain, and a far better one than
    silently reaching a different brain.
    """
    import json as _json

    try:
        raw = _json.loads(_runtime_state_path().read_text())
    except Exception:
        return None
    port = raw.get("port") if isinstance(raw, dict) else None
    if isinstance(port, int) and 1 <= port <= 65535:
        return port
    return None


def brain_public_scheme() -> str:
    return os.getenv("FERAL_PUBLIC_SCHEME", "http")


def brain_public_host() -> str:
    return (
        os.getenv("FERAL_PUBLIC_HOST")
        or os.getenv("FERAL_BRAIN_HOST")
        or "localhost"
    )


def brain_public_port() -> int:
    """Env first, then the brain actually serving in this FERAL_HOME.

    The recorded endpoint sits between the env vars and the 9090
    default so that pointing a command at a home addresses the brain
    that lives there, while an operator behind a proxy can still
    override with FERAL_PUBLIC_PORT.
    """
    env = _int_env("FERAL_PUBLIC_PORT", "FERAL_BRAIN_PORT", "FERAL_PORT", default=-1)
    if env != -1:
        return env
    recorded = running_brain_port()
    if recorded is not None:
        return recorded
    return 9090


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
