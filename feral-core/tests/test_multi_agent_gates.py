"""The multi-agent path must honour plan mode and the approval gate.

``AgentWorker`` calls ``SkillExecutor.execute`` directly and never reaches
``ToolRunner``, which owns both ``enforce_plan_mode`` and ``enforce_safety``.
So both gates were skipped, and ``features.multi_agent`` defaults to True,
which makes this the primary text chat path rather than an edge case.

Proven against a live brain before the fix: with plan mode active and
autonomy set to ``strict``, ``feral_reminders__create`` executed and created
the reminder, with no plan-mode refusal and no approval frame, while
``resolve_policy`` for that tool returns ``confirm``.

Two gates were already documented as bypassed on the voice realtime proxies.
This was a third, and the one a normal chat turn actually goes through.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agents.multi_agent import AgentWorker


def _worker(refusals: dict) -> tuple[AgentWorker, MagicMock]:
    """An AgentWorker whose orchestrator carries a stub ToolRunner."""
    runner = MagicMock()
    runner.enforce_plan_mode = MagicMock(
        return_value=refusals.get("plan_mode")
    )
    runner.enforce_safety = MagicMock(return_value=refusals.get("safety"))

    orchestrator = MagicMock()
    orchestrator.tool_runner = runner

    worker = AgentWorker.__new__(AgentWorker)
    worker._orchestrator = orchestrator
    worker._executor = MagicMock()
    worker._executor.execute = AsyncMock(return_value={"success": True})
    return worker, runner


def test_plan_mode_refusal_is_returned_instead_of_executing():
    refusal = {"success": False, "error_code": "plan_mode_blocked"}
    worker, runner = _worker({"plan_mode": refusal})

    got = worker._gate_tool_call("feral_reminders__create", {"title": "x"}, "s1")

    assert got is refusal, (
        "a plan-mode refusal must be returned as the tool result; returning "
        "None here is how the live brain created a reminder while in a mode "
        "whose entire contract is that it cannot mutate"
    )
    runner.enforce_plan_mode.assert_called_once_with(
        "feral_reminders__create", "s1"
    )


def test_safety_gate_is_consulted_even_when_plan_mode_allows():
    """enforce_safety is the autonomy gate, and it was skipped too."""
    refusal = {"success": False, "error_code": "needs_approval"}
    worker, runner = _worker({"safety": refusal})

    got = worker._gate_tool_call("feral_reminders__create", {"title": "x"}, "s1")

    assert got is refusal
    runner.enforce_safety.assert_called_once()


def test_an_allowed_call_returns_none_so_execution_proceeds():
    worker, runner = _worker({})

    assert worker._gate_tool_call("notes_memory__search", {}, "s1") is None
    runner.enforce_plan_mode.assert_called_once()
    runner.enforce_safety.assert_called_once()


def test_plan_mode_is_checked_before_safety():
    """Order matters: a plan-mode refusal must not depend on the tier.

    A tool can be plan-unsafe and still be auto-approved under a loose
    autonomy mode, so checking safety first would let it through.
    """
    plan_refusal = {"error_code": "plan_mode_blocked"}
    worker, runner = _worker({"plan_mode": plan_refusal})

    assert worker._gate_tool_call("cutebot__set_lights", {}, "s1") is plan_refusal
    runner.enforce_safety.assert_not_called()


def test_a_missing_tool_runner_does_not_break_the_turn():
    """Several tests build an orchestrator double with no tool_runner."""
    worker = AgentWorker.__new__(AgentWorker)
    worker._orchestrator = MagicMock(spec=[])

    assert worker._gate_tool_call("notes_memory__search", {}, "s1") is None


def test_a_raising_gate_does_not_break_the_turn():
    worker, runner = _worker({})
    runner.enforce_plan_mode = MagicMock(side_effect=RuntimeError("registry gone"))

    assert worker._gate_tool_call("notes_memory__search", {}, "s1") is None


def test_no_orchestrator_at_all_is_tolerated():
    worker = AgentWorker.__new__(AgentWorker)
    worker._orchestrator = None

    assert worker._gate_tool_call("notes_memory__search", {}, "s1") is None
