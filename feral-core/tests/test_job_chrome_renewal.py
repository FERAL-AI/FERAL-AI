"""Real phone goal, paired HTTP renewal, exact approval, selected CDP fake peer."""
import copy
import hashlib
import json

import httpx
import pytest
import pytest_asyncio

from api.state import state
from api.server import APIKeyMiddleware, FERAL_API_KEY
from security.approval_ingress import PairedDevicePrincipal
from security.device_pairing import DevicePairingStore
from api.existing_chrome_connection import local_owner_scope
from security.approval_ingress import review_terms_digest
from agents.taskflow import TaskFlowRuntime
from agents.tool_runner import ToolRunner
from security.exec_approvals import ApprovalManager
from security.trust_ledger import TrustLedger
from api.job_chrome_grants import JobChromeGrants
from api.routes import existing_chrome as chrome_routes
from tests.test_existing_chrome_connection import owner
from tests import test_job_chrome_grants as chrome
from tests.test_job_chrome_grants import HEADERS, grant, review, http, base, page_packets
from tests.test_phone_review_restart import phone_goal

pytestmark = [pytest.mark.asyncio, pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]
wired = chrome.wired
chrome_fixture = chrome.chrome_fixture


@pytest_asyncio.fixture
async def phone(wired, tmp_path, monkeypatch):
    store = DevicePairingStore(db_path=str(tmp_path / 'pair.db'))
    issued = store.pair_device('Synthetic phone', mint_phone_bearer=True)
    wired.principal = PairedDevicePrincipal(issued['device_id'], lambda: store.admitted_credential_current(
        device_id=issued['device_id'], credential=issued['phone_bearer'], bearer_kind='phone_bearer') and wired.live[0])
    monkeypatch.setattr(state, 'device_pairing_store', store)
    monkeypatch.setitem(HEADERS, 'Authorization', 'Bearer ' + FERAL_API_KEY)
    wired.app.add_middleware(APIKeyMiddleware)
    wired.phone_bearer = issued['phone_bearer']
    wired.pairing = store
    wired.flow_id = phone_goal(wired.runtime, wired.principal)
    return wired


async def propose(phone):
    flow = phone.runtime.get_flow(phone.flow_id)
    await grant(phone, flow, ['browser__press_key'])
    async def model(session, prompt, context=None):
        result = await phone.orch.tool_runner.execute_tool_call_for_llm(session,
            {'id': 'stable-phone-action', 'name': 'browser__press_key', 'args': {'key': 'Tab'}}, [], surface='http_api')
        assert result.get('status') == 'pending_approval', result
        return 'Waiting for exact phone approval'
    phone.orch._handle_command_impl = model
    await phone.runtime._run_flow(phone.flow_id)
    waiting = phone.runtime.get_flow(phone.flow_id)
    assert waiting['status'] == 'waiting', waiting
    return waiting['steps'][0]['result']['task_review']


def phone_http(phone):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=phone.app, client=('198.51.100.5', 23000)),
        base_url='http://fixture.invalid', headers={'Authorization': 'Bearer ' + phone.phone_bearer}, trust_env=False)


async def fresh_grant(phone, *, tools=None):
    old = next(iter(phone.manager._grants.values()))
    async with http(phone) as client:
        response = await client.post(f"/api/browser/existing/job-grants/{old['grant_id']}/revoke", json=base(phone))
        assert response.status_code == 200
    return await grant(phone, phone.runtime.get_flow(phone.flow_id), tools or ['browser__press_key'])


async def test_real_phone_goal_uses_existing_execution_sid_fallback(phone):
    flow = phone.runtime.get_flow(phone.flow_id)
    assert flow['session_id'] == ''
    receipt = await grant(phone, flow, ['browser__get_page_info'])
    assert receipt['execution_session_id'] == 'taskflow-' + phone.flow_id


async def test_new_mac_grant_then_phone_renew_rebinds_exact_card_without_dispatch(phone):
    old_card = await propose(phone)
    prior = copy.deepcopy(phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'])
    new_grant = await fresh_grant(phone)
    before = len(page_packets(phone))
    async with phone_http(phone) as client:
        renewed = await client.post('/api/approvals/' + old_card['request_id'] + '/renew',
            json={'session_id': 'origin-A', 'task_review': old_card})
    assert renewed.status_code == 200, renewed.text
    card = renewed.json()['task_review']
    assert card['request_id'] != old_card['request_id'] and card['terms_digest'] != old_card['terms_digest']
    after = phone.runtime.get_flow(phone.flow_id)['steps'][0]['result']
    terms = after['model_action']['terms']
    assert terms['browser_job_grant'] == new_grant
    assert {k: v for k, v in terms.items() if k != 'browser_job_grant'} == {
        k: v for k, v in prior['model_action']['terms'].items() if k != 'browser_job_grant'}
    digest = hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    assert after['model_action']['terms_digest'] == digest
    assert phone.runtime.get_flow(phone.flow_id)['status'] == 'waiting'
    assert len(page_packets(phone)) == before
    async with phone_http(phone) as client:
        old = await client.post('/api/approvals/' + old_card['request_id'] + '/approve',
            json={'session_id': 'origin-A', 'task_review': old_card})
        assert old.status_code != 200 or old.json().get('status') != 'approved'
        approved = await client.post('/api/approvals/' + card['request_id'] + '/approve',
            json={'session_id': 'origin-A', 'task_review': card})
        assert approved.status_code == 200 and approved.json()['result']['success'] is True, approved.text
        count = len(page_packets(phone))
        duplicate = await client.post('/api/approvals/' + card['request_id'] + '/approve',
            json={'session_id': 'origin-A', 'task_review': card})
        assert duplicate.status_code != 200 or duplicate.json().get('status') != 'approved'
    assert count > before and len(page_packets(phone)) == count
    async def continuation(session, prompt, context=None):
        result = await phone.orch.tool_runner.execute_tool_call_for_llm(session,
            {'id': 'stable-phone-action', 'name': 'browser__press_key', 'args': {'key': 'Tab'}}, [], surface='http_api')
        assert result.get('success') is True, result
        return 'Completed from the recorded approved receipt'
    phone.orch._handle_command_impl = continuation
    await phone.runtime._run_flow(phone.flow_id)
    assert phone.runtime.get_flow(phone.flow_id)['status'] == 'completed'
    assert len(page_packets(phone)) == count  # Recomputed immutable terms hit the real receipt cache.


async def test_mixed_inventory_omits_non_browser_steps_without_hiding_phone_goal(phone):
    other = phone.runtime.create_flow(session_id='other-job', title='Synthetic non-tool task',
        steps=[{'type': 'noop'}], handoff_key='c' * 64, terms_digest='d' * 64,
        origin_session_id='origin-A', origin_surface='http_api', source_principal=phone.principal,
        creation_guard=phone.principal.require_current)
    before = len(page_packets(phone))
    async with http(phone) as client:
        inventory = await client.post('/api/browser/existing/job-grants/tasks', json=base(phone))
        assert inventory.status_code == 200
        rows = inventory.json()['tasks']
        assert [row['flow_id'] for row in rows] == [phone.flow_id]
        assert rows[0]['execution_session_id'] == 'taskflow-' + phone.flow_id
        invalid = await client.post('/api/browser/existing/job-grants/review', json={**base(phone),
            'target_id': 'page-a', 'flow_id': other['id'], 'step_id': other['steps'][0]['id'],
            'flow_terms_digest': rows[0]['flow_terms_digest'], 'allowed_tools': ['browser__get_page_info'],
            'expires_in_seconds': 300})
        assert invalid.status_code == 409
    assert len(page_packets(phone)) == before and not phone.manager._reviews


async def test_fresh_scope_cannot_widen_old_action_and_narrow_regrant_can_recover(phone):
    card = await propose(phone)
    prior = copy.deepcopy(phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'])
    await fresh_grant(phone, tools=['browser__press_key', 'browser__navigate'])
    before = len(page_packets(phone))
    async with phone_http(phone) as client:
        refused = await client.post('/api/approvals/' + card['request_id'] + '/renew',
            json={'session_id': 'origin-A', 'task_review': card})
        assert refused.status_code == 409
    assert phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'] == prior
    assert len(page_packets(phone)) == before
    # A new explicit local review may choose the original narrow scope.
    await grant(phone, phone.runtime.get_flow(phone.flow_id), ['browser__press_key'])
    async with phone_http(phone) as client:
        recovered = await client.post('/api/approvals/' + card['request_id'] + '/renew',
            json={'session_id': 'origin-A', 'task_review': card})
    assert recovered.status_code == 200 and len(page_packets(phone)) == before


async def test_forged_pending_grant_cannot_substitute_current_scope_for_old_card(phone):
    card = await propose(phone)
    pending = copy.deepcopy(phone.orch.tool_runner.get_pending(card['request_id']))
    actual = await fresh_grant(phone)
    forged = {**pending, 'browser_job_grant': actual}
    assert phone.runtime._approval_step(forged) == (None, None)
    assert phone.orch.tool_runner.phone_review_descriptor(forged) is None
    assert review_terms_digest(pending, 'http_api') != review_terms_digest(forged, 'http_api')
    assert not [p for p in page_packets(phone) if p['method'] == 'Input.dispatchKeyEvent']


async def test_grant_revocation_before_final_atomic_commit_preserves_prior_card(phone, monkeypatch):
    card = await propose(phone)
    prior = copy.deepcopy(phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'])
    actual = await fresh_grant(phone)
    original = phone.runtime._bind_phone_review_checkpoint
    captured = {}
    def revoke_after_card_binding(runner, pending, **kwargs):
        captured['new_id'] = pending['request_id']
        assert phone.runtime._conn.in_transaction
        result = original(runner, pending, **kwargs)
        with local_owner_scope('owner-a'):
            phone.manager.revoke(actual['grant_id'], 'owner-a', actual['browser_resource']['connection_id'])
        return result
    monkeypatch.setattr(phone.runtime, '_bind_phone_review_checkpoint', revoke_after_card_binding)
    before = len(page_packets(phone))
    async with phone_http(phone) as client:
        refused = await client.post('/api/approvals/' + card['request_id'] + '/renew',
            json={'session_id': 'origin-A', 'task_review': card})
    assert refused.status_code == 409
    assert phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'] == prior
    assert phone.orch.tool_runner.get_pending(captured['new_id']) is None
    assert phone.runtime.list_device_review_checkpoints('origin-A', source_principal=phone.principal)[0]['task_review'] == card
    assert len(page_packets(phone)) == before


async def test_current_repeated_rest_admission_preserves_live_mac_grant(phone):
    card = await propose(phone)
    cached = phone.runtime._job_chrome_principals[phone.flow_id]
    before = len(page_packets(phone))
    async with phone_http(phone) as client:
        for _ in range(2):
            response = await client.post('/api/approvals/' + card['request_id'] + '/renew',
                json={'session_id': 'origin-A', 'task_review': card})
            assert response.status_code == 409  # The existing exact card remains current.
    assert phone.runtime._job_chrome_principals[phone.flow_id] is cached
    assert phone.orch.tool_runner.get_pending(card['request_id']) is not None
    assert len(page_packets(phone)) == before
    async with phone_http(phone) as client:
        approved = await client.post('/api/approvals/' + card['request_id'] + '/approve',
            json={'session_id': 'origin-A', 'task_review': card})
        assert approved.status_code == 200 and approved.json()['result']['success'] is True, approved.text


async def test_revoked_connection_cache_refresh_requires_explicit_mac_regrant(phone):
    card = await propose(phone)
    prior = copy.deepcopy(phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'])
    cached = phone.runtime._job_chrome_principals[phone.flow_id]
    phone.live[0] = False  # The cached old socket admission is revoked, bearer remains valid.
    before = len(page_packets(phone))
    async with phone_http(phone) as client:
        denied = await client.post('/api/approvals/' + card['request_id'] + '/renew',
            json={'session_id': 'origin-A', 'task_review': card})
        assert denied.status_code == 409
    assert phone.runtime._job_chrome_principals[phone.flow_id] is not cached
    assert phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'] == prior
    assert len(page_packets(phone)) == before
    await fresh_grant(phone)
    async with phone_http(phone) as client:
        renewed = await client.post('/api/approvals/' + card['request_id'] + '/renew',
            json={'session_id': 'origin-A', 'task_review': card})
    assert renewed.status_code == 200, renewed.text
    assert len(page_packets(phone)) == before


async def test_pruning_never_revives_old_grant_card_or_prepared_broader_review(phone):
    card = await propose(phone)
    old = next(iter(phone.manager._grants.values()))
    old_pending = copy.deepcopy(phone.orch.tool_runner.get_pending(card['request_id']))
    stale, _ = await review(phone, phone.runtime.get_flow(phone.flow_id),
        ['browser__press_key', 'browser__navigate'])
    replacement = await grant(phone, phone.runtime.get_flow(phone.flow_id), ['browser__press_key'])
    before = len(page_packets(phone))
    assert old['superseded'] is True
    assert phone.manager._reviews[stale['review_id']]['superseded'] is True
    assert phone.orch.tool_runner.approval_scope_for(old_pending) is None
    async with http(phone) as client:
        revoke = await client.post(f"/api/browser/existing/job-grants/{replacement['grant_id']}/revoke", json=base(phone))
        assert revoke.status_code == 200
    unrelated = phone.runtime.get_flow(phone_goal(phone.runtime, phone.principal))
    await grant(phone, unrelated, ['browser__get_page_info'])  # Prunes the newer revoked grant.
    async with http(phone) as client:
        status = await client.post(f"/api/browser/existing/job-grants/{old['grant_id']}/status", json=base(phone))
        assert status.status_code != 200 or status.json()['status'] != 'active'
        stale_confirm = await client.post(f"/api/browser/existing/job-grants/{stale['review_id']}/confirm",
            json={'session_id': 'owner-a', 'review_token': stale['review_token']})
        assert stale_confirm.status_code == 409
    assert phone.orch.tool_runner.approval_scope_for(old_pending) is None
    async with phone_http(phone) as client:
        old_approval = await client.post('/api/approvals/' + card['request_id'] + '/approve',
            json={'session_id': 'origin-A', 'task_review': card})
        assert old_approval.status_code != 200 or old_approval.json().get('status') != 'approved'
    assert len(page_packets(phone)) == before
    # Consent prepared after the revocation is a new explicit review, not reuse.
    await grant(phone, phone.runtime.get_flow(phone.flow_id), ['browser__press_key'])
    async with phone_http(phone) as client:
        renewed = await client.post('/api/approvals/' + card['request_id'] + '/renew',
            json={'session_id': 'origin-A', 'task_review': card})
    assert renewed.status_code == 200 and len(page_packets(phone)) == before


async def test_actual_reopen_requires_authenticated_cache_then_fresh_mac_scope_and_new_action_card(phone, monkeypatch):
    card = await propose(phone)
    old_resource = phone.connector.controller.approval_binding()
    old_runtime = phone.runtime
    await old_runtime.stop()
    reopened = TaskFlowRuntime(db_path=old_runtime._db_path, skill_registry=phone.orch.skills, orchestrator=phone.orch)
    reopened._recover_after_restart()
    monkeypatch.setattr(state, 'taskflows', reopened)
    phone.orch.taskflows = reopened
    phone.runtime = reopened
    phone.orch.tool_runner = ToolRunner(phone.orch, approval_manager=ApprovalManager(db_path=':memory:'))
    phone.orch.tool_runner._autonomy_mode = 'strict'
    phone.orch.tool_runner._trust = TrustLedger(persist=False)
    manager = JobChromeGrants(lambda: state, clock=lambda: phone.clock[0])
    monkeypatch.setattr(chrome_routes, 'job_chrome_grants', manager)
    phone.manager = manager
    phone.orch._job_chrome_grants = manager
    try:
        with owner():
            await phone.connector.select('page-b')
            await phone.connector.select('page-a')
        monkeypatch.setattr(state, 'browser', phone.connector.controller)
        current_resource = phone.connector.controller.approval_binding()
        assert current_resource['connection_id'] != old_resource['connection_id']
        assert current_resource['owner_session_id'] == old_resource['owner_session_id']
        assert current_resource['target_id'] == old_resource['target_id']
        prior = copy.deepcopy(reopened.get_flow(phone.flow_id)['steps'][0]['result'])
        before = len(page_packets(phone))
        assert not vars(reopened).get('_job_chrome_principals') and not manager._grants
        async with http(phone) as client:
            inventory = await client.post('/api/browser/existing/job-grants/tasks', json=base(phone))
            assert inventory.status_code == 200 and inventory.json()['tasks'] == []
        async with phone_http(phone) as client:
            initial = await client.post('/api/approvals/' + card['request_id'] + '/renew',
                json={'session_id': 'origin-A', 'task_review': card})
            assert initial.status_code == 409
        assert type(reopened._job_chrome_principals[phone.flow_id]) is PairedDevicePrincipal
        assert reopened.get_flow(phone.flow_id)['steps'][0]['result'] == prior
        assert not manager._grants and len(page_packets(phone)) == before
        receipt = await grant(phone, reopened.get_flow(phone.flow_id), ['browser__press_key'])
        assert receipt['browser_resource'] == current_resource
        async with phone_http(phone) as client:
            renewed = await client.post('/api/approvals/' + card['request_id'] + '/renew',
                json={'session_id': 'origin-A', 'task_review': card})
            assert renewed.status_code == 200, renewed.text
            new_card = renewed.json()['task_review']
            assert new_card['request_id'] != card['request_id']
            assert len(page_packets(phone)) == before
            terms = reopened.get_flow(phone.flow_id)['steps'][0]['result']['model_action']['terms']
            assert terms['resource'] == terms['browser_job_grant']['browser_resource'] == current_resource
            approved = await client.post('/api/approvals/' + new_card['request_id'] + '/approve',
                json={'session_id': 'origin-A', 'task_review': new_card})
            assert approved.status_code == 200 and approved.json()['result']['success'] is True, approved.text
        assert len(page_packets(phone)) > before
    finally:
        await reopened.stop()
        reopened._conn.close()


async def test_fresh_mac_grant_cannot_retarget_waiting_action_to_different_tab(phone, monkeypatch):
    card = await propose(phone)
    prior = copy.deepcopy(phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'])
    with owner():
        await phone.connector.select('page-b')
    monkeypatch.setattr(state, 'browser', phone.connector.controller)
    # Real local review explicitly targets B; the waiting action still names A.
    with local_owner_scope('owner-a'):
        row = phone.manager.tasks('owner-a', state.browser.approval_binding()['connection_id'])['tasks'][0]
        prepared = phone.manager.review(session_id='owner-a', connection_id=state.browser.approval_binding()['connection_id'],
            target_id='page-b', flow_id=phone.flow_id, step_id=row['step_id'], flow_terms_digest=row['flow_terms_digest'],
            allowed_tools=['browser__press_key'], expires_in_seconds=300)
        phone.manager.confirm(prepared['review_id'], prepared['review_token'])
    before = len(page_packets(phone))
    async with phone_http(phone) as client:
        refused = await client.post('/api/approvals/' + card['request_id'] + '/renew',
            json={'session_id': 'origin-A', 'task_review': card})
    assert refused.status_code == 409
    assert phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'] == prior
    assert len(page_packets(phone)) == before
