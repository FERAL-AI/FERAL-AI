"""Regression tests for -A9 execution approval wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator
from security.exec_approvals import ApprovalManager


@pytest.fixture
def async_send() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def orchestrator(async_send: AsyncMock) -> Orchestrator:
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    orch = Orchestrator(
        skill_registry=reg,
        send_to_client=async_send,
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
        approval_manager=ApprovalManager(db_path=":memory:"),
    )
    orch._send_text = AsyncMock()  # isolate helper behavior
    orch._try_genui_for_result = AsyncMock()
    return orch


@pytest.mark.asyncio
async def test_text_approved_executes_latest_pending_tool(orchestrator: Orchestrator) -> None:
    session_id = "sess-a9"
    pending = orchestrator.tool_runner.enforce_safety(
        "browser__navigate",
        {"url": "https://www.google.com"},
        session_id=session_id,
    )
    assert pending is not None
    assert pending["status"] == "pending_approval"

    orchestrator._execute_tool_call_for_llm = AsyncMock(
        return_value={"success": True, "data": {"note": "opened"}},
    )

    handled = await orchestrator._maybe_handle_pending_tool_approval_text(
        session_id,
        "approved",
    )
    assert handled is True
    orchestrator._execute_tool_call_for_llm.assert_awaited_once()
    assert orchestrator.tool_runner.latest_pending_for_session(session_id) is None


@pytest.mark.asyncio
async def test_text_reject_cancels_latest_pending_tool(orchestrator: Orchestrator) -> None:
    session_id = "sess-a9-deny"
    pending = orchestrator.tool_runner.enforce_safety(
        "browser__navigate",
        {"url": "https://www.google.com"},
        session_id=session_id,
    )
    assert pending is not None
    orchestrator._execute_tool_call_for_llm = AsyncMock(
        return_value={"success": True, "data": {}},
    )

    handled = await orchestrator._maybe_handle_pending_tool_approval_text(
        session_id,
        "no",
    )
    assert handled is True
    orchestrator._execute_tool_call_for_llm.assert_not_awaited()
    assert orchestrator.tool_runner.latest_pending_for_session(session_id) is None


@pytest.mark.asyncio
async def test_non_ack_text_does_not_consume_pending(orchestrator: Orchestrator) -> None:
    session_id = "sess-a9-noop"
    pending = orchestrator.tool_runner.enforce_safety(
        "browser__navigate",
        {"url": "https://www.google.com"},
        session_id=session_id,
    )
    assert pending is not None

    handled = await orchestrator._maybe_handle_pending_tool_approval_text(
        session_id,
        "how are you",
    )
    assert handled is False
    still_pending = orchestrator.tool_runner.latest_pending_for_session(session_id)
    assert still_pending is not None
    assert still_pending["request_id"] == pending["request_id"]
