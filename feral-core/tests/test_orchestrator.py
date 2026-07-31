"""
Unit tests for FERAL orchestrator (`agents.orchestrator`).

Covers initialization, routing, context compaction, system prompt assembly,
GenUI enrichment for location-like tool results, and conversation history.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.orchestrator import Orchestrator
from models.protocol import FeralMessage
from models.skill_manifest import WEATHER_SKILL
from perception.fusion import PerceptionFrame


@pytest.fixture
def async_send() -> AsyncMock:
    """Mock WebSocket / client send coroutine."""
    return AsyncMock()


@pytest.fixture
def orchestrator(async_send: AsyncMock) -> Orchestrator:
    """Minimal orchestrator with mocked registry and send hook."""
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    return Orchestrator(
        skill_registry=reg,
        send_to_client=async_send,
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
    )


class TestOrchestratorInit:
    """Constructor wiring."""

    def test_init_has_expected_attributes(self, orchestrator: Orchestrator) -> None:
        """Core collaborators and mutable state exist after construction."""
        assert orchestrator.skills is not None
        assert orchestrator.send is not None
        assert orchestrator.daemons == {}
        assert orchestrator.conversation_history == {}
        assert orchestrator.executor is not None
        assert orchestrator.genui is not None
        assert orchestrator.llm is None


class TestRoutingAndContext:
    """Prompt routing and history helpers."""

    @pytest.mark.asyncio
    async def test_route_prompt_returns_skill_manifests_mock_registry(
        self, orchestrator: Orchestrator
    ) -> None:
        """`_route_prompt` returns manifests from the registry when LLM is off."""
        orchestrator.skills.skills = {"weather_current": WEATHER_SKILL}
        orchestrator.skills.find_skills_for_query = MagicMock(return_value=[WEATHER_SKILL])
        mock_llm = MagicMock()
        mock_llm.available = False
        orchestrator.llm = mock_llm

        out = await orchestrator._route_prompt("what is the weather today")
        assert out == [WEATHER_SKILL]
        orchestrator.skills.find_skills_for_query.assert_called()

    def test_compact_context_keeps_recent_turns(self, orchestrator: Orchestrator) -> None:
        """Compaction windows by TURN, not raw row count.

        The window used to be 15 raw rows, so one assistant turn with
        six parallel tool calls evicted the rest of the session. It is
        now the newest ``max_turns`` user turns, whole, and it never
        cuts away the last assistant message.
        """
        long_hist: list[dict] = []
        for i in range(20):
            long_hist.append({"role": "user", "content": f"u{i}"})
            long_hist.append({"role": "assistant", "content": f"a{i}"})
        compact = orchestrator._compact_context(long_hist)
        assert len(compact) == 24  # 12 user turns x (user + assistant)
        assert compact[0]["content"] == "u8"
        assert compact[-1]["content"] == "a19"
        roles = [m["role"] for m in compact]
        assert not any(a == b == "user" for a, b in zip(roles, roles[1:]))

    async def test_build_system_prompt_includes_identity_and_skills(
        self, orchestrator: Orchestrator
    ) -> None:
        """System prompt contains identity text and skill branding.

        The agents-prompt deepening pass renamed the legacy "How to
        respond" section to "Tone & Companionship" and added a
        Tool-Selection Discipline header. Assert against the new
        sentinels — the structural contract (identity threaded in,
        skill branding visible) is unchanged.
        """
        frame = PerceptionFrame()
        with patch.object(orchestrator, "_load_identity", return_value="CUSTOM_IDENTITY_LINE"):
            text = await orchestrator._build_system_prompt(frame, [WEATHER_SKILL], "session-z")
        assert "CUSTOM_IDENTITY_LINE" in text
        assert "Tone & Companionship" in text or "Tool-Selection Discipline" in text
        assert "Weather" in text or "Relevant skills" in text


class TestGenUIAndHistory:
    """GenUI side effects and conversation list growth."""

    @pytest.mark.asyncio
    async def test_try_genui_for_result_location_data_mocks_engine(
        self, orchestrator: Orchestrator
    ) -> None:
        """Rich location results trigger GenUI generation via self.genui.generate()."""
        orchestrator.skills.skills = {"weather_current": WEATHER_SKILL}
        tool_call = {"name": "weather_current__current", "id": "tc1"}
        result_data = {
            "success": True, "status_code": 200,
            "data": {"lat": 40.7, "lon": -74.0, "label": "NYC"},
            "error": None,
        }

        orchestrator.genui.generate = MagicMock(
            return_value={"type": "VStack", "children": [{"type": "MapView"}]}
        )
        await orchestrator._try_genui_for_result("sess-1", tool_call, result_data)

        orchestrator.genui.generate.assert_called_once()
        call_kwargs = orchestrator.genui.generate.call_args
        assert call_kwargs[1]["data"]["lat"] == 40.7
        orchestrator.send.assert_awaited()
        sent = orchestrator.send.call_args[0]
        assert isinstance(sent[1], FeralMessage)
        assert sent[1].type == "sdui"

    def test_conversation_history_grows_on_user_message(self, orchestrator: Orchestrator) -> None:
        """Pushing a user entry increases per-session history length."""
        sid = "sess-grow"
        orchestrator.conversation_history[sid] = []
        orchestrator.conversation_history[sid].append(
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        )
        assert len(orchestrator.conversation_history[sid]) == 1


class TestSubagentTool:
    """Parallel subagent execution helper behavior."""

    @pytest.mark.asyncio
    async def test_spawn_subagents_requires_tasks(self, orchestrator: Orchestrator) -> None:
        out = await orchestrator._spawn_subagents_for_task("sess-1", {})
        assert out["success"] is False
        assert out["status_code"] == 400

    @pytest.mark.asyncio
    async def test_spawn_subagents_parallel_happy_path(self, orchestrator: Orchestrator) -> None:
        mock_llm = MagicMock()
        mock_llm.available = True
        mock_llm.chat = AsyncMock(return_value={"choices": [{"message": {"content": "done"}}]})
        mock_llm.extract_response = MagicMock(return_value=("done", []))
        orchestrator.llm = mock_llm

        out = await orchestrator._spawn_subagents_for_task(
            "sess-1",
            {"tasks": ["task a", "task b"], "max_workers": 2, "max_iterations": 2},
        )
        assert out["success"] is True
        assert out["status_code"] == 200
        assert out["data"]["task_count"] == 2
        assert out["data"]["success_count"] == 2
        assert len(out["data"]["results"]) == 2
