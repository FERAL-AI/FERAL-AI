"""Unit tests for perception_query skill + best-camera picker."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skills.impl.perception_query import (
    CAMERA_CAPABILITIES,
    PerceptionQuerySkill,
    pick_best_camera,
)


class FakeWS:
    def __init__(self, capabilities):
        self._feral_capabilities = list(capabilities)


class FakeVisionBuffer:
    def __init__(self, latest_map=None, node_ids=None):
        self._latest_map = latest_map or {}
        self._node_ids = list(node_ids or [])

    def latest(self, node_id):
        return self._latest_map.get(node_id)

    def node_ids_with_frames(self):
        return list(self._node_ids)


class TestBestCameraPicker:
    def test_empty_daemons_returns_none(self):
        assert pick_best_camera({}, vision_buffer=None) is None

    def test_prefers_iphone_over_w610(self):
        daemons = {
            "w610-001": FakeWS(["camera", "w610_camera"]),
            "phone-abc": FakeWS(["iphone_camera", "iphone_microphone"]),
        }
        assert pick_best_camera(daemons) == "phone-abc"

    def test_prefers_browser_over_w610_when_no_iphone(self):
        daemons = {
            "w610-001": FakeWS(["w610_camera", "camera"]),
            "browser-xyz": FakeWS(["browser_camera", "camera"]),
        }
        assert pick_best_camera(daemons) == "browser-xyz"

    def test_tiebreaker_most_recent_frame(self):
        daemons = {
            "phone-a": FakeWS(["iphone_camera"]),
            "phone-b": FakeWS(["iphone_camera"]),
        }
        vb = FakeVisionBuffer(latest_map={
            "phone-a": {"timestamp": 1000.0},
            "phone-b": {"timestamp": 9999.0},
        })
        assert pick_best_camera(daemons, vision_buffer=vb) == "phone-b"

    def test_tiebreaker_missing_frame_falls_back_to_0(self):
        daemons = {
            "phone-a": FakeWS(["iphone_camera"]),
            "phone-b": FakeWS(["iphone_camera"]),
        }
        vb = FakeVisionBuffer(latest_map={"phone-a": {"timestamp": 500.0}})
        assert pick_best_camera(daemons, vision_buffer=vb) == "phone-a"

    def test_fallback_to_any_node_with_frame(self):
        daemons = {"legacy-1": FakeWS([])}
        vb = FakeVisionBuffer(
            latest_map={"legacy-1": {"timestamp": 1.0}},
            node_ids=["legacy-1"],
        )
        assert pick_best_camera(daemons, vision_buffer=vb) == "legacy-1"

    def test_no_capability_no_frame_returns_none(self):
        daemons = {"other": FakeWS(["heart_rate"])}
        assert pick_best_camera(daemons, vision_buffer=None) is None

    def test_capability_priority_order_is_stable(self):
        # The priority tuple must stay in the published order — changing it
        # rewires the orchestrator's best-camera semantics silently.
        assert CAMERA_CAPABILITIES == (
            "iphone_camera",
            "browser_camera",
            "w610_camera",
            "camera",
        )


@pytest.fixture
def skill():
    return PerceptionQuerySkill()


class TestPerceptionQuerySkillExecution:
    @pytest.mark.asyncio
    async def test_unknown_endpoint_returns_404(self, skill):
        out = await skill.execute("does_not_exist", {}, {})
        assert out["success"] is False
        assert out["status_code"] == 404

    @pytest.mark.asyncio
    async def test_no_camera_connected_returns_404(self, skill):
        mock_state = MagicMock()
        mock_state.orchestrator = MagicMock()
        mock_state.daemons = {}
        mock_state.vision_buffer = FakeVisionBuffer()
        mock_state.scene = None
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {}, {})
        assert out["success"] is False
        assert out["status_code"] == 404
        assert "No camera" in out["error"]

    @pytest.mark.asyncio
    async def test_happy_path_uses_best_camera_and_scene(self, skill):
        frame = {
            "data_b64": "BASE64PAYLOAD",
            "encoding": "jpeg",
            "resolution": [640, 480],
            "frame_id": "f1",
            "timestamp": 1234.5,
        }
        orchestrator = MagicMock()
        orchestrator.request_frame = AsyncMock(return_value=frame)
        scene = SimpleNamespace(
            available=True,
            analyze_frame=AsyncMock(return_value={
                "scene_description": "A person standing in a kitchen",
                "answer": "A person standing in a kitchen",
            }),
        )
        mock_state = MagicMock()
        mock_state.orchestrator = orchestrator
        mock_state.daemons = {"phone-a": FakeWS(["iphone_camera"])}
        mock_state.vision_buffer = FakeVisionBuffer()
        mock_state.scene = scene
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {"reason": "grocery"}, {})
        assert out["success"] is True
        assert out["data"]["node_id"] == "phone-a"
        assert out["data"]["scene_description"] == "A person standing in a kitchen"
        assert out["data"]["data_b64"] == "BASE64PAYLOAD"
        assert out["data"]["autonomy_tier"] == "user_confirm"
        orchestrator.request_frame.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_explicit_node_id_passthrough(self, skill):
        frame = {"data_b64": "x", "encoding": "jpeg", "resolution": [320, 240], "frame_id": "f"}
        orchestrator = MagicMock()
        orchestrator.request_frame = AsyncMock(return_value=frame)
        mock_state = MagicMock()
        mock_state.orchestrator = orchestrator
        mock_state.daemons = {"phone-a": FakeWS(["iphone_camera"]), "w610-1": FakeWS(["w610_camera"])}
        mock_state.vision_buffer = FakeVisionBuffer()
        mock_state.scene = None
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {"node_id": "w610-1"}, {})
        assert out["success"] is True
        assert out["data"]["node_id"] == "w610-1"

    @pytest.mark.asyncio
    async def test_explicit_node_id_unknown_returns_404(self, skill):
        mock_state = MagicMock()
        mock_state.orchestrator = MagicMock()
        mock_state.daemons = {"phone-a": FakeWS(["iphone_camera"])}
        mock_state.vision_buffer = FakeVisionBuffer()
        mock_state.scene = None
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {"node_id": "not-connected"}, {})
        assert out["success"] is False
        assert out["status_code"] == 404

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_latest_frame(self, skill):
        cached = {"data_b64": "CACHED", "encoding": "jpeg", "resolution": [640, 480]}
        orchestrator = MagicMock()
        orchestrator.request_frame = AsyncMock(return_value=None)
        mock_state = MagicMock()
        mock_state.orchestrator = orchestrator
        mock_state.daemons = {"phone-a": FakeWS(["iphone_camera"])}
        mock_state.vision_buffer = FakeVisionBuffer(latest_map={"phone-a": cached})
        mock_state.scene = None
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {}, {})
        assert out["success"] is True
        assert out["data"]["data_b64"] == "CACHED"

    @pytest.mark.asyncio
    async def test_timeout_no_cache_returns_504(self, skill):
        orchestrator = MagicMock()
        orchestrator.request_frame = AsyncMock(return_value=None)
        mock_state = MagicMock()
        mock_state.orchestrator = orchestrator
        mock_state.daemons = {"phone-a": FakeWS(["iphone_camera"])}
        mock_state.vision_buffer = FakeVisionBuffer()
        mock_state.scene = None
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {}, {})
        assert out["success"] is False
        assert out["status_code"] == 504

    @pytest.mark.asyncio
    async def test_orchestrator_missing_returns_503(self, skill):
        mock_state = MagicMock()
        mock_state.orchestrator = None
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {}, {})
        assert out["success"] is False
        assert out["status_code"] == 503

    @pytest.mark.asyncio
    async def test_scene_unavailable_returns_empty_description(self, skill):
        frame = {"data_b64": "x", "encoding": "jpeg", "resolution": [640, 480], "frame_id": "f"}
        orchestrator = MagicMock()
        orchestrator.request_frame = AsyncMock(return_value=frame)
        mock_state = MagicMock()
        mock_state.orchestrator = orchestrator
        mock_state.daemons = {"phone-a": FakeWS(["iphone_camera"])}
        mock_state.vision_buffer = FakeVisionBuffer()
        mock_state.scene = SimpleNamespace(available=False, analyze_frame=AsyncMock())
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {}, {})
        assert out["success"] is True
        assert out["data"]["scene_description"] == ""

    @pytest.mark.asyncio
    async def test_scene_failure_does_not_break_skill(self, skill):
        frame = {"data_b64": "x", "encoding": "jpeg", "resolution": [640, 480], "frame_id": "f"}
        orchestrator = MagicMock()
        orchestrator.request_frame = AsyncMock(return_value=frame)
        scene = SimpleNamespace(
            available=True,
            analyze_frame=AsyncMock(side_effect=RuntimeError("VLM down")),
        )
        mock_state = MagicMock()
        mock_state.orchestrator = orchestrator
        mock_state.daemons = {"phone-a": FakeWS(["iphone_camera"])}
        mock_state.vision_buffer = FakeVisionBuffer()
        mock_state.scene = scene
        with patch("api.state.state", mock_state):
            out = await skill.execute("what_do_i_see", {}, {})
        assert out["success"] is True
        assert out["data"]["scene_description"] == ""


class TestManifestContract:
    def test_manifest_matches_skill_id(self):
        import json
        from pathlib import Path

        manifest_path = (
            Path(__file__).resolve().parent.parent
            / "skills" / "manifests" / "perception_query.json"
        )
        manifest = json.loads(manifest_path.read_text())
        assert manifest["skill_id"] == "perception_query"
        assert any(ep["id"] == "what_do_i_see" for ep in manifest["endpoints"])
        # autonomy_tier intent rides the categories + permissions arrays.
        # `permissions` is now a closed vocabulary (models.skill_manifest
        # .SkillPermission) because it is rendered in the install consent
        # dialog, so the free-text "autonomy:user_confirm" spelling is
        # carried by `categories` and the capability by `autonomy`.
        assert "autonomy:user_confirm" in manifest["categories"]
        assert "autonomy" in manifest["permissions"]
