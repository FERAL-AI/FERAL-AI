"""Lane U1 — ``feral models add`` + multi-model favorites contract.

Covers the additive ``llm.models[]`` list that ``feral models add``
appends to and that ``feral models set`` keeps deduped without ever
clobbering the operator's previous favorites.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_feral_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path))


def test_models_add_appends_second_model_to_list(tmp_path):
    """Two ``feral models add`` invocations with different ``--model``
    must produce a length-2 ``llm.models`` list containing both ids.
    """
    from cli.model_commands import cmd_models_add

    rc1 = cmd_models_add(provider="openai", model="gpt-4o")
    assert rc1 == 0
    rc2 = cmd_models_add(provider="openai", model="gpt-4o-mini")
    assert rc2 == 0

    settings = json.loads((tmp_path / "settings.json").read_text())
    models = settings["llm"]["models"]
    assert isinstance(models, list)
    assert len(models) == 2, f"expected 2 entries, got {models!r}"
    assert "gpt-4o" in models
    assert "gpt-4o-mini" in models


def test_models_add_dedupes_same_model(tmp_path):
    """Re-running ``feral models add`` with the same ``--model`` must
    be a no-op on the favorites list (idempotent)."""
    from cli.model_commands import cmd_models_add

    rc1 = cmd_models_add(provider="openai", model="gpt-4o")
    assert rc1 == 0
    rc2 = cmd_models_add(provider="openai", model="gpt-4o")
    assert rc2 == 0

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["llm"]["models"] == ["gpt-4o"]


def test_models_set_also_appends_to_models_list(tmp_path):
    """The existing scalar ``llm.model`` write must also populate
    ``llm.models[]`` so the favorites list stays in sync with the
    active model."""
    from cli.model_commands import cmd_models_set

    rc = cmd_models_set(provider="openai", model="gpt-5")
    assert rc == 0

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["llm"]["provider"] == "openai"
    assert settings["llm"]["model"] == "gpt-5"
    assert "gpt-5" in settings["llm"]["models"]


def test_models_set_does_not_remove_existing_favorites(tmp_path):
    """``feral models set`` must NOT clobber previously-added
    favorites — the list is additive."""
    from cli.model_commands import cmd_models_add, cmd_models_set

    assert cmd_models_add(provider="openai", model="gpt-4o-mini") == 0
    assert cmd_models_set(provider="openai", model="gpt-5") == 0

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "gpt-4o-mini" in settings["llm"]["models"]
    assert "gpt-5" in settings["llm"]["models"]
    assert settings["llm"]["model"] == "gpt-5"


def test_models_add_rejects_missing_provider(capsys):
    from cli.model_commands import cmd_models_add

    rc = cmd_models_add(provider="", model="gpt-4o")
    out = capsys.readouterr().out
    assert rc == 2
    assert "--provider" in out


def test_models_add_subcommand_registered():
    """The ``add`` action must appear in the argparse dispatch table
    so ``feral models add`` is wired end-to-end."""
    import argparse
    from cli.model_commands import register_models_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="subcommand")
    register_models_subparser(sub)
    args = parser.parse_args(["models", "add", "--provider", "openai", "--model", "gpt-4o"])
    assert args.action == "add"
    assert args.provider == "openai"
    assert args.model == "gpt-4o"


def test_setup_from_step_flag_jumps_to_named_step(tmp_path):
    """Lane U1 fix #8 — ``feral setup --from-step llm_model`` must
    skip the resume-sidecar prompt and execute only the named step
    (plus whatever follows it in the wizard order)."""
    import asyncio
    from cli.setup.state import WizardState
    from cli.setup.state_machine import StateMachine

    state = WizardState.load(tmp_path / "feral")
    executed: list[str] = []

    def step(name: str):
        def _run(_state):
            executed.append(name)
        return _run

    machine = StateMachine(
        state=state,
        from_step="llm_model",
        steps=[
            ("welcome", step("welcome")),
            ("llm_provider", step("llm_provider")),
            ("llm_model", step("llm_model")),
            ("audio", step("audio")),
        ],
    )
    asyncio.run(machine.run())
    # Must NOT execute welcome / llm_provider — those are upstream of
    # the --from-step target.
    assert "welcome" not in executed
    assert "llm_provider" not in executed
    assert executed[0] == "llm_model"
    assert "audio" in executed


def test_setup_from_step_flag_unknown_step_aborts(tmp_path, capsys):
    """Unknown ``--from-step`` value must abort cleanly with a hint
    listing the known step names — never silently run the full wizard.
    """
    import asyncio
    from cli.setup.state import WizardState
    from cli.setup.state_machine import StateMachine

    state = WizardState.load(tmp_path / "feral")
    executed: list[str] = []

    def step(name: str):
        def _run(_state):
            executed.append(name)
        return _run

    machine = StateMachine(
        state=state,
        from_step="bogus_step_name",
        steps=[
            ("welcome", step("welcome")),
            ("llm_model", step("llm_model")),
        ],
    )
    asyncio.run(machine.run())
    assert executed == []
    out = capsys.readouterr().out
    assert "bogus_step_name" in out
    assert "Known" in out or "known" in out
