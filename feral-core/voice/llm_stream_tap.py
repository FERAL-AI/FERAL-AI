"""Watch the orchestrator's token stream from outside the orchestrator.

The problem
-----------
``Orchestrator.handle_command_stream`` returns when the whole turn is
over. It publishes tokens as they arrive, but it publishes them by
calling ``self.send(session_id, FeralMessage(type="stream_delta"))``,
straight out to the client. There is no return value to iterate, no
callback parameter, and no subscriber registry.

The chained voice pipeline needs those tokens as they land, because
synthesising sentence by sentence is the difference between hearing
the reply after the model finishes and hearing it while the model is
still writing.

The approach
------------
Wrap ``send``. The tap installs itself once per orchestrator instance,
forwards every message onward untouched, and additionally hands
``stream_delta`` payloads to whichever session subscribed. When no one
is subscribed the wrapper is a single dict lookup on the way past.

This is a wrapper, not a patch of behaviour: the original callable is
always invoked, its return value is always returned, and a subscriber
that raises can never break delivery to the client.

It is still a wrapper on someone else's attribute, which is a seam
this lane could not open properly because ``agents/orchestrator.py``
is outside it. The clean version is a first-class subscriber API on
the orchestrator; see the lane report. Until then this is contained,
reversible (``uninstall``) and observation-only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger("feral.voice.llm_tap")

_TAP_ATTR = "_feral_chained_stream_tap"


def _frame_type(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("type") or "")
    return str(getattr(message, "type", "") or "")


def _frame_payload(message: Any) -> dict:
    if isinstance(message, dict):
        payload = message.get("payload")
    else:
        payload = getattr(message, "payload", None)
    return payload if isinstance(payload, dict) else {}


class LLMStreamTap:
    """Per-orchestrator fan-out of ``stream_delta`` text.

    Use :meth:`attach` rather than constructing directly; it reuses the
    tap already installed on a given handle so N sessions do not stack
    N wrappers around ``send``.
    """

    def __init__(self, handle: Any):
        self._handle = handle
        self._original: Callable | None = None
        self._subscribers: dict[str, Callable[[str], None]] = {}
        self._installed = False

    @classmethod
    def attach(cls, handle: Any) -> "LLMStreamTap | None":
        """Return the tap for *handle*, installing one if needed.

        Returns ``None`` when the handle has no wrappable ``send``,
        which is the honest answer for a stub orchestrator in a test
        or an LLM handle that predates streaming. Callers fall back to
        the buffered path.
        """
        if handle is None:
            return None
        existing = getattr(handle, _TAP_ATTR, None)
        if isinstance(existing, cls):
            return existing
        send = getattr(handle, "send", None)
        if send is None or not callable(send):
            return None
        tap = cls(handle)
        if not tap.install():
            return None
        try:
            setattr(handle, _TAP_ATTR, tap)
        except Exception:
            # Slotted or frozen handle. The tap still works for this
            # caller, it just cannot be shared, so do not cache it.
            logger.debug("LLM stream tap could not be cached on the handle")
        return tap

    def install(self) -> bool:
        if self._installed:
            return True
        original = getattr(self._handle, "send", None)
        if original is None or not callable(original):
            return False
        self._original = original

        async def _tapped(session_id: str, message: Any = None, *args, **kwargs):
            self._dispatch(session_id, message)
            result = original(session_id, message, *args, **kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result

        try:
            setattr(self._handle, "send", _tapped)
        except Exception:
            logger.debug("LLM stream tap could not wrap send()", exc_info=True)
            self._original = None
            return False
        self._installed = True
        return True

    def uninstall(self) -> None:
        if not self._installed or self._original is None:
            return
        try:
            setattr(self._handle, "send", self._original)
            if getattr(self._handle, _TAP_ATTR, None) is self:
                setattr(self._handle, _TAP_ATTR, None)
        except Exception:
            logger.debug("LLM stream tap could not restore send()", exc_info=True)
        self._installed = False
        self._original = None
        self._subscribers.clear()

    def _dispatch(self, session_id: str, message: Any) -> None:
        if not self._subscribers:
            return
        callback = self._subscribers.get(session_id)
        if callback is None:
            return
        try:
            if _frame_type(message) != "stream_delta":
                return
            payload = _frame_payload(message)
            delta = payload.get("delta") or ""
            if delta:
                callback(str(delta))
        except Exception:
            # Never let an observer break the client's own frame.
            logger.debug("LLM stream tap subscriber raised", exc_info=True)

    def subscribe(self, session_id: str, callback: Callable[[str], None]) -> None:
        self._subscribers[session_id] = callback

    def unsubscribe(self, session_id: str) -> None:
        self._subscribers.pop(session_id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class DeltaCollector:
    """Turns tap callbacks into an awaitable async stream.

    The tap fires synchronously from inside ``send``; the pipeline
    consumes asynchronously. This is the buffer between them, and it
    is unbounded on purpose: back-pressuring the orchestrator's send
    path to slow down TTS would stall the user's chat transcript too.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self.total_chars = 0

    def on_delta(self, delta: str) -> None:
        self.total_chars += len(delta)
        try:
            self._queue.put_nowait(delta)
        except Exception:
            logger.debug("delta queue rejected a chunk", exc_info=True)

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass

    async def stream(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item
