"""Third-party integrations — Google, Notion, Spotify, MS365, Gmail.

``cli/integration_commands.py`` already implements every connect flow
(OAuth self-serve walkthrough + the Gmail app-password path), and
``feral integrations list`` already renders connection status. The
wizard offered exactly one integration (Home Assistant) and left the
rest undiscoverable, so this step is a thin front-end over the same
dispatcher rather than a second implementation.
"""

from __future__ import annotations

from ..helpers import (
    Option,
    ask_choice,
    confirm,
    get_console,
    _RICH_AVAILABLE,
)
from ..state import WizardState


# id → (label, blurb). Ids are the ones
# ``cmd_integrations_connect`` accepts.
_INTEGRATIONS = (
    ("google", "Google", "Calendar, Drive, Contacts (OAuth)"),
    ("gmail", "Gmail", "read + send mail via an app password, no OAuth app needed"),
    ("notion", "Notion", "read + write pages and databases (OAuth)"),
    ("spotify", "Spotify", "playback + playlist control (OAuth)"),
    ("microsoft", "Microsoft 365", "Outlook, Calendar, OneDrive (OAuth)"),
)


def run(state: WizardState) -> None:
    console = get_console()
    console.print(
        "Connect accounts the agent can act on. Each flow opens your "
        "browser or asks for a token; you can skip and run "
        "`feral integrations connect <id>` later."
    )

    if not confirm("  Connect any integrations now?", default=False):
        return

    connected: list[str] = list(state.get_setting("integrations", "connected", []) or [])

    while True:
        options = [
            Option(
                id=integration_id,
                label=f"{label} — {blurb}"
                + ("  · connected" if integration_id in connected else ""),
            )
            for integration_id, label, blurb in _INTEGRATIONS
        ]
        options.append(Option(id="__done__", label="Done — move on"))

        chosen = ask_choice("  Which integration?", options, default="__done__")
        if chosen.id == "__done__":
            break

        if _connect(chosen.id, console) and chosen.id not in connected:
            connected.append(chosen.id)

        if not confirm("  Connect another?", default=False):
            break

    state.set_setting("integrations", "connected", connected)


def _connect(integration_id: str, console) -> bool:
    """Run the real connect walkthrough. Returns True on exit code 0.

    ``cmd_integrations_connect`` prompts and prints on its own, so the
    wizard gets the identical flow an operator would get from
    ``feral integrations connect``, remediation text included.
    """
    from cli.integration_commands import cmd_integrations_connect

    console.print()
    code = cmd_integrations_connect(
        integration_id=integration_id,
        self_hosted=False,
        no_browser=False,
    )
    if code == 0:
        console.print(
            f"  [green]✓[/] {integration_id} connected."
            if _RICH_AVAILABLE else f"  {integration_id} connected."
        )
        return True
    console.print(
        f"  [yellow]{integration_id} not connected (exit {code}). "
        f"Retry any time with `feral integrations connect "
        f"{integration_id}`.[/]"
        if _RICH_AVAILABLE else
        f"  {integration_id} not connected (exit {code}). Retry with "
        f"`feral integrations connect {integration_id}`."
    )
    return False
