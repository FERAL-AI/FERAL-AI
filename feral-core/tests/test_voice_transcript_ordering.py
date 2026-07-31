"""Ordering metadata + role attribution on the voice transcript wire.

Operator report (2026-07-28): using voice with the chat pane open, the
spoken transcription rendered BELOW the assistant reply that answered
it. OpenAI's Realtime docs are explicit that
``conversation.item.input_audio_transcription.completed`` "runs
asynchronously with Response creation, so this event may come before or
after the Response events" — so arrival order is not conversation order,
and the brain was forwarding frames with zero ordering metadata on the
wire.

Two independent defects are pinned here:

Bug A (ordering)
    ``item_id`` was read off no transcript event at all, and
    ``previous_item_id`` (which OpenAI documents as "used to maintain
    ordering when items are inserted") was never consumed, even though
    ``conversation.item.added`` was already routed to a handler whose
    entire body was a ``logger.debug``. Clients therefore had nothing
    but arrival time to sort by. The wire now carries ``item_id``,
    ``previous_item_id`` and a brain-assigned monotonic ``seq``.

Bug B (role)
    ``VoiceRouter._handle_whisper_path`` built the web-client frame as
    ``TranscriptPayload(text=transcript, is_partial=False)`` with NO
    ``role``, and ``TranscriptPayload`` defaults ``role`` to
    ``"assistant"`` — so the user's own speech was tagged as the
    assistant on the web path, while the node branch three lines below
    tagged the same text ``"user"``. ``tests/test_voice_transcript_role_wire.py``
    covers only the realtime path, which is why this survived.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from models.protocol import TranscriptPayload
from voice.realtime_proxy import RealtimeProxy, RealtimeSession
from voice.router import VoiceRouter
from voice.transcript_order import (
    MAX_ITEMS_PER_SESSION,
    TRANSCRIPT_ORDER,
    TranscriptOrder,
)


@pytest.fixture(autouse=True)
def _reset_shared_order():
    """``TRANSCRIPT_ORDER`` is process-wide; keep seq assertions isolated.

    Production session ids are UUIDs so they never collide, but tests
    reuse readable ids and would otherwise inherit each other's
    counters.
    """
    yield
    for session_id in ("sess-web", "sess-node", "sess-whisper",
                       "sess-both", "sess-seq"):
        TRANSCRIPT_ORDER.forget(session_id)


# ─────────────────────────────────────────────
# TranscriptOrder primitive
# ─────────────────────────────────────────────

def test_next_seq_is_monotonic_and_per_session():
    order = TranscriptOrder()
    assert [order.next_seq("a") for _ in range(3)] == [0, 1, 2]
    # A second session gets its own counter, not a shared global one.
    assert order.next_seq("b") == 0
    assert order.next_seq("a") == 3


def test_note_item_records_and_resolves_links():
    order = TranscriptOrder()
    order.note_item("s", "item_b", "item_a")
    assert order.previous_of("s", "item_b") == "item_a"
    # Unknown / head items resolve to blank rather than raising.
    assert order.previous_of("s", "item_a") == ""
    assert order.previous_of("s", "") == ""


def test_note_item_does_not_overwrite_known_parent_with_blank():
    """``added`` then ``done`` for the same item must not lose the link."""
    order = TranscriptOrder()
    order.note_item("s", "item_b", "item_a")
    order.note_item("s", "item_b", "")
    assert order.previous_of("s", "item_b") == "item_a"


def test_item_graph_is_bounded():
    order = TranscriptOrder()
    for i in range(MAX_ITEMS_PER_SESSION + 50):
        order.note_item("s", f"item_{i}", f"item_{i - 1}" if i else "")
    # Oldest entries are trimmed, recent tail (what clients resolve
    # against) is retained.
    assert order.previous_of("s", "item_0") == ""
    last = MAX_ITEMS_PER_SESSION + 49
    assert order.previous_of("s", f"item_{last}") == f"item_{last - 1}"


def test_forget_drops_session_state():
    order = TranscriptOrder()
    order.next_seq("s")
    order.note_item("s", "i", "p")
    order.forget("s")
    assert order.next_seq("s") == 0
    assert order.previous_of("s", "i") == ""


# ─────────────────────────────────────────────
# Bug A — item_id / previous_item_id capture
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_assistant_transcript_forwards_item_id():
    """``item_id`` used to be dropped on the floor by every branch."""
    calls: list[dict] = []

    async def on_tr(sid, text, final, **ordering):
        calls.append({"text": text, **ordering})

    rs = RealtimeSession("s", "n", api_key="k", on_transcript=on_tr)
    await rs._handle_event({
        "type": "response.output_audio_transcript.done",
        "transcript": "Hello there",
        "item_id": "item_assistant",
    })

    assert calls[0]["text"] == "Hello there"
    assert calls[0]["item_id"] == "item_assistant"


@pytest.mark.asyncio
async def test_user_transcript_forwards_item_id():
    calls: list[dict] = []

    async def on_tr(sid, text, final, **ordering):
        calls.append({"text": text, **ordering})

    rs = RealtimeSession("s", "n", api_key="k", on_transcript=on_tr)
    await rs._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "what's the weather",
        "item_id": "item_user",
    })

    assert calls[0]["text"] == "[user] what's the weather"
    assert calls[0]["item_id"] == "item_user"


@pytest.mark.asyncio
async def test_conversation_item_added_supplies_previous_item_id():
    """The linked list OpenAI hands us for free is now actually used."""
    calls: list[dict] = []

    async def on_tr(sid, text, final, **ordering):
        calls.append(ordering)

    rs = RealtimeSession("s", "n", api_key="k", on_transcript=on_tr)
    # The assistant's item is announced as following the user's item.
    await rs._handle_event({
        "type": "conversation.item.added",
        "previous_item_id": "item_user",
        "item": {"id": "item_assistant", "role": "assistant"},
    })
    await rs._handle_event({
        "type": "response.output_audio_transcript.done",
        "transcript": "It is sunny",
        "item_id": "item_assistant",
    })

    assert calls[0]["item_id"] == "item_assistant"
    assert calls[0]["previous_item_id"] == "item_user"


@pytest.mark.asyncio
async def test_input_audio_buffer_committed_supplies_previous_item_id():
    """``input_audio_buffer.committed`` carries the user turn's ordering."""
    calls: list[dict] = []

    async def on_tr(sid, text, final, **ordering):
        calls.append(ordering)

    rs = RealtimeSession("s", "n", api_key="k", on_transcript=on_tr)
    await rs._handle_event({
        "type": "input_audio_buffer.committed",
        "item_id": "item_user_2",
        "previous_item_id": "item_assistant_1",
    })
    await rs._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "and tomorrow?",
        "item_id": "item_user_2",
    })

    assert calls[0]["item_id"] == "item_user_2"
    assert calls[0]["previous_item_id"] == "item_assistant_1"


# ─────────────────────────────────────────────
# Bug A — ordering metadata reaches the wire
# ─────────────────────────────────────────────

def _web_proxy():
    sent: list = []

    async def fake_send_to_session(session_id, msg):
        sent.append(msg)

    proxy = RealtimeProxy(send_to_session=fake_send_to_session)
    rs = RealtimeSession("sess-web", "webclient_ab", api_key="k")
    proxy._sessions["sess-web"] = rs
    return proxy, sent


@pytest.mark.asyncio
async def test_web_transcript_payload_carries_ordering_metadata():
    proxy, sent = _web_proxy()

    await proxy._handle_transcript(
        "sess-web", "It is sunny", True,
        item_id="item_assistant", previous_item_id="item_user",
    )

    payload = sent[0].payload
    assert payload["item_id"] == "item_assistant"
    assert payload["previous_item_id"] == "item_user"
    assert payload["seq"] == 0
    assert payload["role"] == "assistant"


@pytest.mark.asyncio
async def test_seq_increments_across_frames_in_a_session():
    proxy, sent = _web_proxy()

    await proxy._handle_transcript("sess-web", "[user] one", True)
    await proxy._handle_transcript("sess-web", "two", True)

    assert [m.payload["seq"] for m in sent] == [0, 1]


@pytest.mark.asyncio
async def test_node_transcript_uses_full_feral_message_envelope():
    """The node branch used to hand-roll a bare dict with no envelope.

    That cost iOS ``timestamp_ms`` and every ordering field, so it had
    strictly less to order by than the web client for the same
    conversation.
    """
    captured: list[dict] = []

    async def fake_send_to_node(node_id, frame):
        captured.append(frame)

    proxy = RealtimeProxy(send_to_node=fake_send_to_node)
    proxy._sessions["sess-node"] = RealtimeSession(
        "sess-node", "iphone-1", api_key="k",
    )

    await proxy._handle_transcript(
        "sess-node", "[user] hello", True,
        item_id="item_user", previous_item_id="",
    )

    frame = captured[0]
    assert frame["type"] == "transcript"
    assert frame["session_id"] == "sess-node"
    assert "timestamp_ms" in frame
    assert frame["payload"]["role"] == "user"
    assert frame["payload"]["text"] == "hello"
    assert frame["payload"]["item_id"] == "item_user"
    assert frame["payload"]["seq"] == 0


def test_transcript_payload_ordering_fields_round_trip():
    payload = TranscriptPayload(
        text="hi", role="user", item_id="i1", previous_item_id="i0", seq=7,
    )
    reloaded = TranscriptPayload(**payload.model_dump())
    assert reloaded.item_id == "i1"
    assert reloaded.previous_item_id == "i0"
    assert reloaded.seq == 7


def test_transcript_payload_ordering_fields_default_to_none():
    """Older brains omit them; the wire stays backward compatible."""
    payload = TranscriptPayload(text="hi")
    assert payload.item_id is None
    assert payload.previous_item_id is None
    assert payload.seq is None


# ─────────────────────────────────────────────
# Bug B — whisper path role attribution
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_whisper_path_web_frame_tags_user_role():
    """``process_audio_chunk`` output is USER speech, on BOTH branches.

    Pre-fix the web branch omitted ``role`` entirely and the payload
    default tagged the user's own words ``"assistant"``, so the web
    client left-aligned them.
    """
    sent: list = []

    async def fake_send_to_session(session_id, msg):
        sent.append(msg)

    audio = MagicMock()
    audio.process_audio_chunk = AsyncMock(return_value="turn on the lights")
    audio.synthesize_speech = AsyncMock(return_value=[])

    router = VoiceRouter(
        realtime_proxy=MagicMock(available=False),
        audio_pipeline=audio,
        send_to_session=fake_send_to_session,
    )

    await router._handle_whisper_path(
        session_id="sess-whisper",
        audio_b64="eHh4",
        chunk_index=0,
        is_final=True,
        encoding="pcm16",
        sample_rate=24000,
    )

    payload = sent[0].payload
    assert payload["role"] == "user"
    assert payload["text"] == "turn on the lights"


@pytest.mark.asyncio
async def test_whisper_path_web_and_node_branches_agree():
    """The two branches contradicted each other for the same utterance."""
    sent_session: list = []
    sent_node: list = []

    async def fake_send_to_session(session_id, msg):
        sent_session.append(msg)

    async def fake_send_to_node(node_id, frame):
        sent_node.append(frame)

    audio = MagicMock()
    audio.process_audio_chunk = AsyncMock(return_value="hello brain")
    audio.synthesize_speech = AsyncMock(return_value=[])

    router = VoiceRouter(
        realtime_proxy=MagicMock(available=False),
        audio_pipeline=audio,
        send_to_session=fake_send_to_session,
        send_to_node=fake_send_to_node,
    )

    await router._handle_whisper_path(
        session_id="sess-both",
        audio_b64="eHh4",
        chunk_index=0,
        is_final=True,
        encoding="pcm16",
        sample_rate=24000,
        source_node_id="phone-1",
    )

    assert sent_session[0].payload["role"] == sent_node[0]["payload"]["role"] == "user"


@pytest.mark.asyncio
async def test_whisper_path_stamps_seq_fallback():
    """Whisper has no item ids, so ``seq`` is the only ordering signal."""
    sent: list = []

    async def fake_send_to_session(session_id, msg):
        sent.append(msg)

    audio = MagicMock()
    audio.process_audio_chunk = AsyncMock(return_value="hello")
    audio.synthesize_speech = AsyncMock(return_value=[])

    router = VoiceRouter(
        realtime_proxy=MagicMock(available=False),
        audio_pipeline=audio,
        send_to_session=fake_send_to_session,
    )

    await router._handle_whisper_path(
        session_id="sess-seq", audio_b64="eHh4", chunk_index=0,
        is_final=True, encoding="pcm16", sample_rate=24000,
    )

    assert isinstance(sent[0].payload["seq"], int)
