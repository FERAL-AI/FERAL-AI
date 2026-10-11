"""Actual scene selection + SQLite reservations; provider transport is inert."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from cost.budget import CostBudget
from perception.scene import SceneAnalyzer
from tests.test_cost_atomic_reservations import provider


@pytest.fixture
def scene_rig(monkeypatch, tmp_path):
    for key, value in {
        "FERAL_VLM_PROVIDER": "openai", "FERAL_VLM_MODEL": "gpt-5.4",
        "FERAL_VLM_BASE_URL": "https://api.openai.com/v1", "FERAL_VLM_API_KEY": "inert-selected-key",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("agents.llm_provider._resolve_api_key", lambda _: "")
    budget = CostBudget(db_path=tmp_path / "cost.db", settings={"cost": {"global_per_hour_usd": 1}})
    calls, clients = [], []
    controls = {"status": 200, "entered": None, "release": None}

    async def wire(request):
        calls.append(request)
        if controls["entered"]:
            controls["entered"].set()
            await controls["release"].wait()
        if controls["status"] != 200:
            return httpx.Response(controls["status"], json={"error": {"message": "private-provider-canary"}})
        return httpx.Response(200, json={"id": "resp-inert", "model": "gpt-5.4", "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text",
                "text": '{"answer":"HARBOR-27","confidence":1,"detected_objects":[]}'}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}})
    owner = provider(budget, lambda _: (_ for _ in ()).throw(AssertionError("primary used")),
        model="claude-sonnet-4-6", name="anthropic")
    owner.available = False
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wire)
        result = original(*args, **kwargs)
        clients.append(result)
        return result
    monkeypatch.setattr(httpx, "AsyncClient", client)
    buffer = io.BytesIO()
    Image.new("RGB", (1680, 60), "white").save(buffer, format="PNG")
    return SimpleNamespace(budget=budget, owner=owner, scene=SceneAnalyzer(owner), calls=calls,
        clients=clients, image=base64.b64encode(buffer.getvalue()).decode(), controls=controls)


async def close(r):
    await r.owner.client.aclose()
    for client in r.clients:
        await client.aclose()
    await r.budget.close()


async def test_scene_selected_gpt54_reserves_and_settles_real_cost(scene_rig):
    r = scene_rig
    try:
        result = await r.scene.analyze_frame(r.image, mode="query", query="Read the synthetic card", force=True)
        assert result and result["answer"] == "HARBOR-27"
        assert len(r.calls) == 1 and r.calls[0].url.host == "api.openai.com"
        body = json.loads(r.calls[0].content)
        assert body["model"] == "gpt-5.4"
        image = next(part for item in body["input"] for part in item.get("content", []) if part["type"] == "input_image")
        assert image["image_url"].endswith(r.image) and image["detail"] == "low"
        rows = await r.budget.get_reservations()
        assert len(rows) == 1 and rows[0]["call_site"] == "vision" and rows[0]["status"] == "settled"
        assert rows[0]["pricing_basis"] == "catalog_estimate" and rows[0]["prompt_tokens"] >= 7374
        assert rows[0]["actual_dollars"] == pytest.approx(.00055)
        assert r.owner.provider == "anthropic" and r.owner.model == "claude-sonnet-4-6"
    finally:
        await close(r)


async def test_scene_cap_still_refuses_before_http(scene_rig):
    r = scene_rig
    r.budget.reload_from_settings({"cost": {"global_per_hour_usd": .00001}})
    try:
        assert await r.scene.analyze_frame(r.image, force=True) is None
        assert r.calls == [] and await r.budget.get_reservations() == []
    finally:
        await close(r)


@pytest.mark.parametrize("selection", ["gpt-5.4-pro", "gpt-4o", "gpt-5.4-future"])
async def test_other_vision_model_remains_fail_closed(scene_rig, selection):
    r = scene_rig
    r.scene._vlm_model = selection
    try:
        assert await r.scene.analyze_frame(r.image, force=True) is None
        assert r.calls == []
    finally:
        await close(r)


async def test_dispatched_failure_does_not_fallback_or_release_as_free(scene_rig, caplog):
    r = scene_rig
    r.controls["status"] = 400
    try:
        assert await r.scene.analyze_frame(r.image, force=True) is None
        assert len(r.calls) == 1
        rows = await r.budget.get_reservations()
        assert rows[0]["status"] == "outcome_unknown"
        assert rows[0]["estimated_dollars"] > 0
        assert "private-provider-canary" not in caplog.text
    finally:
        await close(r)


async def test_concurrent_scene_attempts_share_image_hold_and_preserve_cap(scene_rig):
    r = scene_rig
    r.controls["entered"], r.controls["release"] = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(r.scene.analyze_frame(r.image, node_id="first", mode="query", query="Synthetic card", force=True))
    try:
        await asyncio.wait_for(r.controls["entered"].wait(), 2)
        held = (await r.budget.get_reservations())[0]["estimated_dollars"]
        r.budget.reload_from_settings({"cost": {"global_per_hour_usd": held * 1.5}})
        assert await r.scene.analyze_frame(r.image, node_id="second", mode="query", query="Synthetic card", force=True) is None
        assert len(r.calls) == 1 and len(await r.budget.get_reservations()) == 1
        r.controls["release"].set()
        assert await first
        assert (await r.budget.get_reservations())[0]["status"] == "settled"
    finally:
        r.controls["release"].set()
        await first
        await close(r)
