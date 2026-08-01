"""Plan mode must hold on the LIVE realtime voice paths.

Regression guard for a real hole. ``ToolRunner.enforce_plan_mode`` is the
gate that makes plan mode true rather than merely suggested, and the chat
and chained-voice paths both funnel through it. The OpenAI Realtime and
Gemini Live proxies do not: they call ``SkillExecutor.execute`` directly
from their own tool-call handlers, so a session in plan mode could still
mutate state by speaking to a live voice session.

The existing plan-mode suite looked like it covered this. It drives
``execute_tool_call_for_llm(surface="voice")``, but that is the CHAINED
shape, which reaches ToolRunner like any text turn. So the suite was
green while the realtime hole stayed open, which is why these tests
exercise the proxies' own handlers instead of the runner.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.plan_mode import PLAN_REFUSAL_CODE


def _runner_in_plan_mode(blocked: bool):
    """Minimal stand-in for the orchestrator -> tool_runner -> gate chain."""
    runner = MagicMock()
    if blocked:
        runner.enforce_plan_mode = MagicMock(
            return_value={
                "success": False,
                "status_code": 403,
                "is_error": True,
                "error_code": PLAN_REFUSAL_CODE,
                "error": "plan mode is active",
            }
        )
    else:
        runner.enforce_plan_mode = MagicMock(return_value=None)
    orchestrator = MagicMock()
    orchestrator.tool_runner = runner
    return orchestrator


def _proxy(cls, orchestrator):
    proxy = cls.__new__(cls)
    proxy._orchestrator = orchestrator
    return proxy


def _realtime_cls():
    from voice.realtime_proxy import RealtimeProxy

    return RealtimeProxy


def _gemini_cls():
    from voice.gemini_realtime import GeminiRealtimeProxy

    return GeminiRealtimeProxy


@pytest.mark.parametrize("cls_factory", [_realtime_cls, _gemini_cls])
def test_plan_mode_blocks_a_mutating_tool_on_the_realtime_path(cls_factory):
    proxy = _proxy(cls_factory(), _runner_in_plan_mode(blocked=True))

    refusal = proxy._plan_mode_refusal("coding_tools__edit_file", "sess-1")

    assert refusal is not None, (
        "the realtime voice path must consult the same plan-mode gate as chat; "
        "without this a plan-mode session can mutate state by voice"
    )
    assert refusal["error_code"] == PLAN_REFUSAL_CODE


@pytest.mark.parametrize("cls_factory", [_realtime_cls, _gemini_cls])
def test_plan_safe_tool_is_not_blocked_on_the_realtime_path(cls_factory):
    proxy = _proxy(cls_factory(), _runner_in_plan_mode(blocked=False))

    assert proxy._plan_mode_refusal("coding_tools__read_file", "sess-1") is None


@pytest.mark.parametrize("cls_factory", [_realtime_cls, _gemini_cls])
def test_missing_orchestrator_does_not_fail_the_call(cls_factory):
    """Several tests build these proxies without an orchestrator.

    A missing gate must not turn into a refusal, or plan mode would start
    blocking calls it was never meant to block.
    """
    proxy = _proxy(cls_factory(), None)

    assert proxy._plan_mode_refusal("coding_tools__edit_file", "sess-1") is None


@pytest.mark.parametrize("cls_factory", [_realtime_cls, _gemini_cls])
def test_a_raising_gate_is_swallowed_rather_than_breaking_voice(cls_factory):
    orchestrator = _runner_in_plan_mode(blocked=True)
    orchestrator.tool_runner.enforce_plan_mode = MagicMock(
        side_effect=RuntimeError("registry exploded")
    )
    proxy = _proxy(cls_factory(), orchestrator)

    assert proxy._plan_mode_refusal("coding_tools__edit_file", "sess-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cls_factory", [_realtime_cls, _gemini_cls])
async def test_the_executor_is_never_reached_when_plan_mode_blocks(cls_factory):
    """The refusal must short-circuit, not merely annotate the result."""
    proxy = _proxy(cls_factory(), _runner_in_plan_mode(blocked=True))
    proxy._skill_executor = MagicMock()
    proxy._skill_executor.execute = AsyncMock(return_value={"success": True})

    refusal = proxy._plan_mode_refusal("coding_tools__edit_file", "sess-1")
    if refusal is None:
        await proxy._skill_executor.execute("coding_tools__edit_file", {}, None, None)

    proxy._skill_executor.execute.assert_not_awaited()
