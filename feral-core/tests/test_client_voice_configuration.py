"""Actual registered client routes with synthetic STT/TTS providers only."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.protocol import MethodRegistry, register_core_methods
from voice.chained_pipeline import ChainedVoicePipeline
from voice.router import VoiceRouter

pytest_plugins = ["tests.test_server_websocket"]
pytestmark = pytest.mark.no_auto_feral_home


class SyntheticSTT:
    async def open_stream(self):
        await asyncio.Future()
        yield None

    async def close(self):
        pass


class SyntheticTTS:
    async def close(self):
        pass


class SyntheticOrchestrator:
    def __init__(self):
        self._locks = {}

    def _get_session_lock(self, session_id):
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def on_session_disconnect(self, session_id):
        pass


def configured_router(monkeypatch, state):
    state.orchestrator = SyntheticOrchestrator()
    router = VoiceRouter(orchestrator=state.orchestrator)
    router.set_chained_pipeline(ChainedVoicePipeline(vad_enabled=False))
    router._resolve_chained_config = Mock(return_value={
        "stt_provider": "faster_whisper", "tts_provider": "macos_say",
        "stt_model": "", "tts_model": "", "tts_voice": "", "tts_voice_id": "",
    })
    stt, tts = SyntheticSTT(), SyntheticTTS()
    monkeypatch.setattr("voice.stt_providers.get_stt_provider", Mock(return_value=stt))
    monkeypatch.setattr("voice.tts_providers.get_tts_provider", Mock(return_value=tts))
    state.voice_router = router
    state.gemini_proxy = None
    state.gateway_registry = MethodRegistry()
    register_core_methods(state.gateway_registry, state)
    return router


def config_request(ws, surface, params):
    if surface == "gateway":
        ws.send_json({"type": "req", "id": "voice-config-test", "method": "voice.config", "params": params})
    else:
        ws.send_json({"type": "voice_config", "hop": "client", "session_id": "foreign", "payload": params})
    return ws.receive_json()


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
def test_chained_ack_requires_opened_pipeline(monkeypatch, ws_mock_state, ws_client, surface):
    router = configured_router(monkeypatch, ws_mock_state)
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        reply = config_request(ws, surface, {"mode": "chained", "provider": "configured", "session_id": "foreign"})
        assert (reply["ok"] is True) if surface == "gateway" else (reply["type"] == "voice_config_ack")
        assert reply["payload"] == {"mode": "chained", "provider": "configured", "status": "ok"}
        assert router._chained.get_session("owned") is not None, "configuration acknowledged without opening the pipeline"
        assert router._chained.get_session("foreign") is None


def refusal(reply, surface, code):
    if surface == "gateway":
        assert reply["ok"] is False and reply["error"]["code"] == code
    else:
        assert reply["type"] == "voice_config_ack"
        assert reply["payload"]["status"] == "error" and reply["payload"]["code"] == code


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
@pytest.mark.parametrize("failure", ["constructor", "open", "partial_open", "missing_pipeline", "missing_router", "missing_lock"])
def test_chained_configuration_reports_actual_start_failure(monkeypatch, ws_mock_state, ws_client, surface, failure):
    router = configured_router(monkeypatch, ws_mock_state)
    code = "voice_chained_open_failed"
    if failure == "constructor":
        monkeypatch.setattr("voice.tts_providers.get_tts_provider", Mock(side_effect=RuntimeError("fixture-private-sentinel")))
    elif failure == "open":
        router._chained.open_session = AsyncMock(side_effect=RuntimeError("fixture-private-sentinel"))
        code = "voice_configuration_failed"
    elif failure == "partial_open":
        actual_open = router._chained.open_session

        async def partial_open(*args, **kwargs):
            await actual_open(*args, **kwargs)
            raise RuntimeError("fixture-private-sentinel")

        router._chained.open_session = partial_open
        code = "voice_configuration_failed"
    elif failure == "missing_pipeline":
        router._chained = None
        code = "voice_chained_unavailable"
    elif failure == "missing_router":
        ws_mock_state.voice_router = None
        code = "voice_runtime_unavailable"
    else:
        ws_mock_state.orchestrator._get_session_lock = Mock(return_value=object())
        code = "voice_runtime_unavailable"
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        reply = config_request(ws, surface, {"mode": "chained", "provider": "configured"})
        refusal(reply, surface, code)
        assert "fixture-private-sentinel" not in str(reply)
        assert "owned" not in router._session_voice_mode
        if router._chained is not None:
            assert router._chained.get_session("owned") is None


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
def test_superseded_configuration_has_no_runtime_effect(monkeypatch, ws_mock_state, ws_client, surface):
    router = configured_router(monkeypatch, ws_mock_state)
    opener = router.open_chained_session = AsyncMock()
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        ws_mock_state.sessions["owned"] = object()
        refusal(config_request(ws, surface, {"mode": "chained", "provider": "configured"}), surface, "voice_owner_superseded")
        opener.assert_not_awaited()
        assert router._session_voice_mode == {}


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
def test_owner_replaced_during_open_is_cleaned_without_success(monkeypatch, ws_mock_state, ws_client, surface):
    router = configured_router(monkeypatch, ws_mock_state)
    actual_open = router.open_chained_session

    async def replaced_open(*args, **kwargs):
        opened = await actual_open(*args, **kwargs)
        ws_mock_state.sessions["owned"] = object()
        return opened

    router.open_chained_session = replaced_open
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        refusal(config_request(ws, surface, {"mode": "chained", "provider": "configured"}), surface, "voice_owner_superseded")
        assert router._chained.get_session("owned") is None
        assert router._session_voice_mode == {}


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
def test_disabled_tears_down_exact_opened_pipeline(monkeypatch, ws_mock_state, ws_client, surface):
    router = configured_router(monkeypatch, ws_mock_state)
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        config_request(ws, surface, {"mode": "chained", "provider": "configured"})
        assert router._chained.get_session("owned") is not None
        reply = config_request(ws, surface, {"mode": "disabled"})
        assert reply["payload"]["status"] == "ok"
        assert router._chained.get_session("owned") is None
        assert "owned" not in router._session_voice_mode


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
@pytest.mark.parametrize("mode", ["realtime", "whisper", "auto"])
def test_existing_lazy_modes_remain_configuration_only(monkeypatch, ws_mock_state, ws_client, surface, mode):
    router = configured_router(monkeypatch, ws_mock_state)
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        reply = config_request(ws, surface, {"mode": mode})
        assert reply["payload"]["status"] == "ok"
        assert router._session_voice_mode["owned"] == mode
        assert router._chained.get_session("owned") is None


def test_unknown_gateway_mode_and_provider_never_mutate_runtime(monkeypatch, ws_mock_state, ws_client):
    router = configured_router(monkeypatch, ws_mock_state)
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        refusal(config_request(ws, "gateway", {"mode": "unknown"}), "gateway", "voice_mode_unsupported")
        refusal(config_request(ws, "gateway", {"mode": "chained", "provider": "unknown"}), "gateway", "voice_provider_unsupported")
        assert router._session_voice_mode == {} and router._chained.get_session("owned") is None


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
def test_client_secrets_and_options_do_not_reach_provider_constructors(monkeypatch, ws_mock_state, ws_client, surface):
    configured_router(monkeypatch, ws_mock_state)
    from voice.stt_providers import get_stt_provider
    from voice.tts_providers import get_tts_provider

    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        reply = config_request(ws, surface, {"mode": "chained", "provider": "configured", "provider_opts": {
            "api_key": "fixture-private-sentinel", "stt_provider": "untrusted", "send_frame": "untrusted",
        }})
        assert reply["payload"]["status"] == "ok"
        assert "fixture-private-sentinel" not in str(get_stt_provider.call_args)
        assert "fixture-private-sentinel" not in str(get_tts_provider.call_args)
        assert get_stt_provider.call_args.args[0] == "faster_whisper"


@pytest.mark.asyncio
async def test_replacement_configuration_waits_for_old_start_cleanup(monkeypatch):
    from bridges.client_voice_configuration import ClientVoiceConfigurationError, configure_client_voice

    old_owner, new_owner = object(), object()
    state = SimpleNamespace(sessions={"owned": old_owner})
    router = configured_router(monkeypatch, state)
    actual_open = router.open_chained_session
    entered, released = asyncio.Event(), asyncio.Event()

    async def slow_open(*args, **kwargs):
        entered.set()
        await released.wait()
        return await actual_open(*args, **kwargs)

    router.open_chained_session = slow_open
    old_start = asyncio.create_task(configure_client_voice(state, "owned", old_owner, {"mode": "chained", "provider": "configured"}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        state.sessions["owned"] = new_owner
        replacement = asyncio.create_task(configure_client_voice(state, "owned", new_owner, {"mode": "whisper"}))
        released.set()
        with pytest.raises(ClientVoiceConfigurationError, match="Voice configuration") as refused:
            await old_start
        assert refused.value.code == "voice_owner_superseded"
        assert (await replacement)["status"] == "ok"
        assert router._session_voice_mode["owned"] == "whisper"
        assert router._chained.get_session("owned") is None
    finally:
        released.set()
        await asyncio.gather(old_start, return_exceptions=True)
        await router.stop_session_voice("owned")


@pytest.mark.asyncio
async def test_cancelled_open_cleans_partial_pipeline_and_mode(monkeypatch):
    from bridges.client_voice_configuration import configure_client_voice

    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    actual_open = router._chained.open_session
    entered = asyncio.Event()

    async def partial_open(*args, **kwargs):
        await actual_open(*args, **kwargs)
        entered.set()
        await asyncio.Future()

    router._chained.open_session = partial_open
    opening = asyncio.create_task(configure_client_voice(state, "owned", owner, {"mode": "chained", "provider": "configured"}))
    await asyncio.wait_for(entered.wait(), 1)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert router._chained.get_session("owned") is None
    assert router._session_voice_mode == {}


@pytest.mark.parametrize("outcome", ["opened", "unavailable", "superseded"])
def test_legacy_direct_gemini_open_preserves_truthful_ack(monkeypatch, ws_mock_state, ws_client, outcome):
    router = configured_router(monkeypatch, ws_mock_state)
    ws_mock_state.identity_workspace = None
    sessions = {}

    async def open_gemini(**kwargs):
        if outcome == "unavailable":
            return None
        opened = SimpleNamespace(session_id=kwargs["session_id"])
        sessions[opened.session_id] = opened
        if outcome == "superseded":
            ws_mock_state.sessions["owned"] = object()
        return opened

    async def stop_gemini(session_id):
        sessions.pop(session_id, None)

    proxy = SimpleNamespace(_sessions=sessions, start_session=AsyncMock(side_effect=open_gemini),
                            stop_session=AsyncMock(side_effect=stop_gemini))
    ws_mock_state.gemini_proxy = proxy
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        reply = config_request(ws, "legacy", {"mode": "realtime", "provider": "gemini"})
        if outcome == "opened":
            assert reply["payload"]["status"] == "ok" and "owned" in sessions
            assert config_request(ws, "legacy", {"mode": "disabled"})["payload"]["status"] == "ok"
            proxy.stop_session.assert_awaited_once_with("owned")
        else:
            refusal(reply, "legacy", "voice_realtime_open_failed" if outcome == "unavailable" else "voice_owner_superseded")
            assert sessions == {} and router._session_voice_mode == {}


def test_gateway_realtime_keeps_its_existing_lazy_open_behavior(monkeypatch, ws_mock_state, ws_client):
    configured_router(monkeypatch, ws_mock_state)
    proxy = SimpleNamespace(_sessions={}, start_session=AsyncMock())
    ws_mock_state.gemini_proxy = proxy
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        reply = config_request(ws, "gateway", {"mode": "realtime", "provider": "gemini"})
        assert reply["ok"] is True and reply["payload"]["status"] == "ok"
        proxy.start_session.assert_not_awaited()


@pytest.mark.parametrize("surface", ["gateway", "legacy"])
@pytest.mark.parametrize("teardown", ["missing", "raises"])
def test_disabled_reports_direct_teardown_failure(monkeypatch, ws_mock_state, ws_client, surface, teardown):
    configured_router(monkeypatch, ws_mock_state)
    active = SimpleNamespace(session_id="owned")
    proxy = SimpleNamespace(_sessions={"owned": active})
    if teardown == "raises":
        proxy.stop_session = AsyncMock(side_effect=RuntimeError("fixture-private-sentinel"))
    ws_mock_state.gemini_proxy = proxy
    with ws_client.websocket_connect("/v1/session?session_id=owned") as ws:
        reply = config_request(ws, surface, {"mode": "disabled"})
        refusal(reply, surface, "voice_realtime_teardown_failed" if teardown == "missing" else "voice_configuration_failed")
        assert proxy._sessions["owned"] is active
        assert "fixture-private-sentinel" not in str(reply)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["chained", "gemini"])
async def test_failed_start_restores_mode_even_when_cleanup_fails(monkeypatch, engine):
    from bridges.client_voice_configuration import ClientVoiceConfigurationError, configure_client_voice

    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    router.set_session_voice_mode("owned", "whisper")
    opened = SimpleNamespace(session_id="owned")
    if engine == "chained":
        sessions = {}

        async def partial_open(*args, **kwargs):
            sessions["owned"] = opened
            raise RuntimeError("fixture-private-sentinel")

        router.open_chained_session = partial_open
        router._chained.get_session = lambda sid: sessions.get(sid)
        router._chained.close_session = AsyncMock(side_effect=RuntimeError("fixture-private-sentinel"))
        params, starter = {"mode": "chained"}, None
    else:
        state.gemini_proxy = SimpleNamespace(_sessions={})

        async def partial_start():
            state.gemini_proxy._sessions["owned"] = opened
            state.sessions["owned"] = object()
            return opened

        params, starter = {"mode": "realtime", "provider": "gemini"}, partial_start
    with pytest.raises(ClientVoiceConfigurationError) as refused:
        await configure_client_voice(state, "owned", owner, params, start_realtime=starter)
    assert refused.value.code == ("voice_configuration_failed" if engine == "chained" else "voice_realtime_teardown_failed")
    # A failed teardown leaves an owned engine present, so its attempted
    # mode cannot be presented as a successful rollback to whisper.
    assert router._session_voice_mode["owned"] == ("whisper" if engine == "chained" else "realtime")


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["chained", "gemini"])
@pytest.mark.parametrize("outcome", ["raises", "returns", "cancelled"])
async def test_replacement_engine_survives_suspended_configuration(monkeypatch, engine, outcome):
    from bridges.client_voice_configuration import ClientVoiceConfigurationError, configure_client_voice

    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    entered, resume = asyncio.Event(), asyncio.Event()
    replacement = SimpleNamespace(session_id="owned", stt_provider=object(), tts_provider=object())
    created = SimpleNamespace(session_id="owned")
    sessions = {}
    closer = AsyncMock()

    async def suspended_open(*args, **kwargs):
        if engine == "chained":
            created.stt_provider = kwargs["stt_provider"]
            created.tts_provider = kwargs["tts_provider"]
        sessions["owned"] = created
        entered.set()
        await resume.wait()
        if outcome == "raises":
            raise RuntimeError("fixture-private-sentinel")
        return created

    if engine == "chained":
        router._chained.open_session = suspended_open
        router._chained.get_session = lambda sid: sessions.get(sid)
        router._chained.close_session = closer
        params, starter = {"mode": "chained"}, None
    else:
        state.gemini_proxy = SimpleNamespace(_sessions=sessions, stop_session=closer)
        params, starter = {"mode": "realtime", "provider": "gemini"}, suspended_open
    task = asyncio.create_task(configure_client_voice(state, "owned", owner, params, start_realtime=starter))
    await asyncio.wait_for(entered.wait(), 1)
    sessions["owned"] = replacement
    router.set_session_voice_mode("owned", "auto")
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        resume.set()
        with pytest.raises(ClientVoiceConfigurationError):
            await task
    assert sessions["owned"] is replacement
    assert router._session_voice_mode["owned"] == "auto"
    closer.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_chained_replacement_does_not_restore_closed_engine_mode(monkeypatch):
    from bridges.client_voice_configuration import ClientVoiceConfigurationError, configure_client_voice

    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    await configure_client_voice(state, "owned", owner, {"mode": "chained"})
    old = router._chained.get_session("owned")
    actual_open = router._chained.open_session

    async def partial_open(*args, **kwargs):
        await actual_open(*args, **kwargs)
        raise RuntimeError("fixture-private-sentinel")

    router._chained.open_session = partial_open
    with pytest.raises(ClientVoiceConfigurationError):
        await configure_client_voice(state, "owned", owner, {"mode": "chained"})
    assert router._chained.get_session("owned") is None
    assert router._session_voice_mode == {}
    assert old._stt_task is None and old._turn_task is None


@pytest.mark.asyncio
async def test_replacement_during_owned_cleanup_keeps_new_mode(monkeypatch):
    from bridges.client_voice_configuration import ClientVoiceConfigurationError, configure_client_voice

    owner = object()
    state = SimpleNamespace(sessions={"owned": owner})
    router = configured_router(monkeypatch, state)
    opened = SimpleNamespace(session_id="owned")
    replacement = SimpleNamespace(session_id="owned")
    sessions = {}
    cleanup_entered, resume = asyncio.Event(), asyncio.Event()

    async def open_and_supersede(*args, **kwargs):
        sessions["owned"] = opened
        router.set_session_voice_mode("owned", "chained")
        state.sessions["owned"] = object()
        return opened

    async def cleanup(session_id):
        assert sessions.pop(session_id) is opened
        cleanup_entered.set()
        await resume.wait()

    router.open_chained_session = open_and_supersede
    router._chained.get_session = lambda sid: sessions.get(sid)
    router._chained.close_session = cleanup
    opening = asyncio.create_task(configure_client_voice(state, "owned", owner, {"mode": "chained"}))
    await asyncio.wait_for(cleanup_entered.wait(), 1)
    sessions["owned"] = replacement
    router.set_session_voice_mode("owned", "whisper")
    resume.set()
    with pytest.raises(ClientVoiceConfigurationError):
        await opening
    assert sessions["owned"] is replacement
    assert router._session_voice_mode["owned"] == "whisper"
