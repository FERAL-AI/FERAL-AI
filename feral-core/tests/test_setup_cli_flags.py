"""``--quick``, ``--non-interactive``, ``--reset``, friendly step names.

`feral setup` had three flags: `--terminal`, `--browser`, `--from-step`.
Comparable tools ship a headless mode, a "only ask what is missing" mode,
a reset, and section names an operator can guess. Those are the gap this
file covers.

The two that carry real risk:

`--non-interactive` must never invent an answer. A wizard that silently
writes an empty API key has produced a broken install and reported
success, which is worse than refusing. Questions with a declared default
take it; questions without one raise `MissingRequiredSetting` naming the
prompt.

`--reset` must not destroy credentials. It moves config into
`~/.feral/backups/<stamp>/` rather than deleting it, so a mistyped flag
costs a rename rather than re-provisioning every API key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.setup import STEP_ALIASES, reset_config, resolve_step_name  # noqa: E402
from cli.setup import helpers  # noqa: E402
from cli.setup.helpers import (  # noqa: E402
    MissingRequiredSetting,
    Option,
    ask_choice,
    ask_text,
    confirm,
    set_non_interactive,
)
from cli.setup.state import WizardState  # noqa: E402
from cli.setup.state_machine import StateMachine  # noqa: E402


@pytest.fixture
def headless():
    """Turn the mode on for one test and off again afterwards.

    It is module-level state, so a leak would silently make every later
    test in the process non-interactive.
    """
    set_non_interactive(True)
    try:
        yield
    finally:
        set_non_interactive(False)


# ----------------------------------------------------------------------
# --non-interactive
# ----------------------------------------------------------------------

def test_the_mode_is_off_by_default():
    assert helpers.is_non_interactive() is False


class TestNonInteractiveTakesDefaults:
    @pytest.mark.parametrize("default", [True, False])
    def test_confirm_returns_its_default(self, headless, default):
        assert confirm("Set this up now?", default=default) is default

    def test_ask_text_returns_its_default(self, headless):
        assert ask_text("Your name", default="ada") == "ada"

    def test_ask_text_allows_empty_when_the_field_does(self, headless):
        assert ask_text("Nickname", allow_empty=True) == ""

    def test_ask_choice_returns_its_default(self, headless):
        options = [Option(id="a", label="A"), Option(id="b", label="B")]
        assert ask_choice("Pick", options, default="b").id == "b"

    def test_pick_returns_its_default(self, headless):
        assert helpers.pick("Pick", [{"name": "x", "value": "x"}], default="x") == "x"


class TestNonInteractiveRefusesToGuess:
    """The half that makes the mode safe to script against."""

    def test_a_required_free_text_field_fails(self, headless):
        with pytest.raises(MissingRequiredSetting) as excinfo:
            ask_text("OpenAI API key", allow_empty=False)
        assert "OpenAI API key" in str(excinfo.value)

    def test_a_choice_with_no_default_fails(self, headless):
        """Picking options[0] would be the wizard choosing the provider."""
        options = [Option(id="anthropic", label="Anthropic"), Option(id="openai", label="OpenAI")]
        with pytest.raises(MissingRequiredSetting):
            ask_choice("Choose a provider", options)

    def test_pick_with_no_default_fails(self, headless):
        with pytest.raises(MissingRequiredSetting):
            helpers.pick("Choose", [{"name": "x", "value": "x"}])

    def test_the_error_names_the_prompt_and_what_to_do(self, headless):
        with pytest.raises(MissingRequiredSetting) as excinfo:
            ask_text("Anthropic API key", allow_empty=False)
        message = str(excinfo.value)
        assert "Anthropic API key" in message
        assert "settings.json" in message or "environment" in message


def test_the_mode_stops_the_reader_from_blocking(headless):
    """``_prompt_raw`` must not reach stdin with nobody there."""
    assert helpers._can_prompt() is False


# ----------------------------------------------------------------------
# --quick
# ----------------------------------------------------------------------

class TestQuick:
    @staticmethod
    def _steps(visited):
        return [
            ("welcome", lambda s: visited.append("welcome")),
            ("llm_provider", lambda s: visited.append("llm_provider")),
            ("llm_model", lambda s: visited.append("llm_model")),
            ("audio", lambda s: visited.append("audio")),
            ("finish", lambda s: visited.append("finish")),
        ]

    def _run(self, state, quick):
        import asyncio

        visited: list[str] = []
        machine = StateMachine(state=state, steps=self._steps(visited), quick=quick)
        asyncio.run(machine.run())
        return visited

    def test_it_steps_over_what_is_already_answered(self, tmp_path):
        state = WizardState.load(tmp_path / "feral")
        state.completed_steps.update({"welcome", "llm_provider", "llm_model"})

        assert self._run(state, quick=True) == ["audio", "finish"]

    def test_a_later_gap_is_still_asked_about(self, tmp_path):
        """Quick is not resume. Resume jumps to the first gap and walks
        everything after it; quick keeps walking and skips only what is
        answered, so a hole in the middle is still filled."""
        state = WizardState.load(tmp_path / "feral")
        state.completed_steps.update({"welcome", "llm_provider", "audio"})

        assert self._run(state, quick=True) == ["llm_model", "finish"]

    def test_finish_is_never_skipped(self, tmp_path):
        """It writes the config and prints the summary."""
        state = WizardState.load(tmp_path / "feral")
        state.completed_steps.update(
            {"welcome", "llm_provider", "llm_model", "audio", "finish"}
        )

        assert self._run(state, quick=True) == ["finish"]

    def test_without_quick_everything_runs(self, tmp_path):
        state = WizardState.load(tmp_path / "feral")
        state.completed_steps.update({"welcome", "llm_provider", "llm_model"})

        assert self._run(state, quick=False) == [
            "welcome", "llm_provider", "llm_model", "audio", "finish",
        ]


# ----------------------------------------------------------------------
# --reset
# ----------------------------------------------------------------------

class TestReset:
    _FILES = ("settings.json", "identity.json", "setup_state.json", "credentials.enc")

    def _home(self, tmp_path):
        home = tmp_path / "feral"
        home.mkdir()
        for name in self._FILES:
            (home / name).write_text(f"contents of {name}")
        return home

    def test_it_moves_config_out_of_the_way(self, tmp_path):
        home = self._home(tmp_path)
        moved = reset_config(home)

        assert len(moved) == len(self._FILES)
        for name in self._FILES:
            assert not (home / name).exists(), f"{name} is still live"

    def test_it_does_not_destroy_credentials(self, tmp_path):
        """The whole point. A mistyped flag costs a rename, not every key."""
        home = self._home(tmp_path)
        reset_config(home)

        backups = list((home / "backups").glob("reset-*/credentials.enc"))
        assert len(backups) == 1
        assert backups[0].read_text() == "contents of credentials.enc"

    def test_it_is_a_no_op_on_a_fresh_install(self, tmp_path):
        home = tmp_path / "feral"
        home.mkdir()
        assert reset_config(home) == []
        assert not (home / "backups").exists()

    def test_two_resets_do_not_collide(self, tmp_path, monkeypatch):
        """Same-second resets must not overwrite the first backup."""
        home = self._home(tmp_path)
        reset_config(home)
        for name in self._FILES:
            (home / name).write_text("second round")

        stamps = iter(["20260829-120000", "20260829-120001"])
        monkeypatch.setattr(
            "time.strftime", lambda *a, **kw: next(stamps, "20260829-120002")
        )
        reset_config(home)

        assert len(list((home / "backups").glob("reset-*"))) == 2


# ----------------------------------------------------------------------
# friendly --from-step names
# ----------------------------------------------------------------------

@pytest.mark.parametrize("friendly,internal", [
    ("model", "llm_model"),
    ("provider", "llm_provider"),
    ("voice", "voice_preflight"),
    ("tools", "tool_keys"),
    ("phone", "pairing"),
    ("permissions", "tcc_preflight"),
    ("network", "network"),
])
def test_friendly_names_resolve(friendly, internal):
    assert resolve_step_name(friendly) == internal


def test_internal_ids_still_work():
    """The documented flag values must not stop working."""
    assert resolve_step_name("llm_model") == "llm_model"
    assert resolve_step_name("tcc_preflight") == "tcc_preflight"


def test_names_are_case_and_space_insensitive():
    assert resolve_step_name("  Model  ") == "llm_model"


def test_an_unknown_name_passes_through():
    """The state machine reports it, listing every valid id."""
    assert resolve_step_name("nonsense") == "nonsense"


def test_every_alias_points_at_a_real_step():
    """An alias for a step that does not exist is a dead flag value."""
    import re

    src = (ROOT / "cli" / "setup" / "__init__.py").read_text()
    block = re.search(r"steps=\[(.*?)\n        \],", src, re.S).group(1)
    real = set(re.findall(r'\("([a-z_]+)",', block))

    dangling = {k: v for k, v in STEP_ALIASES.items() if v not in real}
    assert not dangling, f"aliases point at steps that do not exist: {dangling}"
