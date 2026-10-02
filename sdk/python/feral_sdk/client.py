"""
FeralClient — Connect to a running FERAL Brain via HTTP + WebSocket.

Usage::

    async with FeralClient("http://localhost:9090") as client:
        response = await client.chat("What's the weather?")
        print(response)

        dashboard = await client.get_dashboard()
        print(dashboard["skills_count"])
"""

from __future__ import annotations
import asyncio
import json
import logging
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, AsyncContextManager, AsyncIterator, Protocol, TypeVar
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx

logger = logging.getLogger("feral.sdk.client")
# websockets debug logging includes sent auth-frame bodies. Give this transport
# a private disabled logger rather than changing the application's logger tree.
_websocket_logger = logging.Logger("feral.sdk.websocket")
_websocket_logger.disabled = True


class TurnProcessingOutcome(StrEnum):
    COMPLETED = "completed"
    AWAITING_APPROVAL = "awaiting_approval"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True)
class ChatTurnReceipt:
    """Whole-turn processing receipt; never proof that external effects succeeded."""

    request_id: str
    turn_id: str
    session_id: str
    processing_outcome: TurnProcessingOutcome
    final_text: str
    action_outcome: str
    approval_request_ids: tuple[str, ...]
    replayed: bool
    durable: bool = True
    contract_version: int = 1


class ChatTurnError(RuntimeError):
    """Bounded SDK/protocol failure, optionally carrying a server receipt."""

    def __init__(self, code: str, *, receipt: ChatTurnReceipt | None = None):
        self.code = code
        self.receipt = receipt
        super().__init__(f"FERAL chat {code}; external action outcomes must be reconciled before retrying")


class ChatTurnTimeout(TimeoutError):
    """The total deadline expired; partial prose is not returned as success."""

    code = "timeout"

    def __init__(self):
        super().__init__("FERAL chat deadline expired; processing and external action outcomes may be unknown")


def _session_identity(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("session_id must be nonempty trimmed text without control characters, at most 1024 characters")
    return value


def _chat_deadline(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError("chat timeout must be a finite positive number")
    return float(value)


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


@dataclass
class _ChatChannel:
    session_id: str
    capability_id: str
    ready: asyncio.Future[None]
    socket: _ChatSocket | None = None
    connection: AsyncContextManager[_ChatSocket] | None = None
    reader: asyncio.Task[None] | None = None
    pending: asyncio.Future[ChatTurnReceipt] | None = None
    request_id: str | None = None
    turn_id: str | None = None
    lost: bool = False


class _ChatSocket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


_FutureValue = TypeVar("_FutureValue")


def _consume_future(future: asyncio.Future[_FutureValue]) -> None:
    if not future.cancelled():
        future.exception()


class FeralClient:
    """HTTP + WebSocket client for the FERAL Brain API."""

    def __init__(
        self,
        base_url: str = "http://localhost:9090",
        *,
        bearer_token: str | None = None,
        timeout: float | httpx.Timeout = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        session_id: str | None = None,
        chat_timeout: float = 60,
        max_chat_threads: int = 8,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username is not None or parsed.query or parsed.fragment:
            raise ValueError("base_url must be HTTP(S) without URL credentials, query or fragment")
        self.base_url = base_url.rstrip("/")
        self.ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://") + "/v1/session"
        headers = {"Accept": "application/json"}
        if bearer_token is not None:
            if not bearer_token.strip() or any(c in bearer_token for c in "\r\n"):
                raise ValueError("bearer_token must be a nonempty, single-line credential")
            headers["Authorization"] = f"Bearer {bearer_token}"
        sdk_session = _session_identity(session_id) if session_id is not None else str(uuid4())
        sdk_deadline = _chat_deadline(chat_timeout)
        if type(max_chat_threads) is not int or not 1 <= max_chat_threads <= 64:
            raise ValueError("max_chat_threads must be an integer from 1 to 64")
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers=headers, transport=transport,
            follow_redirects=False,
        )
        self._ws = None
        self._bearer_token = bearer_token
        self._session_id = sdk_session
        self._chat_timeout = sdk_deadline
        self._chat_sockets: set[_ChatSocket] = set()
        self._chat_readers: set[asyncio.Task[None]] = set()
        self._chat_sessions: set[str] = set()
        self._channels: dict[str, _ChatChannel] = {}
        self._lost_threads: set[str] = set()
        self._max_chat_threads = max_chat_threads
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        self._closed = True
        await asyncio.gather(*(self._retire_channel(channel, ChatTurnError("client_closed"))
                               for channel in tuple(self._channels.values())))
        # A reader may have removed a lost channel while still releasing its
        # transport. Keep its reference until cleanup finishes, including idle loss.
        if self._chat_readers:
            await asyncio.gather(*tuple(self._chat_readers), return_exceptions=True)
        await self._http.aclose()
        if self._ws:
            await self._ws.close()
        if self._chat_sockets:
            await asyncio.gather(*(ws.close() for ws in tuple(self._chat_sockets)), return_exceptions=True)

    async def close_thread(self, session_id: str | None = None) -> None:
        """Release an owned live thread; its old context must not silently reopen."""
        sid = _session_identity(session_id) if session_id is not None else self._session_id
        channel = self._channels.get(sid)
        if channel is not None:
            await self._retire_channel(channel, ChatTurnError("context_lost"))

    def _mark_channel_lost(self, channel: _ChatChannel, error: Exception) -> None:
        channel.lost = True
        self._lost_threads.add(channel.session_id)
        if self._channels.get(channel.session_id) is channel:
            self._channels.pop(channel.session_id)
        for future in (channel.ready, channel.pending):
            if future is not None and not future.done():
                future.set_exception(error)

    async def _retire_channel(self, channel: _ChatChannel, error: Exception) -> None:
        already_lost = channel.lost
        self._mark_channel_lost(channel, error)
        reader = channel.reader
        # A reader that detected loss is already closing its context. Do not
        # cancel that cleanup when its awaiting caller receives the error.
        if reader is not None and reader is not asyncio.current_task() and not already_lost:
            reader.cancel()
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
        await self._release_transport(channel)

    async def _release_transport(self, channel: _ChatChannel) -> None:
        connection, channel.connection = channel.connection, None
        try:
            if channel.socket is not None:
                await channel.socket.close()
        except Exception:
            logger.debug("Chat transport cleanup failed; thread remains unavailable")
        finally:
            if channel.socket is not None:
                self._chat_sockets.discard(channel.socket)
        try:
            if connection is not None:
                await connection.__aexit__(None, None, None)
        except Exception:
            logger.debug("Chat connection context cleanup failed; thread remains unavailable")

    async def health(self) -> dict:
        """Check brain health."""
        return await self._request_object("GET", "/health")

    async def _request_json(self, method: str, path: str, **kwargs) -> Any:
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            # Do not include a response body or credential in diagnostics.
            raise ValueError("Brain returned invalid JSON") from None

    async def _request_object(self, method: str, path: str, **kwargs) -> dict:
        data = await self._request_json(method, path, **kwargs)
        if not isinstance(data, dict):
            raise ValueError("Brain returned a JSON value where an object was required")
        return data

    @staticmethod
    def _rows(data: Any) -> list[dict]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("Brain returned an invalid record list")
        return data

    async def get_dashboard(self) -> dict:
        """Get aggregated dashboard data."""
        return await self._request_object("GET", "/api/dashboard")

    async def get_system_info(self) -> dict:
        """Get system info (version, memory stats, etc.)."""
        return await self._request_object("GET", "/api/system/info")

    async def chat(self, message: str, session_id: str | None = None, *, timeout: float | None = None) -> str:
        """Return final prose after processing completed, not a task-effect receipt.

        Use chat_turn to inspect approval/refusal/unknown outcomes explicitly.
        A server without terminal-contract support is never treated as success.
        """
        receipt = await self.chat_turn(message, session_id, timeout=timeout)
        if receipt.processing_outcome != TurnProcessingOutcome.COMPLETED:
            raise ChatTurnError(receipt.processing_outcome.value, receipt=receipt)
        if not receipt.final_text:
            raise ChatTurnError("missing_response", receipt=receipt)
        return receipt.final_text

    async def _open_channel(self, channel: _ChatChannel, duration: float) -> None:
        import websockets
        url = urlsplit(self.ws_url)
        ws_url = urlunsplit((url.scheme, url.netloc, url.path,
                            urlencode({"session_id": channel.session_id}), ""))
        channel.connection = websockets.connect(
            ws_url, open_timeout=duration, close_timeout=min(duration, 1), logger=_websocket_logger,
        )
        channel.socket = await channel.connection.__aenter__()
        if channel.lost or self._closed:
            await channel.socket.close()
            raise ChatTurnError("client_closed" if self._closed else "context_lost")
        self._chat_sockets.add(channel.socket)
        if self._bearer_token is not None:
            await channel.socket.send(json.dumps({"type": "auth", "token": self._bearer_token}))
        await channel.socket.send(json.dumps({"type": "req", "id": channel.capability_id,
                                            "method": "chat.capabilities", "params": {}}))

    async def _read_channel(self, channel: _ChatChannel, duration: float) -> None:
        from websockets.exceptions import ConnectionClosed, WebSocketException
        try:
            await self._open_channel(channel, duration)
            socket = channel.socket
            if socket is None:
                raise ChatTurnError("connection_failed")
            while True:
                try:
                    frame = json.loads(await socket.recv())
                except (ValueError, TypeError):
                    raise ChatTurnError("invalid_json") from None
                self._dispatch_chat_frame(channel, frame)
        except asyncio.CancelledError:
            # Event-loop/application shutdown may cancel an idle owned reader
            # without going through close_thread. It still invalidates context.
            if not channel.lost:
                self._mark_channel_lost(channel, ChatTurnError("context_lost"))
            raise
        except ConnectionClosed as exc:
            close = getattr(exc, "rcvd", None)
            self._mark_channel_lost(channel, ChatTurnError(
                "unauthorized" if getattr(close, "code", None) == 4001 else "connection_closed"))
        except (OSError, WebSocketException):
            self._mark_channel_lost(channel, ChatTurnError("connection_failed"))
        except ChatTurnError as exc:
            self._mark_channel_lost(channel, exc)
        except Exception:
            # Never put remote bodies or transport exception credentials in errors.
            self._mark_channel_lost(channel, ChatTurnError("connection_failed"))
        finally:
            await self._release_transport(channel)

    def _dispatch_chat_frame(self, channel: _ChatChannel, frame: object) -> None:
        sid = channel.session_id
        if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
            raise ChatTurnError("invalid_frame")
        kind = frame["type"]
        if kind == "res" and frame.get("id") == channel.capability_id and not channel.ready.done():
            capability = frame.get("payload")
            if (frame.get("ok") is not True or not isinstance(capability, dict)
                    or not isinstance(capability.get("turn_contract_versions"), list)
                    or not any(type(version) is int and version == 1 for version in capability["turn_contract_versions"])
                    or capability.get("durable_receipts") is not True
                    or capability.get("whole_turn_terminal") is not True or capability.get("session_id") != sid):
                raise ChatTurnError("unsupported_turn_contract")
            channel.ready.set_result(None)
            return
        payload = frame.get("payload", {})
        if not isinstance(payload, dict):
            raise ChatTurnError("invalid_payload")
        if kind == "error":
            if channel.request_id is not None and payload.get("request_id") == channel.request_id:
                code = payload.get("code")
                known = ("chat_turn_request_conflict", "chat_turn_invalid_request", "chat_turn_quota", "chat_turn_receipt_unavailable")
                raise ChatTurnError(code if code in known else "request_rejected")
            return
        if kind not in ("chat_turn_accepted", "chat_turn_terminal"):
            return
        if not channel.ready.done():
            raise ChatTurnError("unnegotiated_turn_receipt")
        pending = channel.pending
        # An old/foreign receipt cannot satisfy a later invocation or idle thread.
        if pending is None or pending.done() or channel.request_id is None or payload.get("request_id") != channel.request_id:
            return
        if (frame.get("session_id") != sid or payload.get("session_id") != sid
                or type(payload.get("contract_version")) is not int or payload["contract_version"] != 1
                or payload.get("durable") is not True or not _canonical_uuid(payload.get("turn_id"))
                or type(payload.get("replayed")) is not bool):
            raise ChatTurnError("invalid_turn_receipt")
        if kind == "chat_turn_accepted":
            if payload.get("status") != "accepted" or (channel.turn_id is not None and channel.turn_id != payload["turn_id"]):
                raise ChatTurnError("invalid_turn_acceptance")
            channel.turn_id = payload["turn_id"]
            return
        if channel.turn_id is None or payload["turn_id"] != channel.turn_id:
            raise ChatTurnError("uncorrelated_terminal")
        raw_outcome = payload.get("processing_outcome")
        if not isinstance(raw_outcome, str):
            raise ChatTurnError("invalid_turn_outcome")
        try:
            outcome = TurnProcessingOutcome(raw_outcome)
        except ValueError:
            raise ChatTurnError("invalid_turn_outcome") from None
        approvals, final_text, action = payload.get("approval_request_ids"), payload.get("final_text"), payload.get("action_outcome")
        if (not isinstance(final_text, str) or action not in ("not_asserted", "unknown")
                or not isinstance(approvals, list) or any(not isinstance(item, str) or not item for item in approvals)):
            raise ChatTurnError("invalid_turn_receipt")
        pending.set_result(ChatTurnReceipt(channel.request_id, channel.turn_id, sid, outcome,
                                          final_text, action, tuple(approvals), payload["replayed"]))

    async def chat_turn(
        self, message: str, session_id: str | None = None, *, timeout: float | None = None,
    ) -> ChatTurnReceipt:
        """Run a turn on a retained live thread. Lost threads never reconnect.

        No automatic approvals, retries or reconnects. Cancelling this coroutine
        closes its thread transport; server effect cancellation is not asserted.
        """
        import websockets
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be nonempty text")
        sid = _session_identity(session_id) if session_id is not None else self._session_id
        duration = _chat_deadline(timeout) if timeout is not None else self._chat_timeout
        if self._closed:
            raise ChatTurnError("client_closed")
        if sid in self._lost_threads:
            raise ChatTurnError("context_lost")
        if sid in self._chat_sessions:
            raise ChatTurnError("session_busy")
        channel = self._channels.get(sid)
        if channel is None:
            if len(self._channels) >= self._max_chat_threads:
                raise ChatTurnError("thread_quota")
            if len(self._channels) + len(self._lost_threads) >= 1024:
                raise ChatTurnError("thread_identity_quota")
            ready = asyncio.get_running_loop().create_future()
            ready.add_done_callback(_consume_future)
            channel = _ChatChannel(sid, str(uuid4()), ready)
            self._channels[sid] = channel
        self._chat_sessions.add(sid)
        try:
            async with asyncio.timeout(duration):
                if channel.reader is None:
                    channel.reader = asyncio.create_task(self._read_channel(channel, duration))
                    self._chat_readers.add(channel.reader)
                    channel.reader.add_done_callback(self._chat_readers.discard)
                await asyncio.shield(channel.ready)
                if channel.lost or self._closed or channel.socket is None:
                    raise ChatTurnError("client_closed" if self._closed else "context_lost")
                channel.request_id, channel.turn_id = str(uuid4()), None
                channel.pending = asyncio.get_running_loop().create_future()
                channel.pending.add_done_callback(_consume_future)
                await channel.socket.send(json.dumps({
                    "msg_id": channel.request_id, "type": "text_command", "session_id": sid,
                    "payload": {"text": message, "turn_contract_version": 1},
                }))
                return await asyncio.shield(channel.pending)
        except TimeoutError:
            await self._retire_channel(channel, ChatTurnTimeout())
            raise ChatTurnTimeout() from None
        except asyncio.CancelledError:
            await self._retire_channel(channel, ChatTurnError("transport_cancelled"))
            raise
        except ChatTurnError:
            await self._retire_channel(channel, ChatTurnError("context_lost"))
            raise
        except (OSError, websockets.exceptions.WebSocketException):
            await self._retire_channel(channel, ChatTurnError("connection_failed"))
            raise ChatTurnError("connection_failed") from None
        finally:
            self._chat_sessions.discard(sid)
            channel.pending = None
            channel.request_id = None
            channel.turn_id = None

    async def list_skills(self) -> list[dict]:
        """List all registered skills."""
        data = await self._request_json("GET", "/skills")
        return self._rows(data.get("skills") if isinstance(data, dict) else data)

    async def search_memory(self, query: str, limit: int = 10) -> list[dict]:
        """Search the agent's memory."""
        data = await self._request_object("GET", "/api/memory/search", params={"q": query, "limit": limit})
        return self._rows(data.get("results"))

    async def create_note(
        self, content: str, tags: list[str] | None = None, *,
        session_id: str | None = None, confirm: bool = False,
    ) -> dict:
        """Save a note through policy gates; return the tool result envelope."""
        return await self.invoke_skill(
            "notes_memory", "save_note", {"content": content, "tags": tags or []},
            session_id=session_id, confirm=confirm,
        )

    async def list_conversations(self, limit: int = 20) -> list[dict]:
        """List conversation threads."""
        data = await self._request_object("GET", "/api/conversations", params={"limit": limit})
        return self._rows(data.get("conversations"))

    async def invoke_skill(
        self, skill_id: str, endpoint: str, args: dict | None = None, *,
        session_id: str | None = None, confirm: bool = False,
    ) -> dict:
        """Invoke through REST policy gates, without confirming or retrying implicitly.

        HTTP failures raise httpx.HTTPStatusError. An HTTP-200 tool rejection is
        returned unchanged; inspect success/status_code/error before reporting an
        action as executed. confirm=True is an explicit caller assertion of review.
        """
        if type(confirm) is not bool:
            raise ValueError("confirm must be a Boolean")
        if args is not None and not isinstance(args, dict):
            raise ValueError("args must be an object")
        body = {"skill_id": skill_id, "endpoint": endpoint, "args": args or {}, "confirm": confirm}
        if session_id is not None:
            body["session_id"] = session_id
        return await self._request_object("POST", "/api/tools/execute", json=body)
