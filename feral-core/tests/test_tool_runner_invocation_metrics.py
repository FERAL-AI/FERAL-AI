"""Per-tool invocation instrumentation.

``ALWAYS_INCLUDE_SKILLS`` puts 59 tools in front of the model on every turn
and the chat path applies no cap at all (``MAX_LLM_TOOLS`` binds only inside
``_run_subagent_task``). Trimming that surface needs evidence about which
tools the model actually reaches for, so ``ToolRunner`` counts every real
invocation per tool.

This file only pins the counter. It deliberately does not assert anything
about trimming the always-include set or capping the chat path; both are
separate, separately-approved changes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import skills.impl  # noqa: F401,E402  register backing skills

from agents.tool_runner import ToolRunner  # noqa: E402
from observability import metrics  # noqa: E402
from skills.registry import SkillRegistry  # noqa: E402

METRIC = "feral_tool_invocations_total"


def _make_runner() -> ToolRunner:
    reg = SkillRegistry()
    reg.load_builtin_skills()
    orch = MagicMock()
    orch.skills = reg
    orch.executor = MagicMock()
    orch.executor.execute = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {}, "error": None},
    )
    orch._mcp_client = None
    orch.daemons = {}
    orch._session_surfaces = {}
    return ToolRunner(orch, autonomy_mode="loose")


@pytest.fixture(autouse=True)
def clean_metrics():
    metrics._reset_inmem()
    yield
    metrics._reset_inmem()


@pytest.mark.asyncio
class TestInvocationCounter:
    async def test_llm_tool_call_increments_the_counter(self):
        runner = _make_runner()

        with patch("observability.metrics.increment") as inc:
            await runner.execute_tool_call_for_llm(
                "s1",
                {
                    "name": "feral_reminders__list",
                    "args": {"include_completed": False},
                    "id": "tc1",
                },
                [],
            )

        names = [call.args[0] for call in inc.call_args_list]
        assert METRIC in names

        dimensional = next(c for c in inc.call_args_list if c.args[0] == METRIC)
        assert dimensional.kwargs["attributes"]["tool"] == "feral_reminders__list"
        assert dimensional.kwargs["attributes"]["session"] == "s1"

    async def test_counter_keeps_tool_identity_without_otel(self):
        """The in-process fallback drops ``attributes`` entirely, so the
        per-tool counter name is what makes the evidence readable."""
        runner = _make_runner()

        await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "feral_reminders__list", "args": {"include_completed": False}, "id": "t1"},
            [],
        )
        await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "feral_reminders__list", "args": {"include_completed": True}, "id": "t2"},
            [],
        )

        counters = metrics.in_memory_snapshot()["counters"]
        assert counters[f"{METRIC}_feral_reminders__list"] == 2
        assert counters[METRIC] == 2

    async def test_distinct_tools_get_distinct_counters(self):
        runner = _make_runner()

        await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "feral_reminders__list", "args": {"include_completed": False}, "id": "t1"},
            [],
        )
        await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "web_search__search", "args": {"query": "feral"}, "id": "t2"},
            [],
        )

        counters = metrics.in_memory_snapshot()["counters"]
        per_tool = {k: v for k, v in counters.items() if k.startswith(f"{METRIC}_")}
        assert len(per_tool) == 2, per_tool
        assert all(v == 1 for v in per_tool.values())

    async def test_blocked_calls_are_still_counted(self):
        """Attempts are the signal for right-sizing, not just successes."""
        runner = _make_runner()

        result = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "smart_home_hue__list_lights", "args": {}, "id": "tc9"},
            [],
        )

        assert result.get("error") or result.get("is_error")
        counters = metrics.in_memory_snapshot()["counters"]
        assert counters[f"{METRIC}_smart_home_hue__list_lights"] == 1

    async def test_metric_name_is_prometheus_legal(self):
        runner = _make_runner()
        assert runner._metric_safe("mcp_server.tool-name") == "mcp_server_tool_name"

    async def test_instrumentation_failure_never_breaks_dispatch(self):
        runner = _make_runner()

        with patch("observability.metrics.increment", side_effect=RuntimeError("boom")):
            result = await runner.execute_tool_call_for_llm(
                "s1",
                {
                    "name": "feral_reminders__list",
                    "args": {"include_completed": False},
                    "id": "tc1",
                },
                [],
            )

        assert result.get("success") is True, result
