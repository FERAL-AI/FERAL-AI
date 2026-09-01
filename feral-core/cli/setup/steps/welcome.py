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


def _navigation_hint() -> str:
    """Describe the navigation this session will really honour.

    Two different input paths exist and they take different gestures.
    ``ask_choice`` uses an InquirerPy arrow-key picker when one is
    available and the shell is a TTY, and a typed prompt otherwise. Only
    the typed prompt parses ``back`` / ``menu`` / ``quit`` as words; the
    picker offers them as selectable rows instead.
    """
    try:
        arrow_keys = ui_kit.is_inquirer_available() and ui_kit.is_interactive()
    except Exception:
        arrow_keys = False

    if arrow_keys:
        return (
            "[dim]The last rows of every list are [/][bold]jump to a "
            "previous step[/][dim], [/][bold]back[/][dim] and [/]"
            "[bold]quit setup[/][dim] - select one to use it.[/]"
        )
    return (
        "[dim]Type [/][bold]back[/][dim] for the previous step, "
        "[/][bold]menu[/][dim] to jump to any step, "
        "[/][bold]quit[/][dim] to stop and keep what you've entered.[/]"
    )


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
            # This list is ordered by, and numbered with, the step
            # indices the operator will actually see in the "Step N of
            # M" header. The previous version listed six unnumbered
            # groups in a different order from the wizard (it put
            # network fourth; network is step 9) and omitted memory and
            # system permissions entirely, so the first screen
            # contradicted the second.
            "Welcome — this wizard sets up your local brain.\n\n"
            "[bold]Steps 1-2 get you a working brain.[/] Everything after "
            "is optional:\nskip it now, come back later with "
            "[bold]feral setup --from-step <name>[/].\n\n"
            "  [bold] 1-2 [/]  LLM provider + model  [dim](any cloud or "
            "local)[/]\n"
            "  [bold] 3-4 [/]  Speech in / out  [dim](cloud or fully "
            "local)[/]\n"
            "  [bold] 5-6 [/]  Identity + personality\n"
            "  [bold] 7   [/]  Capabilities  [dim](vision, autonomy, "
            "workspace access)[/]\n"
            "  [bold] 8   [/]  Memory + semantic search\n"
            "  [bold] 9   [/]  Network access  [dim](localhost / LAN / "
            "Tailscale)[/]\n"
            "  [bold]10-16[/]  Integrations, Home Assistant, tool keys, "
            "coding\n           agents, messaging channels, phone, "
            "permissions\n\n"
            "[dim]After step 2 you can stop and start using FERAL, or keep "
            "going.[/]\n\n"
            # The pickers are enter-on-highlight (ui_kit.pick), not the
            # space-to-mark ui_kit.select — telling operators to press
            # space made them think the wizard was stuck.
            "[dim]At any prompt: ↑/↓ navigate · enter to choose the "
            "highlighted row.[/]\n"
            # `menu` is advertised because it is the strongest escape
            # hatch in the wizard and was the only one nobody was told
            # about. `back` walks one step at a time, which is no help
            # sixteen steps in; `menu` jumps straight to any step. An
            # operator who feels stuck reaches for the exit they know
            # exists, so the exits have to be named.
            #
            # But name them the way THIS session can actually use them.
            # ``back`` / ``menu`` / ``quit`` are parsed only on the typed
            # fallback in ``helpers.ask_choice`` (the branch whose own
            # comment reads "the path the existing tests drive"). On the
            # arrow-key picker, which is what an operator with a TTY and
            # InquirerPy actually gets, typing those words does nothing:
            # the equivalents are rows at the bottom of the list. Telling
            # someone to type at a prompt with no text field is the same
            # class of error as the old "press space" hint this block
            # already exists to correct.
            + _navigation_hint()
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
    console.print(
        "Type 'back' for the previous step, 'menu' to jump to any step, "
        "'quit' to stop."
    )
