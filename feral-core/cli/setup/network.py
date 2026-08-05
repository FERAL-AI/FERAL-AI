"""Shared network/access core used by the wizard step + ``feral access``.

Single source of truth for the three "how do I reach this Brain?"
profiles:

* **localhost** — bind ``127.0.0.1`` (default; loopback only).
* **lan** — bind ``0.0.0.0`` (or an operator-chosen interface) so
  other devices on the same Wi-Fi/LAN can pair without Tailscale.
* **tailscale_funnel** — public DNS via Tailscale Funnel for remote
  pairing across networks.

The wizard step (``cli/setup/steps/network.py``) and the legacy
``feral access {status,remote-up,remote-down}`` CLI both call into
this module so the persistence rules, error remediation, and
truthful-failure semantics live in one place. We never silently swap
"unavailable" for "looks fine" — every failure surfaces a structured
``NetworkApplyError`` with an actionable remediation hint.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass, field

from config.access_mode import AccessMode
from config.loader import feral_home
from config.runtime import brain_port

logger = logging.getLogger("feral.cli.setup.network")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TailscaleView:
    installed: bool = False
    running: bool = False
    logged_in: bool = False
    dns_name: str = ""
    ipv4: str = ""
    tailnet: str = ""
    error: str = ""


@dataclass
class NetworkSnapshot:
    """A point-in-time description of how this Brain is reachable.

    ``mode`` is the operator-visible profile name; ``bind_host`` is the
    actual address the FastAPI server binds to on next ``feral start``;
    ``lan_ipv4`` is the local network IP we'd advertise to other
    devices on the same Wi-Fi; ``remote_url`` is the Tailscale Funnel
    public URL when Mode C is active.
    """

    mode: str = "localhost"  # "localhost" | "lan" | "remote"
    bind_host: str = "127.0.0.1"
    lan_ipv4: str = ""
    remote_url: str = ""
    tailscale: TailscaleView = field(default_factory=TailscaleView)
    funnel_active: bool = False
    funnel_ports: list[int] = field(default_factory=list)


class NetworkApplyError(Exception):
    """Raised by ``apply_*`` when a profile cannot be activated.

    Carries a machine-readable ``code`` and a human-readable
    ``remediation`` string so both the wizard step and the REST/CLI
    paths can render actionable next-steps.
    """

    def __init__(self, code: str, message: str, *, remediation: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.remediation = remediation


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _detect_lan_ipv4() -> str:
    """Best-effort local network IP detection.

    Opens a UDP socket to a public address (no packet is actually
    sent for UDP "connect") and reads the local end of the route. On
    hosts without a default route we return an empty string rather
    than the loopback IP so the wizard can show "no LAN detected"
    truthfully.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and ip != "127.0.0.1":
            return ip
        return ""
    except Exception:
        return ""
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def get_snapshot() -> NetworkSnapshot:
    """Read the current network/access state from disk + tailscale."""
    snap = NetworkSnapshot()
    snap.lan_ipv4 = _detect_lan_ipv4()

    settings = _read_settings()
    network = settings.get("network") or {}
    access = settings.get("access") or {}
    persisted_bind = network.get("bind_host") or os.environ.get("FERAL_BIND_HOST") or os.environ.get("FERAL_HOST") or "127.0.0.1"
    snap.bind_host = persisted_bind

    pairing_mode = access.get("pairing_mode", "")
    if pairing_mode == "remote":
        snap.mode = "remote"
    elif pairing_mode == "local" or persisted_bind not in ("127.0.0.1", "localhost", ""):
        # "local" is the config-layer spelling of Mode A; the bind-host
        # check stays as the fallback for installs whose access block
        # predates the wizard writing a pairing mode at all.
        snap.mode = "lan"
    else:
        snap.mode = "localhost"

    ts_view = TailscaleView()
    try:
        from integrations import tailscale  # local import — heavy module

        ts_status = tailscale.status()
        ts_view.installed = ts_status.installed
        ts_view.running = ts_status.running
        ts_view.logged_in = ts_status.logged_in
        ts_view.dns_name = ts_status.dns_name
        ts_view.ipv4 = ts_status.ipv4
        ts_view.tailnet = ts_status.tailnet_name
        ts_view.error = ts_status.error or ""

        if ts_view.installed and ts_view.running:
            try:
                fn = tailscale.funnel_status()
                snap.funnel_active = bool(fn.get("active"))
                snap.funnel_ports = list(fn.get("ports") or [])
            except tailscale.TailscaleError as exc:
                logger.debug("get_snapshot: funnel_status failed: %s", exc)
    except Exception as exc:
        # The integration shouldn't import-fail under normal installs,
        # but keep the snapshot truthful if it does.
        logger.debug("get_snapshot: tailscale module unavailable: %s", exc)
        ts_view.error = str(exc)
    snap.tailscale = ts_view

    ts_settings = (access.get("tailscale") or {}) if isinstance(access, dict) else {}
    snap.remote_url = str(ts_settings.get("tailnet_url") or "")
    return snap


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


async def apply_localhost() -> NetworkSnapshot:
    """Switch the Brain back to loopback-only binding."""
    _apply_access_mode(AccessMode.LOCALHOST)
    return await get_snapshot()


async def apply_lan(bind_host: str = "0.0.0.0") -> NetworkSnapshot:
    """Bind the Brain to ``bind_host`` so the same-LAN devices can pair.

    The wizard caller is responsible for showing the security warning
    before invoking this — opening the Brain to anyone on the local
    network is opt-in only and must be a deliberate operator choice.

    ``bind_host`` is now derived from the mode rather than chosen here,
    so the parameter only survives to reject the empty string for
    callers (and tests) that still pass it. LAN mode always binds
    0.0.0.0; binding a single interface was never actually plumbed
    through to the pair-URL resolver, which detects the LAN address
    independently.
    """
    if not bind_host:
        raise NetworkApplyError(
            code="invalid_bind_host",
            message="bind_host cannot be empty",
        )
    _apply_access_mode(AccessMode.LAN)
    return await get_snapshot()


async def apply_tailscale_funnel() -> NetworkSnapshot:
    """Enable Tailscale Funnel for the brain port and persist the URL.

    Mirrors the existing ``api/routes/access.py::access_remote_up``
    flow but is callable from sync CLI contexts too. Raises
    ``NetworkApplyError`` with a structured remediation when Tailscale
    isn't installed / not logged in / Funnel isn't enabled in the
    tailnet admin.
    """
    try:
        from integrations import tailscale
    except Exception as exc:  # pragma: no cover - import failure shouldn't happen
        raise NetworkApplyError(
            code="tailscale_module_unavailable",
            message=str(exc),
            remediation="Reinstall feral-ai or report this as a packaging bug.",
        )

    snap = tailscale.status()
    if not snap.installed:
        raise NetworkApplyError(
            code="tailscale_not_installed",
            message="tailscale binary not found on PATH.",
            remediation=(
                "Install Tailscale — macOS: `brew install --cask tailscale`; "
                "Linux: `curl -fsSL https://tailscale.com/install.sh | sh`. "
                "Then re-run the network step."
            ),
        )
    if not snap.running:
        raise NetworkApplyError(
            code="tailscale_daemon_unreachable",
            message="tailscaled is not running or its socket is missing.",
            remediation=(
                "Start Tailscale (open the macOS menubar app, or run "
                "`sudo systemctl start tailscaled` on Linux), then re-run."
            ),
        )
    if not snap.logged_in:
        raise NetworkApplyError(
            code="tailscale_not_logged_in",
            message="Tailscale daemon is up but not authenticated.",
            remediation=(
                "Run `tailscale up` in a terminal and complete the browser "
                "OAuth, then re-run the network step."
            ),
        )

    port = brain_port()
    try:
        result = await asyncio.to_thread(tailscale.funnel_enable, port)
    except tailscale.TailscaleFunnelDisabledInTailnet as exc:
        import re

        msg = str(exc)
        m = re.search(r"https://login\.tailscale\.com/f/funnel\?node=\S+", msg)
        remediation = m.group(0) if m else "https://login.tailscale.com/admin/settings/features"
        raise NetworkApplyError(
            code="funnel_disabled_in_tailnet",
            message=msg,
            remediation=remediation,
        )
    except tailscale.TailscalePermissionDenied as exc:
        raise NetworkApplyError(
            code="tailscale_permission_denied",
            message=str(exc),
            remediation=(
                "Linux: add yourself to the tailscale group with "
                "`sudo usermod -aG tailscale $USER`, then log out / in."
            ),
        )
    except tailscale.TailscaleError as exc:
        raise NetworkApplyError(
            code="tailscale_subprocess_error",
            message=str(exc),
        )

    url = result.get("url") or ""
    if not url:
        raise NetworkApplyError(
            code="no_funnel_url",
            message=(
                "Funnel enabled but no DNS name resolved. "
                "Check `tailscale status` and retry."
            ),
        )

    _persist_remote_url(url)
    return await get_snapshot()


async def disable_tailscale_funnel() -> NetworkSnapshot:
    """Idempotent disable for Funnel — used by ``feral access remote-down``."""
    port = brain_port()
    try:
        from integrations import tailscale

        try:
            await asyncio.to_thread(tailscale.funnel_disable, port)
        except tailscale.TailscaleNotInstalled:
            # No-op equivalent — clear settings so the operator isn't stuck.
            pass
        except tailscale.TailscaleError as exc:
            raise NetworkApplyError(
                code="tailscale_subprocess_error",
                message=str(exc),
            )
    except ImportError as exc:  # pragma: no cover
        raise NetworkApplyError(
            code="tailscale_module_unavailable",
            message=str(exc),
        )

    _clear_remote_url()
    return await get_snapshot()


# ---------------------------------------------------------------------------
# Persistence (settings.json)
# ---------------------------------------------------------------------------


def _settings_path():
    return feral_home() / "settings.json"


def _read_settings() -> dict:
    import json

    path = _settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_settings(data: dict) -> None:
    import json

    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


class _SettingsFileConfig:
    """Minimal ``ConfigLoader``-shaped adapter over ``settings.json``.

    The CLI usually runs in a different process from the brain, so there
    is no live ``ConfigLoader`` to mutate. Rather than duplicate the
    mode-to-bind-host derivation for the file case (duplicating it is
    what allowed the two to drift apart in the first place), wrap the
    file in the small interface ``config.access_mode.apply_mode`` needs.
    One derivation, two backends.
    """

    def __init__(self) -> None:
        self._data = _read_settings()

    def get(self, section: str, key: str, default=None):
        return (self._data.get(section) or {}).get(key, default)

    def update_settings(self, section: str, key: str, value) -> None:
        self._data.setdefault(section, {})[key] = value
        _write_settings(self._data)


def _apply_access_mode(mode) -> None:
    """Persist an access mode (and its bind host) from a CLI context."""
    from config.access_mode import apply_mode

    apply_mode(_SettingsFileConfig(), mode)


def persist_port(port: int) -> None:
    """Persist the brain's HTTP listen port to ``settings.json``.

    Public (no underscore) so the network setup step + the CLI's
    ``feral config network port`` surface can both write it. Read
    back at boot by ``config.runtime.brain_port`` when neither
    ``FERAL_PORT`` nor ``FERAL_BRAIN_PORT`` is set in the env.
    """
    if not (1 <= int(port) <= 65535):
        raise ValueError(f"port {port} out of range (1-65535)")
    data = _read_settings()
    data.setdefault("network", {})["port"] = int(port)
    _write_settings(data)


def persist_tls(enabled: bool) -> None:
    """Persist the brain's TLS-on flag to ``settings.json``.

    Public for the same reasons as ``persist_port``. Read back at
    boot by ``config.runtime.brain_tls_enabled`` when ``FERAL_TLS``
    is unset in the env. The actual certs still live in
    ``~/.feral/tls/`` (or the env-pointed locations); this setting
    only toggles whether ``feral start`` looks for them.
    """
    data = _read_settings()
    data.setdefault("network", {})["tls"] = bool(enabled)
    _write_settings(data)


def _persist_remote_url(url: str) -> None:
    data = _read_settings()
    access = data.setdefault("access", {})
    ts = dict(access.get("tailscale", {}) or {})
    ts["funnel"] = True
    ts["tailnet_url"] = url
    access["tailscale"] = ts
    access["remote_provider"] = "tailscale"
    _write_settings(data)
    # Mode + bind host together, after the tailscale block is on disk.
    _apply_access_mode(AccessMode.TAILSCALE)


def _clear_remote_url() -> None:
    data = _read_settings()
    access = data.setdefault("access", {})
    ts = dict(access.get("tailscale", {}) or {})
    ts["funnel"] = False
    ts["tailnet_url"] = ""
    access["tailscale"] = ts
    # Dropping Funnel demotes Mode C to whatever the bind host still
    # supports — a brain bound to 0.0.0.0 is reachable on the LAN, so
    # demoting it all the way to "localhost" would revoke phone
    # pairing that still works.
    bind_host = (data.get("network") or {}).get("bind_host") or "127.0.0.1"
    _write_settings(data)
    _apply_access_mode(
        AccessMode.LOCALHOST
        if bind_host in ("127.0.0.1", "localhost", "")
        else AccessMode.LAN
    )


# ---------------------------------------------------------------------------
# Pretty rendering helpers (used by both ``feral access status`` and the
# wizard step so the operator sees identical chrome).
# ---------------------------------------------------------------------------


def render_snapshot_lines(snap: NetworkSnapshot) -> list[str]:
    """Format a snapshot as the canonical multi-line status block."""
    lines: list[str] = []
    lines.append(f"  Pairing mode: {snap.mode}")
    lines.append(f"  Bind host:    {snap.bind_host}")
    if snap.lan_ipv4:
        lines.append(
            f"  LAN URL:      http://{snap.lan_ipv4}:{brain_port()}  "
            f"(reachable from any device on this Wi-Fi)"
        )
    else:
        lines.append("  LAN URL:      (no local network detected)")
    if snap.remote_url:
        lines.append(f"  Remote URL:   {snap.remote_url}")
    else:
        lines.append("  Remote URL:   (none — pick `tailscale` to enable Funnel)")
    ts = snap.tailscale
    if ts.installed:
        if ts.running and ts.logged_in:
            lines.append(
                f"  Tailscale:    OK — {ts.dns_name} ({ts.ipv4})"
            )
            if ts.tailnet:
                lines.append(f"  Tailnet:      {ts.tailnet}")
        elif ts.running:
            lines.append(
                "  Tailscale:    daemon running but not logged in. "
                "Run `tailscale up` then retry."
            )
        else:
            lines.append(
                "  Tailscale:    daemon NOT running. Start the menubar "
                "app (macOS) or `sudo systemctl start tailscaled` (Linux)."
            )
    else:
        lines.append(
            "  Tailscale:    NOT installed. "
            "macOS: `brew install --cask tailscale`. "
            "Linux: `curl -fsSL https://tailscale.com/install.sh | sh`."
        )
    if snap.funnel_active:
        ports = ", ".join(str(p) for p in snap.funnel_ports)
        lines.append(f"  Funnel:       ACTIVE on port(s) {ports or '?'}")
    else:
        lines.append("  Funnel:       not active")
    return lines


__all__ = [
    "NetworkSnapshot",
    "NetworkApplyError",
    "TailscaleView",
    "get_snapshot",
    "apply_localhost",
    "apply_lan",
    "apply_tailscale_funnel",
    "disable_tailscale_funnel",
    "render_snapshot_lines",
]
