"""Actual desktop adapters, inert OS sinks, real worker cancellation/settlement."""

import asyncio
from collections import deque
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from security import desktop_input as lane
from skills.impl.gui_computer_use import GUIComputerUseSkill
from skills.impl import macos_ax


async def until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "inert worker did not reach expected boundary"
        await asyncio.sleep(0.001)


def idle():
    with lane._state:
        return lane._owner is None and not lane._workers


@pytest.fixture
def sink(monkeypatch):
    assert idle()
    monkeypatch.setattr(lane, "_cleanup_unknown", False)
    monkeypatch.setenv("FERAL_GUI_MAX_ACTIONS_PER_S", "100")
    events = []
    gui = SimpleNamespace(
        keyDown=lambda key: events.append(("key_down", key)),
        keyUp=lambda key: events.append(("key_up", key)),
        mouseDown=lambda x, y, **kw: events.append(("mouse_down", kw["button"])),
        mouseUp=lambda **kw: events.append(("mouse_up", kw["button"])),
        moveTo=lambda x, y: events.append(("move", x, y)),
        scroll=lambda amount, **kw: events.append(("scroll", amount)),
    )
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    skill = GUIComputerUseSkill()
    skill._scale = 1.0
    yield SimpleNamespace(events=events, gui=gui, skill=skill)
    deadline = time.monotonic() + 3
    while not idle() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert idle(), "fixture left a real worker unsettled"


@pytest.mark.asyncio
async def test_stop_during_key_down_releases_key_and_fences_remaining_text(sink):
    entered, release = threading.Event(), threading.Event()

    def down(key):
        sink.events.append(("key_down", key))
        entered.set()
        assert release.wait(3)

    sink.gui.keyDown = down
    task = asyncio.create_task(sink.skill.execute("type_text", {"text": "abc"}, {}))
    try:
        await until(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        refused = await sink.skill.execute("mouse_move", {"x": 4, "y": 8}, {})
        assert refused["success"] is False and refused["status_code"] == 409
        assert refused["reason"] == "desktop_input_draining"
        assert sink.events == [("key_down", "a")]
    finally:
        release.set()
    await until(idle)
    assert sink.events == [("key_down", "a"), ("key_up", "a")]
    assert (await sink.skill.execute("mouse_move", {"x": 4, "y": 8}, {}))["success"] is True


@pytest.mark.asyncio
async def test_active_input_excludes_second_adapter_without_queueing(sink):
    entered, release = threading.Event(), threading.Event()

    def move(x, y):
        sink.events.append(("move", x, y))
        entered.set()
        assert release.wait(3)

    sink.gui.moveTo = move
    first = asyncio.create_task(sink.skill.execute("mouse_move", {"x": 1, "y": 2}, {}))
    try:
        await until(entered.is_set)
        other = GUIComputerUseSkill()
        other._scale = 1.0
        refused = await other.execute("mouse_click", {"x": 3, "y": 4}, {})
        assert refused["success"] is False and refused["reason"] == "desktop_input_busy"
        assert sink.events == [("move", 1, 2)]
    finally:
        release.set()
    assert (await first)["success"] is True
    await until(idle)
    assert sink.events == [("move", 1, 2)]


@pytest.mark.asyncio
async def test_cancel_mouse_down_releases_button_and_avoids_second_click(sink):
    entered, release = threading.Event(), threading.Event()

    def down(x, y, **kwargs):
        sink.events.append(("mouse_down", kwargs["button"]))
        entered.set()
        assert release.wait(3)

    sink.gui.mouseDown = down
    task = asyncio.create_task(sink.skill.execute("mouse_double_click", {"x": 1, "y": 2}, {}))
    try:
        await until(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await until(idle)
    assert sink.events == [("mouse_down", "left"), ("mouse_up", "left")]


@pytest.mark.asyncio
async def test_exception_cleans_attempted_key_and_releases_lane(sink, caplog):
    def down(key):
        sink.events.append(("key_down", key))
        raise ValueError("private text must not leak")

    sink.gui.keyDown = down
    failed = await sink.skill.execute("type_text", {"text": "private"}, {})
    assert failed["success"] is False and failed["status_code"] == 500
    assert "private text" not in caplog.text + str(failed)
    await until(idle)
    assert sink.events == [("key_down", "p"), ("key_up", "p")]
    assert (await sink.skill.execute("mouse_move", {"x": 1, "y": 2}, {}))["success"] is True


@pytest.mark.asyncio
async def test_failed_cleanup_is_unknown_and_refuses_new_input(sink):
    def broken(**kwargs):
        raise RuntimeError("inert button release failure")

    sink.gui.mouseUp = broken
    failed = await sink.skill.execute("mouse_click", {"x": 1, "y": 2}, {})
    assert failed["success"] is False and failed["data"]["outcome"] == "unknown"
    await until(idle)
    refused = await sink.skill.execute("mouse_move", {"x": 3, "y": 4}, {})
    assert refused["success"] is False
    assert refused["reason"] == "desktop_input_cleanup_unknown"
    assert sink.events == [("mouse_down", "left")]


@pytest.mark.asyncio
async def test_hotkey_cancellation_releases_held_keys_only(sink):
    entered, release = threading.Event(), threading.Event()

    def down(key):
        sink.events.append(("key_down", key))
        entered.set()
        assert release.wait(3)

    sink.gui.keyDown = down
    task = asyncio.create_task(sink.skill.execute("key_press", {"keys": "ctrl+shift+a"}, {}))
    try:
        await until(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await until(idle)
    assert sink.events == [("key_down", "ctrl"), ("key_up", "ctrl")]


@pytest.mark.asyncio
async def test_cancel_before_worker_start_retains_lane_and_dispatches_zero_input(sink):
    release = threading.Event()
    entered = [threading.Event() for _ in range(4)]

    def block(event):
        event.set()
        assert release.wait(3)

    reads = [asyncio.create_task(lane.run_worker(block, event)) for event in entered]
    pending = None
    try:
        await until(lambda: all(event.is_set() for event in entered))
        pending = asyncio.create_task(sink.skill.execute("mouse_move", {"x": 1, "y": 2}, {}))
        await until(lambda: lane._owner is not None)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        refused = await sink.skill.execute("mouse_click", {"x": 4, "y": 5}, {})
        assert refused["reason"] == "desktop_input_draining"
        assert sink.events == []
    finally:
        release.set()
        await asyncio.gather(*reads)
    await until(idle)
    assert sink.events == []


def ax_setup(monkeypatch, events, *, actions=()):
    element = object()
    node = macos_ax.AXNode(ref="", role="AXButton", subrole="", label="inert", label_source="fixture", depth=0,
                          actions=list(actions), bounds={"x": 1, "y": 2, "width": 3, "height": 4})
    ref = macos_ax._REFS.add(element, node, "inert", 99)
    monkeypatch.setattr(macos_ax, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(macos_ax, "_role", lambda _: "AXButton")
    monkeypatch.setattr(macos_ax, "_subrole", lambda _: "")
    monkeypatch.setattr(macos_ax, "_enabled", lambda _: True)
    monkeypatch.setattr(macos_ax, "_actions", lambda _: list(actions))
    monkeypatch.setattr(macos_ax, "_bounds", lambda _: node.bounds)
    quartz = SimpleNamespace(kCGEventMouseMoved="move", kCGEventLeftMouseDown="down",
                             kCGEventLeftMouseUp="up", kCGMouseButtonLeft=0, kCGHIDEventTap=0,
                             CGEventCreateMouseEvent=lambda _, kind, point, button: kind,
                             CGEventPost=lambda _, event: events.append(("quartz", event)))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    return macos_ax.MacOSAccessibilitySkill(), ref, quartz


@pytest.mark.asyncio
async def test_ax_coordinate_cancel_releases_and_shares_gui_lane(sink, monkeypatch):
    ax, ref, quartz = ax_setup(monkeypatch, sink.events)
    entered, release = threading.Event(), threading.Event()

    def post(_, kind):
        sink.events.append(("quartz", kind))
        if kind == "down":
            entered.set()
            assert release.wait(3)

    quartz.CGEventPost = post
    task = asyncio.create_task(ax.execute("click", {"ref": ref}, {}))
    try:
        await until(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        refused = await sink.skill.execute("mouse_move", {"x": 1, "y": 2}, {})
        assert refused["reason"] == "desktop_input_draining"
    finally:
        release.set()
    await until(idle)
    assert sink.events == [("quartz", "move"), ("quartz", "down"), ("quartz", "up")]


@pytest.mark.asyncio
async def test_gui_busy_does_not_disable_ax_semantics_or_secure_field_guard(sink, monkeypatch):
    ax, ref, _ = ax_setup(monkeypatch, sink.events, actions=("AXPress",))
    monkeypatch.setattr(ax, "_perform", lambda *args: 0)
    entered, release = threading.Event(), threading.Event()

    def move(x, y):
        entered.set()
        assert release.wait(3)

    sink.gui.moveTo = move
    task = asyncio.create_task(sink.skill.execute("mouse_move", {"x": 1, "y": 2}, {}))
    try:
        await until(entered.is_set)
        result = await ax.execute("click", {"ref": ref}, {})
        assert result["success"] is True and result["data"]["method"] == "AXPress"
        macos_ax._REFS.get(ref).secure = True
        denied = await ax.execute("click", {"ref": ref}, {})
        assert denied["status_code"] == 403 and denied["success"] is False
        assert sink.events == []
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_registered_executor_preserves_actual_adapter_draining_failure(sink, monkeypatch, tmp_path):
    from agents.tool_runner import ToolRunner
    from models.skill_manifest import SkillManifest
    from security.exec_approvals import ApprovalManager
    from security.trust_ledger import TrustLedger
    from skills.executor import SkillExecutor
    from skills.registry import SkillRegistry

    monkeypatch.setenv("FERAL_AUTONOMY", "strict")
    manifest = SkillManifest(**json.loads((Path(__file__).parents[1] / "skills/manifests/gui_computer_use.json").read_text()))
    registry = SkillRegistry()
    registry.register(manifest)
    executor = SkillExecutor.__new__(SkillExecutor)
    executor._skill_call_times = {manifest.skill_id: deque()}
    executor._vault, executor._blind_vault = {}, None
    monkeypatch.setattr("skills.impl.get_implementation", lambda _: sink.skill)
    monkeypatch.setattr("skills.executor._record_action_reversal", lambda *a: None)
    monkeypatch.setattr(executor, "_record_audit", AsyncMock())
    orch = SimpleNamespace(skills=registry, executor=executor, _active_turns={},
                           _session_surfaces={}, _mcp_client=None, daemons={}, _send_text=AsyncMock())
    runner = ToolRunner(orch, autonomy_mode="strict", trust_ledger=TrustLedger(persist=False),
                        approval_manager=ApprovalManager(db_path=str(tmp_path / "inert.sqlite")))
    orch.tool_runner = runner
    monkeypatch.setitem(sys.modules, "api.state", SimpleNamespace(state=SimpleNamespace(orchestrator=orch)))
    entered, release = threading.Event(), threading.Event()

    def down(key):
        entered.set()
        assert release.wait(3)

    sink.gui.keyDown = down
    task = asyncio.create_task(sink.skill.execute("type_text", {"text": "ab"}, {}))
    try:
        await until(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        call = {"id": "inert", "name": "gui_computer_use__mouse_move", "args": {"x": 1, "y": 2}}
        pending = await runner.execute_tool_call_for_llm("owner", call, registry.get_all_tools())
        assert pending["status"] == "pending_approval"
        approved = runner.approve_pending(pending["request_id"], session_id="owner", exact_once=True)
        failed = await runner.execute_tool_call_for_llm("owner", call, registry.get_all_tools(), approval=approved["approval"])
        assert failed["success"] is False and failed["status_code"] == 409
        assert "desktop_input_draining" in failed["error"]
    finally:
        release.set()
    await until(idle)
