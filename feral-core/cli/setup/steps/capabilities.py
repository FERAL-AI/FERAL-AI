"""Capability toggles — the feature flags the wizard never exposed.

Everything here is a settings key the brain already reads at boot; the
only thing missing was a way to reach it without hand-editing
``~/.feral/settings.json``. One multi-select covers the ``features.*``
flags plus vision, then two follow-ups cover the two choices that are
not booleans: the autonomy tier and a workspace folder grant.
"""

from __future__ import annotations

from pathlib import Path

from cli import ui_kit

from ..helpers import (
    Option,
    QuitNavigation,
    ask_choice,
    ask_text,
    confirm,
    get_console,
    _RICH_AVAILABLE,
)
from ..state import WizardState


# (section, key, label, help) for every boolean the operator can flip
# here. ``vision.enabled`` lives in its own section but ConfigLoader
# keeps it in lockstep with ``features.vision``, so writing either is
# enough — we write the one the runtime treats as canonical.
_TOGGLES = (
    (
        "features", "proactive", "Proactive nudges",
        "the agent starts conversations when it notices something",
    ),
    (
        "features", "streaming", "Streaming replies",
        "tokens appear as they are generated instead of all at once",
    ),
    (
        "features", "self_learning", "Self-learning",
        "the agent updates its own notes about you over time",
    ),
    (
        "features", "multi_agent", "Multi-agent",
        "hand long tasks to sub-agents running in parallel",
    ),
    (
        "vision", "enabled", "Vision",
        "let the agent look at screenshots and camera frames",
    ),
)


_AUTONOMY_MODES = (
    ("strict", "Strict — ask before every tool that touches anything"),
    ("hybrid", "Hybrid — ask only for destructive or costly actions (recommended)"),
    ("loose", "Loose — act freely, ask only for the genuinely dangerous"),
)


def run(state: WizardState) -> None:
    console = get_console()
    console.print(
        "Turn on the capabilities you want. Every one of these is a "
        "setting you can change later in Settings or with `feral`."
    )

    _run_toggles(state, console)
    _run_autonomy(state, console)
    _run_workspace_grant(state, console)


def _run_toggles(state: WizardState, console) -> None:
    choices = []
    for section, key, label, blurb in _TOGGLES:
        current = bool(state.get_setting(section, key, _default_for(section, key)))
        choices.append({
            "name": f"{label} — {blurb}",
            "value": f"{section}.{key}",
            "enabled": current,
        })

    try:
        picked = ui_kit.multi_select("Capabilities to enable", choices)
    except KeyboardInterrupt:
        raise QuitNavigation()

    marked = set(picked)
    for section, key, label, _blurb in _TOGGLES:
        on = f"{section}.{key}" in marked
        state.set_setting(section, key, on)
        console.print(
            f"  {'[green]on [/]' if on else '[dim]off[/]'} {label}"
            if _RICH_AVAILABLE else
            f"  {'on ' if on else 'off'} {label}"
        )


def _default_for(section: str, key: str) -> bool:
    """Current shipped default for a toggle, so an untouched install
    pre-marks the rows the brain would actually run with."""
    try:
        from config.loader import DEFAULT_SETTINGS

        return bool((DEFAULT_SETTINGS.get(section) or {}).get(key, False))
    except Exception:
        return False


def _run_autonomy(state: WizardState, console) -> None:
    console.print()
    console.print(
        "How much should the agent do without asking? "
        "(`security.autonomy_mode` — the brain reads this at boot.)"
    )
    options = [Option(id=mode, label=label) for mode, label in _AUTONOMY_MODES]
    current = state.get_setting("security", "autonomy_mode", "hybrid")
    chosen = ask_choice("  Autonomy", options, default=current)
    state.set_setting("security", "autonomy_mode", chosen.id)


def _run_workspace_grant(state: WizardState, console) -> None:
    console.print()
    console.print(
        "The file tools refuse any path outside an explicit grant. "
        "Grant a folder now, or add one later with `feral grant add <path>`."
    )
    if not confirm("  Grant a workspace folder?", default=False):
        return

    while True:
        raw = ask_text(
            "  Folder to grant (e.g. ~/Projects)",
            default=str(Path.home() / "Desktop"),
            allow_empty=False,
        )
        target = Path(raw).expanduser()
        if not target.is_dir():
            console.print(
                f"  [yellow]{target} is not a directory on this machine.[/]"
                if _RICH_AVAILABLE else
                f"  {target} is not a directory on this machine."
            )
            if confirm("  Try a different path?", default=True):
                continue
            return

        mode_opts = [
            Option(id="readwrite", label="Read + write"),
            Option(id="read", label="Read only"),
        ]
        mode = ask_choice("  Access level", mode_opts, default="readwrite")
        _grant(str(target), mode.id, console)
        if not confirm("  Grant another folder?", default=False):
            return


def _grant(path: str, mode: str, console) -> None:
    """Delegate to the same SandboxPolicy ``feral grant add`` writes to."""
    from security.sandbox_policy import SandboxPolicy

    result = SandboxPolicy.load_default().grant_folder(path, mode=mode)
    if result.get("ok"):
        console.print(
            f"  [green]✓[/] granted {result.get('mode')} on {result.get('path')}"
            if _RICH_AVAILABLE else
            f"  granted {result.get('mode')} on {result.get('path')}"
        )
        return
    console.print(
        f"  [red]✘[/] grant failed: {result.get('error', 'unknown error')}"
        if _RICH_AVAILABLE else
        f"  grant failed: {result.get('error', 'unknown error')}"
    )
