"""
Verifies PR 9 voice/chat tool-trace parity.

Before PR 9, both ``RealtimeProxy._handle_tool_call`` and
``GeminiRealtimeProxy._handle_tool_call`` ran tools directly through
``SkillExecutor`` and reported progress via ``_send_tool_feedback``
(transcript-only). The v2 Chat composer's ``ToolTrace`` reducer
consumes ``tool_start`` / ``tool_result`` envelopes — the same ones
the orchestrator emits during text turns — so any voice-initiated
tool call was *invisible* in the chat trace beyond a stray transcript
line.

These tests pin the fix: both proxies now call the orchestrator's
emit helpers around the executor call, producing identical
envelopes to the chat path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _RecordingOrchestrator:
    """Captures the (tool_call, result) pairs the proxy hands the
    orchestrator's emit helpers. We deliberately don't ship a fake
    `send` — the contract under test is only "voice calls the emit
    methods with the right shape", not "FeralMessage round-trips"."""

    def __init__(self):
        self.starts: list[dict] = []
        self.results: list[dict] = []

    async def _emit_tool_start(self, session_id: str, tool_call: dict) -> None:
        self.starts.append({"session_id": session_id, **tool_call})

    async def _emit_tool_result(
        self, session_id: str, tool_call: dict, result_data: dict, latency_ms: float,
    ) -> None:
        self.results.append({
            "session_id": session_id,
            "tool_call": tool_call,
            "result": result_data,
            "latency_ms": latency_ms,
        })


class _FakeSkillExecutor:
    # Parameter names mirror SkillExecutor.execute (tool_name, not name).
    # A double that renames a parameter passes every positional call and
    # fails the moment a caller switches to a keyword, which is how F-01
    # stayed hidden for 40 releases.
    async def execute(self, tool_name, args, skill, endpoint):
        return {"success": True, "data": {"ok": True, "echo": args}, "error": None}


def _registry_with_endpoint(skill_id: str, endpoint_id: str):
    skill = SimpleNamespace(
        skill_id=skill_id,
        endpoints=[SimpleNamespace(id=endpoint_id)],
    )
    return SimpleNamespace(skills={skill_id: skill})


# ── OpenAI Realtime path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_voice_tool_emits_chat_trace_envelopes():
    from voice.realtime_proxy import RealtimeProxy

    orch = _RecordingOrchestrator()
    proxy = RealtimeProxy(
        skill_registry=_registry_with_endpoint("computer_use", "read_file"),
        skill_executor=_FakeSkillExecutor(),
        orchestrator=orch,
    )

    out = await proxy._handle_tool_call(
        session_id="sess-1",
        call_id="call-abc",
        name="computer_use__read_file",
        arguments='{"path": "/tmp/x"}',
    )
    # Tool result returned to the model is still a JSON string.
    assert "ok" in out

    # Emit envelopes match the chat-path tool_call shape exactly so the
    # v2 ToolTrace reducer ties them together by call_id.
    assert len(orch.starts) == 1
    s = orch.starts[0]
    assert s["session_id"] == "sess-1"
    assert s["name"] == "computer_use__read_file"
    assert s["id"] == "call-abc"
    assert s["args"] == {"path": "/tmp/x"}

    assert len(orch.results) == 1
    r = orch.results[0]
    assert r["session_id"] == "sess-1"
    assert r["tool_call"]["id"] == "call-abc"
    assert r["result"]["success"] is True
    assert r["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_openai_voice_tool_no_orchestrator_still_returns_result():
    """Backward compat: a proxy constructed without an orchestrator
    (legacy code paths, tests) must still execute tools and return
    the JSON result — it just won't emit the trace envelopes."""
    from voice.realtime_proxy import RealtimeProxy

    proxy = RealtimeProxy(
        skill_registry=_registry_with_endpoint("computer_use", "read_file"),
        skill_executor=_FakeSkillExecutor(),
        orchestrator=None,
    )
    out = await proxy._handle_tool_call(
        session_id="sess-2", call_id="cid", name="computer_use__read_file",
        arguments='{"path": "/tmp/y"}',
    )
    assert "ok" in out


@pytest.mark.asyncio
async def test_openai_voice_tool_emit_errors_do_not_break_execution():
    """Trace emission must never abort a voice tool call."""
    from voice.realtime_proxy import RealtimeProxy

    class _BrokenOrchestrator(_RecordingOrchestrator):
        async def _emit_tool_start(self, *_args, **_kwargs):
            raise RuntimeError("network down")

        async def _emit_tool_result(self, *_args, **_kwargs):
            raise RuntimeError("still down")

    proxy = RealtimeProxy(
        skill_registry=_registry_with_endpoint("computer_use", "read_file"),
        skill_executor=_FakeSkillExecutor(),
        orchestrator=_BrokenOrchestrator(),
    )
    out = await proxy._handle_tool_call(
        session_id="s", call_id="c", name="computer_use__read_file",
        arguments='{"path": "/p"}',
    )
    # Still returns the executor's payload despite emit errors.
    assert "ok" in out


# ── Gemini Live path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_voice_tool_emits_chat_trace_envelopes():
    from voice.gemini_realtime import GeminiRealtimeProxy

    orch = _RecordingOrchestrator()
    proxy = GeminiRealtimeProxy(
        skill_registry=_registry_with_endpoint("notes_memory", "search"),
        skill_executor=_FakeSkillExecutor(),
        orchestrator=orch,
    )
    out = await proxy._handle_tool_call(
        session_id="sess-g", call_id="cid-g",
        name="notes_memory__search", arguments='{"q": "hi"}',
    )
    assert "ok" in out

    assert len(orch.starts) == 1
    assert orch.starts[0]["name"] == "notes_memory__search"
    assert orch.starts[0]["id"] == "cid-g"

    assert len(orch.results) == 1
    assert orch.results[0]["tool_call"]["name"] == "notes_memory__search"
    assert orch.results[0]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_gemini_voice_tool_no_orchestrator_still_returns():
    from voice.gemini_realtime import GeminiRealtimeProxy

    proxy = GeminiRealtimeProxy(
        skill_registry=_registry_with_endpoint("notes_memory", "search"),
        skill_executor=_FakeSkillExecutor(),
        orchestrator=None,
    )
    out = await proxy._handle_tool_call(
        session_id="s", call_id="c", name="notes_memory__search",
        arguments="{}",
    )
    assert "ok" in out


_ = pytest  # keep import quiet
