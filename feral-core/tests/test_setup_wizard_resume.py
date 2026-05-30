"""audit-r14 / lane-07 () — wizard resume + setup_complete gating.

Closes finding 09 D-D items:
  * "quit doesn't set setup_complete=True" — pre-fix, ``state.save()``
    set the flag unconditionally in a finally block.
  * "Resume from last completed step on next run" — pre-fix, no
    sidecar existed.

Both bugs are covered here against a stubbed wizard so tests don't
need the full LLM/voice/identity flows to run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def feral_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    return tmp_path


# ----------------------------------------------------------------------
# state.save() no longer marks complete; mark_complete() does.
# ----------------------------------------------------------------------


def test_save_does_not_mark_complete(feral_home):
    """Pre-Lane-07 ``state.save()`` set ``setup_complete=True``
    unconditionally. The new contract: only ``mark_complete()``
    flips that flag."""
    from cli.setup.state import WizardState

    state = WizardState.load(feral_home)
    state.set_setting("llm", "provider", "openai")
    state.save()

    settings = json.loads((feral_home / "settings.json").read_text())
    meta = settings.get("meta", {})
    assert meta.get("setup_complete") is not True, (
        f"state.save() must not flip setup_complete; got meta={meta!r}"
    )


def test_save_preserves_pre_existing_complete_flag(feral_home):
    """If a previous successful run set ``setup_complete=True``,
    a subsequent ``state.save()`` (e.g. a wizard re-run that
    quits early) must not downgrade the flag to False."""
    from cli.setup.state import WizardState

    pre = {"meta": {"setup_complete": True}, "llm": {"provider": "anthropic"}}
    (feral_home / "settings.json").write_text(json.dumps(pre))

    state = WizardState.load(feral_home)
    state.set_setting("llm", "provider", "openai")
    state.save()

    settings = json.loads((feral_home / "settings.json").read_text())
    assert settings["meta"]["setup_complete"] is True


def test_mark_complete_sets_flag_and_clears_sidecar(feral_home):
    """``mark_complete()`` MUST set ``setup_complete=True`` AND remove
    the resume sidecar."""
    from cli.setup.state import WizardState

    state = WizardState.load(feral_home)
    state.write_setup_state(last_step="audio", completed_steps=["welcome", "llm_provider"])
    assert (feral_home / "setup_state.json").is_file()

    state.mark_complete()

    settings = json.loads((feral_home / "settings.json").read_text())
    assert settings["meta"]["setup_complete"] is True
    assert not (feral_home / "setup_state.json").is_file()


# ----------------------------------------------------------------------
# Resume sidecar — write + read symmetry, atomic writes
# ----------------------------------------------------------------------


def test_write_setup_state_persists_step_and_completed(feral_home):
    from cli.setup.state import WizardState

    state = WizardState.load(feral_home)
    state.write_setup_state(
        last_step="home_assistant",
        completed_steps=["welcome", "llm_provider", "llm_model", "audio"],
    )
    sidecar = json.loads((feral_home / "setup_state.json").read_text())
    assert sidecar["last_step"] == "home_assistant"
    assert sidecar["completed_steps"] == [
        "welcome", "llm_provider", "llm_model", "audio",
    ]
    assert "ts" in sidecar
    assert sidecar["schema"] == 1


def test_read_setup_state_returns_empty_for_missing_file(feral_home):
    from cli.setup.state import WizardState

    assert WizardState.read_setup_state(feral_home) == {}


def test_read_setup_state_returns_empty_for_corrupt_json(feral_home):
    """Sidecar corruption (truncated mid-write, manual edit) must NOT
    crash the wizard. Reading returns empty + the wizard starts over."""
    from cli.setup.state import WizardState

    (feral_home / "setup_state.json").write_text("{not json")
    assert WizardState.read_setup_state(feral_home) == {}


# ----------------------------------------------------------------------
# StateMachine resume integration
# ----------------------------------------------------------------------


def test_state_machine_resumes_at_next_step_when_sidecar_present(
    feral_home, monkeypatch,
):
    """When the sidecar marks step ``audio`` as the last completed,
    the next ``feral setup`` MUST start at step ``identity`` (the
    one after audio in the wizard's ordered step list)."""
    import asyncio
    from cli.setup.state import WizardState
    from cli.setup.state_machine import StateMachine
    from cli.setup import helpers as helpers_mod

    state = WizardState.load(feral_home)
    state.write_setup_state(
        last_step="audio",
        completed_steps=["welcome", "llm_provider", "llm_model", "audio"],
    )

    # Auto-confirm the resume prompt.
    monkeypatch.setattr(helpers_mod, "confirm", lambda *a, **kw: True)
    # Same import target inside state_machine.
    import cli.setup.state_machine as sm_mod
    monkeypatch.setattr(sm_mod, "confirm", lambda *a, **kw: True)

    visited = []

    def make_step(name):
        def _step(_state):
            visited.append(name)
        return _step

    machine = StateMachine(
        state=state,
        steps=[
            ("welcome", make_step("welcome")),
            ("llm_provider", make_step("llm_provider")),
            ("llm_model", make_step("llm_model")),
            ("audio", make_step("audio")),
            ("identity", make_step("identity")),
            ("network", make_step("network")),
            ("home_assistant", make_step("home_assistant")),
            ("channels", make_step("channels")),
            ("finish", make_step("finish")),
        ],
    )
    asyncio.run(machine.run())

    # Resume MUST skip welcome..audio and start at identity.
    assert visited == ["identity", "network", "home_assistant", "channels", "finish"]


def test_state_machine_starts_over_when_user_declines_resume(
    feral_home, monkeypatch,
):
    """Operator says No to the resume prompt → sidecar deleted, wizard
    starts at step 0."""
    import asyncio
    from cli.setup.state import WizardState
    from cli.setup.state_machine import StateMachine
    import cli.setup.state_machine as sm_mod

    state = WizardState.load(feral_home)
    state.write_setup_state(last_step="audio", completed_steps=["welcome"])

    monkeypatch.setattr(sm_mod, "confirm", lambda *a, **kw: False)

    visited = []
    machine = StateMachine(
        state=state,
        steps=[
            ("welcome", lambda s: visited.append("welcome")),
            ("llm_provider", lambda s: visited.append("llm_provider")),
            ("audio", lambda s: visited.append("audio")),
        ],
    )
    asyncio.run(machine.run())

    # Operator declined the resume → wizard re-walks every step from
    # the beginning. (The sidecar gets re-written as each new step
    # completes, but it's the start-from-0 behaviour we're pinning.)
    assert visited == ["welcome", "llm_provider", "audio"]


def test_state_machine_quit_does_not_mark_complete_but_writes_sidecar(
    feral_home, monkeypatch,
):
    """Operator quits at step 2 → sidecar exists with last completed
    step + ``setup_complete`` is NOT True."""
    import asyncio
    from cli.setup.state import WizardState
    from cli.setup.state_machine import StateMachine
    from cli.setup.helpers import QuitNavigation
    import cli.setup.state_machine as sm_mod

    state = WizardState.load(feral_home)
    monkeypatch.setattr(sm_mod, "confirm", lambda *a, **kw: True)

    def quit_step(_state):
        raise QuitNavigation()

    machine = StateMachine(
        state=state,
        steps=[
            ("welcome", lambda s: None),
            ("llm_provider", quit_step),
            ("finish", lambda s: state.mark_complete()),
        ],
    )
    asyncio.run(machine.run())

    # Sidecar exists pointing at the LAST successfully completed step
    # (welcome). The quit_step never wrote a sidecar.
    sidecar = WizardState.read_setup_state(feral_home)
    assert sidecar.get("last_step") == "welcome"

    settings = json.loads((feral_home / "settings.json").read_text())
    assert settings.get("meta", {}).get("setup_complete") is not True
