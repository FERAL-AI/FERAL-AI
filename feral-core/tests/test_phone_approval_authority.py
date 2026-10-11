"""Actual HTTP middleware and exact dispatcher; providers/effects remain inert."""

from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests import test_taskflow_model_steps as model_steps

wired = model_steps.wired


def http_boundary(monkeypatch, orch, runtime):
    from api import state as state_module, server
    from api.routes import approvals

    brain = SimpleNamespace(
        orchestrator=orch,
        taskflows=runtime,
        device_pairing_store=SimpleNamespace(
            verify_phone_bearer=lambda token: "foreign-device"
            if token == "inert-foreign-token"
            else "origin-device"
            if token == "inert-origin-token"
            else None
        ),
        daemons={},
        _daemon_session_bindings={"foreign-node": {"origin-A", "other-chat"}},
    )
    monkeypatch.setattr(state_module, "state", brain)
    monkeypatch.setattr(approvals, "state", brain)
    app = FastAPI()
    app.include_router(approvals.router)
    app.add_middleware(server.APIKeyMiddleware)
    transport = ASGITransport(app=app, client=("198.51.100.9", 54321))
    return transport, server.FERAL_API_KEY


@pytest.mark.parametrize("token", ["inert-foreign-token", "inert-origin-token"])
@pytest.mark.parametrize(
    "query", ["", "?session_id=origin-A", "?session_id=other-chat"]
)
async def test_paired_inbox_without_durable_principal_is_denied_before_projection(
    wired, monkeypatch, token, query
):
    rt, orch, calls = wired
    flow_id = model_steps.goal(rt)
    _, pending = await model_steps.ask(rt, orch, flow_id)
    transport, _ = http_boundary(monkeypatch, orch, rt)
    async with AsyncClient(
        transport=transport,
        base_url="http://fixture.invalid",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        response = await client.get("/api/approvals" + query)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "approval_device_authority_unavailable"
    assert (
        pending["request_id"] not in response.text
        and "exact terms" not in response.text
    )
    assert (
        calls == [] and orch.tool_runner.get_pending(pending["request_id"]) is not None
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("body_mode", ["missing", "origin", "execution", "forged"])
async def test_paired_resolution_cannot_use_sid_or_owner_body_claims(
    wired, monkeypatch, decision, body_mode
):
    rt, orch, calls = wired
    flow_id = model_steps.goal(rt)
    _, pending = await model_steps.ask(rt, orch, flow_id)
    transport, _ = http_boundary(monkeypatch, orch, rt)
    body = (
        {}
        if body_mode == "missing"
        else {"session_id": "origin-A"}
        if body_mode == "origin"
        else {"session_id": pending["session_id"]}
    )
    if body_mode == "forged":
        body.update(
            flow_id=flow_id,
            owner_verified=True,
            paired_device_id="origin-device",
            task_origin={"surface": "local_cli"},
        )
    async with AsyncClient(
        transport=transport,
        base_url="http://fixture.invalid",
        headers={"Authorization": "Bearer inert-foreign-token"},
    ) as client:
        response = await client.post(
            "/api/approvals/" + pending["request_id"] + "/" + decision, json=body
        )
    assert (
        response.status_code == 403
        and response.json()["detail"]["code"] == "approval_device_authority_unavailable"
    )
    assert (
        calls == [] and orch.tool_runner.get_pending(pending["request_id"]) is not None
    )
    assert (
        rt.read_origin_receipt("origin-A", flow_id)["processing_outcome"]
        == "awaiting_approval"
    )


async def test_operator_correct_sid_executes_once_and_foreground_shape_is_unchanged(
    wired, monkeypatch
):
    rt, orch, calls = wired
    # Ordinary foreground pending: no TaskFlow origin is invented.
    pending = orch.tool_runner.enforce_safety(
        "notes_memory__save_note",
        {"value": "ordinary"},
        session_id="ordinary-chat",
        surface="websocket",
    )
    transport, operator = http_boundary(monkeypatch, orch, rt)
    async with AsyncClient(
        transport=transport,
        base_url="http://fixture.invalid",
        headers={"Authorization": "Bearer " + operator},
    ) as client:
        inbox = await client.get("/api/approvals?session_id=ordinary-chat")
        assert (
            inbox.status_code == 200
            and inbox.json()["approvals"][0]["session_id"] == "ordinary-chat"
        )
        path = "/api/approvals/" + pending["request_id"] + "/approve"
        assert (
            await client.post(path, json={"session_id": "foreign"})
        ).status_code == 409
        approved = await client.post(path, json={"session_id": "ordinary-chat"})
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
        assert (
            await client.post(path, json={"session_id": "ordinary-chat"})
        ).status_code == 404
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["changed_terms", "cancelled"])
async def test_operator_stale_task_review_cannot_dispatch(wired, monkeypatch, mode):
    rt, orch, calls = wired
    flow_id = model_steps.goal(rt)
    _, pending = await model_steps.ask(rt, orch, flow_id)
    if mode == "changed_terms":
        orch.tool_runner._pending_approvals[pending["request_id"]]["args"] = {
            "value": "changed"
        }
    else:
        rt.cancel_flow(flow_id)
    transport, operator = http_boundary(monkeypatch, orch, rt)
    async with AsyncClient(
        transport=transport,
        base_url="http://fixture.invalid",
        headers={"Authorization": "Bearer " + operator},
    ) as client:
        response = await client.post(
            "/api/approvals/" + pending["request_id"] + "/approve",
            json={"session_id": pending["session_id"]},
        )
    assert response.status_code == 404 and calls == []


async def test_paired_legacy_foreground_review_is_explicitly_unavailable(
    wired, monkeypatch
):
    rt, orch, calls = wired
    pending = orch.tool_runner.enforce_safety(
        "notes_memory__save_note",
        {"value": "ordinary"},
        session_id="ordinary-chat",
        surface="websocket",
    )
    transport, _ = http_boundary(monkeypatch, orch, rt)
    async with AsyncClient(
        transport=transport,
        base_url="http://fixture.invalid",
        headers={"Authorization": "Bearer inert-origin-token"},
    ) as client:
        response = await client.post(
            "/api/approvals/" + pending["request_id"] + "/approve",
            json={"session_id": "ordinary-chat"},
        )
    assert response.status_code == 403 and calls == []
