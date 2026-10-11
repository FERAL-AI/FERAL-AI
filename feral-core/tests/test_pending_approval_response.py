"""Actual turn loops must not ask a model to claim a gated action completed."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_stream_nonstream_parity import _make_default_gate_orchestrator, _capture_sends


@pytest.mark.parametrize("handler", ["handle_command", "handle_command_stream"])
@pytest.mark.parametrize("batch", ["pending", "mixed", "multiple"])
async def test_pending_batch_stops_before_fabricated_completion(handler, batch):
    orch = _make_default_gate_orchestrator()
    orch._streaming_enabled = True
    calls = [{"id": "write", "name": "notes_memory__default", "args": {}}]
    if batch != "pending":
        calls.append({"id": "other", "name": "weather_current__default", "args": {}})
    pending = {"success": False, "status": "pending_approval", "request_id": "review-fixture"}
    results = [pending]
    if batch == "mixed":
        results.append({"success": True, "data": {"temperature": 20}})
    elif batch == "multiple":
        results.append({**pending, "request_id": "second-review"})
    orch._execute_tool_call_for_llm = AsyncMock(side_effect=results)
    orch._try_genui_for_result = AsyncMock()
    orch.llm.chat_with_failover = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    orch.llm.extract_response = MagicMock(side_effect=[("", calls), ("I have written ACCEPTANCE_ONLY", [])])
    stream_rounds = []

    async def stream(messages, **kwargs):
        stream_rounds.append(messages)
        if len(stream_rounds) == 1:
            for call in calls:
                yield {"type": "tool_call_delta", "tool_call": call}
        else:
            yield {"type": "text_delta", "content": "I have written ACCEPTANCE_ONLY"}
        yield {"type": "done"}

    orch.llm.chat_stream = stream
    sends = _capture_sends(orch)
    result = await getattr(orch, handler)(session_id="pending-fixture", text="Save a note")
    expected = (
        "Those actions were sent for your approval. Review the approval cards for their current status."
        if batch == "multiple" else
        "That action was sent for your approval. Review the approval card for its current status."
    )
    assert result == expected
    assert orch._execute_tool_call_for_llm.await_count == len(calls)
    assert len(stream_rounds) == (1 if handler.endswith("stream") else 0)
    assert orch.llm.chat_with_failover.await_count == (0 if handler.endswith("stream") else 1)
    assert "ACCEPTANCE_ONLY" not in str(sends)
    assert any(frame["type"] == "text_response" and frame["payload"]["text"] == expected for frame in sends)
    assert [row["tool_call_id"] for row in orch.conversation_history["pending-fixture"] if row["role"] == "tool"] == [call["id"] for call in calls]
    assert orch.conversation_history["pending-fixture"][-1]["content"] == expected


def test_successful_batch_does_not_get_pending_response():
    from agents.orchestrator import Orchestrator
    assert Orchestrator._pending_review_response([{"result": {"success": True}}]) == ""
