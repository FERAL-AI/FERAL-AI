"""Recoverable Realtime ``error`` events must not collapse the session.

Voice-collapse audit (2026-07), findings 3 and 8.

Finding 3: every ``error`` event except the "Cancellation failed / no
active response" race went to ``_on_error`` -> ``_handle_error``, whose
catch-all (``reason="openai_realtime_error"``) triggered the chained
morph. But the GA Realtime API uses ``error`` events for per-event
rejections too — unknown parameter, invalid value, empty audio buffer
commit, a ``tool_choice`` naming an undeclared tool — and leaves the
socket open. One of those was enough to destroy a healthy call.

Finding 8: ``force_tool_for_turn`` bypassed
``resolve_forced_tool_choice`` (which the ``configure`` path uses), so
forcing a tool the 128-cap had evicted sent OpenAI a ``tool_choice``
the session never declared. That is one of the rejections above, which
under finding 3 meant a full collapse.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from voice.realtime_proxy import RealtimeProxy, RealtimeSession
from voice.router import VoiceRouter


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _session_with_capture():
    """RealtimeSession recording ``on_error`` / ``on_notice`` traffic."""
    errors: list[str] = []
    notices: list[tuple[str, str]] = []

    async def on_error(_sid, msg):
        errors.append(msg)

    async def on_notice(_sid, code, detail):
        notices.append((code, detail))

    rs = RealtimeSession(
        session_id="s", node_id="n", api_key="sk",
        on_error=on_error, on_notice=on_notice,
    )
    rs._connected = True
    return rs, errors, notices


# ── non-fatal (session survives) ────────────────────────────────────


@pytest.mark.parametrize(
    "error_obj",
    [
        {"type": "invalid_request_error", "code": "unknown_parameter",
         "message": "Unknown parameter: 'session.temperature'."},
        {"type": "invalid_request_error", "code": "invalid_value",
         "message": "Invalid value: 'shimmer'."},
        {"type": "invalid_request_error", "code": "input_audio_buffer_commit_empty",
         "message": "Error committing input audio buffer: buffer too small."},
        {"type": "invalid_request_error", "code": "invalid_value",
         "message": "Invalid 'tool_choice': tool 'feral_routines__create' not found."},
        {"type": "server_error", "message": "The server had an error."},
    ],
)
@pytest.mark.asyncio
async def test_per_event_rejection_does_not_fail_the_session_over(error_obj):
    rs, errors, notices = _session_with_capture()

    await rs._handle_event({"type": "error", "event_id": "evt_1", "error": error_obj})

    assert errors == [], "recoverable rejection must not reach on_error"
    assert notices, "recoverable rejection must surface as a soft notice"
    assert rs.connected is True


@pytest.mark.asyncio
async def test_notice_reaches_the_router_as_an_available_voice_status():
    captured: list = []

    async def send(_sid, msg):
        captured.append(msg)

    router = VoiceRouter(audio_pipeline=MagicMock(), send_to_session=send)
    proxy = RealtimeProxy()
    proxy.attach_fallback_router(router)

    await proxy._handle_notice("sess-1", "unknown_parameter", "Unknown parameter: 'x'.")

    statuses = [m for m in captured if getattr(m, "type", None) == "voice_status"]
    assert len(statuses) == 1
    payload = statuses[0].payload
    assert payload["state"] == "available"
    assert payload["reason"] == "openai_realtime_unknown_parameter"
    # A notice must never fake the fallback bookkeeping.
    assert not router.is_session_degraded("sess-1")


@pytest.mark.asyncio
async def test_notice_does_not_clear_an_existing_degraded_banner():
    captured: list = []

    async def send(_sid, msg):
        captured.append(msg)

    router = VoiceRouter(audio_pipeline=MagicMock(), send_to_session=send)
    router._session_degraded["sess-1"] = {"state": "degraded"}

    await router.emit_voice_notice("sess-1", reason="openai_realtime_invalid_value")

    assert not [m for m in captured if getattr(m, "type", None) == "voice_status"]


# ── fatal (failover still fires) ────────────────────────────────────


@pytest.mark.parametrize(
    "error_obj",
    [
        {"code": "insufficient_quota", "message": "You exceeded your current quota."},
        {"code": "invalid_api_key", "message": "Incorrect API key provided."},
        {"code": "session_expired", "message": "Your session hit the maximum duration."},
        {"type": "invalid_request_error", "message": "401 Unauthorized"},
    ],
)
@pytest.mark.asyncio
async def test_session_fatal_error_still_reaches_on_error(error_obj):
    rs, errors, notices = _session_with_capture()

    await rs._handle_event({"type": "error", "error": error_obj})

    assert errors, "credential/expiry failures must still fail the session over"
    assert notices == []


@pytest.mark.asyncio
async def test_ws_close_level_failure_still_reaches_on_error():
    """The receive loop is the WS-close path and is unconditional."""

    class _DeadWS:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("received 1011 (internal error)")

    rs, errors, _notices = _session_with_capture()
    rs._ws = _DeadWS()

    await rs._receive_loop()

    assert errors and "1011" in errors[0]


# ── forced tool membership guard (finding 8) ────────────────────────


def _wired_session(tools: list[dict]):
    rs = RealtimeSession(session_id="s", node_id="n", api_key="sk", tools=tools)
    rs._connected = True
    sent: list[dict] = []
    rs._ws = AsyncMock()
    rs._ws.send = AsyncMock(side_effect=lambda m: sent.append(json.loads(m)))
    return rs, sent


@pytest.mark.asyncio
async def test_force_tool_absent_from_session_list_degrades_to_auto():
    """Never name a tool the session did not declare."""
    rs, sent = _wired_session([_tool("some_other__tool")])

    await rs.force_tool_for_turn("feral_routines__create")

    updates = [e for e in sent if e["type"] == "session.update"]
    assert updates, "the turn still restarts, just unpinned"
    assert updates[-1]["session"]["tool_choice"] == "auto"
    # Nothing to reset later — the pin never landed.
    assert rs._active_force_tool == ""


@pytest.mark.asyncio
async def test_force_tool_evicted_by_the_128_cap_degrades_to_auto():
    """The cap is what makes this reachable in production."""
    tools = [_tool(f"zz_tail__endpoint_{i}") for i in range(200)]
    rs, sent = _wired_session(tools)

    await rs.force_tool_for_turn("zz_tail__endpoint_199")

    updates = [e for e in sent if e["type"] == "session.update"]
    assert updates[-1]["session"]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_force_tool_present_in_session_list_is_still_pinned():
    rs, sent = _wired_session([_tool("feral_routines__create")])

    await rs.force_tool_for_turn("feral_routines__create")

    updates = [e for e in sent if e["type"] == "session.update"]
    assert updates[-1]["session"]["tool_choice"] == {
        "type": "function",
        "name": "feral_routines__create",
    }
    assert rs._active_force_tool == "feral_routines__create"
