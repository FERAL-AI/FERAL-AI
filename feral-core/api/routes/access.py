"""Access-mode REST endpoints (Mode A / B / C onboarding).

Wires the ``feral-core/integrations/tailscale.py`` module to the
dashboard so Settings → Access can show current status and trigger
``remote-up`` / ``remote-down`` without the operator having to drop
to a terminal.

Endpoints:

  GET   /api/access/status        — current mode + Tailscale state
  POST  /api/access/remote-up     — enable Tailscale Funnel + persist
  POST  /api/access/remote-down   — disable Funnel + clear settings

All routes are Bearer-gated by the global APIKeyMiddleware (the
operator's dashboard key) — they're not part of the open allowlist.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from api.state import state
from config.access_mode import LOOPBACK_HOSTS, AccessMode, apply_mode
from config.runtime import bound_host, brain_port
from integrations import tailscale

logger = logging.getLogger("feral.api.access")
router = APIRouter()


def _persist_remote_url(url: str) -> None:
    """Write the resolved Tailscale Funnel URL into settings so the
    pair URL resolver picks it up on subsequent /api/devices/pair/url
    calls."""
    cfg = getattr(state, "config", None)
    if cfg is None:
        return
    # Through apply_mode, not a bare update_settings, so bind_host moves
    # with the mode. The previous direct write left the two free to
    # contradict each other.
    apply_mode(cfg, AccessMode.TAILSCALE)
    existing = cfg._merged.get("access", {}) or {}
    ts = dict(existing.get("tailscale", {}) or {})
    ts["funnel"] = True
    ts["tailnet_url"] = url
    cfg.update_settings("access", "tailscale", ts)
    cfg.update_settings("access", "remote_provider", "tailscale")


def _clear_remote_url() -> None:
    """Drop the Funnel URL and demote the mode.

    Demotion is deliberately *not* an unconditional fall to localhost.
    The CLI equivalent used to demote to "local" when the brain was
    bound to a LAN address, on the reasoning that revoking still-working
    LAN pairing is worse than leaving it up, while this REST version
    always went to localhost. The two disagreed. This is now the single
    implementation, and it keeps the CLI's kinder behaviour: if the live
    listener is still reachable on the LAN, land on LAN rather than
    cutting off phones that are working fine.
    """
    cfg = getattr(state, "config", None)
    if cfg is None:
        return
    existing = cfg._merged.get("access", {}) or {}
    ts = dict(existing.get("tailscale", {}) or {})
    ts["funnel"] = False
    ts["tailnet_url"] = ""
    cfg.update_settings("access", "tailscale", ts)

    actual = bound_host()
    still_on_lan = actual is not None and actual not in LOOPBACK_HOSTS
    apply_mode(cfg, AccessMode.LAN if still_on_lan else AccessMode.LOCALHOST)


@router.post("/api/access/mode")
async def set_access_mode(body: dict):
    """Set the access mode. The one supported way to change reachability.

    Writes ``access.pairing_mode`` and ``network.bind_host`` together so
    they cannot contradict, and tells the caller honestly whether the
    change is live yet. ``bind_host`` is read once at bind time, so a
    mode change on a running brain is persisted but not in effect until
    a restart — which is precisely what nothing used to say.
    """
    cfg = getattr(state, "config", None)
    if cfg is None:
        raise HTTPException(status_code=503, detail={"code": "config_unavailable"})

    try:
        result = apply_mode(cfg, body.get("mode"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_mode", "message": str(exc)},
        )

    payload = result.as_dict()
    payload["ok"] = True
    if result.restart_required:
        payload["restart"] = {
            "required": True,
            "reason": (
                f"listening on {bound_host()} but {result.mode.value} mode "
                f"needs {result.bind_host}"
            ),
            "command": "feral restart",
        }
    logger.info("access mode set to %s (restart_required=%s)",
                result.mode.value, result.restart_required)
    return payload


@router.get("/api/access/status")
async def access_status():
    """Return current pairing mode + Tailscale state.

    Response shape::

        {
          "pairing_mode": "local" | "localhost" | "remote",
          "remote_url": "<https://… tailnet url>",
          "tailscale": {
            "installed": bool,
            "running": bool,
            "logged_in": bool,
            "dns_name": "...",
            "ipv4": "...",
            "tailnet": "...",
            "error": "tailscale_not_installed" | "daemon_unreachable" |
                     "not_logged_in" | "" 
          },
          "funnel": {"active": bool, "ports": [int, ...]}
        }
    """
    cfg = getattr(state, "config", None)
    pairing_mode = cfg.access_pairing_mode if cfg else "localhost"
    remote_url = cfg.access_remote_url if cfg else ""

    snap = tailscale.status()
    funnel_view = {"active": False, "ports": []}
    if snap.installed and snap.running:
        try:
            funnel_view = tailscale.funnel_status()
        except tailscale.TailscaleError as exc:
            logger.debug("access_status: funnel_status failed: %s", exc)

    return {
        "pairing_mode": pairing_mode,
        "remote_url": remote_url,
        "tailscale": {
            "installed": snap.installed,
            "running": snap.running,
            "logged_in": snap.logged_in,
            "dns_name": snap.dns_name,
            "ipv4": snap.ipv4,
            "ipv6": snap.ipv6,
            "tailnet": snap.tailnet_name,
            "error": snap.error,
        },
        "funnel": funnel_view,
    }


@router.post("/api/access/remote-up")
async def access_remote_up():
    """Enable Mode C (Tailscale Funnel) for the brain port.

    Calls ``tailscale funnel <port> on``, reads the resolved DNS name,
    persists it under ``access.tailscale.tailnet_url`` so the pair URL
    resolver emits the public URL on subsequent /api/devices/pair/url.

    On failure: returns a structured error with ``code`` + ``message``
    + (where applicable) ``remediation`` so the dashboard can render
    actionable guidance ("install tailscale", "log in to tailscale",
    "enable Funnel in admin", "add yourself to the tailscale group").
    """
    snap = tailscale.status()
    if not snap.installed:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "tailscale_not_installed",
                "message": "tailscale binary not found on PATH.",
                "remediation": "Install Tailscale: macOS `brew install --cask tailscale`; "
                               "Linux `curl -fsSL https://tailscale.com/install.sh | sh`. "
                               "Then click 'Set up remote access' again.",
            },
        )
    if not snap.running:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "tailscale_daemon_unreachable",
                "message": "tailscaled is not running or its socket is missing.",
                "remediation": "Start Tailscale (open the menubar app on macOS, or "
                               "`sudo systemctl start tailscaled` on Linux).",
            },
        )
    if not snap.logged_in:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "tailscale_not_logged_in",
                "message": "Tailscale daemon is up but not authenticated.",
                "remediation": "Run `tailscale up` in a terminal, complete the "
                               "browser OAuth, then click 'Set up remote access' again.",
            },
        )

    port = brain_port()
    try:
        result = tailscale.funnel_enable(port)
    except tailscale.TailscaleFunnelDisabledInTailnet as exc:
        # The integration extracts the per-node enable URL from the
        # CLI's stderr when present (Tailscale 1.66+) and embeds it in
        # the exception message. Pull it back out for the dashboard so
        # it can render a one-click button.
        import re
        msg = str(exc)
        m = re.search(
            r"https://login\.tailscale\.com/f/funnel\?node=\S+", msg,
        )
        remediation_url = m.group(0) if m else (
            "https://login.tailscale.com/admin/settings/features"
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "funnel_disabled_in_tailnet",
                "message": msg,
                "remediation": remediation_url,
            },
        )
    except tailscale.TailscalePermissionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "tailscale_permission_denied",
                "message": str(exc),
                "remediation": "Linux: add yourself to the tailscale group with "
                               "`sudo usermod -aG tailscale $USER`, then log out / in.",
            },
        )
    except tailscale.TailscaleError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "tailscale_subprocess_error", "message": str(exc)},
        )

    url = result.get("url") or ""
    if not url:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "no_funnel_url",
                "message": "Funnel enabled but no DNS name resolved. "
                           "Check `tailscale status` and retry.",
            },
        )

    _persist_remote_url(url)
    logger.info("access remote-up: pairing_mode=remote tailnet_url=%s", url)
    return {
        "ok": True,
        "pairing_mode": "remote",
        "remote_url": url,
        "tailscale": result,
    }


@router.post("/api/access/remote-down")
async def access_remote_down():
    """Disable Tailscale Funnel and revert to localhost pairing mode.

    Idempotent — runs ``tailscale funnel <port> off`` (which is itself
    a no-op if Funnel was already off) and clears the persisted
    tailnet_url. Pairing falls back to localhost.
    """
    port = brain_port()
    try:
        result = tailscale.funnel_disable(port)
    except tailscale.TailscaleNotInstalled:
        # No-op equivalent: nothing to disable, and we still clear
        # any stale settings rather than leaving the operator stuck.
        result = {"enabled": False, "port": port}
    except tailscale.TailscaleError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "tailscale_subprocess_error", "message": str(exc)},
        )

    _clear_remote_url()
    logger.info("access remote-down: pairing_mode=localhost")
    return {"ok": True, "pairing_mode": "localhost", "tailscale": result}
