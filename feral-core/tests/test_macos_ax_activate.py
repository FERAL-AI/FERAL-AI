"""`activate` is the capable route: one call, no cursor, any element.

`click` means the click gesture and performs `AXPress`. That is precise
and it should stay precise, but it only covers the elements that publish
`AXPress`: measured on this machine, 44 of Finder's 261 interactive
elements. The other 217 are `AXCell` rows publishing `AXOpen`.

Refusing those with a pointer at `perform_action` made `click` honest,
but honest is not capable. It hands the model a round-trip it has to
know to take, and two calls where one would do.

`activate` closes that. It means "do this element's primary thing,
whatever that is", so `AXOpen` is a correct answer rather than a
substitution: the caller asked for the outcome, not the gesture. It
picks in a fixed order, reports which action it used, and never touches
the cursor. There is no coordinate fallback at all, because an element
with no activating action has no primary thing to do and saying so is
more useful than moving the mouse and hoping.

The pair is the point:

  click     precise gesture, AXPress only, cursor fallback available
  activate  primary outcome, any activating action, never the cursor
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


def _ref_with(monkeypatch, actions, role="AXCell", enabled=True) -> str:
    element = _StubElement(role)
    node = AXNode(
        ref="", role=role, subrole="", label="Applications",
        label_source="AXTitle", depth=1, actions=list(actions), secure=False,
    )
    ref = macos_ax._REFS.add(element, node, "Finder", 4242)
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(macos_ax, "_role", lambda el: role)
    monkeypatch.setattr(macos_ax, "_subrole", lambda el: "")
    monkeypatch.setattr(macos_ax, "_enabled", lambda el: enabled)
    monkeypatch.setattr(macos_ax, "_actions", lambda el: list(actions))
    return ref


@pytest.fixture()
def skill():
    return macos_ax.MacOSAccessibilitySkill()


@pytest.fixture()
def performed(monkeypatch):
    """Record the action performed instead of touching a real app."""
    calls: list[str] = []

    def _perform(self, element, action):
        calls.append(action)
        return 0

    monkeypatch.setattr(macos_ax.MacOSAccessibilitySkill, "_perform", _perform)
    return calls


@pytest.fixture()
def no_real_clicks(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        macos_ax.MacOSAccessibilitySkill, "_coordinate_click",
        lambda self, entry, ref, actions: calls.append(ref),
    )
    return calls


def test_it_opens_a_finder_row_in_one_call(skill, monkeypatch, performed, no_real_clicks):
    """The 217 elements `click` could only refuse."""
    ref = _ref_with(monkeypatch, ["AXOpen", "AXShowMenu", "AXCancel"])
    result = skill._activate({"ref": ref})

    assert result["success"] is True, result
    assert performed == ["AXOpen"]
    assert result["data"]["method"] == "AXOpen"
    assert no_real_clicks == [], "activate must never move the cursor"


def test_axpress_wins_when_both_are_published(skill, monkeypatch, performed):
    """Order matters: AXPress is the most specific activation."""
    ref = _ref_with(monkeypatch, ["AXOpen", "AXPress"], role="AXButton")
    result = skill._activate({"ref": ref})
    assert performed == ["AXPress"]
    assert result["data"]["method"] == "AXPress"


@pytest.mark.parametrize("actions,expected", [
    (["AXPress"], "AXPress"),
    (["AXOpen"], "AXOpen"),
    (["AXConfirm"], "AXConfirm"),
    (["AXPick"], "AXPick"),
    (["AXShowMenu", "AXPick"], "AXPick"),
    (["AXCancel", "AXConfirm"], "AXConfirm"),
])
def test_it_picks_the_activating_action(skill, monkeypatch, performed, actions, expected):
    ref = _ref_with(monkeypatch, actions)
    result = skill._activate({"ref": ref})
    assert result["success"] is True, result
    assert performed == [expected]


def test_an_element_with_nothing_to_activate_is_refused_not_clicked(
    skill, monkeypatch, performed, no_real_clicks,
):
    """No primary thing to do. Say so rather than moving the mouse.

    AXShowMenu and AXCancel are published almost universally and neither
    activates anything.
    """
    ref = _ref_with(monkeypatch, ["AXShowMenu", "AXCancel"])
    result = skill._activate({"ref": ref})

    assert result["success"] is False
    assert result["status_code"] == 422
    assert performed == []
    assert no_real_clicks == [], "activate has no coordinate fallback by design"
    assert result["data"]["actions"] == ["AXShowMenu", "AXCancel"]


def test_a_disabled_element_is_refused_before_anything_is_performed(
    skill, monkeypatch, performed,
):
    ref = _ref_with(monkeypatch, ["AXPress"], enabled=False)
    result = skill._activate({"ref": ref})
    assert result["success"] is False
    assert result["status_code"] == 409
    assert performed == []


def test_a_secure_field_is_never_activated(skill, monkeypatch, performed):
    """The password-field refusal must hold on the new endpoint too."""
    element = _StubElement("AXSecureTextField")
    node = AXNode(
        ref="", role="AXSecureTextField", subrole="", label="Password",
        label_source="AXTitle", depth=1, actions=["AXPress"], secure=True,
    )
    ref = macos_ax._REFS.add(element, node, "Finder", 4242)
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(macos_ax, "_role", lambda el: "AXSecureTextField")
    monkeypatch.setattr(macos_ax, "_subrole", lambda el: "")

    result = skill._activate({"ref": ref})
    assert result["success"] is False
    assert performed == []


def test_activate_is_registered_and_declared(skill):
    """Reachable as a tool, not just as a method."""
    import asyncio
    import json

    manifest = json.loads(
        (ROOT / "skills" / "manifests" / "macos_ax.json").read_text()
    )
    ids = {e.get("id") for e in manifest.get("endpoints", [])}
    assert "activate" in ids, "activate is not declared in the manifest"

    # An unknown endpoint returns 404; a registered one must not.
    result = asyncio.run(skill.execute("activate", {}, {}))
    assert result.get("status_code") != 404, (
        "activate is not wired into the dispatch table"
    )
