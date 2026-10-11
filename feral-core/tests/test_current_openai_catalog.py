"""Official 2026-10-01 model metadata; mocked transport only, no live access claim."""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from providers.model_classes import classify, classify_endpoint

CATALOG = Path(__file__).parents[1] / "providers/model_catalog.json"


def entry():
    return json.loads(CATALOG.read_text())["providers"]["openai"]


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-luna"])
def test_published_ids_use_reasoning_responses_for_tools(model):
    assert classify("openai", model) == "reasoning"
    assert classify_endpoint("openai", model) == "responses"
    assert classify_endpoint("openrouter", "openai/" + model) == "responses"
    assert model in entry()["models"]
    caps = entry()["capabilities"][model]
    assert caps["max_input_tokens"] == 1_050_000
    assert caps["max_tokens"] == 128_000
    assert caps["image_input"] is True
    assert caps["audio_input"] is False
    assert caps["default_reasoning_effort"] == "medium"
    assert caps["function_calling_endpoint"] == "responses"


@pytest.mark.parametrize("model,input_rate,output_rate,cache", [
    ("gpt-6.1-sol", .002, .01, .0001), ("gpt-6-luna", .0001, .0005, .00001),
])
def test_standard_short_context_token_rates_are_per_thousand(model, input_rate, output_rate, cache):
    price = entry()["pricing"][model]
    assert (price["input"], price["output"], price["cache_read"]) == (input_rate, output_rate, cache)
    assert price["source"] == "https://developers.openai.com/api/docs/models/" + model
    assert "272K" in price["notes"]


def test_efforts_do_not_advertise_none_for_sol():
    caps = entry()["capabilities"]
    assert caps["gpt-6.1-sol"]["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert caps["gpt-6-luna"]["reasoning_efforts"] == ["none", "low", "medium", "high", "xhigh", "max"]


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-luna"])
def test_actual_adapter_builds_responses_tools_and_reasoning(model):
    from agents.llm_provider import LLMProvider, _responses_endpoint_for
    provider = LLMProvider.__new__(LLMProvider)
    provider.provider = "openai"
    provider.model = model
    tools = [{"type": "function", "function": {"name": "review", "parameters": {"type": "object", "properties": {}}}}]
    body = provider._build_responses_body([{"role": "user", "content": "hello"}], tools, 1, 128, stream=False)
    assert _responses_endpoint_for("openai", model)
    assert body["model"] == model
    assert body["reasoning"] == {"effort": "medium"}
    assert body["tools"][0]["name"] == "review"
    assert body["max_output_tokens"] == 128
    assert "messages" not in body
    assert "reasoning_effort" not in body
    assert "temperature" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-luna"])
async def test_actual_adapter_probe_posts_responses_not_chat(model):
    from agents.llm_provider import LLMProvider
    provider = LLMProvider.__new__(LLMProvider)
    provider.provider = "openai"
    provider.model = model
    provider.client = AsyncMock()
    provider.client.post.return_value = httpx.Response(200, json={"status": "completed"})
    assert await provider._probe_chat_availability() == (True, "")
    args = provider.client.post.await_args
    assert args.args == ("/responses",)
    assert args.kwargs["json"]["reasoning"] == {"effort": "medium"}


def test_live_is_informational_not_selectable_or_token_billed():
    o = entry()
    assert "gpt-live-1" not in o["models"]
    assert "gpt-live-1" not in o["pricing"]
    live = o["unsupported_models"]["gpt-live-1"]
    assert live["runtime_supported"] is False
    assert live["api_protocol"] == "live"
    assert live["endpoint"] == "/v1/live/sessions"
    assert live["pricing"] == {"unit": "USD per minute", "session_duration": .05, "billed_per_second": True, "backend_usage_separate": True}
    assert live["full_duplex"] is True
    assert live["tool_execution"] == "backend_delegation"
    assert live["image_input"] is False


def test_no_invented_gpt6_tiers_or_implicit_default_migration():
    from providers.recommended import recommended_for
    assert classify("openai", "gpt-6.1-hyperthinking") == "unknown"
    assert recommended_for("openai", ["gpt-5.6-sol", "gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"])[0] == "gpt-5.6-sol"
    assert "gpt-6-astra" in entry()["models"]
    assert "gpt-realtime-2.1" in entry()["models"]


@pytest.mark.parametrize("provider,model", [("openai", "gpt-live-1"), ("openrouter", "openai/gpt-live-1"), ("openrouter", "openai/gpt-live-1:free")])
def test_live_dynamic_inventory_never_becomes_chat_or_old_realtime(provider, model):
    from providers.model_classes import filter_models
    assert classify(provider, model) == "audio"
    assert classify_endpoint(provider, model) == "unsupported_live"
    assert model not in filter_models(provider, [model, "gpt-6.1-sol"], model_class="chat")
    assert model not in filter_models(provider, [model], model_class="realtime")


@pytest.mark.asyncio
async def test_dynamic_provider_refresh_excludes_live_from_chat(monkeypatch):
    from providers.openai_provider import OpenAIProvider
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, json={"data": [{"id": "gpt-live-1"}, {"id": "gpt-6.1-sol"}]}, request=httpx.Request("GET", "https://unit.test/models"))
    factory = AsyncMock()
    factory.__aenter__.return_value = client
    monkeypatch.setattr("providers.openai_provider.httpx.AsyncClient", lambda **kw: factory)
    adapter = OpenAIProvider(api_key="synthetic-unit-key")
    assert "gpt-live-1" in await adapter.refresh_models()
    assert "gpt-live-1" not in adapter.list_models(model_class="chat")
    assert "gpt-6.1-sol" in adapter.list_models(model_class="chat")


@pytest.mark.asyncio
async def test_live_runtime_fails_before_probe_chat_stream_or_failover_http():
    from agents.llm_provider import LLMProvider
    provider = LLMProvider.__new__(LLMProvider)
    provider.provider = "openai"
    provider.model = "gpt-live-1"
    provider.client = AsyncMock()
    assert (await provider.chat([]))["error_code"] == "unsupported_live_protocol"
    events = [event async for event in provider.chat_stream([])]
    assert events[0]["error_code"] == "unsupported_live_protocol"
    assert len(events) == 1
    ok, reason = await provider._probe_chat_availability()
    assert not ok and "unsupported_live_protocol" in reason
    with pytest.raises(RuntimeError, match="unsupported_live_protocol"):
        await provider._call_provider("openai", {"model": "gpt-live-1"}, [], None)
    with pytest.raises(ValueError, match="unsupported_live_protocol"):
        provider._build_responses_body([], None, 1, 128, stream=False)
    provider.client.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-luna"])
async def test_direct_adapter_actual_http_contract_uses_responses(monkeypatch, model):
    from providers.base import ChatMessage
    from providers.openai_provider import OpenAIProvider
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json={"model": model, "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "synthetic answer"}]}], "usage": {"input_tokens": 10, "output_tokens": 3}}, request=httpx.Request("POST", "https://unit.test/responses"))
    factory = AsyncMock()
    factory.__aenter__.return_value = client
    monkeypatch.setattr("providers.openai_provider.httpx.AsyncClient", lambda **kw: factory)
    adapter = OpenAIProvider(api_key="synthetic-unit-key", base_url="https://unit.test/v1")
    response = await adapter.chat([ChatMessage("user", "hello")], model=model, temperature=1, max_tokens=128, reasoning_effort="high", tools=[{"type": "function", "function": {"name": "review", "parameters": {"type": "object", "properties": {}}}}])
    assert response.text == "synthetic answer"
    call = client.post.await_args
    assert call.args == ("https://unit.test/v1/responses",)
    assert call.kwargs["json"]["reasoning"] == {"effort": "high"}
    assert call.kwargs["json"]["tools"][0]["name"] == "review"
    assert "temperature" not in call.kwargs["json"]
    assert "messages" not in call.kwargs["json"]
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_direct_adapter_live_never_creates_http_client(monkeypatch):
    from providers.openai_provider import OpenAIProvider
    def forbidden(**kw):
        raise AssertionError("HTTP client must not be constructed")
    monkeypatch.setattr("providers.openai_provider.httpx.AsyncClient", forbidden)
    with pytest.raises(RuntimeError, match="unsupported_live_protocol"):
        await OpenAIProvider(api_key="synthetic-unit-key").chat([], model="gpt-live-1")
