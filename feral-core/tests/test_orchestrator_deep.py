"""
Extended orchestrator tests — covers handle_command paths not reached by the
existing test_orchestrator.py: direct mode, proactive triggers, session
disconnect, biometric updates, multi-agent detection, and skill routing.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.orchestrator import Orchestrator
from models.protocol import FeralMessage
from models.skill_manifest import WEATHER_SKILL
from perception.fusion import PerceptionEngine, PerceptionFrame


@pytest.fixture
def send():
    return AsyncMock()


@pytest.fixture
def memory():
    m = MagicMock()
    m.episode_save = AsyncMock()
    m.working_push = MagicMock()
    m.log_execution = AsyncMock()
    m.build_context_for_llm = AsyncMock(return_value="ctx")
    m.episode_recent = AsyncMock(return_value=[])
    return m


@pytest.fixture
def orch(send, memory, monkeypatch):
    monkeypatch.delenv("FERAL_MULTI_AGENT", raising=False)
    reg = MagicMock()
    reg.skills = {"weather_current": WEATHER_SKILL}
    reg.find_skills_for_query = MagicMock(return_value=[WEATHER_SKILL])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    return Orchestrator(
        skill_registry=reg,
        send_to_client=send,
        daemons={},
        memory=memory,
        vision_buffer=None,
        perception=PerceptionEngine(),
        learner=None,
    )


class TestHandleCommandDirect:
    @pytest.mark.asyncio
    async def test_no_llm_falls_to_direct(self, orch, send):
        mock_llm = MagicMock()
        mock_llm.available = False
        orch.llm = mock_llm
        with patch.object(orch, "_direct_execute", new_callable=AsyncMock) as de:
            await orch.handle_command("s1", "hello world")
        de.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_error_falls_to_direct(self, orch, send):
        mock_llm = MagicMock()
        mock_llm.available = True
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
        mock_llm.chat_with_failover = mock_llm.chat
        orch.llm = mock_llm
        with patch.object(orch, "_direct_execute", new_callable=AsyncMock) as de:
            await orch.handle_command("s1", "do something")
        de.assert_awaited_once()


class TestHandleCommandWithLLM:
    @pytest.mark.asyncio
    async def test_text_response_sent_to_client(self, orch, send):
        mock_llm = MagicMock()
        mock_llm.available = True
        mock_llm.chat = AsyncMock(return_value={"choices": [{"message": {"content": "Hi there"}}]})
        mock_llm.chat_with_failover = mock_llm.chat
        mock_llm.extract_response = MagicMock(return_value=("Hi there", []))
        orch.llm = mock_llm

        with patch.object(orch, "_send_text", new_callable=AsyncMock) as st:
            await orch.handle_command("s1", "hello")
        st.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tool_call_executed(self, orch, send, memory):
        tool_call = {"id": "tc1", "name": "weather_current__current", "args": {"lat": 0, "lon": 0}}
        mock_llm = MagicMock()
        mock_llm.available = True
        mock_llm.chat = AsyncMock(side_effect=[
            {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "weather_current__current", "arguments": '{"lat":0,"lon":0}'}}
            ]}}]},
            {"choices": [{"message": {"content": "The weather is sunny"}}]},
        ])
        mock_llm.chat_with_failover = mock_llm.chat
        mock_llm.extract_response = MagicMock(side_effect=[
            ("", [tool_call]),
            ("The weather is sunny", []),
        ])
        orch.llm = mock_llm

        with patch.object(orch, "_execute_tool_call_for_llm", new_callable=AsyncMock, return_value={"success": True, "data": {}}):
            with patch.object(orch, "_send_text", new_callable=AsyncMock) as st:
                await orch.handle_command("s1", "weather?")
        st.assert_awaited_once()
        memory.log_execution.assert_called()


class TestProactiveTriggers:
    @pytest.mark.asyncio
    async def test_proactive_disabled_is_noop(self, orch):
        orch._proactive_enabled = False
        mock_llm = MagicMock()
        mock_llm.available = True
        orch.llm = mock_llm
        await orch.check_proactive_triggers("s1")
        # No crash, no action

    @pytest.mark.asyncio
    async def test_proactive_cooldown_respected(self, orch):
        orch._proactive_enabled = True
        mock_llm = MagicMock()
        mock_llm.available = True
        orch.llm = mock_llm
        import time
        orch._last_proactive_check["s1"] = time.time()
        await orch.check_proactive_triggers("s1")

    @pytest.mark.asyncio
    async def test_proactive_high_hr_fires(self, orch, memory):
        orch._proactive_enabled = True
        mock_llm = MagicMock()
        mock_llm.available = True
        orch.llm = mock_llm
        frame = PerceptionFrame()
        frame.heart_rate = 160
        orch.perception.update_sensors("s1", {"vitals": {"ppg_heart_rate": 160}})

        with patch.object(orch, "handle_command", new_callable=AsyncMock) as hc:
            orch._last_proactive_check["s1"] = 0
            await orch.check_proactive_triggers("s1")
        hc.assert_awaited_once()
        args = hc.call_args
        assert "HEALTH ALERT" in args[1].get("text", args[0][1] if len(args[0]) > 1 else "")


class TestSessionDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_clears_state(self, orch):
        orch.conversation_history["s1"] = [{"role": "user", "content": "hi"}]
        orch._last_proactive_check["s1"] = 100
        await orch.on_session_disconnect("s1")
        assert "s1" not in orch.conversation_history
        assert "s1" not in orch._last_proactive_check

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self, orch):
        await orch.on_session_disconnect("s1")
        await orch.on_session_disconnect("s1")  # second call is a no-op

    @pytest.mark.asyncio
    async def test_disconnect_with_learner(self, orch):
        learner = AsyncMock()
        orch.learner = learner
        await orch.on_session_disconnect("s2")
        learner.extract_knowledge.assert_awaited_once_with("s2")
        learner.summarize_session.assert_awaited_once_with("s2")


class TestBiometricUpdate:
    def test_update_biometric_stores(self, orch):
        orch.update_biometric("s1", {"hr": 72, "spo2": 98})
        assert orch.biometric_state["s1"]["hr"] == 72


class TestMultiAgent:
    def test_multi_agent_enabled_by_default(self, orch):
        """With no env var set, the orchestrator agrees with settings.

        This used to assert False, matching a literal "false" default in
        ``Orchestrator.__init__`` that disagreed with
        ``DEFAULT_SETTINGS["features"]["multi_agent"] is True``. Since
        ``export_as_env`` publishes FERAL_MULTI_AGENT=true at boot, the
        False was only ever reachable in a process where the config
        loader had not run, which is to say in tests like this one and
        not on any real install. Both readers now share
        ``config.loader.DEFAULT_MULTI_AGENT``.
        """
        from config.loader import DEFAULT_MULTI_AGENT

        assert orch._multi_agent_enabled is DEFAULT_MULTI_AGENT
        assert orch._multi_agent_enabled is True

    def test_multi_agent_disabled_via_env(self, send, memory, monkeypatch):
        """An explicit opt-out still wins over the shared default."""
        monkeypatch.setenv("FERAL_MULTI_AGENT", "false")
        reg = MagicMock()
        reg.skills = {}
        o = Orchestrator(
            skill_registry=reg, send_to_client=send,
            daemons={}, memory=memory,
        )
        assert o._multi_agent_enabled is False

    def test_multi_agent_enabled_via_env(self, send, memory, monkeypatch):
        monkeypatch.setenv("FERAL_MULTI_AGENT", "true")
        reg = MagicMock()
        reg.skills = {}
        o = Orchestrator(
            skill_registry=reg, send_to_client=send,
            daemons={}, memory=memory,
        )
        assert o._multi_agent_enabled is True


class TestRoutingHelpers:
    def test_ensure_core_skills_adds_missing(self, orch):
        orch.skills.skills["desktop_control"] = MagicMock(skill_id="desktop_control")
        result = orch._ensure_core_skills([WEATHER_SKILL])
        ids = {s.skill_id if hasattr(s, "skill_id") else s for s in result}
        assert "desktop_control" in ids

    def test_runtime_status(self, orch):
        status = orch.runtime_status
        assert "multi_agent_enabled" in status
        assert "pending_confirmations" in status

    def test_set_llm(self, orch):
        mock_llm = MagicMock()
        orch.set_llm(mock_llm)
        assert orch.llm is mock_llm

    def test_set_vault(self, orch):
        mock_vault = MagicMock()
        orch.set_vault(mock_vault)

    def test_set_mcp_client(self, orch):
        mock_mcp = MagicMock()
        orch.set_mcp_client(mock_mcp)
        assert orch._mcp_client is mock_mcp
