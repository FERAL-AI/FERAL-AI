"""
Deeper tests for voice.router (routing, fallback, sessions, config) and
voice.realtime_proxy (sessions, connection state, messages, errors).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice.router import VoiceRouter, _ENV_VOICE_PROVIDER
from voice.realtime_proxy import (
    AUDIO_FORMAT,
    DEFAULT_MODEL,
    RealtimeProxy,
    RealtimeSession,
    SAMPLE_RATE,
)


# ═══════════════════════════════════════════════════════════════════════════
# VoiceRouter — provider selection & config
# ═══════════════════════════════════════════════════════════════════════════


def test_resolve_provider_whisper_when_mode_whisper():
    rt = MagicMock(available=True)
    gem = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.register_voice_config("n1", {"mode": "whisper", "supports_realtime": True})
    assert r._resolve_provider("n1") == "whisper"


def test_resolve_provider_explicit_gemini_when_available(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rt = MagicMock(available=True)
    gem = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.register_voice_config("n1", {"voice_provider": "gemini"})
    assert r._resolve_provider("n1") == "gemini"


def test_resolve_provider_explicit_openai_when_available(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rt = MagicMock(available=True)
    gem = MagicMock(available=False)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.register_voice_config("n1", {"voice_provider": "openai"})
    assert r._resolve_provider("n1") == "openai"


def test_resolve_provider_env_gemini_overrides(monkeypatch):
    gem = MagicMock(available=True)
    rt = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.register_voice_config("n1", {"supports_realtime": True})
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "gemini")
    assert r._resolve_provider("n1") == "gemini"


def test_resolve_provider_supports_realtime_falls_back_openai(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rt = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.register_voice_config("n1", {"supports_realtime": True})
    assert r._resolve_provider("n1") == "openai"


def test_resolve_provider_whisper_when_realtime_unavailable(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rt = MagicMock(available=False)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.register_voice_config("n1", {"supports_realtime": True, "voice_provider": "openai"})
    assert r._resolve_provider("n1") == "whisper"


def test_resolve_session_provider_whisper_when_not_realtime_mode():
    r = VoiceRouter(realtime_proxy=MagicMock(available=True), audio_pipeline=MagicMock())
    r.set_session_voice_mode("sess-full", "whisper")
    assert r._resolve_session_provider("sess-full") == "whisper"


def test_resolve_session_provider_openai_for_realtime_with_proxy(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rt = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_session_voice_mode("abc", "realtime")
    assert r._resolve_session_provider("abc") == "openai"


def test_resolve_session_provider_gemini_from_env(monkeypatch):
    gem = MagicMock(available=True)
    rt = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.set_session_voice_mode("sess1", "realtime")
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "gemini")
    assert r._resolve_session_provider("sess1") == "gemini"


def test_should_use_realtime_and_session_uses_realtime():
    rt = MagicMock(available=True)
    gem = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.register_voice_config("nG", {"voice_provider": "gemini"})
    assert r.should_use_realtime("nG") is True

    r.set_session_voice_mode("s2", "realtime")
    assert r.session_uses_realtime("s2") is True


def test_bind_register_and_node_session_map():
    r = VoiceRouter(realtime_proxy=MagicMock(), audio_pipeline=MagicMock())
    r.register_voice_config("node-a", {"mode": "whisper"})
    r.bind_node_to_session("node-a", "sess-99")
    assert r._node_voice_config["node-a"]["mode"] == "whisper"
    assert r._node_session_map["node-a"] == "sess-99"


@pytest.mark.asyncio
async def test_handle_audio_from_node_gemini_path(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    gem = MagicMock(available=True)
    gs = MagicMock(connected=True)
    gs.send_audio = AsyncMock()
    gem.get_session.return_value = gs
    gem.start_session = AsyncMock(return_value=gs)
    rt = MagicMock(available=True)

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.register_voice_config("n1", {"voice_provider": "gemini"})

    await r.handle_audio_from_node("n1", "sid", "YWFh")
    gem.get_session.assert_called_with("n1")
    gs.send_audio.assert_awaited_once_with("YWFh")


@pytest.mark.asyncio
async def test_handle_audio_from_node_openai_starts_session(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rs = MagicMock(connected=True)
    rs.send_audio = AsyncMock()
    rt = MagicMock(available=True)
    rt.get_session.return_value = None
    rt.start_session = AsyncMock(return_value=rs)

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.register_voice_config("n1", {"voice_provider": "openai"})

    await r.handle_audio_from_node("n1", "sid", "YmJi")
    rt.start_session.assert_awaited_once()
    kwargs = rt.start_session.await_args.kwargs
    assert rt.start_session.await_args.args == ("sid", "n1")
    assert kwargs["model"] == "gpt-realtime"
    assert kwargs["voice"] == "marin"
    assert kwargs["input_sample_rate"] == 24000
    assert kwargs["language_hint"] == ""
    rs.send_audio.assert_awaited_once_with("YmJi")


@pytest.mark.asyncio
async def test_handle_audio_from_client_whisper_invokes_pipeline(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    audio = MagicMock()
    audio.process_audio_chunk = AsyncMock(return_value=None)

    r = VoiceRouter(
        realtime_proxy=MagicMock(available=False),
        audio_pipeline=audio,
    )
    r.set_session_voice_mode("full-sess-id", "whisper")

    await r.handle_audio_from_client("full-sess-id", "eHh4")
    audio.process_audio_chunk.assert_awaited()


@pytest.mark.asyncio
async def test_handle_audio_for_gemini_noop_when_proxy_unavailable():
    r = VoiceRouter(realtime_proxy=None, audio_pipeline=MagicMock())
    await r.handle_audio_for_gemini("s", "YWFh")
    # no exception


@pytest.mark.asyncio
async def test_stop_session_voice_stops_gemini_and_realtime_and_clears_mode():
    gem = MagicMock()
    gem._node_to_session = {"webclient_abcdef00": "gsid"}
    gem.stop_session = AsyncMock()

    rt = MagicMock()
    rt._node_to_session = {"webclient_abcdef00": "rsid"}
    rt.stop_session = AsyncMock()

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.set_session_voice_mode("abcdef00-uuid-rest", "realtime")

    await r.stop_session_voice("abcdef00-uuid-rest")

    gem.stop_session.assert_awaited_once_with("gsid")
    rt.stop_session.assert_awaited_once_with("rsid")
    assert "abcdef00-uuid-rest" not in r._session_voice_mode


@pytest.mark.asyncio
async def test_shutdown_invokes_both_proxies():
    gem = MagicMock()
    gem.shutdown = AsyncMock()
    rt = MagicMock()
    rt.shutdown = AsyncMock()

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)

    await r.shutdown()
    rt.shutdown.assert_awaited_once()
    gem.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_text_from_node_orchestrator_when_no_realtime_session(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    orch = MagicMock()
    orch.handle_command_stream = AsyncMock()
    rt = MagicMock(available=True)
    rt.get_session.return_value = None

    r = VoiceRouter(
        realtime_proxy=rt,
        audio_pipeline=MagicMock(),
        orchestrator=orch,
    )
    r.register_voice_config("n1", {"voice_provider": "openai"})

    await r.handle_text_from_node("n1", "sid", "hello")
    orch.handle_command_stream.assert_awaited_once()
    ctx = orch.handle_command_stream.await_args.kwargs["context"]
    assert ctx["source"] == "node_text"
    assert ctx["node_id"] == "n1"


@pytest.mark.asyncio
async def test_get_last_assistant_text_from_memory():
    mem = MagicMock()
    mem.working_get.return_value = [
        {"role": "user", "text": "u"},
        {"role": "assistant", "text": "final answer"},
    ]
    r = VoiceRouter(memory=mem, audio_pipeline=MagicMock())
    assert r._get_last_assistant_text("sid") == "final answer"


# ═══════════════════════════════════════════════════════════════════════════
# RealtimeProxy & RealtimeSession
# ═══════════════════════════════════════════════════════════════════════════


def test_realtime_proxy_available_reflects_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = RealtimeProxy()
    assert p.available is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    p2 = RealtimeProxy()
    assert p2.available is True


def test_realtime_proxy_get_session_unknown_node():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    p = RealtimeProxy()
    assert p.get_session("no-such") is None
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_realtime_session_connect_skips_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rs = RealtimeSession("sid", "nid", api_key="")
    await rs.connect()
    assert rs.connected is False


@pytest.mark.asyncio
async def test_realtime_session_send_audio_noop_when_disconnected():
    rs = RealtimeSession("sid", "nid", api_key="x")
    await rs.send_audio("aaa")
    # _send not invoked — no ws


@pytest.mark.asyncio
async def test_realtime_session_configure_noop_when_disconnected():
    rs = RealtimeSession("sid", "nid", api_key="x")
    await rs.configure(system_prompt="hi", tools=[{"function": {"name": "a__b"}}])


@pytest.mark.asyncio
async def test_realtime_proxy_start_session_registers_node_map(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    async def fake_connect(self):
        self._connected = True
        self._ws = MagicMock()
        self._ws.send = AsyncMock()

    with patch.object(RealtimeSession, "connect", fake_connect):
        p = RealtimeProxy()
        rs = await p.start_session("sess-1", "node-1")
        assert p.get_session("node-1") is rs
        assert p._node_to_session["node-1"] == "sess-1"
        await rs.disconnect()


@pytest.mark.asyncio
async def test_realtime_proxy_stop_session_removes_mappings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    async def fake_connect(self):
        self._connected = True
        self._ws = MagicMock()
        self._ws.send = AsyncMock()

    with patch.object(RealtimeSession, "connect", fake_connect):
        p = RealtimeProxy()
        await p.start_session("sess-1", "node-1")
        await p.stop_session("sess-1")
        assert p.get_session("node-1") is None
        assert "sess-1" not in p._sessions


@pytest.mark.asyncio
async def test_realtime_proxy_relay_audio_when_connected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    async def fake_connect(self):
        self._connected = True
        self._ws = MagicMock()
        self._ws.send = AsyncMock()

    with patch.object(RealtimeSession, "connect", fake_connect):
        p = RealtimeProxy()
        await p.start_session("sess-1", "node-1")
        rs = p.get_session("node-1")
        rs.send_audio = AsyncMock()
        await p.relay_audio("node-1", "pcm")
        rs.send_audio.assert_awaited_once_with("pcm")
        await p.stop_session("sess-1")


@pytest.mark.asyncio
async def test_handle_tool_call_invalid_tool_name():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    p = RealtimeProxy(skill_registry=MagicMock(), skill_executor=MagicMock())
    out = await p._handle_tool_call("sid", "c1", "badname", "{}")
    assert "Invalid tool name" in out
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_handle_tool_call_skill_not_found():
    reg = MagicMock()
    reg.skills = {}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    p = RealtimeProxy(skill_registry=reg, skill_executor=MagicMock())
    out = await p._handle_tool_call("sid", "c1", "missing__bash", "{}")
    assert "Skill not found" in out
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_handle_event_error_invokes_on_error():
    errors: list[tuple[str, str]] = []

    async def on_err(sid: str, msg: str):
        errors.append((sid, msg))

    rs = RealtimeSession("sid", "nid", api_key="k", on_error=on_err)
    await rs._handle_event({"type": "error", "error": {"message": "rate limited"}})
    assert errors and "rate" in errors[0][1].lower()


@pytest.mark.asyncio
async def test_handle_event_audio_delta_invokes_callback():
    deltas: list[tuple[str, str, bool]] = []

    async def on_delta(sid: str, b64: str, done: bool):
        deltas.append((sid, b64, done))

    rs = RealtimeSession("sid", "nid", api_key="k", on_audio_delta=on_delta)
    await rs._handle_event({"type": "response.output_audio.delta", "delta": "QQ=="})
    assert deltas == [("sid", "QQ==", False)]


@pytest.mark.asyncio
async def test_handle_event_tool_call_executes_and_sends_result(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    ep = MagicMock(id="bash")
    skill = MagicMock()
    skill.endpoints = [ep]
    reg = MagicMock()
    reg.skills = {"computer_use": skill}

    skill_exec = MagicMock()
    skill_exec.execute = AsyncMock(return_value={"data": {"ok": True}})

    sent: list[dict] = []

    async def capture_send(event: dict):
        sent.append(event)

    p = RealtimeProxy(skill_registry=reg, skill_executor=skill_exec)

    async def fake_connect(self):
        self._connected = True
        self._ws = MagicMock()
        self._send = capture_send

    with patch.object(RealtimeSession, "connect", fake_connect):
        rs = await p.start_session("sess-t", "node-t")

    await rs._handle_event({
        "type": "response.function_call_arguments.done",
        "call_id": "call-1",
        "name": "computer_use__bash",
        "arguments": "{}",
    })

    skill_exec.execute.assert_awaited()
    assert any(
        x.get("type") == "conversation.item.create"
        and x.get("item", {}).get("call_id") == "call-1"
        for x in sent
    )
    await rs.disconnect()


@pytest.mark.asyncio
async def test_realtime_proxy_shutdown_stops_all_sessions(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    async def fake_connect(self):
        self._connected = True
        self._ws = MagicMock()
        self._ws.send = AsyncMock()

    with patch.object(RealtimeSession, "connect", fake_connect):
        p = RealtimeProxy()
        await p.start_session("a", "n1")
        await p.start_session("b", "n2")
        await p.shutdown()
        assert not p._sessions


# ═══════════════════════════════════════════════════════════════════════════
# VoiceRouter — additional routing, wake word, whisper/orchestrator flow
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handle_audio_from_node_wake_word_skips_downstream_when_false(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    wake = MagicMock()
    wake.enabled = True
    wake.process_frame = AsyncMock(return_value=False)
    rt = MagicMock(available=True)
    rt.get_session = MagicMock()
    r = VoiceRouter(
        realtime_proxy=rt,
        audio_pipeline=MagicMock(),
        wake_word_detector=wake,
    )
    r.register_voice_config("n1", {"voice_provider": "openai"})

    await r.handle_audio_from_node("n1", "sid", "YWFh")

    rt.get_session.assert_not_called()


@pytest.mark.asyncio
async def test_handle_audio_from_client_openai_starts_session_and_sends(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rs = MagicMock(connected=True)
    rs.send_audio = AsyncMock()
    rt = MagicMock(available=True)
    rt.get_session.return_value = None
    rt.start_session = AsyncMock(return_value=rs)

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_session_voice_mode("12345678-abcd", "realtime")

    await r.handle_audio_from_client("12345678-abcd", "QUJD")
    rt.start_session.assert_awaited_once()
    assert rt.start_session.await_args.args == ("12345678-abcd", "webclient_12345678")
    assert rt.start_session.await_args.kwargs["input_sample_rate"] == 24000
    rs.send_audio.assert_awaited_once_with("QUJD")


@pytest.mark.asyncio
async def test_handle_audio_from_client_gemini_starts_session(monkeypatch):
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "gemini")
    gs = MagicMock(connected=True)
    gs.send_audio = AsyncMock()
    gem = MagicMock(available=True)
    gem.get_session.return_value = None
    gem.start_session = AsyncMock(return_value=gs)
    rt = MagicMock(available=True)

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.set_session_voice_mode("87654321-zzzz", "realtime")

    await r.handle_audio_from_client("87654321-zzzz", "Zm9v")
    gem.start_session.assert_awaited_once_with("87654321-zzzz", "webclient_87654321")
    gs.send_audio.assert_awaited_once_with("Zm9v")


@pytest.mark.asyncio
async def test_resolve_session_provider_whisper_when_realtime_but_openai_down(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rt = MagicMock(available=False)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_session_voice_mode("sess9999", "realtime")
    assert r._resolve_session_provider("sess9999") == "whisper"
    assert r.session_uses_realtime("sess9999") is False


@pytest.mark.asyncio
async def test_handle_whisper_path_orchestrator_and_tts(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    audio = MagicMock()
    audio.process_audio_chunk = AsyncMock(return_value="turn on lights")
    audio.synthesize_speech = AsyncMock(return_value=[{"pcm": "a"}])
    send_sess = AsyncMock()
    orch = MagicMock()
    orch.handle_command_stream = AsyncMock()
    mem = MagicMock()
    mem.working_get.return_value = [{"role": "assistant", "text": "done"}]

    r = VoiceRouter(
        realtime_proxy=MagicMock(available=False),
        audio_pipeline=audio,
        orchestrator=orch,
        memory=mem,
        send_to_session=send_sess,
    )
    r.set_session_voice_mode("abcd1234-eeee", "whisper")

    await r.handle_audio_from_client("abcd1234-eeee", "eHh4")

    orch.handle_command_stream.assert_awaited_once()
    audio.synthesize_speech.assert_awaited_once_with("done")
    assert send_sess.await_count >= 2


@pytest.mark.asyncio
async def test_handle_text_from_client_voice_gemini_connected(monkeypatch):
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "gemini")
    gs = MagicMock(connected=True)
    gs.send_text = AsyncMock()
    gem = MagicMock(available=True)
    gem.get_session.return_value = gs

    r = VoiceRouter(realtime_proxy=MagicMock(available=True), audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.set_session_voice_mode("11111111-2222", "realtime")

    await r.handle_text_from_client_voice("11111111-2222", "hello")
    gs.send_text.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_handle_text_from_client_voice_openai_connected(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rs = MagicMock(connected=True)
    rs.send_text = AsyncMock()
    rt = MagicMock(available=True)
    rt.get_session.return_value = rs

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_session_voice_mode("aaaaaaaa-bbbb", "realtime")

    await r.handle_text_from_client_voice("aaaaaaaa-bbbb", "hi")
    rt.get_session.assert_called_with("webclient_aaaaaaaa")
    rs.send_text.assert_awaited_once_with("hi")


@pytest.mark.asyncio
async def test_handle_text_from_client_voice_falls_back_to_orchestrator(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    orch = MagicMock()
    orch.handle_command_stream = AsyncMock()
    rt = MagicMock(available=False)

    r = VoiceRouter(
        realtime_proxy=rt,
        audio_pipeline=MagicMock(),
        orchestrator=orch,
    )
    r.set_session_voice_mode("bbbbbbbb-cccc", "realtime")

    await r.handle_text_from_client_voice("bbbbbbbb-cccc", "plain")
    orch.handle_command_stream.assert_awaited_once()
    assert orch.handle_command_stream.await_args.kwargs["context"]["source"] == "voice_text"


@pytest.mark.asyncio
async def test_handle_audio_for_gemini_start_session_and_send(monkeypatch):
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "gemini")
    gs = MagicMock(connected=True)
    gs.send_audio = AsyncMock()
    gem = MagicMock(available=True)
    gem.get_session.return_value = None
    gem.start_session = AsyncMock(return_value=gs)

    r = VoiceRouter(realtime_proxy=MagicMock(), audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)

    await r.handle_audio_for_gemini("full-session-id", "YmFy", node_id="node-x")
    gem.start_session.assert_awaited_once_with("full-session-id", "node-x")
    gs.send_audio.assert_awaited_once_with("YmFy")


def test_get_last_assistant_text_empty_when_no_assistant():
    mem = MagicMock()
    mem.working_get.return_value = [{"role": "user", "text": "u"}]
    r = VoiceRouter(memory=mem, audio_pipeline=MagicMock())
    assert r._get_last_assistant_text("sid") == ""


@pytest.mark.asyncio
async def test_handle_text_from_node_gemini_sends_text(monkeypatch):
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "gemini")
    gs = MagicMock(connected=True)
    gs.send_text = AsyncMock()
    gem = MagicMock(available=True)
    gem.get_session.return_value = gs

    r = VoiceRouter(realtime_proxy=MagicMock(available=True), audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.register_voice_config("n1", {"voice_provider": "gemini"})

    await r.handle_text_from_node("n1", "sid", "hello gemini")
    gs.send_text.assert_awaited_once_with("hello gemini")


@pytest.mark.asyncio
async def test_handle_text_from_node_openai_connected(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rs = MagicMock(connected=True)
    rs.send_text = AsyncMock()
    rt = MagicMock(available=True)
    rt.get_session.return_value = rs

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.register_voice_config("n1", {"voice_provider": "openai"})

    await r.handle_text_from_node("n1", "sid", "hi openai")
    rs.send_text.assert_awaited_once_with("hi openai")


@pytest.mark.asyncio
async def test_handle_audio_from_node_openai_no_send_when_not_connected(monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    rs = MagicMock(connected=False)
    rs.send_audio = AsyncMock()
    rt = MagicMock(available=True)
    rt.get_session.return_value = rs

    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.register_voice_config("n1", {"voice_provider": "openai"})

    await r.handle_audio_from_node("n1", "sid", "YWFh")
    rs.send_audio.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# RealtimeProxy / RealtimeSession — deeper message & state coverage
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_realtime_session_send_text_and_cancel_when_connected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    sent: list[str] = []

    async def fake_send(event: dict):
        sent.append(json.dumps(event))

    rs = RealtimeSession("sid", "nid", api_key="k")
    rs._connected = True
    rs._ws = MagicMock()
    rs._send = fake_send
    # cancel_response now guards on _response_in_progress (v2026.5.9
    # fix for the "Cancellation failed: no active response" spam
    # that prevented GA Realtime from ever producing audio in the
    # live test). Seed the flag so the guard lets the cancel through.
    rs._response_in_progress = True

    await rs.send_text("hello")
    await rs.cancel_response()
    assert any("conversation.item.create" in s for s in sent)
    assert any("response.cancel" in s for s in sent)


@pytest.mark.asyncio
async def test_realtime_session_inject_context_skips_when_empty_or_disconnected():
    rs = RealtimeSession("sid", "nid", api_key="k")
    await rs.inject_context("")
    rs._connected = True
    await rs.inject_context("")
    assert rs._ws is None


@pytest.mark.asyncio
async def test_realtime_session_disconnect_clears_state(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    rs = RealtimeSession("sid", "nid", api_key="k")
    rs._connected = True
    rs._ws = MagicMock()
    rs._ws.close = AsyncMock()
    rs._recv_task = asyncio.create_task(asyncio.sleep(3600))
    await rs.disconnect()
    assert rs.connected is False


@pytest.mark.asyncio
async def test_handle_event_session_created_triggers_configure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    rs = RealtimeSession("sid", "nid", api_key="k")
    rs.configure = AsyncMock()
    await rs._handle_event({"type": "session.created"})
    rs.configure.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_event_audio_done_invokes_delta_with_done_flag():
    calls: list[tuple[str, str, bool]] = []

    async def on_delta(sid: str, b64: str, done: bool):
        calls.append((sid, b64, done))

    rs = RealtimeSession("sid", "nid", api_key="k", on_audio_delta=on_delta)
    await rs._handle_event({"type": "response.output_audio.done"})
    assert calls == [("sid", "", True)]


@pytest.mark.asyncio
async def test_handle_tool_call_json_decode_error_uses_empty_dict(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    ep = MagicMock(id="bash")
    skill = MagicMock()
    skill.endpoints = [ep]
    reg = MagicMock()
    reg.skills = {"s": skill}
    exec_mock = MagicMock()
    exec_mock.execute = AsyncMock(return_value={"data": {"ok": 1}})

    p = RealtimeProxy(skill_registry=reg, skill_executor=exec_mock)
    await p._handle_tool_call("sid", "c1", "s__bash", "not-json{{{")
    exec_mock.execute.assert_awaited()


@pytest.mark.asyncio
async def test_handle_tool_call_endpoint_not_found(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    skill = MagicMock()
    skill.endpoints = []
    reg = MagicMock()
    reg.skills = {"computer_use": skill}
    p = RealtimeProxy(skill_registry=reg, skill_executor=MagicMock())
    out = await p._handle_tool_call("sid", "c1", "computer_use__missing", "{}")
    assert "Endpoint not found" in out


def test_tool_feedback_text_branches():
    assert "Searching" in RealtimeProxy._tool_feedback_text("web_search__q")
    assert "weather" in RealtimeProxy._tool_feedback_text("weather_current__x").lower()
    assert "browser" in RealtimeProxy._tool_feedback_text("browser__navigate").lower()
    assert "command" in RealtimeProxy._tool_feedback_text("computer_use__bash").lower()
    assert "Running" in RealtimeProxy._tool_feedback_text("other_skill__do_thing")


@pytest.mark.asyncio
async def test_realtime_proxy_update_context_injects_when_perception_has_frame(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    async def fake_connect(self):
        self._connected = True
        self._ws = MagicMock()
        self._ws.send = AsyncMock()

    frame = MagicMock()
    frame.to_system_context.return_value = "ctx line"
    perc = MagicMock()
    perc.get_frame.return_value = frame

    with patch.object(RealtimeSession, "connect", fake_connect):
        p = RealtimeProxy(perception=perc)
        await p.start_session("sess-u", "node-u")
        rs = p.get_session("node-u")
        rs.inject_context = AsyncMock()
        await p.update_context("node-u", "sess-u")
        rs.inject_context.assert_awaited_once()
        arg = rs.inject_context.await_args[0][0]
        assert "ctx line" in arg
        await p.stop_session("sess-u")


@pytest.mark.asyncio
async def test_realtime_proxy_relay_text_when_connected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    async def fake_connect(self):
        self._connected = True
        self._ws = MagicMock()
        self._ws.send = AsyncMock()

    with patch.object(RealtimeSession, "connect", fake_connect):
        p = RealtimeProxy()
        await p.start_session("s1", "n1")
        rs = p.get_session("n1")
        rs.send_text = AsyncMock()
        await p.relay_text("n1", "x")
        rs.send_text.assert_awaited_once_with("x")
        await p.stop_session("s1")


@pytest.mark.asyncio
async def test_handle_transcript_forwards_web_or_node_not_both(monkeypatch):
    """Routing contract (operator report 2026-05-09 — chat bubbles
    rendered each voice turn TWICE because the prior implementation
    fired both `_send_to_session` AND `_send_to_node` for the same
    iPhone session). The contract is now web-OR-node, mirroring
    `_handle_audio_delta`. This test pins both halves: a daemon node
    gets ONE node emit and ZERO session emits; the web client case
    is covered by `test_voice_transcript_role_wire.py`.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    send_sess = AsyncMock()
    send_node = AsyncMock()
    mem = MagicMock()

    p = RealtimeProxy(
        memory=mem,
        send_to_session=send_sess,
        send_to_node=send_node,
    )
    # phone-1 is a daemon node (NOT a webclient_) — must route via
    # send_to_node only.
    p._sessions["sess-x"] = MagicMock(node_id="phone-1")

    await p._handle_transcript("sess-x", "final text", True)
    mem.working_push.assert_called()
    send_sess.assert_not_awaited()  # daemon node MUST NOT touch the session path
    send_node.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_tool_feedback_to_webclient_session(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    send_sess = AsyncMock()
    p = RealtimeProxy(send_to_session=send_sess)
    p._sessions["s-web"] = MagicMock(node_id="webclient_abcdef12")

    await p._send_tool_feedback("s-web", "progress")
    send_sess.assert_awaited()


@pytest.mark.asyncio
async def test_realtime_session_send_ws_error_marks_disconnected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    rs = RealtimeSession("sid", "nid", api_key="k")
    rs._connected = True
    rs._ws = MagicMock()
    rs._ws.send = AsyncMock(side_effect=RuntimeError("broken"))

    await rs._send({"type": "ping"})
    assert rs.connected is False


@pytest.mark.asyncio
async def test_handle_event_transcript_deltas(monkeypatch):
    calls: list[tuple[str, str, bool]] = []

    async def on_tr(sid: str, text: str, final: bool):
        calls.append((sid, text, final))

    rs = RealtimeSession("sid", "nid", api_key="k", on_transcript=on_tr)
    await rs._handle_event({"type": "response.output_audio_transcript.delta", "delta": "partial"})
    await rs._handle_event({
        "type": "response.output_audio_transcript.done",
        "transcript": "full",
    })
    assert ("sid", "partial", False) in calls
    assert ("sid", "full", True) in calls


@pytest.mark.asyncio
async def test_handle_event_input_transcription_completed(monkeypatch):
    calls: list[str] = []

    async def on_tr(sid: str, text: str, final: bool):
        calls.append(text)

    rs = RealtimeSession("sid", "nid", api_key="k", on_transcript=on_tr)
    await rs._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "user spoke",
    })
    assert any("[user]" in c for c in calls)


@pytest.mark.asyncio
async def test_handle_event_speech_started_invokes_callback():
    started: list[str] = []

    async def on_sp(sid: str):
        started.append(sid)

    rs = RealtimeSession("sid", "nid", api_key="k", on_speech_started=on_sp)
    await rs._handle_event({"type": "input_audio_buffer.speech_started"})
    assert started == ["sid"]


# ----------------------------------------------------------------------
# audio.realtime_primary — the wizard's realtime pick reaching the router
# ----------------------------------------------------------------------
#
# ``feral setup``'s voice preflight has always written
# ``audio.realtime_primary``, but nothing read it: the router only ever
# consulted FERAL_VOICE_PROVIDER. An operator who picked Gemini Live in
# the wizard still got OpenAI Realtime on every call.


def _write_audio_settings(tmp_path, monkeypatch, audio: dict) -> None:
    import json

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"audio": audio}))


def test_preferred_realtime_provider_reads_settings(tmp_path, monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(tmp_path, monkeypatch, {"realtime_primary": "gemini_live"})
    assert VoiceRouter._preferred_realtime_provider() == "gemini"


def test_preferred_realtime_provider_env_wins(tmp_path, monkeypatch):
    _write_audio_settings(tmp_path, monkeypatch, {"realtime_primary": "gemini_live"})
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "openai")
    assert VoiceRouter._preferred_realtime_provider() == "openai"


def test_preferred_realtime_provider_empty_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(tmp_path, monkeypatch, {})
    assert VoiceRouter._preferred_realtime_provider() == ""


def test_session_routes_to_gemini_from_settings(tmp_path, monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(tmp_path, monkeypatch, {"realtime_primary": "gemini_live"})
    gem = MagicMock(available=True)
    rt = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(gem)
    r.set_session_voice_mode("sess-cfg", "realtime")
    assert r._resolve_session_provider("sess-cfg") == "gemini"


def test_settings_pick_does_not_override_an_unavailable_proxy(tmp_path, monkeypatch):
    """Picking Gemini in the wizard must not route to a proxy that
    isn't up — the router still falls back to OpenAI Realtime."""
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(tmp_path, monkeypatch, {"realtime_primary": "gemini_live"})
    rt = MagicMock(available=True)
    r = VoiceRouter(realtime_proxy=rt, audio_pipeline=MagicMock())
    r.set_gemini_proxy(MagicMock(available=False))
    r.set_session_voice_mode("sess-down", "realtime")
    assert r._resolve_session_provider("sess-down") == "openai"
