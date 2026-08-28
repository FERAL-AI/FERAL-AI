"""Voice must remember what text remembers.

Voice is the systematically under-tested twin of the web path, and the
same three defects appear at three different levels:

  * Live voice turns write NO episode at all. `_save_episode_async` is
    wired into `_finalize_turn` and the two `handle_command*_impl`
    preludes; realtime audio arrives through `note_voice_*_turn` ->
    `_append_voice_row`, which touches only the in-memory
    `conversation_history`. So the fix that made transcripts outlive
    compaction never reached voice: a purely conversational voice
    session is invisible to `episode_search`, `episode_recent` and the
    timeline.

  * The Gemini path truncates every stored transcript to 300 characters
    while the OpenAI path stores the full text, same call, same table.

  * A tool-call episode failure logs at `debug` on Gemini where its
    OpenAI twin was deliberately raised to `warning` with a comment
    saying those episodes are "the only trace those calls left, so
    losing one at debug level loses it entirely".

Each test asserts behaviour and fails against the code as it stood.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.orchestrator import Orchestrator  # noqa: E402


def _orchestrator():
    """Bare orchestrator with only what the voice row path touches."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.conversation_history = {}
    orch._session_locks = {}
    orch._conversation_max_per_session = 200
    # Stamped by _append_voice_row so eviction can rank by real
    # staleness. A live-voice session never reaches _finalize_turn,
    # so without this stamp it would look idle forever and be the
    # first thing evicted while the operator was still speaking.
    orch._session_last_active = {}
    orch._background_tasks = set()
    saved: list[dict] = []
    orch._save_episode_async = lambda **kw: saved.append(kw)
    return orch, saved


@pytest.mark.asyncio
async def test_live_voice_user_turn_is_persisted_as_an_episode():
    orch, saved = _orchestrator()
    await orch._append_voice_row("voice-1", "user", "remind me to call the vet")

    kinds = [s.get("event_type") for s in saved]
    assert "user_command" in kinds, (
        "a live voice user turn wrote no episode; it exists only in "
        f"conversation_history, which compaction replaces. saved={saved}"
    )


@pytest.mark.asyncio
async def test_live_voice_assistant_turn_is_persisted_as_an_episode():
    orch, saved = _orchestrator()
    await orch._append_voice_row("voice-1", "assistant", "I booked it for Tuesday")

    kinds = [s.get("event_type") for s in saved]
    assert "assistant_reply" in kinds, (
        f"a live voice assistant turn wrote no episode. saved={saved}"
    )
    detail = " ".join(str(s.get("detail", "")) for s in saved)
    assert "Tuesday" in detail


@pytest.mark.asyncio
async def test_voice_episode_carries_the_full_text_not_a_preview():
    orch, saved = _orchestrator()
    long_turn = "the vet is on " + ("Elm street " * 40) + "and the code is 4417"
    assert len(long_turn) > 200
    await orch._append_voice_row("voice-1", "user", long_turn)

    assert saved, "no episode written at all"
    detail = " ".join(str(s.get("detail", "")) for s in saved)
    assert "4417" in detail, (
        "the tail of a long voice turn was dropped; the 200-char summary "
        "preview must not be the only durable copy"
    )


@pytest.mark.asyncio
async def test_a_repeated_final_transcript_does_not_double_write():
    """Providers re-emit the same final transcript on reconnect."""
    orch, saved = _orchestrator()
    await orch._append_voice_row("voice-1", "user", "same utterance")
    await orch._append_voice_row("voice-1", "user", "same utterance")

    assert len(orch.conversation_history["voice-1"]) == 1
    assert len(saved) == 1, f"one utterance produced {len(saved)} episodes: {saved}"


def test_gemini_stores_the_full_transcript_like_openai():
    """Same call, same table, one provider silently lossy."""
    gemini = (ROOT / "voice" / "gemini_realtime.py").read_text()
    calls = re.findall(
        r"conversation_append\(\s*[^)]*?,\s*(?:\"[^\"]*\"|[a-z_]+)\s*,\s*([^,\n]+),",
        gemini,
    )
    assert calls, "conversation_append call sites not found; update this test"
    for arg in calls:
        assert "[:300]" not in arg, (
            f"gemini_realtime truncates the stored transcript to 300 chars ({arg.strip()}); "
            "voice/realtime_proxy.py stores the full text for the same call"
        )


def test_gemini_tool_call_episode_failure_is_not_swallowed_at_debug(caplog):
    """The OpenAI twin was deliberately raised to warning; match it.

    realtime_proxy.py carries the reason inline: a tool-call episode is
    "the only trace those calls left, so losing one at debug level loses
    it entirely".
    """
    gemini = (ROOT / "voice" / "gemini_realtime.py").read_text()
    # Target the handler itself, not the first mention of the name. An
    # earlier `getattr(self._memory, "episode_save", None)` sits ~60
    # lines above, and anchoring on that made this test pass for the
    # wrong reason.
    m = re.search(
        # `(?:\s*#[^\n]*\n)*` so an explanatory comment between the
        # `except` and the log call does not hide the handler.
        r"except Exception:\s*\n(?:\s*#[^\n]*\n)*\s*logger\.(\w+)\(\s*\n?\s*"
        r"[\"'][^\"']*episode_save[^\"']*[\"']",
        gemini,
    )
    assert m, "the episode_save failure handler was not found; update this test"
    level = m.group(1)
    assert level != "debug", (
        f"gemini logs a failed tool-call episode at logger.{level}; its OpenAI "
        "twin logs at warning because that episode is the only trace the call "
        "leaves, so losing one at debug level loses it entirely"
    )
