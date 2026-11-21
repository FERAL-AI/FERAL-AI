"""Focused reviewer faults at real private publication and renewal boundaries."""
import asyncio
import copy
import json
import inspect
import os
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.orchestrator import Orchestrator
from agents.taskflow import TaskFlowRuntime
from agents.tool_runner import ToolRunner
from security.exec_approvals import ApprovalManager
from api.state import BrainState
from security.approval_ingress import PairedDevicePrincipal
from security.device_pairing import DevicePairingStore
from tests.test_hup_protocol import _node_client, _register_node
from tests.test_phone_chat_responsive_intake import _state
from tests.test_phone_review_restart import phone_goal
from tests import test_taskflow_model_steps as model_steps

wired = model_steps.wired
ask = model_steps.ask

pytestmark = [pytest.mark.no_auto_feral_home, pytest.mark.timeout(20)]


def evidence(name, receipt):
    directory = os.environ.get('FERAL_COUNTERCHECK_EVIDENCE')
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')


def identity(tmp_path):
    store = DevicePairingStore(db_path=str(tmp_path / 'paired.db'))
    issued = store.pair_device('Inert countercheck phone', mint_phone_bearer=True)
    assert store.verify_phone_bearer(issued['phone_bearer']) == issued['device_id']
    source = PairedDevicePrincipal(issued['device_id'], lambda: store.admitted_credential_current(
        device_id=issued['device_id'], credential=issued['phone_bearer'], bearer_kind='phone_bearer'))
    return store, issued, source


@pytest.mark.parametrize('kind,replacement_kind', [('approval_request', 'orchestrator'),
    ('approval_resolved', 'orchestrator'), ('approval_resolved', 'taskflow'),
    ('approval_request', 'runner'), ('approval_resolved', 'runner')])
async def test_replaced_runtime_cannot_publish_private_card_to_new_registered_socket(wired, tmp_path, monkeypatch, kind, replacement_kind):
    rt, orch, effects = wired
    store, issued, source = identity(tmp_path)
    from api import state as state_module
    initial_state = state_module.state
    old_state = SimpleNamespace(orchestrator=orch, taskflows=rt, push_to_review_device=AsyncMock())
    monkeypatch.setattr(state_module, 'state', old_state)
    flow_id = phone_goal(rt, source)
    _, pending = await ask(rt, orch, flow_id)
    issuer_runner = orch.tool_runner
    descriptor = issuer_runner.phone_review_descriptor(pending, source_principal=source)
    assert descriptor is not None and effects == []
    replacement = _state()
    fresh_runtime = None
    if replacement_kind == 'taskflow':
        fresh_runtime = TaskFlowRuntime(db_path=str(tmp_path / 'replacement.db'), skill_registry=orch.skills, orchestrator=orch)
        replacement.orchestrator = orch
        replacement.taskflows = fresh_runtime
        orch.taskflows = fresh_runtime
    if replacement_kind == 'runner':
        replacement.orchestrator, replacement.taskflows = orch, rt
        orch.tool_runner = ToolRunner(orch, approval_manager=ApprovalManager(db_path=':memory:'))
    replacement.push_to_review_device = MethodType(BrainState.push_to_review_device, replacement)
    replacement._queue_dict_to_node = MethodType(BrainState._queue_dict_to_node, replacement)
    # The old runtime remains alive with its issued review. A replacement state
    # authenticates the same device, which is legitimate recipient authority.
    # The helper reloads the registered server before installing its mock;
    # restore the isolated import state for that reload, not the old minimal rig.
    monkeypatch.setattr(state_module, 'state', initial_state)
    with _node_client(replacement) as app:
        replacement.device_pairing_store = store
        with app.websocket_connect('/v1/node', headers={'Authorization': 'Bearer ' + issued['phone_bearer']}) as ws:
            _register_node(ws, 'same-device-new-socket', 'phone')
            peer = replacement.daemons['same-device-new-socket']
            peer.send_json = AsyncMock(wraps=peer.send_json)
            assert replacement.orchestrator is not orch or replacement.taskflows is not rt or orch.tool_runner is not issuer_runner
            if kind == 'approval_request':
                await issuer_runner._notify_user_of_pending_approval(pending['session_id'], pending['tool_name'], pending)
            else:
                # Exercise the same captured-runtime proof once the repair adds
                # it; the baseline publisher has no parameter for that proof.
                parameters = inspect.signature(Orchestrator._push_approval_resolved).parameters
                options = {'phone_review_runtime': rt} if 'phone_review_runtime' in parameters else {}
                if 'phone_review_runner' in parameters:
                    options['phone_review_runner'] = issuer_runner
                await Orchestrator._push_approval_resolved(orch, pending['session_id'], pending['request_id'],
                    'approved', pending['tool_name'], 'api', phone_review=descriptor, private_review=True, **options)
            delivered = [call.args[0] for call in peer.send_json.await_args_list if call.args[0]['type'] == kind]
            assert effects == []
            evidence(kind + '-' + replacement_kind, {'issuer_orchestrator_is_current': replacement.orchestrator is orch,
                'issuer_taskflow_is_current': replacement.taskflows is rt,
                'issuer_runner_is_current': replacement.orchestrator is orch and orch.tool_runner is issuer_runner,
                'stale_private_frames_delivered': len(delivered), 'inert_effect_count': len(effects)})
            try:
                assert delivered == [], 'Old runtime published a private review to the replacement runtime socket'
            finally:
                orch.tool_runner = issuer_runner
                if fresh_runtime is not None:
                    orch.taskflows = rt
                    await fresh_runtime.stop()
                    fresh_runtime._conn.close()


async def test_revocation_between_renewal_commits_preserves_prior_discoverable_checkpoint(wired, tmp_path, monkeypatch):
    rt, orch, effects = wired
    store, issued, source = identity(tmp_path)
    monkeypatch.setattr('api.state.state', SimpleNamespace(orchestrator=orch, taskflows=rt, push_to_review_device=AsyncMock()))
    flow_id = phone_goal(rt, source)
    _, pending = await ask(rt, orch, flow_id)
    prior = copy.deepcopy(rt.get_flow(flow_id)['steps'][0]['result'])
    old_card = prior['task_review']
    orch.tool_runner.deny_pending(pending['request_id'], session_id=pending['session_id'])
    original_bind = rt._bind_phone_review_checkpoint
    fault = {}
    def revoke_before_public_card_binding(runner, renewed_pending, *, source_principal=None, **binding_options):
        fault['transaction_active'] = rt._conn.in_transaction
        fault['renewed_id'] = renewed_pending['request_id']
        fault['checkpoint_id_before_binding'] = rt.get_flow(flow_id)['steps'][0]['result']['approval']['request_id']
        fault['credential'] = store.rotate_phone_bearer(issued['device_id'])
        return original_bind(runner, renewed_pending, source_principal=source_principal, **binding_options)
    monkeypatch.setattr(rt, '_bind_phone_review_checkpoint', revoke_before_public_card_binding)
    with pytest.raises(asyncio.CancelledError):
        await rt.renew_device_model_review(flow_id, origin_session_id='origin-A', task_review=old_card, source_principal=source)
    assert fault['renewed_id'] != old_card['request_id']
    assert effects == [] and orch.tool_runner.list_pending() == []
    rotated = fault['credential']
    assert store.verify_phone_bearer(rotated['phone_bearer']) == issued['device_id']
    fresh = PairedDevicePrincipal(issued['device_id'], lambda: store.admitted_credential_current(
        device_id=issued['device_id'], credential=rotated['phone_bearer'], bearer_kind='phone_bearer'))
    stored = rt.get_flow(flow_id)['steps'][0]['result']
    rows = rt.list_device_review_checkpoints('origin-A', source_principal=fresh)
    evidence('renewal', {'transaction_active_before_card_binding': fault['transaction_active'],
        'approval_id_changed': stored['approval']['request_id'] != old_card['request_id'],
        'public_card_id_changed': stored['task_review']['request_id'] != old_card['request_id'],
        'approval_matches_public_card': stored['approval']['request_id'] == stored['task_review']['request_id'],
        'prior_checkpoint_retained': stored == prior, 'same_device_discovery_count': len(rows),
        'pending_review_count': len(orch.tool_runner.list_pending()), 'inert_effect_count': len(effects)})
    assert stored == prior and len(rows) == 1 and rows[0]['task_review'] == old_card, (
        'Interrupted renewal stranded checkpoint: durable approval changed, public card remains old, '
        'no pending review and no same-device renewal discovery')
