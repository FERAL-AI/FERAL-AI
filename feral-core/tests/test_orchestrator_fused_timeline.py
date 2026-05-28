"""Pin the orchestrator-side emission of the ``timeline`` WS frame.

When the LLM dispatches ``notes_memory__fused_timeline`` the
orchestrator's ``_emit_tool_result`` path now ALSO emits a dedicated
``timeline`` FeralMessage so the WebUI's TimelineCard can mount in
parallel with the streaming prose response. These tests pin that
behaviour by driving the helper directly with a fake ``send``
sink — full orchestrator boot is far too heavyweight for a focused
test, and the helper is the single integration point that matters.

The second test block pins the **routing-closure** half of the S1
gap: when the user asks a temporal-recall question, the heuristic
``_R_MEMORY`` regex must promote ``notes_memory`` so the LLM sees
``notes_memory__fused_timeline`` in its tool list. Phrasings the live
v2026.5.43 brain previously slipped through ("summarize my morning",
"what happened today", "earlier today", "recap my day") are pinned
here. Non-temporal turns must NOT route to ``notes_memory``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

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
# Routing closure — temporal-recall phrasings must promote
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
