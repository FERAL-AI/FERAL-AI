"""Newline-delimited JSON-RPC 2.0 over a pair of asyncio streams.

Why this file exists instead of a dependency
============================================
ACP's transport is exactly "one JSON-RPC 2.0 message per line, UTF-8, no
Content-Length header". There is a Python SDK on PyPI
(``agent-client-protocol``) that would supply this plus pydantic models
for the whole schema, but:

* it lags the protocol badly (0.11.1 while the TypeScript SDK that the
  real agents build against is at 1.3.x), and version skew in a *typed*
  model layer is worse than no model layer, because an unknown field
  becomes a validation error instead of a dict key nobody reads;
* the framing is the part we would actually be importing, and it is the
  ~200 lines below.

So this module owns the wire and ``bridges/acp.py`` treats payloads as
plain dicts, forward-compatible with fields we have never heard of.

Concurrency contract
====================
A JSON-RPC peer is symmetric: both ends may send requests. The read loop
therefore must never block on a handler, or an inbound
``session/request_permission`` (which waits on a human) would deadlock
the outbound ``session/prompt`` it belongs to. Every inbound request runs
in its own task; the read loop only parses and dispatches.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("feral.bridges.jsonrpc")

# JSON-RPC 2.0 reserved codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A single tool result from a coding agent (a full file read, a large
# diff) blows straight through asyncio's 64 KiB default stream limit and
# surfaces as a ``LimitOverrunError`` that looks like a protocol bug. 32
# MiB is well past any real ACP line and still bounded.
DEFAULT_STREAM_LIMIT = 32 * 1024 * 1024


class JsonRpcError(Exception):
    """A JSON-RPC error, either received from the peer or raised by a handler.

    Raising this from a request handler sends a well-formed error response
    instead of the generic internal error.
    """

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


class NdjsonRpcPeer:
    """One end of a newline-delimited JSON-RPC 2.0 conversation.

    ``request_handler`` and ``notification_handler`` receive
    ``(method, params)`` and are only consulted for messages the peer
    sends *to us*. A missing request handler answers METHOD_NOT_FOUND,
    which is the correct fail-closed behaviour: an agent asking us to do
    something we do not implement must be told no, not left hanging.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        request_handler: Optional[Callable[[str, dict], Awaitable[Any]]] = None,
        notification_handler: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        name: str = "peer",
    ):
        self._reader = reader
        self._writer = writer
        self._request_handler = request_handler
        self._notification_handler = notification_handler
        self._name = name

        self._ids = itertools.count(1)
        self._pending: dict[Any, asyncio.Future] = {}
        self._inflight: set[asyncio.Task] = set()
        self._read_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._close_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._read_task is None:
            self._read_task = asyncio.ensure_future(self._read_loop())

    async def close(self, reason: str = "closed") -> None:
        """Stop reading, fail every pending request, drop the writer.

        Pending futures are failed rather than left to time out: a caller
        blocked on ``session/prompt`` when the child dies should learn
        that immediately, with the reason, not after its own timeout.
        """
        if self._closed:
            return
        self._closed = True
        self._close_reason = reason

        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
            self._read_task = None

        for task in list(self._inflight):
            task.cancel()
        self._inflight.clear()

        self._fail_pending(reason)

        with contextlib.suppress(Exception):
            self._writer.close()

    def _fail_pending(self, reason: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(
                    JsonRpcError(INTERNAL_ERROR, f"connection {reason}")
                )
        self._pending.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        if self._closed:
            raise JsonRpcError(
                INTERNAL_ERROR, f"connection {self._close_reason or 'closed'}"
            )
        request_id = next(self._ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params if params is not None else {},
        }
        try:
            await self._send(message)
        except Exception:
            self._pending.pop(request_id, None)
            raise

        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            # Best effort: tell the peer to stop working on it. ACP
            # defines $/cancel_request for exactly this.
            with contextlib.suppress(Exception):
                await self.notify("$/cancel_request", {"id": request_id})
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        if self._closed:
            return
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params if params is not None else {},
            }
        )

    async def _send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self._writer.write(line.encode("utf-8") + b"\n")
            await self._writer.drain()

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    raw = await self._reader.readline()
                except (asyncio.LimitOverrunError, ValueError) as exc:
                    logger.error("%s: oversized JSON-RPC line: %s", self._name, exc)
                    break
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    # Agents occasionally emit a stray banner line on
                    # stdout. Log and keep going rather than tearing the
                    # session down over one unparseable line.
                    logger.warning(
                        "%s: non-JSON line on stdout: %.200s",
                        self._name,
                        line.decode("utf-8", "replace"),
                    )
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s: read loop died", self._name)
        finally:
            if not self._closed:
                self._fail_pending("eof")

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if "method" in message:
            if message.get("id") is None:
                self._spawn(self._handle_notification(message))
            else:
                self._spawn(self._handle_request(message))
            return
        self._resolve_response(message)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    def _resolve_response(self, message: dict) -> None:
        future = self._pending.pop(message.get("id"), None)
        if future is None or future.done():
            return
        if "error" in message and message["error"] is not None:
            err = message["error"] or {}
            future.set_exception(
                JsonRpcError(
                    int(err.get("code", INTERNAL_ERROR)),
                    str(err.get("message", "unknown error")),
                    err.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))

    async def _handle_notification(self, message: dict) -> None:
        if self._notification_handler is None:
            return
        try:
            await self._notification_handler(
                str(message.get("method")), message.get("params") or {}
            )
        except Exception:
            logger.exception(
                "%s: notification handler failed for %s",
                self._name,
                message.get("method"),
            )

    async def _handle_request(self, message: dict) -> None:
        request_id = message.get("id")
        method = str(message.get("method"))
        params = message.get("params") or {}

        if self._request_handler is None:
            await self._respond_error(
                request_id, JsonRpcError(METHOD_NOT_FOUND, f"no handler for {method}")
            )
            return

        try:
            result = await self._request_handler(method, params)
        except JsonRpcError as exc:
            await self._respond_error(request_id, exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("%s: request handler failed for %s", self._name, method)
            await self._respond_error(
                request_id, JsonRpcError(INTERNAL_ERROR, str(exc) or type(exc).__name__)
            )
            return

        with contextlib.suppress(Exception):
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "result": result}
            )

    async def _respond_error(self, request_id: Any, error: JsonRpcError) -> None:
        with contextlib.suppress(Exception):
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "error": error.to_payload()}
            )
