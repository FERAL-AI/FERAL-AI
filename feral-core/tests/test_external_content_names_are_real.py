"""A name in EXTERNAL_CONTENT_SKILLS that matches no real skill is silent.

``EXTERNAL_CONTENT_SKILLS`` listed ``browser_use``, the module name. The
CDP browser registers as ``browser``, so the tools are ``browser__navigate``,
``browser__snapshot``, ``browser__get_page_text``. The predicate matched
none of them.

Every page the browser read therefore reached the model with no boundary
fencing, no injection regexes and no classifier, while the entry that was
supposed to protect it guarded a tool that does not exist. Nothing failed,
nothing logged, and a test asserted on the non-existent name so the suite
stayed green.

This guard makes the mismatch loud: every entry must correspond to a skill
that really registers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.tool_runner import EXTERNAL_CONTENT_SKILLS, is_external_content_tool

MANIFESTS = Path(__file__).resolve().parents[1] / "skills" / "manifests"

# Skills that register from code rather than from a manifest file. Each
# needs the skill_id it actually registers under and where that is set,
# so an entry cannot be added here without someone checking.
CODE_REGISTERED = {
    # skills/impl/browser_use.py, get_browser_skill_manifest()
    "browser": "skills/impl/browser_use.py",
    # kept as an alias: the module name is what a reader expects
    "browser_use": "alias of browser, module name",
}


def _manifest_skill_ids() -> set[str]:
    ids = set()
    for path in MANIFESTS.glob("*.json"):
        try:
            ids.add(json.loads(path.read_text())["skill_id"])
        except Exception:  # a malformed manifest is another test's problem
            continue
    return ids


@pytest.mark.parametrize("name", sorted(EXTERNAL_CONTENT_SKILLS))
def test_every_listed_skill_actually_exists(name):
    """The assertion that would have caught the browser hole."""
    known = _manifest_skill_ids() | set(CODE_REGISTERED)
    assert name in known, (
        f"{name!r} is in EXTERNAL_CONTENT_SKILLS but no manifest declares it "
        "and it is not a documented code-registered skill. Untrusted content "
        "from the real skill is reaching the model unscreened."
    )


def test_the_browser_is_screened():
    """Named explicitly because this is the regression that happened."""
    for tool in (
        "browser__navigate",
        "browser__snapshot",
        "browser__get_page_text",
        "browser__evaluate",
    ):
        assert is_external_content_tool(tool), (
            f"{tool} reads attacker-authored web content and is not screened"
        )


def test_the_accessibility_tree_is_screened():
    """An AX snapshot of a browser window is web content arriving through
    a desktop-shaped door.

    Verified live against Chrome on macOS: ``macos_ax__snapshot`` returns
    the page's own accessibility nodes -- ``AXWebArea``, ``AXLink``,
    ``AXStaticText`` -- whose text the page author wrote, not the
    operator. Same intake class as ``browser__get_page_text``, different
    skill_id.
    """
    for tool in (
        "macos_ax__snapshot",
        "macos_ax__find",
        "macos_ax__describe",
        "macos_ax__list_windows",
    ):
        assert is_external_content_tool(tool), (
            f"{tool} can return attacker-authored page text and is not screened"
        )


def test_a_sensor_is_not_screened():
    """Keeps the check above honest: the predicate has to discriminate.

    Screening everything would pass the test above and destroy the
    signal, so assert the other direction too.
    """
    assert not is_external_content_tool("gui_computer_use__screenshot")
    assert not is_external_content_tool("desktop_control__system_info")
