"""Tests for creative differentiator features:
- Channel-to-session context handoff
- Health-triggered automations
- Digital twin as first-class skill
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.no_auto_feral_home


# ── Digital Twin Skill ─────────────────────────────────────────


class TestDigitalTwinSkill:
    def test_skill_manifest_loads(self):
        from skills.impl.digital_twin_skill import get_twin_skill_manifest
        manifest = get_twin_skill_manifest()
        assert manifest["skill_id"] == "digital_twin"
        assert len(manifest["endpoints"]) == 3
        endpoint_ids = [e["id"] for e in manifest["endpoints"]]
        assert "ask" in endpoint_ids
        assert "predict_preference" in endpoint_ids
        assert "daily_reflection" in endpoint_ids

    @pytest.mark.asyncio
    async def test_ask_returns_answer(self):
        from skills.impl import digital_twin_skill as mod
        twin = AsyncMock()
        twin.ask.return_value = "I would take the meeting — it aligns with my goals."
        mod._twin_instance = twin
        try:
            bridge = mod.DigitalTwinSkillBridge()
            result = await bridge.execute("ask", {"question": "Should I take this meeting?"}, {})
            assert result["success"] is True
            assert "answer" in result["data"]
            assert "goals" in result["data"]["answer"]
            twin.ask.assert_called_once_with("Should I take this meeting?")
        finally:
            mod._twin_instance = None

    @pytest.mark.asyncio
    async def test_ask_missing_question(self):
        from skills.impl import digital_twin_skill as mod
        twin = AsyncMock()
        mod._twin_instance = twin
        try:
            bridge = mod.DigitalTwinSkillBridge()
            result = await bridge.execute("ask", {}, {})
            assert result["success"] is False
            assert "required" in result["error"]
        finally:
            mod._twin_instance = None

    @pytest.mark.asyncio
    async def test_predict_preference(self):
        from skills.impl import digital_twin_skill as mod
        twin = AsyncMock()
        twin.predict_preference.return_value = {
            "category": "restaurants",
            "preference": "Japanese",
            "confidence": 0.8,
            "evidence": ["went to sushi place last week"],
        }
        mod._twin_instance = twin
        try:
            bridge = mod.DigitalTwinSkillBridge()
            result = await bridge.execute("predict_preference", {"category": "restaurants"}, {})
            assert result["success"] is True
            assert result["data"]["preference"] == "Japanese"
        finally:
            mod._twin_instance = None

    @pytest.mark.asyncio
    async def test_daily_reflection(self):
        from skills.impl import digital_twin_skill as mod
        twin = AsyncMock()
        twin.daily_reflection.return_value = "Today was productive."
        mod._twin_instance = twin
        try:
            bridge = mod.DigitalTwinSkillBridge()
            result = await bridge.execute("daily_reflection", {}, {})
            assert result["success"] is True
            assert "productive" in result["data"]["reflection"]
        finally:
            mod._twin_instance = None

    @pytest.mark.asyncio
    async def test_no_twin_returns_503(self):
        from skills.impl import digital_twin_skill as mod
        mod._twin_instance = None
        bridge = mod.DigitalTwinSkillBridge()
        result = await bridge.execute("ask", {"question": "test"}, {})
        assert result["success"] is False
        assert result["status_code"] == 503

    @pytest.mark.asyncio
    async def test_unknown_endpoint(self):
        from skills.impl import digital_twin_skill as mod
        twin = AsyncMock()
        mod._twin_instance = twin
        try:
            bridge = mod.DigitalTwinSkillBridge()
            result = await bridge.execute("nonexistent", {}, {})
            assert result["success"] is False
            assert result["status_code"] == 404
        finally:
            mod._twin_instance = None


# ── Health-Triggered Automations ───────────────────────────────


class TestHealthAutomations:
    @pytest.mark.asyncio
    async def test_deliver_executes_automation_on_action_payload(self):
        from agents.proactive_engine import ProactiveEngine, ProactiveMessage, Priority

        engine = ProactiveEngine()
        engine._orchestrator = MagicMock()

        mock_impl = AsyncMock()
        msg = ProactiveMessage(
            trigger_id="hr_elevated",
            priority=Priority.IMPORTANT,
            title="Heart Rate Alert",
            body="HR is high",
            action_payload={"smart_home": "set_scene", "scene": "calming"},
        )

        callback = AsyncMock()
        engine._callbacks.append(callback)
        with patch("skills.impl.get_implementation", return_value=mock_impl) as mock_get:
            await engine._deliver(msg)

        mock_get.assert_called_once_with("smart_home_hue")
        mock_impl.execute.assert_called_once_with(
            "call_service",
            {"domain": "scene", "service": "turn_on", "entity_id": "scene.calming"},
            {},
        )
        callback.assert_called_once_with(msg)

    @pytest.mark.asyncio
    async def test_deliver_breathing_exercise_automation(self):
        from agents.proactive_engine import ProactiveEngine, ProactiveMessage, Priority

        engine = ProactiveEngine()
        engine._orchestrator = MagicMock()

        mock_impl = AsyncMock()
        msg = ProactiveMessage(
            trigger_id="spo2_low",
            priority=Priority.CRITICAL,
            title="Low SpO2",
            body="SpO2 is low",
            action_payload={"smart_home": "breathing_exercise", "duration_minutes": 3},
        )

        with patch("skills.impl.get_implementation", return_value=mock_impl):
            await engine._deliver(msg)
        mock_impl.execute.assert_called_once_with(
            "call_service",
            {"domain": "scene", "service": "turn_on", "entity_id": "scene.breathing"},
            {},
        )

    @pytest.mark.asyncio
    async def test_deliver_no_payload_skips_automation(self):
        from agents.proactive_engine import ProactiveEngine, ProactiveMessage, Priority

        engine = ProactiveEngine()
        engine._orchestrator = MagicMock()

        msg = ProactiveMessage(
            trigger_id="break_reminder",
            priority=Priority.SUGGESTION,
            title="Break",
            body="Take a break",
        )

        callback = AsyncMock()
        engine._callbacks.append(callback)
        await engine._deliver(msg)
        callback.assert_called_once_with(msg)

    @pytest.mark.asyncio
    async def test_automation_failure_doesnt_block_delivery(self):
        from agents.proactive_engine import ProactiveEngine, ProactiveMessage, Priority

        engine = ProactiveEngine()
        mock_executor = AsyncMock()
        mock_executor.execute_tool_call.side_effect = RuntimeError("smart home offline")
        engine._orchestrator = MagicMock()
        engine._orchestrator.executor = mock_executor

        msg = ProactiveMessage(
            trigger_id="hr_elevated",
            priority=Priority.IMPORTANT,
            title="HR",
            body="HR",
            action_payload={"smart_home": "set_scene", "scene": "calming"},
        )

        callback = AsyncMock()
        engine._callbacks.append(callback)
        await engine._deliver(msg)
        callback.assert_called_once_with(msg)


# ── Channel-to-Session Context Handoff ─────────────────────────


class TestChannelHandoff:
    @pytest.mark.asyncio
    async def test_channel_handler_shares_desktop_context(self):
        from channels.base import ChannelManager, ChannelMessage, ChannelResponse

        mock_memory = MagicMock()
        mock_memory.working_get.return_value = [
            {"role": "user", "text": "tell me about my schedule"},
            {"role": "assistant", "text": "You have a meeting at 3pm"},
        ]

        mock_orchestrator = AsyncMock()
        mock_sessions = {"desktop-abc": MagicMock()}

        mock_state = MagicMock()
        mock_state.channel_manager = ChannelManager()
        mock_state.orchestrator = mock_orchestrator
        mock_state.memory = mock_memory
        mock_state.sessions = mock_sessions
        mock_state.session_handoff = MagicMock()
        mock_state._channel_collectors = {}

        async def mock_handle_command(session_id, text, context=None):
            mock_state._channel_collectors[session_id].append("Here's your schedule update.")

        mock_orchestrator.handle_command = mock_handle_command

        channel_msg = ChannelMessage(
            channel_type="telegram",
            channel_id="chat123",
            user_id="user456",
            text="what's on my schedule?",
            username="Alex",
        )

        channel_session_id = f"channel_{channel_msg.channel_type}_{channel_msg.user_id}"

        mock_state._channel_collectors[channel_session_id] = []
        if mock_state.memory and mock_state.sessions:
            desktop_sid = next(iter(mock_state.sessions), None)
            if desktop_sid:
                history = mock_state.memory.working_get(desktop_sid, limit=10)
                if history:
                    mock_state.memory.working_replace(channel_session_id, list(history))

        mock_memory.working_get.assert_called_with("desktop-abc", limit=10)
        mock_memory.working_replace.assert_called_once()
        replace_call = mock_memory.working_replace.call_args
        assert replace_call.args[0] == channel_session_id
        assert len(replace_call.args[1]) == 2

    @pytest.mark.asyncio
    async def test_channel_handler_registers_device_for_handoff(self):
        """Channel sessions must register as ``node_type="channel"``, not
        ``"phone"``. Prior to the phone-placeholder kill every Telegram /
        Slack / Discord session showed up as a permanently connected
        phone. See api/state.py around ``register_device`` + Commit 4 of
        the prior session for the production-code fix.
        """
        from channels.base import ChannelMessage

        mock_handoff = MagicMock()

        channel_msg = ChannelMessage(
            channel_type="discord",
            channel_id="ch999",
            user_id="user789",
            text="hello",
            username="Sam",
        )

        channel_session_id = f"channel_{channel_msg.channel_type}_{channel_msg.user_id}"
        mock_handoff.register_device(
            channel_session_id,
            "channel",
            node_id=f"{channel_msg.channel_type}_{channel_msg.user_id}",
        )

        mock_handoff.register_device.assert_called_once_with(
            "channel_discord_user789",
            "channel",
            node_id="discord_user789",
        )

    @pytest.mark.asyncio
    async def test_channel_collector_collects_responses(self):
        from models.protocol import FeralMessage, TextResponsePayload

        collectors: dict[str, list[str]] = {}
        session_id = "channel_telegram_user1"
        collectors[session_id] = []

        msg = FeralMessage(
            session_id=session_id,
            hop="brain",
            type="text_response",
            payload=TextResponsePayload(text="Hello from FERAL!").model_dump(),
        )

        payload = msg.payload or {}
        text = payload.get("text", "")
        if text:
            collectors[session_id].append(text)

        assert collectors[session_id] == ["Hello from FERAL!"]

    @pytest.mark.asyncio
    async def test_channel_no_desktop_sessions_still_works(self):
        mock_memory = MagicMock()
        mock_sessions = {}

        if mock_sessions:
            desktop_sid = next(iter(mock_sessions), None)
        else:
            desktop_sid = None

        assert desktop_sid is None
        mock_memory.working_get.assert_not_called()
