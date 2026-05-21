"""
FERAL Messaging Integration — Telegram + Slack + Discord
==========================================================
Thin skill-facing wrappers that delegate send operations to the
canonical channel implementations in ``channels.base``.  Read-only
helpers (``get_updates``, ``list_channels``) that have no channel
equivalent are kept here.

The ``MessagingHub.execute(endpoint_id, args, vault)`` contract
consumed by the skill executor is unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from channels.base import (
    ChannelResponse,
    DiscordChannel,
    SlackChannel,
    TelegramChannel,
)

logger = logging.getLogger("feral.integrations.messaging")


# ── helpers ────────────────────────────────────────────────────────

def _init_telegram(token: str) -> TelegramChannel:
    """Create a TelegramChannel wired for outbound use (no poll loop)."""
    ch = TelegramChannel({"bot_token": token})
    ch._http = httpx.AsyncClient(timeout=10.0)
    ch._base_url = f"https://api.telegram.org/bot{token}"
    return ch


def _init_slack(token: str) -> SlackChannel:
    ch = SlackChannel({"bot_token": token})
    ch._http = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=10.0,
    )
    return ch


def _init_discord(token: str) -> DiscordChannel:
    ch = DiscordChannel({"bot_token": token})
    ch._http = httpx.AsyncClient(
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        timeout=10.0,
    )
    return ch


# ── Telegram ───────────────────────────────────────────────────────

class TelegramBridge:
    """Skill-facing Telegram wrapper — delegates sends to TelegramChannel."""

    def __init__(self):
        self._token: Optional[str] = os.environ.get("FERAL_TELEGRAM_BOT_TOKEN")
        self._channel: Optional[TelegramChannel] = None
        if self._token:
            self._channel = _init_telegram(self._token)

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        return is_connected_cached(
            "telegram", fallback=self._token is not None,
        )

    async def probe_connected(self) -> bool:
        from integrations._probe_status import refresh

        result = await refresh("telegram")
        if result is None:
            return self.connected
        return result

    async def send(self, chat_id: str = "", text: str = "", **kwargs) -> dict:
        if not self._channel:
            return {"success": False, "error": "Telegram bot token not configured"}
        try:
            await self._channel.send(chat_id, ChannelResponse(text=text))
            return {"success": True, "data": {"chat_id": chat_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_updates(self, offset: int = 0, **kwargs) -> dict:
        """No channel equivalent — uses the channel's HTTP client directly."""
        if not self._channel:
            return {"success": False, "error": "Telegram bot token not configured"}
        try:
            resp = await self._channel._http.get(
                f"{self._channel._base_url}/getUpdates",
                params={"offset": offset, "timeout": 0, "limit": 25},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return {"success": False, "error": data.get("description", "Unknown Telegram error")}
            updates = []
            for u in data.get("result", []):
                msg = u.get("message", {})
                updates.append({
                    "update_id": u.get("update_id"),
                    "chat_id": msg.get("chat", {}).get("id"),
                    "from": msg.get("from", {}).get("first_name", ""),
                    "text": msg.get("text", ""),
                    "date": msg.get("date"),
                })
            return {"success": True, "data": {"updates": updates}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        if self._channel and hasattr(self._channel, "_http"):
            await self._channel._http.aclose()


# ── Slack ──────────────────────────────────────────────────────────

class SlackBridge:
    """Skill-facing Slack wrapper — delegates sends to SlackChannel."""

    def __init__(self):
        self._token: Optional[str] = os.environ.get("FERAL_SLACK_BOT_TOKEN")
        self._channel: Optional[SlackChannel] = None
        if self._token:
            self._channel = _init_slack(self._token)

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        return is_connected_cached(
            "slack", fallback=self._token is not None,
        )

    async def probe_connected(self) -> bool:
        from integrations._probe_status import refresh

        result = await refresh("slack")
        if result is None:
            return self.connected
        return result

    async def send(self, channel: str = "", text: str = "", **kwargs) -> dict:
        if not self._channel:
            return {"success": False, "error": "Slack bot token not configured"}
        try:
            await self._channel.send(channel, ChannelResponse(text=text))
            return {"success": True, "data": {"channel": channel}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def list_channels(self, **kwargs) -> dict:
        """No channel equivalent — uses the channel's HTTP client directly."""
        if not self._channel:
            return {"success": False, "error": "Slack bot token not configured"}
        try:
            resp = await self._channel._http.get(
                "https://slack.com/api/conversations.list",
                params={"types": "public_channel,private_channel", "limit": 100},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return {"success": False, "error": data.get("error", "Unknown Slack error")}
            channels = [
                {"id": c["id"], "name": c.get("name", ""), "is_member": c.get("is_member", False)}
                for c in data.get("channels", [])
            ]
            return {"success": True, "data": {"channels": channels}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def read_channel_history(self, channel: str = "", limit: int = 25, **kwargs) -> dict:
        """Fetch recent messages from a Slack channel."""
        if not self._channel:
            return {"success": False, "error": "Slack bot token not configured"}
        try:
            resp = await self._channel._http.get(
                "https://slack.com/api/conversations.history",
                params={"channel": channel, "limit": min(limit, 100)},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return {"success": False, "error": data.get("error", "Unknown Slack error")}
            messages = [
                {
                    "ts": m.get("ts", ""),
                    "user": m.get("user", ""),
                    "text": m.get("text", ""),
                    "thread_ts": m.get("thread_ts"),
                }
                for m in data.get("messages", [])
            ]
            return {"success": True, "data": {"channel": channel, "messages": messages}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def reply_to_thread(self, channel: str = "", thread_ts: str = "", text: str = "", **kwargs) -> dict:
        """Reply to a Slack thread."""
        if not self._channel:
            return {"success": False, "error": "Slack bot token not configured"}
        try:
            resp = await self._channel._http.post(
                "https://slack.com/api/chat.postMessage",
                json={"channel": channel, "thread_ts": thread_ts, "text": text},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return {"success": False, "error": data.get("error", "Unknown Slack error")}
            return {"success": True, "data": {"channel": channel, "ts": data.get("ts", "")}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_status(self, status_text: str = "", status_emoji: str = "", **kwargs) -> dict:
        """Set the bot user's Slack status."""
        if not self._channel:
            return {"success": False, "error": "Slack bot token not configured"}
        try:
            profile = {"status_text": status_text, "status_emoji": status_emoji}
            resp = await self._channel._http.post(
                "https://slack.com/api/users.profile.set",
                json={"profile": profile},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return {"success": False, "error": data.get("error", "Unknown Slack error")}
            return {"success": True, "data": {"status_text": status_text, "status_emoji": status_emoji}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        if self._channel and hasattr(self._channel, "_http"):
            await self._channel._http.aclose()


# ── Discord ────────────────────────────────────────────────────────

class DiscordBridge:
    """Skill-facing Discord wrapper — delegates sends to DiscordChannel."""

    DISCORD_API = "https://discord.com/api/v10"

    def __init__(self):
        self._token: Optional[str] = os.environ.get("FERAL_DISCORD_BOT_TOKEN")
        self._channel: Optional[DiscordChannel] = None
        if self._token:
            self._channel = _init_discord(self._token)

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        return is_connected_cached(
            "discord", fallback=self._token is not None,
        )

    async def probe_connected(self) -> bool:
        from integrations._probe_status import refresh

        result = await refresh("discord")
        if result is None:
            return self.connected
        return result

    async def send(self, channel_id: str = "", text: str = "", **kwargs) -> dict:
        if not self._channel:
            return {"success": False, "error": "Discord bot token not configured"}
        try:
            await self._channel.send(channel_id, ChannelResponse(text=text))
            return {"success": True, "data": {"channel_id": channel_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def list_channels(self, guild_id: str = "", **kwargs) -> dict:
        """No channel equivalent — uses the channel's HTTP client directly."""
        if not self._channel:
            return {"success": False, "error": "Discord bot token not configured"}
        try:
            resp = await self._channel._http.get(
                f"{self.DISCORD_API}/guilds/{guild_id}/channels",
            )
            resp.raise_for_status()
            raw = resp.json()
            channels = [
                {"id": c["id"], "name": c.get("name", ""), "type": c.get("type", 0)}
                for c in raw if c.get("type") in (0, 2, 5)
            ]
            return {"success": True, "data": {"channels": channels, "guild_id": guild_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        if self._channel and hasattr(self._channel, "_http"):
            await self._channel._http.aclose()


# ── Unified Hub ────────────────────────────────────────────────────

class MessagingHub:
    """
    Unified messaging dispatcher.
    Routes endpoint_id to the correct platform bridge.
    """

    def __init__(self, oauth_manager=None):
        self._oauth = oauth_manager
        self.telegram = TelegramBridge()
        self.slack = SlackBridge()
        self.discord = DiscordBridge()

    @property
    def connected(self) -> bool:
        return self.telegram.connected or self.slack.connected or self.discord.connected

    async def execute(self, endpoint_id: str, args: dict, vault: dict = None) -> dict:
        """Skill executor interface — called by SkillExecutor."""
        dispatch = {
            "telegram_send": self.telegram.send,
            "telegram_get_updates": self.telegram.get_updates,
            "slack_send": self.slack.send,
            "slack_list_channels": self.slack.list_channels,
            "slack_read_channel_history": self.slack.read_channel_history,
            "slack_reply_to_thread": self.slack.reply_to_thread,
            "slack_set_status": self.slack.set_status,
            "discord_send": self.discord.send,
            "discord_list_channels": self.discord.list_channels,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    async def close(self):
        await self.telegram.close()
        await self.slack.close()
        await self.discord.close()
