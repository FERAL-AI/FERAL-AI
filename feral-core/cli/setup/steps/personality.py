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
    identity_path = write_identity_yaml(state.home, agent_name, soul_text)
    console.print(
        f"  [green]✓[/] wrote {soul_path}" if _RICH_AVAILABLE
        else f"  wrote {soul_path}"
    )
    if identity_path is not None:
        console.print(
            f"  [green]✓[/] wrote {identity_path}" if _RICH_AVAILABLE
            else f"  wrote {identity_path}"
        )


# Kept in sync with ``api/routes/config.py``'s identity writer so the
# wizard and the web Settings page produce the same file.
_IDENTITY_TAGLINE = (
    "Your personal AI operating system: local, private, always learning."
)
_IDENTITY_RULES = [
    "Never make up sensor data or health readings. Only report what's actually connected.",
    "If a tool call fails, explain what went wrong in plain language.",
    "Keep responses concise, 1-3 sentences for simple questions.",
    "Respect user privacy. Everything runs locally unless they explicitly ask to share.",
]
_IDENTITY_GREETING = (
    "Keep greetings brief and contextual. If you know the user's name, use it. "
    "Don't list all your capabilities unless asked."
)


def write_identity_yaml(home, agent_name: str, soul_text: str):
    """Write ``IDENTITY.yaml``, the only file that carries the agent name.

    ``agents/identity_loader.load_identity()`` builds the system prompt's
    opening line from ``IDENTITY.yaml`` and falls back to a hardcoded
    "You are FERAL, a personal AI operating system." when the file is
    absent. The modular wizard wrote the name to ``settings.identity``
    and to the SOUL.md heading, neither of which that loader reads, so
    naming the agent Jarvis still produced "You are FERAL". The
    pre-rewrite monolith wrote this file and the web Settings page still
    does; only the wizard dropped it.

    Returns the path written, or ``None`` when the write failed (a
    personality is not worth aborting setup over).
    """
    from pathlib import Path

    path = Path(home) / "IDENTITY.yaml"
    data = {
        "name": agent_name,
        "tagline": _IDENTITY_TAGLINE,
        "personality": soul_text,
        "rules": list(_IDENTITY_RULES),
        "greeting_style": _IDENTITY_GREETING,
    }
    try:
        import yaml

        path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False)
        )
        return path
    except ImportError:
        import json

        path.write_text(json.dumps(data, indent=2))
        return path
    except OSError:
        return None
