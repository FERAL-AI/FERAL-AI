"""Voice-attempt correlation with disposable state and synthetic providers."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from bridges.client_voice_configuration import ClientVoiceConfigurationError, configure_client_voice
from models.protocol import (AudioChunkPayload, AudioResponsePayload, TranscriptPayload,
                             TTSChunkPayload, VoiceConfigPayload, VoiceInterruptPayload,
                             VoiceStatusPayload)
from tests.test_client_voice_configuration import configured_router

pytest_plugins = ["tests.test_server_websocket"]


def attempt():
    return {"voice_attempt_version": 1, "voice_attempt_id": str(uuid4())}


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
def test_registered_voice_attempt_ack(monkeypatch, ws_mock_state, ws_client, surface):
    from tests.test_client_voice_configuration import config_request
    configured_router(monkeypatch, ws_mock_state)
    identity = attempt()
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        reply = config_request(ws, surface, {"mode": "realtime", **identity})
        assert reply["payload"] == {"mode": "realtime", "provider": "openai", "status": "ok", **identity}


@pytest.mark.parametrize("model,required", [
    (VoiceConfigPayload, {}), (AudioChunkPayload, {"data_b64": "AAAA"}),
    (VoiceInterruptPayload, {"voice_request_id": str(uuid4())}), (AudioResponsePayload, {}),
    (TranscriptPayload, {"text": "fixture"}), (TTSChunkPayload, {}),
    (VoiceStatusPayload, {}),
])
def test_canonical_voice_payload_retains_attempt(model, required):
    identity = attempt()
    payload = model(**required, **identity).model_dump()
    assert {key: payload.get(key) for key in identity} == identity


@pytest.mark.parametrize("invalid", [
    {"voice_attempt_version": True}, {"voice_attempt_version": 1.0},
    {"voice_attempt_version": "1"}, {"voice_attempt_version": 2},
    {"voice_attempt_version": None}, {"voice_attempt_id": None},
    {"voice_attempt_id": "not-a-uuid"},
    {"voice_attempt_id": "12345678-ABCD-1234-ABCD-123456789ABC"},
])
def test_canonical_attempt_refuses_invalid_identity(invalid):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        VoiceConfigPayload(**{**attempt(), **invalid})


@pytest.mark.parametrize("partial", ["voice_attempt_id", "voice_attempt_version"])
def test_partial_attempt_is_refused_and_legacy_omits_fields(partial):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        VoiceConfigPayload(**{partial: attempt()[partial]})
    payload = VoiceConfigPayload().model_dump()
    assert "voice_attempt_id" not in payload and "voice_attempt_version" not in payload


@pytest.mark.asyncio
async def test_configuration_ack_echoes_attempt(monkeypatch):
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    configured_router(monkeypatch, state)
    identity = attempt()
    ack = await configure_client_voice(state, "owned", owner, {"mode": "realtime", **identity})
    assert ack == {"mode": "realtime", "provider": "openai", "status": "ok", **identity}


@pytest.mark.asyncio
async def test_stale_stop_never_stops_replacement(monkeypatch):
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    first, second = attempt(), attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **first})
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **second})
    router.stop_session_voice = AsyncMock()
    with pytest.raises(ClientVoiceConfigurationError) as refusal:
        await configure_client_voice(state, "owned", owner, {"mode": "disabled", **first})
    assert refusal.value.code == "voice_attempt_superseded"
    router.stop_session_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_attempt_cannot_be_reopened_and_ids_are_bounded(monkeypatch):
    from bridges.client_voice_attempt import MAX_VOICE_ATTEMPTS_PER_CONNECTION
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    first = attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **first})
    await configure_client_voice(state, "owned", owner, {"mode": "disabled", **first})
    with pytest.raises(ClientVoiceConfigurationError) as reused:
        await configure_client_voice(state, "owned", owner, {"mode": "realtime", **first})
    assert reused.value.code == "voice_attempt_reused"
    for _ in range(MAX_VOICE_ATTEMPTS_PER_CONNECTION - 1):
        await configure_client_voice(state, "owned", owner, {"mode": "realtime", **attempt()})
    current = router._client_voice_attempts["owned"].binding
    with pytest.raises(ClientVoiceConfigurationError) as capacity:
        await configure_client_voice(state, "owned", owner, {"mode": "realtime", **attempt()})
    assert capacity.value.code == "voice_attempt_capacity"
    assert router._client_voice_attempts["owned"].binding is current and current.current()


@pytest.mark.asyncio
async def test_disconnect_releases_only_captured_owner_and_legacy_never_adopts_v1(monkeypatch):
    from bridges.client_voice_attempt import release_voice_attempt, voice_attempt_payload, voice_attempt_scope
    old_owner, replacement = object(), object()
    state = SimpleNamespace(sessions={"owned": old_owner})
    router = configured_router(monkeypatch, state)
    await configure_client_voice(state, "owned", old_owner, {"mode": "realtime", **attempt()})
    state.sessions["owned"] = replacement
    await configure_client_voice(state, "owned", replacement, {"mode": "realtime", **attempt()})
    binding = router._client_voice_attempts["owned"].binding
    release_voice_attempt(state, "owned", old_owner)
    assert binding.current()
    with voice_attempt_scope(binding):
        assert voice_attempt_payload({"text": "legacy"}, None) == {"text": "legacy"}
    release_voice_attempt(state, "owned", replacement)
    assert not binding.current() and "owned" not in router._client_voice_attempts


@pytest.mark.parametrize("kind", ["stop", "audio", "mute", "interrupt"])
@pytest.mark.parametrize("surface", ["legacy", "gateway"])
def test_registered_stale_controls_never_touch_replacement(monkeypatch, ws_mock_state, ws_client, kind, surface):
    from tests.test_client_voice_configuration import config_request
    router = configured_router(monkeypatch, ws_mock_state)
    first, second = attempt(), attempt()
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        config_request(ws, "legacy", {"mode": "realtime", **first})
        config_request(ws, "legacy", {"mode": "realtime", **second})
        stopper = router.stop_session_voice = AsyncMock()
        audio = router.handle_audio_from_client = AsyncMock()
        mute = router.set_session_muted = AsyncMock()
        cancel = router.cancel_chained_response = AsyncMock()
        params = {**first, "voice_request_id": str(uuid4())}
        msg_type = {"stop": "voice_config", "audio": "audio_chunk", "mute": "voice_mute", "interrupt": "voice_interrupt"}[kind]
        params.update({"mode": "disabled", "data_b64": "AAAA", "muted": True})
        if surface == "gateway":
            method = {"stop": "voice.config", "audio": "voice.audio", "mute": "voice.mute", "interrupt": "voice.interrupt"}[kind]
            ws.send_json({"type": "req", "id": "stale-control", "method": method, "params": params})
        else:
            ws.send_json({"type": msg_type, "hop": "client", "payload": params})
        reply = ws.receive_json()
        if surface == "gateway":
            assert reply["id"] == "stale-control" and not reply["ok"]
            assert reply["error"]["code"] == "voice_attempt_superseded"
        else:
            assert reply["payload"]["voice_attempt_id"] == first["voice_attempt_id"]
            assert "voice_attempt_superseded" in str(reply["payload"])
        for call in (stopper, audio, mute, cancel):
            call.assert_not_awaited()
        assert router._client_voice_attempts["owned"].binding.attempt_id == second["voice_attempt_id"]


@pytest.mark.asyncio
async def test_interrupt_echoes_request_identity_and_never_replays_it(monkeypatch):
    from bridges.client_voice_control import interrupt_client_voice_attempt
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    identity = attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **identity})
    router.cancel_chained_response = AsyncMock(return_value=True)
    params = {**identity, "voice_request_id": str(uuid4())}
    ack = await interrupt_client_voice_attempt(state, "owned", owner, params)
    assert ack == {"status": "requested", "cancel_requested": True, "session_preserved": True, **params}
    duplicate = await interrupt_client_voice_attempt(state, "owned", owner, params)
    assert duplicate["status"] == "duplicate" and not duplicate["cancel_requested"]
    router.cancel_chained_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_replacement_retires_old_provider_without_activity_claim(monkeypatch):
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    router._send_to_session = AsyncMock()
    await configure_client_voice(state, "owned", owner, {"mode": "chained", "provider": "configured", **attempt()})
    old = router._chained.get_session("owned")
    monkeypatch.setattr("voice.tts_providers.get_tts_provider", Mock(side_effect=RuntimeError("synthetic refusal")))
    with pytest.raises(ClientVoiceConfigurationError):
        await configure_client_voice(state, "owned", owner, {"mode": "chained", "provider": "configured", **attempt()})
    assert router._chained.get_session("owned") is None and "owned" not in router._session_voice_mode
    assert not router._client_voice_attempts["owned"].binding.current()
    router._send_to_session.reset_mock()
    with pytest.raises(asyncio.CancelledError):
        await old.send_frame("owned", {"type": "transcript", "payload": {"text": "late"}})
    router._send_to_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "gemini"])
async def test_realtime_producer_captures_attempt_before_connect(monkeypatch, kind):
    from bridges.client_voice_control import client_voice_control_scope
    from voice.realtime_proxy import RealtimeProxy, RealtimeSession
    from voice.gemini_realtime import GeminiRealtimeProxy, GeminiRealtimeSession
    session_class = RealtimeSession if kind == "openai" else GeminiRealtimeSession
    proxy_class = RealtimeProxy if kind == "openai" else GeminiRealtimeProxy
    async def connect(session):
        session._connected = True
    monkeypatch.setattr(session_class, "connect", connect)
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    configured_router(monkeypatch, state)
    send = AsyncMock()
    proxy = proxy_class(send_to_session=send)
    proxy._build_system_prompt = AsyncMock(return_value="synthetic")
    first, second = attempt(), attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **first})
    async with client_voice_control_scope(state, "owned", owner, first, audio=True):
        old = await proxy.start_session("owned", "webclient_owned")
    await old._on_audio_delta("owned", "AAAA", False)
    assert send.await_args.args[1].payload["voice_attempt_id"] == first["voice_attempt_id"]
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **second})
    send.reset_mock()
    with pytest.raises(asyncio.CancelledError):
        await old._on_audio_delta("owned", "AAAA", False)
    send.assert_not_awaited()
    async with client_voice_control_scope(state, "owned", owner, second, audio=True):
        new = await proxy.start_session("owned", "webclient_owned")
    await new._on_audio_delta("owned", "AAAA", False)
    assert send.await_args.args[1].payload["voice_attempt_id"] == second["voice_attempt_id"]
    await old.disconnect()
    await proxy.stop_session("owned")


@pytest.mark.asyncio
async def test_chained_producer_keeps_immutable_attempt(monkeypatch):
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    router._send_to_session = AsyncMock()
    first, second = attempt(), attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "chained", "provider": "configured", **first})
    old = router._chained.get_session("owned")
    try:
        await old.send_frame("owned", {"type": "transcript", "payload": {"text": "first"}})
        assert router._send_to_session.await_args.args[1].payload["voice_attempt_id"] == first["voice_attempt_id"]
        await configure_client_voice(state, "owned", owner, {"mode": "realtime", **second})
        router._send_to_session.reset_mock()
        with pytest.raises(asyncio.CancelledError):
            await old.send_frame("owned", {"type": "transcript", "payload": {"text": "late"}})
        router._send_to_session.assert_not_awaited()
    finally:
        await router.stop_session_voice("owned")


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["audio", "mute", "interrupt", "stop", "start"])
async def test_v1_controls_refuse_foreign_direct_producer(monkeypatch, control):
    from bridges.client_voice_attempt import VoiceAttemptError
    from bridges.client_voice_control import handle_client_voice_audio, interrupt_client_voice_attempt, mute_client_voice
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    identity = attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **identity})
    foreign = SimpleNamespace(session_id="owned", _voice_attempt=None, _connected=True,
                              _response_in_progress=True, send_audio=AsyncMock(), cancel_response=AsyncMock())
    state.gemini_proxy = SimpleNamespace(_sessions={"owned": foreign}, has_session=lambda sid: True,
                                         stop_session=AsyncMock())
    original_binding = router._client_voice_attempts["owned"].binding
    functions = {
        "audio": lambda: handle_client_voice_audio(state, "owned", owner, {**identity, "data_b64": "AAAA"}),
        "mute": lambda: mute_client_voice(state, "owned", owner, {**identity, "muted": True}),
        "interrupt": lambda: interrupt_client_voice_attempt(state, "owned", owner, {**identity, "voice_request_id": str(uuid4())}),
        "stop": lambda: configure_client_voice(state, "owned", owner, {**identity, "mode": "disabled"}),
        "start": lambda: configure_client_voice(state, "owned", owner, {**attempt(), "mode": "realtime"}),
    }
    with pytest.raises(VoiceAttemptError) as refusal:
        await functions[control]()
    assert refusal.value.code == "voice_producer_superseded"
    foreign.send_audio.assert_not_awaited()
    foreign.cancel_response.assert_not_awaited()
    state.gemini_proxy.stop_session.assert_not_awaited()
    assert not router.is_session_muted("owned")
    assert router._client_voice_attempts["owned"].binding is original_binding and original_binding.current()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["audio", "interrupt"])
async def test_control_owner_replaced_during_await_has_no_success_ack(monkeypatch, control):
    from bridges.client_voice_attempt import VoiceAttemptError
    from bridges.client_voice_control import handle_client_voice_audio, interrupt_client_voice_attempt
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    identity = attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **identity})
    binding = router._client_voice_attempts["owned"].binding
    async def retire(*args):
        state.sessions["owned"] = object()
    producer = SimpleNamespace(session_id="owned", _voice_attempt=binding, _connected=True,
                               _response_in_progress=True, send_audio=AsyncMock(side_effect=retire),
                               cancel_response=AsyncMock(side_effect=retire))
    state.gemini_proxy = SimpleNamespace(_sessions={"owned": producer}, has_session=lambda sid: True)
    with pytest.raises(VoiceAttemptError) as refusal:
        if control == "audio":
            await handle_client_voice_audio(state, "owned", owner, {**identity, "data_b64": "AAAA"})
        else:
            await interrupt_client_voice_attempt(state, "owned", owner, {**identity, "voice_request_id": str(uuid4())})
    assert refusal.value.code == "voice_attempt_superseded"
    # The already issued provider call is uncertain, not rolled back/replayed.
    assert producer.send_audio.await_count + producer.cancel_response.await_count == 1


@pytest.mark.asyncio
async def test_matching_producer_accepts_audio_mute_interrupt(monkeypatch):
    from bridges.client_voice_control import handle_client_voice_audio, interrupt_client_voice_attempt, mute_client_voice
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    identity = attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **identity})
    producer = SimpleNamespace(session_id="owned", _voice_attempt=router._client_voice_attempts["owned"].binding,
                               _connected=True, _response_in_progress=True, send_audio=AsyncMock(), cancel_response=AsyncMock())
    state.gemini_proxy = SimpleNamespace(_sessions={"owned": producer}, has_session=lambda sid: True)
    audio = await handle_client_voice_audio(state, "owned", owner, {**identity, "data_b64": "AAAA"})
    assert audio == {"received": True, **identity}
    mute = await mute_client_voice(state, "owned", owner, {**identity, "muted": True})
    assert mute == {"muted": True, **identity}
    params = {**identity, "voice_request_id": str(uuid4())}
    interrupt = await interrupt_client_voice_attempt(state, "owned", owner, params)
    assert interrupt == {"status": "requested", "cancel_requested": True, "session_preserved": True, **params}
    producer.send_audio.assert_awaited_once_with("AAAA")
    producer.cancel_response.assert_awaited_once()
    await handle_client_voice_audio(state, "owned", owner, {**identity, "data_b64": "BBBB"})
    producer.send_audio.assert_awaited_once_with("AAAA")


def test_registered_gateway_current_controls_keep_identity_and_mute_gate(monkeypatch, ws_mock_state, ws_client):
    from tests.test_client_voice_configuration import config_request
    router = configured_router(monkeypatch, ws_mock_state)
    identity = attempt()
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        config_request(ws, "gateway", {"mode": "realtime", **identity})
        producer = SimpleNamespace(session_id="owned", _voice_attempt=router._client_voice_attempts["owned"].binding,
                                   _connected=True, _response_in_progress=True, send_audio=AsyncMock(), cancel_response=AsyncMock())
        ws_mock_state.gemini_proxy = SimpleNamespace(_sessions={"owned": producer}, has_session=lambda sid: True)
        for index, (method, extra) in enumerate([
            ("voice.audio", {"data_b64": "AAAA"}), ("voice.mute", {"muted": True}),
            ("voice.audio", {"data_b64": "BBBB"}), ("voice.interrupt", {"voice_request_id": str(uuid4())}),
        ]):
            request_id = f"current-{index}"
            ws.send_json({"type": "req", "id": request_id, "method": method, "params": {**identity, **extra}})
            reply = ws.receive_json()
            assert reply["id"] == request_id and reply["ok"]
            assert all(reply["payload"][key] == value for key, value in identity.items())
            if method == "voice.interrupt":
                assert reply["payload"]["voice_request_id"] == extra["voice_request_id"]
        producer.send_audio.assert_awaited_once_with("AAAA")
        producer.cancel_response.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", ["owner", "producer"])
async def test_stop_rechecks_authority_between_provider_teardowns(monkeypatch, replace):
    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    identity = attempt()
    await configure_client_voice(state, "owned", owner, {"mode": "realtime", **identity})
    binding = router._client_voice_attempts["owned"].binding
    old = SimpleNamespace(session_id="owned", _voice_attempt=binding)
    foreign = SimpleNamespace(session_id="owned", _voice_attempt=None)
    node_id = "webclient_owned"
    realtime_sessions = {node_id: old}
    realtime_stop = AsyncMock()
    router._realtime = SimpleNamespace(_node_to_session={node_id: "owned"},
                                      get_session=realtime_sessions.get, stop_session=realtime_stop)
    async def stop_gemini(sid):
        if replace == "owner":
            state.sessions["owned"] = object()
        else:
            realtime_sessions[node_id] = foreign
        router._session_voice_mode["owned"] = "replacement-marker"
    router._gemini = SimpleNamespace(_node_to_session={node_id: "owned"},
                                    get_session=lambda node: old, stop_session=AsyncMock(side_effect=stop_gemini))
    with pytest.raises(ClientVoiceConfigurationError) as refusal:
        await configure_client_voice(state, "owned", owner, {**identity, "mode": "disabled"})
    assert refusal.value.code == ("voice_attempt_superseded" if replace == "owner" else "voice_producer_superseded")
    realtime_stop.assert_not_awaited()
    assert router._session_voice_mode["owned"] == "replacement-marker"
