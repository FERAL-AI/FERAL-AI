"""Phone-pairing hand-off step.

The pairing *mechanism* (pairing token + SDK pending-code + runtime
phone-bearer + optional PIN second factor) lives in
``security/device_pairing.py`` and is driven from the WebUI Devices page;
the iOS companion pairs by scanning the QR shown there.

The Brain is NOT running during ``feral setup`` (the operator runs
``feral start`` afterwards), so this step deliberately does not mint a
token or poll for a claim. Instead it closes the gap the modular wizard
had: it hands the operator the URL their *phone* can actually reach (a
loopback bind can't be reached from a separate device) plus the exact
path to pair — so the first-run "connect my phone" story works instead
of dead-ending at ``localhost``.
"""

from __future__ import annotations

from cli import ui_kit

from .. import network
from ..helpers import JumpToStep, QuitNavigation, get_console, _RICH_AVAILABLE
from ..state import WizardState


def _brain_port() -> int:
    from config.runtime import brain_port

    return brain_port()


def resolve_pair_url(snap, port: int) -> tuple[str, bool]:
    """Resolve the URL a *separate phone* should open to pair.

    Returns ``(url, warn_localhost)``. ``warn_localhost`` is True when the
    Brain is bound to loopback only — a phone on another device can't reach
    it, so the operator must switch to LAN/Tailscale first. When we can't
    determine a concrete reachable host we return a ``<this-mac-lan-ip>``
    placeholder so the instructions are still actionable.
    """
    placeholder = f"http://<this-mac-lan-ip>:{port}"
    if snap is None:
        return placeholder, False
    if snap.mode == "remote" and snap.remote_url:
        return snap.remote_url.rstrip("/"), False
    if snap.mode == "lan":
        if snap.lan_ipv4:
            return f"http://{snap.lan_ipv4}:{port}", False
        return placeholder, False
    if snap.mode == "localhost":
        return placeholder, True
    return placeholder, False


async def run(state: WizardState) -> None:
    console = get_console()
    port = _brain_port()

    if _RICH_AVAILABLE:
        console.print()
        console.print("[bold]Step · Connect your phone[/]")
        console.print(
            "FERAL's iOS companion pairs by scanning a QR code the Brain "
            "shows in its web UI. Here's how to reach it from your phone."
        )

    try:
        snap = await network.get_snapshot()
    except Exception:
        snap = None

    url, warn_localhost = resolve_pair_url(snap, port)

    if warn_localhost:
        ui_kit.banner_line(
            "Network mode is localhost — a separate phone can't reach a "
            'loopback Brain. Re-run the network step and pick "Same '
            'Wi-Fi / LAN" (or Tailscale) to pair your phone.',
            style="yellow",
        )

    for line in [
        "",
        "To pair your iPhone:",
        "  1. Start the Brain:  feral start",
        "  2. On your phone (same network), open this in its browser:",
        f"       {url}",
        "  3. Go to Settings → Devices → Pair device.",
        "  4. Open the FERAL app on your iPhone and scan the QR shown.",
        "",
        "  (The same Devices page also pairs a browser node.)",
    ]:
        console.print(line)

    # Informational hand-off — the Brain isn't up yet, so there's nothing
    # to verify. Offer continue, or step back to redo the network mode
    # (the remediation when bound to localhost).
    choices = [
        {"name": "Continue", "value": "__continue__"},
        {"name": "← back (change network mode)", "value": "__back__"},
    ]
    try:
        picked = ui_kit.pick("Phone pairing", choices, default="__continue__")
    except KeyboardInterrupt:
        raise QuitNavigation()
    if picked == "__back__":
        # The button says "change network mode", so it must land on the
        # network step. A bare ``BackNavigation`` is ``idx -= 1`` in the
        # state machine, and ``network`` is five steps earlier than
        # ``pairing``. The operator who followed the loopback warning
        # got dropped on "Messaging channels" instead. ``JumpToStep``
        # already carried an optional target for exactly this.
        raise JumpToStep("network")
