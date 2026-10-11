"""Registered connection routes and owner-preserving manager transitions.

Only the external Chrome connector boundary is controlled. Routes, manager,
call-context binding and ToolRunner resource capture use actual local classes.
No Chrome, account, profile discovery, model or browser input occurs here.
"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest

from agents.tool_runner import ToolRunner, current_browser_resource_binding
from api.existing_chrome_connection import ChromeConnectionManager
from security.exec_approvals import ApprovalManager
from security.trust_ledger import TrustLedger
from skills.call_context import bind_context, current_context
from skills.impl.existing_chrome import ExistingChromeError

HEADERS = {'X-FERAL-Browser-View': 'native-v1'}
OWNER = 'synthetic-owner-A'
FOREIGN = 'synthetic-owner-B'


class ControlledBrowser:
    def __init__(self, owner=None, connection=None, target=None):
        self.connected = owner is not None
        self.binding = None if owner is None else {
            'owner_session_id': owner, 'connection_id': connection, 'target_id': target}
        self.browser_close_calls = 0
        self.initialize_calls = 0

    def approval_binding(self):
        if self.binding is None:
            return None
        if current_context().session_id != self.binding['owner_session_id']:
            raise ExistingChromeError('existing_chrome_owner_required', 403)
        return dict(self.binding)

    async def close(self):
        self.browser_close_calls += 1
        raise AssertionError('Manager must never close a browser process')

    async def initialize(self):
        self.initialize_calls += 1
        raise AssertionError('Connection must never implicitly launch Chrome')


class ControlledConnector:
    def __init__(self, owner):
        self.owner = owner
        self.connection = str(uuid4())
        self.connected = False
        self.controller = None
        self.calls = []
        self.hooks = {}
        self.target_calls = 0

    async def hook(self, name):
        if name in self.hooks:
            await self.hooks[name]()

    def status(self):
        return {'connected': self.connected, 'mode': 'existing_chrome',
                'owner_session_id': self.owner, 'connection_id': self.connection,
                'selected_target_id': self.controller.binding['target_id'] if self.controller else None}

    async def connect(self, consent):
        self.calls.append(('connect', consent, current_context().session_id))
        assert consent is True and current_context().session_id == self.owner
        await self.hook('connect')
        self.connected = True
        return {'success': True, **self.status()}

    async def targets(self):
        self.target_calls += 1
        await self.hook('targets')
        return [{'id': 'tab-A', 'label': 'Chrome tab 1'}, {'id': 'tab-B', 'label': 'Chrome tab 2'}]

    async def select(self, target):
        self.calls.append(('select', target, current_context().session_id))
        assert current_context().session_id == self.owner
        if target not in {'tab-A', 'tab-B'}:
            raise ExistingChromeError('existing_chrome_target_unavailable')
        if self.controller:
            self.controller.connected = False
        self.controller = None
        self.connection = str(uuid4())
        await self.hook('select')
        self.controller = ControlledBrowser(self.owner, self.connection, target)
        return {'success': True, **self.status()}

    async def disconnect(self):
        self.calls.append(('disconnect',))
        await self.hook('disconnect')
        self.connected = False
        if self.controller:
            self.controller.connected = False
        self.controller = None
        return {'success': True, **self.status(), 'browser_closed': False}


@pytest.fixture
def wired(monkeypatch):
    # Import state-owning routers only after the disposable-home autouse fixture.
    from api.routes import existing_chrome as routes
    from api.routes import marketplace_browser
    monkeypatch.setenv('FERAL_TOOL_CALL_CONTEXT', 'on')
    orch = SimpleNamespace()
    orch.tool_runner = ToolRunner(orch, trust_ledger=TrustLedger(persist=False),
                                  approval_manager=ApprovalManager(db_path=':memory:'))
    previous = ControlledBrowser()
    brain = SimpleNamespace(browser=previous, orchestrator=orch)
    owner = [brain]
    connectors = []

    def factory(session):
        connector = ControlledConnector(session)
        connectors.append(connector)
        return connector

    manager = ChromeConnectionManager(lambda: owner[0], factory=factory)
    injected = []
    import skills.impl
    monkeypatch.setattr(skills.impl, 'get_implementation', lambda name:
                        SimpleNamespace(set_browser=injected.append) if name == 'web_actions' else None)
    monkeypatch.setattr(routes, 'chrome_connections', manager)
    app = FastAPI()
    app.include_router(marketplace_browser.router)
    return SimpleNamespace(manager=manager, brain=brain, owner=owner, previous=previous,
                           connectors=connectors, injected=injected, app=app)


def client(wired, peer='127.0.0.1', headers=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wired.app, client=(peer, 21000)),
                             base_url='http://fixture.local', trust_env=False,
                             headers=HEADERS if headers is None else headers)


async def connect(wired):
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
    assert response.status_code == 200
    return response.json()['connection_id']


async def select(wired, connection, target='tab-A'):
    async with client(wired) as http:
        return await http.post('/api/browser/existing/select', json={
            'session_id': OWNER, 'connection_id': connection, 'target_id': target})


@pytest.mark.asyncio
async def test_registered_selection_resource_rotation_restore_and_shutdown(wired):
    async with client(wired) as http:
        status = await http.post('/api/browser/existing/status', json={'session_id': OWNER})
        assert status.status_code == 200 and status.json()['connected'] is False
        assert status.headers['cache-control'] == 'no-store'
    connection = await connect(wired)
    assert wired.brain.browser is wired.previous and wired.injected == []
    first = await select(wired, connection)
    assert first.status_code == 200
    selected = wired.brain.browser
    current = first.json()['connection_id']
    assert current != connection and wired.injected == [selected]
    with bind_context(session_id=OWNER, tool_name='browser__click'):
        with wired.brain.orchestrator.tool_runner.browser_resource_scope('browser__click', OWNER):
            assert current_browser_resource_binding() == {
                'owner_session_id': OWNER, 'connection_id': current, 'target_id': 'tab-A'}
    second = await select(wired, current, 'tab-B')
    assert second.status_code == 200 and second.json()['connection_id'] != current
    assert selected.connected is False
    assert (await select(wired, current)).status_code == 409
    with bind_context(session_id=OWNER):
        await wired.manager.shutdown()
    assert wired.brain.browser is wired.previous and wired.injected[-1] is wired.previous
    assert wired.previous.initialize_calls == 0 and selected.browser_close_calls == 0
    assert wired.connectors[0].calls[-1] == ('disconnect',)


@pytest.mark.asyncio
@pytest.mark.parametrize('peer,headers', [
    ('192.0.2.1', HEADERS), ('127.0.0.1', {}),
    ('127.0.0.1', {**HEADERS, 'Origin': 'http://localhost:5173'}),
    ('127.0.0.1', {**HEADERS, 'Forwarded': 'for=127.0.0.1'}),
    ('127.0.0.1', {**HEADERS, 'X-Forwarded-For': '127.0.0.1'}),
    ('127.0.0.1', {**HEADERS, 'Sec-Fetch-Site': 'same-origin'}),
])
async def test_local_header_not_remote_or_browser_authority(wired, peer, headers):
    async with client(wired, peer, headers) as http:
        response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
    assert response.status_code == 403 and wired.connectors == []


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'session_id': OWNER, 'consent': 'true'}, {'session_id': OWNER, 'consent': 1},
    {'session_id': OWNER, 'consent': True, 'target_id': 'tab-A'},
    {'session_id': '', 'consent': True}, {'session_id': ' owner ', 'consent': True},
    {'session_id': 'owner\n', 'consent': True}, {'session_id': 1, 'consent': True},
])
async def test_strict_consent_owner_never_dispatches(wired, body):
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/connect', json=body)
    assert response.status_code == 422 and wired.connectors == []


@pytest.mark.asyncio
async def test_false_consent_context_disabled_and_unbound_manager_fail_closed(wired, monkeypatch):
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': False})
        assert response.status_code == 400
        monkeypatch.setenv('FERAL_TOOL_CALL_CONTEXT', 'off')
        response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
        assert response.status_code == 403
    monkeypatch.setenv('FERAL_TOOL_CALL_CONTEXT', 'on')
    with pytest.raises(ExistingChromeError):
        await wired.manager.connect(OWNER, True)
    assert wired.connectors == []


@pytest.mark.asyncio
async def test_foreign_owner_stale_id_invalid_target_and_no_action_route(wired):
    connection = await connect(wired)
    async with client(wired) as http:
        for name, extra in [('status', {}), ('select', {'connection_id': connection, 'target_id': 'tab-A'}),
                            ('disconnect', {'connection_id': connection})]:
            response = await http.post('/api/browser/existing/'+name, json={'session_id': FOREIGN, **extra})
            assert response.status_code == 403
        for bad in ['not-uuid', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA']:
            response = await http.post('/api/browser/existing/disconnect', json={'session_id': OWNER, 'connection_id': bad})
            assert response.status_code == 422
        for target in ['../tab', 'tab A', '', 'x'*129]:
            response = await http.post('/api/browser/existing/select', json={
                'session_id': OWNER, 'connection_id': connection, 'target_id': target})
            assert response.status_code == 422
        assert (await http.post('/api/browser/existing/action', json={'action': 'click'})).status_code == 404
    assert (await select(wired, connection, 'missing')).status_code == 409
    assert wired.brain.browser is wired.previous


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['regular_connected', 'missing_runner'])
async def test_existing_regular_connection_or_missing_authority_refuses(wired, unavailable):
    if unavailable == 'regular_connected':
        wired.previous.connected = True
    else:
        wired.brain.orchestrator.tool_runner = None
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
    assert response.status_code in {409, 503} and wired.connectors == []


@pytest.mark.asyncio
async def test_failed_connect_does_not_install_or_overwrite(wired):
    def factory(session):
        value = ControlledConnector(session)
        async def fail():
            raise ExistingChromeError('existing_chrome_unavailable', 503)
        value.hooks['connect'] = fail
        wired.connectors.append(value)
        return value
    wired.manager.factory = factory
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
    assert response.status_code == 503 and wired.brain.browser is wired.previous
    assert wired.manager._connector is None and wired.injected == []
    assert wired.connectors[0].calls[-1] == ('disconnect',)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['select', 'final_inventory'])
async def test_failed_selection_restores_exact_previous_controller(wired, phase):
    connection = await connect(wired)
    first = await select(wired, connection)
    assert first.status_code == 200
    current = first.json()['connection_id']
    connector = wired.connectors[0]
    async def fail():
        raise ExistingChromeError('existing_chrome_protocol_error', 502)
    connector.hooks['select' if phase == 'select' else 'targets'] = fail
    response = await select(wired, current, 'tab-B')
    assert response.status_code == 502
    assert wired.brain.browser is wired.previous
    assert wired.manager._connector is None and wired.injected[-1] is wired.previous


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['connect', 'select'])
async def test_cancelled_transition_no_publish_and_owned_cleanup(wired, phase):
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    if phase == 'connect':
        def factory(owner):
            connector = ControlledConnector(owner)
            connector.hooks['connect'] = hold
            wired.connectors.append(connector)
            return connector
        wired.manager.factory = factory
        async def operation():
            return await wired.manager.connect(OWNER, True)
    else:
        connection = await connect(wired)
        first = await select(wired, connection)
        current = first.json()['connection_id']
        wired.connectors[0].hooks['select'] = hold
        async def operation():
            return await wired.manager.select(OWNER, current, 'tab-B')
    async def run():
        with bind_context(session_id=OWNER):
            return await operation()
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wired.brain.browser is wired.previous and wired.manager._connector is None
    assert wired.connectors[0].calls[-1] == ('disconnect',)


@pytest.mark.asyncio
@pytest.mark.parametrize('replace', ['brain', 'controller'])
async def test_selection_await_cannot_overwrite_changed_owner(wired, replace):
    connection = await connect(wired)
    connector = wired.connectors[0]
    external = ControlledBrowser()
    async def change():
        if replace == 'brain':
            wired.owner[0] = SimpleNamespace(browser=external, orchestrator=SimpleNamespace())
        else:
            wired.brain.browser = external
    connector.hooks['select'] = change
    response = await select(wired, connection)
    assert response.status_code == 409
    assert wired.owner[0].browser is external and wired.injected == []


@pytest.mark.asyncio
async def test_status_await_refuses_replaced_brain(wired):
    await connect(wired)
    connector = wired.connectors[0]
    async def change():
        wired.owner[0] = SimpleNamespace(browser=ControlledBrowser(), orchestrator=SimpleNamespace())
    connector.hooks['targets'] = change
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/status', json={'session_id': OWNER})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_disconnect_never_overwrites_external_controller(wired):
    connection = await connect(wired)
    first = await select(wired, connection)
    external = ControlledBrowser()
    wired.brain.browser = external
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/disconnect', json={
            'session_id': OWNER, 'connection_id': first.json()['connection_id']})
    assert response.status_code == 200 and response.json()['browser_closed'] is False
    assert wired.brain.browser is external


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['orchestrator', 'runner'])
async def test_disconnect_await_cannot_restore_into_replaced_authority(wired, replacement):
    connection = await connect(wired)
    first = await select(wired, connection)
    assert first.status_code == 200
    installed = wired.brain.browser
    connector = wired.connectors[0]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_disconnect():
        entered.set()
        await release.wait()

    connector.hooks['disconnect'] = hold_disconnect
    async with client(wired) as http:
        task = asyncio.create_task(http.post('/api/browser/existing/disconnect', json={
            'session_id': OWNER, 'connection_id': first.json()['connection_id']}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if replacement == 'orchestrator':
                orch = SimpleNamespace()
                orch.tool_runner = ToolRunner(orch, trust_ledger=TrustLedger(persist=False),
                                              approval_manager=ApprovalManager(db_path=':memory:'))
                wired.brain.orchestrator = orch
            else:
                wired.brain.orchestrator.tool_runner = ToolRunner(
                    wired.brain.orchestrator, trust_ledger=TrustLedger(persist=False),
                    approval_manager=ApprovalManager(db_path=':memory:'))
        finally:
            release.set()
            response = await asyncio.wait_for(task, 2)

    assert response.status_code == 200
    assert response.json()['disconnected'] is True
    assert response.json()['browser_closed'] is False
    assert connector.connected is False and connector.controller is None
    # The retired backing is left to the replacement runtime, not overwritten
    # with a controller captured under the previous execution authority.
    assert installed.connected is False and wired.brain.browser is installed
    assert wired.injected == [installed]
    assert wired.manager._connector is None
    assert wired.manager._installed is None and wired.manager._previous is None
    assert installed.browser_close_calls == 0 and wired.previous.initialize_calls == 0


@pytest.mark.asyncio
async def test_maximum_session_contract_matches_actual_connector(wired, tmp_path):
    from skills.impl.existing_chrome import ExistingChromeConnector
    sid = 's'*1024
    actual = ExistingChromeConnector(sid, profile_dir=tmp_path)
    assert actual.owner_session_id == sid
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/connect', json={'session_id': sid, 'consent': True})
    assert response.status_code == 200 and wired.connectors[0].owner == sid


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['brain', 'controller', 'connected'])
async def test_connect_final_inventory_cannot_overwrite_changed_owner(wired, replacement):
    external = ControlledBrowser()
    def factory(owner):
        value = ControlledConnector(owner)
        async def change():
            if replacement == 'brain':
                wired.owner[0] = SimpleNamespace(browser=external, orchestrator=SimpleNamespace())
            elif replacement == 'controller':
                wired.brain.browser = external
            else:
                wired.previous.connected = True
        value.hooks['targets'] = change
        wired.connectors.append(value)
        return value
    wired.manager.factory = factory
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
    assert response.status_code == 409 and wired.manager._connector is None
    assert wired.connectors[0].calls[-1] == ('disconnect',)
    assert wired.owner[0].browser is (wired.previous if replacement == 'connected' else external)
    assert wired.injected == []


@pytest.mark.asyncio
async def test_context_revoked_during_inventory_refuses_publication(wired, monkeypatch):
    def factory(owner):
        value = ControlledConnector(owner)
        async def revoke():
            monkeypatch.setenv('FERAL_TOOL_CALL_CONTEXT', 'off')
        value.hooks['targets'] = revoke
        wired.connectors.append(value)
        return value
    wired.manager.factory = factory
    try:
        async with client(wired) as http:
            response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
    finally:
        monkeypatch.setenv('FERAL_TOOL_CALL_CONTEXT', 'on')
    assert response.status_code == 403
    assert wired.manager._connector is None and wired.brain.browser is wired.previous


@pytest.mark.asyncio
async def test_private_upstream_failure_not_in_public_response_or_logs(wired, caplog):
    def factory(owner):
        value = ControlledConnector(owner)
        async def fail():
            raise RuntimeError('PRIVATE_SYNTHETIC_EXCEPTION_SENTINEL')
        value.hooks['connect'] = fail
        wired.connectors.append(value)
        return value
    wired.manager.factory = factory
    async with client(wired) as http:
        response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
    assert response.status_code == 502
    assert response.json()['error_code'] == 'existing_chrome_operation_unconfirmed'
    assert 'PRIVATE_SYNTHETIC_EXCEPTION_SENTINEL' not in response.text + caplog.text
    assert wired.brain.browser is wired.previous and wired.manager._connector is None


@pytest.mark.asyncio
async def test_queued_connect_revocation_refuses_before_connector_creation(wired, monkeypatch):
    await wired.manager._lock.acquire()
    async def run():
        with bind_context(session_id=OWNER):
            return await wired.manager.connect(OWNER, True)
    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    monkeypatch.setenv('FERAL_TOOL_CALL_CONTEXT', 'off')
    wired.manager._lock.release()
    try:
        with pytest.raises(ExistingChromeError) as failure:
            await task
    finally:
        monkeypatch.setenv('FERAL_TOOL_CALL_CONTEXT', 'on')
    assert failure.value.code == 'existing_chrome_context_disabled'
    assert wired.connectors == [] and wired.brain.browser is wired.previous


@pytest.mark.asyncio
async def test_web_injection_failure_cannot_publish_or_keep_uncertain_controller(wired, monkeypatch):
    connection = await connect(wired)
    injected = []
    def set_browser(browser):
        injected.append(browser)
        if browser is not wired.previous:
            raise RuntimeError('synthetic web injection failed after mutation')
    import skills.impl
    monkeypatch.setattr(skills.impl, 'get_implementation', lambda name:
                        SimpleNamespace(set_browser=set_browser) if name == 'web_actions' else None)
    response = await select(wired, connection)
    assert response.status_code == 502
    assert wired.brain.browser is wired.previous and wired.manager._connector is None
    assert injected[-1] is wired.previous


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['connect', 'select', 'status'])
@pytest.mark.parametrize('replacement', ['orchestrator', 'runner'])
async def test_same_brain_authority_replacement_during_inventory_refused(wired, phase, replacement):
    def change_owner():
        if replacement == 'orchestrator':
            orch = SimpleNamespace()
            orch.tool_runner = ToolRunner(orch, trust_ledger=TrustLedger(persist=False),
                                          approval_manager=ApprovalManager(db_path=':memory:'))
            wired.brain.orchestrator = orch
        else:
            wired.brain.orchestrator.tool_runner = ToolRunner(
                wired.brain.orchestrator, trust_ledger=TrustLedger(persist=False),
                approval_manager=ApprovalManager(db_path=':memory:'))
    async def change():
        change_owner()
    if phase == 'connect':
        def factory(owner):
            connector = ControlledConnector(owner)
            connector.hooks['targets'] = change
            wired.connectors.append(connector)
            return connector
        wired.manager.factory = factory
        async with client(wired) as http:
            response = await http.post('/api/browser/existing/connect', json={'session_id': OWNER, 'consent': True})
    else:
        connection = await connect(wired)
        wired.connectors[0].hooks['targets'] = change
        if phase == 'select':
            response = await select(wired, connection)
        else:
            async with client(wired) as http:
                response = await http.post('/api/browser/existing/status', json={'session_id': OWNER})
    assert response.status_code == 409
    assert wired.brain.browser is wired.previous and wired.injected == []
