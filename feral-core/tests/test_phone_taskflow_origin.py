"""Real tracked/private TaskFlow identity; no external tools or providers."""
import asyncio
from dataclasses import asdict
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agents.chat_turns import TrackedTaskOriginGuard, observe_tool_result, turn_audit
from agents.taskflow import TaskFlowHandoffConflict, TaskFlowRuntime
from api.routes import taskflows
from security.approval_ingress import (
    PairedDevicePrincipal, begin_node_approval_ingress, end_node_approval_ingress,
    device_approval_authority_unavailable,
)
from security.device_pairing import DevicePairingStore
from skills.call_context import bind_context
from tests import test_taskflow_origin_handoff, test_task_approval_origin_transfer

rig = test_taskflow_origin_handoff.rig
wired_origin = test_task_approval_origin_transfer.wired_origin
approve_http = test_task_approval_origin_transfer.approve_http

pytestmark = pytest.mark.no_auto_feral_home


def paired(tmp_path, name="phone"):
    store = DevicePairingStore(db_path=str(tmp_path / (name + ".db")))
    issued = store.pair_device(name, kind="browser_node_v2")
    assert store.verify_phone_bearer(issued["phone_bearer"]) == issued["device_id"]
    principal = PairedDevicePrincipal(issued["device_id"], lambda: store.admitted_credential_current(
        device_id=issued["device_id"], credential=issued["phone_bearer"], bearer_kind="phone_bearer"))
    return principal, store


async def phone_turn(brain, manager, source, operation, *, sid="shared"):
    brain.sessions.setdefault(sid, object())  # The Mac remains the routing owner.
    native = brain.sessions[sid]
    results = []
    async def run():
        results.append(await operation())
        return "inert processing finished"
    token = begin_node_approval_ingress(source)
    try:
        receipt = await manager.submit(owner=object(), session_id=sid, request_id=str(uuid4()),
            terms={"text": "inert task"}, run=run, emit=AsyncMock(), source_principal=source)
        await manager._live[(sid, receipt["turn_id"])].task
    finally:
        end_node_approval_ingress(token)
    assert brain.sessions[sid] is native
    return results, receipt


async def backing(skill, endpoint="start", args=None, sid="shared"):
    with bind_context(session_id=sid, surface="brain_host",
                      tool_name="background_task__" + endpoint, call_id="stable-call"):
        return await skill.execute(endpoint, args or {"goal": "inert goal"}, {})


async def test_direct_verified_phone_shared_native_owner_private_commit_exact_retry(rig, tmp_path):
    brain, manager, skill = rig
    source, _ = paired(tmp_path)
    async def operation():
        first = await backing(skill)
        second = await backing(skill)
        conflict = await backing(skill, args={"goal": "changed goal"})
        return first, second, conflict
    results, receipt = await phone_turn(brain, manager, source, operation)
    first, second, conflict = results[0]
    assert first["success"] and second["success"]
    assert first["data"]["flow_id"] == second["data"]["flow_id"]
    assert second["data"]["handoff"]["replayed"] is True
    assert conflict["success"] is False and conflict["data"]["error_code"] == "task_handoff_conflict"
    flow_id = first["data"]["flow_id"]
    runtime = brain.taskflows
    binding = json.loads(runtime._conn.execute("SELECT origin_principal_json FROM taskflows WHERE id=?", (flow_id,)).fetchone()[0])
    assert binding == source.storage_binding()
    assert runtime.flow_matches_source(flow_id, source)
    assert len(runtime.list_flows()) == 1
    assert source.device_id not in json.dumps(first)
    assert source.device_id not in json.dumps(runtime.get_flow(flow_id))
    assert source.device_id not in json.dumps(runtime.read_origin_receipt("shared", flow_id))
    assert first["data"]["handoff"]["origin"]["turn_id"] == receipt["turn_id"]


async def test_reviewed_creation_keeps_private_phone_source_and_device_denial(wired_origin, tmp_path):
    brain, manager, orch = wired_origin
    source, _ = paired(tmp_path)
    async def propose():
        pending = await orch.tool_runner.execute_tool_call_for_llm("thread-A",
            {"id": "original-call", "name": "background_task__start", "args": {"goal": "inert reviewed goal"}},
            [], surface="http_api")
        observe_tool_result("thread-A", pending)
        return pending
    results, original = await phone_turn(brain, manager, source, propose, sid="thread-A")
    pending = results[0]
    assert pending["status"] == "pending_approval"
    token = begin_node_approval_ingress(source)
    try:
        denied = await orch.resolve_tool_approval_request(pending["request_id"], approved=True, session_id="thread-A")
        assert denied["status"] == "approval_device_authority_unavailable"
        assert device_approval_authority_unavailable()
    finally:
        end_node_approval_ingress(token)
    approved = await approve_http(pending)  # Existing local operator route only.
    assert approved.status_code == 200
    data = approved.json()["result"]["data"]
    assert data["handoff"]["origin"]["turn_id"] == original["turn_id"]
    assert brain.taskflows.flow_matches_source(data["flow_id"], source)
    assert source.device_id not in approved.text
    assert (await approve_http(pending)).status_code == 404
    assert len(brain.taskflows.list_flows()) == 1


async def test_foreign_and_legacy_null_are_excluded_from_device_discovery(rig, tmp_path):
    brain, manager, skill = rig
    owner, _ = paired(tmp_path, "owner")
    foreign, _ = paired(tmp_path, "foreign")
    results, _ = await phone_turn(brain, manager, owner, lambda: backing(skill))
    owned_id = results[0]["data"]["flow_id"]
    legacy = brain.taskflows.create_flow(session_id="", title="legacy", steps=[{"type": "note.save", "text": "inert"}], origin_session_id="shared")
    async def foreign_read():
        status = await backing(skill, "status", {"flow_id": owned_id})
        listing = await backing(skill, "list", {"limit": 20})
        return status, listing
    results, _ = await phone_turn(brain, manager, foreign, foreign_read)
    status, listing = results[0]
    assert status["success"] is False and status["data"]["error_code"] == "task_not_found"
    assert listing["data"]["tasks"] == []
    results, _ = await phone_turn(brain, manager, owner, lambda: backing(skill, "list", {"limit": 20}))
    assert [row["flow_id"] for row in results[0]["data"]["tasks"]] == [owned_id]
    assert not brain.taskflows.flow_matches_source(legacy["id"], owner)
    assert len(brain.taskflows.list_origin_flows("shared")) == 2  # Local operator unchanged.


async def test_raw_taskflow_rest_remains_operator_only(rig, tmp_path, monkeypatch):
    from api import server, state as state_module
    brain, manager, skill = rig
    source, _ = paired(tmp_path)
    results, _ = await phone_turn(brain, manager, source, lambda: backing(skill))
    flow_id = results[0]["data"]["flow_id"]
    foreign, pairing = paired(tmp_path, "foreign")
    issued = pairing.rotate_phone_bearer(foreign.device_id)
    assert issued is not None
    monkeypatch.setattr(brain, "device_pairing_store", pairing, raising=False)
    monkeypatch.setattr(state_module, "state", brain)
    app = FastAPI()
    app.include_router(taskflows.router)
    app.add_middleware(server.APIKeyMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app, client=("198.51.100.9", 4321)),
                           base_url="http://fixture.invalid") as client:
        headers = {"Authorization": "Bearer " + issued["phone_bearer"]}
        for method, path in (("GET", "/api/taskflows"), ("GET", "/api/taskflows/" + flow_id),
                             ("POST", "/api/taskflows/" + flow_id + "/cancel"),
                             ("POST", "/api/taskflows/" + flow_id + "/resume")):
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 401 and flow_id not in response.text
        local = await client.get("/api/taskflows/" + flow_id,
                                 headers={"Authorization": "Bearer " + server.FERAL_API_KEY})
        assert local.status_code == 200 and local.json()["id"] == flow_id
    assert brain.taskflows.get_flow(flow_id)["status"] == "queued"


async def test_binding_persists_reopen_and_conflicts_on_foreign_same_handoff(rig, tmp_path):
    brain, manager, skill = rig
    source, pairing = paired(tmp_path)
    results, _ = await phone_turn(brain, manager, source, lambda: backing(skill))
    flow_id = results[0]["data"]["flow_id"]
    flow = brain.taskflows.get_flow(flow_id)
    origin = flow["context"]["task_origin"]
    foreign, _ = paired(tmp_path, "foreign")
    reopened = TaskFlowRuntime(db_path=brain.taskflows._db_path)
    # A restart reacquires authenticated identity, never restores old callable authority.
    issued = pairing.rotate_phone_bearer(source.device_id)
    assert issued is not None
    assert pairing.verify_phone_bearer(issued["phone_bearer"]) == source.device_id
    reacquired = PairedDevicePrincipal(source.device_id, lambda: pairing.admitted_credential_current(
        device_id=source.device_id, credential=issued["phone_bearer"], bearer_kind="phone_bearer"))
    kwargs = dict(session_id="", title=flow["title"], steps=[{"type": "llm.chat", "prompt": "inert goal"}],
                  context=flow["context"], handoff_key=origin["handoff_key"], terms_digest=origin["terms_digest"],
                  origin_session_id="shared", origin_surface="brain_host", creation_guard=lambda: None)
    try:
        assert reopened.flow_matches_source(flow_id, reacquired)
        assert reopened.create_flow(**kwargs, source_principal=reacquired)["handoff_replayed"]
        with pytest.raises(TaskFlowHandoffConflict):
            reopened.create_flow(**kwargs, source_principal=foreign)
        with pytest.raises(TaskFlowHandoffConflict):
            reopened.create_flow(**kwargs)  # A missing binding cannot demote an existing owner.
        assert len(reopened.list_flows()) == 1
    finally:
        await reopened.stop()
        reopened._conn.close()


async def test_migration_preserves_null_local_flow_without_device_upgrade(rig, tmp_path):
    runtime = rig[0].taskflows
    old = runtime.create_flow(session_id="local", title="old", steps=[{"type": "note.save", "text": "inert"}], origin_session_id="shared")
    runtime._conn.execute("ALTER TABLE taskflows DROP COLUMN origin_principal_json")
    runtime._conn.commit()
    reopened = TaskFlowRuntime(db_path=runtime._db_path)
    source, _ = paired(tmp_path)
    try:
        assert reopened.get_flow(old["id"])["title"] == "old"
        assert reopened._conn.execute("SELECT origin_principal_json FROM taskflows").fetchone()[0] is None
        assert not reopened.flow_matches_source(old["id"], source)
        assert reopened.list_origin_flows("shared", source_principal=source) == []
        assert len(reopened.list_origin_flows("shared")) == 1
    finally:
        await reopened.stop()
        reopened._conn.close()


@pytest.mark.parametrize("claim", ["source_principal", "origin_principal_json", "paired_device_id"])
async def test_request_and_context_private_claims_are_refused(rig, claim):
    brain, _, _ = rig
    raw = await taskflows.create_taskflow({"steps": [{"type": "note.save", "text": "inert"}], claim: "forged"})
    assert "error" in raw and brain.taskflows.list_flows() == []
    for context in ({claim: "forged"}, {"task_origin": {claim: "forged"}}):
        raw = await taskflows.create_taskflow({"steps": [{"type": "note.save", "text": "inert"}], "context": context})
        assert "error" in raw
        with pytest.raises(ValueError):
            brain.taskflows.create_flow(session_id="", title="inert", steps=[{"type": "note.save"}], context=context)
    assert brain.taskflows.list_flows() == []


@pytest.mark.parametrize("source", [{"device_id": "forged"}, lambda: True, "forged"])
async def test_raw_source_cannot_masquerade_as_typed_admission(rig, source):
    with pytest.raises(ValueError):
        rig[0].taskflows.create_flow(session_id="", title="inert", steps=[{"type": "note.save"}], source_principal=source)
    assert rig[0].taskflows.list_flows() == []


async def test_backing_task_args_do_not_supply_private_identity(rig, tmp_path):
    brain, manager, skill = rig
    source, _ = paired(tmp_path)
    async def operation():
        results = []
        for claim in ({"source_principal": lambda: True}, {"paired_device_id": source.device_id},
                      {"context": {"task_origin": {"origin_principal_json": "forged"}}}):
            results.append(await backing(skill, args={"goal": "inert", **claim}))
        return results
    results, _ = await phone_turn(brain, manager, source, operation)
    assert all(not result["success"] for result in results[0])
    assert brain.taskflows.list_flows() == []


async def test_backing_task_requires_source_filtered_committed_receipt(rig, tmp_path):
    brain, manager, skill = rig
    source, _ = paired(tmp_path)
    foreign, _ = paired(tmp_path, "foreign")
    async def operation():
        audit = turn_audit("shared")
        assert audit is not None and audit.source_principal is source
        audit.source_principal = foreign  # An inherited claim alone is not a durable receipt.
        try:
            return await backing(skill)
        finally:
            audit.source_principal = source
    results, _ = await phone_turn(brain, manager, source, operation)
    assert not results[0]["success"] and results[0]["data"]["error_code"] == "task_origin_unavailable"
    assert brain.taskflows.list_flows() == []


async def test_revocation_after_read_await_never_releases_device_status(rig, tmp_path, monkeypatch):
    brain, manager, skill = rig
    source, pairing = paired(tmp_path)
    results, _ = await phone_turn(brain, manager, source, lambda: backing(skill))
    flow_id = results[0]["data"]["flow_id"]
    original = taskflows._owned_thread_call
    async def read_then_revoke(*args):
        result = await original(*args)
        pairing.revoke_device(source.device_id)
        return result
    monkeypatch.setattr(taskflows, "_owned_thread_call", read_then_revoke)
    results, receipt = await phone_turn(brain, manager, source, lambda: backing(skill, "status", {"flow_id": flow_id}))
    assert results == []
    row = await brain.memory.chat_turn_get(session_id="shared", request_id=receipt["request_id"])
    assert row["processing_outcome"] == "cancelled"


async def test_revocation_before_commit_rolls_back_private_flow(rig):
    current = [True]
    source = PairedDevicePrincipal(str(uuid4()), lambda: current[0])
    runtime = rig[0].taskflows
    runtime._conn.create_function("revoke_fixture", 0, lambda: current.__setitem__(0, False) or 0)
    runtime._conn.execute("CREATE TRIGGER revoke_before_commit AFTER INSERT ON taskflow_steps BEGIN SELECT revoke_fixture(); END")
    runtime._conn.commit()
    with pytest.raises(asyncio.CancelledError):
        runtime.create_flow(session_id="", title="inert", steps=[{"type": "llm.chat", "prompt": "inert"}],
            source_principal=source, creation_guard=lambda: None, handoff_key="a" * 64,
            terms_digest="b" * 64, origin_session_id="shared", origin_surface="brain_host")
    assert runtime.list_flows() == []


def test_private_callable_wrapper_excludes_principal_from_dataclass_projection():
    source = PairedDevicePrincipal(str(uuid4()), lambda: True)
    wrapper = TrackedTaskOriginGuard(lambda: None, source)
    assert wrapper.source_principal is source
    assert source.device_id not in repr(wrapper)
    assert source.device_id not in str(asdict(wrapper))
    other = PairedDevicePrincipal(str(uuid4()), lambda: True)
    other_wrapper = TrackedTaskOriginGuard(lambda: None, other)
    assert other_wrapper.source_principal is other and wrapper.source_principal is source
    assert other.device_id not in str(asdict(other_wrapper))
    wrapper()
