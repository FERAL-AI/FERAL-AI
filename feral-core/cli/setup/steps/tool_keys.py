"""Optional tool API keys — search, weather, GitHub, music.

Without a search key the ``web_search`` skill silently degrades to
DuckDuckGo scraping and the weather tool has nothing to call at all.
Both are quiet failures: the agent answers, just badly, and the
operator has no idea a key would have fixed it.

The catalogue (env var, name, where to get one) already existed in the
pre-rewrite monolith as ``cli.setup_wizard.TOOL_KEYS`` and was left
stranded when the wizard was modularised. We import it rather than
maintaining a second copy that can drift.
"""

from __future__ import annotations

import os

from cli import ui_kit

from ..helpers import (
    QuitNavigation,
    ask_text,
    confirm,
    get_console,
    _RICH_AVAILABLE,
)
from ..state import WizardState


def _catalogue() -> list[dict]:
    from cli.setup_wizard import TOOL_KEYS

    return list(TOOL_KEYS)


def run(state: WizardState) -> None:
    console = get_console()
    console.print(
        "Optional keys that unlock tools. Web search falls back to "
        "DuckDuckGo scraping without one of the search keys, and weather "
        "needs OpenWeatherMap. Everything here is skippable."
    )

    if not confirm("  Add any tool keys now?", default=False):
        return

    catalogue = _catalogue()
    choices = []
    for entry in catalogue:
        env = entry["env"]
        have = bool(state.credentials.get(env) or os.environ.get(env))
        suffix = "  · already set" if have else ""
        choices.append({
            "name": f"{entry['name']} — {entry['desc']}{suffix}",
            "value": env,
            "enabled": False,
        })

    try:
        picked = ui_kit.multi_select(
            "Which tool keys do you want to enter?", choices,
        )
    except KeyboardInterrupt:
        raise QuitNavigation()

    wanted = set(picked)
    if not wanted:
        console.print("  (none picked — skipping)")
        return

    for entry in catalogue:
        if entry["env"] not in wanted:
            continue
        _prompt_entry(state, entry, console)


def _prompt_entry(state: WizardState, entry: dict, console) -> None:
    """Prompt for one catalogue entry plus any companion keys."""
    env = entry["env"]
    console.print()
    console.print(
        f"[bold]{entry['name']}[/] — {entry['hint']}"
        if _RICH_AVAILABLE else f"{entry['name']} — {entry['hint']}"
    )

    existing = state.credentials.get(env) or os.environ.get(env) or ""
    if existing and not confirm(f"  {env} is already set. Replace it?", default=False):
        return

    value = ask_text(f"  {env}", default="", allow_empty=True, secret=True)
    if not value:
        console.print("  (blank — skipped)")
        return
    state.set_credential(env, value)
    os.environ[env] = value

    # Some entries need a second value to be usable at all (Spotify's
    # client secret). Prompting for the id alone would store a
    # half-credential that fails at call time.
    for companion in entry.get("extra_keys") or []:
        companion_value = ask_text(
            f"  {companion}", default="", allow_empty=True, secret=True,
        )
        if companion_value:
            state.set_credential(companion, companion_value)
            os.environ[companion] = companion_value
        else:
            console.print(
                f"  [yellow]{companion} left blank — {entry['name']} won't "
                f"work until it's set.[/]"
                if _RICH_AVAILABLE else
                f"  {companion} left blank — {entry['name']} won't work "
                f"until it's set."
            )
