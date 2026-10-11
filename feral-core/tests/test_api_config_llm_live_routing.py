"""Generic config writes must update real per-call-site routing immediately.

No adapter/model calls: construction is bypassed, the existing provider swap
is an awaited mock, and the real route_call implementation resolves references.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def routing_client(monkeypatch):
    from agents.llm_provider import LLMProvider
    from api.routes import config as config_routes, llm as llm_routes

    class Settings:
        def __init__(self):
            self._merged = {
                "llm": {
                    "provider": "ollama", "model": "fixture-primary",
                    "base_url": "http://127.0.0.1:11435/v1",
                    "fallback_providers": ["openai", "anthropic"],
                    "call_site_tiers": {"chat": "balanced", "future-site": "future-tier"},
                    "tier_map": {"future-site": {"opaque": {"retain": True}}},
                    "future": {"nested": ["retain"]},
                },
                "unrelated": {"keep": True},
            }

        def update_settings(self, section, key, value):
            self._merged.setdefault(section, {})[key] = deepcopy(value)

        def to_client_safe_dict(self):
            return deepcopy(self._merged)

    settings = Settings()
    provider = LLMProvider.__new__(LLMProvider)
    provider.provider = "ollama"
    provider.model = "fixture-primary"
    provider.base_url = "http://127.0.0.1:11435/v1"
    provider.api_key = "existing-fixture-key"
    provider.set_config(deepcopy(settings._merged["llm"]))

    async def swap(selected, *, model, base_url, api_key):
        provider.provider = selected
        provider.model = model or provider.model
        provider.base_url = base_url
        provider.api_key = api_key

    provider.switch_provider = AsyncMock(side_effect=swap)
    fake_state = SimpleNamespace(config=settings, orchestrator=SimpleNamespace(llm=provider))
    monkeypatch.setattr(config_routes, "state", fake_state)
    monkeypatch.setattr(llm_routes, "state", fake_state)
    monkeypatch.setenv("OLLAMA_API_KEY", "fixture-provider-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-fallback-secret")
    app = FastAPI()
    app.include_router(config_routes.router)
    app.include_router(llm_routes.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, settings, provider


def update(client, key, value):
    return client.post("/api/config/update", json={"section": "llm", "key": key, "value": value})


@pytest.mark.parametrize("site", ["chat", "routing", "vision", "embedding"])
def test_saved_call_site_tier_is_used_without_restart(routing_client, site):
    client, settings, provider = routing_client
    tiers = {**settings._merged["llm"]["call_site_tiers"], site: "cheap"}
    response = update(client, "call_site_tiers", tiers)
    assert response.status_code == 200 and response.json()["ok"] is True
    route = client.get("/api/llm/route", params={"call_site": site}).json()
    assert route["call_site"] == site and route["tier"] == "cheap"
    assert route["source"] == "settings"
    assert route["fallback_providers"] == ["openai", "anthropic"]
    assert provider._config["call_site_tiers"]["future-site"] == "future-tier"
    assert provider._config["future"] == {"nested": ["retain"]}
    assert settings._merged["unrelated"] == {"keep": True}
    provider.switch_provider.assert_awaited_once_with(
        "ollama", model="fixture-primary", base_url="http://127.0.0.1:11435/v1",
        api_key="fixture-provider-secret",
    )
    assert "fixture-provider-secret" not in response.text
    assert "fixture-fallback-secret" not in response.text


def test_target_override_and_return_to_automatic_use_real_route(routing_client):
    client, settings, provider = routing_client
    tier_map = deepcopy(settings._merged["llm"]["tier_map"])
    tier_map["chat"] = {"cheap": {"provider": "openai", "model": "fixture-explicit", "future": {"keep": True}}}
    assert update(client, "tier_map", tier_map).json()["ok"]
    route = provider.route_call("chat", tier="cheap")
    assert (route["provider"], route["model"]) == ("openai", "fixture-explicit")
    del tier_map["chat"]["cheap"]["provider"]
    del tier_map["chat"]["cheap"]["model"]
    assert update(client, "tier_map", tier_map).json()["ok"]
    route = client.get("/api/llm/route", params={"call_site": "chat", "tier": "cheap"}).json()
    assert (route["provider"], route["model"]) == ("ollama", "fixture-primary")
    assert provider._config["tier_map"]["chat"]["cheap"]["future"] == {"keep": True}
    assert provider._config["tier_map"]["future-site"] == {"opaque": {"retain": True}}


def test_runtime_snapshot_detached_from_mutable_saved_maps(routing_client):
    client, settings, provider = routing_client
    assert update(client, "call_site_tiers", {"chat": "cheap"}).json()["ok"]
    settings._merged["llm"]["call_site_tiers"]["chat"] = "premium"
    settings._merged["llm"]["future"]["nested"].append("later")
    assert provider.route_call("chat")["tier"] == "cheap"
    assert provider._config["future"] == {"nested": ["retain"]}


def test_existing_primary_credential_and_base_arguments_unchanged(routing_client, monkeypatch):
    client, settings, provider = routing_client
    monkeypatch.delenv("OLLAMA_API_KEY")
    assert update(client, "model", "fixture-new-primary").json()["ok"]
    provider.switch_provider.assert_awaited_once_with(
        "ollama", model="fixture-new-primary", base_url="http://127.0.0.1:11435/v1",
        api_key="fixture-fallback-secret",
    )
    assert provider.route_call("chat")["model"] == "fixture-new-primary"
    assert provider._config["fallback_providers"] == ["openai", "anthropic"]


def test_failed_swap_does_not_claim_runtime_snapshot_applied(routing_client):
    client, settings, provider = routing_client
    before = deepcopy(provider._config)
    provider.switch_provider.side_effect = RuntimeError("fixture-secret-system-detail")
    response = update(client, "call_site_tiers", {"chat": "cheap"})
    assert response.status_code == 500
    assert "fixture-secret-system-detail" not in response.text
    assert settings._merged["llm"]["call_site_tiers"] == {"chat": "cheap"}
    assert provider._config == before
    assert provider.route_call("chat")["tier"] == "balanced"


def test_without_orchestrator_write_stays_persisted(routing_client, monkeypatch):
    client, settings, provider = routing_client
    from api.routes import config as config_routes
    monkeypatch.setattr(config_routes.state, "orchestrator", None)
    assert update(client, "call_site_tiers", {"chat": "cheap"}).json()["ok"]
    assert settings._merged["llm"]["call_site_tiers"] == {"chat": "cheap"}
    provider.switch_provider.assert_not_awaited()
