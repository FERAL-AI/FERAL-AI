"""Voice must be torn down when a socket drops — and only then.

Voice-collapse audit (2026-07), findings 1 and 7.

Finding 1: ``VoiceRouter.stop_session_voice`` had exactly one caller —
the explicit ``voice_config {mode:"disabled"}`` branch. Neither
disconnect handler touched the voice router, so a tab close, a network
blip, or the phone app backgrounding left the OpenAI Realtime
WebSocket open (and billing) plus a stale ``_node_to_session`` entry
that handed the NEXT ``voice_session_start`` a dead handle.

Finding 7: ``voice_interrupt`` fell through to full ``stop_session``
calls when ``get_session`` missed — so a barge-in TERMINATED the
session, silently, with the client still rendering "listening".
A barge-in must never be able to end a session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_daemon_session_phone_branches import (  # noqa: F401
    _mock_state_with_supervisor,
    _phone_recorded,
    _flush_with_known_error,
)
from tests.test_hup_protocol import _TEST_NODE_KEY, _node_client, _register_node
from tests.test_server_websocket import ws_client, ws_mock_state  # noqa: F401
from voice.router import VoiceRouter

pytestmark = pytest.mark.no_auto_feral_home


def _voice_router_mock() -> MagicMock:
    vr = MagicMock()
    vr.stop_session_voice = AsyncMock()
    vr.stop_node_voice = AsyncMock()
    return vr


# ── web /v1/session disconnect ──────────────────────────────────────


def test_web_disconnect_stops_voice(ws_mock_state, ws_client):  # noqa: F811
    """Closing the tab tears the realtime session down."""
    ws_mock_state.voice_router = _voice_router_mock()

    with ws_client.websocket_connect("/v1/session") as ws:
        ws.receive_json()  # greeting
        session_id = next(iter(ws_mock_state.sessions.keys()))

    ws_mock_state.voice_router.stop_session_voice.assert_awaited_once_with(session_id)


def test_web_disconnect_of_a_superseded_socket_does_not_stop_voice(
    ws_mock_state, ws_client,  # noqa: F811
):
    """A stale socket must not kill the voice session of its replacement.

    When the same session_id has already reconnected, ``state.sessions``
    points at the NEWER socket and the live voice session belongs to
    it. The identity check is what keeps the late disconnect of the
    old socket from cutting the new call off.
    """
    ws_mock_state.voice_router = _voice_router_mock()

    with ws_client.websocket_connect("/v1/session") as ws:
        ws.receive_json()
        session_id = next(iter(ws_mock_state.sessions.keys()))
        # Simulate the reconnect having re-registered this session_id
        # against a different socket object.
        ws_mock_state.sessions[session_id] = object()

    ws_mock_state.voice_router.stop_session_voice.assert_not_awaited()


# ── daemon /v1/node disconnect ──────────────────────────────────────


def test_daemon_disconnect_stops_node_voice():
    """A phone dropping its socket releases its OpenAI session."""
    mock = _mock_state_with_supervisor()
    mock.voice_router = _voice_router_mock()

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-drop", node_type="phone")

    mock.voice_router.stop_node_voice.assert_awaited_once_with("phone-drop")


# ── VoiceRouter.stop_node_voice ─────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_node_voice_stops_realtime_and_clears_bindings():
    realtime = MagicMock()
    realtime._node_to_session = {"phone-1": "sess-1"}
    realtime.stop_session = AsyncMock()

    router = VoiceRouter(realtime_proxy=realtime, audio_pipeline=MagicMock())
    router.register_voice_config("phone-1", {"mode": "openai_realtime"})
    router.bind_node_to_session("phone-1", "sess-1")

    await router.stop_node_voice("phone-1")

    realtime.stop_session.assert_awaited_once_with("sess-1")
    assert "phone-1" not in router._node_voice_config
    assert "phone-1" not in router._node_session_map


@pytest.mark.asyncio
async def test_stop_node_voice_closes_the_nodes_chained_session():
    chained = MagicMock()
    chained.get_session = MagicMock(return_value=MagicMock())
    chained.close_session = AsyncMock()

    router = VoiceRouter(audio_pipeline=MagicMock())
    router.set_chained_pipeline(chained)
    router.bind_node_to_session("phone-1", "sess-1")
    router.set_session_voice_mode("sess-1", "chained")

    await router.stop_node_voice("phone-1")

    chained.close_session.assert_awaited_once_with("sess-1")
    assert "sess-1" not in router._session_voice_mode


@pytest.mark.asyncio
async def test_stop_node_voice_leaves_another_surfaces_session_mode_alone():
    """No chained session for the node -> session-scoped state untouched."""
    chained = MagicMock()
    chained.get_session = MagicMock(return_value=None)
    chained.close_session = AsyncMock()

    router = VoiceRouter(audio_pipeline=MagicMock())
    router.set_chained_pipeline(chained)
    router.bind_node_to_session("phone-1", "sess-shared")
    router.set_session_voice_mode("sess-shared", "realtime")

    await router.stop_node_voice("phone-1")

    chained.close_session.assert_not_awaited()
    assert router._session_voice_mode.get("sess-shared") == "realtime"


# ── voice_interrupt (finding 7) ─────────────────────────────────────


def test_voice_interrupt_never_stops_the_session():
    """Barge-in with no live handle is a no-op, not a teardown."""
    mock = _mock_state_with_supervisor()
    realtime = MagicMock()
    # A zombie session is exactly what makes `get_session` miss.
    realtime.get_session = MagicMock(return_value=None)
    realtime._node_to_session = {"phone-interrupt": "sess-1"}
    realtime.stop_session = AsyncMock()
    gemini = MagicMock()
    gemini.get_session = MagicMock(return_value=None)
    gemini._node_to_session = {"phone-interrupt": "gsess-1"}
    gemini.stop_session = AsyncMock()
    voice_router = MagicMock()
    voice_router._realtime = realtime
    voice_router._gemini = gemini
    mock.voice_router = voice_router

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-interrupt", node_type="phone")
            ws.send_json({
                "type": "voice_interrupt",
                "hup_version": "1.3.0",
                "ts": 1734369924.0,
                "payload": {"stream_id": "voice-stream-1", "reason": "barge_in"},
            })
            _flush_with_known_error(ws)

    realtime.stop_session.assert_not_awaited()
    gemini.stop_session.assert_not_awaited()
    # Reported honestly as "nothing to cancel" rather than "allowed".
    assert _phone_recorded(mock, "voice_interrupt", "denied")
