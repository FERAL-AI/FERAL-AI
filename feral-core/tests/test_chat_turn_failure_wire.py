"""The shared desktop/phone turn runner must never report failure as prose."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_unexpected_turn_failure_is_error_frame(monkeypatch):
    import api.server as server

    orchestrator = SimpleNamespace(handle_command_stream=AsyncMock(side_effect=RuntimeError("private diagnostic")))
    monkeypatch.setattr(server, "state", SimpleNamespace(orchestrator=orchestrator, skill_gen=None))
    ws = SimpleNamespace(send_json=AsyncMock())
    await server._build_chat_turn_runner(ws=ws, session_id="test", refined_text="hello", ctx={})

    ws.send_json.assert_awaited_once()
    frame = ws.send_json.await_args.args[0]
    assert frame["type"] == "error"
    assert frame["session_id"] == "test"
    assert frame["payload"] == {
        "code": "chat_turn_failed",
        "message": "The chat turn failed. Please try again.",
        "recoverable": True,
    }
    assert "private diagnostic" not in str(frame)


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_emit_success_or_failure(monkeypatch):
    import api.server as server

    orchestrator = SimpleNamespace(handle_command_stream=AsyncMock(side_effect=asyncio.CancelledError()))
    monkeypatch.setattr(server, "state", SimpleNamespace(orchestrator=orchestrator, skill_gen=None))
    ws = SimpleNamespace(send_json=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await server._build_chat_turn_runner(ws=ws, session_id="test", refined_text="hello", ctx={})
    ws.send_json.assert_not_awaited()
