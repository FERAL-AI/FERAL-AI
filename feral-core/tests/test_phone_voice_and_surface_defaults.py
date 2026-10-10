"""Phone voice joins the primary session; phone chat defaults to brain_host.

Two requests from the Theora app side, both about the brain not depending on
the client to get routing right:

1. voice_session_start carried no session_id, so every phone voice session
   ran on `voice-<node>` and nothing said by voice reached chat or the web
   UI. A phone now defaults to the primary session. Because a web tab may
   hold that same session, a tab closing must not end the phone's call.
2. A phone chat_request with no usable device_target resolved through
   source "phone_surface" to http_api, which denies the agentic computer-use
   loop and the desktop shell. It now resolves to brain_host.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_daemon_session_phone_branches import (
    _flush_with_known_error,
    _mock_state_with_supervisor,
)
from tests.test_hup_protocol import _TEST_NODE_KEY, _node_client, _register_node
from tests.test_server_websocket import ws_client, ws_mock_state  # noqa: F401

pytestmark = pytest.mark.no_auto_feral_home


def _start_voice(mock, *, node_type="phone", session_id=None):
    if mock.voice_router is None:
        mock.voice_router = MagicMock()
    mock.voice_router.open_session = AsyncMock(return_value=MagicMock())
    payload = {
        "stream_id": "voice-stream-1", "sample_rate": 16000, "channels": 1,
        "language_hint": "en-US", "mode": "push_to_talk",
        "interrupt_policy": "barge_in", "camera_linked": False,
    }
    if session_id is not None:
        payload["session_id"] = session_id
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-voice", node_type=node_type)
            ws.send_json({"type": "voice_session_start", "hup_version": "1.3.0",
                          "ts": 1.0, "payload": payload})
            _flush_with_known_error(ws)
    return mock.voice_router.open_session.call_args.kwargs["session_id"]


def test_phone_voice_defaults_to_the_primary_session():
    mock = _mock_state_with_supervisor()
    mock.primary_session_id = "primary-abc"
    assert _start_voice(mock) == "primary-abc"
    mock.voice_router.bind_node_to_session.assert_called_with("phone-voice", "primary-abc")


def test_an_explicit_voice_session_id_wins():
    mock = _mock_state_with_supervisor()
    mock.primary_session_id = "primary-abc"
    assert _start_voice(mock, session_id="thread-7") == "thread-7"


def test_a_non_phone_node_keeps_its_stream_session():
    mock = _mock_state_with_supervisor()
    mock.primary_session_id = "primary-abc"
    assert _start_voice(mock, node_type="desktop") == "voice-stream-1"


def test_chained_barge_in_cancels_the_session_the_phone_is_bound_to():
    """Barge-in used to re-derive `stream_id or voice-<node>`.

    With phone voice on the primary session that derivation names a session
    that does not exist, so speaking over a chained-mode reply cancelled
    nothing. The router's node binding is now the source of truth.
    """
    mock = _mock_state_with_supervisor()
    vr = MagicMock()
    vr._realtime = None
    vr._gemini = None
    vr.session_for_node = MagicMock(return_value="primary-abc")
    vr.cancel_chained_response = AsyncMock(return_value=True)
    mock.voice_router = vr
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-voice", node_type="phone")
            ws.send_json({"type": "voice_interrupt", "hup_version": "1.3.0", "ts": 1.0,
                          "payload": {"stream_id": "voice-stream-1", "reason": "barge_in"}})
            _flush_with_known_error(ws)
    vr.cancel_chained_response.assert_awaited_once_with("primary-abc")


def _chat(mock, *, node_type="phone", device_target=None):
    mock.orchestrator.handle_command = AsyncMock(return_value={"text": "ok"})
    payload = {"session_id": "s-1", "text": "check my computer",
               "reply_mode": "final", "channel": "chat", "reply_to": None}
    if device_target is not None:
        payload["device_target"] = device_target
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-chat", node_type=node_type)
            ws.send_json({"type": "chat_request", "hup_version": "1.3.0",
                          "ts": 1.0, "payload": payload})
            assert ws.receive_json()["type"] == "chat_response"
    return mock.orchestrator.handle_command.call_args.kwargs["context"]


def test_phone_chat_without_device_target_resolves_to_brain_host():
    from security.dangerous_tools import resolve_surface_from_context

    ctx = _chat(_mock_state_with_supervisor())
    assert ctx["surface"] == "brain_host"
    assert ctx["source"] == "phone_surface"  # provenance unchanged
    assert resolve_surface_from_context(ctx) == "brain_host"


def test_auto_device_target_resolves_to_brain_host_and_is_dropped():
    """`auto` means no preference, the only non-device value the wire allows."""
    ctx = _chat(_mock_state_with_supervisor(), device_target="auto")
    assert ctx["surface"] == "brain_host"
    assert "device_target" not in ctx


def test_an_unknown_device_target_never_reaches_routing():
    """ChatRequestPayload.device_target is a Literal, so "tv" is refused."""
    mock = _mock_state_with_supervisor()
    mock.orchestrator.handle_command = AsyncMock(return_value={"text": "ok"})
    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-chat", node_type="phone")
            ws.send_json({"type": "chat_request", "hup_version": "1.3.0", "ts": 1.0,
                          "payload": {"session_id": "s-1", "text": "hi", "reply_mode": "final",
                                      "channel": "chat", "reply_to": None, "device_target": "tv"}})
            assert ws.receive_json()["type"] == "error"
    mock.orchestrator.handle_command.assert_not_awaited()


def test_an_explicit_phone_target_still_routes_to_the_phone():
    from security.dangerous_tools import resolve_surface_from_context

    ctx = _chat(_mock_state_with_supervisor(), device_target="phone")
    assert "surface" not in ctx
    assert resolve_surface_from_context(ctx) == "phone_actuator"


def test_a_non_phone_node_is_not_promoted():
    from security.dangerous_tools import resolve_surface_from_context

    ctx = _chat(_mock_state_with_supervisor(), node_type="desktop")
    assert "surface" not in ctx
    assert resolve_surface_from_context(ctx) == "http_api"


def _web_disconnect(ws_mock_state, ws_client, bound):  # noqa: F811
    vr = MagicMock()
    vr.stop_session_voice = AsyncMock()
    vr.nodes_bound_to_session = MagicMock(return_value=bound)
    ws_mock_state.voice_router = vr
    with ws_client.websocket_connect("/v1/session") as ws:
        ws.receive_json()  # greeting
    return vr.stop_session_voice


def test_web_tab_closing_does_not_end_a_phone_call_on_its_session(
    ws_mock_state, ws_client,  # noqa: F811
):
    stop = _web_disconnect(ws_mock_state, ws_client, bound=["feral-iphone-1"])
    stop.assert_not_awaited()


def test_web_tab_closing_still_stops_its_own_voice(ws_mock_state, ws_client):  # noqa: F811
    stop = _web_disconnect(ws_mock_state, ws_client, bound=[])
    stop.assert_awaited_once()
