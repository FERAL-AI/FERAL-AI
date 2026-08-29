"""Never move the operator's cursor when a cursor-free route exists.

`macos_ax__click` performs `AXPress`. When an element publishes no
`AXPress` it falls through to `_coordinate_click`, which posts a real
mouse event and teleports the operator's cursor. That fallback is the
right last resort, and it was reached far too eagerly.

Measured on this machine during the capability audit: of Finder's 261
interactive elements only 44 published `AXPress`. The other 217 are
`AXCell` rows publishing **`AXOpen`**. So "click Applications in Finder"
silently became a real mouse click, in a module whose whole premise is
that it does not touch the cursor.

The fix is deliberately NOT "press AXOpen when AXPress is missing".
Those are different gestures: a coordinate click on a Finder row
*selects* it, `AXOpen` *opens* it. Silently swapping one for the other
would trade a cursor problem for a correctness problem, and the module
cannot know which the caller meant.

What it does instead is refuse, name the actions the element actually
publishes, and point at `macos_ax__perform_action`. That refusal already
existed and was already well worded; it was only reachable when the
caller had explicitly disabled the coordinate fallback, which is not the
default. Making it reachable by default means the model is told there is
a cursor-free route and gets to choose, rather than having the cursor
moved on its behalf.

The coordinate fallback keeps its actual job: elements with no usable
accessibility action at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.impl import macos_ax  # noqa: E402
from skills.impl.macos_ax import AXNode  # noqa: E402


class _StubElement:
    def __init__(self, name: str) -> None:
        self.name = name


def _ref_with(monkeypatch, actions: list[str], role: str = "AXCell") -> str:
    """Register a stub element publishing exactly ``actions``."""
    element = _StubElement(role)
    node = AXNode(
        ref="", role=role, subrole="", label="Applications",
        label_source="AXTitle", depth=1, actions=list(actions), secure=False,
    )
    ref = macos_ax._REFS.add(element, node, "Finder", 4242)
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(macos_ax, "_role", lambda el: role)
    monkeypatch.setattr(macos_ax, "_subrole", lambda el: "")
    monkeypatch.setattr(macos_ax, "_enabled", lambda el: True)
    monkeypatch.setattr(macos_ax, "_actions", lambda el: list(actions))
    return ref


@pytest.fixture()
def skill():
    return macos_ax.MacOSAccessibilitySkill()


@pytest.fixture()
def no_real_clicks(monkeypatch):
    """Record coordinate clicks instead of moving the real cursor."""
    calls: list = []

    def _record(self, entry, ref, actions):
        calls.append(ref)
        return {"success": True, "status_code": 200,
                "data": {"ref": ref, "method": "coordinate"}, "error": None}

    monkeypatch.setattr(
        macos_ax.MacOSAccessibilitySkill, "_coordinate_click", _record,
    )
    return calls


def test_an_element_with_only_axopen_is_not_clicked_with_the_mouse(
    skill, monkeypatch, no_real_clicks,
):
    """The Finder case: 217 of 261 elements took this path."""
    ref = _ref_with(monkeypatch, ["AXOpen", "AXShowMenu", "AXCancel"])
    result = skill._click({"ref": ref})

    assert no_real_clicks == [], (
        "the operator's cursor was moved even though the element publishes "
        "AXOpen, a cursor-free route"
    )
    assert result["success"] is False
    assert result["status_code"] == 422
    assert "AXOpen" in str(result.get("error", "")), (
        f"the refusal does not name the action that would work: {result}"
    )
    assert "perform_action" in str(result.get("error", "")), (
        "the refusal does not tell the caller how to proceed"
    )


def test_the_available_actions_are_returned_as_data_not_only_prose(
    skill, monkeypatch, no_real_clicks,
):
    """A model should not have to parse an error string to recover."""
    ref = _ref_with(monkeypatch, ["AXOpen"])
    result = skill._click({"ref": ref})
    assert result.get("data", {}).get("actions") == ["AXOpen"]


def test_axpress_is_still_preferred_and_still_cursor_free(
    skill, monkeypatch, no_real_clicks,
):
    ref = _ref_with(monkeypatch, ["AXPress"], role="AXButton")
    monkeypatch.setattr(
        macos_ax.MacOSAccessibilitySkill, "_perform", lambda self, el, a: 0,
    )
    result = skill._click({"ref": ref})
    assert result["success"] is True
    assert result["data"]["method"] == "AXPress"
    assert no_real_clicks == []


def test_an_element_with_no_usable_action_still_falls_back(
    skill, monkeypatch, no_real_clicks,
):
    """The fallback keeps its real job.

    `AXShowMenu` and `AXCancel` are published almost universally and
    neither activates anything, so an element carrying only those has no
    accessibility route and the mouse is genuinely the last resort.
    """
    ref = _ref_with(monkeypatch, ["AXShowMenu", "AXCancel"])
    result = skill._click({"ref": ref})
    assert no_real_clicks == [ref], (
        "an element with no activating action should still reach the "
        "coordinate fallback"
    )
    assert result["success"] is True


def test_an_element_with_no_actions_at_all_still_falls_back(
    skill, monkeypatch, no_real_clicks,
):
    ref = _ref_with(monkeypatch, [])
    skill._click({"ref": ref})
    assert no_real_clicks == [ref]


def test_explicitly_disabling_the_fallback_still_refuses(
    skill, monkeypatch, no_real_clicks,
):
    """Pre-existing behaviour must not regress."""
    ref = _ref_with(monkeypatch, ["AXShowMenu"])
    result = skill._click({"ref": ref, "allow_coordinate_fallback": False})
    assert no_real_clicks == []
    assert result["success"] is False
    assert result["status_code"] == 422
