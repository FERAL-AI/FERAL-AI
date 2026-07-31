"""
Ordering metadata for voice transcript frames.

Why this exists
---------------
Transcript frames do not arrive in conversation order. OpenAI's Realtime
docs state that ``conversation.item.input_audio_transcription.completed``
"runs asynchronously with Response creation, so this event may come
before or after the Response events" — so the user's own transcript can
land *after* the assistant reply that answered it. The brain used to
forward frames in raw arrival order with no ordering metadata on the
wire, and the web client appended by arrival time, so the chat rendered
the user's question below the answer (operator report 2026-07-28).

The providers hand us most of what is needed and the brain was throwing
it away:

* ``item_id`` — stable identity for a conversation item, present on both
  transcript event families.
* ``previous_item_id`` — present on ``conversation.item.added`` and
  ``input_audio_buffer.committed``; OpenAI documents it as "used to
  maintain ordering when items are inserted". Chained together the links
  form a linked list that *is* the canonical order, for free.

Gemini Live and the chained whisper path supply neither, so this tracker
also stamps a brain-assigned per-session monotonic ``seq``. For those
providers the brain drives the turn boundaries itself, so the order in
which it commits frames is authoritative and ``seq`` is a sound
fallback.

Scope
-----
This module only *derives and remembers* ordering metadata. Applying it
is the client's job (``feral-client-v2/src/lib/transcriptOrder.js``),
because only the client knows what is already on screen.

``TRANSCRIPT_ORDER`` is a process-wide default instance. All state is
keyed by session id, and the three emit sites that need it
(``voice/realtime_proxy.py``, ``voice/router.py``,
``voice/gemini_realtime.py``) live on separate objects with no shared
parent to thread an instance through.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("feral.voice.order")

# Cap the per-session item graph. A long voice session can accumulate
# thousands of conversation items and clients only ever resolve
# ``previous_item_id`` against the recent tail (anything older is
# already rendered and settled). Trimming keeps a day-long session from
# growing the map without bound.
MAX_ITEMS_PER_SESSION = 512


class TranscriptOrder:
    """Per-session transcript ordering state.

    Tracks two independent things:

    * the provider's ``item_id -> previous_item_id`` graph, populated
      from the conversation-item events that carry it, and
    * a monotonic ``seq`` counter used as the provider-agnostic
      fallback.
    """

    def __init__(self) -> None:
        # session_id -> next seq value to hand out
        self._seq: dict[str, int] = {}
        # session_id -> {item_id: previous_item_id}
        self._prev: dict[str, dict[str, str]] = {}
        # session_id -> insertion-ordered item ids (for trimming)
        self._items: dict[str, list[str]] = {}

    def next_seq(self, session_id: str) -> int:
        """Return the next monotonic sequence number for ``session_id``.

        Starts at 0 and never repeats within a session. Callers stamp
        this on every emitted transcript frame so a client can order
        frames even when the provider gave no item identity at all.
        """
        current = self._seq.get(session_id, 0)
        self._seq[session_id] = current + 1
        return current

    def note_item(
        self, session_id: str, item_id: str, previous_item_id: str = "",
    ) -> None:
        """Record that ``item_id`` follows ``previous_item_id``.

        Called from the conversation-item events. A blank ``item_id`` is
        ignored (nothing to key on). A blank ``previous_item_id`` is
        still recorded: it means "this item is the head", which is
        distinct from "we never saw this item".
        """
        if not item_id:
            return
        prev_map = self._prev.setdefault(session_id, {})
        items = self._items.setdefault(session_id, [])
        if item_id not in prev_map:
            items.append(item_id)
        # A re-announced item (``added`` then ``done``) may arrive with
        # the link absent the second time; never overwrite a known
        # parent with a blank.
        if previous_item_id or item_id not in prev_map:
            prev_map[item_id] = previous_item_id

        if len(items) > MAX_ITEMS_PER_SESSION:
            overflow = len(items) - MAX_ITEMS_PER_SESSION
            for stale in items[:overflow]:
                prev_map.pop(stale, None)
            del items[:overflow]

    def previous_of(self, session_id: str, item_id: str) -> str:
        """Return the recorded predecessor of ``item_id``, or ``""``.

        Blank means either "head of the conversation" or "never seen" —
        the client treats both the same way (fall back to ``seq``), so
        they do not need to be distinguished on the wire.
        """
        if not item_id:
            return ""
        return self._prev.get(session_id, {}).get(item_id, "")

    def forget(self, session_id: str) -> None:
        """Drop all ordering state for a finished session."""
        self._seq.pop(session_id, None)
        self._prev.pop(session_id, None)
        self._items.pop(session_id, None)


# Process-wide default. See the module docstring for why this is shared
# rather than threaded through three constructors.
TRANSCRIPT_ORDER = TranscriptOrder()
