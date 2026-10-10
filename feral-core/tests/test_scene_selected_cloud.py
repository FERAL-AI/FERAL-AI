"""Explicit scene vision stays on its selected shared request adapter."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from perception.scene import SceneAnalyzer


@pytest.fixture
def selected(monkeypatch):
    monkeypatch.setenv("FERAL_VLM_PROVIDER", "openai")
    monkeypatch.setenv("FERAL_VLM_MODEL", "gpt-4o")
    monkeypatch.setenv("FERAL_VLM_BASE_URL", "https://selected.invalid/v1")
    monkeypatch.setenv("FERAL_VLM_API_KEY", "inert-selected-key")
    owner = SimpleNamespace(provider="anthropic", model="claude-sonnet-4-6",
                            available=False, chat=AsyncMock(),
                            chat_selected_vision=AsyncMock(return_value={"choices": []}),
                            extract_response=Mock(return_value=('{"scene_description":"fixture"}', [])))
    return SceneAnalyzer(owner), owner


async def test_explicit_cloud_uses_exact_selection_even_when_primary_unavailable(selected):
    scene, owner = selected
    assert scene.configured
    result = await scene.analyze_frame("aW5lcnQ=", force=True)
    assert result["scene_description"] == "fixture"
    owner.chat.assert_not_awaited()
    args = owner.chat_selected_vision.await_args.kwargs
    assert (args["provider"], args["model"], args["base_url"], args["api_key"]) == (
        "openai", "gpt-4o", "https://selected.invalid/v1", "inert-selected-key")
    assert args["messages"][0]["content"][1]["image_url"]["url"].endswith("aW5lcnQ=")
    assert owner.provider == "anthropic" and owner.model == "claude-sonnet-4-6"


async def test_selected_error_does_not_fall_back_or_claim_analysis(selected):
    scene, owner = selected
    owner.chat_selected_vision.return_value = {"error": "unavailable", "error_code": "vision_request_unavailable"}
    assert await scene.analyze_frame("aW5lcnQ=", force=True) is None
    owner.chat.assert_not_awaited()
    owner.extract_response.assert_not_called()


async def test_unknown_explicit_provider_never_uses_primary(selected):
    scene, owner = selected
    scene._vlm_provider = "unknown-provider"
    assert await scene.analyze_frame("aW5lcnQ=", force=True) is None
    owner.chat.assert_not_awaited()
    owner.chat_selected_vision.assert_not_awaited()


async def test_capture_empty_analysis_is_explicit_and_reuses_runtime(monkeypatch, tmp_path):
    from PIL import Image
    from api.state import state
    from skills.impl.screen_capture import ScreenCaptureSkill

    scene = SimpleNamespace(configured=True, provider_health={},
                            analyze_frame=AsyncMock(return_value=None))
    monkeypatch.setattr(state, "scene", scene)
    skill = ScreenCaptureSkill()
    path = tmp_path / "capture.png"

    async def capture(save_path, region=""):
        Image.new("RGB", (2, 2), "white").save(save_path)
        return {"success": True}

    monkeypatch.setattr(skill, "_capture_screen", capture)
    result = await skill._capture({"path": str(path), "analyze": True})
    assert result["success"] is True  # The capture itself succeeded.
    assert result["data"]["analysis"] is None
    assert "has not been interpreted" in result["data"]["analysis_error"]
    assert skill._llm is None and skill._scene is None
    scene.analyze_frame.assert_awaited_once()
