"""
Tests for Sprint 2B Glass Brain event emission — channel messages,
voice sessions, email received, device routing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_state(orchestrator=None, sessions=None):
    """Return a mock api.state.state with orchestrator and sessions."""
    st = MagicMock()
    st.orchestrator = orchestrator
    st.sessions = sessions or {"s1": MagicMock()}
    return st


STATE_PATH = "api.state.state"


# ── channel_message_in / channel_message_out ─────────────────────────────────


@pytest.mark.asyncio
async def test_channel_emit_comms_event_inbound():
    from channels.base import TelegramChannel

    orch = MagicMock()
    orch._emit_brain_event = AsyncMock()
    fake_state = _mock_state(orchestrator=orch)

    ch = TelegramChannel({"bot_token": "tok"})

    with patch(STATE_PATH, fake_state):
        await ch._emit_comms_event("in", "alice", "hello there")

    orch._emit_brain_event.assert_awaited_once()
    args = orch._emit_brain_event.await_args
    assert args[0][0] == "s1"
    assert args[0][1] == "channel_message_in"
    payload = args[0][2]
    assert payload["channel"] == "telegram"
    assert payload["direction"] == "in"
    assert payload["sender"] == "alice"
    assert payload["preview"] == "hello there"


@pytest.mark.asyncio
async def test_channel_emit_comms_event_outbound():
    from channels.base import SlackChannel

    orch = MagicMock()
    orch._emit_brain_event = AsyncMock()
    fake_state = _mock_state(orchestrator=orch)

    ch = SlackChannel({"bot_token": "x"})

    with patch(STATE_PATH, fake_state):
        await ch._emit_comms_event("out", "C123", "response text")

    orch._emit_brain_event.assert_awaited_once()
    args = orch._emit_brain_event.await_args
    assert args[0][1] == "channel_message_out"
    payload = args[0][2]
    assert payload["channel"] == "slack"
    assert payload["recipient"] == "C123"


@pytest.mark.asyncio
async def test_channel_emit_comms_event_preview_truncated():
    from channels.base import DiscordChannel

    orch = MagicMock()
    orch._emit_brain_event = AsyncMock()
    fake_state = _mock_state(orchestrator=orch)

    ch = DiscordChannel({"bot_token": "t"})
    long_text = "x" * 200

    with patch(STATE_PATH, fake_state):
        await ch._emit_comms_event("in", "user", long_text)

    payload = orch._emit_brain_event.await_args[0][2]
    assert len(payload["preview"]) == 100


@pytest.mark.asyncio
async def test_channel_emit_comms_event_no_orchestrator():
    from channels.base import TelegramChannel

    fake_state = _mock_state(orchestrator=None)
    ch = TelegramChannel({"bot_token": "t"})

    with patch(STATE_PATH, fake_state):
        await ch._emit_comms_event("in", "bob", "hi")


@pytest.mark.asyncio
async def test_telegram_handle_message_emits_inbound():
    from channels.base import TelegramChannel, ChannelResponse

    orch = MagicMock()
    orch._emit_brain_event = AsyncMock()
    fake_state = _mock_state(orchestrator=orch)

    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()

    ch = TelegramChannel({"bot_token": "tok"})
    ch._running = True
    ch._http = mock_http
    ch._base_url = "https://api.telegram.org/bottok"
    ch.set_handler(AsyncMock(return_value=ChannelResponse(text="ok")))

    msg = {"chat": {"id": 42}, "from": {"id": 7, "first_name": "Bob"}, "text": "hello"}

    with patch(STATE_PATH, fake_state):
        await ch._handle_message(msg)

    calls = orch._emit_brain_event.await_args_list
    event_types = [c[0][1] for c in calls]
    assert "channel_message_in" in event_types


# ── voice_session ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_voice_proxy_emits_voice_session_start():
    from voice.realtime_proxy import RealtimeProxy

    orch = MagicMock()
    orch._emit_brain_event = AsyncMock()
    fake_state = _mock_state(orchestrator=orch)

    proxy = RealtimeProxy.__new__(RealtimeProxy)
    proxy._sessions = {}
    proxy._node_to_session = {}
    proxy._api_key = "test-key"
    # GA migration (Subagent A) added self._voice as a default voice
    # threaded into each RealtimeSession; tests that bypass __init__
    # via __new__ must seed it explicitly.
    proxy._voice = "marin"
    proxy._voice_personality = MagicMock()
    proxy._voice_personality.current_time_of_day = MagicMock(return_value="morning")
    proxy._voice_personality.get_voice_instructions = MagicMock(return_value="test")
    proxy._memory = None
    proxy._perception = None
    proxy._skill_registry = None
    proxy._skill_executor = None
    proxy._send_to_node = None
    proxy._send_to_session = None

    mock_session = MagicMock()
    mock_session.connected = True
    mock_session._ws = True
    mock_session.connect = AsyncMock()
    mock_session.node_id = "phone_1"

    with patch("voice.realtime_proxy.RealtimeSession", return_value=mock_session), \
         patch(STATE_PATH, fake_state):
        result = await proxy.start_session("vs-1", "phone_1")

    assert result is not None
    calls = [c for c in orch._emit_brain_event.await_args_list if c[0][1] == "voice_session"]
    assert len(calls) == 1
    payload = calls[0][0][2]
    assert payload["active"] is True
    assert payload["provider"] == "openai"
    assert payload["session_id"] == "vs-1"


@pytest.mark.asyncio
async def test_openai_voice_proxy_emits_voice_session_stop():
    from voice.realtime_proxy import RealtimeProxy

    orch = MagicMock()
    orch._emit_brain_event = AsyncMock()
    fake_state = _mock_state(orchestrator=orch)

    proxy = RealtimeProxy.__new__(RealtimeProxy)
    proxy._sessions = {}
    proxy._node_to_session = {}

    mock_session = MagicMock()
    mock_session.node_id = "phone_1"
    mock_session.disconnect = AsyncMock()
    proxy._sessions["vs-1"] = mock_session
    proxy._node_to_session["phone_1"] = "vs-1"

    with patch(STATE_PATH, fake_state):
        await proxy.stop_session("vs-1")

    calls = [c for c in orch._emit_brain_event.await_args_list if c[0][1] == "voice_session"]
    assert len(calls) == 1
    assert calls[0][0][2]["active"] is False


# ── email_received ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_watcher_emits_email_received_for_vip():
    from integrations.email_watcher import EmailWatcher

    orch = MagicMock()
    orch._emit_brain_event = AsyncMock()
    fake_state = _mock_state(orchestrator=orch)

    watcher = EmailWatcher()
    watcher._vip_senders = ["ceo@company.com"]
    watcher._filter_subjects = []
    watcher._on_email = None
    watcher._processed_count = 0

    raw_email_bytes = (
        b"From: CEO <ceo@company.com>\r\n"
        b"Subject: Urgent\r\n"
        b"Date: Thu, 10 Apr 2026 12:00:00 +0000\r\n"
        b"Message-ID: <test-1@example.com>\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Important message body"
    )

    mock_mail = MagicMock()
    mock_mail.fetch = MagicMock(return_value=("OK", [(b"1", raw_email_bytes)]))
    watcher._mail = mock_mail

    # The loop the brain captured in start(). This test used to patch
    # ``email_watcher.asyncio`` wholesale so ``get_event_loop()`` handed
    # back a mock — which masked the RuntimeError the real worker thread
    # hit, and let the "drops every email" bug ship green. Set the same
    # attribute start() sets instead, so the hand-off path under test is
    # the production one.
    mock_loop = MagicMock()
    watcher._loop = mock_loop

    with patch(STATE_PATH, fake_state), \
         patch("integrations.email_watcher.asyncio.create_task", MagicMock()):
        watcher._process_message(b"1")

    assert watcher._processed_count == 1
    assert mock_loop.call_soon_threadsafe.called
    brain_calls = [
        c for c in mock_loop.call_soon_threadsafe.call_args_list
    ]
    assert len(brain_calls) >= 1


# ── device_route ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hardware_mesh_emits_device_route_on_invoke():
    from hardware.mesh import HardwareMesh
    from hardware.command_contract import CommandLedger, NodeHealth

    orch = MagicMock()
    orch._emit_brain_event = AsyncMock()
    fake_state = _mock_state(orchestrator=orch)

    mock_ws = MagicMock()
    mock_ws.send_json = AsyncMock()

    mock_registry = MagicMock()
    daemons = {"node-A": mock_ws}
    ledger = CommandLedger()
    health = NodeHealth()

    mesh = HardwareMesh(mock_registry, daemons, ledger, health)

    async def fake_wait_for(fut, timeout):
        mesh._pending_invokes.pop(list(mesh._pending_invokes.keys())[0], None)
        return {"success": True, "data": {"snapshot": "ok"}}

    with patch(STATE_PATH, fake_state), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await mesh.invoke("node-A", "camera.snap", {"resolution": "1080p"})

    brain_calls = [c for c in orch._emit_brain_event.await_args_list if c[0][1] == "device_route"]
    assert len(brain_calls) == 1
    payload = brain_calls[0][0][2]
    assert payload["from_node"] == "brain"
    assert payload["to_node"] == "node-A"
    assert payload["payload_kind"] == "camera.snap"


# ── Dashboard channels ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_includes_channels():
    from channels.base import ChannelManager, SlackChannel, TelegramChannel

    mgr = ChannelManager()
    slack = SlackChannel({"bot_token": "x", "enabled": True})
    slack._running = True
    mgr._channels["slack"] = slack
    tg = TelegramChannel({"bot_token": "t", "enabled": True})
    tg._running = True
    mgr._channels["telegram"] = tg

    assert hasattr(mgr, 'channels')
    channels = mgr.channels
    assert "slack" in channels
    assert "telegram" in channels

    channel_types = []
    for ch_id, ch in mgr.channels.items():
        if getattr(ch, 'enabled', False):
            channel_types.append({"type": ch_id, "connected": getattr(ch, '_running', False)})

    assert len(channel_types) == 2
    types = {c["type"] for c in channel_types}
    assert types == {"slack", "telegram"}
    assert all(c["connected"] for c in channel_types)
