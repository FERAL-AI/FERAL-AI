"""Network / access wizard step — pick localhost / LAN / Tailscale.

Calls into :mod:`cli.setup.network` (shared core also used by
``feral access``) so the persistence + Tailscale remediation rules
live in exactly one place. Truthful failure: if Tailscale isn't
installed / not logged in / Funnel isn't enabled, the operator sees
the exact error and remediation; we never silently fall back to
localhost.
"""

from __future__ import annotations

from cli import ui_kit

from .. import network
from ..helpers import BackNavigation, QuitNavigation, get_console, _RICH_AVAILABLE
from ..state import WizardState


_LAN_WARNING = (
    "LAN mode binds the Brain to 0.0.0.0 — any device on this Wi-Fi "
    "or LAN can reach it. Make sure you trust the network and that "
    "you've configured the operator API key. Default stays loopback "
    "unless you opt in."
)


async def run(state: WizardState) -> None:
    console = get_console()
    if _RICH_AVAILABLE:
        console.print()
        console.print("[bold]Step · Network access[/]")
        console.print(
            "How will you reach this Brain? Pick the profile that "
            "matches the device you'll be talking to it from."
        )

    snap = await network.get_snapshot()
    for line in network.render_snapshot_lines(snap):
        console.print(line)
    console.print()

    choices = [
        {
            "name": "Localhost only — phone/laptop on this same machine (default, safest)",
            "value": "localhost",
        },
        {
            "name": (
                f"Same Wi-Fi / LAN — bind 0.0.0.0 so other devices on "
                f"{snap.lan_ipv4 or 'this network'} can reach the Brain"
            ),
            "value": "lan",
        },
        {
            "name": "Anywhere on the internet — Tailscale Funnel (free, opens an auth browser)",
            "value": "tailscale",
        },
        {"name": "← back", "value": "__back__"},
        {"name": "↳ skip — keep current settings", "value": "__skip__"},
    ]

    default = snap.mode if snap.mode in ("localhost", "lan", "remote") else "localhost"
    if default == "remote":
        default = "tailscale"

    try:
        # v2026.5.28 — pick (enter-on-cursor-position) instead of select
        # (mark-then-confirm). Network profile is a one-of-three choice
        # and operators expect arrow-keys-then-enter.
        picked = ui_kit.pick(
            "Pick the access profile",
            choices,
            default=default,
        )
    except KeyboardInterrupt:
        raise QuitNavigation()

    if picked == "__back__":
        raise BackNavigation()
    if picked == "__skip__":
        ui_kit.banner_line("Network step skipped — current settings preserved.")
        return

    if picked == "localhost":
        snap = await network.apply_localhost()
        ui_kit.banner_line(f"Bound to {snap.bind_host} (loopback only).")
    elif picked == "lan":
        ui_kit.banner_line(_LAN_WARNING, style="yellow")
        if not ui_kit.confirm("Open the Brain to your local network?", default=False):
            ui_kit.banner_line("Skipped — kept current bind host.")
            return
        try:
            snap = await network.apply_lan()
        except network.NetworkApplyError as exc:
            _render_error(console, exc)
            return
        ui_kit.banner_line(
            f"Bound to {snap.bind_host}. "
            f"Reach the Brain from this LAN at "
            f"http://{snap.lan_ipv4 or '<your-lan-ip>'}:{_brain_port()}"
        )
        if not snap.lan_ipv4:
            ui_kit.banner_line(
                "  (Couldn't auto-detect a LAN IPv4 — check `ifconfig` "
                "/ `ip addr` for the right interface.)",
                style="yellow",
            )
    elif picked == "tailscale":
        try:
            snap = await network.apply_tailscale_funnel()
        except network.NetworkApplyError as exc:
            _render_error(console, exc)
            # Offer LAN as a fallback so the operator isn't stuck.
            if ui_kit.confirm(
                "Fall back to LAN-only mode for now?", default=False
            ):
                try:
                    snap = await network.apply_lan()
                    ui_kit.banner_line(
                        f"OK — bound to {snap.bind_host} for now. "
                        f"Re-run the network step once Tailscale is ready."
                    )
                except network.NetworkApplyError as exc2:
                    _render_error(console, exc2)
            return
        ui_kit.banner_line(f"Tailscale Funnel active — public URL: {snap.remote_url}")

    _mirror_into_state(state, snap)


def _mirror_into_state(state: WizardState, snap) -> None:
    """Copy the applied profile back into ``state.settings``.

    :mod:`cli.setup.network` already persisted the profile to
    ``settings.json`` directly (shared with ``feral access``), but the
    wizard's in-memory settings are what the finish summary renders and
    what ``state.save()`` merges at the end of the run. Mirroring keeps
    the two views consistent.

    We write ``access.pairing_mode`` rather than a ``network.mode``
    key: the former is what ``ConfigLoader.access_pairing_mode`` and
    ``GET /api/devices/pair/url`` read at runtime, while the latter was
    written by this step and read by nothing.

    Both keys come from :class:`~config.access_mode.AccessMode`, so the
    bind host is derived from the mode here exactly as it is on every
    other write path. Mirroring ``snap.bind_host`` independently would
    reintroduce a second derivation, which is what allowed the mode and
    the bind host to drift apart in the first place.
    """
    from config.access_mode import AccessMode

    mode = {
        "lan": AccessMode.LAN,
        "remote": AccessMode.TAILSCALE,
    }.get(snap.mode, AccessMode.LOCALHOST)
    state.set_setting("network", "bind_host", mode.bind_host)
    state.set_setting("access", "pairing_mode", mode.value)
    if snap.remote_url:
        access = state.settings.setdefault("access", {})
        tailscale = dict(access.get("tailscale") or {})
        tailscale["tailnet_url"] = snap.remote_url
        tailscale["funnel"] = True
        access["tailscale"] = tailscale
        access["remote_provider"] = "tailscale"


def _brain_port() -> int:
    from config.runtime import brain_port

    return brain_port()


def _render_error(console, exc: network.NetworkApplyError) -> None:
    if _RICH_AVAILABLE:
        console.print(f"[red]✘[/red] {exc.code}: {exc}")
        if exc.remediation:
            console.print(f"  [dim]→[/dim] {exc.remediation}")
    else:
        console.print(f"  ✘ {exc.code}: {exc}")
        if exc.remediation:
            console.print(f"  → {exc.remediation}")
