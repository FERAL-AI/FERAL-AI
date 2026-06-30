"""Regression tests for the live-voice fix batch (branch
``fix/voice-live-coref-memory-phantom``).

Three bugs covered, each kept as a focused module section:

  Bug 1  Voice coreference: "how about now" / "what about now" must be
         treated as an underspecified follow-up so the active subject
         (cutebot) is reused; the orchestrator's
         ``note_voice_user_turn`` hook tracks the subject and returns a
         coref-resolved text + a system-style context hint that the
         realtime proxies can inject into the live session.

  Bug 2  (A) Persistence: every voice-driven hardware tool call writes
              an episode anchored to the LIVE session id (not the
              anonymous ``hwdev-*`` per-device session) so recall can
              find it.
         (B) Retrieval: ``_R_TEMPORAL`` / ``_R_MEMORY`` match
              "what did my robot/device/cutebot do" so the timeline
              side-channel mounts and the routing heuristic picks
              ``notes_memory``.

  Bug 3  Phantom transcript gate: ``voice.transcript_filter`` drops the
         whisper-1 / Deepgram stock closers (`"bye-bye"`,
         `"thank you"`, `"thanks for watching"`, …) BEFORE they reach
         the proxy callback so phantom user turns never reach the
         orchestrator or generate a reply. Real short commands
         ("stop", "halt") still go through.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.orchestrator import Orchestrator
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest


# ─────────────────────────────────────────────
# Shared orchestrator fixture (mirrors test_automation_time_context._make_orchestrator)
# ─────────────────────────────────────────────


def _skill(skill_id, triggers, categories=None):
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="test",
        brand=BrandProfile(name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"),
        description=f"{skill_id} skill",
        categories=categories or [],
        trigger_phrases=triggers,
        endpoints=[
            SkillEndpoint(
                id="default",
                method="POST",
                url=f"https://example.test/{skill_id}",
                description="default endpoint",
                returns_description="result",
                ui_hint="detail_card",
            )
        ],
    )


CATALOG = {
    "cutebot": _skill(
        "cutebot",
        ["follow the line", "cutebot", "drive the robot"],
        ["robot", "hardware"],
    ),
    "notes_memory": _skill(
        "notes_memory",
        ["recall", "remember", "what did i"],
        ["memory"],
    ),
    "spotify_music": _skill("spotify_music", ["play music"], ["music"]),
    "calendar_google": _skill(
        "calendar_google",
        ["what's on my calendar", "schedule a meeting"],
        ["calendar"],
    ),
}


def _make_orchestrator():
    reg = MagicMock()
    reg.skills = CATALOG

    def _find(query, top_k=5):
        scored = []
        for sk in CATALOG.values():
            s = Orchestrator._trigger_score(query, sk)
            if s > 0:
                scored.append((s, sk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [sk for _, sk in scored[:top_k]]

    reg.find_skills_for_query = _find
    reg.get_tools_for_skills = MagicMock(return_value=[])

    orch = Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
    )
    orch.llm = MagicMock()
    orch.llm.available = False
    orch.llm.chat_with_failover = AsyncMock(
        side_effect=AssertionError("LLM routing must not be needed here")
    )
    orch.llm.route_call = MagicMock(
        side_effect=AssertionError("route_call must not be needed here")
    )
    return orch


# ═════════════════════════════════════════════════════════════════════
# Bug 1 — Voice coreference for "how about now" / "what about now"
# ═════════════════════════════════════════════════════════════════════


def test_how_about_now_classified_as_followup():
    """The pre-fix gate required every token to be a cue / bare-verb /
    stopword, so "how" / "about" / "what" disqualified the utterance.
    """
    orch = _make_orchestrator()
    assert orch._is_underspecified_followup("how about now") is True
    assert orch._is_underspecified_followup("what about now") is True
    assert orch._is_underspecified_followup("how about now?") is True
    assert orch._is_underspecified_followup("What about now?") is True
    assert orch._is_underspecified_followup("how about") is True
    assert orch._is_underspecified_followup("what about it") is True
    # Don't over-trigger on full questions that merely begin with "how".
    assert orch._is_underspecified_followup("how do I configure the cutebot") is False
    assert orch._is_underspecified_followup("what is the weather like") is False


@pytest.mark.asyncio
async def test_how_about_now_resolves_against_cutebot():
    """Voice transcript "how about now" right after a concrete cutebot
    turn must coref-resolve to the cutebot so routing keeps the device
    skill in the candidate set."""
    orch = _make_orchestrator()
    sid = "voice-coref-1"
    orch.conversation_history[sid] = [
        {"role": "user", "content": "check the cutebot"},
        {"role": "assistant", "content": "The cutebot is online."},
    ]
    result = await orch._route_prompt("how about now", session_id=sid)
    assert "cutebot" in [s.skill_id for s in result]


@pytest.mark.asyncio
async def test_what_about_now_resolves_against_cutebot():
    orch = _make_orchestrator()
    sid = "voice-coref-2"
    orch.conversation_history[sid] = [
        {"role": "user", "content": "check the cutebot"},
        {"role": "assistant", "content": "The cutebot is online."},
    ]
    result = await orch._route_prompt("what about now?", session_id=sid)
    assert "cutebot" in [s.skill_id for s in result]


# ═════════════════════════════════════════════════════════════════════
# Bug 1 + 2 — note_voice_user_turn hook contract
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_note_voice_user_turn_tracks_active_subject():
    """The voice hook records concrete turns as the active subject so
    a follow-up next turn sees the right context.
    """
    orch = _make_orchestrator()
    sid = "voice-hook-1"
    # Concrete turn: tracked as active subject, no context_hint.
    out1 = await orch.note_voice_user_turn(sid, "check the cutebot")
    assert out1["active_subject"] == "check the cutebot"
    assert out1["resolved_text"] == "check the cutebot"
    assert out1["context_hint"] == ""

    # Underspecified follow-up: resolved against active subject.
    out2 = await orch.note_voice_user_turn(sid, "how about now")
    assert "cutebot" in out2["resolved_text"].lower()
    assert out2["resolved_text"] != "how about now"
    assert out2["context_hint"]
    assert "cutebot" in out2["context_hint"].lower()


@pytest.mark.asyncio
async def test_note_voice_user_turn_appends_to_conversation_history():
    """Voice user turns must land in conversation_history so subsequent
    text or voice turns can route against them."""
    orch = _make_orchestrator()
    sid = "voice-hook-history"
    await orch.note_voice_user_turn(sid, "check the cutebot")
    hist = orch.conversation_history.get(sid) or []
    assert any(
        m.get("role") == "user" and "cutebot" in (m.get("content") or "").lower()
        for m in hist
    )


@pytest.mark.asyncio
async def test_note_voice_user_turn_handles_empty():
    orch = _make_orchestrator()
    out = await orch.note_voice_user_turn("sid", "")
    assert out["resolved_text"] == ""
    assert out["context_hint"] == ""
    assert "sid" not in orch.conversation_history


# ═════════════════════════════════════════════════════════════════════
# Bug 2 (B) — recall regex catches device queries
# ═════════════════════════════════════════════════════════════════════


def test_temporal_regex_matches_device_recall():
    """The pre-fix _R_TEMPORAL only matched "what did I" — device
    recall ("what did my robot do yesterday?") fell through to the
    generalist path and the timeline side-channel never mounted.
    """
    R = Orchestrator._R_TEMPORAL
    assert R.search("what did my robot do yesterday")
    assert R.search("What did my robot do today?")
    assert R.search("what has my cutebot done")
    assert R.search("what did the device do this morning")
    assert R.search("What did it do yesterday")
    # The "what did I" clause must still match.
    assert R.search("what did i do yesterday")


def test_memory_regex_matches_device_recall():
    R = Orchestrator._R_MEMORY
    assert R.search("what did my robot do yesterday")
    assert R.search("What has my cutebot done today?")
    assert R.search("what did the device do this morning")
    assert R.search("what did i do today")


@pytest.mark.asyncio
async def test_device_recall_routes_to_notes_memory():
    """A device-recall question must route to ``notes_memory`` so the
    LLM gets the timeline-fusion tool in its candidate set."""
    orch = _make_orchestrator()
    result = await orch._route_prompt("what did my robot do yesterday")
    ids = [s.skill_id for s in result]
    assert "notes_memory" in ids


# ═════════════════════════════════════════════════════════════════════
# Bug 2 (A) — episode persistence on the voice tool path
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_voice_tool_call_persists_episode_anchored_to_session():
    """A voice-driven cutebot tool call must write an episode anchored
    to the LIVE session id (not the device-anchored ``hwdev-*`` fallback).
    """
    from voice.realtime_proxy import RealtimeProxy

    sk = MagicMock()
    sk.skill_id = "cutebot"
    sk.endpoints = [MagicMock(id="drive")]
    skill_registry = MagicMock()
    skill_registry.skills = {"cutebot": sk}

    skill_executor = MagicMock()
    skill_executor.execute = AsyncMock(return_value={
        "success": True,
        "data": {"verified": True, "mode": "drive"},
    })

    saves: list[dict] = []

    class _Memory:
        def __init__(self):
            pass

        def working_push(self, *a, **kw):
            pass

        async def conversation_append(self, *a, **kw):
            pass

        async def episode_save(self, **kwargs):
            saves.append(kwargs)
            return {"id": "abc"}

    memory = _Memory()

    with patch("voice.realtime_proxy._resolve_openai_key", return_value="sk-test"), \
         patch("voice.personality.VoicePersonality"):
        proxy = RealtimeProxy(
            skill_registry=skill_registry,
            skill_executor=skill_executor,
            memory=memory,
            perception=MagicMock(),
        )

    live_session = "live-session-xyz"
    out = await proxy._handle_tool_call(
        live_session, "call_42", "cutebot__drive", '{"left": 50, "right": 50}',
    )
    assert '"verified"' in out or "verified" in out  # tool result returned

    # The voice tool path runs the episode save as a background task;
    # await any tasks scheduled during this turn so the assertion is
    # deterministic.
    import asyncio
    pending = [t for t in asyncio.all_tasks() if not t.done()]
    # Drop the running task (this test).
    pending = [t for t in pending if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    assert saves, "voice tool call did not persist an episode"
    last = saves[-1]
    assert last["session_id"] == live_session
    assert last["event_type"] == "actuator"
    assert "cutebot" in last["summary"].lower()


@pytest.mark.asyncio
async def test_voice_tool_call_status_persisted_as_sensor_event():
    """Status / read endpoints should land as event_type='sensor' so
    the recall surface can distinguish reads from actuator commands.
    """
    from voice.realtime_proxy import RealtimeProxy

    sk = MagicMock()
    sk.skill_id = "cutebot"
    sk.endpoints = [MagicMock(id="status")]
    skill_registry = MagicMock()
    skill_registry.skills = {"cutebot": sk}

    skill_executor = MagicMock()
    skill_executor.execute = AsyncMock(return_value={
        "success": True,
        "data": {"online": True, "mode": "stopped"},
    })

    saves: list[dict] = []

    class _Memory:
        def working_push(self, *a, **kw): pass
        async def conversation_append(self, *a, **kw): pass
        async def episode_save(self, **kwargs):
            saves.append(kwargs)
            return {"id": "x"}

    with patch("voice.realtime_proxy._resolve_openai_key", return_value="sk-test"), \
         patch("voice.personality.VoicePersonality"):
        proxy = RealtimeProxy(
            skill_registry=skill_registry,
            skill_executor=skill_executor,
            memory=_Memory(),
            perception=MagicMock(),
        )

    await proxy._handle_tool_call("sess-y", "call_1", "cutebot__status", "{}")
    import asyncio
    pending = [t for t in asyncio.all_tasks() if not t.done()]
    pending = [t for t in pending if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    assert saves
    assert saves[-1]["event_type"] == "sensor"


# ═════════════════════════════════════════════════════════════════════
# Bug 3 — phantom transcript gate
# ═════════════════════════════════════════════════════════════════════


def test_phantom_phrases_dropped():
    from voice.transcript_filter import should_commit_user_transcript

    for raw in (
        "",
        "   ",
        ".",
        "...",
        "Bye.",
        "Bye-bye.",
        "bye bye",
        "Goodbye.",
        "Thank you.",
        "Thank you very much",
        "Thanks for watching",
        "Thanks for watching the video",
        "You.",
        "Yeah.",
        "okay bye",
    ):
        assert not should_commit_user_transcript(raw), f"phantom not dropped: {raw!r}"


def test_real_short_commands_pass():
    from voice.transcript_filter import should_commit_user_transcript

    for raw in (
        "stop",
        "halt",
        "halt the robot",
        "yes",
        "no",
        "Drive forward",
        "what's the weather",
        "play music",
        "how about now",  # routed elsewhere — must NOT be filtered here
        "what about now",
        "what did my robot do yesterday",
    ):
        assert should_commit_user_transcript(raw), f"real utterance dropped: {raw!r}"


def test_deepgram_confidence_floor_drops_low_conf_uncommon():
    """A low-confidence non-allowlisted utterance is dropped; a low-conf
    legitimate-short command ("stop") is still honoured."""
    from voice.transcript_filter import should_commit_user_transcript

    assert not should_commit_user_transcript("uh", confidence=0.2)
    assert not should_commit_user_transcript("hmm yeah", confidence=0.3)
    # Even low confidence shouldn't drop a halt command.
    assert should_commit_user_transcript("stop", confidence=0.2)


def test_deepgram_high_conf_borderline_pass():
    """High confidence keeps borderline utterances through (Deepgram
    real-utterance commits are typically 0.7+)."""
    from voice.transcript_filter import should_commit_user_transcript

    assert should_commit_user_transcript("yes", confidence=0.9)
    assert should_commit_user_transcript("play music", confidence=0.85)


# ═════════════════════════════════════════════════════════════════════
# Bug 3 — gate is wired into the realtime proxy event path
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_realtime_session_drops_phantom_input_audio_event():
    """The OpenAI Realtime
    ``conversation.item.input_audio_transcription.completed`` event for
    a phantom commit must NOT reach the on_transcript callback. The
    realtime proxy short-circuits before calling the callback.
    """
    from voice.realtime_proxy import RealtimeSession

    callback = AsyncMock()
    rs = RealtimeSession(
        session_id="sess-phantom",
        node_id="node-phantom",
        api_key="sk-test",
        on_transcript=callback,
    )

    # Phantom — should NOT fire callback.
    await rs._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Bye-bye.",
    })
    callback.assert_not_called()

    # Real utterance — fires.
    await rs._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "drive the robot forward",
    })
    callback.assert_called_once()
    args, _ = callback.call_args
    assert args[0] == "sess-phantom"
    assert args[1].startswith("[user] ")
    assert "drive the robot forward" in args[1]
    assert args[2] is True
