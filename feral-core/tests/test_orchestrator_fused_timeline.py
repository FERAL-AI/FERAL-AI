"""Pin the orchestrator-side emission of the ``timeline`` WS frame.

When the LLM dispatches ``notes_memory__fused_timeline`` the
orchestrator's ``_emit_tool_result`` path now ALSO emits a dedicated
``timeline`` FeralMessage so the WebUI's TimelineCard can mount in
parallel with the streaming prose response. These tests pin that
behaviour by driving the helper directly with a fake ``send``
sink — full orchestrator boot is far too heavyweight for a focused
test, and the helper is the single integration point that matters.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.protocol import FeralMessage  # noqa: E402
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
