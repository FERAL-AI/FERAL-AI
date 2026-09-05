"""One spoken reply must be one assistant row, even when OpenAI restarts it.

Operator store, conversation ``voice:voice-feral-iphone-60dc6b3aa07e-BC78452A``:
"Hey Omar," (17:29:21.868) and "How's it going?" (17:29:23.844) were two
assistant messages. brain.err for the same seconds:

    17:29:20,752 response.created
    17:29:21,870 response.done status=cancelled details={'reason': 'turn_detected'}
    17:29:22,958 response.created
    17:29:23,848 response.done status=completed

Each cancellation was immediately followed by ``dropping phantom user
transcript 'Bye. Bye.'``. The assistant's own audio, played by the
phone, tripped OpenAI's server VAD; OpenAI cancelled the response mid
sentence; whisper transcribed the echo as a stock closer that the
phantom filter dropped; the model started over. The proxy persisted
every ``response.output_audio_transcript.done`` on arrival, so the
half sentence became its own row. Same shape for a standalone "Got it."
row (17:29:59.218 against ``response.done status=cancelled
turn_detected`` at 17:29:59,220).

These tests drive ``RealtimeSession._handle_event`` with that exact
sequence and check the durable ``voice:<sid>`` thread plus the
orchestrator hand-off. Live UI emits (the wire frames) are asserted
unchanged: this is about what is persisted, not what is streamed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.store import MemoryStore  # noqa: E402
from voice.realtime_proxy import RealtimeProxy, RealtimeSession  # noqa: E402
from voice.transcript_order import TRANSCRIPT_ORDER  # noqa: E402

SID = "voice-feral-iphone-test-BC78452A"


def _created():
    return {"type": "response.created", "response": {"id": "resp"}}


def _assistant_done(text: str, item_id: str):
    return {
        "type": "response.output_audio_transcript.done",
        "transcript": text,
        "item_id": item_id,
    }


def _done_cancelled_turn_detected():
    return {
        "type": "response.done",
        "response": {
            "status": "cancelled",
            "status_details": {"type": "cancelled", "reason": "turn_detected"},
        },
    }


def _done_completed():
    return {"type": "response.done", "response": {"status": "completed"}}


def _user_transcript(text: str, item_id: str):
    return {
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": text,
        "item_id": item_id,
    }


@pytest.fixture
def harness(tmp_path):
    """Proxy + session wired exactly as ``RealtimeProxy.start_session`` does."""
    mem = MemoryStore(db_path=str(tmp_path / "mem.db"))
    orch = MagicMock()
    orch.note_voice_assistant_turn = AsyncMock()
    orch.note_voice_user_turn = AsyncMock(return_value={})
    frames: list[dict] = []

    async def send_to_node(node_id, frame):
        frames.append(frame)

    proxy = RealtimeProxy(memory=mem, orchestrator=orch, send_to_node=send_to_node)
    rs = RealtimeSession(
        SID, "iphone-1", api_key="k",
        on_transcript=proxy._handle_transcript,
        on_response_created=proxy._handle_response_created,
        on_response_done=proxy._handle_response_done,
    )
    proxy._sessions[SID] = rs
    proxy._node_to_session["iphone-1"] = SID
    yield proxy, rs, mem, orch, frames
    TRANSCRIPT_ORDER.forget(SID)


async def _settle(proxy: RealtimeProxy):
    if proxy._bg_tasks:
        await asyncio.gather(*proxy._bg_tasks, return_exceptions=True)


async def _rows(mem: MemoryStore):
    conv = await mem.conversation_get(f"voice:{SID}")
    if conv is None:
        return []
    return [(m["role"], m["content"]) for m in conv.get("messages", [])]


@pytest.mark.asyncio
async def test_echo_cancelled_reply_persists_as_one_row(harness):
    """The exact brain.err sequence from 17:29:20,752 to 17:29:23,848."""
    proxy, rs, mem, orch, frames = harness

    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("Hey Omar,", "item_a1"))
    await rs._handle_event(_done_cancelled_turn_detected())
    # Whisper's rendering of the assistant's own echo. Dropped by the
    # phantom filter inside the session, never reaches the proxy.
    await rs._handle_event(_user_transcript("Bye. Bye.", "item_u_phantom"))
    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("How's it going?", "item_a2"))
    await rs._handle_event(_done_completed())
    await _settle(proxy)

    assert await _rows(mem) == [("assistant", "Hey Omar, How's it going?")]
    orch.note_voice_assistant_turn.assert_awaited_once_with(
        SID, "Hey Omar, How's it going?",
    )
    orch.note_voice_user_turn.assert_not_awaited()
    # Working memory saw one merged turn too.
    working = mem.working_get(SID) if hasattr(mem, "working_get") else None
    if working is not None:
        assistant_turns = [w for w in working if w.get("role") == "assistant"]
        assert [w["text"] for w in assistant_turns] == ["Hey Omar, How's it going?"]


@pytest.mark.asyncio
async def test_live_wire_still_streams_each_final_while_buffering(harness):
    """Persistence is deferred; the client keeps seeing text as it lands."""
    proxy, rs, mem, orch, frames = harness

    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("Hey Omar,", "item_a1"))
    # Nothing persisted yet, but the frame went out.
    assert await _rows(mem) == []
    assert [f["payload"]["text"] for f in frames] == ["Hey Omar,"]

    await rs._handle_event(_done_cancelled_turn_detected())
    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("How's it going?", "item_a2"))
    assert [f["payload"]["text"] for f in frames] == ["Hey Omar,", "How's it going?"]
    assert all(f["payload"]["role"] == "assistant" for f in frames)


@pytest.mark.asyncio
async def test_real_user_interruption_still_produces_two_rows(harness):
    """A real transcript between the cancel and the restart means the
    operator actually cut in; the fragment is its own row, in order."""
    proxy, rs, mem, orch, frames = harness

    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("Hey Omar,", "item_a1"))
    await rs._handle_event(_done_cancelled_turn_detected())
    await rs._handle_event(_user_transcript("Wait, what?", "item_u1"))
    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("How's it going?", "item_a2"))
    await rs._handle_event(_done_completed())
    await _settle(proxy)

    assert await _rows(mem) == [
        ("assistant", "Hey Omar,"),
        ("user", "Wait, what?"),
        ("assistant", "How's it going?"),
    ]
    assert [c.args for c in orch.note_voice_assistant_turn.await_args_list] == [
        (SID, "Hey Omar,"),
        (SID, "How's it going?"),
    ]


@pytest.mark.asyncio
async def test_fragment_pending_at_session_close_is_flushed(harness):
    """The 17:29:59 "Got it." case, with the session ending before any
    completed response follows. Nothing the assistant said may be lost."""
    proxy, rs, mem, orch, frames = harness

    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("Got it.", "item_a1"))
    await rs._handle_event(_done_cancelled_turn_detected())
    await rs._handle_event(_user_transcript("Bye. Bye.", "item_u_phantom"))
    assert await _rows(mem) == []

    await proxy.stop_session(SID)
    await _settle(proxy)

    assert await _rows(mem) == [("assistant", "Got it.")]
    orch.note_voice_assistant_turn.assert_awaited_once_with(SID, "Got it.")
    assert SID not in proxy._pending_replies


@pytest.mark.asyncio
async def test_completed_reply_without_a_cancel_is_unchanged(harness):
    """The common case: created, transcript, completed, one row."""
    proxy, rs, mem, orch, frames = harness

    await rs._handle_event(_user_transcript("what time is it", "item_u1"))
    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("It is noon.", "item_a1"))
    await rs._handle_event(_done_completed())
    await _settle(proxy)

    assert await _rows(mem) == [("user", "what time is it"), ("assistant", "It is noon.")]


@pytest.mark.asyncio
async def test_failed_response_flushes_what_was_said(harness):
    """Only ``turn_detected`` is a restart signal. Any other non-completed
    status is the end of that reply; its transcript is written as is."""
    proxy, rs, mem, orch, frames = harness

    await rs._handle_event(_created())
    await rs._handle_event(_assistant_done("Let me check", "item_a1"))
    await rs._handle_event({
        "type": "response.done",
        "response": {"status": "incomplete", "status_details": {"reason": "max_output_tokens"}},
    })
    await _settle(proxy)

    assert await _rows(mem) == [("assistant", "Let me check")]


@pytest.mark.asyncio
async def test_proxy_without_lifecycle_events_persists_immediately(tmp_path):
    """Direct ``_handle_transcript`` callers (older tests, providers that
    send no ``response.*`` events) keep the pre-existing behaviour."""
    mem = MemoryStore(db_path=str(tmp_path / "mem.db"))
    proxy = RealtimeProxy(memory=mem)

    await proxy._handle_transcript("plain", "hello back", True)

    conv = await mem.conversation_get("voice:plain")
    assert [m["content"] for m in conv["messages"]] == ["hello back"]
