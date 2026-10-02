"""Real adapter/HTTP/ASGI hook integration with disposable injected services."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest
from fastapi import FastAPI
from security.agent_bootstrap_lifecycle import create_agent_bootstrap_controller
from security.agent_bootstrap_fence import AgentBootstrapFence
from security.vault_coordinator import VaultCoordinator
from api.routes.agent_bootstrap import create_agent_bootstrap_router


def setup(monkeypatch):
    vault = SimpleNamespace(_data={"default":{}}); events = []
    class Memory:
        def start_background_tasks(self): events.append("memory-start")
        def close(self): events.append("memory-close")
    class Cron:
        def start(self, callback): events.append("cron-start")
        def stop(self): events.append("cron-stop")
    async def close_llm(): events.append("llm-close")
    async def stop_scheduler(): events.append("scheduler-stop")
    state = SimpleNamespace(vault=vault, config=SimpleNamespace(settings={},credentials={}),
        memory=Memory(), orchestrator=None, cron_service=None,
        _native_vault_deferred=True,_native_bootstrap_required=True,
        _native_agent_hooks_complete=False,_background_tasks=set(),
        _native_continuation_checkpoint=None,_native_pending_memory_close=[],
        _native_hydrated_env={},_native_previous_llm_credentials=None,_native_pending_agent_turns=[])
    def register(task): state._background_tasks.add(task); return task
    state.register_background_task = register
    from api.boot_report import BootReport, SubsystemStatus
    state._boot_report = BootReport()
    async def init():
        # Exact bound init method called by production adapter; services are
        # injected here to forbid actual provider/account/device requests.
        events.append("init")
        state._native_continuation_checkpoint()
        state.orchestrator = SimpleNamespace(llm=SimpleNamespace(close=close_llm),
                                            stop_consolidation_scheduler=stop_scheduler)
        state.cron_service = Cron()
        for name in ("SkillRegistry", "NodeSubdeviceStore", "ProviderCatalog", "LLMProvider", "Orchestrator"):
            state._boot_report.record(name, SubsystemStatus.OK, optional=False)
    state.init = init
    def disable():
        from api.state import BrainState
        BrainState._disable_native_vault_dependents(state)
    coordinator = VaultCoordinator(disable_dependents=disable)
    coordinator._vault=vault;coordinator._state="ready";coordinator._code="ready"
    state.vault_coordinator = coordinator
    from api import server
    async def backlog(): events.append("backlog")
    monkeypatch.setattr(server,"_resume_ambient_backlog",backlog)
    monkeypatch.setattr("services.mdns.stop_advertisement",lambda: events.append("mdns-stop"))
    c = create_agent_bootstrap_controller(state,server._start_reviewed_agent_hooks)
    state.agent_bootstrap_controller=c
    app=FastAPI();app.include_router(create_agent_bootstrap_router(lambda:c))
    app.add_middleware(AgentBootstrapFence,state=state)
    @app.get('/agent-action')
    async def action(): return {"ok":True}
    @app.get('/health')
    async def health(): return {"bootstrap_required":state._native_bootstrap_required}
    return c,state,events,app


@pytest.mark.asyncio
async def test_real_adapter_hooks_http_resume_and_lock_cleanup(monkeypatch):
    c,s,events,app=setup(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        assert (await client.get('/agent-action')).status_code==503
        assert (await client.get('/health')).status_code==200
        review=await client.post('/api/security/agent-bootstrap/review',json={})
        assert review.status_code==200 and events==[]
        token=review.json()['review_token']
        receipt=await client.post('/api/security/agent-bootstrap',json={'review_token':token})
        assert receipt.status_code==200 and receipt.json()['ok']
        assert s.vault is s.vault_coordinator.require_ready()
        assert events.count('memory-start')==events.count('cron-start')==1
        from api.server import _start_reviewed_agent_hooks
        await _start_reviewed_agent_hooks(s)
        assert events.count('memory-start')==events.count('cron-start')==1
        assert (await client.get('/agent-action')).status_code==200
        await s.vault_coordinator.lock()
        assert (await client.get('/agent-action')).status_code==503
        await c._operation
        assert 'cron-stop' in events and 'llm-close' in events
        assert events.index('cron-stop')<events.index('memory-close')
        assert events.index('llm-close')<events.index('memory-close')
        assert not c.status()['agent_ready'] and c.status()['restart_required']


@pytest.mark.asyncio
async def test_partial_created_services_torn_down_preexisting_task_preserved(monkeypatch):
    c,s,events,app=setup(monkeypatch)
    preexisting=asyncio.create_task(asyncio.Event().wait());s._background_tasks.add(preexisting)
    run=s.init
    async def partial():
        await run()
        task=asyncio.create_task(asyncio.Event().wait());s.register_background_task(task)
        raise RuntimeError('private provider response')
    s.init=partial
    result=await c.confirm(c.review()['review_token'])
    assert not result['ok'] and c.status()['restart_required']
    assert not preexisting.done() and s.orchestrator is None
    assert events.index('cron-stop')<events.index('llm-close')
    assert 'private' not in str(result)
    preexisting.cancel();await asyncio.gather(preexisting,return_exceptions=True)


@pytest.mark.asyncio
async def test_checked_real_state_await_cannot_swallow_locked_identity(monkeypatch):
    from api.state import BrainState
    changed=False
    def check():
        if changed: raise RuntimeError('private credential changed')
    async def operation():
        nonlocal changed;changed=True;return 3
    state=SimpleNamespace(_native_continuation_checkpoint=check)
    with pytest.raises(asyncio.CancelledError):
        await BrainState._checked_bootstrap_await(state,operation())


@pytest.mark.asyncio
async def test_active_lock_cancels_owned_init_and_cleans_partial_resources(monkeypatch):
    c,s,events,app=setup(monkeypatch);gate=asyncio.Event();run=s.init
    async def pending(): await run();await gate.wait()
    s.init=pending
    result=await c.confirm(c.review()['review_token'],timeout=0)
    await asyncio.sleep(0)
    await s.vault_coordinator.lock()
    await c._operation
    assert not c.status()['agent_ready'] and c.status()['restart_required']
    assert s.orchestrator is None and 'llm-close' in events


@pytest.mark.asyncio
async def test_fence_covers_new_and_existing_websocket_and_local_fresh_use():
    state=SimpleNamespace(_native_vault_deferred=True,_native_bootstrap_required=True)
    called=[];sent=[]
    async def app(scope,receive,send): called.append(await receive())
    async def receive(): state._native_bootstrap_required=True;return {'type':'websocket.receive','text':'invoke'}
    async def send(value):sent.append(value)
    fence=AgentBootstrapFence(app,state)
    await fence({'type':'websocket','path':'/v1/node'},receive,send)
    assert sent==[{'type':'websocket.close','code':1013}] and called==[]
    sent.clear();state._native_bootstrap_required=False
    await fence({'type':'websocket','path':'/v1/session'},receive,send)
    assert called[-1]['type']=='websocket.disconnect'
    state._native_bootstrap_required=False
    assert not fence.blocked()


@pytest.mark.asyncio
async def test_authenticated_vault_content_change_invalidates_review(monkeypatch):
    from security.agent_bootstrap_continuation import BootstrapRefusal
    c,s,events,app=setup(monkeypatch);review=c.review()
    s.vault._data["default"]["provider-key"]="changed private key"
    with pytest.raises(BootstrapRefusal): await c.confirm(review["review_token"])
    assert events==[]


@pytest.mark.asyncio
async def test_immediate_lock_before_owned_task_starts_has_fixed_failed_receipt(monkeypatch):
    c,s,events,app=setup(monkeypatch)
    token=c.review()["review_token"]
    caller=asyncio.create_task(c.confirm(token))
    await asyncio.sleep(0)
    await s.vault_coordinator.lock()
    result=await caller
    assert not result["ok"] and result["status"]["restart_required"]
    assert events==["memory-close"]  # No producers existed; lock safely closes restored memory.


@pytest.mark.asyncio
async def test_connected_but_not_published_device_is_captured_for_cleanup(monkeypatch):
    c,s,events,app=setup(monkeypatch)
    class Device:
        async def disconnect(self): events.append("device-disconnect")
    async def partial():
        s._native_continuation_resources.append(Device())
        raise asyncio.CancelledError()
    s.init=partial
    result=await c.confirm(c.review()["review_token"])
    assert not result["ok"] and "device-disconnect" in events


@pytest.mark.asyncio
async def test_unknown_review_and_extra_request_values_do_not_dispatch(monkeypatch):
    c,s,events,app=setup(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://local") as client:
        r=await client.post("/api/security/agent-bootstrap",json={"review_token":"A"*43})
        assert r.status_code==409 and r.json()["detail"]["code"]=="review_expired"
        r=await client.post("/api/security/agent-bootstrap/review",json={"skip_review":True})
        assert r.status_code==400
        assert events==[]


@pytest.mark.asyncio
async def test_required_boot_report_failure_cannot_publish_ready(monkeypatch):
    c,s,events,app=setup(monkeypatch);run=s.init
    async def failed_required():
        await run()
        from api.boot_report import SubsystemStatus
        s._boot_report.record("Orchestrator",SubsystemStatus.FAILED,optional=False)
    s.init=failed_required
    result=await c.confirm(c.review()["review_token"])
    assert not result["ok"] and result["status"]["restart_required"]
    assert "memory-start" not in events and "llm-close" in events


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [{"phone_bridge_url":"auto"},{"llm":{"provider":"openai","model":"gpt-4o-mini-transcribe"}}])
async def test_owned_boot_self_heal_is_refused_before_effects(monkeypatch,settings):
    from security.agent_bootstrap_continuation import BootstrapRefusal
    c,s,events,app=setup(monkeypatch);s.config.settings=settings
    with pytest.raises(BootstrapRefusal) as refusal:c.review()
    assert refusal.value.code=="bootstrap_config_requires_update" and events==[]
    assert not c.status()["restart_required"]


@pytest.mark.asyncio
async def test_failed_producer_drain_retains_unavailable_memory_until_process_exit(monkeypatch):
    c,s,events,app=setup(monkeypatch);run=s.init;restored=s.memory
    def cannot_stop():events.append("cron-stop-failed");raise RuntimeError("private stop failure")
    async def partial():
        await run();s.cron_service.stop=cannot_stop;raise RuntimeError("partial startup")
    s.init=partial
    result=await c.confirm(c.review()["review_token"])
    assert not result["ok"] and result["status"]["restart_required"]
    assert s.memory is None and s._native_pending_memory_close==[restored]
    assert "memory-close" not in events and "cron-stop-failed" in events


@pytest.mark.asyncio
async def test_pending_oauth_preflight_preserves_authenticated_record(monkeypatch):
    from security.agent_bootstrap_continuation import BootstrapRefusal
    c,s,events,app=setup(monkeypatch)
    pending={"value":"private pending authorization","created":0}
    s.vault._data["credentials"]={"oauth_pending_abc":pending}
    with pytest.raises(BootstrapRefusal) as refusal:c.review()
    assert refusal.value.code=="bootstrap_config_requires_update" and events==[]
    assert s.vault._data["credentials"]["oauth_pending_abc"] is pending


@pytest.mark.asyncio
async def test_live_turn_is_drained_before_restored_memory_closes(monkeypatch):
    from security.agent_turn_lease import spawn_agent_turn
    c,s,events,app=setup(monkeypatch)
    await c.confirm(c.review()["review_token"])
    entered=asyncio.Event()
    async def turn():
        entered.set()
        try: await asyncio.Event().wait()
        finally: events.append("turn-drained")
    task=spawn_agent_turn(s,turn());await entered.wait()
    await s.vault_coordinator.lock()
    assert s.memory is None and "memory-close" not in events
    await c._operation
    assert task.done() and events.index("turn-drained")<events.index("memory-close")
