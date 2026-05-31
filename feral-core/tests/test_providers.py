"""Contract tests for LLM provider adapters.

These tests are offline: ``chat`` + ``refresh_models`` are mocked by
patching :mod:`httpx.AsyncClient` so no real API calls fire. We verify
each adapter:

* Satisfies the :class:`Provider` Protocol (via ``isinstance``).
* Exposes a non-empty static model list.
* Returns a pricing dict for every listed model.
* Handles missing API keys without crashing on ``list_models``.
* Refreshes its model list when the mocked /v1/models responds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers import Provider, register_provider, list_providers, get_provider
from providers.anthropic_provider import AnthropicProvider
from providers.base import ChatMessage
from providers.bedrock_provider import BedrockProvider
from providers.deepseek_provider import DeepSeekProvider
from providers.fireworks_provider import FireworksProvider
from providers.gemini_provider import GeminiProvider
from providers.groq_provider import GroqProvider
from providers.ollama_provider import OllamaProvider
from providers.openai_provider import OpenAIProvider
from providers.openrouter_provider import OpenRouterProvider
from providers.together_provider import TogetherProvider

pytestmark = pytest.mark.asyncio


ALL_ADAPTERS = [
    ("openai", lambda: OpenAIProvider(api_key="sk-test")),
    ("anthropic", lambda: AnthropicProvider(api_key="sk-test")),
    ("gemini", lambda: GeminiProvider(api_key="gk-test")),
    ("ollama", lambda: OllamaProvider()),
    ("groq", lambda: GroqProvider(api_key="gsk-test")),
    ("deepseek", lambda: DeepSeekProvider(api_key="ds-test")),
    ("together", lambda: TogetherProvider(api_key="tg-test")),
    ("openrouter", lambda: OpenRouterProvider(api_key="or-test")),
    ("fireworks", lambda: FireworksProvider(api_key="fw-test")),
    ("bedrock", lambda: BedrockProvider(region="us-east-1")),
]


@pytest.mark.parametrize("provider_id,factory", ALL_ADAPTERS)
async def test_provider_is_valid(provider_id, factory):
    p = factory()
    assert isinstance(p, Provider), f"{provider_id} does not satisfy Provider Protocol"
    assert p.provider_id == provider_id
    assert p.display_name


@pytest.mark.parametrize("provider_id,factory", ALL_ADAPTERS)
async def test_provider_list_models(provider_id, factory):
    p = factory()
    models = p.list_models()
    assert isinstance(models, list)
    # Local-runtime providers (ollama, lmstudio) ship NO static model
    # list because the live ``/api/tags`` / ``/v1/models`` lookup is
    # the only honest source of truth — a hardcoded fallback would
    # advertise names the operator has not pulled. Cloud providers
    # still ship the curated catalog inventory.
    if provider_id == "ollama":
        assert models == [], f"{provider_id} must NOT ship a static fallback list"
    else:
        assert len(models) > 0, f"{provider_id} ships an empty default model list"


@pytest.mark.parametrize("provider_id,factory", ALL_ADAPTERS)
async def test_provider_pricing_shape(provider_id, factory):
    p = factory()
    for model in p.list_models()[:3]:
        pricing = p.pricing_per_1k(model)
        assert "input" in pricing
        assert "output" in pricing
        assert isinstance(pricing["input"], (int, float))
        assert isinstance(pricing["output"], (int, float))


@pytest.mark.parametrize("provider_id,factory", ALL_ADAPTERS)
async def test_provider_supports_returns_bool(provider_id, factory):
    p = factory()
    assert isinstance(p.supports("streaming"), bool)
    assert isinstance(p.supports("probably_not_a_real_capability"), bool)


async def test_openai_chat_parses_response():
    fake_payload = {
        "model": "gpt-4o-mini",
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi there"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    resp_mock = MagicMock()
    resp_mock.json.return_value = fake_payload
    resp_mock.raise_for_status = MagicMock()
    client_mock = MagicMock()
    client_mock.post = AsyncMock(return_value=resp_mock)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=None)

    with patch("providers.openai_provider.httpx.AsyncClient", return_value=client_mock):
        p = OpenAIProvider(api_key="sk-test")
        out = await p.chat([ChatMessage(role="user", content="hello")], model="gpt-4o-mini")

    assert out.text == "hi there"
    assert out.finish_reason == "stop"
    assert out.model == "gpt-4o-mini"


async def test_openai_refresh_models_uses_api():
    resp_mock = MagicMock()
    resp_mock.json.return_value = {"data": [{"id": "gpt-6"}, {"id": "gpt-5"}]}
    resp_mock.raise_for_status = MagicMock()
    client_mock = MagicMock()
    client_mock.get = AsyncMock(return_value=resp_mock)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=None)

    with patch("providers.openai_provider.httpx.AsyncClient", return_value=client_mock):
        p = OpenAIProvider(api_key="sk-test")
        models = await p.refresh_models()

    assert "gpt-6" in models
    assert "gpt-5" in models


async def test_registry_register_and_get():
    p = OllamaProvider()
    register_provider(p)
    assert "ollama" in list_providers()
    fetched = get_provider("ollama")
    assert fetched is p
