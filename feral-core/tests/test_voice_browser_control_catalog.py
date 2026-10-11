"""A capped voice catalogue must retain a usable text browser workflow.

Only the provider transport is inert. Manifests, registry conversion and the
Realtime configuration wire are real; no browser, account or model is contacted.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents.tool_list import (
    OPENAI_TOOL_HARD_LIMIT,
    PINNED_OPENAI_TOOL_NAMES,
    cap_tools_with_pins,
    skill_id_from_tool_name,
    tool_name_from_def,
)
from skills.registry import SkillRegistry


REQUIRED_BROWSER_CONTROLS = {
    "browser__navigate", "browser__get_page_info", "browser__get_page_text",
    "browser__snapshot", "browser__find", "browser__click",
    "browser__type_text", "browser__fill_form", "browser__press_key",
}
REQUIRED_MAC_CONTROLS = {
    "macos_ax__check_permission", "macos_ax__snapshot", "macos_ax__find",
    "macos_ax__list_windows", "macos_ax__click", "macos_ax__activate",
    "macos_ax__set_value", "macos_ax__perform_action", "screen_capture__capture",
    "gui_computer_use__mouse_click", "gui_computer_use__type_text",
    "gui_computer_use__key_press", "gui_computer_use__scroll", "gui_computer_use__window_focus",
    "background_task__start",
}
# A disconnected integration is removed before either realtime provider caps
# its catalogue. This fixture covers that smaller, still oversized catalogue.
DISCONNECTED_SKILLS = {
    "calendar_google", "cutebot", "github_api", "google_contacts",
    "google_drive", "microsoft365", "notion", "smart_home_hue", "spotify_music",
}


@pytest.fixture(scope="module")
def shipped_tools():
    registry = SkillRegistry()
    # Do not load marketplace skills or inspect a personal runtime home.
    registry.load_from_directory(Path(__file__).parents[1] / "skills" / "manifests")
    return registry.get_all_tools()


def catalogue(tools, order, connected):
    selected = [tool for tool in tools if connected or
                skill_id_from_tool_name(tool_name_from_def(tool)) not in DISCONNECTED_SKILLS]
    if order == "browser_last":
        selected.sort(key=lambda tool: skill_id_from_tool_name(tool_name_from_def(tool)) == "browser")
    elif order == "reversed":
        selected.reverse()
    elif order == "oversized":
        selected = [{"type": "function", "function": {"name": f"fixture__read_{i}"}}
                    for i in range(150)] + selected
    return selected


@pytest.mark.parametrize("order", ["registered", "browser_last", "reversed", "oversized"])
@pytest.mark.parametrize("connected", [False, True])
def test_browser_workflow_survives_cap_without_changing_authority(shipped_tools, order, connected):
    raw = catalogue(shipped_tools, order, connected)
    assert len(raw) > OPENAI_TOOL_HARD_LIMIT
    capped = cap_tools_with_pins(raw)
    kept = {tool_name_from_def(tool) for tool in capped}
    assert REQUIRED_BROWSER_CONTROLS <= kept
    assert REQUIRED_MAC_CONTROLS <= kept
    assert len(capped) == OPENAI_TOOL_HARD_LIMIT
    assert {skill_id_from_tool_name(tool_name_from_def(tool)) for tool in capped} == {
        skill_id_from_tool_name(tool_name_from_def(tool)) for tool in raw
    }
    assert set(PINNED_OPENAI_TOOL_NAMES) & {tool_name_from_def(tool) for tool in raw} <= kept
    # Selection exposes the original definitions; it cannot grant a profile,
    # rewrite arguments, bypass policy or execute an action.
    originals = {tool_name_from_def(tool): tool for tool in raw}
    assert all(tool is originals[tool_name_from_def(tool)] for tool in capped)


@pytest.mark.parametrize("session_id", ["paired-phone-fixture", "ordinary-voice-fixture"])
async def test_actual_realtime_wire_retains_browser_workflow(shipped_tools, session_id):
    from voice.realtime_proxy import RealtimeSession

    raw = catalogue(shipped_tools, "browser_last", False)
    session = RealtimeSession(session_id, "fixture-node", api_key="fixture", tools=raw)
    session._connected = True
    session._send = AsyncMock()
    await session.configure()
    update = session._send.call_args.args[0]["session"]
    wire_tools = {tool["name"]: tool for tool in update["tools"]}
    assert REQUIRED_BROWSER_CONTROLS <= wire_tools.keys()
    assert len(wire_tools) == OPENAI_TOOL_HARD_LIMIT
    assert update["tool_choice"] == "auto"
    assert "Voice Tool Limit" in update["instructions"]
    originals = {tool_name_from_def(tool): tool["function"] for tool in raw}
    for name in REQUIRED_BROWSER_CONTROLS:
        assert wire_tools[name]["parameters"] == originals[name]["parameters"]
        assert wire_tools[name]["description"] == originals[name]["description"]


def test_uncapped_catalogue_is_unchanged(shipped_tools):
    raw = [tool for tool in shipped_tools if tool_name_from_def(tool) in REQUIRED_BROWSER_CONTROLS]
    assert cap_tools_with_pins(raw) == raw
