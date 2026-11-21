"""Protocol-stop honesty: no agent/model/file/credential operations."""

import asyncio
from types import SimpleNamespace as NS

import pytest
from bridges.acp import AcpEvent, PromptResult
from skills.impl.external_agent import ExternalAgentSkill


def managed(events, turn):
    return NS(
        handle="ext-fixture",
        agent_id="opencode",
        cwd="/fixture",
        turn=turn,
        process=NS(stderr_tail=""),
        turn_events=lambda: events,
        pending_permissions=lambda: [],
    )


@pytest.mark.asyncio
async def test_actual_empty_usage_only_end_turn_is_failed_not_completed():
    # Exact two event kinds observed from the isolated actual9.21 app.
    events = [
        AcpEvent("available_commands_update", "fixture"),
        AcpEvent("usage_update", "fixture"),
    ]
    future = asyncio.get_running_loop().create_future()
    future.set_result(PromptResult(stop_reason="end_turn", events=events, text=""))
    payload = ExternalAgentSkill()._turn_payload(managed(events, future), "completed")
    assert payload["status"] == "failed" and payload["error_code"] == "empty_agent_turn"
    assert payload["stop_reason"] == "end_turn" and payload["text"] == ""
    assert payload["events_total"] == 2 and not payload["tool_calls"]
    assert payload["tool_outcome_verified"] is False
    assert "No execution evidence" in payload["error"]


@pytest.mark.asyncio
async def test_whitespace_thought_or_plan_is_not_an_answer_or_execution_evidence():
    events = [
        AcpEvent("agent_message_chunk", "fixture", text=" \n"),
        AcpEvent("agent_thought_chunk", "fixture", text="thinking"),
        AcpEvent("plan", "fixture", text="I will write a file"),
    ]
    future = asyncio.get_running_loop().create_future()
    future.set_result(PromptResult(stop_reason="max_tokens", events=events, text=" \n"))
    payload = ExternalAgentSkill()._turn_payload(managed(events, future), "completed")
    assert payload["status"] == "failed" and payload["error_code"] == "empty_agent_turn"
    assert payload["stop_reason"] == "max_tokens"


@pytest.mark.asyncio
async def test_cancelled_future_returns_truthful_terminal_receipt():
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    payload = ExternalAgentSkill()._turn_payload(managed([], future), "completed")
    assert (
        payload["status"] == "failed"
        and payload["error_code"] == "agent_turn_cancelled"
    )
    assert (
        payload["stop_reason"] == "cancelled"
        and payload["tool_outcome_verified"] is False
    )
    assert "unknown outcome" in payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        AcpEvent("agent_message_chunk", "fixture", text="42"),
        AcpEvent("tool_call", "fixture", tool_call_id="call-1", status="completed"),
    ],
)
async def test_substantive_reply_or_tool_report_preserved_without_fake_execution_claim(
    event,
):
    future = asyncio.get_running_loop().create_future()
    future.set_result(
        PromptResult(stop_reason="end_turn", events=[event], text=event.text)
    )
    payload = ExternalAgentSkill()._turn_payload(managed([event], future), "completed")
    assert payload["status"] == "completed" and "error_code" not in payload
    assert payload["events"][0]["kind"] == event.kind
    assert payload["text"] == event.text
