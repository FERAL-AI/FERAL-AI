"""Actual voice/selected-Chrome dispatch; inert CDP peer, disposable policy."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from api.state import state
from tests.test_existing_chrome_approval_integration import registered_chrome as _registered_fixture
from tests.test_existing_chrome_connection import fixture as _chrome_fixture, owner
from voice.gemini_realtime import GeminiRealtimeProxy
from voice.realtime_proxy import RealtimeProxy

chrome_fixture = _chrome_fixture
registered_chrome = _registered_fixture


def proxy_for(orch, provider='openai'):
    cls = RealtimeProxy if provider == 'openai' else GeminiRealtimeProxy
    proxy = cls.__new__(cls)
    proxy._sessions = {}
    proxy._skill_registry = orch.skills
    proxy._skill_executor = orch.executor
    proxy._orchestrator = orch
    proxy._memory = None
    proxy._send_tool_feedback = AsyncMock()
    proxy._record_voice_tool_episode = AsyncMock()
    orch._emit_tool_start = AsyncMock()
    orch._emit_tool_result = AsyncMock()
    return proxy


async def invoke(orch, provider='openai', session='owner-a', tool='browser__get_page_info', args=None):
    proxy = proxy_for(orch, provider)
    raw = await proxy._handle_tool_call(session, 'read-fixture', tool, json.dumps(args or {}))
    return json.loads(raw)


@pytest.mark.parametrize('provider', ['openai', 'gemini'])
async def test_actual_voice_read_carries_same_owner_scope(registered_chrome, provider):
    _client, orch, connector, socket = registered_chrome
    before = len(socket.sent)
    result = await invoke(orch, provider)
    assert connector.controller.connected
    assert result['success'] is True
    packets = socket.sent[before:]
    assert packets and all(p['method'] not in {'Input.dispatchMouseEvent', 'Input.insertText'} for p in packets)


@pytest.mark.parametrize('provider', ['openai', 'gemini'])
async def test_wrong_owner_refused_before_any_selected_command(registered_chrome, provider):
    _client, orch, connector, socket = registered_chrome
    before = len(socket.sent)
    result = await invoke(orch, provider, session='foreign')
    assert result['success'] is False
    assert result['error_code'] == 'browser_resource_changed'
    assert result['outcome_unknown'] is False
    assert 'owner-a' not in json.dumps(result)
    assert len(socket.sent) == before and connector.controller.connected


@pytest.mark.parametrize('provider', ['openai', 'gemini'])
async def test_actual_cdp_reselection_during_read_cannot_publish_stale_success(registered_chrome, provider):
    _client, orch, connector, socket = registered_chrome
    socket.held_method = 'Runtime.evaluate'
    task = asyncio.create_task(invoke(orch, provider))
    await asyncio.wait_for(socket.arrived.wait(), 2)
    held = list(socket.held)
    with owner():
        await connector.select('page-b')
    state.browser = connector.controller
    for packet in held:
        socket.reply(packet, result={'result': {'value': '{"url":"about:blank","title":"old"}'}})
    result = await asyncio.wait_for(task, 2)
    assert result['success'] is False
    assert result['error_code'] == 'browser_resource_changed'
    assert result['outcome_unknown'] is True
    assert result['data'] is None
    assert connector.controller.approval_binding()['target_id'] == 'page-b'
    assert not [p for p in socket.sent if p['method'].startswith('Input.')]


@pytest.mark.parametrize('provider', ['openai', 'gemini'])
async def test_same_orchestrator_runner_replacement_discards_read_result(registered_chrome, provider, monkeypatch):
    _client, orch, _connector, socket = registered_chrome
    socket.held_method = 'Runtime.evaluate'
    task = asyncio.create_task(invoke(orch, provider))
    await asyncio.wait_for(socket.arrived.wait(), 2)
    monkeypatch.setattr(orch, 'tool_runner', object())
    for packet in socket.held:
        socket.reply(packet, result={'result': {'value': '{"url":"about:blank"}'}})
    result = await asyncio.wait_for(task, 2)
    assert result['success'] is False and result['outcome_unknown'] is True
    assert result['error_code'] == 'browser_resource_changed' and result['data'] is None
    assert not [p for p in socket.sent if p['method'].startswith('Input.')]


@pytest.mark.parametrize('provider', ['openai', 'gemini'])
async def test_scope_does_not_grant_voice_mutation_permission(registered_chrome, provider):
    client, orch, connector, socket = registered_chrome
    before = len(socket.sent)
    result = await invoke(orch, provider, tool='browser__click_at', args={'x': 17, 'y': 23})
    assert result['status'] == 'pending_approval'
    assert result['browser_resource'] == connector.controller.approval_binding()
    assert result['session_id'] == 'owner-a'
    assert len(socket.sent) == before
    path = f"/api/approvals/{result['request_id']}/approve"
    approved = await client.post(path, json={'session_id': 'owner-a'})
    assert approved.status_code == 200 and approved.json()['result']['success'] is True
    clicks = [p for p in socket.sent[before:] if p['method'] == 'Input.dispatchMouseEvent']
    assert len(clicks) == 2
    assert all(p['sessionId'] == 'session-page-a' for p in clicks)
    assert (await client.post(path, json={'session_id': 'owner-a'})).status_code == 404
    assert len([p for p in socket.sent[before:] if p['method'] == 'Input.dispatchMouseEvent']) == 2
