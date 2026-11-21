"""Actual new phone job and selected CDP path; fake provider and wire only."""
import asyncio
import copy

import pytest

from agents.taskflow import TaskFlowRuntime
from agents.taskflow_browser_consent import REASON, refresh_waiting_task_sources, structural_checkpoint
from api.routes.taskflows import router as task_router
from tests import test_job_chrome_renewal as renewal
from tests.test_job_chrome_grants import base, create, grant, http, page_packets
from tests.test_job_chrome_renewal import phone_http

pytestmark = [pytest.mark.asyncio, pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]
wired = renewal.wired
chrome_fixture = renewal.chrome_fixture
phone = renewal.phone


async def pause_phone_job(phone, *, tool='browser__press_key', args=None):
    counts = {'provider': 0, 'after': 0}
    async def model(session, prompt, context=None):
        counts['provider'] += 1
        await phone.orch.tool_runner.execute_tool_call_for_llm(session,
            {'id': 'saved-phone-call', 'name': tool, 'args': args if args is not None else {'key': 'Tab'}}, [], surface='http_api')
        counts['after'] += 1
        return 'Not reached before explicit consent'
    phone.orch._handle_command_impl = model
    before = len(page_packets(phone))
    await phone.runtime._run_flow(phone.flow_id)
    flow = phone.runtime.get_flow(phone.flow_id)
    assert flow['status'] == 'waiting', repr(flow)
    result = flow['steps'][0]['result']
    assert result['reason'] == REASON and structural_checkpoint(result) is not None
    assert 'task_review' not in result and not phone.orch.tool_runner._pending_approvals
    assert counts == {'provider': 1, 'after': 0}
    assert len(page_packets(phone)) == before
    return counts, copy.deepcopy(result)


async def resume_http(phone):
    if not getattr(phone, 'task_router_added', False):
        phone.app.include_router(task_router)
        phone.task_router_added = True
    async with http(phone) as client:
        response = await client.post('/api/taskflows/' + phone.flow_id + '/resume')
    assert response.status_code == 200, response.text
    return response.json()


async def test_new_phone_job_mac_consent_then_explicit_resume_exact_review(phone):
    counts, saved = await pause_phone_job(phone)
    before = len(page_packets(phone))
    await grant(phone, phone.runtime.get_flow(phone.flow_id), ['browser__press_key'])
    assert phone.runtime.get_flow(phone.flow_id)['status'] == 'waiting'
    assert len(page_packets(phone)) == before and counts['provider'] == 1
    queued = await resume_http(phone)
    assert queued['resource_resume']['status'] == 'queued', queued
    assert len(page_packets(phone)) == before
    phone.orch.tool_runner._autonomy_mode = 'loose'
    await phone.runtime._run_flow(phone.flow_id)
    waiting = phone.runtime.get_flow(phone.flow_id)
    assert waiting['status'] == 'waiting', waiting
    result = waiting['steps'][0]['result']
    assert result['reason'] == 'approval_required' and result['model_actions'] == saved['model_actions']
    assert result['model_action']['terms']['call_id'] == saved['pending_call']['call']['id']
    assert result['model_action']['terms']['args'] == saved['pending_call']['call']['args']
    assert counts['provider'] == 1 and len(page_packets(phone)) == before
    card = result['task_review']
    async with phone_http(phone) as client:
        approved = await client.post('/api/approvals/' + card['request_id'] + '/approve',
            json={'session_id': 'origin-A', 'task_review': card})
        assert approved.status_code == 200 and approved.json()['result']['success'] is True, approved.text
        packets = len(page_packets(phone))
        duplicate = await client.post('/api/approvals/' + card['request_id'] + '/approve',
            json={'session_id': 'origin-A', 'task_review': card})
        assert duplicate.status_code != 200 or duplicate.json().get('status') != 'approved'
    assert packets > before and len(page_packets(phone)) == packets and counts['provider'] == 1


async def test_saved_read_runs_once_without_provider_recomputation(phone):
    counts, saved = await pause_phone_job(phone, tool='browser__get_page_info', args={})
    await grant(phone, phone.runtime.get_flow(phone.flow_id), ['browser__get_page_info'])
    assert (await resume_http(phone))['resource_resume']['status'] == 'queued'
    await phone.runtime._run_flow(phone.flow_id)
    flow = phone.runtime.get_flow(phone.flow_id)
    assert flow['status'] == 'queued', flow
    result = flow['steps'][0]['result']
    assert result['reason'] == 'model_continuation'
    assert result['model_actions']['saved-phone-call']['result']['success'] is True
    assert counts['provider'] == 1 and saved['pending_call_dispatch_started'] is False


@pytest.mark.parametrize('drift', ['revoke', 'credential', 'cancel', 'step', 'target', 'runtime'])
async def test_explicit_resume_refuses_drift_without_packets(phone, drift):
    _, saved = await pause_phone_job(phone)
    receipt = await grant(phone, phone.runtime.get_flow(phone.flow_id), ['browser__press_key'])
    before = len(page_packets(phone))
    if drift == 'revoke':
        async with http(phone) as client:
            assert (await client.post('/api/browser/existing/job-grants/' + receipt['grant_id'] + '/revoke', json=base(phone))).status_code == 200
    elif drift == 'credential':
        phone.live[0] = False
    elif drift == 'cancel':
        phone.runtime.cancel_flow(phone.flow_id)
    elif drift == 'step':
        with phone.runtime._lock:
            phone.runtime._conn.execute('UPDATE taskflows SET current_step=1 WHERE id=?', (phone.flow_id,))
            phone.runtime._conn.commit()
    elif drift == 'target':
        from tests.test_existing_chrome_connection import owner
        with owner():
            await phone.connector.select('page-b')
        before = len(page_packets(phone))
    else:
        phone.runtime._stop_event.set()
    returned = phone.runtime.resume_flow(phone.flow_id)
    assert returned['status'] != 'queued', returned
    assert len(page_packets(phone)) == before
    assert phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'] == saved


async def test_grant_revoked_after_queue_restores_known_no_effect_wait(phone):
    counts, saved = await pause_phone_job(phone)
    receipt = await grant(phone, phone.runtime.get_flow(phone.flow_id), ['browser__press_key'])
    assert (await resume_http(phone))['status'] == 'queued'
    async with http(phone) as client:
        await client.post('/api/browser/existing/job-grants/' + receipt['grant_id'] + '/revoke', json=base(phone))
    before = len(page_packets(phone))
    await phone.runtime._run_flow(phone.flow_id)
    final = phone.runtime.get_flow(phone.flow_id)
    assert final['status'] == 'waiting', final
    assert final['steps'][0]['result'] == saved and counts['provider'] == 1
    assert len(page_packets(phone)) == before


async def test_direct_skill_missing_grant_waits_and_resume_read_completes(phone):
    flow = create(phone)
    await phone.runtime._run_flow(flow['id'])
    waiting = phone.runtime.get_flow(flow['id'])
    assert waiting['status'] == 'waiting' and waiting['steps'][0]['result']['reason'] == REASON, waiting
    await grant(phone, waiting)
    assert phone.runtime.resume_flow(flow['id'])['status'] == 'queued'
    await phone.runtime._run_flow(flow['id'])
    assert phone.runtime.get_flow(flow['id'])['status'] == 'completed'


async def test_refresh_is_bounded_observational_and_none_runtime_safe(phone):
    _, saved = await pause_phone_job(phone)
    assert await asyncio.to_thread(refresh_waiting_task_sources, None, phone.principal) == 0
    phone.runtime._job_chrome_principals.clear()
    before = len(page_packets(phone))
    assert await asyncio.to_thread(refresh_waiting_task_sources, phone.runtime, phone.principal) == 1
    assert phone.runtime.get_flow(phone.flow_id)['steps'][0]['result'] == saved
    assert len(page_packets(phone)) == before and not phone.manager._grants


async def test_restart_loses_resume_ticket_and_never_dispatches_without_fresh_consent(phone, monkeypatch):
    _, saved = await pause_phone_job(phone)
    await grant(phone, phone.runtime.get_flow(phone.flow_id), ['browser__press_key'])
    assert phone.runtime.resume_flow(phone.flow_id)['status'] == 'queued'
    prior = phone.runtime
    reopened = TaskFlowRuntime(db_path=prior._db_path, skill_registry=prior._skill_registry, orchestrator=phone.orch)
    reopened._recover_after_restart()
    from api.state import state
    monkeypatch.setattr(state, 'taskflows', reopened)
    monkeypatch.setattr(phone.orch, 'taskflows', reopened)
    try:
        flow = reopened.get_flow(phone.flow_id)
        assert flow['status'] == 'waiting' and flow['steps'][0]['result'] == saved
        assert refresh_waiting_task_sources(reopened, phone.principal) == 1
        before = len(page_packets(phone))
        assert reopened.resume_flow(phone.flow_id)['resource_resume']['status'] == 'refused'
        assert len(page_packets(phone)) == before
    finally:
        await reopened._http.aclose()
        reopened._conn.close()


async def test_prior_real_runner_receipt_retained_and_never_replayed(phone, monkeypatch):
    from models.skill_manifest import BrandProfile, EndpointParam, SkillEndpoint, SkillManifest
    from agents.taskflow_browser_consent import BrowserConsentWait
    from agents.taskflow import model_step_for
    registry = phone.runtime._skill_registry
    registry.register(SkillManifest(skill_id="notes_memory", brand=BrandProfile(name="Fixture"), description="Fixture",
        endpoints=[SkillEndpoint(id="search_notes", method="PYTHON", url="", description="fixture", safety_tier="safe",
            read_only_hint=True, params=[EndpointParam(name="value", type="string", required=True)])]))
    original = phone.orch.executor.execute
    effects = []
    async def execute(**kwargs):
        if kwargs["tool_name"] == "notes_memory__search_notes":
            effects.append(kwargs)
            return {"success": True, "data": {"nonce": "previous-result"}, "tool_outcome_verified": True}
        return await original(**kwargs)
    monkeypatch.setattr(phone.orch.executor, 'execute', execute)
    captured, providers = {}, []
    async def model(session, prompt, context=None):
        providers.append(prompt)
        result = await phone.orch.tool_runner.execute_tool_call_for_llm(session,
            {'id': 'prior-local-call', 'name': 'notes_memory__search_notes', 'args': {'value': 'fixture'}}, [], surface='http_api')
        assert result['success'] is True, result
        captured['actions'] = copy.deepcopy(model_step_for(session, phone.orch).actions)
        try:
            await phone.orch.tool_runner.execute_tool_call_for_llm(session,
                {'id': 'first-browser', 'name': 'browser__press_key', 'args': {'key': 'Tab'}}, [], surface='http_api')
        except BrowserConsentWait:
            try:
                await phone.orch.tool_runner.execute_tool_call_for_llm(session,
                    {'id': 'late-browser', 'name': 'browser__navigate', 'args': {'url': 'https://example.invalid'}}, [], surface='http_api')
            except BrowserConsentWait:
                pass
        return 'Provider swallowed cancellation; durable checkpoint still wins'
    phone.orch._handle_command_impl = model
    await phone.runtime._run_flow(phone.flow_id)
    waiting = phone.runtime.get_flow(phone.flow_id)
    saved = waiting['steps'][0]['result']
    assert waiting['status'] == 'waiting' and saved['pending_call']['call']['id'] == 'first-browser'
    assert saved['model_actions'] == captured['actions'] and len(effects) == 1
    await grant(phone, waiting, ['browser__press_key'])
    assert phone.runtime.resume_flow(phone.flow_id)['status'] == 'queued'
    await phone.runtime._run_flow(phone.flow_id)
    after = phone.runtime.get_flow(phone.flow_id)['steps'][0]['result']
    assert after['reason'] == 'approval_required' and after['model_actions'] == captured['actions']
    assert len(effects) == len(providers) == 1


async def test_direct_skill_saved_mutation_requires_exact_review(phone):
    flow = create(phone, endpoint='press_key', args={'key': 'Tab'})
    await phone.runtime._run_flow(flow['id'])
    waiting = phone.runtime.get_flow(flow['id'])
    assert waiting['status'] == 'waiting' and waiting['steps'][0]['result']['reason'] == REASON
    await grant(phone, waiting, ['browser__press_key'])
    phone.orch.tool_runner._autonomy_mode = 'loose'
    assert phone.runtime.resume_flow(flow['id'])['status'] == 'queued'
    before = len(page_packets(phone))
    await phone.runtime._run_flow(flow['id'])
    result = phone.runtime.get_flow(flow['id'])['steps'][0]['result']
    assert result['reason'] == 'approval_required', result
    assert len(page_packets(phone)) == before
    card = result['task_review']
    async with phone_http(phone) as client:
        approved = await client.post('/api/approvals/' + card['request_id'] + '/approve',
            json={'session_id': 'phone-shared', 'task_review': card})
    assert approved.status_code == 200 and approved.json()['result']['success'] is True, approved.text
    assert len(page_packets(phone)) > before


async def test_old_revoked_read_failure_cannot_become_first_use_wait_after_pruning(phone):
    flow = create(phone)
    receipt = await grant(phone, flow)
    async with http(phone) as client:
        await client.post('/api/browser/existing/job-grants/' + receipt['grant_id'] + '/revoke', json=base(phone))
    await phone.runtime._run_flow(flow['id'])
    failed = phone.runtime.get_flow(flow['id'])
    assert failed['status'] == 'failed', failed
    phone.manager._grants.clear()  # Same proof after bounded expired-entry pruning.
    phone.runtime.resume_flow(flow['id'])
    before = len(page_packets(phone))
    await phone.runtime._run_flow(flow['id'])
    final = phone.runtime.get_flow(flow['id'])
    assert final['status'] == 'failed', final
    assert final['steps'][0]['result'].get('reason') != REASON
    assert len(page_packets(phone)) == before
