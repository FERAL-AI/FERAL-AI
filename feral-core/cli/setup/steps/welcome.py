"""Welcome banner — first-run greeting with the raccoon logo."""

from __future__ import annotations

from cli import ui_kit

from ..helpers import get_console, _RICH_AVAILABLE
from ..state import WizardState


# ASCII art block. Mirrors what `claude-code` / `codex` print at first
# run — single brand panel that immediately tells the operator which
# tool they're configuring and what version, in the brand colour.
_LOGO_LINES = (
    "███████╗███████╗██████╗  █████╗ ██╗",
    "██╔════╝██╔════╝██╔══██╗██╔══██╗██║",
    "█████╗  █████╗  ██████╔╝███████║██║",
    "██╔══╝  ██╔══╝  ██╔══██╗██╔══██║██║",
    "██║     ███████╗██║  ██║██║  ██║███████╗",
    "╚═╝     ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝",
)


def _version() -> str:
    try:
        from version import VERSION

        return str(VERSION)
    except Exception:
        return ""


def run(state: WizardState) -> None:
    # Bug 4 — render exactly once per ``feral setup`` invocation.
    # The state machine re-enters the welcome step on BackNavigation
    # from step 1 ("can't go back from the first step" is the very
    # first prompt operators hit when they overshoot) and on
    # ``JumpToStep`` from any later step. Without this guard the
    # operator sees the big ASCII banner a second time and reads it
    # as "the wizard just restarted from scratch" — the operator's
    # screenshot in the audit showed the duplicated banner.
    #
    # ``completed_steps`` is populated by the state machine after a
    # step returns normally, and is also restored from the resume
    # sidecar so a resumed run also skips the second banner (the
    # operator already saw it in the prior session).
    if "welcome" in state.completed_steps:
        return
    console = get_console()
    version = _version()
    if _RICH_AVAILABLE:
        from rich.align import Align
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        logo = Text("\n".join(_LOGO_LINES), style=f"bold {ui_kit.BRAND_COLOR}")
        subtitle = Text(
            f"{ui_kit.BRAND_EMOJI}  Unleashed AI" + (f"  ·  v{version}" if version else ""),
            style="bold",
        )
        body = Text.from_markup(
            "Welcome — this wizard sets up your local brain in a few steps:\n\n"
            "  [bold]1.[/]  LLM provider + model (any cloud or local)\n"
            "  [bold]2.[/]  Speech in / out (cloud or fully local)\n"
            "  [bold]3.[/]  Identity (so the agent knows who it is talking to)\n"
            "  [bold]4.[/]  Network access (localhost / LAN / Tailscale)\n"
            "  [bold]5.[/]  Optional: Home Assistant + messaging channels\n\n"
            "[dim]At any prompt: ↑/↓ navigate · space to mark · enter to confirm.[/]\n"
            "[dim]Type [/][bold]back[/][dim] to return to the previous step, "
            "[/][bold]quit[/][dim] to stop and keep what you've entered.[/]"
        )
        block = Group(Align.center(logo), Align.center(subtitle), Text(""), body)
        console.print(
            Panel(
                block,
                title=f"{ui_kit.BRAND_EMOJI}  feral setup",
                border_style=ui_kit.BRAND_COLOR,
                padding=(1, 2),
            )
        )
        return

    console.print("=" * 60)
    console.print(f"{ui_kit.BRAND_EMOJI}  FERAL — Unleashed AI" + (f"  v{version}" if version else ""))
    for line in _LOGO_LINES:
        console.print(line)
    console.print("=" * 60)
    console.print("Welcome to FERAL setup.")
    console.print("Type 'back' to go back, 'quit' to stop.")
