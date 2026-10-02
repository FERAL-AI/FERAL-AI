from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bridges.client_voice_control import interrupt_client_voice
from tests.test_server_websocket import ws_client, ws_mock_state  # noqa: F401

pytestmark = pytest.mark.no_auto_feral_home


def state_with(voice=None, *, chained=None):
    proxy = SimpleNamespace(get_session=Mock(return_value=voice), stop_session=AsyncMock())
    router = SimpleNamespace(_realtime=proxy, _gemini=None,
                             cancel_chained_response=chained or AsyncMock(return_value=False),
                             stop_session_voice=AsyncMock())
    return SimpleNamespace(voice_router=router, gemini_proxy=None)


@pytest.mark.asyncio
async def test_interrupt_requests_only_owned_response_and_never_closes_session():
    voice = SimpleNamespace(session_id="owned-session", cancel_response=AsyncMock())
    state = state_with(voice)
    result = await interrupt_client_voice(state, "owned-session")
    assert result == {"status": "requested", "cancel_requested": True, "session_preserved": True}
    voice.cancel_response.assert_awaited_once_with()
    state.voice_router.stop_session_voice.assert_not_awaited()
    state.voice_router._realtime.stop_session.assert_not_awaited()
    state.voice_router.cancel_chained_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefix_collision_does_not_cancel_another_session():
    voice = SimpleNamespace(session_id="sameprefix-other", cancel_response=AsyncMock())
    state = state_with(voice)
    assert (await interrupt_client_voice(state, "sameprefix-owned"))["status"] == "no_active_response"
    voice.cancel_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_chained_cancel_is_exactly_session_scoped():
    state = state_with(chained=AsyncMock(return_value=True))
    result = await interrupt_client_voice(state, "owned-session")
    assert result["status"] == "requested"
    state.voice_router.cancel_chained_response.assert_awaited_once_with("owned-session")
    state.voice_router.stop_session_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_provider_is_reported_without_session_teardown():
    state = state_with(SimpleNamespace(session_id="owned-session"))
    assert (await interrupt_client_voice(state, "owned-session"))["status"] == "unsupported"
    state.voice_router.stop_session_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_failure_does_not_fall_back_to_session_stop():
    voice = SimpleNamespace(session_id="owned-session", cancel_response=AsyncMock(side_effect=RuntimeError("fixture")))
    state = state_with(voice)
    result = await interrupt_client_voice(state, "owned-session")
    assert result["status"] == "error" and not result["cancel_requested"]
    state.voice_router.stop_session_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_gemini_lookup_is_exact_and_no_router_is_required():
    voice = SimpleNamespace(session_id="owned-session", cancel_response=AsyncMock())
    state = SimpleNamespace(voice_router=None, gemini_proxy=SimpleNamespace(_sessions={"owned-session": voice}))
    assert (await interrupt_client_voice(state, "owned-session"))["status"] == "requested"
    voice.cancel_response.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_provider_lookup_failure_is_reported_without_closing_session():
    state = state_with()
    state.voice_router._realtime.get_session.side_effect = RuntimeError("fixture lookup failure")
    assert (await interrupt_client_voice(state, "owned-session"))["status"] == "error"
    state.voice_router.stop_session_voice.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["_connected", "_response_in_progress"])
async def test_known_inactive_response_is_not_reported_as_cancellation_requested(flag):
    voice = SimpleNamespace(session_id="owned-session", cancel_response=AsyncMock())
    setattr(voice, flag, False)
    state = state_with(voice)
    assert (await interrupt_client_voice(state, "owned-session"))["status"] == "no_active_response"
    voice.cancel_response.assert_not_awaited()


def test_web_interrupt_uses_socket_session_instead_of_payload_destination(ws_mock_state, ws_client):
    voice = SimpleNamespace(session_id="owned-session", cancel_response=AsyncMock())
    router = state_with(voice).voice_router
    ws_mock_state.voice_router = router
    ws_mock_state.gemini_proxy = None
    with ws_client.websocket_connect("/v1/session?session_id=owned-session") as ws:
        ws.send_json({"type": "voice_interrupt", "hop": "client", "session_id": "another-session",
                      "payload": {"session_id": "another-session", "stream_id": "another-session"}})
        receipt = ws.receive_json()
        assert receipt["type"] == "voice_interrupt_ack" and receipt["session_id"] == "owned-session"
        assert receipt["payload"]["status"] == "requested"
        voice.cancel_response.assert_awaited_once_with()
        router.stop_session_voice.assert_not_awaited()


def test_superseded_socket_cannot_interrupt_replacement_voice(ws_mock_state, ws_client):
    voice = SimpleNamespace(session_id="owned-session", cancel_response=AsyncMock())
    ws_mock_state.voice_router = state_with(voice).voice_router
    ws_mock_state.gemini_proxy = None
    with ws_client.websocket_connect("/v1/session?session_id=owned-session") as ws:
        ws_mock_state.sessions["owned-session"] = object()
        ws.send_json({"type": "voice_interrupt", "hop": "client", "payload": {}})
        receipt = ws.receive_json()
        assert receipt["payload"]["status"] == "superseded"
        voice.cancel_response.assert_not_awaited()
