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
import json
import logging
from pathlib import Path
from typing import Optional

from config.loader import feral_home

from .state import WizardState
from .state_machine import StateMachine
from .steps import (
    audio,
    channels,
    finish,
    home_assistant,
    identity,
    llm,
    pairing,
    welcome,
)
from .steps import network as network_step

logger = logging.getLogger("feral.cli.setup")


def run_setup(*, from_step: str = "") -> None:
    """Entry point used by :func:`cli.main.cmd_setup`.

    Historical call sites import ``cli.setup_wizard.run_setup``; the
    legacy module now delegates here so we don't have to touch every
    installer script.

    Lane U1 — ``from_step`` lets the operator re-enter one step
    (e.g. ``llm_model``) without deleting the resume sidecar.
    """
    try:
        asyncio.run(_run_async(from_step=from_step))
    except KeyboardInterrupt:
        from rich.console import Console

        Console().print("\n[yellow]Setup cancelled — run `feral setup` again when ready.[/]")


async def _run_async(*, from_step: str = "") -> None:
    state = WizardState.load(feral_home())

    # Lane 07  — voice + TCC preflight steps. Voice preflight runs
    # AFTER llm_model so the LLM choice is locked in first; TCC
    # preflight runs LAST before finish so the operator's most
    # recent action is granting permissions and re-probing.
    from .steps import voice_preflight, tcc_preflight

    machine = StateMachine(
        state=state,
        from_step=from_step,
        steps=[
            ("welcome", welcome.run),
            ("llm_provider", llm.run_provider_step),
            ("llm_model", llm.run_model_step),
            ("voice_preflight", voice_preflight.run),
            ("audio", audio.run),
            ("identity", identity.run),
            ("network", network_step.run),
            ("home_assistant", home_assistant.run),
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
