"""The welcome must describe the input the session will actually honour.

`feral setup` has two input paths and they take different gestures.
`helpers.ask_choice` uses an InquirerPy arrow-key picker when one is
importable and the shell is a TTY, and a typed prompt otherwise. Only
the typed prompt parses ``back`` / ``menu`` / ``quit`` as words
(`helpers.py:198-202`, in the branch whose own comment reads "the typed
fallback (and the path the existing tests drive)"). The picker offers
those as selectable rows instead.

The welcome told every operator to type them, unconditionally. On the
arrow-key path -- which is what anyone with a normal terminal gets --
there is no text field to type into, so the instruction described an
interface the reader did not have. Reported from a screenshot: "telling
me to type back and this and that but no field to type".

This is the same class of error as the "press space" hint the welcome
block already exists to correct, and it recurred because the tests drive
the fallback rather than the path users get.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.setup.steps.welcome import _navigation_hint  # noqa: E402


def _plain(markup: str) -> str:
    """Rich markup out, so assertions read like the operator's screen."""
    return re.sub(r"\[/?[a-z ]*\]", "", markup)


@pytest.fixture
def arrow_keys():
    with patch("cli.ui_kit.is_inquirer_available", return_value=True), \
         patch("cli.ui_kit.is_interactive", return_value=True):
        yield


@pytest.fixture
def typed_fallback():
    with patch("cli.ui_kit.is_inquirer_available", return_value=False), \
         patch("cli.ui_kit.is_interactive", return_value=True):
        yield


def test_the_arrow_key_session_is_not_told_to_type(arrow_keys):
    """The reported bug. There is no text field on this path."""
    hint = _plain(_navigation_hint()).lower()
    assert "type " not in hint, (
        f"the arrow-key picker has no text input, but the hint says: {hint!r}"
    )


def test_the_arrow_key_session_is_told_where_the_exits_are(arrow_keys):
    """Removing the wrong instruction is not enough on its own.

    An operator sixteen steps in needs to know the escape hatches exist;
    they are simply rows rather than words here.
    """
    hint = _plain(_navigation_hint()).lower()
    assert "select" in hint
    for exit_name in ("back", "quit"):
        assert exit_name in hint, f"{exit_name} is not named"


def test_the_typed_session_still_gets_the_typed_instruction(typed_fallback):
    """The words really do work on this path, so keep telling people."""
    hint = _plain(_navigation_hint()).lower()
    assert "type" in hint
    for word in ("back", "menu", "quit"):
        assert word in hint


def test_the_two_paths_do_not_give_the_same_hint(arrow_keys):
    """A refactor that collapses them reintroduces the bug."""
    with patch("cli.ui_kit.is_inquirer_available", return_value=True), \
         patch("cli.ui_kit.is_interactive", return_value=True):
        picker = _navigation_hint()
    with patch("cli.ui_kit.is_inquirer_available", return_value=False):
        typed = _navigation_hint()
    assert picker != typed


def test_a_broken_probe_falls_back_to_the_typed_wording():
    """If we cannot tell which path this is, describe the one that is
    always available. ``_prompt_raw`` exists on every path; the picker
    does not."""
    with patch("cli.ui_kit.is_inquirer_available", side_effect=RuntimeError("boom")):
        hint = _plain(_navigation_hint()).lower()
    assert "type" in hint


def test_the_typed_words_the_hint_promises_are_really_parsed():
    """Pins the hint to the parser rather than to a comment.

    If ``ask_choice`` ever stops accepting one of these, the typed hint
    becomes wrong in the same way the arrow-key one was.
    """
    src = (ROOT / "cli" / "setup" / "helpers.py").read_text()
    for word in ("back", "quit", "menu"):
        assert f'"{word}"' in src, f"ask_choice no longer parses {word!r}"
