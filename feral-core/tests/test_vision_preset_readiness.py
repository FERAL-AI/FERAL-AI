"""Public preset application keeps installed models separate from inference proof."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from agents.llm_provider import LLMProvider, LLM_PRESETS
from api.routes import llm as routes
from config.loader import ConfigLoader


@pytest_asyncio.fixture
async def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("FERAL_LLM_PROVIDER", "openai")
    monkeypatch.setenv("FERAL_LLM_MODEL", "fixture-text")
    monkeypatch.setattr("agents.llm_provider._resolve_api_key", lambda _: "inert-fixture-key")
    monkeypatch.setattr(LLMProvider, "_detect_ollama", lambda _: None)
    llm = LLMProvider()
    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover(load_credentials=False)
    config.update_settings("llm", "provider", "openai")
    config.update_settings("llm", "model", "fixture-text")
    config.update_settings("vision", "enabled", False)
    config.update_settings("vision", "provider", "unchanged")
    config.update_settings("vision", "model", "unchanged")
    writes = Mock(wraps=config.update_settings)
    monkeypatch.setattr(config, "update_settings", writes)
    switch = AsyncMock(wraps=llm.switch_provider)
    monkeypatch.setattr(llm, "switch_provider", switch)
    monkeypatch.setattr(routes, "state", SimpleNamespace(orchestrator=SimpleNamespace(llm=llm), config=config))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://inert.local") as client:
        yield llm, config, writes, switch, client
    await llm.close()


@pytest.mark.parametrize("inventory,error_code", [
    ({"mistral", "mistral:7b"}, "vision_model_not_installed"),
    (set(), "vision_model_not_installed"),
    (None, "vision_model_unverified"),
    ({"llava"}, "vision_model_not_installed"),
    ({"llava", "llava:"}, "vision_model_not_installed"),
])
async def test_public_refusal_keeps_provider_and_saved_vision_unchanged(wired, monkeypatch, inventory, error_code):
    llm, config, writes, switch, client = wired
    monkeypatch.setattr(llm, "_ollama_pulled_models", AsyncMock(return_value=inventory))
    settings = config.user_home / "settings.json"
    before_bytes = settings.read_bytes()
    before_config = copy.deepcopy(config._merged)
    original_client = llm.client
    response = await client.post("/api/llm/presets/apply", json={"preset": "ollama_vision"})
    result = response.json()
    assert response.status_code == 200 and result["ok"] is False
    assert result["error_code"] == error_code
    assert llm.provider == "openai" and llm.model == "fixture-text"
    assert llm.client is original_client and not original_client.is_closed
    assert settings.read_bytes() == before_bytes and config._merged == before_config
    writes.assert_not_called()
    switch.assert_not_awaited()


async def test_inventory_failure_is_redacted_and_never_mutates_settings(wired, monkeypatch):
    llm, config, writes, switch, client = wired
    monkeypatch.setattr(llm, "_ollama_pulled_models", AsyncMock(side_effect=RuntimeError("private-inventory-canary")))
    settings = config.user_home / "settings.json"
    before = settings.read_bytes()
    response = await client.post("/api/llm/presets/apply", json={"preset": "ollama_vision"})
    assert response.json()["error_code"] == "vision_model_unverified"
    assert response.json()["ok"] is False and "private-inventory-canary" not in response.text
    writes.assert_not_called()
    switch.assert_not_awaited()
    assert settings.read_bytes() == before


@pytest.mark.parametrize("model", [None, 0, True, {}, []])
async def test_invalid_preset_model_is_refused_before_inventory_or_switch(wired, monkeypatch, model):
    llm, config, writes, switch, client = wired
    monkeypatch.setitem(LLM_PRESETS, "ollama_vision", {**LLM_PRESETS["ollama_vision"], "model": model})
    inventory = AsyncMock(side_effect=AssertionError("Invalid model must not trigger discovery"))
    monkeypatch.setattr(llm, "_ollama_pulled_models", inventory)
    settings = config.user_home / "settings.json"
    before = settings.read_bytes()
    result = (await client.post("/api/llm/presets/apply", json={"preset": "ollama_vision"})).json()
    assert result["ok"] is False and result["error_code"] == "preset_model_invalid"
    inventory.assert_not_awaited()
    writes.assert_not_called()
    switch.assert_not_awaited()
    assert llm.provider == "openai" and llm.model == "fixture-text"
    assert settings.read_bytes() == before


@pytest.mark.parametrize("failure", ["unsupported", "exception"])
async def test_existing_capability_helper_refuses_before_switch(wired, monkeypatch, failure):
    llm, config, writes, switch, client = wired
    monkeypatch.setattr(llm, "_ollama_pulled_models", AsyncMock(return_value={"llava", "llava:7b"}))
    capability = Mock(return_value=(False, "not supported")) if failure == "unsupported" else Mock(side_effect=RuntimeError("private-capability-canary"))
    monkeypatch.setattr(llm, "_vision_support_for", capability)
    settings = config.user_home / "settings.json"
    before = settings.read_bytes()
    response = await client.post("/api/llm/presets/apply", json={"preset": "ollama_vision"})
    result = response.json()
    assert result["ok"] is False
    assert result["error_code"] == ("vision_model_unsupported" if failure == "unsupported" else "vision_capability_unverified")
    assert "private-capability-canary" not in response.text
    capability.assert_called_once_with("ollama", "llava:7b")
    switch.assert_not_awaited()
    writes.assert_not_called()
    assert settings.read_bytes() == before


@pytest.mark.parametrize("requested,inventory,selected", [
    ("llava", {"llava", "llava:7b"}, "llava:7b"),
    ("llava", {"llava", "llava:7b", "llava:latest"}, "llava:latest"),
    ("llava:7b", {"llava", "llava:7b"}, "llava:7b"),
])
async def test_installed_classified_vision_selection_does_not_claim_inference(wired, monkeypatch, requested, inventory, selected):
    llm, config, writes, switch, client = wired
    monkeypatch.setitem(LLM_PRESETS, "ollama_vision", {**LLM_PRESETS["ollama_vision"], "model": requested})
    monkeypatch.setattr(llm, "_ollama_pulled_models", AsyncMock(return_value=inventory))
    capability = Mock(wraps=llm._vision_support_for)
    monkeypatch.setattr(llm, "_vision_support_for", capability)
    response = await client.post("/api/llm/presets/apply", json={"preset": "ollama_vision"})
    result = response.json()
    assert result["ok"] is True and result["vision_supported"] is True
    assert result["model"] == selected and llm.model == selected and llm.provider == "ollama"
    assert result["readiness"] == {"model_presence": "confirmed", "capability_basis": "model_classification", "inference_verified": False}
    capability.assert_called_once_with("ollama", selected)
    switch.assert_awaited_once_with(provider="ollama", model=selected, api_key="")
    assert config._merged["vision"]["enabled"] is True and config._merged["vision"]["model"] == selected
    assert writes.call_count == 5


async def test_exact_requested_tag_never_matches_manufactured_base_alias(wired, monkeypatch):
    llm, _config, writes, switch, client = wired
    monkeypatch.setitem(LLM_PRESETS, "ollama_vision", {**LLM_PRESETS["ollama_vision"], "model": "llava:7b"})
    monkeypatch.setattr(llm, "_ollama_pulled_models", AsyncMock(return_value={"llava", "llava:13b"}))
    result = (await client.post("/api/llm/presets/apply", json={"preset": "ollama_vision"})).json()
    assert result["ok"] is False and result["error_code"] == "vision_model_not_installed"
    writes.assert_not_called()
    switch.assert_not_awaited()


async def test_ordinary_text_preset_still_auto_detects_without_vision_inventory(wired, monkeypatch):
    llm, config, writes, switch, client = wired
    inventory = AsyncMock(side_effect=AssertionError("text preset must not discover vision inventory"))
    monkeypatch.setattr(llm, "_ollama_pulled_models", inventory)
    monkeypatch.setattr(llm, "_detect_ollama", lambda: "fixture-text:8b")
    result = (await client.post("/api/llm/presets/apply", json={"preset": "ollama_text"})).json()
    assert result["ok"] is True and result["model"] == "fixture-text:8b"
    assert result["vision_supported"] is False
    assert config._merged["vision"]["enabled"] is False and writes.call_count == 2
    inventory.assert_not_awaited()
    switch.assert_awaited_once_with(provider="ollama", model="", api_key="")
