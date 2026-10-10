"""Ollama capacity preflight: actual paths/bodies, no daemon or model changes."""

import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.context_manager import OllamaContextRefusal, verify_ollama_request_context
from agents.llm_provider import LLMProvider, ProviderCooldownTracker


class Server:
    def __init__(self, *, capacity=16384, parameters="", metadata_error=None):
        self.capacity = capacity
        self.parameters = parameters
        self.metadata_error = metadata_error
        self.requests = []
        self.inference = []

    def respond(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/api/ps"):
            if self.metadata_error is not None:
                return httpx.Response(self.metadata_error, text="private metadata body")
            rows = [] if self.capacity is None else [{"name": "fixture-model", "context_length": self.capacity}]
            return httpx.Response(200, json={"models": rows})
        if request.url.path.endswith("/api/show"):
            assert json.loads(request.content) == {"model": "fixture-model"}
            return httpx.Response(200, json={"parameters": self.parameters,
                                           "model_info": {"fixture.context_length": 128000}})
        body = json.loads(request.content)
        self.inference.append(body)
        if request.url.path.endswith("/api/chat"):
            return httpx.Response(200, json={"message": {"content": "42"}, "model": "fixture-model", "done": True})
        assert request.url.path.endswith("/v1/chat/completions")
        if body.get("stream"):
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"42"}}]}\n\ndata: [DONE]\n\n',
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "42"}}]})


def provider(server, *, name="ollama", base_url="http://127.0.0.1:11436/v1"):
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = name
    llm.model = "fixture-model"
    llm.base_url = base_url
    llm.api_key = "fixture"
    llm._local_engine = None
    llm._config = {"fallback_providers": []}
    llm._budget_check = AsyncMock(return_value=None)
    llm._budget_record = AsyncMock()
    llm._cooldown = ProviderCooldownTracker()
    llm._last_budget_routing = {}
    llm.client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(server.respond))
    return llm


async def invoke(llm, path, messages, tools=None):
    if path == "chat":
        return await llm.chat(messages, tools, max_tokens=256)
    if path == "stream":
        return [event async for event in llm.chat_stream(messages, tools, max_tokens=256)]
    if path == "routed":
        return await llm._call_provider("ollama", {"model": llm.model}, messages, tools, max_tokens=256)
    real_client = httpx.AsyncClient

    def isolated_client(*args, **kwargs):
        kwargs["transport"] = llm.client._transport
        return real_client(*args, **kwargs)

    with patch("agents.llm_provider.httpx.AsyncClient", isolated_client):
        return await llm._call_provider("ollama", {"model": llm.model, "api_key": "fixture",
                                                   "base_url": "http://127.0.0.1:11436/v1"},
                                        messages, tools, max_tokens=256)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "stream", "routed", "fallback"])
async def test_all_router_transports_check_capacity_before_unchanged_inference(path, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=16384)
    llm = provider(server, name="openai" if path == "fallback" else "ollama")
    messages = [{"role": "system", "content": "Exact user policy"}, {"role": "user", "content": "31 + 11"}]
    before = copy.deepcopy(messages)
    try:
        result = await invoke(llm, path, messages)
        if path == "stream":
            assert any(event.get("content") == "42" for event in result)
            assert not any(event["type"] == "error" for event in result)
        else:
            assert result["choices"][0]["message"]["content"] == "42"
        assert len(server.inference) == 1
        assert server.inference[0]["messages"] == before
        assert server.inference[0]["max_tokens"] == 256
        assert "options" not in server.inference[0]  # Unsupported num_ctx is never faked on /v1.
        assert [request.url.path for request in server.requests] == ["/api/ps", "/v1/chat/completions"]
        assert messages == before
    finally:
        await llm.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "stream", "routed", "fallback"])
async def test_low_capacity_refuses_without_truncation_or_inference(path, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=4096)
    llm = provider(server, name="openai" if path == "fallback" else "ollama")
    messages = [{"role": "system", "content": "user policy " * 1500}, {"role": "user", "content": "Latest input"}]
    before = copy.deepcopy(messages)
    try:
        if path in ("routed", "fallback"):
            with pytest.raises(OllamaContextRefusal) as error:
                await invoke(llm, path, messages)
            assert error.value.code == "local_context_overflow"
        else:
            result = await invoke(llm, path, messages)
            failure = result[0] if path == "stream" else result
            assert failure["error_code"] == "local_context_overflow"
            assert "estimated" in failure.get("content", failure.get("error"))
        assert server.inference == []
        assert messages == before
    finally:
        await llm.close()


@pytest.mark.asyncio
async def test_unloaded_model_needs_explicit_num_ctx_not_trained_capacity(monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=None)
    llm = provider(server)
    try:
        result = await llm.chat([{"role": "user", "content": "hello"}], max_tokens=256)
        assert result["error_code"] == "local_context_unverified"
        assert "PARAMETER num_ctx" in result["error"]
        assert server.inference == []
        server.parameters = "temperature 0.7\nnum_ctx 16384"
        result = await llm.chat([{"role": "user", "content": "hello"}], max_tokens=256)
        assert result["choices"]
        assert llm.context_window_tokens == 16384
        assert [request.url.path for request in server.requests] == ["/api/ps", "/api/show", "/api/ps", "/api/show", "/v1/chat/completions"]
    finally:
        await llm.close()


@pytest.mark.asyncio
async def test_live_allocation_overrides_larger_model_parameter_and_rechecks(monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=16384, parameters="num_ctx 128000")
    llm = provider(server)
    messages = [{"role": "user", "content": "Long input " * 1000}]
    try:
        assert llm.context_window_tokens == 4096  # Unverified planning fallback.
        assert (await llm.chat(messages, max_tokens=256))["choices"]
        assert llm.context_window_tokens == 16384
        server.capacity = 512
        result = await llm.chat(messages, max_tokens=256)
        assert result["error_code"] == "local_context_overflow"
        assert len(server.inference) == 1
        assert sum(request.url.path == "/api/ps" for request in server.requests) == 2
        llm.model = "other-model"
        assert llm.context_window_tokens == 4096
    finally:
        await llm.close()


@pytest.mark.asyncio
async def test_explicit_context_mismatch_is_not_runtime_configuration(monkeypatch):
    monkeypatch.setenv("FERAL_CONTEXT_WINDOW_TOKENS", "16384")
    server = Server(capacity=4096)
    llm = provider(server)
    try:
        result = await llm.chat([{"role": "user", "content": "hello"}])
        assert result["error_code"] == "local_context_mismatch"
        assert "16384" in result["error"] and "4096" in result["error"]
        assert server.inference == []
        monkeypatch.setenv("FERAL_CONTEXT_WINDOW_TOKENS", "512")
        result = await llm.chat([{"role": "user", "content": "hello"}], max_tokens=128)
        assert result["choices"]
        assert llm.context_window_tokens == 512
    finally:
        await llm.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["bad", "0", "-4", "999999999"])
async def test_invalid_declared_context_refuses_before_metadata_network(configuration, monkeypatch):
    monkeypatch.setenv("FERAL_CONTEXT_WINDOW_TOKENS", configuration)
    server = Server()
    llm = provider(server)
    try:
        result = await llm.chat([{"role": "user", "content": "hello"}])
        assert result["error_code"] == "local_context_configuration"
        assert server.requests == []
    finally:
        await llm.close()


@pytest.mark.asyncio
async def test_metadata_failure_is_redacted_and_does_not_infer(monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(metadata_error=503)
    llm = provider(server)
    try:
        result = await llm.chat([{"role": "user", "content": "private prompt"}])
        assert result["error_code"] == "local_context_unverified"
        assert "private metadata body" not in result["error"]
        assert "private prompt" not in result["error"]
        assert server.inference == []
        assert len(server.requests) == 1
    finally:
        await llm.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["timeout", "invalid_json", "missing_models"])
async def test_unusable_capacity_response_cannot_trigger_inference(response, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    requests = []

    def respond(request):
        requests.append(request)
        if response == "timeout":
            raise httpx.ReadTimeout("private address/credential", request=request)
        if response == "invalid_json":
            return httpx.Response(200, text="private non-json response")
        return httpx.Response(200, json={"choices": []})

    async with httpx.AsyncClient(base_url="http://brain/v1", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OllamaContextRefusal) as error:
            await verify_ollama_request_context(client, {"model": "fixture-model", "messages": [], "max_tokens": 128})
        assert error.value.code == "local_context_unverified"
        assert "private" not in str(error.value)
    assert len(requests) == 1
    assert requests[0].url.path == "/api/ps"
    assert requests[0].extensions["timeout"]["read"] == 5.0


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["num_ctx -1", "num_ctx 0", "num_ctx 999999999", "num_ctx 4.2"])
async def test_bad_explicit_model_context_is_not_a_capacity_claim(parameter, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=None, parameters=parameter)
    llm = provider(server)
    try:
        result = await llm.chat([{"role": "user", "content": "hello"}])
        assert result["error_code"] == "local_context_unverified"
        assert server.inference == []
    finally:
        await llm.close()


@pytest.mark.asyncio
async def test_schemas_and_output_are_counted_and_no_prompt_logs(monkeypatch, caplog):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=512)
    async with httpx.AsyncClient(base_url="http://brain/v1", transport=httpx.MockTransport(server.respond)) as client:
        body = {"model": "fixture-model", "messages": [{"role": "user", "content": "private message"}],
                "tools": [{"function": {"description": "private schema " * 100}}], "max_tokens": 128}
        caplog.set_level("INFO", logger="feral.orchestrator.context")
        with pytest.raises(OllamaContextRefusal) as error:
            await verify_ollama_request_context(client, body)
        assert error.value.code == "local_context_overflow"
        assert "tokenizer_verified=false" in caplog.text
        assert "private message" not in caplog.text and "private schema" not in caplog.text
        assert server.inference == []


@pytest.mark.asyncio
async def test_other_model_allocation_cannot_certify_selected_model(monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "foreign-model", "context_length": 128000}]})
        return httpx.Response(200, json={"parameters": "", "model_info": {"fixture.context_length": 128000}})

    async with httpx.AsyncClient(base_url="http://brain/proxy/v1", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OllamaContextRefusal):
            await verify_ollama_request_context(client, {"model": "fixture-model", "messages": [], "max_tokens": 128})
    assert [request.url.path for request in requests] == ["/proxy/api/ps", "/proxy/api/show"]


@pytest.mark.asyncio
async def test_proven_overflow_does_not_use_cloud_fallback(monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=512)
    llm = provider(server)
    llm._config = {"fallback_providers": ["openai"]}
    llm._build_candidate_list = MagicMock(return_value=[
        ("ollama", {"model": "fixture-model", "supported": True}),
        ("openai", {"model": "fixture-cloud", "supported": True, "api_key": "fixture", "base_url": "http://foreign/v1"}),
    ])
    try:
        result = await llm.chat_with_failover([{"role": "user", "content": "Too much input " * 500}], max_tokens=256)
        assert result["error_code"] == "local_context_overflow"
        assert server.inference == []
        assert len(server.requests) == 1
    finally:
        await llm.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["failover", "stream"])
async def test_unverified_metadata_preserves_only_existing_configured_fallback(path, monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(metadata_error=503)
    llm = provider(server)
    llm._config = {"fallback_providers": ["openai"]}
    llm._build_candidate_list = MagicMock(return_value=[
        ("ollama", {"model": "fixture-model", "supported": True}),
        ("openai", {"model": "fixture-cloud", "supported": True, "api_key": "fixture", "base_url": "http://configured-cloud/v1"}),
    ])
    real_client = httpx.AsyncClient

    def isolated_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.respond)
        return real_client(*args, **kwargs)

    try:
        with patch("agents.llm_provider.httpx.AsyncClient", isolated_client):
            if path == "failover":
                result = await llm.chat_with_failover([{"role": "user", "content": "hello"}], max_tokens=256)
                assert result["choices"][0]["message"]["content"] == "42"
                assert result["last_failover"]["to"] == "openai"
            else:
                events = [event async for event in llm.chat_stream([{"role": "user", "content": "hello"}], max_tokens=256)]
                assert any(event.get("content") == "42" for event in events)
                assert not any(event["type"] == "error" for event in events)
        assert len(server.inference) == 1
        assert server.inference[0]["model"] == "fixture-cloud"
        assert [request.url.host for request in server.requests] == ["127.0.0.1", "configured-cloud"]
    finally:
        await llm.close()


@pytest.mark.asyncio
async def test_planning_cache_cannot_cross_endpoint_model_or_age(monkeypatch):
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server()
    llm = provider(server)
    try:
        await llm.chat([{"role": "user", "content": "hello"}], max_tokens=256)
        assert llm.context_window_tokens == 16384
        previous = llm.base_url
        llm.base_url = "http://other-endpoint/v1"
        assert llm.context_window_tokens == 4096
        llm.base_url = previous
        endpoint, model, capacity, observed = llm._ollama_context_observation
        llm._ollama_context_observation = (endpoint, model, capacity, observed - 31)
        assert llm.context_window_tokens == 4096
    finally:
        await llm.close()


@pytest.mark.asyncio
async def test_native_ollama_adapter_checks_before_unchanged_api_chat(monkeypatch):
    from providers.base import ChatMessage
    from providers.ollama_provider import OllamaProvider
    monkeypatch.delenv("FERAL_CONTEXT_WINDOW_TOKENS", raising=False)
    server = Server(capacity=16384)
    real_client = httpx.AsyncClient

    def isolated_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.respond)
        return real_client(*args, **kwargs)

    with patch("providers.ollama_provider.httpx.AsyncClient", isolated_client):
        adapter = OllamaProvider("http://127.0.0.1:11436")
        result = await adapter.chat([ChatMessage("user", "31+11")], model="fixture-model")
        assert result.text == "42"
        assert [request.url.path for request in server.requests] == ["/api/ps", "/api/chat"]
        assert server.inference[0]["options"] == {"num_predict": 1024}
        assert "num_ctx" not in server.inference[0]["options"]
        server.capacity = 512
        with pytest.raises(OllamaContextRefusal):
            await adapter.chat([ChatMessage("user", "31+11")], model="fixture-model")
        assert len(server.inference) == 1
