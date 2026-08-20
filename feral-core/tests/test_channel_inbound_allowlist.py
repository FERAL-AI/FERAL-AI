"""Inbound owner allowlist for messaging channels.

Before this gate, ``TelegramChannel._handle_message`` read ``chat_id``
off the wire and handed the text straight to
``api.state._start_channels._channel_message_handler``, which calls
``brain.orchestrator.handle_command``. No sender check existed anywhere
in that path. With ``security.autonomy_mode = loose`` (nothing requires
approval) that meant anyone who found the bot handle got the owner's
full agent, with filesystem, shell and computer-use skills, on the
owner's personal machine.

These tests assert the gate at the point that matters: whether the
message reaches ``handle_command``.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from channels.base import (
    ChannelManager,
    ChannelMessage,
    ChannelResponse,
    DiscordChannel,
    SlackChannel,
    TelegramChannel,
    WhatsAppChannel,
    _coerce_id_set,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _telegram(config: dict) -> TelegramChannel:
    ch = TelegramChannel(config)
    ch._http = MagicMock()
    ch._http.post = AsyncMock()
    ch._base_url = "https://api.telegram.org/bottok"
    ch._running = True
    return ch


def _tg_message(user_id: int = 7, chat_id: int = 42, text: str = "rm -rf ~") -> dict:
    return {
        "chat": {"id": chat_id},
        "from": {"id": user_id, "first_name": "Mallory"},
        "text": text,
    }


# ── _coerce_id_set ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, set()),
        ("", set()),
        ([], set()),
        ("123", {"123"}),
        (123, {"123"}),
        ("123,456", {"123", "456"}),
        ("123; 456\n789", {"123", "456", "789"}),
        (["123", 456], {"123", "456"}),
        ("@alice", {"alice"}),
        ({"unhashable": "shape"}, set()),
        # api.state unions two config sources into a list, so a
        # list-shaped settings entry arrives nested.
        (["111", ["222", "333"]], {"111", "222", "333"}),
        (["111", "222,333"], {"111", "222", "333"}),
    ],
)
def test_coerce_id_set_normalises_every_config_shape(raw, expected):
    assert _coerce_id_set(raw) == expected


def test_empty_allowlist_is_the_fail_closed_value():
    """The misconfiguration case must be deny-all, never allow-all."""
    ch = TelegramChannel({"bot_token": "t"})
    assert ch.access_configured is False
    assert ch._allowed_senders == set()
    assert ch._allowed_chats == set()


# ── Telegram: the reported hole ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unlisted_telegram_sender_never_reaches_the_handler(caplog):
    caplog.set_level(logging.WARNING, logger="feral.channels.access")
    ch = _telegram({"bot_token": "t", "allowed_senders": ["999"]})
    handler = AsyncMock(return_value=ChannelResponse(text="pwned"))
    ch.set_handler(handler)

    await ch._handle_message(_tg_message(user_id=7, chat_id=42))

    handler.assert_not_awaited()
    # And no oracle: nothing at all was sent back to the stranger.
    ch._http.post.assert_not_called()
    # But the owner can see it happened, with the id they need.
    denials = [
        r.getMessage() for r in caplog.records
        if "DENIED inbound message" in r.getMessage()
    ]
    assert denials, "rejection was not logged"
    assert "sender_id=7" in denials[0]
    assert "chat_id=42" in denials[0]


@pytest.mark.asyncio
async def test_allowlisted_telegram_sender_reaches_the_handler():
    ch = _telegram({"bot_token": "t", "allowed_senders": ["7"]})
    handler = AsyncMock(return_value=ChannelResponse(text="ok"))
    ch.set_handler(handler)

    await ch._handle_message(_tg_message(user_id=7, chat_id=42, text="status"))

    handler.assert_awaited_once()
    assert handler.await_args.args[0].text == "status"


@pytest.mark.asyncio
async def test_allowlisted_chat_id_also_admits():
    """A chat id entry admits the whole chat, which is how a group is opted in."""
    ch = _telegram({"bot_token": "t", "allowed_chats": ["42"]})
    handler = AsyncMock(return_value=ChannelResponse(text="ok"))
    ch.set_handler(handler)

    await ch._handle_message(_tg_message(user_id=7, chat_id=42))

    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_denied_sender_is_not_remembered_as_a_broadcast_target():
    """A rejected chat must not end up in ``_known_chat_ids``.

    ``ChannelManager.broadcast`` fans out to every known chat, so a
    stranger landing in that set would make the brain volunteer output
    to them later even though it never answered them directly.
    """
    ch = _telegram({"bot_token": "t", "allowed_senders": ["999"]})
    ch.set_handler(AsyncMock(return_value=ChannelResponse(text="x")))

    await ch._handle_message(_tg_message(user_id=7, chat_id=42))

    assert "42" not in ch.active_chat_ids


@pytest.mark.asyncio
async def test_callback_query_is_gated_too():
    """A button payload is routed to the orchestrator like any text."""
    ch = _telegram({"bot_token": "t", "allowed_senders": ["999"]})
    handler = AsyncMock(return_value=ChannelResponse(text="x"))
    ch.set_handler(handler)

    await ch._handle_callback({
        "id": "cbq-1",
        "data": "skills__shell__run",
        "from": {"id": 7},
        "message": {"chat": {"id": 42}},
    })

    handler.assert_not_awaited()
    ch._http.post.assert_not_called()


# ── Discord / Slack / WhatsApp share the gate ────────────────────────────────


@pytest.mark.asyncio
async def test_discord_denies_unlisted_and_admits_listed():
    denied = DiscordChannel({"bot_token": "t", "allowed_senders": ["owner"]})
    denied._http = MagicMock(post=AsyncMock())
    h1 = AsyncMock(return_value=ChannelResponse(text="x"))
    denied.set_handler(h1)
    payload = {
        "channel_id": "CH-1",
        "author": {"id": "stranger", "username": "mallory", "bot": False},
        "content": "hi",
    }
    await denied._handle_discord_message(payload)
    h1.assert_not_awaited()
    assert "CH-1" not in denied.active_chat_ids

    allowed = DiscordChannel({"bot_token": "t", "allowed_senders": ["stranger"]})
    allowed._http = MagicMock(post=AsyncMock(return_value=MagicMock(
        status_code=200, json=lambda: {"id": "m1"},
    )))
    h2 = AsyncMock(return_value=ChannelResponse(text="x"))
    allowed.set_handler(h2)
    await allowed._handle_discord_message(payload)
    h2.assert_awaited_once()


@pytest.mark.asyncio
async def test_slack_denies_unlisted_and_admits_listed():
    event = {"channel": "C900", "user": "U12", "text": "hi"}

    denied = SlackChannel({"bot_token": "xoxb", "allowed_senders": ["UOWNER"]})
    denied._http = MagicMock(post=AsyncMock())
    h1 = AsyncMock(return_value=ChannelResponse(text="x"))
    denied.set_handler(h1)
    await denied._handle_slack_message(event)
    h1.assert_not_awaited()

    allowed = SlackChannel({"bot_token": "xoxb", "allowed_senders": ["U12"]})
    allowed._http = MagicMock(post=AsyncMock(return_value=MagicMock(
        status_code=200, json=lambda: {"ok": True, "ts": "1.0"},
    )))
    h2 = AsyncMock(return_value=ChannelResponse(text="x"))
    allowed.set_handler(h2)
    await allowed._handle_slack_message(event)
    h2.assert_awaited_once()


@pytest.mark.asyncio
async def test_whatsapp_webhook_is_gated_even_with_a_valid_signature():
    """A valid X-Hub-Signature proves the webhook came from Meta.

    It says nothing about who messaged the business number, so the
    sender still has to be allowlisted.
    """
    ch = WhatsAppChannel({"access_token": "t", "phone_number_id": "p"})
    ch._running = True
    ch._phone_id = "p"
    ch._http = MagicMock(post=AsyncMock())
    handler = AsyncMock(return_value=ChannelResponse(text="x"))
    ch.set_handler(handler)

    body = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "+1999", "text": {"body": "hi"}},
    ]}}]}]}
    assert await ch.handle_webhook(body) is None
    handler.assert_not_awaited()


# ── time-boxed, opt-in pairing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pairing_is_off_unless_explicitly_opted_into():
    ch = _telegram({"bot_token": "t"})
    ch._announce_access_posture()
    assert ch._pairing_window_sec == 0
    assert ch._pairing_window_is_open() is False


@pytest.mark.asyncio
async def test_pairing_window_binds_exactly_one_sender_then_closes():
    paired: list[tuple] = []

    def on_pair(channel_type, user_id, chat_id):
        paired.append((channel_type, user_id, chat_id))

    ch = _telegram({
        "bot_token": "t",
        "pairing_window_sec": 300,
        "on_pair": on_pair,
    })
    ch._announce_access_posture()
    assert ch._pairing_window_is_open() is True

    handler = AsyncMock(return_value=ChannelResponse(text="ok"))
    ch.set_handler(handler)

    await ch._handle_message(_tg_message(user_id=7, chat_id=42))
    handler.assert_awaited_once()
    assert paired == [("telegram", "7", "42")]
    # The window is now closed: it is a one-shot, not a duration during
    # which everyone gets in.
    assert ch._pairing_window_is_open() is False

    handler.reset_mock()
    await ch._handle_message(_tg_message(user_id=8, chat_id=43))
    handler.assert_not_awaited()

    # ...but the sender that DID pair stays admitted.
    await ch._handle_message(_tg_message(user_id=7, chat_id=42))
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_pairing_window_admits_nobody():
    ch = _telegram({"bot_token": "t", "pairing_window_sec": 1})
    ch._announce_access_posture()
    # Wind the clock past the window without sleeping for real.
    ch._pairing_opened_at -= 10.0
    assert ch._pairing_window_is_open() is False

    handler = AsyncMock(return_value=ChannelResponse(text="ok"))
    ch.set_handler(handler)
    await ch._handle_message(_tg_message())
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_pairing_survives_an_on_pair_hook_that_raises():
    """Persistence is best-effort; a failed write must not deny the owner."""
    def boom(*_a, **_kw):
        raise RuntimeError("settings.json is read-only")

    ch = _telegram({"bot_token": "t", "pairing_window_sec": 300, "on_pair": boom})
    ch._announce_access_posture()
    handler = AsyncMock(return_value=ChannelResponse(text="ok"))
    ch.set_handler(handler)
    await ch._handle_message(_tg_message(user_id=7, chat_id=42))
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_on_pair_hook_is_awaited():
    seen: list = []

    async def on_pair(channel_type, user_id, chat_id):
        await asyncio.sleep(0)
        seen.append(user_id)

    ch = _telegram({"bot_token": "t", "pairing_window_sec": 300, "on_pair": on_pair})
    ch._announce_access_posture()
    ch.set_handler(AsyncMock(return_value=ChannelResponse(text="ok")))
    await ch._handle_message(_tg_message(user_id=7, chat_id=42))
    assert seen == ["7"]


# ── first-run discoverability ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_senders_lets_the_owner_find_their_own_id():
    """The first-run story: message the bot, get silence, read your id locally."""
    ch = _telegram({"bot_token": "t"})
    ch.set_handler(AsyncMock(return_value=ChannelResponse(text="x")))

    await ch._handle_message(_tg_message(user_id=7, chat_id=42))
    await ch._handle_message(_tg_message(user_id=7, chat_id=42))

    rows = ch.pending_senders
    assert len(rows) == 1
    assert rows[0]["user_id"] == "7"
    assert rows[0]["chat_id"] == "42"
    assert rows[0]["count"] == 2
    # Attacker-controlled message text is never recorded.
    assert "rm -rf" not in repr(rows)


@pytest.mark.asyncio
async def test_pending_senders_is_bounded_against_a_flood():
    ch = _telegram({"bot_token": "t"})
    ch.set_handler(AsyncMock(return_value=ChannelResponse(text="x")))
    for i in range(200):
        await ch._handle_message(_tg_message(user_id=i, chat_id=i))
    assert len(ch.pending_senders) <= TelegramChannel._MAX_PENDING_SENDERS


@pytest.mark.asyncio
async def test_manager_stats_surface_the_access_posture():
    mgr = ChannelManager()
    ch = _telegram({"bot_token": "t", "allowed_senders": ["7"]})
    mgr._channels["telegram"] = ch
    row = mgr.stats["details"]["telegram"]
    assert row["access_configured"] is True
    assert row["allowed_sender_count"] == 1
    assert row["pairing_window_open"] is False
    assert row["pending_senders"] == []


# ── the end-to-end assertion: handle_command is never called ─────────────────


@pytest.mark.asyncio
async def test_denied_sender_never_reaches_orchestrator_handle_command():
    """Wire the real ``_channel_message_handler`` shape to the channel.

    ``api.state._start_channels`` installs a handler that forwards the
    text to ``brain.orchestrator.handle_command``. This asserts the gate
    at exactly that call.
    """
    handle_command = AsyncMock()

    async def channel_message_handler(msg: ChannelMessage) -> ChannelResponse:
        await handle_command(
            f"channel_{msg.channel_type}_{msg.user_id}",
            msg.text,
            context={"source": "channel"},
        )
        return ChannelResponse(text="done")

    ch = _telegram({"bot_token": "t", "allowed_senders": ["999"]})
    ch.set_handler(channel_message_handler)

    await ch._handle_message(_tg_message(user_id=7, chat_id=42, text="delete everything"))
    handle_command.assert_not_awaited()

    # Control: the owner's own id does reach it.
    owner = _telegram({"bot_token": "t", "allowed_senders": ["999"]})
    owner._http.post = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: {"ok": True, "result": {}}),
    )
    owner.set_handler(channel_message_handler)
    await owner._handle_message(_tg_message(user_id=999, chat_id=999, text="status"))
    handle_command.assert_awaited_once()
    assert handle_command.await_args.args[1] == "status"
