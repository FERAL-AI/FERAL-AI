"""
Deeper channel tests — Telegram polling/callbacks, Discord/Slack WebSocket paths,
WhatsApp webhooks, and ChannelManager orchestration (fully mocked I/O).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.base import (
    ChannelManager,
    ChannelMessage,
    ChannelResponse,
    DiscordChannel,
    SlackChannel,
    TelegramChannel,
    WhatsAppChannel,
)

pytestmark = pytest.mark.no_auto_feral_home


# ── Telegram — httpx mocks ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_callback_query_invokes_handler_and_answers_callback():
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    mock_http.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))

    handler = AsyncMock(return_value=ChannelResponse(text="picked"))
    with patch("httpx.AsyncClient", return_value=mock_http):
        ch = TelegramChannel(
            {"bot_token": "tok", "enabled": True, "allowed_senders": ["99"]},
        )
        ch.set_handler(handler)
        await ch.start()

        cb = {
            "id": "cbq-1",
            "data": "action:ok",
            "from": {"id": 99},
            "message": {"chat": {"id": 555}},
        }
        await ch._handle_callback(cb)
        await ch.stop()

    handler.assert_awaited_once()
    cm = handler.await_args.args[0]
    assert isinstance(cm, ChannelMessage)
    assert cm.metadata.get("callback") is True
    assert cm.text == "action:ok"
    answer_calls = [c for c in mock_http.post.call_args_list if "answerCallbackQuery" in str(c)]
    assert answer_calls


@pytest.mark.asyncio
async def test_telegram_photo_message_downloads_via_httpx_and_passes_image_b64():
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    file_get = MagicMock()
    file_get.json = lambda: {"result": {"file_path": "photos/x.jpg"}}
    bin_get = MagicMock()
    bin_get.content = b"\xff\xd8\xff"
    mock_http.get = AsyncMock(side_effect=[file_get, bin_get])

    handler = AsyncMock(return_value=ChannelResponse(text="got pic"))
    ch = TelegramChannel(
        {"bot_token": "tok", "enabled": True, "allowed_senders": ["2"]},
    )
    ch._http = mock_http
    ch._base_url = "https://api.telegram.org/bot/tok"
    ch.set_handler(handler)

    msg = {
        "chat": {"id": 1},
        "from": {"id": 2, "username": "cam"},
        "photo": [{"file_id": "ignored"}, {"file_id": "fid-final"}],
    }
    await ch._handle_message(msg)

    handler.assert_awaited_once()
    out = handler.await_args.args[0]
    assert out.image_b64
    assert mock_http.get.await_count >= 2


@pytest.mark.asyncio
async def test_telegram_poll_loop_one_update_then_stops():
    update = {
        "update_id": 10,
        "message": {
            "chat": {"id": 77},
            "from": {"id": 3, "first_name": "Ann"},
            "text": "poll hi",
        },
    }
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    ch_box: list[TelegramChannel | None] = [None]
    poll_n = [0]

    async def get_side_effect(url, *args, **kwargs):
        # start() calls /getMe first; route it through a generic 200/ok
        # response so the poll loop actually starts.
        if "getMe" in url:
            resp = MagicMock(status_code=200)
            resp.json = lambda: {"ok": True, "result": {"username": "feral_test_bot"}}
            return resp
        # Subsequent calls are /getUpdates — feed the single update, then
        # flip _running=False to exit the loop cleanly on the next poll.
        poll_n[0] += 1
        if poll_n[0] == 1:
            return MagicMock(json=lambda: {"result": [update]})
        if ch_box[0] is not None:
            ch_box[0]._running = False
        return MagicMock(json=lambda: {"result": []})

    mock_http.get = AsyncMock(side_effect=get_side_effect)
    handler = AsyncMock(return_value=ChannelResponse(text="r"))

    with patch("httpx.AsyncClient", return_value=mock_http):
        ch = TelegramChannel(
            {"bot_token": "tok", "enabled": True, "allowed_senders": ["3"]},
        )
        ch_box[0] = ch
        ch.set_handler(handler)
        await ch.start()
        # Give the poll loop time to fire at least once on slow CI runners.
        for _ in range(20):
            if handler.call_count:
                break
            await asyncio.sleep(0.05)
        await ch.stop()

    handler.assert_called()


# ── Discord — websockets + httpx ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discord_gateway_connect_delivers_message_create_to_handler():
    hello_raw = json.dumps({"op": 10, "d": {"heartbeat_interval": 999999000}})
    msg_raw = json.dumps(
        {
            "t": "MESSAGE_CREATE",
            "d": {
                "channel_id": "CH-1",
                "content": "gateway hi",
                "author": {"id": "a1", "username": "bob", "bot": False},
            },
        }
    )

    class FakeWS:
        def __init__(self):
            self.send = AsyncMock()

        async def recv(self):
            return hello_raw

        def __aiter__(self):
            self._done = False
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return msg_raw

    fake_ws = FakeWS()

    @asynccontextmanager
    async def fake_connect(*_a, **_kw):
        yield fake_ws

    gateway_resp = MagicMock()
    gateway_resp.json = lambda: {"url": "wss://discord.test/gateway"}

    mock_http = MagicMock()
    mock_http.get = AsyncMock(
        side_effect=[
            gateway_resp,
        ]
    )
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()

    handler = AsyncMock(return_value=ChannelResponse(text="discord reply"))

    with patch("httpx.AsyncClient", return_value=mock_http), patch(
        "websockets.connect", fake_connect,
    ):
        ch = DiscordChannel(
            {
                "bot_token": "DISC.TOKEN",
                "enabled": True,
                "allowed_senders": ["a1"],
            },
        )
        ch.set_handler(handler)
        await ch.start()
        await asyncio.sleep(0.15)
        await ch.stop()

    handler.assert_called()
    args0 = handler.call_args[0][0]
    assert isinstance(args0, ChannelMessage)
    assert args0.channel_id == "CH-1"
    assert args0.text == "gateway hi"


@pytest.mark.asyncio
async def test_discord_handle_message_skips_bot_authors():
    ch = DiscordChannel({"bot_token": "t"})
    handler = AsyncMock(return_value=ChannelResponse(text="x"))
    ch.set_handler(handler)
    await ch._handle_discord_message(
        {
            "channel_id": "c",
            "author": {"id": "1", "username": "botty", "bot": True},
            "content": "ignored",
        },
    )
    handler.assert_not_called()


# ── Slack — Socket Mode websockets ──────────────────────────────────────────


class _FakeSlackWS:
    """Fake WebSocketClientProtocol for Slack Socket Mode tests.

    Yields one canned frame, then raises ``StopAsyncIteration`` so the
    ``async for raw in ws:`` loop in
    ``SlackChannel._socket_mode`` exits cleanly. Tracks ``send`` calls
    for ack assertions.
    """

    def __init__(self, raw):
        self._raw = raw
        self.send = AsyncMock()

    def __aiter__(self):
        self._gone = False
        return self

    async def __anext__(self):
        if self._gone:
            raise StopAsyncIteration
        self._gone = True
        return self._raw


@pytest.mark.asyncio
async def test_slack_socket_mode_events_api_acknowledges_and_handles_message():
    """Pins the v13-compatible ``async with websockets.connect(...) as ws:``
    pattern in ``SlackChannel._socket_mode``. The previous test used
    ``AsyncMock(return_value=fake_ws)`` which only ever exercised the
    historical (broken) ``await connect(...)`` form — masking the
    ``TypeError`` users hit in production with ``websockets>=11``.
    """
    ev = {
        "type": "events_api",
        "envelope_id": "env-42",
        "payload": {
            "event": {
                "type": "message",
                "channel": "C900",
                "user": "U12",
                "text": "socket hi",
            },
        },
    }
    raw = json.dumps(ev)
    fake_ws = _FakeSlackWS(raw)

    @asynccontextmanager
    async def fake_connect(*_a, **_kw):
        yield fake_ws

    socket_client = MagicMock()
    socket_client.post = AsyncMock(
        return_value=MagicMock(json=lambda: {"url": "wss://slack.test/socket"}),
    )
    socket_cm = MagicMock()
    socket_cm.__aenter__ = AsyncMock(return_value=socket_client)
    socket_cm.__aexit__ = AsyncMock(return_value=None)

    main_http = MagicMock()
    main_http.post = AsyncMock()
    main_http.aclose = AsyncMock()

    handler = AsyncMock(return_value=ChannelResponse(text="slack out"))

    with patch("httpx.AsyncClient", side_effect=[main_http, socket_cm]), patch(
        "websockets.connect", fake_connect,
    ):
        ch = SlackChannel(
            {
                "bot_token": "xoxb-test",
                "app_token": "xapp-test",
                "enabled": True,
                "allowed_senders": ["U12"],
            },
        )
        ch.set_handler(handler)
        await ch.start()
        await asyncio.sleep(0.12)
        ch._running = False
        await ch.stop()

    handler.assert_called()
    cm = handler.call_args[0][0]
    assert cm.channel_id == "C900"
    assert cm.text == "socket hi"
    fake_ws.send.assert_called()
    ack = json.loads(fake_ws.send.call_args[0][0])
    assert ack.get("envelope_id") == "env-42"


@pytest.mark.asyncio
async def test_slack_socket_mode_uses_async_with_connect_directly():
    """Regression pin: ``websockets.connect`` MUST be used as an async
    context manager directly (no ``await`` first). If a future change
    re-introduces ``ws = await websockets.connect(...)`` followed by
    ``async with ws as conn:`` this test will fail because the
    ``@asynccontextmanager`` factory returns an ``_AsyncGeneratorContextManager``
    that is NOT awaitable.
    """
    raw = json.dumps({
        "type": "events_api",
        "envelope_id": "env-1",
        "payload": {"event": {"type": "message", "channel": "C1", "user": "U1", "text": "hi"}},
    })
    fake_ws = _FakeSlackWS(raw)

    awaited = {"on_connect": False}

    @asynccontextmanager
    async def strict_connect(*_a, **_kw):
        # If anyone tries ``await websockets.connect(...)`` instead of
        # ``async with websockets.connect(...) as ws:``, the awaited
        # result will be this generator-context-manager — which is NOT
        # awaitable, and Python will raise TypeError BEFORE entering
        # the body. The test would then fail.
        awaited["on_connect"] = True
        yield fake_ws

    socket_client = MagicMock()
    socket_client.post = AsyncMock(
        return_value=MagicMock(json=lambda: {"url": "wss://slack.test/socket"}),
    )
    socket_cm = MagicMock()
    socket_cm.__aenter__ = AsyncMock(return_value=socket_client)
    socket_cm.__aexit__ = AsyncMock(return_value=None)

    main_http = MagicMock()
    main_http.post = AsyncMock()
    main_http.aclose = AsyncMock()

    handler = AsyncMock(return_value=ChannelResponse(text="ok"))

    with patch("httpx.AsyncClient", side_effect=[main_http, socket_cm]), patch(
        "websockets.connect", strict_connect,
    ):
        ch = SlackChannel(
            {
                "bot_token": "xoxb-test",
                "app_token": "xapp-test",
                "enabled": True,
                "allowed_senders": ["U1"],
            },
        )
        ch.set_handler(handler)
        await ch.start()
        await asyncio.sleep(0.12)
        ch._running = False
        await ch.stop()

    assert awaited["on_connect"], "Slack socket mode never entered the connect context"
    handler.assert_called()


# ── WhatsApp ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whatsapp_handle_webhook_skips_when_no_text_even_if_handler_set():
    ch = WhatsAppChannel({"access_token": "t", "phone_number_id": "p", "enabled": True})
    ch._running = True
    ch._phone_id = "p"
    ch._http = MagicMock()
    ch._http.post = AsyncMock()
    h = AsyncMock(return_value=ChannelResponse(text="nope"))
    ch.set_handler(h)
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": "+1", "text": {}},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    assert await ch.handle_webhook(body) is None
    h.assert_not_called()


@pytest.mark.asyncio
async def test_whatsapp_handle_webhook_returns_first_response_when_multiple_messages():
    ch = WhatsAppChannel(
        {
            "access_token": "t",
            "phone_number_id": "p",
            "enabled": True,
            "allowed_senders": ["111", "222"],
        },
    )
    ch._running = True
    ch._phone_id = "p"
    ch._http = MagicMock()
    ch._http.post = AsyncMock()
    calls = []

    async def counting_handler(msg: ChannelMessage):
        calls.append(msg.text)
        return ChannelResponse(text=f"ack-{len(calls)}")

    ch.set_handler(counting_handler)
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": "111", "text": {"body": "first"}},
                                {"from": "222", "text": {"body": "second"}},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    out = await ch.handle_webhook(body)
    assert out is not None
    assert out.text == "ack-1"
    assert calls == ["first"]


# ── ChannelManager ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_manager_set_handler_before_start_propagates():
    mgr = ChannelManager()
    h = AsyncMock(return_value=ChannelResponse(text="early"))
    mgr.set_handler(h)
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    with patch("httpx.AsyncClient", return_value=mock_http):
        await mgr.start_channel("telegram", {"bot_token": "t"})
    tg = mgr.get_channel("telegram")
    assert tg is not None
    assert tg._handler is h
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_channel_manager_stats_reflects_multiple_channels():
    mgr = ChannelManager()
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    with patch("httpx.AsyncClient", return_value=mock_http):
        await mgr.start_channel("telegram", {"bot_token": "a"})
    wa = WhatsAppChannel({"access_token": "x", "phone_number_id": "y", "enabled": True})
    wa._running = True
    wa._known_chat_ids.add("p1")
    mgr._channels["whatsapp"] = wa

    s = mgr.stats
    assert s["channel_count"] == 2
    assert set(s["active_channels"]) == {"telegram", "whatsapp"}
    assert s["details"]["telegram"]["running"] is True
    assert s["details"]["whatsapp"]["known_chats"] == 1
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_channel_manager_broadcast_and_send_across_mixed_channels():
    mgr = ChannelManager()
    mock_t = MagicMock()
    mock_t.post = AsyncMock()
    mock_t.aclose = AsyncMock(return_value=None)
    mock_t.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))

    tg = TelegramChannel({"bot_token": "z"})
    tg._http = mock_t
    tg._base_url = "https://api.telegram.org/bot/z"
    tg._running = True
    tg._known_chat_ids.add("10")

    mock_s = MagicMock()
    mock_s.post = AsyncMock()
    mock_s.aclose = AsyncMock(return_value=None)
    slack = SlackChannel({"bot_token": "xoxb"})
    slack._http = mock_s
    slack._running = True
    slack._known_chat_ids.add("C55")

    mgr._channels["telegram"] = tg
    mgr._channels["slack"] = slack

    await mgr.broadcast(ChannelResponse(text="all hands"))
    assert mock_t.post.called
    assert mock_s.post.called

    mock_t.post.reset_mock()
    await mgr.send_to_channel("telegram", "10", ChannelResponse(text="dm"))
    mock_t.post.assert_called_once()
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_channel_manager_set_handler_updates_all_existing_channels():
    mgr = ChannelManager()
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    h1 = AsyncMock(return_value=ChannelResponse(text="1"))
    h2 = AsyncMock(return_value=ChannelResponse(text="2"))
    with patch("httpx.AsyncClient", return_value=mock_http):
        await mgr.start_channel("telegram", {"bot_token": "t"})
        mgr.set_handler(h1)
        assert mgr.get_channel("telegram")._handler is h1
        mgr.set_handler(h2)
    assert mgr.get_channel("telegram")._handler is h2
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_channel_manager_stop_all_clears_channels():
    mgr = ChannelManager()
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    with patch("httpx.AsyncClient", return_value=mock_http):
        await mgr.start_channel("telegram", {"bot_token": "t"})
    await mgr.stop_all()
    assert mgr.active_channels == []
    assert mgr.stats["channel_count"] == 0


def test_channel_runtime_failure_fuse_disables_broken_instance():
    ch = TelegramChannel({"bot_token": "tok"})
    ch._running = True
    ch._runtime_failure_fuse = 2

    ch._record_runtime_failure("unit-test", "boom-1")
    assert ch._running is True
    assert ch._degraded is False

    ch._record_runtime_failure("unit-test", "boom-2")
    assert ch._running is False
    assert ch._degraded is True
    assert "unit-test" in ch._degraded_reason


@pytest.mark.asyncio
async def test_channel_manager_does_not_register_degraded_channel():
    mgr = ChannelManager()

    class _BrokenChannel:
        def __init__(self, config: dict):
            self.config = config
            self._running = False
            self._connected = False
            self._degraded = True
            self._degraded_reason = "startup fuse open"
            self._known_chat_ids = set()

        def set_handler(self, handler):
            self._handler = handler

        async def start(self):
            return None

        async def stop(self):
            return None

        async def send(self, channel_id: str, response: ChannelResponse):
            return None

        @property
        def channel_type(self) -> str:
            return "broken_unit"

    mgr.CHANNEL_TYPES["broken_unit"] = _BrokenChannel
    try:
        await mgr.start_channel("broken_unit", {"enabled": True})
        assert mgr.get_channel("broken_unit") is None
        assert "broken_unit" not in mgr.active_channels
    finally:
        mgr.CHANNEL_TYPES.pop("broken_unit", None)


# ── A3 — Wave 2 hardening regressions ───────────────────────────────────────


def _make_resp(status_code=200, json_body=None, text_body=None, content_type="application/json"):
    """Shape a ``MagicMock`` that looks enough like an ``httpx.Response``.

    ``resp.json()`` will either return ``json_body`` or raise a JSON
    decode error if ``json_body`` is ``None`` and ``text_body`` is set
    (mimicking httpx's real ``JSONDecodeError`` on HTML/empty bodies).
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}

    def _json():
        if json_body is not None:
            return json_body
        raise ValueError("Expecting value: line 1 column 1 (char 0)")
    resp.json = _json
    resp.text = text_body if text_body is not None else ""
    return resp


@pytest.mark.asyncio
async def test_telegram_start_strips_whitespace_from_token():
    """Tokens pasted from clipboard managers frequently carry a trailing
    newline. Before A3, this landed in the URL path segment and turned
    every ``getUpdates`` into an HTML 404 from Telegram's frontend.
    """
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    mock_http.get = AsyncMock(
        return_value=_make_resp(
            200, {"ok": True, "result": {"username": "stripbot"}}
        )
    )

    with patch("httpx.AsyncClient", return_value=mock_http):
        ch = TelegramChannel({"bot_token": "  123:ABC\n ", "enabled": True})
        await ch.start()
        ch._running = False
        await ch.stop()

    assert ch._base_url == "https://api.telegram.org/bot123:ABC"
    # getMe is the first call; the URL must use the stripped token.
    first_call = mock_http.get.await_args_list[0]
    assert "bot123:ABC/getMe" in first_call.args[0]


async def _run_poll_loop_once(ch: TelegramChannel, expected_gets: int = 1):
    """Run ``_poll_loop`` until the mocked ``_http.get`` has been awaited
    ``expected_gets`` times, then cancel the task. Avoids patching
    ``asyncio.sleep`` (which mutates the global module because
    ``channels.base.asyncio`` IS the real asyncio module)."""
    task = asyncio.create_task(ch._poll_loop())
    for _ in range(60):
        await asyncio.sleep(0.01)
        if ch._http.get.await_count >= expected_gets:
            break
    ch._running = False
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_telegram_poll_loop_survives_empty_body(caplog):
    """200-with-empty-body should log 'non-JSON body' and back off,
    NOT raise ``JSONDecodeError`` out of the task.
    """
    bad = _make_resp(200, json_body=None, text_body="", content_type="text/html")

    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    mock_http.get = AsyncMock(return_value=bad)

    ch = TelegramChannel({"bot_token": "t"})
    ch._http = mock_http
    ch._base_url = "https://api.telegram.org/bott"
    ch._running = True
    ch._offset = 0

    caplog.set_level("WARNING", logger="feral.channels")
    await _run_poll_loop_once(ch, expected_gets=1)

    # No uncaught JSONDecodeError — the loop logged and continued.
    assert any("non-JSON body" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_telegram_poll_loop_survives_html_200(caplog):
    html = _make_resp(
        200, json_body=None, text_body="<html>oops</html>", content_type="text/html"
    )
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    mock_http.get = AsyncMock(return_value=html)

    ch = TelegramChannel({"bot_token": "t"})
    ch._http = mock_http
    ch._base_url = "https://api.telegram.org/bott"
    ch._running = True
    ch._offset = 0

    caplog.set_level("WARNING", logger="feral.channels")
    await _run_poll_loop_once(ch, expected_gets=1)
    assert any("non-JSON body" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_telegram_poll_loop_backs_off_on_http_error(caplog):
    bad = _make_resp(502, json_body=None, text_body="bad gateway", content_type="text/plain")
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    mock_http.get = AsyncMock(return_value=bad)

    ch = TelegramChannel({"bot_token": "t"})
    ch._http = mock_http
    ch._base_url = "https://api.telegram.org/bott"
    ch._running = True
    ch._offset = 0

    caplog.set_level("WARNING", logger="feral.channels")
    await _run_poll_loop_once(ch, expected_gets=1)
    assert any("HTTP 502" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_telegram_start_skips_poll_loop_on_unauthorized():
    """If ``getMe`` returns 401+ok=false, the token is bad. Starting the
    poll loop anyway just spams the logs forever — so we don't.
    """
    unauth = _make_resp(
        401,
        {"ok": False, "error_code": 401, "description": "Unauthorized"},
    )
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    mock_http.get = AsyncMock(return_value=unauth)

    with patch("httpx.AsyncClient", return_value=mock_http):
        ch = TelegramChannel({"bot_token": "bad-token"})
        await ch.start()

    assert ch._connected is False
    assert ch._running is False
    # Only the one getMe call should have happened — no poll loop.
    assert mock_http.get.await_count == 1
    # aclose() should have been invoked exactly once in start()'s cleanup.
    assert mock_http.aclose.await_count == 1


@pytest.mark.asyncio
async def test_channel_manager_start_twice_stops_first_instance():
    """Regression for duplicate Telegram poll loops. A second
    ``start_channel('telegram', ...)`` must ``stop()`` the first one.
    """
    mock_http_a = MagicMock()
    mock_http_a.post = AsyncMock()
    mock_http_a.aclose = AsyncMock()
    mock_http_a.get = AsyncMock(
        return_value=_make_resp(200, {"ok": True, "result": {"username": "a"}})
    )
    mock_http_b = MagicMock()
    mock_http_b.post = AsyncMock()
    mock_http_b.aclose = AsyncMock()
    mock_http_b.get = AsyncMock(
        return_value=_make_resp(200, {"ok": True, "result": {"username": "b"}})
    )

    mgr = ChannelManager()

    with patch("httpx.AsyncClient", side_effect=[mock_http_a, mock_http_b]):
        await mgr.start_channel("telegram", {"bot_token": "first"})
        first = mgr.get_channel("telegram")
        assert first is not None
        assert first._running is True

        await mgr.start_channel("telegram", {"bot_token": "second"})
        second = mgr.get_channel("telegram")

    assert second is not first
    # First instance must have been stopped and its httpx client closed.
    assert first._running is False
    assert mock_http_a.aclose.await_count >= 1
    # Second instance should be live.
    assert second._running is True
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_whatsapp_verify_prefers_feral_env(monkeypatch):
    """Meta verify GET must succeed when only ``FERAL_WHATSAPP_VERIFY_TOKEN``
    is set (the canonical FERAL_* name).
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.channels import router

    app = FastAPI()
    app.include_router(router)

    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    monkeypatch.setenv("FERAL_WHATSAPP_VERIFY_TOKEN", "canonical-secret")

    with TestClient(app) as client:
        r = client.get(
            "/api/channels/whatsapp/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "canonical-secret",
                "hub.challenge": "42",
            },
        )
    assert r.status_code == 200
    assert r.text == "42"


@pytest.mark.asyncio
async def test_whatsapp_verify_backcompat_old_env(monkeypatch):
    """Legacy deployments that only set the unprefixed name still work."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.channels import router

    app = FastAPI()
    app.include_router(router)

    monkeypatch.delenv("FERAL_WHATSAPP_VERIFY_TOKEN", raising=False)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "legacy-secret")

    with TestClient(app) as client:
        r = client.get(
            "/api/channels/whatsapp/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "legacy-secret",
                "hub.challenge": "ok",
            },
        )
    assert r.status_code == 200
    assert r.text == "ok"


def test_whatsapp_env_keys_are_accepted_by_credentials_whitelist():
    """``/api/config/credentials`` must accept the FERAL_WHATSAPP_* keys
    the rest of the system reads. Prior to A3 they were silently
    rejected because they don't match ``*_API_KEY``.
    """
    from api.routes.config import _is_accepted_env_key

    for key in (
        "FERAL_WHATSAPP_ACCESS_TOKEN",
        "FERAL_WHATSAPP_PHONE_NUMBER_ID",
        "FERAL_WHATSAPP_APP_SECRET",
        "FERAL_WHATSAPP_VERIFY_TOKEN",
    ):
        assert _is_accepted_env_key(key), f"{key} should be accepted"
