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

#: How many folders the grant step will walk through, counting retries
#: after a bad path. See the comment at the loop.
_MAX_GRANT_ATTEMPTS = 8


# (section, key, label, help) for every boolean the operator can flip
# here. ``vision.enabled`` lives in its own section and ConfigLoader
# coalesces it with ``features.vision``, but it does so with a logical
# OR (``_unify_feature_flags``). "Writing either is enough" is true for
# turning vision ON and false for turning it OFF: an operator who
# enabled vision in the web UI (which writes ``features.vision``) and
# then unticked it here kept a running ScreenLoop, the exact ambient
# API-quota burner the loader warns about. Both keys are mirrored on
# write, see ``_MIRRORED_KEYS``.
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


# Toggles whose value must be written to more than one settings key,
# because the loader ORs the pair and a single write can therefore only
# ever turn the feature on.
_MIRRORED_KEYS = {
    ("vision", "enabled"): (("features", "vision"),),
}


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
        # Mirror keys are OR'd by the loader, so the row is pre-marked
        # when EITHER key is on. Otherwise an install that enabled
        # vision through the web UI would show the row unticked here
        # and look like it was already off.
        current = bool(state.get_setting(section, key, _default_for(section, key)))
        for mirror_section, mirror_key in _MIRRORED_KEYS.get((section, key), ()):
            current = current or bool(
                state.get_setting(
                    mirror_section, mirror_key, _default_for(mirror_section, mirror_key)
                )
            )
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
        for mirror_section, mirror_key in _MIRRORED_KEYS.get((section, key), ()):
            state.set_setting(mirror_section, mirror_key, on)
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

    # Bounded, like every other retry in this wizard. Two loops here can
    # re-ask: a bad path with "Try a different path?" defaulting to yes,
    # and "Grant another folder?" defaulting to no. The first is the one
    # that matters, and the same bound covers both.
    #
    # Lower risk than the other cases, because the offered default IS a
    # directory, so entering through the defaults exits on the first
    # pass. Bounded anyway: "it happens to terminate today because the
    # default is valid" is not a termination guarantee, and this file is
    # one changed default away from the `while True` bug the wizard has
    # already shipped twice.
    for _attempt in range(_MAX_GRANT_ATTEMPTS):
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
