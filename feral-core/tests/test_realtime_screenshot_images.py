"""Actual realtime callbacks carry owned screenshots on an inert GA wire."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from voice.realtime_proxy import RealtimeProxy, RealtimeSession, _realtime_screenshot_policy
from voice.tool_result_envelope import (
    MAX_REALTIME_IMAGE_CHARS, MAX_REALTIME_RESULT_CHARS, RealtimeToolResult,
    prepare_realtime_screenshot_result,
)


def screenshot(*, success=True, encoded=None, encoding="png"):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(buffer, format="PNG")
    return {"success": success, "status_code": 200 if success else 503,
            "data": {"image_b64": encoded if encoded is not None else base64.b64encode(buffer.getvalue()).decode(),
                     "encoding": encoding}, "error": None if success else "Capture unavailable"}


class Wire:
    def __init__(self, after=None):
        self.frames, self.after = [], after

    async def send(self, raw):
        frame = json.loads(raw)
        self.frames.append(frame)
        if self.after:
            await self.after(frame)


def bound(result, *, after=None, model="gpt-realtime", name="screen_capture__capture"):
    owner = RealtimeSession("image-result-sid", "fixture-node", api_key="fixture", model=model)
    owner._connected, owner._ws = True, Wire(after)
    proxy = RealtimeProxy.__new__(RealtimeProxy)
    proxy._memory = proxy._orchestrator = None
    proxy._sessions = {owner.session_id: owner}
    skill_id, endpoint_id = name.split("__")
    proxy._skill_registry = SimpleNamespace(skills={skill_id: SimpleNamespace(
        endpoints=[SimpleNamespace(id=endpoint_id)],
    )})
    proxy._skill_executor = SimpleNamespace(execute=AsyncMock(return_value=result))
    proxy._send_tool_feedback = proxy._record_voice_tool_episode = AsyncMock()
    owner._callback_guard = lambda: proxy._sessions.get(owner.session_id) is owner
    owner._on_tool_call = proxy._bind_callback(owner, proxy._handle_tool_call, response_scoped=True)
    owner._active_response_id, owner._response_in_progress = "fixture-response", True
    return owner, proxy


async def invoke(owner, name="screen_capture__capture"):
    await owner._handle_event({"type": "response.function_call_arguments.done",
        "response_id": "fixture-response", "call_id": "fixture-capture",
        "name": name, "arguments": "{}"})


@pytest.fixture(autouse=True)
def explicit_image_policy(monkeypatch):
    monkeypatch.setenv("FERAL_VLM_PROVIDER", "openai")
    monkeypatch.setenv("FERAL_VLM_BASE_URL", "https://api.openai.com/v1")


async def test_bound_capture_delivers_image_between_receipt_and_response():
    result = screenshot()
    owner, proxy = bound(result)
    await invoke(owner)
    frames = owner._ws.frames
    assert len(frames) == 3
    receipt = json.loads(frames[0]["item"]["output"])
    assert receipt["success"] is True and receipt["status_code"] == 200
    assert len(frames[0]["item"]["output"]) < MAX_REALTIME_RESULT_CHARS
    assert result["data"]["image_b64"] not in frames[0]["item"]["output"]
    assert frames[1]["item"]["role"] == "user"
    image = frames[1]["item"]["content"][1]
    assert image == {"type": "input_image", "image_url": "data:image/png;base64," + result["data"]["image_b64"]}
    assert "untrusted" in frames[1]["item"]["content"][0]["text"]
    assert frames[2] == {"type": "response.create"}
    proxy._skill_executor.execute.assert_awaited_once()
    assert not owner._callback_tasks


async def test_actual_browser_bridge_and_manifest_deliver_pixels(monkeypatch):
    from api.state import BrainState
    from models.skill_manifest import SkillManifest
    bridge = {}
    monkeypatch.setattr("skills.impl.register_instance", lambda skill_id, instance: bridge.update({skill_id: instance}))
    monkeypatch.setattr("skills.impl.get_implementation", lambda _: None)
    state = BrainState.__new__(BrainState)
    raw = {"success": True, "image_b64": screenshot()["data"]["image_b64"], "format": "png"}
    state._execute_browser_action = AsyncMock(return_value=raw)
    state._bind_browser_bridge("browser")
    result = await bridge["browser"].execute("screenshot", {}, {})
    owner, proxy = bound(result, name="browser__screenshot")
    manifest = SkillManifest.model_validate_json((Path(__file__).parents[1] / "skills/manifests/browser_use.json").read_text())
    assert manifest.skill_id == "browser"
    proxy._skill_registry.skills[manifest.skill_id] = manifest
    await invoke(owner, "browser__screenshot")
    assert owner._ws.frames[1]["item"]["content"][1]["image_url"].endswith(raw["image_b64"])


async def test_actual_gui_capture_jpeg_shape_reaches_realtime(monkeypatch):
    from skills.impl.gui_computer_use import GUIComputerUseSkill
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(buffer, format="JPEG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    skill = GUIComputerUseSkill()
    skill._scale = skill._dpi_scale = 1.0
    monkeypatch.setattr(skill, "_sync_screenshot", lambda: encoded)
    result = await skill._screenshot({})
    owner, _ = bound(result, name="gui_computer_use__screenshot")
    await invoke(owner, "gui_computer_use__screenshot")
    assert owner._ws.frames[1]["item"]["content"][1] == {
        "type": "input_image", "image_url": "data:image/jpeg;base64," + encoded}


async def test_actual_desktop_screenshot_path_receipt_does_not_read_or_upload_pixels(monkeypatch):
    from skills.impl.desktop_control import DesktopControlSkill
    skill = DesktopControlSkill()
    daemon = AsyncMock(return_value={"success": True, "status_code": 200,
        "data": {"output": "", "exit_code": 0}, "error": None})
    monkeypatch.setattr(skill, "_run_daemon", daemon)
    result = await skill.execute("screenshot", {"command": "screencapture -x /tmp/inert.png"}, {})
    owner, _ = bound(result, name="desktop_control__screenshot")
    await invoke(owner, "desktop_control__screenshot")
    assert len(owner._ws.frames) == 2
    receipt = json.loads(owner._ws.frames[0]["item"]["output"])
    assert receipt["success"] is True and "NOT seen" in receipt["_image_delivery"]
    assert "not pixels" in receipt["_image_delivery"]
    daemon.assert_awaited_once()


@pytest.mark.parametrize("provider,endpoint", [("ollama", ""), ("", ""),
    ("anthropic", ""), ("openai", "https://separate.invalid/v1")])
async def test_separate_or_local_vision_choice_does_not_upload(monkeypatch, provider, endpoint):
    monkeypatch.setenv("FERAL_VLM_PROVIDER", provider)
    monkeypatch.setenv("FERAL_VLM_BASE_URL", endpoint)
    owner, _ = bound(screenshot())
    await invoke(owner)
    assert len(owner._ws.frames) == 2
    receipt = json.loads(owner._ws.frames[0]["item"]["output"])
    assert receipt["success"] is True and receipt["_image_omitted"] is True
    assert "NOT seen" in receipt["_image_delivery"]


@pytest.mark.parametrize("model", ["gpt-realtime", "gpt-realtime-mini", "gpt-realtime-2025-08-28"])
def test_supported_realtime_models_have_image_transport(model):
    assert _realtime_screenshot_policy(model)


@pytest.mark.parametrize("model", ["gpt-4o-realtime-preview", "unknown-model", "gpt-realtime-unknown"])
async def test_unsupported_model_is_truthfully_unseen(model):
    owner, _ = bound(screenshot(), model=model)
    await invoke(owner)
    assert len(owner._ws.frames) == 2
    assert "NOT seen" in json.loads(owner._ws.frames[0]["item"]["output"])["_image_delivery"]


@pytest.mark.parametrize("result", [screenshot(success=False), screenshot(encoded="invalid"),
    screenshot(encoded="A" * (MAX_REALTIME_IMAGE_CHARS + 1)),
    screenshot(encoded="https://private.invalid/image.png"), screenshot(encoding="webp")])
async def test_failed_invalid_remote_oversized_or_unsupported_image_never_uploads(result):
    owner, _ = bound(result)
    await invoke(owner)
    assert len(owner._ws.frames) == 2
    receipt = json.loads(owner._ws.frames[0]["item"]["output"])
    assert receipt["success"] is result["success"]
    assert "NOT seen" in receipt["_image_delivery"]


async def test_cancel_after_receipt_fences_image_and_continuation():
    async def after(frame):
        if frame.get("item", {}).get("type") == "function_call_output":
            await owner.cancel_response()
    owner, _ = bound(screenshot(), after=after)
    await invoke(owner)
    assert [frame["type"] for frame in owner._ws.frames] == ["conversation.item.create", "response.cancel"]


async def test_replaced_owner_after_receipt_fences_image_and_continuation():
    async def after(frame):
        if frame.get("item", {}).get("type") == "function_call_output":
            proxy._sessions[owner.session_id] = object()
    owner, proxy = bound(screenshot(), after=after)
    await invoke(owner)
    assert len(owner._ws.frames) == 1


async def test_cancel_after_image_does_not_continue_response():
    async def after(frame):
        if any(part.get("type") == "input_image" for part in frame.get("item", {}).get("content", [])):
            await owner.cancel_response()
    owner, _ = bound(screenshot(), after=after)
    await invoke(owner)
    assert [frame["type"] for frame in owner._ws.frames] == ["conversation.item.create", "conversation.item.create", "response.cancel"]


async def test_policy_change_after_receipt_sends_unseen_notice(monkeypatch):
    async def after(frame):
        if frame.get("item", {}).get("type") == "function_call_output":
            monkeypatch.setenv("FERAL_VLM_PROVIDER", "ollama")
    owner, _ = bound(screenshot(), after=after)
    await invoke(owner)
    assert len(owner._ws.frames) == 3
    assert "NOT seen" in owner._ws.frames[1]["item"]["content"][0]["text"]
    assert "input_image" not in json.dumps(owner._ws.frames)


async def test_policy_changed_before_send_preserves_capture_receipt(monkeypatch):
    prepared = prepare_realtime_screenshot_result("screen_capture__capture", screenshot(), allow_images=True)
    assert isinstance(prepared, RealtimeToolResult)
    owner, _ = bound(screenshot())
    monkeypatch.setenv("FERAL_VLM_PROVIDER", "ollama")
    await owner.send_tool_result("fixture-capture", prepared, continue_response=False)
    assert len(owner._ws.frames) == 1
    receipt = json.loads(owner._ws.frames[0]["item"]["output"])
    assert receipt["success"] is True and "NOT seen" in receipt["_image_delivery"]


async def test_queued_result_delivers_image_without_independent_response():
    prepared = prepare_realtime_screenshot_result("screen_capture__capture", screenshot(), allow_images=True)
    owner, _ = bound(screenshot())
    await owner.send_tool_result("fixture-capture", prepared, continue_response=False)
    assert len(owner._ws.frames) == 2
    assert owner._ws.frames[1]["item"]["content"][1]["type"] == "input_image"


def test_image_lift_preserves_redaction_and_does_not_mutate_result():
    result = screenshot()
    result["data"]["api_key"] = "sk-" + "private" * 8
    prepared = prepare_realtime_screenshot_result("screen_capture__capture", result, allow_images=True)
    assert isinstance(prepared, RealtimeToolResult)
    assert result["data"]["api_key"] not in prepared
    assert result["data"]["image_b64"] not in prepared
    assert result["data"]["image_b64"] in prepared.image_url


async def test_session_guidance_routes_supported_tools_and_requires_readback():
    owner, _ = bound(screenshot())
    await owner.configure(system_prompt="inert")
    instructions = owner._ws.frames[0]["session"]["instructions"]
    assert "browser DOM tools" in instructions and "Accessibility" in instructions
    assert "fresh readback" in instructions and "_image_delivery" in instructions
