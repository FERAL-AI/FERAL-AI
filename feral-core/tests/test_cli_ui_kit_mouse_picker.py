"""``ui_kit.pick`` runs on prompt_toolkit so the operator can click.

Why this file exists
--------------------
The wizard's list prompts were arrow-key only. InquirerPy's
``inquirer.select`` exposes no mouse parameter at all, so there was no
way to reach ``prompt_toolkit.Application(mouse_support=...)`` through
it. ``pick`` (the direct single-pick, 7 call sites under ``cli/setup``)
is now built straight on prompt_toolkit; ``select``, ``multi_select``,
``fuzzy_select``, ``fuzzy_pick``, ``password``, ``confirm`` and ``text``
stay on InquirerPy.

What is actually asserted here
------------------------------
Mouse *reporting* is a terminal-level escape-sequence conversation that
pytest cannot have, so nothing below claims to have clicked a real
terminal. What is testable, and is tested:

* the ``mouse_support`` flag handed to ``Application`` is on by default
  and off under the documented opt-out env var;
* the click / scroll handlers attached to each rendered row do what
  they claim when invoked with a ``MouseEvent``;
* up / down / enter / ctrl-c are bound, do not cycle at the ends, and
  ctrl-c exits with ``KeyboardInterrupt`` (which ``ask_choice`` turns
  into ``QuitNavigation``);
* the rows rendered are byte-identical in label and order to what
  ``_fallback_select`` prints, so the two cannot drift;
* every degradation path (no TTY, no prompt_toolkit, construction
  blows up) still lands on ``_fallback_select``.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys

import pytest
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from cli import ui_kit


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeApp:
    """Stands in for a running ``Application``.

    ``Application.exit()`` raises unless the app is genuinely running,
    so handlers are exercised against this instead. The handlers reach
    the app through ``event.app``, never through a closure, which is
    what makes the substitution honest.
    """

    def __init__(self, result=None):
        self._result = result
        self.exit_calls: list[dict] = []
        self.invalidated = 0

    def exit(self, **kwargs):
        self.exit_calls.append(kwargs)

    def invalidate(self):
        self.invalidated += 1

    def run(self):
        return self._result


class _FakeEvent:
    def __init__(self, app):
        self.app = app


@contextlib.contextmanager
def mouse_env(value):
    """Set (or clear) the opt-out variable, restoring it inside the test.

    ``monkeypatch.setenv`` would do this, but conftest's env-leak guard
    samples the environment before monkeypatch's undo runs and reports
    every such test as a leak. Restoring inline keeps that report
    meaning what it says.
    """
    previous = os.environ.get(ui_kit.MOUSE_ENV_VAR)
    if value is None:
        os.environ.pop(ui_kit.MOUSE_ENV_VAR, None)
    else:
        os.environ[ui_kit.MOUSE_ENV_VAR] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ui_kit.MOUSE_ENV_VAR, None)
        else:
            os.environ[ui_kit.MOUSE_ENV_VAR] = previous


def _picker(choices, *, message="Pick one", default=None, instruction="hint", mouse=None):
    """``mouse=None`` reads the env var, which is what production does."""
    return ui_kit._MousePicker(
        message,
        ui_kit._fallback_pairs(choices),
        default=default,
        instruction=instruction,
        mouse=mouse,
    )


def _build(picker):
    """Build the real Application without touching the real terminal."""
    return picker.build_application(input=DummyInput(), output=DummyOutput())


def _handler_for_row(picker, index):
    """The mouse handler prompt_toolkit would call for that row."""
    for style, text, *rest in picker.fragments():
        del style, text
        if rest and getattr(rest[0], "row_index", None) == index:
            return rest[0]
    return None


def _click(handler, event_type, *, button=MouseButton.LEFT):
    return handler(
        MouseEvent(
            position=Point(x=0, y=0),
            event_type=event_type,
            button=button,
            modifiers=frozenset(),
        )
    )


PROVIDERS = [
    {"name": "OpenAI", "value": "openai"},
    {"name": "Anthropic", "value": "anthropic"},
    {"name": "Ollama (local)", "value": "ollama"},
]


# ---------------------------------------------------------------------------
# Mouse capture is opt-out, because it costs drag-to-select
# ---------------------------------------------------------------------------


class TestMouseOptOut:
    def test_mouse_is_on_by_default(self):
        with mouse_env(None):
            assert ui_kit.mouse_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "off", "false", "no", "OFF", " 0 "])
    def test_documented_opt_out_values_turn_it_off(self, raw):
        with mouse_env(raw):
            assert ui_kit.mouse_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "on", "true", "yes", ""])
    def test_other_values_leave_it_on(self, raw):
        with mouse_env(raw):
            assert ui_kit.mouse_enabled() is True

    def test_env_var_is_in_the_feral_family(self):
        assert ui_kit.MOUSE_ENV_VAR.startswith("FERAL_")

    def test_application_requests_mouse_support_by_default(self):
        with mouse_env(None):
            app = _build(_picker(PROVIDERS))
        assert bool(app.mouse_support()) is True

    def test_application_does_not_request_mouse_when_opted_out(self):
        with mouse_env("0"):
            app = _build(_picker(PROVIDERS))
        assert bool(app.mouse_support()) is False

    def test_opted_out_rows_carry_no_click_handler(self):
        """Belt and braces: no handler to fire even if a stray event arrives."""
        with mouse_env("0"):
            picker = _picker(PROVIDERS)
        assert _handler_for_row(picker, 1) is None

    def test_instruction_only_advertises_clicking_when_it_works(self):
        with mouse_env(None):
            on = _picker(PROVIDERS).header_text()
        with mouse_env("0"):
            off = _picker(PROVIDERS).header_text()
        assert "click" in on.lower()
        assert "click" not in off.lower()


# ---------------------------------------------------------------------------
# The picker and the typed fallback must show the same list
# ---------------------------------------------------------------------------


def _fallback_labels(choices) -> list[str]:
    """Run the real ``_fallback_select`` and read the list off its stdout."""
    buf = io.StringIO()
    stdin = io.StringIO("1\n")
    real_out, real_in = sys.stdout, sys.stdin
    sys.stdout, sys.stdin = buf, stdin
    try:
        ui_kit._fallback_select("Pick one", choices)
    finally:
        sys.stdout, sys.stdin = real_out, real_in
    labels = []
    for line in buf.getvalue().splitlines():
        m = re.match(r"^\s+\d+\. (.*)$", line)
        if m:
            labels.append(m.group(1))
    return labels


class TestRenderedListMatchesFallback:
    @pytest.mark.parametrize(
        "choices",
        [
            ["alpha", "beta", "gamma"],
            PROVIDERS,
            [{"name": "Only name"}, {"value": "only-value"}],
        ],
        ids=["strings", "dicts", "half-specified-dicts"],
    )
    def test_same_labels_in_the_same_order(self, choices):
        assert _picker(choices).row_labels() == _fallback_labels(choices)

    def test_inquirer_choice_objects_render_the_same_way(self):
        if not ui_kit.is_inquirer_available():
            pytest.skip("InquirerPy not installed")
        choices = [
            ui_kit.Choice(value="openai", name="OpenAI"),
            ui_kit.Choice(value="ollama", name="Ollama (local)"),
        ]
        assert _picker(choices).row_labels() == _fallback_labels(choices)

    def test_every_row_is_rendered(self):
        picker = _picker(PROVIDERS)
        assert len(picker.row_labels()) == len(PROVIDERS)


# ---------------------------------------------------------------------------
# Keyboard stays first-class
# ---------------------------------------------------------------------------


class TestKeyboard:
    @pytest.mark.parametrize(
        "key",
        [Keys.Up, Keys.Down, Keys.ControlM, Keys.ControlC, Keys.ControlP, Keys.ControlN],
    )
    def test_key_is_bound(self, key):
        app = _build(_picker(PROVIDERS))
        assert app.key_bindings.get_bindings_for_keys((key,)), f"{key} is not bound"

    def _handler(self, app, key):
        return app.key_bindings.get_bindings_for_keys((key,))[-1].handler

    def test_down_then_up_returns_to_the_start(self):
        picker = _picker(PROVIDERS)
        app = _build(picker)
        fake = _FakeApp()
        self._handler(app, Keys.Down)(_FakeEvent(fake))
        assert picker.index == 1
        self._handler(app, Keys.Up)(_FakeEvent(fake))
        assert picker.index == 0

    def test_up_at_the_top_does_not_wrap(self):
        """``cycle=False`` was the InquirerPy setting; keep it."""
        picker = _picker(PROVIDERS)
        app = _build(picker)
        self._handler(app, Keys.Up)(_FakeEvent(_FakeApp()))
        assert picker.index == 0

    def test_down_at_the_bottom_does_not_wrap(self):
        picker = _picker(PROVIDERS)
        app = _build(picker)
        handler = self._handler(app, Keys.Down)
        for _ in range(10):
            handler(_FakeEvent(_FakeApp()))
        assert picker.index == len(PROVIDERS) - 1

    def test_enter_commits_the_row_under_the_cursor(self):
        picker = _picker(PROVIDERS)
        app = _build(picker)
        fake = _FakeApp()
        self._handler(app, Keys.Down)(_FakeEvent(fake))
        self._handler(app, Keys.ControlM)(_FakeEvent(fake))
        assert fake.exit_calls == [{"result": 1}]

    def test_ctrl_c_exits_with_keyboard_interrupt(self):
        """``ask_choice`` converts this into ``QuitNavigation``."""
        app = _build(_picker(PROVIDERS))
        fake = _FakeApp()
        self._handler(app, Keys.ControlC)(_FakeEvent(fake))
        assert len(fake.exit_calls) == 1
        exc = fake.exit_calls[0]["exception"]
        assert exc is KeyboardInterrupt or isinstance(exc, KeyboardInterrupt)

    def test_default_places_the_cursor_on_the_default_row(self):
        assert _picker(PROVIDERS, default="ollama").index == 2

    def test_unknown_default_starts_at_the_top(self):
        assert _picker(PROVIDERS, default="not-a-provider").index == 0

    def test_cursor_position_follows_the_selection_so_long_lists_scroll(self):
        picker = _picker(PROVIDERS)
        first = picker.cursor_position()
        picker.move(1)
        assert isinstance(first, Point)
        assert picker.cursor_position().y == first.y + 1


# ---------------------------------------------------------------------------
# Mouse handlers
# ---------------------------------------------------------------------------


class TestMouseHandlers:
    def test_press_moves_the_cursor_without_committing(self):
        picker = _picker(PROVIDERS, mouse=True)
        picker.app = fake = _FakeApp()
        _click(_handler_for_row(picker, 2), MouseEventType.MOUSE_DOWN)
        assert picker.index == 2
        assert fake.exit_calls == []

    def test_release_on_the_pressed_row_commits_it(self):
        picker = _picker(PROVIDERS, mouse=True)
        picker.app = fake = _FakeApp()
        handler = _handler_for_row(picker, 1)
        _click(handler, MouseEventType.MOUSE_DOWN)
        _click(handler, MouseEventType.MOUSE_UP)
        assert fake.exit_calls == [{"result": 1}]

    def test_release_on_a_different_row_than_the_press_does_not_commit(self):
        """A drag off the row is a cancelled click, the way a button behaves."""
        picker = _picker(PROVIDERS, mouse=True)
        picker.app = fake = _FakeApp()
        _click(_handler_for_row(picker, 0), MouseEventType.MOUSE_DOWN)
        _click(_handler_for_row(picker, 2), MouseEventType.MOUSE_UP)
        assert fake.exit_calls == []

    def test_scroll_moves_the_cursor_without_committing(self):
        picker = _picker(PROVIDERS, mouse=True)
        picker.app = fake = _FakeApp()
        _click(_handler_for_row(picker, 0), MouseEventType.SCROLL_DOWN)
        assert picker.index == 1
        _click(_handler_for_row(picker, 0), MouseEventType.SCROLL_UP)
        assert picker.index == 0
        assert fake.exit_calls == []

    def test_a_row_click_does_not_swallow_the_row_label(self):
        """The handler is attached to the same fragment that draws the row."""
        picker = _picker(PROVIDERS, mouse=True)
        rendered = "".join(text for _style, text, *_ in picker.fragments())
        for label in picker.row_labels():
            assert label in rendered


# ---------------------------------------------------------------------------
# End to end: real escape sequences through a real Application
# ---------------------------------------------------------------------------


def _drive(data, *, mouse=True, default=None, choices=PROVIDERS):
    """Run the real picker over a pipe, feeding it terminal bytes.

    This is the closest a test can get to a terminal without one: the
    same ``Application``, the same vt100 parser, the same key and mouse
    dispatch. Only the file descriptors are ours.
    """
    picker = _picker(choices, message="Provider", default=default, mouse=mouse)
    with create_pipe_input() as pipe:
        pipe.send_text(data)
        app = picker.build_application(input=pipe, output=DummyOutput())
        index = app.run()
    return picker.rows[index][1] if isinstance(index, int) else index


# SGR mouse reports: button 0 press (M) / release (m) at column 5, and
# wheel-down (button 65). Row is 1-based, and row 1 is the header, so
# row 3 is the second choice.
def _sgr(button, row, *, press=True):
    return f"\x1b[<{button};5;{row}{'M' if press else 'm'}"


class TestEndToEnd:
    def test_arrow_keys_and_enter(self):
        assert _drive("\x1b[B\x1b[B\r") == "ollama"

    def test_down_then_up_lands_where_it_started(self):
        assert _drive("\x1b[B\x1b[A\r") == "openai"

    def test_up_at_the_top_does_not_wrap_to_the_bottom(self):
        assert _drive("\x1b[A\r") == "openai"

    def test_enter_takes_the_default_without_any_navigation(self):
        assert _drive("\r", default="ollama") == "ollama"

    def test_ctrl_c_raises_out_of_the_application(self):
        with pytest.raises(KeyboardInterrupt):
            _drive("\x03")

    @pytest.mark.parametrize(
        "row, expected",
        [(2, "openai"), (3, "anthropic"), (4, "ollama")],
    )
    def test_a_click_on_a_row_picks_that_row(self, row, expected):
        assert _drive(_sgr(0, row) + _sgr(0, row, press=False)) == expected

    def test_the_wheel_moves_the_cursor(self):
        assert _drive(_sgr(65, 2) + "\r") == "anthropic"

    def test_a_click_does_nothing_when_the_mouse_is_opted_out(self):
        """The terminal would not even send this, but prove it is inert."""
        clicked = _sgr(0, 4) + _sgr(0, 4, press=False)
        assert _drive(clicked + "\r", mouse=False) == "openai"


# ---------------------------------------------------------------------------
# ``pick`` itself: routing, contract, degradation
# ---------------------------------------------------------------------------


def _explode(*_a, **_kw):
    raise AssertionError("InquirerPy must not be used by pick any more")


class TestPickRouting:
    @pytest.fixture
    def interactive(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_is_interactive", lambda: True)
        if ui_kit.is_inquirer_available():
            monkeypatch.setattr(ui_kit.inquirer, "select", _explode)
        yield

    def test_pick_runs_prompt_toolkit_and_returns_the_value(self, monkeypatch, interactive):
        monkeypatch.setattr(
            ui_kit._MousePicker,
            "build_application",
            lambda self, **kw: _FakeApp(result=1),
        )
        assert ui_kit.pick("Provider", PROVIDERS) == "anthropic"

    def test_pick_echoes_the_chosen_label(self, monkeypatch, interactive, capsys):
        monkeypatch.setattr(
            ui_kit._MousePicker,
            "build_application",
            lambda self, **kw: _FakeApp(result=2),
        )
        ui_kit.pick("Provider", PROVIDERS)
        out = capsys.readouterr().out
        assert "Ollama (local)" in out

    def test_pick_falls_back_when_not_a_tty(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_is_interactive", lambda: False)
        monkeypatch.setattr(
            ui_kit._MousePicker, "build_application", _explode
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("2\n"))
        assert ui_kit.pick("Provider", PROVIDERS) == "anthropic"

    def test_pick_falls_back_when_prompt_toolkit_is_missing(self, monkeypatch, interactive):
        monkeypatch.setattr(ui_kit, "_PROMPT_TOOLKIT_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("3\n"))
        assert ui_kit.pick("Provider", PROVIDERS) == "ollama"

    def test_pick_falls_back_when_the_application_cannot_be_built(
        self, monkeypatch, interactive
    ):
        def _boom(self, **kw):
            raise RuntimeError("weird terminal")

        monkeypatch.setattr(ui_kit._MousePicker, "build_application", _boom)
        monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
        assert ui_kit.pick("Provider", PROVIDERS) == "openai"

    def test_pick_falls_back_on_an_empty_choice_list(self, monkeypatch, interactive):
        monkeypatch.setattr(ui_kit._MousePicker, "build_application", _explode)
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
        with pytest.raises(EOFError):
            ui_kit.pick("Provider", [])

    def test_ctrl_c_propagates_instead_of_falling_back(self, monkeypatch, interactive):
        """``helpers.ask_choice`` catches this and raises ``QuitNavigation``."""

        class _Interrupting(_FakeApp):
            def run(self):
                raise KeyboardInterrupt()

        monkeypatch.setattr(
            ui_kit._MousePicker,
            "build_application",
            lambda self, **kw: _Interrupting(),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
        with pytest.raises(KeyboardInterrupt):
            ui_kit.pick("Provider", PROVIDERS)

    def test_pick_still_works_inside_a_running_event_loop(self, monkeypatch, interactive):
        """The wizard calls every prompt from inside ``asyncio.run``."""
        import asyncio
        import warnings

        monkeypatch.setattr(
            ui_kit._MousePicker,
            "build_application",
            lambda self, **kw: _FakeApp(result=0),
        )

        async def driver():
            return ui_kit.pick("Provider", PROVIDERS)

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            assert asyncio.run(driver()) == "openai"

    def test_choice_objects_still_return_their_value(self, monkeypatch, interactive):
        if not ui_kit.is_inquirer_available():
            pytest.skip("InquirerPy not installed")
        monkeypatch.setattr(
            ui_kit._MousePicker,
            "build_application",
            lambda self, **kw: _FakeApp(result=1),
        )
        choices = [
            ui_kit.Choice(value="openai", name="OpenAI"),
            ui_kit.Choice(value="ollama", name="Ollama"),
        ]
        assert ui_kit.pick("Provider", choices) == "ollama"
