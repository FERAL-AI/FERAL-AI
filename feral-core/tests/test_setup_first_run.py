"""The first screen a new operator sees, and the navigation off it.

Four defects, all on the first run, all reported from a screenshot of a
real ``feral setup``:

1. ``back`` from step 1 reprinted an identical "Step 1 of 16" header
   with nothing between the two copies.
2. The welcome's numbered list was in a different order from the wizard
   (it put network fourth; network is step 9).
3. The welcome omitted two steps entirely -- memory and system
   permissions.
4. The welcome promised "a few steps" over six lines and the very next
   line said "Step 1 of 16", with nothing explaining that those count
   different things.

(1) is the interesting one. ``welcome`` is index 0 and returns early
once it is in ``completed_steps``, so it renders nothing on re-entry --
a guard added to stop the ASCII banner appearing twice. ``back`` from
step 1 therefore moved to welcome, printed nothing, and bounced straight
forward again. The ``idx == 0`` guard that was supposed to catch this
never fired, because the operator is at index 1 when they press back.

That earlier fix made the symptom *worse*: before it, ``back`` at least
showed the banner. After it, an identical header appeared with no cause
on screen at all.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.setup import state_machine as sm_mod  # noqa: E402
from cli.setup.helpers import BackNavigation, JumpToStep  # noqa: E402
from cli.setup.state import WizardState  # noqa: E402
from cli.setup.state_machine import StateMachine  # noqa: E402


class _RecordingConsole:
    """Captures what the operator would actually see."""

    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))

    def __getattr__(self, _name):
        return lambda *a, **kw: None

    def headers(self) -> list[str]:
        return [ln for ln in self.lines if "Step " in ln and " of " in ln]


@pytest.fixture
def console(monkeypatch) -> _RecordingConsole:
    rec = _RecordingConsole()
    monkeypatch.setattr(sm_mod, "get_console", lambda: rec)
    return rec


def _machine(state, steps, console):
    machine = StateMachine(state=state, steps=steps)
    machine.console = console
    return machine


def _run(machine):
    asyncio.run(machine.run())


# ----------------------------------------------------------------------
# 1. back from the first step
# ----------------------------------------------------------------------

def test_back_from_the_first_step_does_not_reprint_the_header(tmp_path, console):
    """The screenshot: two identical headers, nothing between them."""
    calls = {"n": 0}

    def first_step(_state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BackNavigation()

    _run(_machine(
        WizardState.load(tmp_path / "feral"),
        [
            ("welcome", lambda s: None),
            ("llm_provider", first_step),
            ("finish", lambda s: None),
        ],
        console,
    ))

    assert calls["n"] == 2, "the step should be re-offered, not skipped"
    headers = console.headers()
    assert len(headers) == 1, (
        f"header printed {len(headers)} times for one step: {headers}"
    )


def test_back_from_the_first_step_says_why_nothing_happened(tmp_path, console):
    """Silence is what made this read as a glitch."""
    calls = {"n": 0}

    def first_step(_state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BackNavigation()

    _run(_machine(
        WizardState.load(tmp_path / "feral"),
        [("welcome", lambda s: None), ("llm_provider", first_step)],
        console,
    ))

    assert any("first step" in ln for ln in console.lines), (
        f"nothing told the operator why back did nothing: {console.lines}"
    )


def test_back_does_not_land_on_a_step_that_renders_nothing(tmp_path, console):
    """``welcome`` must be stepped over, not onto."""
    visited: list[str] = []
    calls = {"n": 0}

    def second_step(_state):
        visited.append("llm_model")
        calls["n"] += 1
        if calls["n"] == 1:
            raise BackNavigation()

    _run(_machine(
        WizardState.load(tmp_path / "feral"),
        [
            ("welcome", lambda s: visited.append("welcome")),
            ("llm_provider", lambda s: visited.append("llm_provider")),
            ("llm_model", second_step),
        ],
        console,
    ))

    # back from step 2 lands on step 1, which is a real step.
    assert visited == [
        "welcome", "llm_provider", "llm_model", "llm_provider", "llm_model",
    ], visited


# ----------------------------------------------------------------------
# jump navigation, which shares the re-prompt path
# ----------------------------------------------------------------------

def test_cancelling_a_jump_does_not_reprint_the_header(tmp_path, console, monkeypatch):
    """Same defect, different door."""
    calls = {"n": 0}

    def step(_state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JumpToStep("")

    machine = _machine(
        WizardState.load(tmp_path / "feral"),
        [("welcome", lambda s: None), ("llm_provider", step)],
        console,
    )
    # Operator cancelled the picker.
    monkeypatch.setattr(machine, "_resolve_jump_target", lambda *a, **kw: None)
    _run(machine)

    assert len(console.headers()) == 1, console.headers()


def test_jumping_to_welcome_is_refused(tmp_path, console):
    """It renders nothing, so the jump would look ignored."""
    machine = _machine(
        WizardState.load(tmp_path / "feral"),
        [("welcome", lambda s: None), ("llm_provider", lambda s: None)],
        console,
    )
    assert machine._resolve_jump_target("welcome", current_idx=1) is None


def test_jumping_to_finish_is_allowed(tmp_path, console):
    """It renders, and it is how the fast path wraps up early."""
    machine = _machine(
        WizardState.load(tmp_path / "feral"),
        [
            ("welcome", lambda s: None),
            ("llm_provider", lambda s: None),
            ("finish", lambda s: None),
        ],
        console,
    )
    assert machine._resolve_jump_target("finish", current_idx=1) == 2


# ----------------------------------------------------------------------
# 2/3/4. the welcome text must describe the wizard that exists
# ----------------------------------------------------------------------

def _welcome_body() -> str:
    return (ROOT / "cli" / "setup" / "steps" / "welcome.py").read_text()


def _real_step_names() -> list[str]:
    import re

    src = (ROOT / "cli" / "setup" / "__init__.py").read_text()
    block = re.search(r"steps=\[(.*?)\n        \],", src, re.S).group(1)
    return re.findall(r'\("([a-z_]+)",', block)


def test_the_wizard_still_has_sixteen_visible_steps():
    """The welcome names ranges up to 16. If the count moves, so must it."""
    visible = [n for n in _real_step_names() if n not in sm_mod._NO_INDICATOR_STEPS]
    assert len(visible) == 16, (
        f"visible step count is now {len(visible)}; the welcome text in "
        "steps/welcome.py names ranges up to 16 and has to be updated with it"
    )


def test_the_fast_path_checkpoint_is_not_a_numbered_step():
    """Announcing "you are done after two" as a 17th step would be a joke."""
    assert "ready" in sm_mod._NO_INDICATOR_STEPS


def test_the_checkpoint_sits_immediately_after_the_model_step():
    names = _real_step_names()
    assert names[names.index("llm_model") + 1] == "ready", names


@pytest.mark.parametrize("topic", ["Memory", "permissions"])
def test_the_welcome_no_longer_omits_a_step(topic):
    """Memory (8) and system permissions (16) were missing entirely."""
    assert topic in _welcome_body()


def test_the_welcome_tells_the_operator_where_the_required_part_ends():
    """The 6-vs-16 confusion: both numbers were true, neither was explained."""
    body = _welcome_body()
    assert "Steps 1-2" in body
    assert "optional" in body


def test_the_welcome_does_not_promise_a_bare_step_count_it_contradicts():
    """It used to say "a few steps" over six lines, then "Step 1 of 16"."""
    assert "in a few steps:" not in _welcome_body()


# ----------------------------------------------------------------------
# The fast-path checkpoint
# ----------------------------------------------------------------------

class TestReadyCheckpoint:
    """Two questions in, the operator has a working brain. Say so."""

    @staticmethod
    def _state(tmp_path):
        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "anthropic")
        state.set_setting("llm", "model", "claude-opus-4")
        return state

    @staticmethod
    def _answer(monkeypatch, *values):
        """Feed ``ui_kit.pick`` a scripted sequence of choices."""
        from cli.setup.steps import ready as ready_mod

        seq = list(values)
        monkeypatch.setattr(
            ready_mod.ui_kit, "pick", lambda *a, **kw: seq.pop(0)
        )
        return seq

    def test_starting_now_jumps_to_finish(self, tmp_path, monkeypatch, console):
        """Returning would advance into the optional steps instead."""
        from cli.setup.steps import ready as ready_mod

        monkeypatch.setattr(ready_mod, "get_console", lambda: console)
        self._answer(monkeypatch, "start")

        with pytest.raises(JumpToStep) as excinfo:
            ready_mod.run(self._state(tmp_path))

        assert excinfo.value.target == "finish"

    def test_keeping_going_just_returns(self, tmp_path, monkeypatch, console):
        from cli.setup.steps import ready as ready_mod

        monkeypatch.setattr(ready_mod, "get_console", lambda: console)
        self._answer(monkeypatch, "continue")

        assert ready_mod.run(self._state(tmp_path)) is None

    def test_showing_whats_left_re_asks(self, tmp_path, monkeypatch, console):
        """The list is information, not a decision."""
        from cli.setup.steps import ready as ready_mod

        monkeypatch.setattr(ready_mod, "get_console", lambda: console)
        remaining = self._answer(monkeypatch, "show", "continue")

        ready_mod.run(self._state(tmp_path))

        assert remaining == [], "the operator was not asked again after the list"
        assert any("Memory" in ln for ln in console.lines), console.lines

    def test_it_renders_once(self, tmp_path, monkeypatch, console):
        """On a ``back`` from the step after it, or a resume, the
        operator has already answered this fork."""
        from cli.setup.steps import ready as ready_mod

        monkeypatch.setattr(ready_mod, "get_console", lambda: console)
        state = self._state(tmp_path)
        state.completed_steps.add("ready")

        def _explode(*a, **kw):
            raise AssertionError("the operator was asked the same fork twice")

        monkeypatch.setattr(ready_mod.ui_kit, "pick", _explode)
        assert ready_mod.run(state) is None

    def test_it_names_the_provider_the_operator_chose(
        self, tmp_path, monkeypatch, console
    ):
        """"Your brain works" is more convincing with the evidence."""
        from cli.setup.steps import ready as ready_mod

        monkeypatch.setattr(ready_mod, "get_console", lambda: console)
        self._answer(monkeypatch, "continue")

        ready_mod.run(self._state(tmp_path))

        blob = "\n".join(console.lines)
        assert "anthropic" in blob and "claude-opus-4" in blob
