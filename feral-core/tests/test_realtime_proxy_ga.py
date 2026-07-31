"""
Tests for OpenAI Realtime GA migration (PR #62 / Subagent A)
==============================================================
Covers:
  - WebSocket handshake: NO ``OpenAI-Beta`` header
  - GA event name dispatch (response.output_audio.delta, etc.)
  - Session update payload shape (type="realtime", audio config)
  - Conversation item events (conversation.item.added / done)
  - client_secrets route: happy path, 401, 502
  - VoiceRouter.open_session dispatch for mode="openai_realtime"
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice.realtime_proxy import (
    RealtimeProxy,
    RealtimeSession,
    DEFAULT_MODEL,
    OPENAI_REALTIME_URL,
)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture()
def mock_personality():
    p = MagicMock()
    p.current_time_of_day.return_value = "morning"
    p.get_voice_instructions.return_value = "You are FERAL, a helpful AI."
    return p


@pytest.fixture()
def proxy(mock_personality):
    with patch("voice.realtime_proxy.os.getenv", return_value="sk-test-key"), \
         patch("voice.personality.VoicePersonality", return_value=mock_personality):
        return RealtimeProxy(
            skill_registry=MagicMock(),
            skill_executor=MagicMock(),
            memory=MagicMock(),
            perception=MagicMock(),
        )


@pytest.fixture()
def session():
    """A standalone RealtimeSession with mocked WS, not connected to a proxy."""
    rs = RealtimeSession(
        session_id="sess-ga-1",
        node_id="phone-1",
        api_key="sk-test",
        model=DEFAULT_MODEL,
        voice="marin",
    )
    rs._ws = AsyncMock()
    rs._connected = True
    return rs


# ─────────────────────────────────────────────
# 1. No OpenAI-Beta header (GA)
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_no_beta_header():
    """GA handshake must NOT include the OpenAI-Beta header."""
    captured_kwargs = {}

    async def _fake_connect(url, **kwargs):
        captured_kwargs.update(kwargs)
        ws = AsyncMock()
        ws.__aiter__ = AsyncMock(return_value=iter([]))
        ws.close = AsyncMock()
        return ws

    rs = RealtimeSession(
        session_id="sess-1",
        node_id="node-1",
        api_key="sk-test",
    )

    # `realtime_proxy._connect_with_retry` prefers
    # `websockets.asyncio.client.connect` (kwarg `additional_headers`)
    # and falls back to legacy `websockets.connect`
    # (kwarg `extra_headers`) only when the asyncio import fails.
    # Patch BOTH so the test works regardless of which path the
    # installed `websockets` exposes. Cross-version pin lives in
    # tests/test_voice_realtime_headers.py.
    patches = []
    try:
        patches.append(patch("websockets.asyncio.client.connect", side_effect=_fake_connect))
    except (AttributeError, ImportError):
        pass
    patches.append(patch("websockets.connect", side_effect=_fake_connect))
    for p in patches:
        p.start()
    try:
        await rs.connect()
    finally:
        for p in patches:
            p.stop()

    # Either kwarg shape is acceptable — accept whichever was used.
    headers = (
        captured_kwargs.get("additional_headers")
        or captured_kwargs.get("extra_headers")
        or {}
    )
    assert "OpenAI-Beta" not in headers, "GA must NOT send the beta header"
    assert "Authorization" in headers
    assert headers["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_connect_url_uses_ga_model():
    """Handshake URL must target the GA model (gpt-realtime)."""
    captured_url = None

    async def _fake_connect(url, **kwargs):
        nonlocal captured_url
        captured_url = url
        ws = AsyncMock()
        ws.__aiter__ = AsyncMock(return_value=iter([]))
        ws.close = AsyncMock()
        return ws

    rs = RealtimeSession(
        session_id="sess-1",
        node_id="node-1",
        api_key="sk-test",
        model="gpt-realtime",
    )

    # Patch both connect entry points (see test_connect_no_beta_header).
    patches = []
    try:
        patches.append(patch("websockets.asyncio.client.connect", side_effect=_fake_connect))
    except (AttributeError, ImportError):
        pass
    patches.append(patch("websockets.connect", side_effect=_fake_connect))
    for p in patches:
        p.start()
    try:
        await rs.connect()
    finally:
        for p in patches:
            p.stop()

    assert captured_url is not None
    assert "model=gpt-realtime" in captured_url
    assert captured_url.startswith(OPENAI_REALTIME_URL)


# ─────────────────────────────────────────────
# 2. Session update payload (GA shape)
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_update_has_type_realtime(session):
    """session.update must include type='realtime' in session body."""
    sent_events = []
    session._ws.send = AsyncMock(side_effect=lambda msg: sent_events.append(json.loads(msg)))

    await session.configure(system_prompt="Be helpful", tools=[])

    session_updates = [e for e in sent_events if e.get("type") == "session.update"]
    assert len(session_updates) == 1
    su = session_updates[0]
    assert su["session"]["type"] == "realtime"


@pytest.mark.asyncio
async def test_session_update_uses_ga_audio_config(session):
    """GA session update must use the nested audio config with input/output."""
    sent_events = []
    session._ws.send = AsyncMock(side_effect=lambda msg: sent_events.append(json.loads(msg)))

    await session.configure()

    su = [e for e in sent_events if e["type"] == "session.update"][0]
    audio = su["session"]["audio"]
    assert "input" in audio
    assert "output" in audio
    assert audio["output"]["voice"] == "marin"
    assert audio["input"]["turn_detection"]["type"] == "server_vad"


@pytest.mark.asyncio
async def test_session_update_uses_output_modalities(session):
    """GA uses output_modalities not modalities."""
    sent_events = []
    session._ws.send = AsyncMock(side_effect=lambda msg: sent_events.append(json.loads(msg)))

    await session.configure()

    su = [e for e in sent_events if e["type"] == "session.update"][0]
    assert "output_modalities" in su["session"]
    assert "modalities" not in su["session"]


@pytest.mark.asyncio
async def test_session_update_uses_max_output_tokens(session):
    """GA uses max_output_tokens not max_response_output_tokens."""
    sent_events = []
    session._ws.send = AsyncMock(side_effect=lambda msg: sent_events.append(json.loads(msg)))

    await session.configure()

    su = [e for e in sent_events if e["type"] == "session.update"][0]
    assert "max_output_tokens" in su["session"]
    assert "max_response_output_tokens" not in su["session"]


# ─────────────────────────────────────────────
# 3. GA event name dispatch
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_output_audio_delta_invokes_callback(session):
    """response.output_audio.delta should invoke the audio delta callback."""
    cb = AsyncMock()
    session._on_audio_delta = cb

    await session._handle_event({
        "type": "response.output_audio.delta",
        "delta": "AAAA==",
    })
    cb.assert_awaited_once_with("sess-ga-1", "AAAA==", False)


@pytest.mark.asyncio
async def test_output_audio_done_invokes_callback(session):
    """response.output_audio.done should invoke audio delta with is_done=True."""
    cb = AsyncMock()
    session._on_audio_delta = cb

    await session._handle_event({"type": "response.output_audio.done"})
    cb.assert_awaited_once_with("sess-ga-1", "", True)


@pytest.mark.asyncio
async def test_output_audio_transcript_delta(session):
    """response.output_audio_transcript.delta should invoke transcript callback."""
    cb = AsyncMock()
    session._on_transcript = cb

    await session._handle_event({
        "type": "response.output_audio_transcript.delta",
        "delta": "Hello",
    })
    cb.assert_awaited_once_with(
        "sess-ga-1", "Hello", False, item_id="", previous_item_id="",
    )


@pytest.mark.asyncio
async def test_output_audio_transcript_done(session):
    """response.output_audio_transcript.done should invoke transcript with is_final=True."""
    cb = AsyncMock()
    session._on_transcript = cb

    await session._handle_event({
        "type": "response.output_audio_transcript.done",
        "transcript": "Hello, world!",
    })
    cb.assert_awaited_once_with(
        "sess-ga-1", "Hello, world!", True, item_id="", previous_item_id="",
    )


@pytest.mark.asyncio
async def test_output_text_delta(session):
    """response.output_text.delta should invoke transcript callback."""
    cb = AsyncMock()
    session._on_transcript = cb

    await session._handle_event({
        "type": "response.output_text.delta",
        "delta": "some text",
    })
    cb.assert_awaited_once_with(
        "sess-ga-1", "some text", False, item_id="", previous_item_id="",
    )


@pytest.mark.asyncio
async def test_output_text_done(session):
    """response.output_text.done should invoke transcript with is_final=True."""
    cb = AsyncMock()
    session._on_transcript = cb

    await session._handle_event({
        "type": "response.output_text.done",
        "text": "full text",
    })
    cb.assert_awaited_once_with(
        "sess-ga-1", "full text", True, item_id="", previous_item_id="",
    )


# ─────────────────────────────────────────────
# 4. Legacy event names are NOT handled
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_audio_delta_ignored(session):
    """Legacy response.audio.delta must NOT trigger the audio callback."""
    cb = AsyncMock()
    session._on_audio_delta = cb

    await session._handle_event({
        "type": "response.audio.delta",
        "delta": "AAAA==",
    })
    cb.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_audio_transcript_delta_ignored(session):
    """Legacy response.audio_transcript.delta must NOT trigger transcript."""
    cb = AsyncMock()
    session._on_transcript = cb

    await session._handle_event({
        "type": "response.audio_transcript.delta",
        "delta": "hello",
    })
    cb.assert_not_awaited()


# ─────────────────────────────────────────────
# 5. Conversation item events (GA)
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversation_item_added(session):
    """conversation.item.added should invoke the conversation item callback."""
    cb = AsyncMock()
    session._on_conversation_item = cb

    item = {"type": "message", "role": "assistant", "content": [{"type": "output_audio"}]}
    await session._handle_event({"type": "conversation.item.added", "item": item})

    cb.assert_awaited_once()
    call_args = cb.call_args[0]
    assert call_args[0] == "sess-ga-1"
    assert call_args[1]["action"] == "added"
    assert call_args[1]["item"]["content"][0]["type"] == "output_audio"


@pytest.mark.asyncio
async def test_conversation_item_done(session):
    """conversation.item.done should invoke the conversation item callback."""
    cb = AsyncMock()
    session._on_conversation_item = cb

    item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi"}]}
    await session._handle_event({"type": "conversation.item.done", "item": item})

    cb.assert_awaited_once()
    assert cb.call_args[0][1]["action"] == "done"
    assert cb.call_args[0][1]["item"]["content"][0]["type"] == "output_text"


# ─────────────────────────────────────────────
# 6. Tool call dispatch (unchanged contract)
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_call_invokes_callback(session):
    """response.function_call_arguments.done should call the tool callback."""
    cb = AsyncMock(return_value='{"result": "ok"}')
    session._on_tool_call = cb

    await session._handle_event({
        "type": "response.function_call_arguments.done",
        "call_id": "call-1",
        "name": "web_search__search",
        "arguments": '{"query": "weather"}',
    })

    cb.assert_awaited_once_with("sess-ga-1", "call-1", "web_search__search", '{"query": "weather"}')


# ─────────────────────────────────────────────
# 7. Proxy-level tests
# ─────────────────────────────────────────────

def test_default_model_is_ga():
    """DEFAULT_MODEL should be the GA model identifier."""
    assert DEFAULT_MODEL == "gpt-realtime"


def test_proxy_available_with_key(proxy):
    assert proxy.available is True


def test_proxy_unavailable_without_key():
    with patch("voice.realtime_proxy.os.getenv", return_value=""), \
         patch("voice.personality.VoicePersonality"):
        p = RealtimeProxy()
    assert p.available is False


# ─────────────────────────────────────────────
# 8. client_secrets route tests
# ─────────────────────────────────────────────

from fastapi.testclient import TestClient
from api.routes.realtime_client_secret import router as cs_router, _verify_bearer
from fastapi import FastAPI

_test_app = FastAPI()
_test_app.include_router(cs_router)


@pytest.fixture()
def cs_client():
    return TestClient(_test_app)


def test_client_secret_happy_path(cs_client):
    """POST with valid bearer + mocked OpenAI 200 → returns value + expires_at."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "client_secret": {
            "value": "ek_test_abc123",
            "expires_at": 1700000000,
        }
    }

    with patch("api.routes.realtime_client_secret._get_feral_api_key", return_value="test-key"), \
         patch("api.routes.realtime_client_secret._get_api_key", return_value="sk-openai-key"), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        resp = cs_client.post(
            "/api/voice/client_secrets",
            json={"model": "gpt-realtime", "voice": "marin", "ttl_seconds": 300},
            headers={"Authorization": "Bearer test-key"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["value"] == "ek_test_abc123"
    assert body["expires_at"] == 1700000000


def test_client_secret_401_no_bearer(cs_client):
    """POST without bearer → 401."""
    with patch("api.routes.realtime_client_secret._get_feral_api_key", return_value="test-key"):
        resp = cs_client.post("/api/voice/client_secrets", json={})
    assert resp.status_code == 401


def test_client_secret_401_wrong_bearer(cs_client):
    """POST with wrong bearer → 401."""
    with patch("api.routes.realtime_client_secret._get_feral_api_key", return_value="correct-key"):
        resp = cs_client.post(
            "/api/voice/client_secrets",
            json={},
            headers={"Authorization": "Bearer wrong-key"},
        )
    assert resp.status_code == 401


def test_client_secret_502_openai_error(cs_client):
    """POST when OpenAI returns non-200 → 502."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("api.routes.realtime_client_secret._get_feral_api_key", return_value="test-key"), \
         patch("api.routes.realtime_client_secret._get_api_key", return_value="sk-openai-key"), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        resp = cs_client.post(
            "/api/voice/client_secrets",
            json={},
            headers={"Authorization": "Bearer test-key"},
        )

    assert resp.status_code == 502
    assert "500" in resp.json()["detail"]


# ─────────────────────────────────────────────
# 9. VoiceRouter.open_session dispatch
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_router_dispatches_openai_realtime():
    """VoiceRouter.open_session with mode='openai_realtime' calls the GA proxy."""
    from voice.router import VoiceRouter

    mock_proxy = MagicMock()
    mock_proxy.available = True
    mock_session = MagicMock()
    mock_proxy.start_session = AsyncMock(return_value=mock_session)

    router = VoiceRouter(realtime_proxy=mock_proxy)
    result = await router.open_session("sess-1", "openai_realtime", {"voice": "cedar"})

    assert result is mock_session
    mock_proxy.start_session.assert_awaited_once()
    call_kwargs = mock_proxy.start_session.call_args
    assert call_kwargs[1]["voice"] == "cedar"
    assert call_kwargs[1]["model"] == "gpt-realtime"


@pytest.mark.asyncio
async def test_voice_router_returns_none_for_unavailable_proxy():
    """VoiceRouter.open_session returns None when proxy is unavailable."""
    from voice.router import VoiceRouter

    mock_proxy = MagicMock()
    mock_proxy.available = False

    router = VoiceRouter(realtime_proxy=mock_proxy)
    result = await router.open_session("sess-1", "openai_realtime")
    assert result is None


@pytest.mark.asyncio
async def test_voice_router_returns_none_for_unknown_mode():
    """VoiceRouter.open_session returns None for modes not handled by Subagent A."""
    from voice.router import VoiceRouter
    router = VoiceRouter()
    result = await router.open_session("sess-1", "chained")
    assert result is None


# ─────────────────────────────────────────────
# 10. Proxy voice parameter flows through
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_voice_parameter():
    """Voice parameter from proxy should flow into session configure."""
    rs = RealtimeSession(
        session_id="sess-v",
        node_id="node-v",
        api_key="sk-test",
        voice="cedar",
    )
    rs._ws = AsyncMock()
    rs._connected = True

    sent_events = []
    rs._ws.send = AsyncMock(side_effect=lambda msg: sent_events.append(json.loads(msg)))

    await rs.configure()

    su = [e for e in sent_events if e["type"] == "session.update"][0]
    assert su["session"]["audio"]["output"]["voice"] == "cedar"
