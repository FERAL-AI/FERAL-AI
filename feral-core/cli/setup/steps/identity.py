"""Identity step — capture who the user is so the agent has context."""

from __future__ import annotations

from ..helpers import ask_text, confirm, get_console
from ..state import WizardState


def run(state: WizardState) -> None:
    console = get_console()

    # Step numbering belongs to the state machine's indicator.
    console.print("Short identity block so the agent knows who it's helping.")

    if not confirm("  Fill in now? (you can edit later in Settings → Self)", default=True):
        return

    name = ask_text("  Your name", default=state.identity.get("name", ""), allow_empty=True)
    if name:
        state.identity["name"] = name

    occupation = ask_text(
        "  What do you do? (founder, engineer, student, …)",
        default=state.identity.get("occupation", ""),
        allow_empty=True,
    )
    if occupation:
        state.identity["occupation"] = occupation

    location = ask_text(
        "  Where are you based? (city / country)",
        default=state.identity.get("location", ""),
        allow_empty=True,
    )
    if location:
        state.identity["location"] = location

    # Write a USER.md so the identity loader picks it up right away.
    user_md = state.home / "USER.md"
    blurb_lines = ["# About Me\n"]
    if state.identity.get("name"):
        blurb_lines.append(f"Name: {state.identity['name']}")
    if state.identity.get("occupation"):
        blurb_lines.append(f"Occupation: {state.identity['occupation']}")
    if state.identity.get("location"):
        blurb_lines.append(f"Location: {state.identity['location']}")
    if len(blurb_lines) > 1:
        user_md.write_text("\n".join(blurb_lines) + "\n")
