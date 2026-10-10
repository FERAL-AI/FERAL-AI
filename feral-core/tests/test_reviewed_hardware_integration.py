"""Actual Mesh/router integration with fake authenticated sockets and isolated stores."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from hardware.command_contract import CommandLedger
from hardware.mesh import HardwareMesh
from hardware.protocol import DeviceRegistry
from security.capability_grants import CapabilityGrantStore
from tests.test_reviewed_hardware_dispatch import Policy

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
async def integrated(tmp_path, monkeypatch):
    from api.routes import hardware_reviewed as routes
    ws = SimpleNamespace(send_json=AsyncMock())
    daemons = {"glasses": ws}
    registry = DeviceRegistry()
    mesh = HardwareMesh(registry, daemons, ledger=CommandLedger(str(tmp_path / "ledger.db")))
    await mesh.on_node_connected("glasses", {"node_type": "glasses", "device_manifest": {
        "name": "Fixture glasses", "device_type": "glasses", "actions": [{
            "name": "temperature", "description": "Private sensor", "category": "sensor",
            "permission_tier": "passive", "requires_confirmation": False, "params": []}]}})
    grants = CapabilityGrantStore(str(tmp_path / "grants.db"))
    state = SimpleNamespace(primary_session_id="primary-test", memory=object(), orchestrator=object(),
        hardware_mesh=mesh, policy=Policy(), _native_bootstrap_required=False)
    monkeypatch.setattr(routes, "state", state)
    monkeypatch.setattr(routes, "live_grants", lambda: grants)
    monkeypatch.setenv("FERAL_API_KEY", "operator-fixture-key")
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5555)),
                                 base_url="http://127.0.0.1:9090") as client:
        yield SimpleNamespace(mesh=mesh, ws=ws, daemons=daemons, registry=registry, state=state,
                              grants=grants, client=client, routes=routes, app=app)
    await mesh.close_reviewed()


async def make_review(rig):
    response = await rig.client.post("/api/hardware/reviewed/review", json={
        "node_id": "glasses", "command": "temperature", "params": {}})
    assert response.status_code == 200, response.text
    return response.json()["review"]


async def dispatch(rig, review, body=None):
    response = await rig.client.post(f'/api/hardware/reviewed/{review["review_id"]}/dispatch', json=body or {})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_real_router_to_captured_socket_frame_ack_and_final_ledger(integrated):
    rig = integrated
    item = await make_review(rig)
    assert item["owner"] == "operator:primary-test"
    receipt = await dispatch(rig, item)
    assert receipt["queued"] is True and receipt["state"] == "submitted"
    assert receipt["physical_outcome_verified"] is False
    await asyncio.sleep(0)
    rig.ws.send_json.assert_awaited_once()
    frame = rig.ws.send_json.call_args.args[0]
    cid = receipt["command_id"]
    assert frame["payload"]["action_id"] == cid
    assert frame["payload"]["name"] == "temperature"
    # The real ingress entrypoint must supply the authenticated socket and node.
    assert rig.mesh.resolve_invoke(cid, {"ack": True}, connection=rig.ws, node_id="glasses")
    response = await rig.client.get(f"/api/hardware/reviewed/commands/{cid}")
    assert response.json()["acknowledged"] is True
    assert response.json()["device_reported_success"] is None
    assert rig.mesh.resolve_invoke(cid, {"success": True, "data": {"temperature": 36}}, connection=rig.ws, node_id="glasses")
    response = await rig.client.get(f"/api/hardware/reviewed/commands/{cid}")
    assert response.json()["state"] == "succeeded"
    assert response.json()["device_reported_success"] is True
    assert response.json()["device_report"] == {"success": True, "data": {"temperature": 36}}
    assert response.json()["physical_outcome_verified"] is False
    await asyncio.sleep(0)
    assert not rig.mesh._pending_invokes  # no legacy future or duplicate command UUID


@pytest.mark.asyncio
async def test_reviewed_results_require_real_socket_not_just_command_uuid(integrated):
    rig = integrated
    cid = (await dispatch(rig, await make_review(rig)))["command_id"]
    assert not rig.mesh.resolve_invoke(cid, {"success": True})
    assert not rig.mesh.resolve_invoke(cid, {"success": True}, connection=object(), node_id="glasses")
    assert not rig.mesh.resolve_invoke(cid, {"success": True}, connection=rig.ws, node_id="foreign")
    assert rig.mesh.ledger.get(cid).state.value == "submitted"


@pytest.mark.asyncio
async def test_permission_drift_between_queue_and_task_prevents_actual_write(integrated):
    rig = integrated
    item = await make_review(rig)
    # Synchronous direct dispatch queues, then an operator revokes before yield.
    receipt = rig.mesh.dispatch_reviewed(owner=item["owner"], review_id=item["review_id"])
    rig.grants.set_grant("glasses", "temperature", False)
    await asyncio.sleep(0)
    rig.ws.send_json.assert_not_awaited()
    assert rig.mesh.ledger.get(receipt["command_id"]).state.value == "failed"
    assert rig.mesh.read_reviewed(owner=item["owner"], command_id=receipt["command_id"])["effect"] == "unknown"


@pytest.mark.asyncio
async def test_same_id_replacement_before_queue_or_send_never_targets_new_socket(integrated):
    rig = integrated
    item = await make_review(rig)
    old_binding = rig.mesh.reviewed_binding("glasses")
    replacement = SimpleNamespace(send_json=AsyncMock())
    rig.daemons["glasses"] = replacement
    await rig.mesh.on_node_connected("glasses", {"node_type": "glasses", "capabilities": ["temperature"]})
    assert rig.mesh.reviewed_binding("glasses").generation != old_binding.generation
    response = await rig.client.post(f'/api/hardware/reviewed/{item["review_id"]}/dispatch', json={})
    assert response.status_code == 409
    rig.ws.send_json.assert_not_awaited()
    replacement.send_json.assert_not_awaited()
    rig.mesh.on_node_disconnected("glasses", connection=rig.ws)
    assert rig.registry.get_device("glasses") is not None  # stale hardware cleanup guarded
    assert rig.mesh.reviewed_binding("glasses").connection is replacement


@pytest.mark.asyncio
async def test_separate_server_authorization_bound_to_exact_review_and_one_use(integrated):
    rig = integrated
    cap = rig.registry.get_device("glasses").capabilities[0]
    cap.requires_confirmation = True
    item = await make_review(rig)
    response = await rig.client.post(f'/api/hardware/reviewed/{item["review_id"]}/dispatch', json={"confirmed": True})
    assert response.status_code == 400
    # Invalid flag does not consume the review, but cannot authorize it.
    response = await rig.client.post(f'/api/hardware/reviewed/{item["review_id"]}/authorize', json={})
    assert response.status_code == 200
    token = response.json()["authorization_token"]
    assert response.json()["dispatch_accepted"] is False
    foreign = await make_review(rig)
    response = await rig.client.post(f'/api/hardware/reviewed/{foreign["review_id"]}/dispatch', json={"authorization_token": token})
    assert response.status_code == 409
    receipt = await dispatch(rig, item, {"authorization_token": token})
    assert receipt["queued"] is True
    response = await rig.client.post(f'/api/hardware/reviewed/{foreign["review_id"]}/dispatch', json={"authorization_token": token})
    assert response.status_code == 409
    response = await rig.client.post(f'/api/hardware/reviewed/{item["review_id"]}/dispatch', json={"authorization_token": token})
    assert response.status_code == 409
    await asyncio.sleep(0)
    rig.ws.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_server_primary_scope_replacement_cannot_consume_prior_review(integrated):
    rig = integrated
    item = await make_review(rig)
    rig.state.primary_session_id = "other-primary"
    response = await rig.client.post(f'/api/hardware/reviewed/{item["review_id"]}/dispatch', json={})
    assert response.status_code == 409
    rig.state.primary_session_id = "primary-test"
    assert (await dispatch(rig, item))["queued"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"owner": "foreign"}, {"confirmed": True}, {"session_id": "foreign"}, {"authorization_token": str(uuid4())}])
async def test_review_rejects_claimed_client_authority(integrated, body):
    response = await integrated.client.post("/api/hardware/reviewed/review", json={
        "node_id": "glasses", "command": "temperature", "params": {}, **body})
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{"origin": "https://evil.example"}, {"origin": "null"},
    {"host": "evil.example:9090", "origin": "http://evil.example:9090"},
    {"origin": "http://user:secret@127.0.0.1:9090"}])
async def test_local_origin_dns_rebinding_and_credential_origins_refused(integrated, headers):
    response = await integrated.client.post("/api/hardware/reviewed/review", headers=headers, json={
        "node_id": "glasses", "command": "temperature", "params": {}})
    assert response.status_code in {401, 403}
    integrated.ws.send_json.assert_not_awaited()


def request(*, client="10.0.0.5", host="brain.local", headers=None, untrusted=False):
    raw = [(b"host", host.encode())] + [(key.encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request({"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/hardware/reviewed/review", "raw_path": b"/api/hardware/reviewed/review",
        "query_string": b"", "headers": raw, "client": (client, 1), "server": (host, 80),
        "feral.untrusted": untrusted})


@pytest.mark.asyncio
async def test_remote_proxy_phone_bearer_and_dev_bypass_cannot_acquire_operator_scope(integrated, monkeypatch):
    from fastapi import HTTPException
    auth = integrated.routes._operator
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    cases = [request(headers={"authorization": "Bearer phone-bearer"}),
        request(headers={"x-forwarded-for": "127.0.0.1"}),
        request(client="127.0.0.1", host="127.0.0.1", untrusted=True)]
    for item in cases:
        with pytest.raises(HTTPException) as exc:
            auth(item)
        assert exc.value.status_code == 401
    assert auth(request(headers={"authorization": "Bearer operator-fixture-key"})) == "operator:primary-test"
    assert auth(request(client="127.0.0.1", host="127.0.0.1")) == "operator:primary-test"


@pytest.mark.asyncio
async def test_limited_agent_cannot_queue_hardware_even_with_operator_auth(integrated):
    integrated.state._native_bootstrap_required = True
    response = await integrated.client.post("/api/hardware/reviewed/review", json={
        "node_id": "glasses", "command": "temperature", "params": {}})
    assert response.status_code == 503
    integrated.ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_and_stop_report_unknown_effect_and_drain_only_owned_tasks(integrated):
    rig = integrated
    rig.ws.send_json.side_effect = OSError("private transport secret")
    item = await make_review(rig)
    receipt = await dispatch(rig, item)
    await asyncio.sleep(0)
    readback = rig.mesh.read_reviewed(owner=item["owner"], command_id=receipt["command_id"])
    assert readback["state"] == "failed" and readback["effect"] == "unknown"
    assert "secret" not in str(readback)
    rig.ws.send_json.side_effect = None
    item = await make_review(rig)
    cid = (await dispatch(rig, item))["command_id"]
    unrelated = asyncio.create_task(asyncio.sleep(60))
    await rig.mesh.close_reviewed()
    assert not unrelated.done()
    assert not rig.mesh._reviewed_tasks
    assert rig.mesh.ledger.get(cid).state.value == "failed"
    unrelated.cancel()
    await asyncio.gather(unrelated, return_exceptions=True)
