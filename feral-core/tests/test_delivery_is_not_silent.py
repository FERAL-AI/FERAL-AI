"""A frame nobody can receive must not be dropped in silence.

`send_to_session` delivers to a live WebSocket, or appends text to a
channel collector, or returns. That third branch is the bug: no log, no
queue, no return value, so every caller believes it succeeded.

Two consequences, both reachable today:

  * A permission prompt registers its `request_id` in
    `_pending_permission_requests` BEFORE sending. If the send lands in
    the silent branch the entry leaks for the life of the process and
    the caller waits on an answer that can never arrive. On the CLI the
    receive loop has no `permission_request` branch at all, so the frame
    falls through and the operator sees "(timeout waiting for response)"
    30 seconds later with no indication that they were asked something.

  * A channel session with a live collector still drops any frame whose
    payload carries no `text`, which is exactly what an SDUI
    confirmation card is.

These tests assert that undeliverable frames are reported rather than
swallowed, and that a permission request which cannot reach anybody
fails closed instead of leaking.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.protocol import FeralMessage  # noqa: E402


def _msg(session_id: str, mtype: str = "text_response", payload: dict | None = None):
    return FeralMessage(
        session_id=session_id,
        hop="brain",
        type=mtype,
        payload=payload if payload is not None else {"text": "hello"},
    )


class _State:
    """The two attributes send_to_session actually reads."""

    def __init__(self):
        self.sessions = {}
        self._channel_collectors = {}

    send_to_session = None  # bound in the fixture below


@pytest.fixture()
def state():
    from api.state import BrainState

    st = _State()
    st.send_to_session = BrainState.send_to_session.__get__(st, _State)
    # `send_to_session` reaches for this when a channel cannot draw the
    # payload; the double needs it bound too or the test measures the
    # double's incompleteness rather than the code.
    st._describe_undrawable_payload = BrainState._describe_undrawable_payload
    return st


@pytest.mark.asyncio
async def test_undeliverable_frame_is_reported_not_swallowed(state, caplog):
    """No websocket, no collector: the caller must be able to tell."""
    with caplog.at_level(logging.WARNING):
        delivered = await state.send_to_session("ghost-session", _msg("ghost-session"))

    assert delivered is False, (
        "send_to_session returned None for a frame it could not deliver; every "
        "caller therefore believes it succeeded"
    )
    assert any("ghost-session" in r.getMessage() for r in caplog.records), (
        "an undeliverable frame produced no log line at WARNING or above"
    )


@pytest.mark.asyncio
async def test_a_card_with_no_text_is_not_dropped_into_a_channel(state, caplog):
    """SDUI cards carry no `text`, and channels only forwarded `text`."""
    state._channel_collectors["channel_telegram_42"] = []

    with caplog.at_level(logging.WARNING):
        delivered = await state.send_to_session(
            "channel_telegram_42",
            _msg("channel_telegram_42", "sdui", {"component": "confirm_card"}),
        )

    if delivered is False:
        assert any(
            "channel_telegram_42" in r.getMessage() for r in caplog.records
        ), "a card dropped into a channel produced no log line"
    else:
        assert state._channel_collectors["channel_telegram_42"], (
            "send_to_session reported success but appended nothing to the collector"
        )


@pytest.mark.asyncio
async def test_delivery_to_a_live_socket_still_works(state):
    sent: list = []

    class _WS:
        async def send_json(self, data):
            sent.append(data)

    state.sessions["live"] = _WS()
    delivered = await state.send_to_session("live", _msg("live"))
    assert delivered is not False
    assert sent, "the happy path stopped delivering"


@pytest.mark.asyncio
async def test_an_undeliverable_permission_request_does_not_leak_its_pending_entry():
    """It registers the request before sending. If the send cannot land,
    the entry must not sit in the dict forever holding a turn open."""
    from agents import ui_handlers

    class _Orch:
        def __init__(self):
            self._pending_permission_requests = {}

        async def send(self, session_id, msg):
            return False  # nobody is listening

    orch = _Orch()
    await ui_handlers.send_permission_request(
        orch, "headless-1", "/tmp/x", "write", "because"
    )

    assert orch._pending_permission_requests == {}, (
        "a permission request that reached nobody left a pending entry behind: "
        f"{orch._pending_permission_requests}"
    )


def test_cli_handles_a_permission_request_frame():
    """The CLI receive loop had no branch for it, so the frame fell
    through and the operator waited out the 30s timeout never knowing
    they had been asked for consent."""
    src = (ROOT / "cli" / "main.py").read_text()
    loop_start = src.find('mtype = msg.get("type"')
    assert loop_start != -1, "CLI receive loop not found; update this test"
    window = src[loop_start: loop_start + 3000]
    assert "permission_request" in window, (
        "the CLI receive loop has no permission_request branch; a consent "
        "prompt is invisible there and the turn dies on the timeout"
    )


def test_cli_sdui_branch_does_not_report_an_unknown_component():
    """It printed `[UI Component: ?]` and broke out of the loop."""
    src = (ROOT / "cli" / "main.py").read_text()
    assert "[UI Component:" not in src, (
        "the CLI still prints '[UI Component: ?]' for an SDUI payload and "
        "breaks the loop, so a confirmation card ends the turn before the "
        "operator can answer it"
    )
