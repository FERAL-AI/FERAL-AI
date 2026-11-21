"""Authenticated HUP reopen restores private waiting-source observations only."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from agents.taskflow import TaskFlowRuntime
from agents.tool_runner import ToolRunner
from memory.store import MemoryStore
from security.exec_approvals import ApprovalManager
from tests.test_hup_protocol import _node_client, _register_node, _TEST_NODE_KEY
from tests.test_phone_chat_responsive_intake import _state
from tests import test_job_chrome_renewal as renewal
from tests.test_job_chrome_grants import page_packets

phone, wired, chrome_fixture = renewal.phone, renewal.wired, renewal.chrome_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]


async def first_use_wait(phone):
    packets = list(page_packets(phone))
    calls = []
    async def model(session, prompt, context=None):
        result = await phone.orch.tool_runner.execute_tool_call_for_llm(session,
            {'id': 'first-phone-browser-action', 'name': 'browser__get_page_info', 'args': {}}, [], surface='http_api')
        calls.append(result)
        return 'Waiting for explicit Mac browser permission'
    phone.orch._handle_command_impl = model
    await phone.runtime._run_flow(phone.flow_id)
    flow = phone.runtime.get_flow(phone.flow_id)
    assert flow['status'] == 'waiting', (flow, calls)
    assert flow['steps'][0]['status'] == 'waiting'
    assert 'task_review' not in flow['steps'][0]['result']
    assert page_packets(phone) == packets
    assert not phone.manager._grants and not phone.manager._reviews
    return flow


async def reopened(phone, tmp_path):
    await first_use_wait(phone)
    await phone.runtime.stop()
    runtime = TaskFlowRuntime(db_path=str(tmp_path / 'jobs.db'), skill_registry=phone.orch.skills, orchestrator=phone.orch)
    runtime._supervisor = phone.runtime._supervisor
    phone.orch.taskflows = runtime
    phone.orch.tool_runner = ToolRunner(phone.orch, approval_manager=ApprovalManager())
    runtime._recover_after_restart()
    assert not vars(runtime).get('_job_chrome_principals', {})
    brain = _state()
    brain.orchestrator, brain.taskflows = phone.orch, runtime
    brain.browser, brain.skill_registry = phone.connector.controller, phone.orch.skills
    brain._native_agent_turn_generation = 0
    brain.memory, brain.chat_turns = MemoryStore(db_path=str(tmp_path / 'handshake-receipts.db')), None
    phone.orch.handle_command = AsyncMock(side_effect=AssertionError('registration cannot dispatch a model'))
    return runtime, brain


def snapshot(runtime):
    return (tuple(runtime._conn.iterdump()), runtime._conn.total_changes)


def assert_inert(phone, runtime, before, packets):
    assert snapshot(runtime) == before
    assert runtime.get_flow(phone.flow_id)['status'] == 'waiting'
    assert phone.orch.tool_runner.list_pending() == []
    phone.orch.handle_command.assert_not_awaited()
    assert page_packets(phone) == packets
    assert not phone.manager._grants and not phone.manager._reviews


async def test_verified_phone_handshake_after_restart_and_reopen_refreshes_first_use_source(phone, tmp_path):
    runtime, brain = await reopened(phone, tmp_path)
    before, packets = snapshot(runtime), page_packets(phone)
    try:
        with _node_client(brain) as client:
            brain.device_pairing_store = phone.pairing
            first = None
            for _ in range(2):
                with client.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + phone.phone_bearer}) as ws:
                    assert _register_node(ws, 'phone', 'phone')['type'] == 'node_ack'
                    retained = runtime._job_chrome_principals[phone.flow_id]
                    principal = brain.daemons['phone']._feral_phone_chat_intake._review_principal
                    assert retained is principal
                    assert retained.storage_binding() == phone.principal.storage_binding()
                    retained.require_current()
                    if first is not None:
                        assert retained is not first
                    first = retained
                    assert_inert(phone, runtime, before, packets)
                with pytest.raises(asyncio.CancelledError):
                    first.require_current()
    finally:
        await brain.memory.aclose()
        await runtime.stop()
        runtime._conn.close()


@pytest.mark.parametrize('credential_kind', ['foreign', 'legacy', 'sensor', 'invalid', 'revoked'])
async def test_nonmatching_or_unverified_handshake_cannot_refresh_waiting_source(phone, tmp_path, credential_kind):
    runtime, brain = await reopened(phone, tmp_path)
    before, packets = snapshot(runtime), page_packets(phone)
    token = phone.phone_bearer
    if credential_kind == 'foreign':
        token = phone.pairing.pair_device('Foreign synthetic phone', kind='browser_node_v2')['phone_bearer']
    elif credential_kind == 'legacy':
        token = _TEST_NODE_KEY
    elif credential_kind == 'invalid':
        token = 'invalid-synthetic-credential'
    elif credential_kind == 'revoked':
        phone.pairing.rotate_phone_bearer(phone.principal.device_id)
    try:
        with _node_client(brain) as client:
            brain.device_pairing_store = phone.pairing
            with client.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + token}) as ws:
                ack = _register_node(ws, 'candidate', 'sensor' if credential_kind == 'sensor' else 'phone')
                assert ack['type'] == ('error' if credential_kind in {'invalid', 'revoked'} else 'node_ack')
                assert not vars(runtime).get('_job_chrome_principals', {})
                assert_inert(phone, runtime, before, packets)
    finally:
        await brain.memory.aclose()
        await runtime.stop()
        runtime._conn.close()


@pytest.mark.parametrize('phase', ['before', 'after', 'during_environment'])
async def test_handshake_checks_source_again_when_callback_changes(phone, tmp_path, monkeypatch, phase):
    from agents import taskflow_browser_consent
    from starlette.websockets import WebSocketDisconnect
    runtime, brain = await reopened(phone, tmp_path)
    before, packets = snapshot(runtime), page_packets(phone)
    real = taskflow_browser_consent.refresh_waiting_task_sources
    calls = []
    def refresh_and_revoke(runtime_arg, principal, **kwargs):
        calls.append(principal)
        result = real(runtime_arg, principal, **kwargs)
        phone.pairing.rotate_phone_bearer(phone.principal.device_id)
        return result
    if phase == 'before':
        async def revoke_during_connect(*args, **kwargs):
            phone.pairing.rotate_phone_bearer(phone.principal.device_id)
        brain.hardware_mesh.on_node_connected = AsyncMock(side_effect=revoke_during_connect)
    elif phase == 'after':
        monkeypatch.setattr(taskflow_browser_consent, 'refresh_waiting_task_sources', refresh_and_revoke)
    else:
        remember = taskflow_browser_consent.remember_task_principal
        def remember_and_change_environment(runtime_arg, flow_id, principal):
            calls.append(principal)
            result = remember(runtime_arg, flow_id, principal)
            brain.taskflows = object()
            return result
        monkeypatch.setattr(taskflow_browser_consent, 'remember_task_principal', remember_and_change_environment)
    try:
        with _node_client(brain) as client:
            brain.device_pairing_store = phone.pairing
            with client.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + phone.phone_bearer}) as ws:
                with pytest.raises(WebSocketDisconnect):
                    _register_node(ws, 'phone', 'phone')
                assert len(calls) == (0 if phase == 'before' else 1)
                if phase == 'during_environment':
                    assert brain.taskflows is not runtime
                else:
                    for retained in vars(runtime).get('_job_chrome_principals', {}).values():
                        with pytest.raises(asyncio.CancelledError):
                            retained.require_current()
                assert_inert(phone, runtime, before, packets)
    finally:
        await brain.memory.aclose()
        await runtime.stop()
        runtime._conn.close()


async def test_verified_phone_without_taskflow_runtime_keeps_normal_ack(tmp_path, monkeypatch):
    from agents import taskflow_browser_consent
    from security.device_pairing import DevicePairingStore
    brain = _state()
    brain.taskflows = brain.orchestrator.taskflows = None
    brain.memory, brain.chat_turns = MemoryStore(db_path=str(tmp_path / 'normal-phone-receipts.db')), None
    store = DevicePairingStore(db_path=str(tmp_path / 'normal-phone-pair.db'))
    issued = store.pair_device('Synthetic phone without tasks', mint_phone_bearer=True)
    real = taskflow_browser_consent.refresh_waiting_task_sources
    calls = []
    def observe(runtime_arg, principal, **kwargs):
        result = real(runtime_arg, principal, **kwargs)
        calls.append((runtime_arg, principal.storage_binding(), result))
        return result
    monkeypatch.setattr(taskflow_browser_consent, 'refresh_waiting_task_sources', observe)
    try:
        with _node_client(brain) as client:
            brain.device_pairing_store = store
            with client.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + issued['phone_bearer']}) as ws:
                ack = _register_node(ws, 'phone', 'phone')
                assert ack['type'] == 'node_ack'
                assert len(calls) == 1 and calls[0][0] is None and calls[0][2] == 0
                assert brain.taskflows is None and brain.orchestrator.taskflows is None
                brain.orchestrator.handle_command.assert_not_awaited()
    finally:
        await brain.memory.aclose()
