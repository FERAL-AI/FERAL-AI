"""Lane 08 — before/after latency numbers for the PR body.

This file isn't a regression test; it's a one-shot benchmark that
prints p50 / p95 / p99 for the orchestrator hot path. Capture the
numbers and paste into the PR. Skipped under regular pytest runs.

Usage:
    pytest tests/perf/test_lane08_latency.py -m perf -s --no-cov
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest


pytestmark = pytest.mark.perf


def _skill(skill_id: str, triggers: list[str]) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id, version="1.0.0", author="test",
        brand=BrandProfile(name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"),
        description=f"{skill_id} skill",
        trigger_phrases=triggers,
        endpoints=[SkillEndpoint(
            id="default", method="POST", url=f"https://x/{skill_id}",
            description="x", returns_description="x", ui_hint="detail_card",
        )],
    )


def _make_orchestrator(*, simulate_slow_save_ms: float = 0.0) -> Orchestrator:
    reg = MagicMock()
    reg.skills = {
        "notes_memory": _skill("notes_memory", ["my notes", "recall", "what did i save"]),
        "calendar_google": _skill("calendar_google", ["my calendar", "schedule"]),
        "weather_current": _skill("weather_current", ["what's the weather"]),
    }
    reg.find_skills_for_query = lambda q, top_k=5: list(reg.skills.values())
    reg.get_tools_for_skills = lambda x: []
    orch = Orchestrator(
        skill_registry=reg, send_to_client=AsyncMock(), daemons={},
        memory=None, vision_buffer=None, perception=None, learner=None,
    )

    async def fake_episode_save(**_):
        if simulate_slow_save_ms:
            await asyncio.sleep(simulate_slow_save_ms / 1000.0)
        return {"event_type": "user_command"}

    memory = MagicMock()
    memory.episode_save = AsyncMock(side_effect=fake_episode_save)
    memory.working_push = MagicMock()
    memory.log_execution = AsyncMock()
    orch.memory = memory

    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "test"
    orch.llm.chat_with_failover = AsyncMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    })
    orch.llm.extract_response = MagicMock(return_value=("ok", []))
    return orch


@pytest.mark.asyncio
async def test_handle_command_latency_distribution():
    """Hit handle_command 200 times and print latency percentiles.

    Uses the heuristic-routed prompt "what did I do yesterday" which
    must exit WS2's routing without an LLM call, then drives a
    single fast LLM iteration via the mocked chat_with_failover.
    Slow-save is configured at 1000ms to prove WS1's fire-and-forget
    keeps the hot path snappy even when SQLite is pathologically
    slow.
    """
    orch = _make_orchestrator(simulate_slow_save_ms=1000.0)

    # Warm so the first call's cold imports don't pollute p99.
    await orch.handle_command(session_id="warmup", text="warmup")

    latencies_ms: list[float] = []
    for _ in range(200):
        t0 = time.perf_counter()
        await orch.handle_command(
            session_id="bench-12345678", text="what did I do yesterday",
        )
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    latencies_ms.sort()

    def _pct(values: list[float], p: float) -> float:
        idx = int(round(p * (len(values) - 1)))
        return values[idx]

    summary = {
        "p50_ms": _pct(latencies_ms, 0.50),
        "p95_ms": _pct(latencies_ms, 0.95),
        "p99_ms": _pct(latencies_ms, 0.99),
        "max_ms": latencies_ms[-1],
        "mean_ms": statistics.fmean(latencies_ms),
        "samples": len(latencies_ms),
    }
    print("\nLANE_08_LATENCY=" + json.dumps(summary))

    # Hot-path budget. The slow-save sleeps 1s but it's
    # fire-and-forget, so the hot path stays well under 100ms.
    assert summary["p99_ms"] < 200.0, summary
