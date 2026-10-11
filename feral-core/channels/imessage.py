"""iMessage transport boundary and inbound event normalization.

The actual macOS transport remains unavailable. The signed FERAL companion
protocol and receipt store below provide the admission boundary a future
version-pinned imsg or BlueBubbles connector must implement. Third-party
webhook/RPC formats must be translated rather than assumed equivalent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

from channels.base import Channel, ChannelMessage, ChannelResponse, ChannelSendError
from channels.inbound_receipts import InboundReceipts

logger = logging.getLogger("feral.channels.imessage")


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 16_384) -> str:
    if not isinstance(value, str):
        return ""
    try:
        if len(value.encode("utf-8")) > limit:
            return ""
    except UnicodeError:
        return ""
    return value


def _stable_id(
    payload: dict[str, Any], message: dict[str, Any], conversation: str
) -> str:
    for key in ("id", "event_id", "message_id", "guid"):
        value = _text(payload.get(key), 256) or _text(message.get(key), 256)
        if value:
            return value
    # Identical human messages are different events. Never invent an ID from
    # their text, which would silently discard legitimate repeated requests.
    return ""


def normalize_event(payload: Any) -> Optional[ChannelMessage]:
    """Normalize a FERAL companion received event.

    Delivery/read receipts are deliberately ignored here.  They need a
    separate outbound receipt store before FERAL can claim delivered/seen.
    """
    root = _obj(payload)
    event_type = _text(
        root.get("type") or root.get("event") or root.get("event_type"), 128
    ).lower()
    if event_type != "imessage.received":
        return None
    data = _obj(root.get("data")) or root
    message = _obj(
        data.get("message") or data.get("imessage") or data.get("text_message")
    )
    if not message and any(
        k in data for k in ("text", "content", "body", "sender", "sender_number")
    ):
        message = data
    conversation = _text(
        message.get("conversation_id")
        or message.get("thread_id")
        or message.get("chat_id")
        or data.get("conversation_id")
        or data.get("thread_id"),
        256,
    )
    sender = _text(
        message.get("sender")
        or message.get("sender_number")
        or message.get("from")
        or message.get("remote_number")
        or data.get("sender"),
        256,
    )
    if not conversation or not sender:
        return None
    text = _text(message.get("text") or message.get("content") or message.get("body"))
    media = message.get("attachments") or message.get("media") or []
    attachments = (
        [item for item in media if isinstance(item, dict)][:16]
        if isinstance(media, list)
        else []
    )
    # Attachments are metadata only until a reviewed retrieval/vision path
    # exists. Never replace missing or rejected instructions with a placeholder.
    if not text:
        return None
    event_id = _stable_id(root, message, conversation)
    if (
        not event_id
        or message.get("is_from_me") is not False
        or message.get("is_group") is not False
    ):
        return None
    metadata = {
        "event_id": event_id,
        "conversation_key": "imessage:" + conversation,
        "source": "imessage",
        "service": _text(message.get("service") or root.get("service"), 32),
        "is_group": bool(message.get("is_group", False)),
        "attachments": attachments,
        "delivery_state": "received",
    }
    reply_to = _text(message.get("reply_to") or message.get("reply_to_message_id"), 256)
    return ChannelMessage(
        channel_type="imessage",
        channel_id=conversation,
        user_id=sender,
        text=text,
        username=sender,
        image_b64="",
        reply_to=reply_to,
        metadata=metadata,
    )


class IMessageChannel(Channel):
    """Explicit iMessage boundary; no implicit Messages.app automation."""

    STOP_DRAIN_TIMEOUT_SECONDS = 2.0

    @property
    def channel_type(self) -> str:
        return "imessage"

    def __init__(self, config: dict):
        super().__init__(config)
        self._receipts: Optional[InboundReceipts] = None
        self._ingress_lock = asyncio.Lock()
        self._receipt_tasks: set[asyncio.Task] = set()
        self._active_ingress: Optional[asyncio.Task] = None

    async def _receipt_call(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._receipt_tasks.add(task)

        def settled(done):
            self._receipt_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(settled)
        return await asyncio.shield(task)

    async def start(self) -> None:
        backend = _text(self.config.get("backend"), 32).lower()
        endpoint = _text(self.config.get("endpoint"), 512)
        if backend not in {"imsg", "bluebubbles"} or not endpoint:
            self._connected = False
            self._running = False
            logger.warning(
                "iMessage channel is disabled: configure backend=imsg/bluebubbles and an explicit endpoint"
            )
            return
        # Transport ownership is deliberately not guessed.  The adapter will
        # be enabled only after a reviewed backend implementation is selected.
        self._connected = False
        self._running = False
        logger.warning(
            "iMessage backend %s is not enabled in this build; no messages were read or sent",
            backend,
        )

    async def ingest_event(self, payload: Any) -> bool:
        """Unsigned ingress cannot acquire execution authority."""
        return False

    async def ingest_signed_event(
        self, raw: bytes, timestamp: str, signature: str
    ) -> dict:
        """FERAL bridge v1: HMAC-SHA256 of timestamp + '.' + exact JSON bytes.

        This is FERAL's companion protocol, not a claim that third-party
        imsg/BlueBubbles webhooks already use this signing scheme.
        """
        if not self._running or self._receipts is None or self._handler is None:
            return {"accepted": False, "state": "unavailable"}
        secret = self.config.get("signing_secret")
        account = self.config.get("account_id")
        if (
            not isinstance(secret, str)
            or len(secret.encode()) < 32
            or not isinstance(account, str)
            or not account
            or type(raw) is not bytes
            or len(raw) > 262144
            or not isinstance(timestamp, str)
            or not timestamp.isdecimal()
            or len(timestamp) > 12
            or abs(time.time() - int(timestamp)) > 300
            or not isinstance(signature, str)
            or len(signature) != 64
            or any(character not in "0123456789abcdef" for character in signature)
        ):
            return {"accepted": False, "state": "unauthenticated"}
        expected = hmac.new(
            secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return {"accepted": False, "state": "unauthenticated"}
        try:
            payload = json.loads(raw)
            # Reject invalid Unicode and non-JSON numeric constants anywhere
            # in the event before creating a durable claim.
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError):
            return {"accepted": False, "state": "invalid_event"}
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
            or payload.get("account_id") != account
        ):
            return {"accepted": False, "state": "invalid_event"}
        message = normalize_event(payload)
        if message is None:
            return {"accepted": False, "state": "invalid_event"}
        # Binding a thread alone cannot authorize another sender or a group.
        if (
            message.user_id not in self._allowed_senders
            or message.channel_id not in self._allowed_chats
        ):
            return {"accepted": False, "state": "denied"}
        if self._ingress_lock.locked() or self._receipt_tasks:
            return {"accepted": False, "state": "busy"}
        async with self._ingress_lock:
            if not self._running:
                return {"accepted": False, "state": "unavailable"}
            ingress = asyncio.current_task()
            self._active_ingress = ingress
            try:
                return await self._ingest_admitted(message, payload, account)
            finally:
                if self._active_ingress is ingress:
                    self._active_ingress = None

    async def _ingest_admitted(self, message, payload, account):
        scope = hashlib.sha256(
            json.dumps([account, message.channel_id, message.user_id]).encode()
        ).hexdigest()
        event_id = message.metadata["event_id"]
        state = await self._receipt_call(self._receipts.claim, scope, event_id, payload)
        if state != "new":
            return {
                "accepted": True,
                "state": "outcome_unknown" if state == "claimed" else state,
                "duplicate": True,
            }
        try:
            if not self._running:
                raise RuntimeError("Inbound adapter stopped before execution")
            response = await self._handler(message)
            if not self._running:
                raise RuntimeError("Inbound adapter stopped before reply")
            await self.send(message.channel_id, response)
        except asyncio.CancelledError:
            await self._receipt_call(
                self._receipts.settle, scope, event_id, "outcome_unknown"
            )
            raise
        except Exception:
            await self._receipt_call(
                self._receipts.settle, scope, event_id, "outcome_unknown"
            )
            return {"accepted": True, "state": "outcome_unknown"}
        await self._receipt_call(
            self._receipts.settle, scope, event_id, "reply_unverified"
        )
        return {"accepted": True, "state": "reply_unverified"}

    async def send(self, channel_id: str, response: ChannelResponse) -> None:
        raise ChannelSendError(
            "iMessage transport is not enabled; no delivery was claimed",
            channel="imessage",
        )

    async def stop(self) -> None:
        # Revoke admission before requesting cooperative cancellation. Stopping
        # a channel cannot undo effects already dispatched by its handler.
        self._running = False
        self._connected = False
        current = asyncio.current_task()
        active = self._active_ingress
        deadline = asyncio.get_running_loop().time() + self.STOP_DRAIN_TIMEOUT_SECONDS
        if active is not None and active is not current and not active.done():
            if not active.cancelling():
                active.cancel()
            await asyncio.wait({active}, timeout=self.STOP_DRAIN_TIMEOUT_SECONDS)
        # shielded thread work is retained by _receipt_call; wait without
        # cancelling it, including settlement started during handler cancellation.
        pending = {task for task in self._receipt_tasks if not task.done()}
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if pending and remaining:
            await asyncio.wait(pending, timeout=remaining)
        unresolved = (
            self._active_ingress is not None and not self._active_ingress.done()
        ) or any(not task.done() for task in self._receipt_tasks)
        if unresolved:
            self._degraded = True
            self._degraded_reason = "Inbound work did not settle within the stop deadline; outcome remains unknown"
            logger.warning(self._degraded_reason)
