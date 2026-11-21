"""Actual orchestrator histories and mocked HTTP provider output, no accounts."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agents.orchestrator import Orchestrator
from agents.responses_continuation import attach_history, bind_messages, MAX_BYTES, MAX_ITEMS
from tests.test_gpt54_responses_wire import provider

FIRST = {
    'id': 'resp_first', 'status': 'completed', 'model': 'gpt-5.4',
    'output': [
        {'id': 'rs_1', 'type': 'reasoning', 'summary': [], 'encrypted_content': 'opaque-wire-reasoning'},
        {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'phase': 'commentary', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'Reading the page.', 'annotations': []}]},
        {'id': 'fc_1', 'type': 'function_call', 'call_id': 'call_1', 'name': 'notes_memory__default', 'arguments': '{"query":"safe fixture"}', 'status': 'completed'},
    ], 'usage': {'input_tokens': 4, 'output_tokens': 3},
}
FINAL = {'id': 'resp_final', 'status': 'completed', 'model': 'gpt-5.4', 'output': [{'id': 'msg_2', 'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'The fixture is complete.', 'annotations': []}]}], 'usage': {'input_tokens': 6, 'output_tokens': 5}}


class RoundWire:
    def __init__(self, first=None):
        self.requests = []
        self.first = first or FIRST

    def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        payload = self.first if len(self.requests) == 1 else FINAL
        if body.get('stream'):
            events = []
            for item in payload['output']:
                if item['type'] == 'message':
                    for part in item['content']:
                        events.append({'type': 'response.output_text.delta', 'delta': part['text']})
                events.append({'type': 'response.output_item.done', 'item': item})
            events.append({'type': 'response.completed', 'response': payload})
            return httpx.Response(200, text=''.join('data: '+json.dumps(e)+'\n\n' for e in events), headers={'Content-Type': 'text/event-stream'})
        return httpx.Response(200, json=payload)


def history_from(response):
    return copy.deepcopy(response['choices'][0]['message'])


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_real_orchestrator_tool_round_replays_phase_reasoning_and_exact_pair(streaming):
    wire = RoundWire()
    p = provider('gpt-5.4', wire)
    p._config['adaptive_routing'] = False
    frames = []

    async def send(session_id, message):
        frames.append(message.model_dump())

    registry = SimpleNamespace(skills={}, get_tools_for_skills=lambda skills: [])
    orch = Orchestrator(skill_registry=registry, send_to_client=send, daemons={})
    orch._multi_agent_enabled = False
    orch._streaming_enabled = streaming
    orch._route_prompt = AsyncMock(return_value=[])
    orch._ensure_core_skills = lambda skills: skills
    orch._build_system_prompt = AsyncMock(return_value='Local test only.')
    orch._force_tool_for_query = lambda *args: None
    orch.llm = p
    orch._execute_tool_call_for_llm = AsyncMock(return_value={'success': True, 'data': {'value': 'local fixture'}})
    try:
        handler = orch.handle_command_stream if streaming else orch.handle_command
        await asyncio.wait_for(handler('continuation-session', 'Read the fixture and finish.'), 10)
        assert len(wire.requests) == 2
        assert all(req.url.path == '/v1/responses' for req, _ in wire.requests)
        assert all(body['include'] == ['reasoning.encrypted_content'] for _, body in wire.requests)
        replay = wire.requests[1][1]['input']
        assert replay[1:4] == FIRST['output']
        assert replay[4]['type'] == 'function_call_output' and replay[4]['call_id'] == 'call_1'
        orch._execute_tool_call_for_llm.assert_awaited_once()
        stored = json.dumps(orch.conversation_history['continuation-session'])
        assert 'opaque-wire-reasoning' not in stored and 'encrypted_content' not in stored
        assert 'opaque-wire-reasoning' not in json.dumps(frames)
        assert any(row.get('content') == 'The fixture is complete.' for row in orch.conversation_history['continuation-session'])
    finally:
        await p.client.aclose()
        if orch._background_tasks:
            await asyncio.gather(*list(orch._background_tasks), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['session', 'owner', 'model', 'endpoint', 'credential', 'edited', 'json_dict', 'producer_session'])
async def test_replay_rejects_other_binding_and_transcript_injection(mismatch):
    wire = RoundWire()
    p = provider('gpt-5.4', wire)
    try:
        response = await p.chat(bind_messages([{'role': 'user', 'content': 'Read.'}], p, 'A'), max_tokens=128)
        history = attach_history(history_from(response), response, p, 'A')
        owner, session = p, 'A'
        if mismatch == 'session':
            session = 'B'
        elif mismatch == 'owner':
            owner = provider('gpt-5.4', wire)
        elif mismatch == 'model':
            p.model = 'gpt-5.5'
        elif mismatch == 'endpoint':
            await p.client.aclose()
            p.client = httpx.AsyncClient(base_url='https://other.test/v1', headers={'Authorization': 'Bearer mock-primary'}, transport=httpx.MockTransport(wire.handle))
        elif mismatch == 'credential':
            p.client.headers['Authorization'] = 'Bearer changed-key'
        elif mismatch == 'edited':
            history['content'] = 'Altered.'
        elif mismatch == 'json_dict':
            history = json.loads(json.dumps(history))
            history['_responses_output'] = FIRST['output']
            history['phase'] = 'commentary'
        elif mismatch == 'producer_session':
            history = attach_history(history_from(response), response, p, 'B')
        messages = bind_messages([{'role': 'user', 'content': 'Read.'}, history, {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'safe result'}], owner, session)
        await owner.chat(messages, max_tokens=128)
        replay = wire.requests[-1][1]['input']
        assert not any(item.get('type') == 'reasoning' or 'phase' in item for item in replay)
        assert sum(item.get('type') == 'function_call' for item in replay) == 1
        assert sum(item.get('type') == 'function_call_output' for item in replay) == 1
        if owner is not p:
            await owner.client.aclose()
    finally:
        await p.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['bytes', 'items', 'missing_encryption', 'incomplete', 'foreign_model', 'unknown_item', 'incomplete_item', 'invalid_phase'])
async def test_oversize_incomplete_and_nonportable_output_fails_closed(bad):
    first = copy.deepcopy(FIRST)
    if bad == 'bytes':
        first['output'][0]['encrypted_content'] = 'x' * (MAX_BYTES + 1)
    elif bad == 'items':
        first['output'] = [first['output'][0]] * (MAX_ITEMS + 1)
    elif bad == 'missing_encryption':
        first['output'][0].pop('encrypted_content')
    elif bad == 'incomplete':
        first['status'] = 'incomplete'
    elif bad == 'foreign_model':
        first['model'] = 'other-model'
    elif bad == 'unknown_item':
        first['output'].append({'type': 'unrecognized'})
    elif bad == 'incomplete_item':
        first['output'][1]['status'] = 'in_progress'
    elif bad == 'invalid_phase':
        first['output'][1]['phase'] = 'invented'
    wire = RoundWire(first)
    p = provider('gpt-5.4', wire)
    try:
        result = await p.chat(bind_messages([{'role': 'user', 'content': 'Read.'}], p, 'A'), max_tokens=128)
        history = attach_history(history_from(result), result, p, 'A')
        await p.chat(bind_messages([history], p, 'A'), max_tokens=128)
        assert not any(item.get('type') == 'reasoning' or 'phase' in item for item in wire.requests[-1][1]['input'])
    finally:
        await p.client.aclose()


@pytest.mark.asyncio
async def test_partial_stream_does_not_replay_or_create_trusted_continuation():
    requests = []

    class Partial(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"Reading."}\n\n'
            raise httpx.ReadError('synthetic interrupted stream')

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=Partial(), headers={'Content-Type': 'text/event-stream'})

    p = provider('gpt-5.4', RoundWire())
    await p.client.aclose()
    p.client = httpx.AsyncClient(base_url='https://example.test/v1', headers={'Authorization': 'Bearer mock-primary'}, transport=httpx.MockTransport(handle))
    try:
        events = [event async for event in p.chat_stream(bind_messages([{'role': 'user', 'content': 'Read.'}], p, 'A'))]
        assert [event['type'] for event in events] == ['text_delta', 'error']
        assert len(requests) == 1
        assert all(not hasattr(event, 'continuation') for event in events)
    finally:
        await p.client.aclose()


@pytest.mark.asyncio
async def test_selected_alias_can_replay_its_provider_resolved_dated_snapshot():
    first = copy.deepcopy(FIRST)
    first['model'] = 'gpt-5.4-2026-03-05'
    wire = RoundWire(first)
    p = provider('gpt-5.4', wire)
    try:
        result = await p.chat(bind_messages([{'role': 'user', 'content': 'Read.'}], p, 'A'), max_tokens=128)
        history = attach_history(history_from(result), result, p, 'A')
        await p.chat(bind_messages([history], p, 'A'), max_tokens=128)
        assert wire.requests[-1][1]['input'] == first['output']
        assert wire.requests[-1][1]['model'] == 'gpt-5.4'
    finally:
        await p.client.aclose()
