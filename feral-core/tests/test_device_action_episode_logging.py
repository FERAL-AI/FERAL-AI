"""Device/robot actions are written to the episodic timeline.

Regression context: physical-device skills (CuteBot, robot arm,
smart-home) executed the requested action but recorded NOTHING durable
— only the user's command *text* was saved as a ``user_command``
episode. So "what did my robot do today?" routed to
``notes_memory__fused_timeline`` and found no entry describing the
action itself; the brain truthfully answered "I don't have logs of
that" even though the robot had been driving/spinning/flashing all day.

The fix logs the *result* of every successful device action in the one
tool-result hook all dispatch paths funnel through
(``Orchestrator._emit_tool_result``). These tests pin that contract:

  * a successful CuteBot motion command saves a ``device_action``
    episode with a human-readable summary,
  * read-only telemetry/status polls do NOT (they'd bury the signal),
  * failed actions do NOT (we never log a robot move that didn't take),
  * non-device tools (web_search, notes_memory) are untouched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator


def _make_orchestrator(memory: Any) -> Orchestrator:
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    return Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=memory,
        vision_buffer=None,
        perception=None,
        learner=None,
    )


def _memory() -> MagicMock:
    memory = MagicMock()
    memory.episode_save = AsyncMock(return_value={})
    return memory


# ── field extraction (pure, no loop) ─────────────────────────────────


def test_fields_built_for_successful_cutebot_action() -> None:
    orch = _make_orchestrator(_memory())
    fields = orch._device_action_episode_fields(
        {"name": "cutebot__drive", "args": {"left": 80, "right": 80}},
        {"success": True, "data": {"verified": True}},
    )
    assert fields is not None
    summary, detail = fields
    assert summary.startswith("CuteBot: drive")
    assert "left=80" in summary and "right=80" in summary
    assert "verified" in summary
    assert "cutebot__drive" in detail


def test_unverified_motion_is_flagged_in_summary() -> None:
    orch = _make_orchestrator(_memory())
    _summary, _detail = orch._device_action_episode_fields(
        {"name": "cutebot__explore", "args": {}},
        {"success": True, "data": {"verified": False}},
    )
    assert "UNVERIFIED" in _summary


def test_read_only_endpoint_is_not_logged() -> None:
    orch = _make_orchestrator(_memory())
    assert (
        orch._device_action_episode_fields(
            {"name": "cutebot__status", "args": {}},
            {"success": True, "data": {"mode": "stopped"}},
        )
        is None
    )


def test_failed_action_is_not_logged() -> None:
    orch = _make_orchestrator(_memory())
    assert (
        orch._device_action_episode_fields(
            {"name": "cutebot__follow_line", "args": {}},
            {"success": False, "error": "robot did not enter line mode"},
        )
        is None
    )


def test_non_device_tool_is_not_logged() -> None:
    orch = _make_orchestrator(_memory())
    assert (
        orch._device_action_episode_fields(
            {"name": "notes_memory__fused_timeline", "args": {}},
            {"success": True, "data": {"entries": []}},
        )
        is None
    )


def test_smart_home_action_is_logged() -> None:
    orch = _make_orchestrator(_memory())
    fields = orch._device_action_episode_fields(
        {"name": "smart_home_hue__set_lights", "args": {"color": "green"}},
        {"success": True, "data": {}},
    )
    assert fields is not None
    assert fields[0].startswith("Smart home: set_lights")
    assert "color=green" in fields[0]


# ── end-to-end through the tool-result hook ──────────────────────────


@pytest.mark.asyncio
async def test_emit_tool_result_saves_device_episode() -> None:
    memory = _memory()
    orch = _make_orchestrator(memory)

    await orch._emit_tool_result(
        session_id="sess-robot-1",
        tool_call={"name": "cutebot__drive", "args": {"left": 60, "right": 60}},
        result_data={"success": True, "data": {"verified": True}},
        latency_ms=12.0,
    )
    await orch.drain_background_tasks(timeout=2.0)

    memory.episode_save.assert_awaited_once()
    kwargs = memory.episode_save.await_args.kwargs
    assert kwargs["event_type"] == "device_action"
    assert kwargs["session_id"] == "sess-robot-1"
    assert kwargs["summary"].startswith("CuteBot: drive")
    assert kwargs["importance"] == 0.6


@pytest.mark.asyncio
async def test_emit_tool_result_skips_status_poll() -> None:
    memory = _memory()
    orch = _make_orchestrator(memory)

    await orch._emit_tool_result(
        session_id="sess-robot-2",
        tool_call={"name": "cutebot__status", "args": {}},
        result_data={"success": True, "data": {"mode": "stopped"}},
        latency_ms=5.0,
    )
    await orch.drain_background_tasks(timeout=2.0)
    memory.episode_save.assert_not_called()


@pytest.mark.asyncio
async def test_emit_tool_result_device_log_survives_memory_unwired() -> None:
    """No memory wired → no crash on the tool-result hot path."""
    orch = _make_orchestrator(memory=None)
    # Must not raise even though there is nowhere to persist the episode.
    await orch._emit_tool_result(
        session_id="sess-robot-3",
        tool_call={"name": "cutebot__drive", "args": {"left": 60, "right": 60}},
        result_data={"success": True, "data": {"verified": True}},
        latency_ms=5.0,
    )
