"""
Tests for the provider-neutral ComputerUseDriver and the rerouted
agentic_computer_use._execute_action path.

We cover:
* Anthropic-shaped, OpenAI-shaped, and FERAL-shaped action normalization
  (left_click vs click vs {action:"click"}).
* Coordinate / key / scroll / drag / wait / shell normalization.
* `agentic_computer_use._execute_action` routes mouse/keyboard/screenshot
  through `gui_computer_use.execute` exactly once (no double DPI scaling
  inside the agentic loop).
* The shell allowlist still rejects free-form commands and points at
  `coding_tools__bash`, preserving PR2's truthful refusal.
* DPI scaling is applied by the primitive (gui_computer_use), not by
  the agentic loop.
* `desktop_automation` is now a shim that delegates to gui_computer_use
  with consistent DPI-scaled coordinates.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from agents.computer_use_driver import (  # noqa: E402
    GUI_ENDPOINT_FOR,
    NormalizedAction,
    gui_args_for,
    normalize_action,
)


# ── Action normalization ──────────────────────────────────────────────


class TestAnthropicShape:
    def test_left_click_with_coordinate_array(self):
        n = normalize_action({"type": "left_click", "coordinate": [100, 200]})
        assert n is not None
        assert n.action == "click"
        assert (n.x, n.y) == (100, 200)
        assert n.provider == "anthropic"

    def test_right_click_with_coordinate_array(self):
        n = normalize_action({"type": "right_click", "coordinate": [50, 60]})
        assert n is not None
        assert n.action == "right_click"
        assert (n.x, n.y) == (50, 60)

    def test_double_click_with_coordinate(self):
        n = normalize_action({"type": "double_click", "coordinate": [10, 20]})
        assert n is not None
        assert n.action == "double_click"

    def test_key_with_text(self):
        n = normalize_action({"type": "key", "text": "ctrl+c"})
        assert n is not None
        assert n.action == "key"
        assert n.keys == "ctrl+c"

    def test_hold_key_carries_duration(self):
        n = normalize_action({"type": "hold_key", "text": "shift", "duration": 1.5})
        assert n is not None
        assert n.action == "key"
        assert n.keys == "shift"
        assert n.duration_ms == 1500

    def test_scroll_with_scroll_direction_and_amount(self):
        n = normalize_action({
            "type": "scroll",
            "coordinate": [400, 500],
            "scroll_direction": "down",
            "scroll_amount": 5,
        })
        assert n is not None
        assert n.action == "scroll"
        assert n.direction == "down"
        assert n.amount == 5
        assert (n.x, n.y) == (400, 500)
        assert n.provider == "anthropic"

    def test_left_click_drag(self):
        n = normalize_action({
            "type": "left_click_drag",
            "start_coordinate": [10, 20],
            "coordinate": [110, 120],
        })
        assert n is not None
        assert n.action == "drag"
        assert n.path == [(10, 20), (110, 120)]

    def test_screenshot(self):
        assert normalize_action({"type": "screenshot"}).action == "screenshot"

    def test_wait_seconds_to_ms(self):
        n = normalize_action({"type": "wait", "duration": 2})
        assert n is not None
        assert n.duration_ms == 2000

    def test_type_text(self):
        n = normalize_action({"type": "type", "text": "hello"})
        assert n is not None
        assert n.action == "type"
        assert n.text == "hello"


class TestOpenAIShape:
    def test_click_with_xy(self):
        n = normalize_action({"type": "click", "x": 100, "y": 200})
        assert n is not None
        assert n.action == "click"
        assert (n.x, n.y) == (100, 200)
        assert n.provider == "openai"

    def test_keypress_array_to_combo(self):
        n = normalize_action({"type": "keypress", "keys": ["CTRL", "C"]})
        assert n is not None
        assert n.action == "key"
        assert n.keys == "ctrl+c"

    def test_scroll_with_xy_and_direction(self):
        n = normalize_action({
            "type": "scroll", "x": 100, "y": 200,
            "direction": "up", "amount": 4,
        })
        assert n is not None
        assert n.direction == "up"
        assert n.amount == 4
        assert (n.x, n.y) == (100, 200)

    def test_drag_with_path_dicts(self):
        n = normalize_action({
            "type": "drag",
            "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}, {"x": 5, "y": 6}],
        })
        assert n is not None
        assert n.action == "drag"
        assert n.path == [(1, 2), (3, 4), (5, 6)]

    def test_wait_ms(self):
        n = normalize_action({"type": "wait", "ms": 500})
        assert n.duration_ms == 500

    def test_move(self):
        n = normalize_action({"type": "move", "x": 7, "y": 8})
        assert n is not None
        assert n.action == "move"
        assert (n.x, n.y) == (7, 8)


class TestFeralShape:
    def test_click(self):
        n = normalize_action({"action": "click", "x": 50, "y": 60})
        assert n.action == "click"
        assert (n.x, n.y) == (50, 60)
        assert n.provider == "feral"

    def test_shell(self):
        n = normalize_action({"action": "shell", "command": "echo hi"})
        assert n.command == "echo hi"

    def test_done(self):
        n = normalize_action({"action": "done", "summary": "ok"})
        assert n.summary == "ok"

    def test_failed(self):
        n = normalize_action({"action": "failed", "reason": "no"})
        assert n.reason == "no"

    def test_unknown_action_returns_none(self):
        assert normalize_action({"action": "explode"}) is None

    def test_garbage_input_returns_none(self):
        assert normalize_action(None) is None
        assert normalize_action("hello") is None
        assert normalize_action({}) is None
        assert normalize_action({"foo": "bar"}) is None


class TestGuiRouting:
    def test_canonical_actions_have_gui_endpoints(self):
        for a in ("click", "double_click", "right_click", "move", "type", "key", "scroll", "screenshot"):
            assert a in GUI_ENDPOINT_FOR, f"{a} should route to a gui_computer_use endpoint"

    def test_gui_args_for_click(self):
        n = NormalizedAction(action="click", x=100, y=200)
        assert gui_args_for(n) == {"x": 100, "y": 200}

    def test_gui_args_for_scroll_with_xy(self):
        n = NormalizedAction(action="scroll", x=10, y=20, direction="down", amount=3)
        assert gui_args_for(n) == {"direction": "down", "amount": 3, "x": 10, "y": 20}

    def test_gui_args_for_scroll_without_xy(self):
        n = NormalizedAction(action="scroll", direction="up", amount=5)
        assert gui_args_for(n) == {"direction": "up", "amount": 5}

    def test_gui_args_for_key(self):
        n = NormalizedAction(action="key", keys="cmd+c")
        assert gui_args_for(n) == {"keys": "cmd+c"}


# ── agentic_computer_use._execute_action delegates to gui_computer_use ──


@pytest.mark.asyncio
async def test_agentic_routes_click_to_gui_computer_use(monkeypatch):
    """An anthropic-shaped click on the agentic loop must reach
    gui_computer_use.mouse_click exactly once with the original
    screenshot-space coordinates (DPI scaling lives inside gui_*)."""
    from skills.impl.agentic_computer_use import AgenticComputerUseSkill
    from skills.impl import register_instance

    calls: list[tuple[str, dict]] = []

    class FakeGUI:
        async def execute(self, endpoint_id: str, args: dict, vault: dict) -> dict:
            calls.append((endpoint_id, dict(args)))
            return {
                "success": True,
                "status_code": 200,
                "data": {"message": f"{endpoint_id}@{args}"},
                "error": None,
            }

    register_instance("gui_computer_use", FakeGUI())

    skill = AgenticComputerUseSkill()
    msg = await skill._execute_action({"type": "left_click", "coordinate": [123, 456]})
    assert calls == [("mouse_click", {"x": 123, "y": 456})]
    assert "mouse_click" in msg


@pytest.mark.asyncio
async def test_agentic_routes_openai_keypress_to_gui_key_press(monkeypatch):
    from skills.impl.agentic_computer_use import AgenticComputerUseSkill
    from skills.impl import register_instance

    calls: list[tuple[str, dict]] = []

    class FakeGUI:
        async def execute(self, endpoint_id: str, args: dict, vault: dict) -> dict:
            calls.append((endpoint_id, dict(args)))
            return {"success": True, "status_code": 200, "data": {"message": "ok"}, "error": None}

    register_instance("gui_computer_use", FakeGUI())

    skill = AgenticComputerUseSkill()
    await skill._execute_action({"type": "keypress", "keys": ["CTRL", "X"]})
    assert calls == [("key_press", {"keys": "ctrl+x"})]


@pytest.mark.asyncio
async def test_agentic_unknown_action_does_not_call_gui(monkeypatch):
    from skills.impl.agentic_computer_use import AgenticComputerUseSkill
    from skills.impl import register_instance

    calls: list[tuple[str, dict]] = []

    class FakeGUI:
        async def execute(self, endpoint_id: str, args: dict, vault: dict) -> dict:
            calls.append((endpoint_id, dict(args)))
            return {"success": True, "status_code": 200, "data": {"message": "ok"}, "error": None}

    register_instance("gui_computer_use", FakeGUI())

    skill = AgenticComputerUseSkill()
    msg = await skill._execute_action({"action": "frobnicate"})
    assert "Unknown action" in msg
    assert calls == []


@pytest.mark.asyncio
async def test_agentic_shell_allowlist_unchanged_after_routing(monkeypatch):
    """PR2's allowlist + 'route through coding_tools__bash' message must
    survive the driver refactor — preserving the truthful refusal."""
    from skills.impl.agentic_computer_use import AgenticComputerUseSkill

    skill = AgenticComputerUseSkill()
    # Free-form commands stay refused.
    msg = await skill._execute_action({"action": "shell", "command": "rm -rf /"})
    assert "blocked" in msg.lower()
    assert "coding_tools__bash" in msg


@pytest.mark.asyncio
async def test_agentic_wait_does_not_call_gui(monkeypatch):
    from skills.impl.agentic_computer_use import AgenticComputerUseSkill
    from skills.impl import register_instance

    calls: list = []

    class FakeGUI:
        async def execute(self, endpoint_id, args, vault):
            calls.append(endpoint_id)
            return {"success": True, "status_code": 200, "data": {"message": "ok"}, "error": None}

    register_instance("gui_computer_use", FakeGUI())

    skill = AgenticComputerUseSkill()
    msg = await skill._execute_action({"type": "wait", "duration": 0.01})
    assert calls == []
    assert "Waited" in msg


@pytest.mark.asyncio
async def test_agentic_reports_missing_gui_skill_truthfully(monkeypatch):
    """Per the truthfulness mission: if gui_computer_use isn't registered,
    surface the real reason instead of pretending an action ran."""
    from skills.impl.agentic_computer_use import AgenticComputerUseSkill
    import skills.impl as impl_mod

    monkeypatch.setattr(impl_mod, "get_implementation", lambda _id: None)

    skill = AgenticComputerUseSkill()
    msg = await skill._execute_action({"type": "click", "x": 1, "y": 2})
    assert "gui_computer_use" in msg
    assert "not registered" in msg or "cannot execute" in msg


# ── desktop_automation shim is honest about delegation ──


@pytest.mark.asyncio
async def test_desktop_automation_now_delegates_to_gui_computer_use():
    """desktop_automation used to bypass DPI scaling. After the shim it
    must round-trip args through the driver and call gui_computer_use,
    keeping coordinates consistent with the rest of the stack."""
    from skills.impl.desktop_automation import DesktopAutomationSkill
    from skills.impl import register_instance

    calls: list[tuple[str, dict]] = []

    class FakeGUI:
        async def execute(self, endpoint_id, args, vault):
            calls.append((endpoint_id, dict(args)))
            return {"success": True, "status_code": 200, "data": {"message": "ok"}, "error": None}

    register_instance("gui_computer_use", FakeGUI())

    skill = DesktopAutomationSkill()
    result = await skill.execute("click_screen", {"x": 100, "y": 200}, {})
    assert result.get("success") is True
    assert calls == [("mouse_click", {"x": 100, "y": 200})]


@pytest.mark.asyncio
async def test_desktop_automation_key_combo_normalizes_list():
    from skills.impl.desktop_automation import DesktopAutomationSkill
    from skills.impl import register_instance

    calls: list[tuple[str, dict]] = []

    class FakeGUI:
        async def execute(self, endpoint_id, args, vault):
            calls.append((endpoint_id, dict(args)))
            return {"success": True, "status_code": 200, "data": {"message": "ok"}, "error": None}

    register_instance("gui_computer_use", FakeGUI())

    skill = DesktopAutomationSkill()
    await skill.execute("key_combo", {"keys": ["cmd", "shift", "T"]}, {})
    # Driver canonicalizes to lowercase so pyautogui.hotkey accepts the
    # modifier names directly without each call re-implementing case
    # normalization.
    assert calls == [("key_press", {"keys": "cmd+shift+t"})]


@pytest.mark.asyncio
async def test_desktop_automation_unknown_endpoint_404():
    from skills.impl.desktop_automation import DesktopAutomationSkill
    from skills.impl import register_instance

    register_instance("gui_computer_use", object())  # any value blocks 503

    skill = DesktopAutomationSkill()
    result = await skill.execute("frobnicate", {}, {})
    assert result["success"] is False
    assert result["status_code"] == 404


@pytest.mark.asyncio
async def test_desktop_automation_reports_missing_gui_truthfully(monkeypatch):
    from skills.impl.desktop_automation import DesktopAutomationSkill
    import skills.impl as impl_mod

    monkeypatch.setattr(impl_mod, "get_implementation", lambda _id: None)

    skill = DesktopAutomationSkill()
    result = await skill.execute("click_screen", {"x": 1, "y": 2}, {})
    assert result["success"] is False
    assert result["status_code"] == 503
    assert "gui_computer_use" in (result.get("error") or "")


# ── macOS permission probes ───────────────────────────────────────────


def test_macos_permissions_module_importable_on_any_platform():
    """The probe module must import cleanly on Linux / Windows so test
    suites running on those hosts don't crash. Probes return
    not_applicable rather than raising."""
    from security import macos_permissions

    ax = macos_permissions.check_accessibility()
    sr = macos_permissions.check_screen_recording()
    assert ax.permission == "accessibility"
    assert sr.permission == "screen_recording"
    # status is one of the documented states
    assert ax.status in ("granted", "denied", "unknown", "not_applicable")
    assert sr.status in ("granted", "denied", "unknown", "not_applicable")
    # Setup step is non-empty so the doctor always has something to print
    assert ax.setup_step
    assert sr.setup_step


def test_macos_probe_returns_not_applicable_off_darwin(monkeypatch):
    import security.macos_permissions as macos_mod
    import platform as plat

    monkeypatch.setattr(plat, "system", lambda: "Linux")
    monkeypatch.setattr(macos_mod, "platform", plat)

    ax = macos_mod.check_accessibility()
    assert ax.status == "not_applicable"
    sr = macos_mod.check_screen_recording()
    assert sr.status == "not_applicable"


def test_tcc_status_to_dict_preserves_error_only_when_set():
    from security.macos_permissions import TCCStatus

    no_err = TCCStatus("a", "granted", "X", "ok").to_dict()
    assert "error" not in no_err
    with_err = TCCStatus("a", "unknown", "X", "fix this", error="boom").to_dict()
    assert with_err["error"] == "boom"
