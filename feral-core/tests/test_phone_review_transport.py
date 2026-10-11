"""Private device reviews cross actual HTTP/HUP boundaries with inert effects."""
import asyncio
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.phone_chat_intake import PhoneChatIntake
from agents.orchestrator import Orchestrator
from agents.taskflow import TaskFlowRuntime
from api.state import BrainState
from api.routes.taskflows import _start_task
from models.protocol import ApprovalRequestPayload, ApprovalResolvedPayload, TaskReviewPayload
from security.approval_ingress import PairedDevicePrincipal
from security.device_pairing import DevicePairingStore
from tests import test_taskflow_model_steps as model_steps
from tests.test_hup_protocol import _node_client, _register_node
from tests.test_phone_chat_responsive_intake import _state

wired = model_steps.wired


@pytest_asyncio.fixture
async def review_boundary(wired, tmp_path, monkeypatch):
    from api import server, state as state_module
    from api.routes import approvals
    rt, orch, calls = wired
    store = DevicePairingStore(db_path=str(tmp_path / "paired.db"))
    own = store.pair_device("Owner fixture", mint_phone_bearer=True)
    other = store.pair_device("Other fixture", mint_phone_bearer=True)
    brain = BrainState.__new__(BrainState)
    brain.orchestrator, brain.memory, brain.taskflows = orch, None, rt
    brain.device_pairing_store = store
    brain.daemons, brain._daemon_session_bindings = {}, {}
    monkeypatch.setattr(state_module, "state", brain)
    monkeypatch.setattr(server, "state", brain)
    monkeypatch.setattr(approvals, "state", brain)
    source = PairedDevicePrincipal(own["device_id"], lambda: store.admitted_credential_current(
        device_id=own["device_id"], credential=own["phone_bearer"], bearer_kind="phone_bearer"))
    origin = {"contract_version": 1, "source": "tracked_chat_turn", "owner_verified": True,
              "session_id": "origin-A", "request_id": str(uuid4()), "turn_id": str(uuid4()),
              "tool_call_id": "phone-created-flow", "surface": "http_api"}
    created = _start_task({"goal": "Inert owned goal", "subtasks": ["Inert step"]},
                          origin, source.require_current, rt, source_principal=source)
    assert created["ok"] is True
    _, pending = await model_steps.ask(rt, orch, created["flow_id"])
    descriptor = orch.tool_runner.phone_review_descriptor(pending, source_principal=source)
    assert descriptor is not None
    app = FastAPI()
    app.include_router(approvals.router)
    app.add_middleware(server.APIKeyMiddleware)
    transport = ASGITransport(app=app, client=("198.51.100.9", 54321))
    yield SimpleNamespace(rt=rt, orch=orch, calls=calls, store=store, own=own, other=other,
                          brain=brain, pending=pending, review=descriptor["task_review"],
                          transport=transport, flow_id=created["flow_id"], source=source)


def client(boundary, *, foreign=False):
    token = boundary.other if foreign else boundary.own
    return AsyncClient(transport=boundary.transport, base_url="http://fixture.invalid",
                       headers={"Authorization": "Bearer " + token["phone_bearer"]})


def decision_body(boundary):
    return {"session_id": "origin-A", "task_review": dict(boundary.review)}


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_actual_owned_rest_card_resolves_once_with_private_execution_sid(review_boundary, decision):
    b = review_boundary
    assert b.pending["session_id"].startswith("taskflow-")
    async with client(b) as phone:
        listed = await phone.get("/api/approvals?session_id=origin-A&task_review_version=1")
        assert listed.status_code == 200
        row = listed.json()["approvals"][0]
        assert row["session_id"] == "origin-A" and row["task_review"] == b.review
        assert b.own["device_id"] not in listed.text and "source_binding" not in listed.text
        path = f"/api/approvals/{row['request_id']}/{decision}"
        resolved = await phone.post(path, json=decision_body(b))
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["session_id"] == "origin-A"
        assert resolved.json()["task_review"] == b.review
        assert resolved.json()["approval_scope"]["kind"] == "exact_request"
        assert (await phone.post(path, json=decision_body(b))).status_code == 403
    assert len(b.calls) == (1 if decision == "approve" else 0)
    if b.calls:
        assert b.calls[0]["tool_name"] == "notes_memory__save_note"


@pytest.mark.parametrize("change", ["foreign", "digest", "execution_sid", "extra_owner", "version"])
async def test_same_sid_foreign_or_changed_review_never_discloses_or_dispatches(review_boundary, change):
    b = review_boundary
    body = decision_body(b)
    if change == "digest":
        body["task_review"]["terms_digest"] = "0" * 64
    elif change == "execution_sid":
        body["session_id"] = b.pending["session_id"]
    elif change == "extra_owner":
        body["paired_device_id"] = b.own["device_id"]
    elif change == "version":
        body["task_review"]["version"] = True
    async with client(b, foreign=change == "foreign") as phone:
        if change == "foreign":
            listed = await phone.get("/api/approvals?session_id=origin-A&task_review_version=1")
            assert listed.status_code == 200 and listed.json() == {"count": 0, "approvals": []}
            assert b.pending["request_id"] not in listed.text and "exact terms" not in listed.text
        response = await phone.post(f"/api/approvals/{b.pending['request_id']}/approve", json=body)
        assert response.status_code in {403, 422}, response.text
    assert b.calls == [] and b.orch.tool_runner.get_pending(b.pending["request_id"]) is not None


async def test_version_negotiation_and_paired_crud_remain_closed(review_boundary):
    b = review_boundary
    async with client(b) as phone:
        assert (await phone.get("/api/approvals?session_id=origin-A")).status_code == 403
        assert (await phone.get("/api/approvals?session_id=origin-A&task_review_version=true")).status_code == 403
        assert (await phone.post(f"/api/taskflows/{b.flow_id}/resume")).status_code == 401
        assert (await phone.post(f"/api/taskflows/{b.flow_id}/cancel")).status_code == 401


@pytest.mark.parametrize("boundary", ["executor", "resolver_return"])
async def test_revocation_after_effect_reports_unknown_and_never_replays(review_boundary, boundary):
    b = review_boundary
    if boundary == "executor":
        execute = b.orch.executor.execute

        async def revoke_after_execute(**kwargs):
            result = await execute(**kwargs)
            b.store.revoke_device(b.own["device_id"])
            return result

        b.orch.executor.execute = revoke_after_execute
    else:
        resolve = b.orch.resolve_tool_approval_request

        async def revoke_after_resolve(*args, **kwargs):
            result = await resolve(*args, **kwargs)
            b.store.revoke_device(b.own["device_id"])
            return result

        b.orch.resolve_tool_approval_request = revoke_after_resolve
    async with client(b) as phone:
        response = await phone.post(f"/api/approvals/{b.pending['request_id']}/approve", json=decision_body(b))
        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["action_outcome"] == "unknown" and detail["effects_may_have_occurred"] is True
        assert detail["retry_safe"] is False
        assert (await phone.post(f"/api/approvals/{b.pending['request_id']}/approve", json=decision_body(b))).status_code == 401
    assert len(b.calls) == 1


async def test_read_guard_refuses_publication_after_checkpoint_read_revokes(review_boundary):
    b = review_boundary
    list_checkpoints = b.rt.list_device_review_checkpoints

    def revoke_after_read(*args, **kwargs):
        result = list_checkpoints(*args, **kwargs)
        b.store.revoke_device(b.own["device_id"])
        return result

    b.rt.list_device_review_checkpoints = revoke_after_read
    async with client(b) as phone:
        response = await phone.get("/api/approvals?session_id=origin-A&task_review_version=1")
    assert response.status_code == 403 and b.pending["request_id"] not in response.text
    assert b.calls == []


async def test_renewal_response_loss_is_not_an_action_success_or_safe_retry(review_boundary):
    b = review_boundary
    b.orch.tool_runner._pending_approvals.clear()
    b.orch.tool_runner._pending_phone_reviews.clear()
    renew = b.rt.renew_device_model_review

    async def revoke_after_renew(*args, **kwargs):
        result = await renew(*args, **kwargs)
        assert result["review_renewal"]["status"] == "waiting"
        b.store.revoke_device(b.own["device_id"])
        return result

    b.rt.renew_device_model_review = revoke_after_renew
    async with client(b) as phone:
        response = await phone.post(f"/api/approvals/{b.pending['request_id']}/renew", json=decision_body(b))
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["processing_outcome"] == "outcome_unknown" and detail["retry_safe"] is False
    assert detail["action_outcome"] == "not_asserted" and detail["effects_may_have_occurred"] is False
    assert b.calls == []


async def test_reopened_checkpoint_is_discovered_and_explicitly_renewed_without_goal_replay(review_boundary):
    b = review_boundary
    old = b.pending["request_id"]
    fresh = Orchestrator(skill_registry=b.orch.skills, send_to_client=AsyncMock(), daemons={})
    reopened = TaskFlowRuntime(db_path=b.rt._db_path, skill_registry=b.orch.skills, orchestrator=fresh)
    fresh.taskflows, fresh.executor = reopened, b.orch.executor
    fresh._try_genui_for_result, fresh._push_approval_resolved = AsyncMock(), AsyncMock()
    fresh._summarize_action_result = lambda *_: "Inert receipt"
    fresh._send_text = AsyncMock()
    fresh._handle_command_impl = AsyncMock(return_value="Must not rerun goal")
    b.brain.orchestrator, b.brain.taskflows = fresh, reopened
    reopened._recover_after_restart()
    assert fresh.tool_runner.list_pending() == []
    try:
        await _assert_renewed_http_review(b, old)
        fresh._handle_command_impl.assert_not_awaited()
    finally:
        await reopened.stop()
        reopened._conn.close()
        await fresh.drain_background_tasks()


async def _assert_renewed_http_review(b, old):
    async with client(b) as phone:
        listed = await phone.get("/api/approvals?session_id=origin-A&task_review_version=1")
        row = listed.json()["approvals"][0]
        assert row["status"] == "renewal_required" and row["approval_available"] is False
        assert row["task_review"] == b.review
        renewed = await phone.post(f"/api/approvals/{old}/renew", json=decision_body(b))
        assert renewed.status_code == 200, renewed.text
        fresh = renewed.json()
        assert fresh["request_id"] != old and fresh["session_id"] == "origin-A"
        assert b.calls == []
        assert (await phone.post(f"/api/approvals/{old}/approve", json=decision_body(b))).status_code == 403
        response = await phone.post(f"/api/approvals/{fresh['request_id']}/approve",
            json={"session_id": "origin-A", "task_review": fresh["task_review"]})
        assert response.status_code == 200, response.text
    assert len(b.calls) == 1


@pytest.mark.parametrize("kind", ["approval_request", "approval_resolved"])
def test_registered_hup_sends_only_current_authenticated_origin_device(tmp_path, kind):
    brain = _state()
    store = brain.device_pairing_store = DevicePairingStore(db_path=str(tmp_path / "hup-paired.db"))
    own = store.pair_device("Owner HUP fixture")
    foreign = store.pair_device("Foreign HUP fixture")
    brain.push_to_review_device = MethodType(BrainState.push_to_review_device, brain)
    brain._queue_dict_to_node = MethodType(BrainState._queue_dict_to_node, brain)
    card = {"version": 1, "request_id": str(uuid4()), "origin_session_id": "shared",
            "kind": "task_start", "turn_id": str(uuid4()), "terms_digest": "a" * 64}
    frame = {"type": kind, "payload": {"request_id": card["request_id"], "session_id": "shared", "task_review": card}}
    source = {"version": 1, "kind": "paired_device", "device_id": own["device_id"]}
    with _node_client(brain) as app:
        # The helper installs a legacy inert store; replace it before admission
        # so the registered route authenticates these real disposable credentials.
        brain.device_pairing_store = store
        with app.websocket_connect(f"/v1/node?api_key={own['token']}") as owner_socket:
            _register_node(owner_socket, "owner-alias", "phone")
            with app.websocket_connect(f"/v1/node?api_key={foreign['token']}") as foreign_socket:
                _register_node(foreign_socket, "foreign-alias", "phone")
                foreign_peer = brain.daemons["foreign-alias"]
                foreign_peer.send_json = AsyncMock(wraps=foreign_peer.send_json)
                brain._daemon_session_bindings = {"owner-alias": {"shared"}, "foreign-alias": {"shared"}}
                assert owner_socket.portal.call(brain.push_to_review_device, source, frame) is True
                received = owner_socket.receive_json()
                assert received["type"] == kind and received["payload"]["task_review"] == card
                assert own["device_id"] not in json.dumps(received)
                foreign_peer.send_json.assert_not_awaited()
                model = ApprovalRequestPayload if kind == "approval_request" else ApprovalResolvedPayload
                assert model.model_validate(received["payload"]).task_review == card
                store.revoke_device(own["device_id"])
                assert owner_socket.portal.call(brain.push_to_review_device, source, frame) is False
                foreign_peer.send_json.assert_not_awaited()


async def test_audience_socket_replacement_cannot_retarget_to_a_new_peer():
    brain = BrainState.__new__(BrainState)
    brain.daemons = {}
    socket = SimpleNamespace(send_json=AsyncMock())
    replacement = SimpleNamespace(send_json=AsyncMock())
    source = PairedDevicePrincipal(str(uuid4()), lambda: True)
    intake = PhoneChatIntake(brain, socket, lambda: brain)
    intake.node_id = "same-alias"
    socket._feral_phone_chat_intake = intake
    brain.daemons["same-alias"] = socket
    intake.bind_review_principal(source)
    queue = brain._queue_dict_to_node
    async def swapped(node, frame, **kwargs):
        brain.daemons[node] = replacement
        return await queue(node, frame, **kwargs)
    brain._queue_dict_to_node = swapped
    assert await brain.push_to_review_device(source.storage_binding(), {"type": "approval_request", "payload": {}}) is False
    socket.send_json.assert_not_awaited()
    replacement.send_json.assert_not_awaited()
    assert intake.review_current({**source.storage_binding(), "version": True}) is False


async def test_captured_review_runtime_replacement_refuses_even_same_device_socket():
    brain = BrainState.__new__(BrainState)
    brain.daemons, brain.orchestrator, brain.memory, brain.taskflows = {}, object(), None, object()
    socket = SimpleNamespace(send_json=AsyncMock())
    intake = PhoneChatIntake(brain, socket, lambda: brain)
    intake.node_id = "own"
    source = PairedDevicePrincipal(str(uuid4()), lambda: True)
    socket._feral_phone_chat_intake = intake
    brain.daemons["own"] = socket
    intake.bind_review_principal(source)
    brain.orchestrator = object()
    assert await brain.push_to_review_device(source.storage_binding(), {"type": "approval_request", "payload": {}}) is False
    socket.send_json.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["bool_version", "unknown", "bool_step", "bad_digest"])
def test_task_review_wire_projection_rejects_ambiguous_fields(mutation):
    card = {"version": 1, "request_id": str(uuid4()), "origin_session_id": "origin-A",
            "kind": "taskflow_action", "flow_id": str(uuid4()), "step_id": 1,
            "action_id": "inert-action", "terms_digest": "a" * 64}
    if mutation == "bool_version":
        card["version"] = True
    elif mutation == "unknown":
        card["source_binding"] = {"device_id": "forged"}
    elif mutation == "bool_step":
        card["step_id"] = True
    else:
        card["terms_digest"] = "A" * 64
    with pytest.raises(ValueError):
        TaskReviewPayload.model_validate(card)
