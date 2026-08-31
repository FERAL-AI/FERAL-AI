"""FERAL setup wizard — modular, state-machine-driven first-run flow.

Replaces the 1700-line monolithic ``cli/setup_wizard.py`` with a small
package that keeps one step per file. The state machine in
:mod:`state_machine` drives ordering + back/skip/quit support; each
step module is a single function taking a :class:`WizardState` and
mutating the ``settings`` / ``credentials`` / ``identity`` sub-dicts
before returning.

Public entry point
------------------

The CLI imports :func:`run_setup` and calls it synchronously. That
function is responsible for starting the asyncio loop, loading any
existing settings, running every step in order, persisting the final
config + credentials atomically, and printing the summary.
"""

from __future__ import annotations

import asyncio
import logging

from config.loader import feral_home

from .state import WizardState
from .state_machine import StateMachine
from .steps import (
    audio,
    capabilities,
    channels,
    external_agents,
    finish,
    home_assistant,
    identity,
    integrations,
    llm,
    pairing,
    personality,
    tool_keys,
    ready,
    welcome,
)
from .steps import memory as memory_step
from .steps import network as network_step

logger = logging.getLogger("feral.cli.setup")


# Friendly names for ``--from-step``. The internal step ids are an
# implementation detail (``llm_model``, ``tool_keys``, ``tcc_preflight``)
# and an operator should not have to read the source to re-run the model
# picker. Internal ids keep working; these are aliases on top.
STEP_ALIASES = {
    "provider": "llm_provider",
    "llm": "llm_provider",
    "model": "llm_model",
    "voice": "voice_preflight",
    "speech": "audio",
    "audio": "audio",
    "tts": "audio",
    "identity": "identity",
    "personality": "personality",
    "capabilities": "capabilities",
    "memory": "memory",
    "network": "network",
    "access": "network",
    "integrations": "integrations",
    "homeassistant": "home_assistant",
    "home-assistant": "home_assistant",
    "tools": "tool_keys",
    "keys": "tool_keys",
    "agents": "external_agents",
    "channels": "channels",
    "phone": "pairing",
    "pairing": "pairing",
    "permissions": "tcc_preflight",
    "tcc": "tcc_preflight",
}


def resolve_step_name(name: str) -> str:
    """Map a friendly section name to its internal step id.

    Unknown names pass through unchanged so the state machine can report
    them with its own "Unknown setup step" message, which already prints
    the full list of valid ids.
    """
    key = (name or "").strip().lower()
    return STEP_ALIASES.get(key, key)


# What ``--reset`` moves aside. ``credentials.enc`` is the encrypted
# vault; ``credentials.json`` is the legacy plaintext path the vault
# still anchors on. Both are listed because an install can have either.
_RESET_FILES = (
    "settings.json",
    "identity.json",
    "setup_state.json",
    "credentials.enc",
    "credentials.json",
)


def reset_config(home=None, *, console=None) -> list:
    """Move existing setup config aside so the wizard starts clean.

    Deliberately a **move, not a delete**. This config holds the
    operator's API keys, and a ``--reset`` that destroys them with no
    undo is a footgun rather than a convenience: mistyping the flag
    should cost a rename, not a re-provisioning of every key.

    Returns the list of (source, backup) pairs actually moved.
    """
    import time
    from pathlib import Path

    home = Path(home or feral_home())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = home / "backups" / f"reset-{stamp}"

    present = [home / n for n in _RESET_FILES if (home / n).exists()]
    if not present:
        return []

    backup_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in present:
        dest = backup_dir / src.name
        src.rename(dest)
        moved.append((src, dest))

    if console is not None:
        console.print(f"  Moved {len(moved)} config file(s) to {backup_dir}")
    return moved


def run_setup(
    *,
    from_step: str = "",
    quick: bool = False,
    non_interactive: bool = False,
    reset: bool = False,
) -> None:
    """Entry point used by :func:`cli.main.cmd_setup`.

    Historical call sites import ``cli.setup_wizard.run_setup``; the
    legacy module now delegates here so we don't have to touch every
    installer script.

    Lane U1 — ``from_step`` lets the operator re-enter one step
    (e.g. ``llm_model``) without deleting the resume sidecar.
    """
    from .helpers import MissingRequiredSetting, confirm, set_non_interactive

    set_non_interactive(non_interactive)

    if reset:
        from rich.console import Console

        console = Console()
        console.print(
            "[yellow]--reset[/] moves your current provider, keys, identity "
            "and progress aside\n(into ~/.feral/backups/, not deleted) and "
            "starts the wizard clean."
        )
        # ``confirm`` returns its default under --non-interactive, so the
        # default here is False: a scripted run must opt in explicitly
        # with --reset rather than have a stray flag wipe live keys.
        if non_interactive or confirm("  Reset now?", default=False):
            moved = reset_config(console=console)
            if not moved:
                console.print("  Nothing to reset — no existing config found.")
        else:
            console.print("  Left your config alone.")
            return

    try:
        asyncio.run(_run_async(
            from_step=resolve_step_name(from_step) if from_step else "",
            quick=quick,
        ))
    except MissingRequiredSetting as exc:
        # A headless run reached a question with no answer. Fail loudly
        # naming the prompt rather than writing a half-configured brain
        # and reporting success.
        from rich.console import Console

        Console().print(f"\n[red]{exc}[/]")
        raise SystemExit(2)
    except KeyboardInterrupt:
        from rich.console import Console

        Console().print("\n[yellow]Setup cancelled — run `feral setup` again when ready.[/]")


async def _run_async(*, from_step: str = "", quick: bool = False) -> None:
    state = WizardState.load(feral_home())

    # Lane 07  — voice + TCC preflight steps. Voice preflight runs
    # AFTER llm_model so the LLM choice is locked in first; TCC
    # preflight runs LAST before finish so the operator's most
    # recent action is granting permissions and re-probing.
    from .steps import voice_preflight, tcc_preflight

    machine = StateMachine(
        state=state,
        quick=quick,
        from_step=from_step,
        steps=[
            ("welcome", welcome.run),
            ("llm_provider", llm.run_provider_step),
            ("llm_model", llm.run_model_step),
            # Not a numbered step. The operator has a working
            # brain at this point and every step below can be
            # declined, so this is where they are told so and
            # offered the exit. See steps/ready.py.
            ("ready", ready.run),
            ("voice_preflight", voice_preflight.run),
            ("audio", audio.run),
            ("identity", identity.run),
            # Personality sits next to identity: both write the markdown
            # files ``agents/identity_loader`` reads on every turn.
            ("personality", personality.run),
            ("capabilities", capabilities.run),
            # Memory sits right after the capability toggles and before
            # the optional-integration tail: it is infrastructure every
            # install depends on, it is the one step that may download a
            # ~130MB model, and it verifies semantic retrieval by
            # behaviour rather than by import. Running it here means the
            # model is warm long before the operator reaches `finish`.
            ("memory", memory_step.run),
            ("network", network_step.run),
            ("integrations", integrations.run),
            ("home_assistant", home_assistant.run),
            ("tool_keys", tool_keys.run),
            # Sits next to tool_keys: both are optional developer
            # tooling the operator can decline without breaking FERAL.
            ("external_agents", external_agents.run),
            ("channels", channels.run),
            ("pairing", pairing.run),
            ("tcc_preflight", tcc_preflight.run),
            ("finish", finish.run),
        ],
    )
    try:
        await machine.run()
    finally:
        # Lane 07  — finally block no longer marks setup_complete.
        # ``state.save()`` persists settings/credentials/identity but
        # the meta.setup_complete flag stays untouched unless the
        # finish step ran (which calls ``state.mark_complete()``
        # explicitly). Closes finding 09's quit-marks-complete bug.
        state.save()


__all__ = ["run_setup", "WizardState"]
