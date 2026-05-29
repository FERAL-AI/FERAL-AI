"""Pin the orchestrator-side emission of the ``timeline`` WS frame.

This file covers three layers of the S1 ``what did I do yesterday?``
pipeline; each layer pins one specific contract and the three
together close the live-brain gap:

1. **Helper-only emit shape** — when ``_emit_tool_result`` is invoked
   with a ``*__fused_timeline`` tool call and a well-shaped result
   payload, the orchestrator emits a ``timeline`` FeralMessage with
   the canonical envelope. These tests drive
   ``_maybe_emit_timeline_frame`` indirectly via ``_emit_tool_result``
   and pin the WS-frame shape only. They do NOT pin live-path
   behaviour; the live model often answers in prose without dispatching
   the tool, in which case this branch never fires. The first
   v2026.5.43 wave's green test sat exclusively at this layer and
   gave false confidence — the live path was still broken because
   nothing forced the LLM to call the tool.

2. **Heuristic routing closure** — temporal-recall phrasings
   ("summarize my morning", "what happened today", "earlier today",
   "recap my day") must promote ``notes_memory`` so the LLM at least
   *sees* ``notes_memory__fused_timeline`` in its tool list, and
   non-temporal turns must NOT route to ``notes_memory``. Routing
   only exposes the tool — it cannot force the model to call it.

3. **Live-path side-channel emit** — when a temporal-recall query
   reaches the live chat-stream path (``_handle_command_stream_impl``
   or ``_handle_command_impl``), the orchestrator dispatches
   ``timeline_fusion`` directly as a background task and emits a
   ``timeline`` WS frame iff the fusion returned ≥ 1 entry —
   regardless of whether the LLM tool-calls. These tests drive the
   real stream-handler entry point with a mock LLM that streams
   text-only (mimicking claude-opus-4-7's observed live behaviour)
   and assert the WS sink receives a ``timeline`` frame. This is the
   layer the v2026.5.43 wave was missing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.protocol import FeralMessage  # noqa: E402
from models.skill_manifest import (  # noqa: E402
    BrandProfile,
    SkillEndpoint,
    SkillManifest,
)
from agents.orchestrator import Orchestrator  # noqa: E402


class _FakeSink:
    """Captures every FeralMessage handed to ``orchestrator.send``."""

    def __init__(self):
        self.frames: list[FeralMessage] = []

    async def __call__(self, session_id: str, msg: FeralMessage) -> None:
        self.frames.append(msg)


@pytest.fixture
def fake_orch():
    """A bare ``Orchestrator`` shell — we only exercise the helper
    methods that emit WS frames, so we don't need the LLM, memory,
    or skill registry wired."""
    sink = _FakeSink()

    # Don't call Orchestrator.__init__ (heavy). Build a minimal stand-in
    # with just the attributes the helper touches.
    orch = Orchestrator.__new__(Orchestrator)
    orch.send = sink  # async callable
    return orch, sink


_SAMPLE_FUSION_RESULT = {
    "success": True,
    "status_code": 200,
    "data": {
        "query": "what did I do yesterday?",
        "entries": [
            {
                "type": "episode",
                "source": "episode",
                "timestamp": 1716700000.0,
                "title": "yesterday morning",
                "content": "standup at 9am",
                "metadata": {"id": "ep-1"},
            },
        ],
        "summary": "",
        "window": {"from": "2026-05-26T00:00:00", "to": "2026-05-27T00:00:00", "label": "yesterday"},
        "sources_queried": ["episode", "note", "knowledge", "calendar", "health", "screen_loop"],
        "degraded_sources": [
            {"source": "calendar", "reason": "no_token"},
            {"source": "health", "reason": "no_provider"},
            {"source": "screen_loop", "reason": "no_query_api"},
        ],
    },
    "error": None,
}


# ─────────────────────────────────────────────────────────────────────
# Layer 1 — helper-only emit shape.
#
# These four tests drive ``_emit_tool_result`` directly with a
# hand-crafted ``tool_call`` and a perfectly-shaped result payload.
# They pin the WS-frame envelope and the field-tolerant skip-paths.
# They do NOT pin live behaviour — the live model may never dispatch
# ``*__fused_timeline``, in which case this branch is dead. Layer 3
# (below) drives the live chat-stream entry point and pins that the
# timeline frame still emits via the proactive side-channel.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emits_timeline_frame_for_fused_timeline_tool(fake_orch):
    orch, sink = fake_orch
    tool_call = {
        "name": "notes_memory__fused_timeline",
        "id": "tc-1",
        "args": {"query": "what did I do yesterday?"},
    }
    await orch._emit_tool_result(
        session_id="sess-1",
        tool_call=tool_call,
        result_data=_SAMPLE_FUSION_RESULT,
        latency_ms=120.0,
    )
    types = [f.type for f in sink.frames]
    # tool_result is always emitted; the timeline frame is the new addition.
    assert "tool_result" in types
    assert "timeline" in types

    timeline_frame = next(f for f in sink.frames if f.type == "timeline")
    assert timeline_frame.session_id == "sess-1"
    p = timeline_frame.payload
    assert p["query"] == "what did I do yesterday?"
    assert p["window"]["label"] == "yesterday"
    assert len(p["entries"]) == 1
    assert p["entries"][0]["metadata"]["id"] == "ep-1"
    degraded = {d["source"] for d in p["degraded_sources"]}
    assert "calendar" in degraded
    assert "screen_loop" in degraded


@pytest.mark.asyncio
async def test_skips_timeline_frame_for_non_fusion_tools(fake_orch):
    """A normal search_notes result must NOT trigger a timeline frame."""
    orch, sink = fake_orch
    tool_call = {
        "name": "notes_memory__search_notes",
        "id": "tc-2",
        "args": {"query": "foo"},
    }
    await orch._emit_tool_result(
        session_id="sess-1",
        tool_call=tool_call,
        result_data={"success": True, "data": [{"id": "n1"}], "error": None},
        latency_ms=5.0,
    )
    assert all(f.type != "timeline" for f in sink.frames)


@pytest.mark.asyncio
async def test_skips_timeline_frame_on_unsuccessful_result(fake_orch):
    orch, sink = fake_orch
    tool_call = {
        "name": "notes_memory__fused_timeline",
        "id": "tc-3",
        "args": {"query": "yesterday"},
    }
    await orch._emit_tool_result(
        session_id="sess-1",
        tool_call=tool_call,
        result_data={"success": False, "error": "memory unavailable", "data": None},
        latency_ms=2.0,
    )
    assert all(f.type != "timeline" for f in sink.frames)


@pytest.mark.asyncio
async def test_skips_timeline_frame_when_data_shape_mismatched(fake_orch):
    """Field-tolerant: a fused_timeline call that returned malformed
    data must NOT crash the result emission. The tool_result frame
    still fires; the timeline frame is silently skipped."""
    orch, sink = fake_orch
    tool_call = {
        "name": "notes_memory__fused_timeline",
        "id": "tc-4",
        "args": {"query": "yesterday"},
    }
    await orch._emit_tool_result(
        session_id="sess-1",
        tool_call=tool_call,
        result_data={"success": True, "data": "not a dict", "error": None},
        latency_ms=1.0,
    )
    assert all(f.type != "timeline" for f in sink.frames)
    assert any(f.type == "tool_result" for f in sink.frames)


# ─────────────────────────────────────────────────────────────────────
# Layer 2 — routing closure.
#
# Temporal-recall phrasings must promote
# ``notes_memory`` so the LLM sees ``fused_timeline`` in its tool set.
#
# Background: at v2026.5.43 the live brain test showed
# claude-opus-4-7 answering "what did I do yesterday?" / "summarize
# my morning" directly from context — never invoking the
# ``notes_memory__fused_timeline`` tool — because the heuristic
# ``_R_MEMORY`` regex either missed the phrase entirely or did not
# cover enough temporal surface for the model to be prompted with
# the timeline tool. The broader regex fixes this; the tests below
# pin the new coverage.
# ─────────────────────────────────────────────────────────────────────


def _build_routing_skill(
    skill_id: str, triggers: list[str], categories: list[str] | None = None
) -> SkillManifest:
    """Minimal SkillManifest used for routing-only tests."""
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="test",
        brand=BrandProfile(
            name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"
        ),
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


@pytest.fixture
def routing_orch():
    """Heuristic-only routing harness. Mirrors the fixture in
    ``test_route_prompt_heuristic.py`` but trimmed to just what the
    timeline-routing tests need."""
    catalog = {
        "notes_memory": _build_routing_skill(
            "notes_memory",
            [
                "remember this",
                "save a note",
                "my notes",
                "recall",
                "what did i save",
            ],
            ["memory", "notes"],
        ),
        "web_search": _build_routing_skill(
            "web_search",
            ["search for", "google", "look up"],
            ["search"],
        ),
        "code_interpreter": _build_routing_skill(
            "code_interpreter",
            ["run python", "execute code", "explain"],
            ["code"],
        ),
        "calendar_google": _build_routing_skill(
            "calendar_google",
            ["what's on my calendar", "my schedule today"],
            ["calendar"],
        ),
        "smart_home_hue": _build_routing_skill(
            "smart_home_hue",
            ["turn on the lights", "dim the lights"],
            ["smart_home"],
        ),
        "spotify_music": _build_routing_skill(
            "spotify_music",
            ["play music", "play some music"],
            ["music"],
        ),
    }

    reg = MagicMock()
    reg.skills = catalog

    def _find(query: str, top_k: int = 5):
        scored = []
        for sk in catalog.values():
            s = Orchestrator._trigger_score(query, sk)
            if s > 0:
                scored.append((s, sk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [sk for _, sk in scored[:top_k]]

    reg.find_skills_for_query = _find
    reg.get_tools_for_skills = MagicMock(return_value=[])

    orch = Orchestrator.__new__(Orchestrator)
    orch.skills = reg
    # `_route_prompt` short-circuits when the LLM is unavailable so we
    # exercise the heuristic exit deterministically. The routing tests
    # below ONLY check `_heuristic_route`, which doesn't touch llm.
    orch.llm = MagicMock()
    orch.llm.available = False
    # Stub the refusal-handler hop used by the action-fallback branch.
    # The full RefusalHandler depends on the brain wiring; the routing
    # tests only need a deterministic boolean.
    refusal = MagicMock()
    refusal.query_implies_action = MagicMock(return_value=False)
    orch.refusal_handler = refusal
    return orch


@pytest.mark.parametrize(
    "prompt",
    [
        # The original failing live-brain query.
        "what did I do yesterday?",
        # "what did I do" anchored on a different time qualifier.
        "what did I do this morning",
        # Past-tense variant.
        "what have I done today",
        # Work / focus variants — common conversational phrasings.
        "what did I work on yesterday",
        "what did I focus on today",
    ],
)
def test_temporal_query_what_did_i_do_yesterday_invokes_timeline_fusion(
    routing_orch, prompt
):
    """Pattern: ``what did|have I do/done/work on/focus on …`` must
    route ``notes_memory`` so the LLM sees ``fused_timeline``."""
    skills, reason = routing_orch._heuristic_route(prompt)
    assert skills, f"no skill routed for {prompt!r} (reason={reason})"
    assert skills[0].skill_id == "notes_memory", (
        f"prompt {prompt!r}: expected notes_memory first, "
        f"got {[s.skill_id for s in skills]} (reason={reason})"
    )
    assert reason.startswith("regex:"), (
        f"expected regex shortcut for {prompt!r}, got reason={reason}"
    )


@pytest.mark.parametrize(
    "prompt",
    [
        # The second failing live-brain query.
        "summarize my morning",
        # Sibling time windows that used to slip through.
        "summarize my afternoon",
        "summarize my evening",
        "summarize my day",
        "summarize my week",
        # recap / review verbs — same intent, different verb.
        "recap my day",
        "review my morning",
        # Bare time-window queries phrased as "my <window> so far".
        "my morning so far",
        "my day in review",
    ],
)
def test_summarize_my_morning_invokes_timeline_fusion(routing_orch, prompt):
    """Pattern: ``summarize/recap/review my <window>`` must route
    notes_memory."""
    skills, reason = routing_orch._heuristic_route(prompt)
    assert skills, f"no skill routed for {prompt!r} (reason={reason})"
    assert skills[0].skill_id == "notes_memory", (
        f"prompt {prompt!r}: expected notes_memory first, "
        f"got {[s.skill_id for s in skills]} (reason={reason})"
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "what happened today",
        "what happened yesterday",
        "what went on this morning",
        "what was going on earlier",
        "earlier today",
        "earlier this morning",
        "earlier yesterday",
    ],
)
def test_what_happened_today_invokes_timeline_fusion(routing_orch, prompt):
    """Pattern: ``what happened …`` / ``earlier …`` must route
    notes_memory so fused_timeline is the LLM-visible tool."""
    skills, reason = routing_orch._heuristic_route(prompt)
    assert skills, f"no skill routed for {prompt!r} (reason={reason})"
    assert skills[0].skill_id == "notes_memory", (
        f"prompt {prompt!r}: expected notes_memory first, "
        f"got {[s.skill_id for s in skills]} (reason={reason})"
    )


@pytest.mark.parametrize(
    "prompt",
    [
        # Pure technical question — no temporal intent, must NOT take
        # the regex:memory shortcut (which would otherwise force a
        # fused-timeline fan-out for every chat turn).
        "explain the TLS handshake",
        "how does HTTPS work",
        "what is a hash table",
        # Calendar phrasing — must hit regex:calendar, not regex:memory.
        "what's on my calendar today",
        # Action verbs — different intent, different regex/trigger.
        "play some music",
        "turn on the lights",
    ],
)
def test_non_temporal_chat_does_not_invoke_timeline_fusion(routing_orch, prompt):
    """Negative control: non-temporal chat MUST NOT take the
    ``regex:memory`` shortcut. The ambiguous-tier may still surface
    notes_memory for the LLM to disambiguate, but the heuristic must
    never force-promote it as the regex:memory winner."""
    skills, reason = routing_orch._heuristic_route(prompt)
    assert reason != "regex:memory", (
        f"prompt {prompt!r}: regex:memory must NOT fire for non-"
        f"temporal chat; got reason={reason}"
    )


# ─────────────────────────────────────────────────────────────────────
# Layer 3 — live-path timeline emission.
#
# The v2026.5.43 wave was missing exactly this layer. The helper-only
# tests above (Layer 1) gave false confidence: they pinned the emit
# shape WHEN the LLM tool-calls ``*__fused_timeline``, but the live
# claude-opus-4-7 routinely answers temporal-recall queries in prose
# without dispatching the tool — so the helper branch never fired and
# the TimelineCard never mounted in the WebUI.
#
# These tests drive the live chat-stream entry point
# (``_handle_command_stream_impl``) with a mock LLM that streams
# text-only deltas (no tool_call_deltas) — exactly the live failure
# mode. The orchestrator must STILL push a ``timeline`` WS frame for
# temporal-recall queries via the proactive side-channel
# (``_maybe_emit_temporal_timeline``). Non-temporal queries
# ("explain TLS") must NOT push a timeline frame.
# ─────────────────────────────────────────────────────────────────────


def _live_skill(skill_id: str, triggers: list[str]) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id, version="1.0.0", author="test",
        brand=BrandProfile(
            name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"
        ),
        description=f"{skill_id} skill",
        trigger_phrases=triggers,
        endpoints=[
            SkillEndpoint(
                id="default", method="POST", url=f"https://x/{skill_id}",
                description="x", returns_description="x", ui_hint="detail_card",
            )
        ],
    )


_LIVE_SKILLS = {
    "notes_memory": _live_skill("notes_memory", ["my notes", "save a note"]),
    "calendar_google": _live_skill("calendar_google", ["calendar", "my agenda"]),
    "weather_current": _live_skill("weather_current", ["what's the weather"]),
}


class _RecordingMemory:
    """Minimal MemoryStore stand-in: returns a single episode in the
    "yesterday" window so ``timeline_fusion`` returns ``len(entries) >= 1``
    and the side-channel emits the frame.

    The real ``MemoryStore`` exposes ``episode_recent`` /
    ``list_recent`` as async methods; we mirror the surface
    ``_fetch_episodes`` and ``_fetch_notes`` consume.
    """

    def __init__(self, *, ts: float):
        self._ts = ts
        self.working_push = MagicMock()
        self.episode_save = AsyncMock(return_value={})
        self.log_execution = AsyncMock()

    async def episode_recent(self, *, limit: int = 1000):
        return [
            {
                "id": "ep-live-1",
                "created_at": self._ts,
                "summary": "yesterday morning standup",
                "user_message": "did standup",
                "assistant_message": "noted",
                "session_id": "s-prior",
                "tags": ["standup"],
            }
        ]

    async def list_recent(self, *, limit: int = 500):
        return []


def _build_live_orchestrator() -> Orchestrator:
    """Construct an Orchestrator wired for live chat-stream testing.

    Mirrors the ``_make_orchestrator`` helper in
    ``test_stream_nonstream_parity.py``: real ``__init__`` (so all
    sub-modules — ``ToolRunner``, ``ContextManager``, etc. — wire
    correctly), then mock the LLM + memory + skill registry.
    """
    reg = MagicMock()
    reg.skills = _LIVE_SKILLS
    reg.find_skills_for_query = lambda q, top_k=5: list(_LIVE_SKILLS.values())
    reg.get_tools_for_skills = lambda skills: [
        {
            "type": "function",
            "function": {
                "name": f"{s.skill_id}__default", "description": "", "parameters": {}
            },
        }
        for s in skills
    ]
    orch = Orchestrator(
        skill_registry=reg, send_to_client=AsyncMock(), daemons={},
        memory=None, vision_buffer=None, perception=None, learner=None,
    )
    return orch


def _capture_live_sends(orch: Orchestrator) -> list[dict]:
    captured: list[dict] = []

    async def _send(session_id: str, msg: Any) -> None:
        dumped = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
        captured.append({
            "type": dumped.get("type") or getattr(msg, "type", None),
            "payload": dumped.get("payload") or {},
            "session_id": dumped.get("session_id", session_id),
        })

    orch.send = _send
    return captured


async def _drain_pending_tasks() -> None:
    """Let any fire-and-forget ``asyncio.create_task`` background jobs
    spawned during the test (the timeline side-channel is one) reach
    completion before assertions run."""
    pending = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
def live_orch_with_yesterday_memory(monkeypatch):
    """Live orchestrator with a memory store that has one episode
    timestamped at yesterday-noon (well within the "yesterday" window
    ``parse_window`` resolves to).

    Patches ``api.state.state.memory`` so the side-channel — which
    falls back to ``api.state.state`` when ``self.memory`` is None —
    finds the same memory regardless of which lookup path runs.
    """
    from datetime import datetime, time as _dtime, timedelta
    _today_midnight = datetime.combine(datetime.now().date(), _dtime.min)
    yesterday_noon = (_today_midnight - timedelta(hours=12)).timestamp()
    memory = _RecordingMemory(ts=yesterday_noon)

    orch = _build_live_orchestrator()
    orch.memory = memory

    from api import state as state_module
    monkeypatch.setattr(state_module.state, "memory", memory, raising=False)
    monkeypatch.setattr(state_module.state, "calendar", None, raising=False)
    monkeypatch.setattr(
        state_module.state, "health_aggregator", None, raising=False
    )

    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "test-model"

    async def _stream_text_only(messages, tools=None, **kwargs):
        # Live failure mode: model answers in prose, never tool-calls
        # ``*__fused_timeline``. No tool_call_deltas, no tool_result.
        for piece in [
            "Yesterday you ", "had a standup ", "and wrote some notes.",
        ]:
            yield {"type": "text_delta", "content": piece}
        yield {"type": "done"}

    orch.llm.chat_stream = _stream_text_only
    orch.llm.extract_response = MagicMock(
        return_value=("Yesterday you had a standup and wrote some notes.", [])
    )
    orch.llm.chat_with_failover = AsyncMock(
        return_value={
            "choices": [
                {"role": "assistant", "content": "Yesterday you had a standup."}
            ]
        }
    )
    orch._route_prompt = AsyncMock(return_value=[_LIVE_SKILLS["notes_memory"]])
    orch._ensure_core_skills = lambda x: x
    return orch


@pytest.mark.asyncio
async def test_live_chat_stream_emits_timeline_frame_for_temporal_query(
    live_orch_with_yesterday_memory,
):
    """**The live-path test.**

    Drive the actual ``_handle_command_stream_impl`` (via
    ``handle_command_stream``) with a mock LLM that streams text only
    — exactly mirroring claude-opus-4-7's observed behaviour on the
    failing demo. Even though the LLM never tool-calls
    ``*__fused_timeline``, the orchestrator MUST still push a
    ``timeline`` WS frame because the heuristic detected a
    temporal-recall query and the side-channel ran the fusion.

    This test was missing from the v2026.5.43 suite — that's why the
    flagship demo was broken on a green-tests branch.
    """
    orch = live_orch_with_yesterday_memory
    sends = _capture_live_sends(orch)

    await orch.handle_command_stream(
        session_id="sess-live-1", text="what did I do yesterday?",
    )
    await _drain_pending_tasks()

    timeline_frames = [f for f in sends if f["type"] == "timeline"]
    stream_deltas = [f for f in sends if f["type"] == "stream_delta"]

    assert timeline_frames, (
        "Live chat-stream path did NOT emit a 'timeline' WS frame for a "
        "temporal-recall query. The TimelineCard will never mount in the "
        "WebUI. Outbound frame types: " + repr([f["type"] for f in sends])
    )
    assert stream_deltas, (
        "Streaming prose must continue in parallel; expected stream_delta "
        "frames alongside the timeline frame."
    )

    frame = timeline_frames[0]
    assert frame["session_id"] == "sess-live-1"
    p = frame["payload"]
    # Canonical TimelinePayload envelope — match the client contract.
    for key in (
        "session_id", "query", "window", "entries",
        "summary", "sources_queried", "degraded_sources",
    ):
        assert key in p, f"timeline payload missing canonical field {key!r}"
    assert p["query"] == "what did I do yesterday?"
    assert isinstance(p["entries"], list) and len(p["entries"]) >= 1
    # The seeded yesterday episode landed in the entries.
    ep_ids = {e.get("metadata", {}).get("id") for e in p["entries"]}
    assert "ep-live-1" in ep_ids
    assert isinstance(p["window"], dict) and p["window"].get("label")


@pytest.mark.asyncio
async def test_live_chat_stream_skips_timeline_frame_for_non_temporal_query(
    live_orch_with_yesterday_memory,
):
    """Negative control on the LIVE path.

    A non-temporal query (the canonical "explain TLS") must NOT push a
    timeline WS frame, even though the memory store has yesterday-window
    episodes loaded — the side-channel's regex gate keeps the frame
    silent on non-temporal turns.
    """
    orch = live_orch_with_yesterday_memory
    sends = _capture_live_sends(orch)

    await orch.handle_command_stream(
        session_id="sess-live-2", text="explain the TLS handshake",
    )
    await _drain_pending_tasks()

    timeline_frames = [f for f in sends if f["type"] == "timeline"]
    assert not timeline_frames, (
        "Non-temporal query must not emit a 'timeline' frame; got: "
        + repr([f["type"] for f in sends])
    )


@pytest.mark.asyncio
async def test_temporal_side_channel_skips_emit_when_no_entries(monkeypatch):
    """When ``timeline_fusion`` returns zero entries (degraded sources
    only, or a memory store with no qualifying rows), the side-channel
    MUST NOT push an empty timeline frame — the WebUI would render an
    empty card. ``entries >= 1`` is the hard gate."""
    orch = _build_live_orchestrator()
    orch.memory = None  # no memory → no entries
    from api import state as state_module
    monkeypatch.setattr(state_module.state, "memory", None, raising=False)
    monkeypatch.setattr(state_module.state, "calendar", None, raising=False)
    monkeypatch.setattr(
        state_module.state, "health_aggregator", None, raising=False
    )

    sink = _capture_live_sends(orch)
    emitted = await orch._maybe_emit_temporal_timeline(
        "sess-empty", "what did I do yesterday?",
    )
    assert emitted is False
    assert not [f for f in sink if f["type"] == "timeline"]


@pytest.mark.asyncio
async def test_temporal_side_channel_skips_non_temporal_text(monkeypatch):
    """Direct gate test: the side-channel must short-circuit on
    non-temporal text BEFORE touching the memory store. This is what
    keeps the live "explain TLS" path from spending time on a fusion
    that would be discarded anyway."""
    orch = _build_live_orchestrator()
    # If the gate is wrong and we DO reach timeline_fusion, the
    # `episode_recent` call would yield an entry — assert that doesn't
    # happen by giving memory a probe we can assert against.
    probe = MagicMock()
    probe.episode_recent = AsyncMock(return_value=[])
    probe.list_recent = AsyncMock(return_value=[])
    orch.memory = probe
    from api import state as state_module
    monkeypatch.setattr(state_module.state, "memory", probe, raising=False)
    monkeypatch.setattr(state_module.state, "calendar", None, raising=False)
    monkeypatch.setattr(
        state_module.state, "health_aggregator", None, raising=False
    )

    sink = _capture_live_sends(orch)
    emitted = await orch._maybe_emit_temporal_timeline(
        "sess-tls", "explain the TLS handshake",
    )
    assert emitted is False
    assert not [f for f in sink if f["type"] == "timeline"]
    # The regex gate must reject before touching memory.
    probe.episode_recent.assert_not_called()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("what did I do yesterday?", "yesterday"),
        ("summarize my morning", "morning"),
        ("what happened this morning", "this_morning"),
        ("recap my evening", "evening"),
        ("what did I work on this week", "this_week"),
        ("review last week", "last_week"),
        ("what did I do last tuesday", "last_tuesday"),
        ("tell me what I did today", "today"),
        ("recap my afternoon", "afternoon"),
        ("", "yesterday"),
    ],
)
def test_temporal_window_label_from_text(text, expected):
    """``parse_window`` consumes labels — pin the text-to-label
    mapping so a phrasing tweak in the side-channel doesn't silently
    regress the window resolution."""
    assert (
        Orchestrator._temporal_window_label_from_text(text) == expected
    ), f"text {text!r}: expected {expected!r}"


# ─────────────────────────────────────────────────────────────────
# Stream batching (AUDIT-r14 round3 surface spec #1)
# The orchestrator coalesces per-token text_delta frames into ~100ms
# windows: far fewer WS frames, same reconstructed text, is_final still
# emitted. FERAL_STREAM_BATCH_MS=0 restores per-token frames.
# ─────────────────────────────────────────────────────────────────

_BATCH_PIECES = [f"tok{i} " for i in range(40)]


def _attach_long_stream(orch):
    async def _stream_many(messages, tools=None, **kwargs):
        for piece in _BATCH_PIECES:
            yield {"type": "text_delta", "content": piece}
        yield {"type": "done"}
    orch.llm.chat_stream = _stream_many


@pytest.mark.asyncio
async def test_stream_batching_coalesces_frames(live_orch_with_yesterday_memory, monkeypatch):
    monkeypatch.setenv("FERAL_STREAM_BATCH_MS", "100")
    orch = live_orch_with_yesterday_memory
    _attach_long_stream(orch)
    sends = _capture_live_sends(orch)

    # Non-temporal query → no timeline side-channel noise, just prose.
    await orch.handle_command_stream(session_id="sess-batch-1", text="tell me a short joke")
    await _drain_pending_tasks()

    deltas = [f for f in sends if f["type"] == "stream_delta"]
    non_final = [f for f in deltas if not f["payload"].get("is_final")]
    finals = [f for f in deltas if f["payload"].get("is_final")]

    # 40 tokens, all arriving well within one 100ms window → coalesced
    # into far fewer non-final frames (typically 1).
    assert 0 < len(non_final) < len(_BATCH_PIECES)
    assert len(non_final) <= 3, f"expected heavy coalescing, got {len(non_final)} frames"
    # No text lost: concatenated deltas reconstruct the full stream.
    merged = "".join(f["payload"].get("delta", "") for f in non_final)
    assert merged == "".join(_BATCH_PIECES)
    # Terminal frame still emitted exactly once.
    assert len(finals) == 1


@pytest.mark.asyncio
async def test_stream_batch_ms_zero_is_per_token(live_orch_with_yesterday_memory, monkeypatch):
    monkeypatch.setenv("FERAL_STREAM_BATCH_MS", "0")
    orch = live_orch_with_yesterday_memory
    _attach_long_stream(orch)
    sends = _capture_live_sends(orch)

    await orch.handle_command_stream(session_id="sess-batch-2", text="tell me a short joke")
    await _drain_pending_tasks()

    non_final = [
        f for f in sends
        if f["type"] == "stream_delta" and not f["payload"].get("is_final")
    ]
    # Debug escape hatch: one frame per token.
    assert len(non_final) == len(_BATCH_PIECES)
