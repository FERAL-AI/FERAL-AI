"""A failed `tool_result` emit must be logged, not swallowed.

`_emit_tool_result` ended in a bare `except Exception: pass` wrapping both
the ToolResultPayload construction and the send. `tool_result` is the frame
that CLOSES the UI's tool chip, so losing it silently meant the chip spun
forever with nothing in the logs or the UI to explain it. The three sibling
handlers in the same function had already been upgraded to `logger.debug`;
this one was missed.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from agents.orchestrator import Orchestrator


@pytest.fixture()
def orch():
    """A bare orchestrator — __init__ is bypassed because this test only
    exercises _emit_tool_result, which needs `send` and `skills` only."""
    o = object.__new__(Orchestrator)
    o.skills = None
    o.send = AsyncMock()
    return o


_TOOL_CALL = {"name": "robot_ext__robot_move", "id": "call-1", "args": {}}
_RESULT = {"success": True, "data": {"ok": True}}


@pytest.mark.asyncio
async def test_successful_emit_sends_tool_result(orch):
    await orch._emit_tool_result("sess-1", _TOOL_CALL, _RESULT, 12.0)

    orch.send.assert_awaited()
    sent = orch.send.await_args.args[1]
    assert sent.type == "tool_result"
    assert sent.payload["tool"] == "robot_ext__robot_move"


@pytest.mark.asyncio
async def test_emit_failure_is_logged_not_swallowed(orch, caplog):
    orch.send = AsyncMock(side_effect=RuntimeError("websocket is gone"))
    caplog.set_level(logging.DEBUG, logger="feral.orchestrator")

    # Must not propagate — the response loop keeps going.
    await orch._emit_tool_result("sess-1", _TOOL_CALL, _RESULT, 12.0)

    records = [
        r for r in caplog.records
        if "tool_result" in r.getMessage() and r.levelno >= logging.WARNING
    ]
    assert records, (
        "a lost tool_result frame produced no log record at WARNING+ — "
        f"got: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    message = records[0].getMessage()
    assert "robot_ext__robot_move" in message, message
    assert records[0].exc_info is not None, "traceback was dropped"


@pytest.mark.asyncio
async def test_emit_failure_names_the_user_visible_symptom(orch, caplog):
    """The log has to say why it matters, or nobody connects it to the
    chip that never resolved."""
    orch.send = AsyncMock(side_effect=RuntimeError("websocket is gone"))
    caplog.set_level(logging.DEBUG, logger="feral.orchestrator")

    await orch._emit_tool_result("sess-1", _TOOL_CALL, _RESULT, 12.0)

    messages = [r.getMessage() for r in caplog.records]
    assert any("chip" in m for m in messages), messages
