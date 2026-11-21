"""Registered paired HUP status against disposable real receipt/pairing stores."""
import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError
from security.approval_ingress import PairedDevicePrincipal
from security.device_pairing import DevicePairingStore

pytestmark = [pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]


def device(tmp_path, name):
    store = DevicePairingStore(db_path=str(tmp_path / "paired.db"))
    issued = store.pair_device(name, kind="browser_node_v2")
    token = issued["phone_bearer"]
    assert store.verify_phone_bearer(token) == issued["device_id"]
    source = PairedDevicePrincipal(issued["device_id"], lambda: store.admitted_credential_current(
        device_id=issued["device_id"], credential=token, bearer_kind="phone_bearer"))
    return store, issued, source


def test_registered_hup_readonly_private_status_and_reconnect_missing_store(tmp_path):
    from unittest.mock import AsyncMock
    from agents.chat_turns import ChatTurnManager
    from memory.store import MemoryStore
    from tests.test_phone_chat_responsive_intake import _state
    from tests.test_hup_protocol import _node_client, _register_node, _TEST_NODE_KEY
    brain = _state()
    memory = MemoryStore(db_path=str(tmp_path / 'receipts.db'))
    brain.memory, brain.chat_turns = memory, None
    store, issued, _ = device(tmp_path, 'owner')
    foreign = store.pair_device('foreign', kind='browser_node_v2')
    request_id = str(uuid4())
    with _node_client(brain) as tc:
        brain.device_pairing_store = store
        with tc.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + issued['phone_bearer']}) as ws:
            ack = _register_node(ws, 'phone', 'phone')
            assert ack['payload']['phone_turn_contract'] == {'contract_version': 1,
                'durable_receipts': True, 'reconnect_reconciliation': True, 'readonly_status': True}
            manager = brain.chat_turns
            assert type(manager) is ChatTurnManager
            source = brain.daemons['phone']._feral_phone_chat_intake._review_principal
            async def seed():
                receipt = await manager.submit(owner=object(), session_id='origin-A', request_id=request_id,
                    terms={'text': 'inert'}, run=AsyncMock(return_value='inert finished'), emit=AsyncMock(), source_principal=source)
                await manager._live[('origin-A', receipt['turn_id'])].task
                return await manager.status(session_id='origin-A', request_id=request_id, source_principal=source)
            receipt = ws.portal.call(seed)
            for requested, expected in [(request_id, 'found'), (str(uuid4()), 'not_found')]:
                ws.send_json({'type': 'phone_turn_status_request', 'payload': {'contract_version': 1,
                    'session_id': 'origin-A', 'request_id': requested}})
                response = ws.receive_json()
                assert response['type'] == 'phone_turn_status'
                assert response['payload']['request_id'] == requested
                assert response['payload']['status'] == expected
                if expected == 'found':
                    assert response['payload']['receipt'] == receipt
                    assert issued['device_id'] not in str(response)
            brain.orchestrator.handle_command.assert_not_awaited()
            with tc.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + foreign['phone_bearer']}) as other:
                _register_node(other, 'foreign', 'phone')
                other.send_json({'type': 'phone_turn_status_request', 'payload': {'contract_version': 1,
                    'session_id': 'origin-A', 'request_id': request_id}})
                assert other.receive_json()['payload'] == {'contract_version': 1, 'request_id': request_id,
                    'session_id': 'origin-A', 'status': 'not_found'}
            ws.portal.call(memory.aclose)
        # Reopen the same pairing identity against an empty receipt store.
        replacement = MemoryStore(db_path=str(tmp_path / 'empty.db'))
        brain.memory, brain.chat_turns = replacement, None
        with tc.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + issued['phone_bearer']}) as ws:
            _register_node(ws, 'phone', 'phone')
            ws.send_json({'type': 'phone_turn_status_request', 'payload': {'contract_version': 1,
                'session_id': 'origin-A', 'request_id': request_id}})
            assert ws.receive_json()['payload']['status'] == 'not_found'
            brain.orchestrator.handle_command.assert_not_awaited()
            ws.portal.call(replacement.aclose)
        with tc.websocket_connect(f'/v1/node?api_key={_TEST_NODE_KEY}') as legacy:
            ack = _register_node(legacy, 'legacy', 'phone')
            assert 'phone_turn_contract' not in ack['payload']



@pytest.mark.parametrize('field,value', [('contract_version', True), ('contract_version', '1'),
    ('request_id', 'ABCDEFAB-1234-1234-1234-ABCDEFABCDEF'), ('session_id', ' padded '),
    ('session_id', 'bad\n'), ('extra', 'goal')])
def test_exact_status_request_model_refuses_coercion_and_extra_claims(field, value):
    from models.protocol import PhoneTurnStatusRequestPayload
    payload = {'contract_version': 1, 'request_id': str(uuid4()), 'session_id': 'origin-A'}
    payload[field] = value
    with pytest.raises(ValidationError):
        PhoneTurnStatusRequestPayload.model_validate(payload)


@pytest.mark.parametrize('invalidation', ['socket', 'state', 'store', 'memory', 'manager', 'orchestrator', 'generation', 'credential'])
def test_post_read_invalidation_does_not_publish_stale_receipt(tmp_path, monkeypatch, invalidation):
    from agents.chat_turns import ChatTurnManager
    from memory.store import MemoryStore
    from starlette.websockets import WebSocketDisconnect
    from tests.test_phone_chat_responsive_intake import _state
    from tests.test_hup_protocol import _node_client, _register_node
    brain = _state()
    memory = MemoryStore(db_path=str(tmp_path / 'receipts.db'))
    brain.memory, brain.chat_turns = memory, None
    brain._native_agent_turn_generation = 7
    store, issued, _ = device(tmp_path, 'owner')
    request_id = str(uuid4())
    with _node_client(brain) as tc:
        from api import server
        brain.device_pairing_store = store
        with tc.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + issued['phone_bearer']}) as ws:
            _register_node(ws, 'phone', 'phone')
            original_get = memory.chat_turn_get
            async def stale_read(**kwargs):
                receipt = await original_get(**kwargs)
                if invalidation == 'socket':
                    brain.daemons['phone'] = object()
                elif invalidation == 'state':
                    server.state = _state()
                elif invalidation == 'store':
                    brain.device_pairing_store = object()
                elif invalidation == 'memory':
                    brain.memory = object()
                elif invalidation == 'manager':
                    brain.chat_turns = ChatTurnManager(brain)
                elif invalidation == 'orchestrator':
                    brain.orchestrator = object()
                elif invalidation == 'generation':
                    brain._native_agent_turn_generation += 1
                else:
                    store.rotate_phone_bearer(issued['device_id'])
                await asyncio.sleep(0)
                return receipt
            monkeypatch.setattr(memory, 'chat_turn_get', stale_read)
            ws.send_json({'type': 'phone_turn_status_request', 'payload': {'contract_version': 1,
                'session_id': 'origin-A', 'request_id': request_id}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
            ws.portal.call(memory.aclose)


def test_registered_bad_query_cannot_admit_turn_or_disclose_receipts(tmp_path):
    from memory.store import MemoryStore
    from tests.test_phone_chat_responsive_intake import _state
    from tests.test_hup_protocol import _node_client, _register_node
    brain = _state()
    memory = MemoryStore(db_path=str(tmp_path / 'receipts.db'))
    brain.memory, brain.chat_turns = memory, None
    store, issued, _ = device(tmp_path, 'owner')
    with _node_client(brain) as tc:
        brain.device_pairing_store = store
        with tc.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + issued['phone_bearer']}) as ws:
            _register_node(ws, 'phone', 'phone')
            ws.send_json({'type': 'phone_turn_status_request', 'payload': {'contract_version': 1,
                'session_id': 'origin-A', 'request_id': str(uuid4()), 'text': 'must never run'}})
            assert ws.receive_json()['type'] == 'error'
            brain.orchestrator.handle_command.assert_not_awaited()
            assert brain.chat_turns._live == {}
            ws.portal.call(memory.aclose)


@pytest.mark.parametrize('failure', ['read_error', 'wrong_identity', 'startup_error'])
def test_unavailable_status_has_no_receipt_and_no_admission(tmp_path, monkeypatch, failure):
    from memory.store import MemoryStore
    from tests.test_phone_chat_responsive_intake import _state
    from tests.test_hup_protocol import _node_client, _register_node
    brain = _state()
    memory = MemoryStore(db_path=str(tmp_path / 'receipts.db'))
    brain.memory, brain.chat_turns = memory, None
    store, issued, _ = device(tmp_path, 'owner')
    request_id = str(uuid4())
    async def unavailable(*args, **kwargs):
        raise RuntimeError('inert store failure')
    async def wrong_identity(**kwargs):
        return {'request_id': str(uuid4()), 'session_id': kwargs['session_id'], 'final_text': 'must not disclose'}
    if failure == 'startup_error':
        monkeypatch.setattr(memory, 'chat_turn_recover', unavailable)
    elif failure == 'read_error':
        monkeypatch.setattr(memory, 'chat_turn_get', unavailable)
    else:
        monkeypatch.setattr(memory, 'chat_turn_get', wrong_identity)
    with _node_client(brain) as tc:
        brain.device_pairing_store = store
        with tc.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + issued['phone_bearer']}) as ws:
            ack = _register_node(ws, 'phone', 'phone')
            assert ('phone_turn_contract' in ack['payload']) is (failure != 'startup_error')
            ws.send_json({'type': 'phone_turn_status_request', 'payload': {'contract_version': 1,
                'session_id': 'origin-A', 'request_id': request_id}})
            assert ws.receive_json()['payload'] == {'contract_version': 1, 'session_id': 'origin-A',
                'request_id': request_id, 'status': 'unavailable'}
            brain.orchestrator.handle_command.assert_not_awaited()
            assert brain.chat_turns._live == {}
            ws.portal.call(memory.aclose)


def test_paired_ack_does_not_claim_durability_for_mock_receipt_store(tmp_path):
    from unittest.mock import MagicMock
    from memory.store import MemoryStore
    from tests.test_phone_chat_responsive_intake import _state
    from tests.test_hup_protocol import _node_client, _register_node
    brain = _state()
    brain.memory = MagicMock(spec=MemoryStore)
    brain.memory.db_path = str(tmp_path / 'invented.db')
    brain.chat_turns = None
    store, issued, _ = device(tmp_path, 'owner')
    with _node_client(brain) as tc:
        brain.device_pairing_store = store
        with tc.websocket_connect('/v1/node', headers={'authorization': 'Bearer ' + issued['phone_bearer']}) as ws:
            assert 'phone_turn_contract' not in _register_node(ws, 'phone', 'phone')['payload']
            ws.send_json({'type': 'phone_turn_status_request', 'payload': {'contract_version': 1,
                'session_id': 'origin-A', 'request_id': str(uuid4())}})
            assert ws.receive_json()['payload']['status'] == 'unavailable'
            brain.memory.chat_turn_recover.assert_not_called()
            brain.orchestrator.handle_command.assert_not_awaited()
            assert brain.chat_turns is None
