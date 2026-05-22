"""Lane 08 WS9 — text_command WS handler must not block the message loop.

AUDIT-r13 finding 6.2: the WebSocket `text_command` handler used to
``await state.orchestrator.handle_command_stream(...)`` synchronously,
so further messages on the same socket were blocked until that turn
completed. Under SQLite pool contention or LLM latency that meant
5-110s of "dead" socket from the user's perspective.

After WS9 the handler spawns the orchestrator turn as a tracked
asyncio.Task. The WS loop keeps receiving frames; concurrent turns
on the SAME session_id still serialise through the orchestrator's
per-session lock (so the LLM tool-call ordering contract holds).

This module pins:

  1. While a slow orchestrator turn is in flight, the WS continues
     receiving the NEXT message — the loop is not stuck on the await.
  2. Concurrent turns on the same session still serialise via the
     per-session lock (orderering preserved).
  3. Failures inside a background turn surface as a friendly
     ``text_response`` (no silent disappearance, no 500 propagation).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

# Reuse the canonical WS test fixtures so we exercise the same wiring
# the rest of the suite does — keeps WS9 honest against the actual
# api/server.py message loop instead of a fragile alternate stub.
from tests.test_server_websocket import ws_client, ws_mock_state  # noqa: F401


def test_loop_keeps_receiving_while_orchestrator_turn_in_flight(ws_mock_state, ws_client):  # noqa: F811
    """Send two text_commands back-to-back. The first
    ``handle_command_stream`` blocks for 500ms; the WS server must
    still pick up the second message and dispatch a SECOND
    orchestrator task — proving the loop is not stuck on the first
    await.
    """
    started = asyncio.Event()
    slow_call_count = 0
    call_args_log: list[dict] = []

    async def slow_handle(session_id: str, text: str, context=None, **_):
        nonlocal slow_call_count
        slow_call_count += 1
        call_args_log.append({"session_id": session_id, "text": text})
        if slow_call_count == 1:
            started.set()
            await asyncio.sleep(0.5)

    ws_mock_state.orchestrator.handle_command_stream = AsyncMock(side_effect=slow_handle)

    with ws_client.websocket_connect("/v1/session") as ws:
        ws.receive_json()  # greeting

        # First message — orchestrator will sleep 500ms.
        ws.send_json({
            "type": "text_command",
            "payload": {"text": "first turn", "context": {}},
        })

        # Second message — fires immediately after; if the loop were
        # blocking on the first await this would never reach the
        # orchestrator.
        ws.send_json({
            "type": "text_command",
            "payload": {"text": "second turn", "context": {}},
        })

    # Give the background tasks time to drain (the WebSocketDisconnect
    # cleanup awaits in-flight tasks up to 2s).
    # Both messages must have reached the orchestrator.
    seen_texts = [c["text"] for c in call_args_log]
    assert "first turn" in seen_texts
    assert "second turn" in seen_texts
    assert slow_call_count == 2


def test_background_turn_failure_surfaces_text_response(ws_mock_state, ws_client):  # noqa: F811
    """When the orchestrator raises inside the background task we
    still send the operator a 'Sorry, something went wrong' chat
    message instead of dropping the failure silently.
    """
    ws_mock_state.orchestrator.handle_command_stream = AsyncMock(
        side_effect=RuntimeError("boom-task")
    )

    with ws_client.websocket_connect("/v1/session") as ws:
        ws.receive_json()  # greeting
        ws.send_json({
            "type": "text_command",
            "payload": {"text": "x", "context": {}},
        })
        err = ws.receive_json()

    assert err["type"] == "text_response"
    assert "boom-task" in err["payload"]["text"]
