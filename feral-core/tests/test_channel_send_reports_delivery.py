"""``Channel.send`` must not report success for an undelivered message.

Telegram answers HTTP 200 with ``{"ok": false, "description": ...}`` for
a blocked bot, a bad chat_id and a Markdown parse error; Slack does the
same with ``{"ok": false, "error": ...}``. ``send`` used to POST and then
emit a "sent" comms event without ever looking at the response, so every
one of those looked identical to a delivery. The sibling ``send_direct``
on each class already got this right, which is the pattern followed here.

Nothing in this module touches a real provider: every response is a
constructed double and the payloads are inspected in-process.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from channels.base import (
    ChannelManager,
    ChannelResponse,
    ChannelSendError,
    DiscordChannel,
    SlackChannel,
    TelegramChannel,
    WhatsAppChannel,
)


def _resp(status: int, body):
    r = MagicMock()
    r.status_code = status
    if isinstance(body, Exception):
        r.json = MagicMock(side_effect=body)
    else:
        r.json = MagicMock(return_value=body)
    return r


def _telegram(response) -> TelegramChannel:
    ch = TelegramChannel({"bot_token": "t"})
    ch._http = MagicMock()
    ch._http.post = AsyncMock(return_value=response)
    ch._base_url = "https://api.telegram.org/bott"
    ch._running = True
    return ch


def _slack(response) -> SlackChannel:
    ch = SlackChannel({"bot_token": "xoxb"})
    ch._http = MagicMock()
    ch._http.post = AsyncMock(return_value=response)
    ch._running = True
    return ch


def _discord(response) -> DiscordChannel:
    ch = DiscordChannel({"bot_token": "t"})
    ch._http = MagicMock()
    ch._http.post = AsyncMock(return_value=response)
    ch._running = True
    return ch


# ── Telegram ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,body,expected_fragment",
    [
        (200, {"ok": False, "description": "Forbidden: bot was blocked by the user"},
         "blocked by the user"),
        (400, {"ok": False, "description": "Bad Request: chat not found"},
         "chat not found"),
        (400, {"ok": False, "description": "Bad Request: can't parse entities"},
         "parse entities"),
        (401, {"ok": False, "description": "Unauthorized"}, "Unauthorized"),
        (502, ValueError("not json"), "HTTP 502"),
    ],
)
@pytest.mark.asyncio
async def test_telegram_send_raises_on_every_rejection(status, body, expected_fragment):
    ch = _telegram(_resp(status, body))
    with pytest.raises(ChannelSendError) as exc:
        await ch.send("42", ChannelResponse(text="hi"))
    assert expected_fragment in str(exc.value)
    assert exc.value.channel == "telegram"


@pytest.mark.asyncio
async def test_telegram_send_emits_no_sent_event_on_rejection(monkeypatch):
    events: list = []

    async def spy(self, direction, who, preview="", extra=None):
        events.append((direction, who))

    monkeypatch.setattr(
        "channels.base.Channel._emit_comms_event", spy, raising=True,
    )
    ch = _telegram(_resp(200, {"ok": False, "description": "chat not found"}))
    with pytest.raises(ChannelSendError):
        await ch.send("42", ChannelResponse(text="hi"))
    assert events == [], f"a 'sent' event was emitted for a failed send: {events}"


@pytest.mark.asyncio
async def test_telegram_send_returns_the_send_direct_envelope_on_success():
    ch = _telegram(_resp(200, {"ok": True, "result": {"message_id": 77}}))
    out = await ch.send("42", ChannelResponse(text="hi"))
    assert out["success"] is True
    assert out["status_code"] == 200
    assert out["message_id"] == 77


@pytest.mark.asyncio
async def test_telegram_send_with_nothing_to_send_is_skipped_not_failed():
    ch = _telegram(_resp(200, {"ok": True, "result": {}}))
    out = await ch.send("42", ChannelResponse())
    assert out["skipped"] is True
    ch._http.post.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_transport_error_raises_rather_than_being_swallowed():
    ch = TelegramChannel({"bot_token": "t"})
    ch._http = MagicMock()
    ch._http.post = AsyncMock(side_effect=OSError("connection reset"))
    ch._base_url = "https://api.telegram.org/bott"
    with pytest.raises(ChannelSendError, match="connection reset"):
        await ch.send("42", ChannelResponse(text="hi"))


# ── Slack ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,body,fragment",
    [
        (200, {"ok": False, "error": "channel_not_found"}, "channel_not_found"),
        (200, {"ok": False, "error": "not_in_channel"}, "not_in_channel"),
        (200, {"ok": False, "error": "invalid_auth"}, "invalid_auth"),
        (500, ValueError("html"), "HTTP 500"),
    ],
)
@pytest.mark.asyncio
async def test_slack_send_raises_on_rejection(status, body, fragment):
    ch = _slack(_resp(status, body))
    with pytest.raises(ChannelSendError) as exc:
        await ch.send("C1", ChannelResponse(text="hi"))
    assert fragment in str(exc.value)


@pytest.mark.asyncio
async def test_slack_send_success_envelope():
    ch = _slack(_resp(200, {"ok": True, "ts": "1699.0001"}))
    out = await ch.send("C1", ChannelResponse(text="hi"))
    assert out["success"] is True
    assert out["message_id"] == "1699.0001"


# ── Discord ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,body,fragment",
    [
        (403, {"message": "Missing Access", "code": 50001}, "Missing Access"),
        (404, {"message": "Unknown Channel", "code": 10003}, "Unknown Channel"),
        (401, ValueError("no body"), "error"),
    ],
)
@pytest.mark.asyncio
async def test_discord_send_raises_on_rejection(status, body, fragment):
    ch = _discord(_resp(status, body))
    with pytest.raises(ChannelSendError) as exc:
        await ch.send("CH1", ChannelResponse(text="hi"))
    assert fragment in str(exc.value)


@pytest.mark.asyncio
async def test_discord_send_success_envelope():
    ch = _discord(_resp(200, {"id": "msg-1"}))
    out = await ch.send("CH1", ChannelResponse(text="hi"))
    assert out["success"] is True
    assert out["message_id"] == "msg-1"


# ── WhatsApp ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whatsapp_send_raises_when_send_text_failed():
    ch = WhatsAppChannel({"access_token": "t", "phone_number_id": "p"})
    ch._running = True
    ch._phone_id = "p"
    ch._access_token = "t"
    ch._http = MagicMock()
    ch.send_text = AsyncMock(return_value={"success": False, "error": "bad number"})
    with pytest.raises(ChannelSendError, match="bad number"):
        await ch.send("+1", ChannelResponse(text="hi"))


# ── the skill-facing wrappers stop lying ─────────────────────────────────────


@pytest.mark.asyncio
async def test_messaging_bridge_reports_failure_for_a_rejected_telegram_send():
    """``integrations.messaging`` backs ``messaging_sms__telegram_send``.

    It calls ``channel.send(...)`` and ignores the return value, so only
    a raise can stop it answering ``success: True`` for a message that
    was never delivered.
    """
    from integrations.messaging import TelegramBridge

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._token = "t"
    bridge._channel = _telegram(
        _resp(200, {"ok": False, "description": "Bad Request: chat not found"}),
    )

    out = await bridge.send(chat_id="nope", text="hi")
    assert out["success"] is False
    assert "chat not found" in out["error"]


@pytest.mark.asyncio
async def test_messaging_bridge_still_reports_success_for_a_real_delivery():
    from integrations.messaging import TelegramBridge

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._token = "t"
    bridge._channel = _telegram(_resp(200, {"ok": True, "result": {"message_id": 1}}))

    out = await bridge.send(chat_id="42", text="hi")
    assert out["success"] is True


# ── manager fan-out isolation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_survives_one_blocked_recipient():
    ch = TelegramChannel({"bot_token": "t"})
    ch._base_url = "https://api.telegram.org/bott"
    ch._running = True
    ch._known_chat_ids.update({"good", "blocked"})

    seen: list[str] = []

    async def post(url, **kw):
        chat = kw["json"]["chat_id"]
        seen.append(chat)
        if chat == "blocked":
            return _resp(200, {"ok": False, "description": "bot was blocked"})
        return _resp(200, {"ok": True, "result": {"message_id": 1}})

    ch._http = MagicMock()
    ch._http.post = AsyncMock(side_effect=post)

    mgr = ChannelManager()
    mgr._channels["telegram"] = ch
    await mgr.broadcast(ChannelResponse(text="all hands"))

    assert sorted(seen) == ["blocked", "good"], (
        "a rejected recipient aborted the rest of the broadcast"
    )


@pytest.mark.asyncio
async def test_send_to_channel_returns_a_failure_envelope():
    ch = _telegram(_resp(200, {"ok": False, "description": "chat not found"}))
    mgr = ChannelManager()
    mgr._channels["telegram"] = ch
    out = await mgr.send_to_channel("telegram", "42", ChannelResponse(text="hi"))
    assert out["success"] is False
    assert "chat not found" in out["error"]

    missing = await mgr.send_to_channel("nope", "x", ChannelResponse(text="hi"))
    assert missing["success"] is False
    assert missing["status_code"] == 404
