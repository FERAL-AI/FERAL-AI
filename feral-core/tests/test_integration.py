"""
FERAL Integration Tests — End-to-End Brain Testing
=====================================================
Tests the full stack: config → server → WebSocket → daemon → skills → memory.
Uses mock daemons and clients to verify the complete pipeline.
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture(autouse=True)
def _clean_feral_env_integration(monkeypatch, tmp_path):
    """Remove FERAL_* env vars leaked by earlier test modules."""
    for key in list(os.environ):
        if key.startswith("FERAL_"):
            monkeypatch.delenv(key, raising=False)
    # The config fixture below uses this exact profile. Vault/audit operations
    # must stay there after resetting ambient settings, including on macOS.
    user_home = tmp_path / ".feral"
    monkeypatch.setenv("FERAL_HOME", str(user_home))
    monkeypatch.setenv("FERAL_AUDIT_LOG_PATH", str(user_home / "audit.log"))

import pytest

from config.loader import ConfigLoader, DEFAULT_SETTINGS
from models.protocol import (
    FeralMessage, NodeRegisterPayload, TextCommandPayload,
    parse_message, MESSAGE_TYPES,
)
from models.skill_manifest import SkillManifest, SkillEndpoint, BrandProfile, AuthConfig
from skills.executor import SkillExecutor
from skills.registry import SkillRegistry
from memory.store import MemoryStore
from perception.fusion import PerceptionEngine, PerceptionFrame


# ─── Fixtures ───

@pytest.fixture
def memory(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def perception():
    return PerceptionEngine()


@pytest.fixture
def skill_registry():
    reg = SkillRegistry()
    reg.load_builtin_skills()
    return reg


@pytest.fixture
def config(tmp_path):
    loader = ConfigLoader(project_dir=str(tmp_path))
    loader.user_home = tmp_path / ".feral"
    loader.user_home.mkdir()
    loader.discover()
    return loader


# ─── Config + Setup Flow ───

class TestSetupFlow:
    """Test the complete setup wizard flow end-to-end."""

    def test_fresh_install_not_complete(self, config):
        assert config.setup_complete is False

    def test_setup_with_api_key(self, config):
        config.save_credentials({"OPENAI_API_KEY": "sk-test-123"})
        (config.user_home / "USER.md").write_text("My name is Test User. I work on AI projects and health technology.")
        config.discover()
        assert config.setup_complete is True

    def test_setup_settings_persist(self, config):
        config.update_settings("llm", "provider", "groq")
        config.update_settings("llm", "model", "mixtral-8x7b")
        config.update_settings("features", "streaming", True)

        reloaded = ConfigLoader(project_dir=str(config.project_dir))
        reloaded.user_home = config.user_home
        reloaded.discover()
        assert reloaded.get("llm", "provider") == "groq"
        assert reloaded.get("llm", "model") == "mixtral-8x7b"
        assert reloaded.get("features", "streaming") is True

    def test_credential_vault_isolation(self, config):
        config.save_credentials({"OPENAI_API_KEY": "sk-secret"})
        safe = config.to_client_safe_dict()
        dumped = json.dumps(safe)
        assert "sk-secret" not in dumped
        assert safe["has_llm_key"] is True

    def test_skill_key_management(self, config):
        config.save_credentials({"skill_keys": {"web_search": "tavily-key-123"}})
        assert config.get_skill_key("web_search") == "tavily-key-123"
        assert config.get_skill_key("nonexistent") is None


# ─── Protocol Roundtrip ───

class TestProtocolRoundtrip:
    """Test that messages survive serialize → parse cycle."""

    def test_text_command_roundtrip(self):
        msg = FeralMessage(
            hop="client", type="text_command",
            payload=TextCommandPayload(text="Hello FERAL").model_dump(),
        )
        raw = msg.model_dump()
        parsed_msg, parsed_payload = parse_message(raw)
        assert parsed_msg.type == "text_command"
        assert isinstance(parsed_payload, TextCommandPayload)
        assert parsed_payload.text == "Hello FERAL"

    def test_node_register_roundtrip(self):
        msg = FeralMessage(
            hop="daemon", type="node_register",
            payload=NodeRegisterPayload(
                node_id="daemon_robot-01",
                node_type="robot",
                capabilities=["telemetry", "robot_move", "robot_grip"],
            ).model_dump(),
        )
        raw = msg.model_dump()
        parsed_msg, parsed_payload = parse_message(raw)
        assert parsed_payload.node_id == "daemon_robot-01"
        assert parsed_payload.node_type == "robot"
        assert "robot_move" in parsed_payload.capabilities

    def test_actuator_node_type_valid(self):
        payload = NodeRegisterPayload(
            node_id="daemon_actuator-01",
            node_type="actuator",
            capabilities=["robot_move"],
        )
        assert payload.node_type == "actuator"

    def test_glasses_node_type_valid(self):
        payload = NodeRegisterPayload(
            node_id="daemon_w300",
            node_type="glasses",
            capabilities=["telemetry", "imu", "capture_frame"],
        )
        assert payload.node_type == "glasses"

    def test_skills_field_optional_default_empty(self):
        # Old clients that don't send `skills` still register cleanly.
        # Phase 4 wire-compat regression: NodeRegisterPayload.skills
        # must default to `[]` so v2026.5.x companion builds keep
        # working after the brain ships the new field.
        payload = NodeRegisterPayload(
            node_id="daemon_legacy",
            node_type="phone",
            capabilities=["accel"],
        )
        assert payload.skills == []

    def test_skills_field_accepts_phase4_manifests(self):
        # Phase 4 iOS companion (CallKitSkill, etc.) publishes
        # structured skill manifests in node_register.skills.
        # Validation is intentionally loose at the protocol layer —
        # Phase 5's capability registry tightens the shape.
        payload = NodeRegisterPayload(
            node_id="daemon_iphone",
            node_type="phone",
            platform="ios",
            capabilities=["phone_call"],
            skills=[
                {
                    "id": "phone_call",
                    "name": "Phone Call",
                    "description": "Place a call via iOS Phone / FaceTime.",
                    "actions": [
                        {
                            "name": "phone.call.start",
                            "summary": "Start a call.",
                            "requiresPermission": None,
                        }
                    ],
                }
            ],
        )
        assert len(payload.skills) == 1
        assert payload.skills[0]["actions"][0]["name"] == "phone.call.start"


# ─── Skill Registry + Executor ───

class TestSkillPipeline:
    """Test skill discovery, routing, and execution."""

    def test_builtin_weather_loaded(self, skill_registry):
        assert "weather_current" in skill_registry.skills
        skill = skill_registry.skills["weather_current"]
        assert len(skill.endpoints) >= 1

    def test_manifests_loaded_from_json(self, skill_registry):
        assert len(skill_registry.skills) > 1

    def test_robot_action_manifest_loads(self, skill_registry):
        """robot_action.json should now load with WS_EXECUTE method."""
        assert "robot_ext" in skill_registry.skills
        skill = skill_registry.skills["robot_ext"]
        assert skill.requires_daemon is True
        move_ep = next((e for e in skill.endpoints if e.id == "robot_move"), None)
        assert move_ep is not None
        assert move_ep.method == "WS_EXECUTE"

    def test_skill_routing_finds_weather(self, skill_registry):
        matches = skill_registry.find_skills_for_query("what's the weather in New York")
        assert len(matches) > 0
        assert any(s.skill_id == "weather_current" for s in matches)

    def test_skill_routing_finds_robot(self, skill_registry):
        matches = skill_registry.find_skills_for_query("move the robot forward")
        assert len(matches) > 0
        assert any(s.skill_id == "robot_ext" for s in matches)

    @pytest.mark.asyncio
    async def test_ws_execute_no_daemon_returns_error(self):
        executor = SkillExecutor(daemons={})
        skill = SkillManifest(
            skill_id="robot_ext", requires_daemon=True, daemon_node_type="robot",
            brand=BrandProfile(name="Robot"),
            description="Robot control",
            endpoints=[SkillEndpoint(id="robot_move", method="WS_EXECUTE", url="local_daemon", description="Move robot")],
        )
        result = await executor.execute("robot_ext__robot_move", {"direction": "forward"}, skill, skill.endpoints[0])
        assert result["success"] is False
        assert "No connected daemon" in result["error"]

    @pytest.mark.asyncio
    async def test_ws_execute_with_mock_daemon(self):
        mock_ws = AsyncMock()

        async def fake_send_json(data):
            pass

        mock_ws.send_json = fake_send_json
        daemons = {"daemon_robot-01": mock_ws}
        executor = SkillExecutor(daemons=daemons)

        skill = SkillManifest(
            skill_id="robot_ext", requires_daemon=True, daemon_node_type="robot",
            brand=BrandProfile(name="Robot"),
            description="Robot control",
            endpoints=[SkillEndpoint(id="robot_move", method="WS_EXECUTE", url="local_daemon", description="Move")],
        )

        async def resolve_soon():
            await asyncio.sleep(0.1)
            for req_id, future in list(executor._pending_results.items()):
                future.set_result({"status": "success", "stdout": "Moved forward"})

        task = asyncio.create_task(resolve_soon())

        result = await executor.execute("robot_ext__robot_move", {"direction": "forward"}, skill, skill.endpoints[0])
        assert result["success"] is True
        assert "Moved forward" in str(result["data"])
        await task


# ─── Memory Integration ───

class TestMemoryIntegration:
    """Test the full memory lifecycle: write → read → context."""

    async def test_full_session_lifecycle(self, memory):
        sid = "session-001"

        memory.working_push(sid, {"role": "user", "content": "What's the weather?"})
        memory.working_push(sid, {"role": "assistant", "content": "It's sunny and 75°F"})
        await memory.save("Weather query for NYC", tags=["weather"])
        await memory.episode_save(sid, "user", "Weather query about NYC")
        await memory.knowledge_store("user", "lives_in", "NYC")
        await memory.log_execution(
            sid, "weather_current", "current",
            {"lat": 40.7, "lon": -74.0}, "success", "Sunny 75F", 0.5,
        )

        ctx = await memory.build_context_for_llm(sid, "what temperature is it?")
        assert ctx

        stats = await memory.stats()
        assert stats["notes"] >= 1
        assert stats["episodes"] >= 1
        assert stats["knowledge_triples"] >= 1
        assert stats["execution_logs"] >= 1


# ─── Perception Integration ───

class TestPerceptionIntegration:
    """Test multimodal perception from multiple hardware sources."""

    def test_glasses_telemetry_flow(self, perception):
        sid = "session-glasses"
        perception.update_sensors(sid, {
            "vitals": {"ppg_heart_rate": 72, "spo2_pct": 98},
            "head_pose": {"pitch": 5.0, "yaw": -3.0, "roll": 0.2},
            "environment": {"ambient_light_lux": 450},
        })

        frame = perception.get_frame(sid)
        assert frame.heart_rate == 72
        assert frame.spo2_pct == 98
        assert frame.ambient_light_lux == 450

    def test_robot_telemetry_flow(self, perception):
        sid = "session-robot"
        perception.update_sensors(sid, {
            "battery_pct": 85.5,
            "joint_temperatures": [42.1, 41.5, 39.8],
            "status": "idle",
        })

        frame = perception.get_frame(sid)
        assert frame.battery_pct == 85.5

    def test_multi_node_perception(self, perception):
        sid = "session-multi"
        perception.update_connected_nodes(sid, ["daemon_w300", "daemon_robot-01"])
        perception.update_sensors(sid, {"vitals": {"ppg_heart_rate": 80}})
        perception.update_gesture(sid, "nod")

        frame = perception.get_frame(sid)
        assert len(frame.connected_nodes) == 2
        assert frame.heart_rate == 80
        assert frame.gesture == "nod"

    def test_vision_frame_update(self, perception):
        sid = "session-vision"
        mock_buffer = MagicMock()
        mock_buffer.latest_data_url.return_value = "data:image/jpeg;base64,abc123"
        perception.update_vision(sid, mock_buffer, "daemon_w300")

        frame = perception.get_frame(sid)
        assert frame.vision_data_url == "data:image/jpeg;base64,abc123"


# ─── Full Pipeline Simulation ───

class TestFullPipeline:
    """Simulate the full flow: config → registry → memory → perception → execute."""

    async def test_hardware_triangle_scenario(self, config, skill_registry, memory, perception):
        """
        The FERAL Triangle: Phone (client) + Glasses (daemon) + Robot (daemon)
        """
        session_id = "session-demo"

        config.save_credentials({"OPENAI_API_KEY": "sk-test"})
        (config.user_home / "USER.md").write_text("My name is Test User. I work on AI projects and robotics.")
        config.discover()
        assert config.setup_complete

        assert "weather_current" in skill_registry.skills
        assert "robot_ext" in skill_registry.skills

        perception.update_connected_nodes(session_id, ["daemon_w300", "daemon_robot-01"])
        perception.update_sensors(session_id, {
            "vitals": {"ppg_heart_rate": 72, "spo2": 98},
            "head_pose": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
        })
        perception.update_gesture(session_id, "nod")

        memory.working_push(session_id, {"role": "user", "content": "Move the robot forward"})
        await memory.log_execution(session_id, "robot_ext", "robot_move", {"direction": "forward"}, "success", "Moved forward", 0.3)

        frame = perception.get_frame(session_id)
        assert len(frame.connected_nodes) == 2
        assert frame.heart_rate == 72
        assert frame.gesture == "nod"

        ctx = await memory.build_context_for_llm(session_id, "move robot")
        assert ctx  # Non-empty context

        stats = await memory.stats()
        assert stats["execution_logs"] >= 1
