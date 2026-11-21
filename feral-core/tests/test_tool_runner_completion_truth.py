"""Bounded child execution cannot convert missing output into completion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.tool_runner import ToolRunner


def _runner(rounds):
    llm = SimpleNamespace(
        available=True,
        chat=AsyncMock(side_effect=rounds),
        extract_response=lambda response: (
            response.get("text", ""), response.get("calls", []),
        ),
    )
    orch = SimpleNamespace(
        llm=llm, skills=None, _mcp_client=None, _max_iterations=8,
        _session_surfaces={"parent": "desktop"},
        _route_prompt=AsyncMock(return_value=[]),
        perception=SimpleNamespace(get_frame=lambda sid: None),
        _build_system_prompt=AsyncMock(return_value="fixture task"),
        _note_agent_round=Mock(), _maybe_prune_tool_images=Mock(),
        _materialize_tool_images=lambda sid, messages: messages,
        _serialize_tool_result_for_history=lambda *args: "recorded fixture result",
    )
    runner = ToolRunner(orch)
    runner.assemble_llm_tool_list = Mock(return_value=[])
    runner.execute_tool_call_for_llm = AsyncMock(return_value={
        "success": True, "data": {"receipt": "fixture-effect"},
    })
    return runner


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_output_is_incomplete_without_automatic_retry(text):
    runner = _runner([{"text": text}])
    result = await runner._run_subagent_task(
        parent_session_id="parent", task_text="read fixture", max_iterations=3, ordinal=1,
    )
    assert result["success"] is False
    assert result["status"] == "incomplete"
    assert result["completion_reason"] == "empty_output"
    assert result["result"] == ""
    assert result["tool_calls_executed"] == 0
    assert runner._orch.llm.chat.await_count == 1


async def test_tool_only_exhaustion_retains_effect_count_and_does_not_replay():
    runner = _runner([{
        "text": "I will do the task", "calls": [{"name": "fixture__effect", "args": {}, "id": "one"}],
    }])
    result = await runner._run_subagent_task(
        parent_session_id="parent", task_text="fixture action", max_iterations=1, ordinal=1,
    )
    assert result["success"] is False
    assert result["status"] == "incomplete"
    assert result["completion_reason"] == "iteration_limit"
    assert result["tool_calls_executed"] == 1
    assert result["iterations"] == 1
    runner.execute_tool_call_for_llm.assert_awaited_once()
    assert runner.execute_tool_call_for_llm.await_args.kwargs["surface"] == "desktop"


async def test_final_answer_after_tool_preserves_existing_completion():
    runner = _runner([
        {"calls": [{"name": "fixture__read", "args": {}, "id": "one"}]},
        {"text": "Fixture result received."},
    ])
    result = await runner._run_subagent_task(
        parent_session_id="parent", task_text="read fixture", max_iterations=2, ordinal=1,
    )
    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["completion_reason"] == "final_answer"
    assert result["result"] == "Fixture result received."
    assert result["tool_calls_executed"] == 1


async def test_provider_error_remains_failed():
    runner = _runner([{"error": "fixture provider unavailable"}])
    result = await runner._run_subagent_task(
        parent_session_id="parent", task_text="read fixture", max_iterations=2, ordinal=1,
    )
    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["completion_reason"] == "provider_error"
    assert result["tool_calls_executed"] == 0


async def test_spawn_aggregate_does_not_count_incomplete_children_as_success():
    runner = _runner([{}])
    result = await runner.spawn_subagents("parent", {"tasks": ["fixture task"]})
    assert result["data"]["success_count"] == 0
    assert result["data"]["results"][0]["status"] == "incomplete"
