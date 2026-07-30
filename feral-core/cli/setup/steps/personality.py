"""Agent personality — writes ``~/.feral/SOUL.md``.

``agents/identity_loader.py`` reads ``SOUL.md`` directly on every turn
and falls back to a generic "Default Personality" block when the file
is absent, which is what every install got: the modular wizard never
wrote one. The presets already existed in the pre-rewrite monolith
(``cli.setup_wizard.PERSONALITY_PRESETS``) and are imported rather
than duplicated.
"""

from __future__ import annotations

from ..helpers import (
    Option,
    ask_choice,
    ask_text,
    confirm,
    get_console,
    _RICH_AVAILABLE,
)
from ..state import WizardState


def _presets() -> dict:
    from cli.setup_wizard import PERSONALITY_PRESETS

    return dict(PERSONALITY_PRESETS)


def run(state: WizardState) -> None:
    console = get_console()
    soul_path = state.home / "SOUL.md"

    console.print(
        "Pick how the agent should talk to you. This writes SOUL.md, "
        "which the agent loads into every conversation."
    )
    if soul_path.is_file():
        console.print(
            f"  [dim]{soul_path} already exists.[/]" if _RICH_AVAILABLE
            else f"  {soul_path} already exists."
        )
        if not confirm("  Replace it?", default=False):
            return
    elif not confirm("  Set a personality now?", default=True):
        return

    presets = _presets()
    options = [
        Option(id=key, label=f"{preset['label']} — {preset['desc']}")
        for key, preset in presets.items()
    ]
    chosen = ask_choice("  Personality", options, default="assistant")
    preset = presets.get(chosen.id) or {}

    if chosen.id == "custom":
        soul_text = ask_text(
            "  Describe the personality you want (one paragraph)",
            default="",
            allow_empty=False,
        ).strip()
    else:
        soul_text = str(preset.get("soul") or "").strip()
        preview = soul_text[:160]
        console.print(
            f"  [dim]{preview}…[/]" if _RICH_AVAILABLE else f"  {preview}…"
        )

    if not soul_text:
        console.print("  (nothing to write — skipped)")
        return

    agent_name = ask_text("  Agent name", default="FERAL", allow_empty=False)
    soul_path.write_text(f"# {agent_name}\n\n{soul_text}\n")
    state.set_setting("identity", "agent_name", agent_name)
    state.set_setting("identity", "personality", chosen.id)
    console.print(
        f"  [green]✓[/] wrote {soul_path}" if _RICH_AVAILABLE
        else f"  wrote {soul_path}"
    )
