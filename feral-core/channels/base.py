"""
FERAL Channels — Multi-Platform Messaging Bridges
=====================================================
Bidirectional messaging bridges with support for text, voice notes,
images, and SDUI.  Every channel can receive and send rich content.

Supported: Telegram, Discord, Slack, WhatsApp
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional, Callable, Awaitable, Any

logger = logging.getLogger("feral.channels")


class ChannelMessage:
    """A message received from any channel."""

    def __init__(
        self,
        channel_type: str,
        channel_id: str,
        user_id: str,
        text: str = "",
        username: str = "",
        is_voice: bool = False,
        audio_b64: str = "",
        image_b64: str = "",
        reply_to: str = "",
        metadata: Optional[dict] = None,
    ):
        self.channel_type = channel_type
        self.channel_id = channel_id
        self.user_id = user_id
        self.text = text
        self.username = username
        self.is_voice = is_voice
        self.audio_b64 = audio_b64
        self.image_b64 = image_b64
        self.reply_to = reply_to
        self.metadata = metadata or {}


class ChannelResponse:
    """A response to send back through a channel."""

    def __init__(
        self,
        text: str = "",
        sdui: Optional[dict] = None,
        audio_b64: str = "",
        image_b64: str = "",
        buttons: Optional[list[dict]] = None,
        is_streaming: bool = False,
    ):
        self.text = text
        self.sdui = sdui
        self.audio_b64 = audio_b64
        self.image_b64 = image_b64
        self.buttons = buttons
        self.is_streaming = is_streaming


MessageHandler = Callable[[ChannelMessage], Awaitable[ChannelResponse]]


class Channel(ABC):
    """Base class for all messaging channels."""

    def __init__(self, config: dict):
        self.config = config
        self.enabled = config.get("enabled", True)
        self._handler: Optional[MessageHandler] = None
        self._running = False
        self._known_chat_ids: set[str] = set()
        # Cache of handle/username → chat_id (e.g. "@alice" -> "123456789")
        self._username_cache: dict[str, str] = {}
        # Populated by subclasses if the upstream API provides it
        self._bot_username: Optional[str] = None
        # Populated to True once the channel has successfully talked to its API
        self._connected: bool = False
        # -A11 runtime containment: repeated loop failures trip a fuse so
        # a broken channel instance disables itself cleanly instead of
        # wedging forever and spamming logs.
        try:
            self._runtime_failure_fuse = max(
                1, int(os.environ.get("FERAL_CHANNEL_FAILURE_FUSE", "8"))
            )
        except Exception:
            self._runtime_failure_fuse = 8
        try:
            self._runtime_backoff_cap_sec = max(
                1, int(os.environ.get("FERAL_CHANNEL_BACKOFF_CAP_SEC", "60"))
            )
        except Exception:
            self._runtime_backoff_cap_sec = 60
        self._runtime_failures: int = 0
        self._degraded: bool = False
        self._degraded_reason: str = ""

    async def send_direct(self, to: str, text: str, reply_to: Optional[str] = None) -> dict:
        """Outbound send used by the messaging_channels skill.

        Default implementation wraps ``send(channel_id, ChannelResponse(text=text))``
        so subclasses that only implemented ``send`` still work. Subclasses that
        need richer behavior (e.g. resolving @handles) should override.
        """
        try:
            await self.send(to, ChannelResponse(text=text))
            return {"success": True, "message_id": None, "raw": None}
        except Exception as e:
            return {"success": False, "error": str(e), "status_code": 502}

    async def resolve_username(self, handle: str) -> Optional[dict]:
        """Resolve an ``@handle`` to a channel-specific chat id.

        Default returns None (not supported). Telegram overrides this.
        """
        return None

    def set_handler(self, handler: MessageHandler):
        self._handler = handler

    async def _emit_comms_event(self, direction: str, sender_or_recipient: str, preview: str = "", extra: dict = None):
        """Emit brain_event for Glass Brain comms visualization. direction: 'in' or 'out'."""
        try:
            from observability.metrics import increment
            channel_name = self.__class__.__name__.replace("Channel", "").lower()
            increment("feral.channel.message_total", attributes={"channel": channel_name, "direction": direction})
        except Exception:
            pass
        try:
            from api.state import state
            if not state.orchestrator:
                return
            payload = {
                "channel": self.__class__.__name__.replace("Channel", "").lower(),
                "direction": direction,
            }
            if direction == "in":
                payload["sender"] = sender_or_recipient
            else:
                payload["recipient"] = sender_or_recipient
            payload["preview"] = (preview or "")[:100]
            if extra:
                payload.update(extra)
            event_type = "channel_message_in" if direction == "in" else "channel_message_out"
            for sid in list(state.sessions.keys()):
                try:
                    await state.orchestrator._emit_brain_event(sid, event_type, payload)
                except Exception:
                    pass
        except Exception:
            pass

    @abstractmethod
    async def start(self):
        ...

    @abstractmethod
    async def stop(self):
        ...

    @abstractmethod
    async def send(self, channel_id: str, response: ChannelResponse):
        ...

    @property
    @abstractmethod
    def channel_type(self) -> str:
        ...

    @property
    def active_chat_ids(self) -> list[str]:
        return list(self._known_chat_ids)

    def _reset_runtime_health(self) -> None:
        self._runtime_failures = 0
        self._degraded = False
        self._degraded_reason = ""

    def _record_runtime_success(self) -> None:
        self._runtime_failures = 0

    def _next_backoff_seconds(self) -> float:
        streak = max(1, int(self._runtime_failures or 1))
        return float(min(self._runtime_backoff_cap_sec, 2 ** min(streak, 6)))

    def _record_runtime_failure(self, context: str, error: Exception | str) -> None:
        self._runtime_failures += 1
        logger.warning(
            "Channel runtime failure: channel=%s context=%s failures=%d/%d error=%s",
            self.channel_type,
            context,
            self._runtime_failures,
            self._runtime_failure_fuse,
            error,
        )
        if self._runtime_failures >= self._runtime_failure_fuse:
            self._degraded = True
            self._degraded_reason = f"{context}: {error}"
            self._running = False
            logger.error(
                "Channel fuse opened: channel=%s reason=%s",
                self.channel_type,
                self._degraded_reason,
            )

    @staticmethod
    async def _http_with_retry(client, method: str, url: str, **kw):
        """Exponential-backoff retry for 429 / 503 responses (up to 3 attempts)."""
        fn = getattr(client, method.lower(), None) or client.request
        r = None
        for attempt in range(3):
            if fn is client.request:
                r = await fn(method, url, **kw)
            else:
                r = await fn(url, **kw)
            if hasattr(r, "status_code") and r.status_code in (429, 503):
                logger.warning("Channel HTTP %s %s returned %d (attempt %d/3)", method, url[:80], r.status_code, attempt + 1)
                await asyncio.sleep(2 ** attempt)
                continue
            return r
        return r


class TelegramChannel(Channel):
    """Telegram bot — full bidirectional with voice/image support."""

    @property
    def channel_type(self) -> str:
        return "telegram"

    async def start(self):
        # Normalize token: users pasting the token into Settings often
        # include trailing newlines or whitespace from clipboard managers,
        # which silently corrupts the URL path segment and makes every
        # request return an HTML error page. Strip once here.
        raw_token = self.config.get("bot_token", "") or ""
        token = raw_token.strip() if isinstance(raw_token, str) else ""
        if not token:
            logger.warning("Telegram channel: no bot_token configured")
            return
        if token != raw_token:
            # Persist the cleaned value back so downstream reads (e.g. file
            # downloads that re-read ``self.config['bot_token']``) see the
            # same value we used to build the base URL.
            self.config["bot_token"] = token

        import httpx
        self._reset_runtime_health()
        self._running = True
        self._http = httpx.AsyncClient(timeout=35.0)
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._offset = 0
        self._poll_task: Optional[asyncio.Task] = None

        getme_authoritative_failure = False
        try:
            me_resp = await self._http.get(f"{self._base_url}/getMe")
            status = getattr(me_resp, "status_code", 0)
            ctype = ""
            try:
                ctype = me_resp.headers.get("content-type", "")
            except Exception:
                pass
            me_data: dict = {}
            if status == 200 and "json" in ctype.lower():
                try:
                    me_data = me_resp.json() or {}
                except Exception as je:
                    logger.error("Telegram getMe: non-JSON body despite 200 (%s): %s", ctype, je)
            elif status == 200:
                try:
                    me_data = me_resp.json() or {}
                except Exception:
                    snippet = ""
                    try:
                        snippet = (me_resp.text or "")[:200]
                    except Exception:
                        pass
                    logger.error(
                        "Telegram getMe: 200 with non-JSON body (content-type=%r); snippet=%r",
                        ctype, snippet,
                    )

            if me_data.get("ok"):
                self._bot_username = me_data.get("result", {}).get("username")
                self._connected = True
                self._record_runtime_success()
                logger.info("Telegram channel started (bot: @%s)", self._bot_username)
            else:
                # Authoritative negative from Telegram (e.g. 401 "Unauthorized"
                # with ok=false) — the token is bad. Don't start the poll loop;
                # otherwise we'd spin forever burning quota and spamming logs.
                self._connected = False
                if status in (401, 403) or (isinstance(me_data, dict) and me_data.get("ok") is False):
                    getme_authoritative_failure = True
                    logger.error(
                        "Telegram getMe rejected (status=%s, description=%r). Not starting poll loop.",
                        status, (me_data or {}).get("description"),
                    )
                else:
                    logger.warning(
                        "Telegram getMe returned non-ok status=%s body=%r; will start poll loop anyway.",
                        status, me_data,
                    )
        except Exception as e:
            # Network error — bot may still be reachable later. Leave
            # _connected False but start the poll loop so it can recover.
            self._connected = False
            logger.warning("Telegram getMe transport error (will still poll): %s", e)

        if getme_authoritative_failure:
            self._running = False
            try:
                await self._http.aclose()
            except Exception:
                pass
            return

        self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def stop(self):
        self._running = False
        # Cancel the long-poll task first so we don't race aclose() with
        # an in-flight getUpdates. Without this, replacing a TelegramChannel
        # in ChannelManager could leak a poll loop that keeps calling
        # getUpdates against the closed httpx client (logs a noisy
        # RuntimeError) or, worse, double-subscribes the same bot.
        task = getattr(self, "_poll_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if hasattr(self, "_http") and self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass

    async def _poll_loop(self):
        while self._running:
            try:
                resp = await self._http.get(
                    f"{self._base_url}/getUpdates",
                    params={"offset": self._offset, "timeout": 30},
                )
                try:
                    status = int(getattr(resp, "status_code", 0) or 0)
                except (TypeError, ValueError):
                    status = 0
                # Telegram will reply with non-2xx + JSON body for
                # ``{"ok": false, "description": "..."}`` errors, but a
                # reverse proxy or outage can return HTML or an empty body.
                # ``.json()`` raises ``JSONDecodeError`` on both, which
                # prior to this fix would spam the logs once per iteration
                # with the unhelpful ``Expecting value: line 1 column 1``.
                if status and status >= 400:
                    snippet = ""
                    try:
                        snippet = (resp.text or "")[:200]
                    except Exception:
                        pass
                    logger.warning(
                        "Telegram getUpdates HTTP %s; snippet=%r", status, snippet,
                    )
                    if status != 429:
                        self._record_runtime_failure(
                            "telegram_getupdates_http",
                            f"HTTP {status}",
                        )
                        if not self._running:
                            break
                        await asyncio.sleep(self._next_backoff_seconds())
                    else:
                        await asyncio.sleep(10)
                    continue
                ctype = ""
                try:
                    ctype = resp.headers.get("content-type", "") or ""
                except Exception:
                    pass
                try:
                    data = resp.json()
                except Exception as je:
                    snippet = ""
                    try:
                        snippet = (resp.text or "")[:200]
                    except Exception:
                        pass
                    logger.warning(
                        "Telegram getUpdates non-JSON body (content-type=%r, status=%s): %s; snippet=%r",
                        ctype, status, je, snippet,
                    )
                    self._record_runtime_failure("telegram_getupdates_non_json", je)
                    if not self._running:
                        break
                    await asyncio.sleep(self._next_backoff_seconds())
                    continue
                if not isinstance(data, dict):
                    logger.warning(
                        "Telegram getUpdates unexpected payload shape: %r", type(data).__name__,
                    )
                    self._record_runtime_failure(
                        "telegram_getupdates_shape",
                        f"type={type(data).__name__}",
                    )
                    if not self._running:
                        break
                    await asyncio.sleep(self._next_backoff_seconds())
                    continue
                if data.get("ok") is False:
                    desc = data.get("description") or "unknown"
                    logger.warning("Telegram getUpdates not ok: %s", desc)
                    self._record_runtime_failure("telegram_getupdates_not_ok", desc)
                    if not self._running:
                        break
                    await asyncio.sleep(self._next_backoff_seconds())
                    continue

                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    message = update.get("message", {})
                    callback = update.get("callback_query")

                    if callback and self._handler:
                        await self._handle_callback(callback)
                    elif message and self._handler:
                        await self._handle_message(message)
                self._record_runtime_success()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._record_runtime_failure("telegram_poll_exception", e)
                if not self._running:
                    break
                await asyncio.sleep(self._next_backoff_seconds())

    async def _handle_message(self, message: dict):
        chat_id = str(message.get("chat", {}).get("id", ""))
        user = message.get("from", {})
        user_id = str(user.get("id", ""))
        username = user.get("first_name", "") or user.get("username", "")
        self._known_chat_ids.add(chat_id)

        text = message.get("text", "")
        is_voice = "voice" in message
        image_b64 = ""

        if message.get("photo"):
            photo = message["photo"][-1]
            image_b64 = await self._download_file(photo.get("file_id", ""))

        channel_msg = ChannelMessage(
            channel_type="telegram",
            channel_id=chat_id,
            user_id=user_id,
            text=text,
            username=username,
            is_voice=is_voice,
            image_b64=image_b64,
        )
        await self._emit_comms_event("in", username or user_id, text)
        response = await self._handler(channel_msg)
        await self.send(chat_id, response)

    async def _handle_callback(self, callback: dict):
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        data = callback.get("data", "")
        user_id = str(callback.get("from", {}).get("id", ""))
        self._known_chat_ids.add(chat_id)

        channel_msg = ChannelMessage(
            channel_type="telegram",
            channel_id=chat_id,
            user_id=user_id,
            text=data,
            metadata={"callback": True},
        )
        await self._emit_comms_event("in", user_id, data, {"callback": True})
        response = await self._handler(channel_msg)
        await self.send(chat_id, response)

        await self._http.post(
            f"{self._base_url}/answerCallbackQuery",
            json={"callback_query_id": callback.get("id", "")},
        )

    async def _download_file(self, file_id: str) -> str:
        import base64
        try:
            resp = await self._http.get(f"{self._base_url}/getFile", params={"file_id": file_id})
            file_path = resp.json().get("result", {}).get("file_path", "")
            if file_path:
                token = self.config.get("bot_token", "")
                file_resp = await self._http.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
                return base64.b64encode(file_resp.content).decode()
        except Exception as e:
            logger.error(f"Telegram file download failed: {e}")
        return ""

    async def send(self, channel_id: str, response: ChannelResponse):
        if not response.text and not response.image_b64:
            return
        tg_logger = logging.getLogger("feral.channel.telegram")
        try:
            payload = {
                "chat_id": channel_id,
                "text": response.text or "(see attachment)",
                "parse_mode": "Markdown",
            }
            if response.buttons:
                payload["reply_markup"] = {
                    "inline_keyboard": [
                        [{"text": b.get("label", ""), "callback_data": b.get("action", "")}]
                        for b in response.buttons
                    ]
                }
            await self._http_with_retry(self._http, "POST", f"{self._base_url}/sendMessage", json=payload)
            await self._emit_comms_event("out", channel_id, response.text)
        except Exception as e:
            tg_logger.error("Telegram send error: %s", e)

    async def resolve_username(self, handle: str) -> Optional[dict]:
        """Map @username → numeric chat_id via Telegram's getChat."""
        if not handle:
            return None
        h = handle.strip()
        if not h.startswith("@"):
            h = "@" + h
        cached = self._username_cache.get(h.lower())
        if cached:
            return {"chat_id": cached, "handle": h}
        if not getattr(self, "_http", None):
            return None
        try:
            resp = await self._http.get(
                f"{self._base_url}/getChat", params={"chat_id": h}
            )
            data = resp.json() if resp.status_code == 200 else {}
            if data.get("ok"):
                result = data.get("result", {})
                chat_id = str(result.get("id", ""))
                title = result.get("title") or result.get("first_name") or h
                if chat_id:
                    self._username_cache[h.lower()] = chat_id
                    self._known_chat_ids.add(chat_id)
                    return {"chat_id": chat_id, "title": title, "handle": h}
        except Exception as e:
            logger.error("Telegram resolve_username error: %s", e)
        return None

    async def send_direct(self, to: str, text: str, reply_to: Optional[str] = None) -> dict:
        """Send a Telegram message to a chat_id or @username."""
        tg_logger = logging.getLogger("feral.channel.telegram")
        if not getattr(self, "_http", None) or not self._running:
            return {"success": False, "error": "Telegram channel not running", "status_code": 503}
        if not text:
            return {"success": False, "error": "text is required", "status_code": 400}

        target = to.strip()
        chat_id: Optional[str] = None
        if target.startswith("@") or (not target.lstrip("-").isdigit()):
            resolved = await self.resolve_username(target)
            if resolved and resolved.get("chat_id"):
                chat_id = resolved["chat_id"]
            else:
                return {
                    "success": False,
                    "status_code": 404,
                    "error": (
                        f"Could not resolve Telegram handle {target!r}. The user must "
                        "open your bot in Telegram and send /start at least once so it "
                        "learns their chat_id."
                    ),
                }
        else:
            chat_id = target

        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_to:
            try:
                payload["reply_to_message_id"] = int(reply_to)
            except (TypeError, ValueError):
                pass

        try:
            r = await self._http_with_retry(
                self._http, "POST", f"{self._base_url}/sendMessage", json=payload
            )
            data = r.json() if getattr(r, "status_code", 0) else {}
            if not data.get("ok"):
                desc = data.get("description") or f"HTTP {getattr(r, 'status_code', '?')}"
                tg_logger.warning("Telegram send_direct failed: %s", desc)
                return {
                    "success": False,
                    "status_code": int(getattr(r, "status_code", 502)),
                    "error": f"Telegram API: {desc}",
                }
            self._known_chat_ids.add(str(chat_id))
            await self._emit_comms_event("out", str(chat_id), text)
            return {
                "success": True,
                "status_code": 200,
                "message_id": data.get("result", {}).get("message_id"),
                "raw": data.get("result"),
            }
        except Exception as e:
            tg_logger.error("Telegram send_direct error: %s", e, exc_info=True)
            return {"success": False, "status_code": 502, "error": str(e)}


class DiscordChannel(Channel):
    """Discord bot — Gateway WebSocket for incoming, REST for outgoing."""

    @property
    def channel_type(self) -> str:
        return "discord"

    async def start(self):
        token = self.config.get("bot_token", "")
        if not token:
            logger.warning("Discord channel: no bot_token configured")
            return

        import httpx
        self._reset_runtime_health()
        self._running = True
        self._token = token
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            timeout=10.0,
        )

        self._connected = True
        asyncio.ensure_future(self._gateway_connect())
        logger.info("Discord channel started (Gateway mode)")

    async def _gateway_connect(self):
        """Connect to Discord Gateway for incoming messages."""
        try:
            import websockets
            resp = await self._http.get("https://discord.com/api/v10/gateway")
            gateway_url = resp.json().get("url", "")
            if not gateway_url:
                logger.warning("Discord: could not get gateway URL")
                return

            async with websockets.connect(f"{gateway_url}?v=10&encoding=json") as ws:
                hello = json.loads(await ws.recv())
                heartbeat_interval = hello.get("d", {}).get("heartbeat_interval", 41250) / 1000

                await ws.send(json.dumps({
                    "op": 2,
                    "d": {
                        "token": self._token,
                        "intents": 512 | 32768,
                        "properties": {"os": "linux", "browser": "feral", "device": "feral"},
                    },
                }))

                asyncio.ensure_future(self._heartbeat_loop(ws, heartbeat_interval))
                self._record_runtime_success()

                async for raw in ws:
                    if not self._running:
                        break
                    event = json.loads(raw)
                    if event.get("t") == "MESSAGE_CREATE":
                        await self._handle_discord_message(event.get("d", {}))

        except Exception as e:
            if self._running:
                self._record_runtime_failure("discord_gateway", e)
                if not self._running:
                    return
                await asyncio.sleep(self._next_backoff_seconds())
                if self._running:
                    asyncio.ensure_future(self._gateway_connect())

    async def _heartbeat_loop(self, ws, interval: float):
        while self._running:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({"op": 1, "d": None}))
            except Exception:
                break

    async def _handle_discord_message(self, data: dict):
        if data.get("author", {}).get("bot"):
            return

        channel_id = data.get("channel_id", "")
        user_id = data.get("author", {}).get("id", "")
        username = data.get("author", {}).get("username", "")
        text = data.get("content", "")
        self._known_chat_ids.add(channel_id)

        if self._handler:
            channel_msg = ChannelMessage(
                channel_type="discord",
                channel_id=channel_id,
                user_id=user_id,
                text=text,
                username=username,
            )
            await self._emit_comms_event("in", username or user_id, text)
            response = await self._handler(channel_msg)
            await self.send(channel_id, response)

    async def stop(self):
        self._running = False
        if hasattr(self, "_http"):
            await self._http.aclose()

    async def send(self, channel_id: str, response: ChannelResponse):
        if not response.text:
            return
        dc_logger = logging.getLogger("feral.channel.discord")
        try:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            await self._http_with_retry(self._http, "POST", url, json={"content": response.text})
            await self._emit_comms_event("out", channel_id, response.text)
        except Exception as e:
            dc_logger.error("Discord send error: %s", e)

    async def send_direct(self, to: str, text: str, reply_to: Optional[str] = None) -> dict:
        dc_logger = logging.getLogger("feral.channel.discord")
        if not getattr(self, "_http", None) or not self._running:
            return {"success": False, "status_code": 503, "error": "Discord channel not running"}
        if not text:
            return {"success": False, "status_code": 400, "error": "text is required"}
        target = (to or "").strip()
        if not target.isdigit():
            return {
                "success": False,
                "status_code": 400,
                "error": (
                    f"Discord needs a numeric channel id, got {target!r}. "
                    "Right-click a channel in Discord with developer mode on → 'Copy Channel ID'."
                ),
            }
        payload: dict = {"content": text}
        if reply_to:
            payload["message_reference"] = {"message_id": str(reply_to)}
        try:
            r = await self._http_with_retry(
                self._http, "POST",
                f"https://discord.com/api/v10/channels/{target}/messages",
                json=payload,
            )
            status = getattr(r, "status_code", 0)
            if status >= 400:
                try:
                    err_body = r.json()
                except Exception:
                    err_body = {"message": f"HTTP {status}"}
                return {
                    "success": False,
                    "status_code": int(status),
                    "error": f"Discord API: {err_body.get('message', 'error')}",
                }
            data = {}
            try:
                data = r.json()
            except Exception:
                pass
            self._known_chat_ids.add(target)
            await self._emit_comms_event("out", target, text)
            return {
                "success": True,
                "status_code": 200,
                "message_id": data.get("id"),
                "raw": data,
            }
        except Exception as e:
            dc_logger.error("Discord send_direct error: %s", e, exc_info=True)
            return {"success": False, "status_code": 502, "error": str(e)}


class SlackChannel(Channel):
    """Slack bot — Socket Mode for incoming, Web API for outgoing."""

    @property
    def channel_type(self) -> str:
        return "slack"

    async def start(self):
        bot_token = self.config.get("bot_token", "")
        app_token = self.config.get("app_token", "")
        if not bot_token:
            logger.warning("Slack channel: no bot_token configured")
            return

        import httpx
        self._reset_runtime_health()
        self._running = True
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            timeout=10.0,
        )

        try:
            r = await self._http.post("https://slack.com/api/auth.test")
            ok = False
            try:
                ok = bool(r.json().get("ok"))
            except Exception:
                pass
            self._connected = ok
            if ok:
                self._bot_username = r.json().get("user")
        except Exception as e:
            logger.warning("Slack auth.test failed: %s", e)

        if app_token:
            asyncio.ensure_future(self._socket_mode(app_token))
            logger.info("Slack channel started (Socket Mode)")
        else:
            logger.info("Slack channel started (outbound only — add app_token for incoming)")

    async def _socket_mode(self, app_token: str):
        """Connect via Slack Socket Mode for incoming events."""
        try:
            import httpx as hx
            async with hx.AsyncClient() as client:
                resp = await client.post(
                    "https://slack.com/api/apps.connections.open",
                    headers={"Authorization": f"Bearer {app_token}"},
                )
                ws_url = resp.json().get("url", "")
                if not ws_url:
                    logger.warning("Slack: could not get Socket Mode URL")
                    return

            import websockets
            # ``websockets>=11`` returns a Connect object that is itself
            # the async context manager; the awaited result of
            # ``connect()`` is a WebSocketClientProtocol that does NOT
            # support ``__aenter__``. The previous pattern raised
            # TypeError on every modern websockets release. Retry/backoff
            # now wraps the ``async with`` directly.
            for _attempt in range(3):
                try:
                    async with websockets.connect(ws_url) as ws:
                        self._record_runtime_success()
                        async for raw in ws:
                            if not self._running:
                                break
                            event = json.loads(raw)

                            if event.get("type") == "events_api":
                                await ws.send(json.dumps({
                                    "envelope_id": event.get("envelope_id", ""),
                                }))
                                payload = event.get("payload", {}).get("event", {})
                                if payload.get("type") == "message" and not payload.get("bot_id"):
                                    await self._handle_slack_message(payload)
                    break
                except Exception:
                    if _attempt == 2:
                        raise
                    await asyncio.sleep(2 ** _attempt)

        except Exception as e:
            if self._running:
                self._record_runtime_failure("slack_socket_mode", e)
                if not self._running:
                    return
                await asyncio.sleep(self._next_backoff_seconds())
                if self._running:
                    asyncio.ensure_future(self._socket_mode(app_token))

    async def _handle_slack_message(self, event: dict):
        channel_id = event.get("channel", "")
        user_id = event.get("user", "")
        text = event.get("text", "")
        self._known_chat_ids.add(channel_id)

        if self._handler:
            channel_msg = ChannelMessage(
                channel_type="slack",
                channel_id=channel_id,
                user_id=user_id,
                text=text,
            )
            await self._emit_comms_event("in", user_id, text)
            response = await self._handler(channel_msg)
            await self.send(channel_id, response)

    async def stop(self):
        self._running = False
        if hasattr(self, "_http"):
            await self._http.aclose()

    async def send(self, channel_id: str, response: ChannelResponse):
        if not response.text:
            return
        try:
            payload = {"channel": channel_id, "text": response.text}
            if response.buttons:
                payload["blocks"] = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": response.text}},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": b.get("label", "")},
                                "action_id": b.get("action", ""),
                            }
                            for b in response.buttons
                        ],
                    },
                ]
            await self._http_with_retry(self._http, "POST", "https://slack.com/api/chat.postMessage", json=payload)
            await self._emit_comms_event("out", channel_id, response.text)
        except Exception as e:
            logging.getLogger("feral.channel.slack").error("Slack send error: %s", e)

    async def send_direct(self, to: str, text: str, reply_to: Optional[str] = None) -> dict:
        sl_logger = logging.getLogger("feral.channel.slack")
        if not getattr(self, "_http", None) or not self._running:
            return {"success": False, "status_code": 503, "error": "Slack channel not running"}
        if not text:
            return {"success": False, "status_code": 400, "error": "text is required"}
        target = (to or "").strip()
        if target.startswith("#") or target.startswith("@"):
            target = target[1:] if target[0] == "#" else target
        payload: dict = {"channel": target, "text": text}
        if reply_to:
            payload["thread_ts"] = reply_to
        try:
            r = await self._http_with_retry(
                self._http, "POST",
                "https://slack.com/api/chat.postMessage", json=payload,
            )
            data = {}
            try:
                data = r.json()
            except Exception:
                pass
            if not data.get("ok"):
                err = data.get("error", f"HTTP {getattr(r, 'status_code', '?')}")
                return {"success": False, "status_code": 502, "error": f"Slack API: {err}"}
            self._known_chat_ids.add(data.get("channel", target))
            await self._emit_comms_event("out", target, text)
            return {
                "success": True,
                "status_code": 200,
                "message_id": data.get("ts"),
                "raw": data,
            }
        except Exception as e:
            sl_logger.error("Slack send_direct error: %s", e, exc_info=True)
            return {"success": False, "status_code": 502, "error": str(e)}


class WhatsAppChannel(Channel):
    """WhatsApp Cloud API — webhook-based incoming, REST outgoing."""

    @property
    def channel_type(self) -> str:
        return "whatsapp"

    async def start(self):
        self._access_token = self.config.get("access_token", "")
        self._phone_id = self.config.get("phone_number_id", "")
        self._app_secret = self.config.get("app_secret", os.environ.get("FERAL_WHATSAPP_APP_SECRET", ""))
        if not self._access_token or not self._phone_id:
            logger.warning("WhatsApp channel: access_token and phone_number_id required")
            return

        import httpx
        self._reset_runtime_health()
        self._running = True
        self._http = httpx.AsyncClient(timeout=10.0)
        self._connected = bool(self._access_token and self._phone_id)
        logger.info("WhatsApp channel started (webhook mode)")

    async def stop(self):
        self._running = False
        if hasattr(self, "_http") and self._http:
            await self._http.aclose()

    def verify_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verify the X-Hub-Signature-256 header from WhatsApp Cloud API."""
        if not self._app_secret:
            return True
        import hashlib
        import hmac
        expected = "sha256=" + hmac.new(
            self._app_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def handle_webhook(self, body: dict) -> Optional[ChannelResponse]:
        """Process an incoming WhatsApp Cloud API webhook."""
        entries = body.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    text = msg.get("text", {}).get("body", "")
                    sender = msg.get("from", "")
                    self._known_chat_ids.add(sender)

                    if text and self._handler:
                        channel_msg = ChannelMessage(
                            channel_type="whatsapp",
                            user_id=sender,
                            text=text,
                            channel_id=self._phone_id,
                        )
                        await self._emit_comms_event("in", sender, text)
                        response = await self._handler(channel_msg)
                        if response and response.text:
                            await self.send_text(sender, response.text)
                        return response
        return None

    async def send_text(self, chat_id: str, text: str, **kwargs) -> dict:
        """Send a text message via WhatsApp Cloud API."""
        wa_logger = logging.getLogger("feral.channel.whatsapp")
        if not self._running or not self._http:
            return {"success": False, "error": "WhatsApp not running"}
        try:
            response = await self._http_with_retry(
                self._http, "POST",
                f"https://graph.facebook.com/v18.0/{self._phone_id}/messages",
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "to": chat_id,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            response.raise_for_status()
            await self._emit_comms_event("out", chat_id, text)
            return {"success": True, "data": response.json()}
        except Exception as e:
            wa_logger.error("WhatsApp send failed: %s", e)
            return {"success": False, "error": str(e)}

    async def send(self, channel_id: str, response: ChannelResponse):
        if not response.text:
            return
        await self.send_text(channel_id, response.text)

    async def send_direct(self, to: str, text: str, reply_to: Optional[str] = None) -> dict:
        if not text:
            return {"success": False, "status_code": 400, "error": "text is required"}
        target = (to or "").strip().lstrip("+")
        if not target.isdigit():
            return {
                "success": False,
                "status_code": 400,
                "error": (
                    f"WhatsApp needs a phone number in E.164 (digits only), got {to!r}."
                ),
            }
        result = await self.send_text(target, text)
        if result.get("success"):
            data = result.get("data") or {}
            msg_id = None
            try:
                msg_id = data.get("messages", [{}])[0].get("id")
            except Exception:
                pass
            return {
                "success": True,
                "status_code": 200,
                "message_id": msg_id,
                "raw": data,
            }
        return {
            "success": False,
            "status_code": 502,
            "error": result.get("error") or "WhatsApp send failed",
        }


class ChannelManager:
    """Manages all messaging channels with proper bidirectional flow."""

    CHANNEL_TYPES = {
        "telegram": TelegramChannel,
        "discord": DiscordChannel,
        "slack": SlackChannel,
        "whatsapp": WhatsAppChannel,
    }

    def __init__(self):
        self._channels: dict[str, Channel] = {}
        self._handler: Optional[MessageHandler] = None

    def set_handler(self, handler: MessageHandler):
        self._handler = handler
        for ch in self._channels.values():
            ch.set_handler(handler)

    async def start_channel(self, channel_type: str, config: dict):
        cls = self.CHANNEL_TYPES.get(channel_type)
        if not cls:
            logger.warning(f"Unknown channel type: {channel_type}")
            return

        # Stop-before-replace. Previously ``self._channels[type] = channel``
        # silently orphaned the previous channel's long-poll / websocket
        # task, which for Telegram meant two getUpdates clients competing
        # for the same bot (one of them is guaranteed to return empty
        # results and confuse downstream logic).
        existing = self._channels.get(channel_type)
        if existing is not None:
            try:
                await existing.stop()
            except Exception as exc:
                logger.warning(
                    "Error stopping previous %s channel before replace: %s",
                    channel_type, exc,
                )
            # Drop the reference even if stop() raised, so we don't keep
            # a dead channel around on top of the new one.
            self._channels.pop(channel_type, None)

        channel = cls(config)
        if self._handler:
            channel.set_handler(self._handler)

        await channel.start()
        if getattr(channel, "_degraded", False):
            logger.error(
                "Channel %s entered DEGRADED state at start: %s",
                channel_type,
                getattr(channel, "_degraded_reason", ""),
            )
            return
        if not bool(getattr(channel, "_running", False)) and not bool(getattr(channel, "_connected", False)):
            logger.warning(
                "Channel %s did not start cleanly (running=%s connected=%s); not activating",
                channel_type,
                bool(getattr(channel, "_running", False)),
                bool(getattr(channel, "_connected", False)),
            )
            return
        self._channels[channel_type] = channel

    async def stop_all(self):
        for ch in self._channels.values():
            await ch.stop()
        self._channels.clear()

    async def broadcast(self, response: ChannelResponse):
        """Send to all known chats across all active channels."""
        for ch in self._channels.values():
            for cid in ch.active_chat_ids:
                await ch.send(cid, response)

    async def send_to_channel(self, channel_type: str, channel_id: str, response: ChannelResponse):
        ch = self._channels.get(channel_type)
        if ch:
            await ch.send(channel_id, response)

    def get_channel(self, channel_type: str) -> Optional[Channel]:
        return self._channels.get(channel_type)

    @property
    def channels(self) -> dict[str, Channel]:
        return self._channels

    @property
    def active_channels(self) -> list[str]:
        return list(self._channels.keys())

    @property
    def stats(self) -> dict:
        channel_details = {}
        for ctype, ch in self._channels.items():
            row = {
                "running": bool(getattr(ch, "_running", False)),
                "connected": bool(getattr(ch, "_connected", False)),
                "known_chats": len(getattr(ch, "_known_chat_ids", []) or []),
                "degraded": bool(getattr(ch, "_degraded", False)),
                "failure_count": int(getattr(ch, "_runtime_failures", 0) or 0),
            }
            if row["degraded"]:
                row["degraded_reason"] = str(getattr(ch, "_degraded_reason", "") or "")
            bot_username = getattr(ch, "_bot_username", None)
            if bot_username:
                row["bot_username"] = bot_username
            channel_details[ctype] = row
        return {
            "active_channels": self.active_channels,
            "channel_count": len(self._channels),
            "details": channel_details,
            # Flattened per-channel shape the UI consumes via `channelStatus?.[ch.id]`.
            **channel_details,
        }
