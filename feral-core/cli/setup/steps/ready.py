"""The "your brain works now" checkpoint, straight after provider + model.

Why this step exists
====================
FERAL's wizard is sixteen visible steps. Every one of them from
``voice_preflight`` onward can be declined, so the operator only ever
*had* to answer the first two -- but nothing on screen said so, and a
first-time operator reading "Step 1 of 16" has no way to tell the
difference between "sixteen questions I must answer" and "two questions
and fourteen offers".

The count is not the problem. FERAL genuinely does more than the tools
it gets compared to: semantic memory, device pairing, TCC permissions,
Home Assistant, messaging channels, external coding agents. The problem
was sequencing -- everything was presented as equally mandatory, with no
moment where the operator was told they had already crossed the finish
line.

So this is that moment. It renders once, immediately after the model is
chosen, and offers three honest options:

* start using FERAL now, jumping straight to ``finish``
* keep going through the optional steps
* see what the optional steps actually are, then decide

It is deliberately NOT a step in the "Step N of M" count. Adding a
seventeenth numbered step to announce that the operator is done after
two would be its own small joke.

Nothing here writes configuration. The brain is already usable at this
point; this step only decides whether the operator keeps walking.
"""

from __future__ import annotations

from cli import ui_kit
from cli.setup.helpers import JumpToStep, get_console
from cli.setup.state import WizardState

# What the operator is choosing to skip, in the order they would meet
# it. Kept here rather than derived from the step list because the
# grouping is editorial: "Integrations" is one line to a human and four
# steps to the state machine.
_WHATS_LEFT = [
    ("Speech in / out", "talk to FERAL and have it talk back"),
    ("Identity + personality", "who FERAL thinks you are, and how it answers"),
    ("Capabilities", "vision, autonomy tier, which folders it may touch"),
    ("Memory + semantic search", "recall across conversations"),
    ("Network access", "reach it from your phone or another machine"),
    ("Integrations + channels", "Home Assistant, tool keys, Slack, iMessage"),
    ("Connect your phone", "pair a device"),
    ("System permissions", "macOS screen recording, accessibility, mic"),
]

_START_NOW = "start"
_KEEP_GOING = "continue"
_SHOW_REST = "show"


def run(state: WizardState) -> None:
    # Render once per run. On a ``back`` from the step after this one,
    # or a resume, the operator has already made this decision and
    # showing it again would put a fork in front of them that they
    # already answered.
    if "ready" in state.completed_steps:
        return

    console = get_console()
    provider = state.get_setting("llm", "provider") or ""
    model = state.get_setting("llm", "model") or ""

    detail = f"{provider} · {model}" if provider and model else "your provider"
    console.print("")
    console.print(f"  [bold green]Your brain works.[/]  [dim]{detail}[/]")
    console.print(
        "  [dim]That was the required part. Everything left is optional "
        "and can be\n  done later with [/][bold]feral setup --from-step "
        "<name>[/][dim].[/]"
    )
    console.print("")

    while True:
        try:
            choice = ui_kit.pick(
                "  What now?",
                [
                    {
                        "name": "Keep configuring — recommended for a first install",
                        "value": _KEEP_GOING,
                    },
                    {"name": "Start using FERAL now", "value": _START_NOW},
                    {"name": "Show me what's left, then decide", "value": _SHOW_REST},
                ],
                default=_KEEP_GOING,
            )
        except KeyboardInterrupt:
            # Ctrl+C here means "stop asking me", not "abandon setup".
            # The brain is already configured, so the useful reading is
            # the same as picking "start now".
            raise JumpToStep("finish")

        if choice == _SHOW_REST:
            console.print("")
            for title, why in _WHATS_LEFT:
                console.print(f"    [bold]{title}[/]  [dim]— {why}[/]")
            console.print("")
            continue

        if choice == _START_NOW:
            # Jump rather than return: returning would advance to the
            # next optional step, which is the opposite of what was
            # asked for. ``finish`` writes the config and prints the
            # how-to-start summary, so the operator still lands
            # somewhere useful rather than being dropped at a shell.
            raise JumpToStep("finish")

        return
