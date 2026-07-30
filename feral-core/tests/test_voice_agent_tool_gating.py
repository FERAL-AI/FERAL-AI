"""Regression tests for Batch 1 fix #4 — safety gating of the
voice-realtime and multi-agent tool-execution paths.

Before this fix, ``RealtimeProxy``, ``GeminiRealtimeProxy`` and the
multi-agent ``AgentWorker`` called ``SkillExecutor.execute`` with NO
policy check, so a CRITICAL shell tool (or any WARN/CONFIRM tool) would
run on a hands-free voice turn or inside an autonomous worker. These
surfaces have no inline approval loop, so only AUTO-tier tools may run;
everything else is refused while the tool_start/tool_result trace is
still emitted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _RecordingOrchestrator:
    def __init__(self):
        self.starts: list[dict] = []
        self.results: list[dict] = []

    async def _emit_tool_start(self, session_id, tool_call):
        self.starts.append({"session_id": session_id, **tool_call})

    async def _emit_tool_result(self, session_id, tool_call, result_data, latency_ms):
        self.results.append({"tool_call": tool_call, "result": result_data})


class _SpyExecutor:
    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, name, args, skill, endpoint):
        self.calls.append(name)
        return {"success": True, "data": {"ok": True}, "error": None}


def _registry(skill_id: str, endpoint_id: str):
    skill = SimpleNamespace(skill_id=skill_id, endpoints=[SimpleNamespace(id=endpoint_id)])
    return SimpleNamespace(skills={skill_id: skill}, get_all_tools=lambda: [])


# ── Voice: OpenAI realtime ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_voice_blocks_critical_shell_tool():
    from voice.realtime_proxy import RealtimeProxy

    orch = _RecordingOrchestrator()
    spy = _SpyExecutor()
    proxy = RealtimeProxy(
        skill_registry=_registry("desktop_control", "shell_command"),
        skill_executor=spy,
        orchestrator=orch,
    )

    out = await proxy._handle_tool_call(
        session_id="s",
        call_id="c",
        name="desktop_control__shell_command",
        arguments='{"cmd": "rm -rf /"}',
    )

    data = json.loads(out)
    assert data["success"] is False
    assert "blocked" in data["error"].lower()
    assert spy.calls == []
    # Trace is still emitted: a start plus a result carrying the refusal.
    assert len(orch.starts) == 1
    assert len(orch.results) == 1
    assert orch.results[0]["result"]["success"] is False


# ── Voice: Gemini realtime ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_voice_blocks_critical_shell_tool():
    from voice.gemini_realtime import GeminiRealtimeProxy

    orch = _RecordingOrchestrator()
    spy = _SpyExecutor()
    proxy = GeminiRealtimeProxy(
        skill_registry=_registry("desktop_control", "shell_command"),
        skill_executor=spy,
        orchestrator=orch,
    )

    out = await proxy._handle_tool_call(
        session_id="s",
        call_id="c",
        name="desktop_control__shell_command",
        arguments='{"cmd": "rm -rf /"}',
    )

    data = json.loads(out)
    assert data["success"] is False
    assert spy.calls == []
    assert len(orch.results) == 1
    assert orch.results[0]["result"]["success"] is False


# ── Multi-agent worker ────────────────────────────────────────────────


class _FakeLLM:
    available = True

    def __init__(self, tool_calls):
        self._tool_calls = tool_calls
        self._round = 0

    async def chat(self, **kwargs):
        return {"choices": [{"message": {}}]}

    def extract_response(self, response):
        self._round += 1
        if self._round == 1:
            return "", self._tool_calls
        return "final answer", []


@pytest.mark.asyncio
async def test_multi_agent_blocks_critical_shell_tool():
    from agents.multi_agent import AgentWorker

    spy = _SpyExecutor()
    worker = AgentWorker(
        worker_id="w1",
        name="test",
        system_prompt="you are a test worker",
        skill_ids=[],
        llm=_FakeLLM(
            [{"name": "desktop_control__shell_command", "args": {"cmd": "rm -rf /"}, "id": "t1"}]
        ),
        skill_registry=_registry("desktop_control", "shell_command"),
        skill_executor=spy,
    )

    result = await worker.run(session_id="s", user_text="run a shell command")

    assert spy.calls == []
    assert result.tool_results
    assert result.tool_results[0]["success"] is False
    assert "blocked" in result.tool_results[0]["error"].lower()


def test_agent_safety_refusal_blocks_warn_allows_auto():
    """Unit-level: the agent gate refuses a WARN tool and permits an
    AUTO tool (registry=None so no manifest lookup is needed)."""
    from agents.multi_agent import AgentWorker

    worker = AgentWorker(
        worker_id="w2", name="t", system_prompt="p", skill_ids=[],
        skill_registry=None,
    )

    # WARN (danger map -> CONFIRM) is refused.
    assert worker._safety_refusal("browser__navigate") is not None
    # AUTO (read-only search) is allowed.
    assert worker._safety_refusal("notes__search") is None
