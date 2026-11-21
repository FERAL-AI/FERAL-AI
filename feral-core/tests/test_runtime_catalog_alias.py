"""Catalog identity survives configuration while runtime routing stays canonical."""
from unittest.mock import patch

from agents.llm_provider import LLMProvider, runtime_provider_id


def test_catalog_identity_boots_the_actual_registered_runtime(monkeypatch):
    monkeypatch.setenv("FERAL_LLM_PROVIDER", "moonshot")
    monkeypatch.setenv("FERAL_LLM_MODEL", "fixture-model")
    monkeypatch.setenv("FERAL_LLM_BASE_URL", "")
    monkeypatch.setenv("MOONSHOT_API_KEY", "fixture-provider-key")
    with patch.object(LLMProvider, "_detect_ollama", return_value=None):
        provider = LLMProvider()
    assert provider.provider == "kimi"
    assert provider.base_url == "https://api.moonshot.ai/v1"
    assert provider.model == "fixture-model"
    provider.set_config({"fallback_providers": ["moonshot", "kimi", "bedrock"]})
    candidates = provider._build_candidate_list()
    assert [name for name, _ in candidates] == ["kimi", "bedrock"]
    assert candidates[-1][1]["supported"] is False
    assert provider._get_provider_config("moonshot")["supported"] is True
    routed = provider._candidates_for_route("moonshot", "fixture-routed-model")
    assert [name for name, _ in routed] == ["kimi", "bedrock"]
    assert routed[0][1]["model"] == "fixture-routed-model"


def test_alias_resolution_does_not_promote_unknown_provider():
    assert runtime_provider_id("my-gateway") == "my-gateway"
    assert runtime_provider_id("bedrock") == "bedrock"
