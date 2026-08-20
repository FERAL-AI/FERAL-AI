"""
Tests for channels.base — ChannelMessage, ChannelResponse, concrete channels,
and ChannelManager (mocked HTTP; no real Telegram/Discord/Slack/WhatsApp APIs).
"""

from __future__ import annotations

import logging
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


# ── ChannelMessage ───────────────────────────────────────────────────────────


def test_channel_message_defaults():
    m = ChannelMessage(channel_type="x", channel_id="c1", user_id="u1")
    assert m.channel_type == "x"
    assert m.channel_id == "c1"
    assert m.user_id == "u1"
    assert m.text == ""
    assert m.username == ""
    assert m.is_voice is False
    assert m.audio_b64 == ""
    assert m.image_b64 == ""
    assert m.reply_to == ""
    assert m.metadata == {}


def test_channel_message_all_fields_and_metadata_not_shared():
    m = ChannelMessage(
        channel_type="slack",
        channel_id="C1",
        user_id="U1",
        text="hi",
        username="alice",
        is_voice=True,
        audio_b64="YWFh",
        image_b64="YmJi",
        reply_to="123",
        metadata={"k": 1},
    )
    assert m.text == "hi"
    assert m.username == "alice"
    assert m.is_voice is True
    assert m.audio_b64 == "YWFh"
    assert m.image_b64 == "YmJi"
    assert m.reply_to == "123"
    assert m.metadata == {"k": 1}

    m2 = ChannelMessage("a", "b", "c")
    m2.metadata["x"] = 1
    m3 = ChannelMessage("a", "b", "c")
    assert "x" not in m3.metadata


# ── ChannelResponse ──────────────────────────────────────────────────────────


def test_channel_response_defaults():
    r = ChannelResponse()
    assert r.text == ""
    assert r.sdui is None
    assert r.audio_b64 == ""
    assert r.image_b64 == ""
    assert r.buttons is None
    assert r.is_streaming is False


def test_channel_response_explicit_fields():
    r = ChannelResponse(
        text="t",
        sdui={"a": 1},
        audio_b64="x",
        image_b64="y",
        buttons=[{"label": "OK", "action": "ok"}],
        is_streaming=True,
    )
    assert r.text == "t"
    assert r.sdui == {"a": 1}
    assert r.buttons == [{"label": "OK", "action": "ok"}]
    assert r.is_streaming is True


# ── TelegramChannel ──────────────────────────────────────────────────────────


def test_telegram_channel_type():
    ch = TelegramChannel({"bot_token": ""})
    assert ch.channel_type == "telegram"


@pytest.mark.asyncio
async def test_telegram_start_without_token_warns(caplog):
    caplog.set_level(logging.WARNING)
    ch = TelegramChannel({"enabled": True})
    await ch.start()
    assert any("no bot_token" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_telegram_send_without_text_or_image_returns_early():
    ch = TelegramChannel({"bot_token": "t"})
    post = AsyncMock()
    ch._http = MagicMock()
    ch._http.post = post
    ch._base_url = "https://api.telegram.org/bot/t"
    await ch.send("1", ChannelResponse())
    post.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_send_with_buttons_posts_inline_keyboard():
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    mock_http.get = AsyncMock(
        return_value=MagicMock(json=lambda: {"result": []}),
    )

    with patch("httpx.AsyncClient", return_value=mock_http):
        ch = TelegramChannel({"bot_token": "test-token", "enabled": True})
        await ch.start()
        await ch.send(
            "chat-9",
            ChannelResponse(
                text="Choose",
                buttons=[
                    {"label": "One", "action": "1"},
                    {"label": "Two", "action": "2"},
                ],
            ),
        )
        await ch.stop()

    mock_http.post.assert_called()
    # Last call should be sendMessage with reply_markup
    calls = [c for c in mock_http.post.call_args_list if "sendMessage" in str(c)]
    assert calls
    payload = calls[-1].kwargs.get("json") or calls[-1][1].get("json")
    assert payload["chat_id"] == "chat-9"
    assert "reply_markup" in payload
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"] == "One"
    assert rows[1][0]["callback_data"] == "2"


@pytest.mark.asyncio
async def test_telegram_handle_message_invokes_handler_and_send():
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    mock_http.get = AsyncMock(
        return_value=MagicMock(json=lambda: {"result": []}),
    )
    handler = AsyncMock(return_value=ChannelResponse(text="reply"))

    with patch("httpx.AsyncClient", return_value=mock_http):
        # Inbound is default-deny (channels.base "Inbound access
        # control"), so the sender under test must be allowlisted
        # for the handler to be reached at all.
        ch = TelegramChannel(
            {"bot_token": "tok", "enabled": True, "allowed_senders": ["7"]},
        )
        ch.set_handler(handler)
        await ch.start()
        msg = {
            "chat": {"id": 42},
            "from": {"id": 7, "first_name": "Bob"},
            "text": "hello",
        }
        await ch._handle_message(msg)
        await ch.stop()

    handler.assert_awaited_once()
    hm = handler.await_args.args[0]
    assert isinstance(hm, ChannelMessage)
    assert hm.channel_id == "42"
    assert hm.text == "hello"
    mock_http.post.assert_called()


# ── DiscordChannel ─────────────────────────────────────────────────────────────


def test_discord_channel_type():
    assert DiscordChannel({}).channel_type == "discord"


@pytest.mark.asyncio
async def test_discord_start_without_token_warns(caplog):
    caplog.set_level(logging.WARNING)
    ch = DiscordChannel({})
    await ch.start()
    assert any("no bot_token" in r.message for r in caplog.records)


# ── SlackChannel ─────────────────────────────────────────────────────────────


def test_slack_channel_type():
    assert SlackChannel({}).channel_type == "slack"


@pytest.mark.asyncio
async def test_slack_start_without_token_warns(caplog):
    caplog.set_level(logging.WARNING)
    ch = SlackChannel({"app_token": "xapp"})
    await ch.start()
    assert any("no bot_token" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_slack_send_with_buttons_includes_blocks():
    ch = SlackChannel({"bot_token": "xoxb-test"})
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    ch._http = mock_http

    await ch.send(
        "C123",
        ChannelResponse(
            text="Pick one",
            buttons=[{"label": "A", "action": "a1"}],
        ),
    )

    mock_http.post.assert_called_once()
    kwargs = mock_http.post.call_args.kwargs
    payload = kwargs["json"]
    assert payload["channel"] == "C123"
    assert "blocks" in payload
    assert payload["blocks"][0]["type"] == "section"
    assert payload["blocks"][1]["type"] == "actions"
    assert payload["blocks"][1]["elements"][0]["action_id"] == "a1"


@pytest.mark.asyncio
async def test_slack_send_without_text_returns_early():
    ch = SlackChannel({"bot_token": "x"})
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    ch._http = mock_http
    await ch.send("C", ChannelResponse(text=""))
    mock_http.post.assert_not_called()


# ── WhatsAppChannel ──────────────────────────────────────────────────────────


def test_whatsapp_channel_type():
    assert WhatsAppChannel({}).channel_type == "whatsapp"


@pytest.mark.asyncio
async def test_whatsapp_start_without_config_warns(caplog):
    caplog.set_level(logging.WARNING)
    ch = WhatsAppChannel({"access_token": "only-token"})
    await ch.start()
    assert any("access_token and phone_number_id" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_whatsapp_handle_webhook_processes_message_and_returns_response():
    ch = WhatsAppChannel(
        {
            "access_token": "t",
            "phone_number_id": "pid",
            "enabled": True,
            "allowed_senders": ["+1000"],
        },
    )
    ch._running = True
    ch._phone_id = "pid"
    ch._token = "t"
    ch._access_token = "t"
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    ch._http = mock_http

    handler = AsyncMock(return_value=ChannelResponse(text="thanks"))
    ch.set_handler(handler)

    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "+1000",
                                    "text": {"body": "hi wa"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    out = await ch.handle_webhook(body)
    assert out is not None
    assert out.text == "thanks"
    handler.assert_awaited_once()
    assert "+1000" in ch.active_chat_ids
    mock_http.post.assert_called_once()


@pytest.mark.asyncio
async def test_whatsapp_handle_webhook_empty_returns_none():
    ch = WhatsAppChannel({"access_token": "t", "phone_number_id": "p"})
    ch._running = True
    ch._phone_id = "p"
    ch._http = MagicMock()
    ch._http.post = AsyncMock()
    ch.set_handler(AsyncMock(return_value=ChannelResponse(text="x")))
    assert await ch.handle_webhook({}) is None


# ── ChannelManager ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_manager_start_channel_and_get_channel():
    mgr = ChannelManager()
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_http):
        await mgr.start_channel("telegram", {"bot_token": "tok"})
    assert mgr.get_channel("telegram") is not None
    assert "telegram" in mgr.active_channels
    await mgr.stop_all()
    assert mgr.get_channel("telegram") is None


@pytest.mark.asyncio
async def test_channel_manager_unknown_channel_type_warns(caplog):
    caplog.set_level(logging.WARNING)
    mgr = ChannelManager()
    await mgr.start_channel("unknown_platform", {})
    assert any("Unknown channel type" in r.message for r in caplog.records)
    assert mgr.active_channels == []


@pytest.mark.asyncio
async def test_channel_manager_set_handler_propagates_to_started_channels():
    mgr = ChannelManager()
    h = AsyncMock(return_value=ChannelResponse(text="ok"))
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_http):
        await mgr.start_channel("telegram", {"bot_token": "t"})
        mgr.set_handler(h)
    tg = mgr.get_channel("telegram")
    assert tg is not None
    assert tg._handler is h
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_channel_manager_broadcast_and_send_to_channel():
    mgr = ChannelManager()
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.aclose = AsyncMock()
    slack = SlackChannel({"bot_token": "xoxb", "enabled": True})
    slack._http = mock_http
    slack._known_chat_ids.add("C1")
    slack._known_chat_ids.add("C2")
    mgr._channels["slack"] = slack

    resp = ChannelResponse(text="all")
    await mgr.broadcast(resp)
    assert mock_http.post.call_count == 2

    mock_http.post.reset_mock()
    await mgr.send_to_channel("slack", "C99", ChannelResponse(text="one"))
    assert mock_http.post.call_count == 1

    await mgr.send_to_channel("missing", "x", ChannelResponse(text="n"))
    # No extra crash; missing channel is no-op


@pytest.mark.asyncio
async def test_channel_manager_stop_all():
    mgr = ChannelManager()
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    mock_http.get = AsyncMock(return_value=MagicMock(json=lambda: {"result": []}))
    mock_http.aclose = AsyncMock()
    with patch("httpx.AsyncClient", return_value=mock_http):
        await mgr.start_channel("telegram", {"bot_token": "t"})
    await mgr.stop_all()
    assert mgr.active_channels == []


def test_channel_manager_stats():
    mgr = ChannelManager()
    slack = SlackChannel({"bot_token": "x"})
    slack._running = True
    slack._known_chat_ids.add("a")
    slack._known_chat_ids.add("b")
    mgr._channels["slack"] = slack

    s = mgr.stats
    assert s["channel_count"] == 1
    assert "slack" in s["active_channels"]
    assert s["details"]["slack"]["running"] is True
    assert s["details"]["slack"]["known_chats"] == 2


@pytest.mark.asyncio
async def test_discord_send_skips_when_no_text():
    ch = DiscordChannel({"bot_token": "t"})
    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    ch._http = mock_http
    await ch.send("ch", ChannelResponse(text=""))
    mock_http.post.assert_not_called()


# ── Rate-limit retry ─────────────────────────────────────────────────────────


class TestHttpWithRetry:
    @pytest.mark.asyncio
    async def test_retry_on_429_then_succeeds(self):
        from channels.base import Channel

        mock_client = MagicMock()
        resp_429 = MagicMock(status_code=429)
        resp_200 = MagicMock(status_code=200)
        mock_client.post = AsyncMock(side_effect=[resp_429, resp_200])

        result = await Channel._http_with_retry(mock_client, "POST", "https://api.example.com/send", json={"text": "hi"})
        assert result.status_code == 200
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_returns_last_response(self):
        from channels.base import Channel

        mock_client = MagicMock()
        resp_503 = MagicMock(status_code=503)
        mock_client.post = AsyncMock(return_value=resp_503)

        result = await Channel._http_with_retry(mock_client, "POST", "https://api.example.com/send")
        assert result.status_code == 503
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self):
        from channels.base import Channel

        mock_client = MagicMock()
        resp_200 = MagicMock(status_code=200)
        mock_client.get = AsyncMock(return_value=resp_200)

        result = await Channel._http_with_retry(mock_client, "GET", "https://api.example.com/ok")
        assert result.status_code == 200
        assert mock_client.get.call_count == 1
