"""No account access: exercise production adapters over HTTP MockTransport."""
import json
import httpx
import pytest

from agents.llm_provider import LLMProvider, ProviderCooldownTracker
from providers.base import ChatMessage
from providers.openai_provider import OpenAIProvider

MODELS = ['gpt-5.4', 'gpt-5.4-mini', 'gpt-5.4-nano', 'gpt-5.4-2026-03-05', 'gpt-5.5', 'gpt-5.5-pro', 'gpt-5.6-sol']
TOOLS = [{'type': 'function', 'function': {'name': 'inspect', 'parameters': {'type': 'object', 'properties': {}}}}]
MESSAGES = [{'role': 'system', 'content': 'Inspect safely.'}, {'role': 'user', 'content': 'Read the page.'}]
OUTPUT = {'id': 'resp_mock', 'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'Inspected.'}]}], 'usage': {'input_tokens': 4, 'output_tokens': 3}}


class Wire:
    def __init__(self, status=200):
        self.requests = []
        self.status = status

    def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        if self.status != 200:
            return httpx.Response(self.status, json={'error': {'message': 'Rejected request', 'type': 'invalid_request_error'}})
        if request.url.path.endswith('/chat/completions'):
            if body.get('tools') and body.get('reasoning_effort') != 'none':
                return httpx.Response(400, json={'error': {'message': 'Function tools with reasoning_effort require Responses'}})
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Legacy.'}}]})
        if body.get('stream'):
            chunks = [
                {'type': 'response.output_text.delta', 'delta': 'Inspected.'},
                {'type': 'response.completed', 'response': OUTPUT},
            ]
            data = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks)
            return httpx.Response(200, text=data, headers={'Content-Type': 'text/event-stream'})
        return httpx.Response(200, json=OUTPUT)

    def assert_responses(self, model, *, key='mock-primary', tools=True):
        assert len(self.requests) == 1
        request, body = self.requests[0]
        assert request.url == 'https://example.test/v1/responses'
        assert request.headers['Authorization'] == 'Bearer ' + key
        assert body['model'] == model
        assert body['reasoning'] == {'effort': 'medium'}
        assert 'reasoning_effort' not in body and 'messages' not in body
        assert 'temperature' not in body
        assert body['max_output_tokens'] == 128
        if tools:
            assert body['tools'][0]['name'] == 'inspect'


def provider(model, wire, name='openai'):
    p = LLMProvider.__new__(LLMProvider)
    p.provider, p.model = name, model
    p.api_key, p.base_url = 'mock-primary', 'https://example.test/v1'
    p.available = True
    p._config = {'fallback_providers': []}
    p._local_engine = p._hybrid_cloud_provider = None
    p._cooldown = ProviderCooldownTracker()
    p._last_failover = None
    p._auth_permanent_until, p._auth_permanent_logged = {}, set()
    p.client = httpx.AsyncClient(base_url=p.base_url, headers={'Authorization': 'Bearer mock-primary'}, transport=httpx.MockTransport(wire.handle))
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('stream', [False, True])
async def test_public_primary_chat_and_stream_wire(model, stream):
    wire = Wire()
    p = provider(model, wire)
    try:
        if stream:
            events = [event async for event in p.chat_stream(MESSAGES, TOOLS, temperature=1, max_tokens=128)]
            assert [e['type'] for e in events] == ['text_delta', 'done']
            assert events[0]['content'] == 'Inspected.'
        else:
            result = await p.chat(MESSAGES, TOOLS, temperature=1, max_tokens=128)
            assert result['choices'][0]['message']['content'] == 'Inspected.'
        wire.assert_responses(model)
        assert (p.provider, p.model, p.api_key) == ('openai', model, 'mock-primary')
    finally:
        await p.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('primary', [False, True])
async def test_candidate_wire_uses_exact_binding(monkeypatch, model, primary):
    wire = Wire()
    p = provider(model if primary else 'deepseek-chat', wire, 'openai' if primary else 'deepseek')
    real_client = httpx.AsyncClient
    monkeypatch.setattr('agents.llm_provider.httpx.AsyncClient', lambda *a, **kw: real_client(*a, **kw, transport=httpx.MockTransport(wire.handle)))
    try:
        result = await p._call_provider('openai', {'base_url': p.base_url, 'api_key': 'mock-candidate', 'model': model, 'supported': True}, MESSAGES, TOOLS, temperature=1, max_tokens=128)
        assert result['choices'][0]['message']['content'] == 'Inspected.'
        wire.assert_responses(model, key='mock-primary' if primary else 'mock-candidate')
        assert p.api_key == 'mock-primary'
    finally:
        await p.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('model', MODELS)
async def test_pluggable_provider_wire(monkeypatch, model):
    wire = Wire()
    real_client = httpx.AsyncClient
    monkeypatch.setattr('providers.openai_provider.httpx.AsyncClient', lambda *a, **kw: real_client(*a, **kw, transport=httpx.MockTransport(wire.handle)))
    p = OpenAIProvider(api_key='mock-primary', base_url='https://example.test/v1')
    result = await p.chat([ChatMessage('user', 'Inspect.')], model=model, tools=TOOLS, temperature=1, max_tokens=128)
    assert result.text == 'Inspected.'
    wire.assert_responses(model)


@pytest.mark.asyncio
async def test_selected_vision_keeps_image_provider_model_and_key(monkeypatch):
    wire = Wire()
    p = provider('gpt-4o', wire)
    p._vision_support_for = lambda *a: (True, '')
    real_client = httpx.AsyncClient
    monkeypatch.setattr('agents.llm_provider.httpx.AsyncClient', lambda *a, **kw: real_client(*a, **kw, transport=httpx.MockTransport(wire.handle)))
    image = 'data:image/png;base64,mocked'
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'Inspect.'}, {'type': 'image_url', 'image_url': {'url': image}}]}]
    try:
        result = await p.chat_selected_vision(messages, provider='openai', model='gpt-5.4', base_url=p.base_url, api_key='mock-vision', max_tokens=128)
        assert not result.get('error')
        wire.assert_responses('gpt-5.4', key='mock-vision', tools=False)
        assert wire.requests[0][1]['input'][0]['content'][1]['image_url'] == image
        assert (p.provider, p.model, p.api_key) == ('openai', 'gpt-4o', 'mock-primary')
    finally:
        await p.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_http_400_never_downgrades_replays_or_falls_through(stream):
    wire = Wire(status=400)
    p = provider('gpt-5.4', wire)
    try:
        if stream:
            events = [event async for event in p.chat_stream(MESSAGES, TOOLS, max_tokens=128)]
            assert [e['type'] for e in events] == ['error']
        else:
            assert (await p.chat(MESSAGES, TOOLS, max_tokens=128)).get('error')
        wire.assert_responses('gpt-5.4')
    finally:
        await p.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['gpt-5.2', 'gpt-4o', 'o3'])
async def test_old_models_keep_legacy_wire(model):
    wire = Wire()
    p = provider(model, wire)
    try:
        result = await p.chat(MESSAGES, None, max_tokens=128)
        assert not result.get('error')
        assert len(wire.requests) == 1
        assert wire.requests[0][0].url.path == '/v1/chat/completions'
    finally:
        await p.client.aclose()


@pytest.mark.asyncio
async def test_unsupported_live_and_unsupported_provider_never_dispatch():
    wire = Wire()
    p = provider('gpt-live-1', wire)
    try:
        assert (await p.chat(MESSAGES, TOOLS)).get('error_code') == 'unsupported_live_protocol'
        with pytest.raises(RuntimeError):
            await p._call_provider('bedrock', {'model': 'gpt-5.4', 'supported': False}, MESSAGES, TOOLS)
        assert wire.requests == []
    finally:
        await p.client.aclose()
