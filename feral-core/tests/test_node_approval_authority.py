"""Actual node ingress cannot manufacture per-review device ownership."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from agents.orchestrator import Orchestrator
from security.approval_ingress import (
    begin_node_approval_ingress,
    device_approval_authority_unavailable,
    end_node_approval_ingress,
)
from tests.test_hup_protocol import _TEST_NODE_KEY, _register_node
from tests.test_phone_chat_responsive_intake import _chat, _state
from tests.test_taskflow_model_steps import action, goal, wired as taskflow_wired  # noqa: F401

pytestmark = [pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]


@pytest.mark.usefixtures("taskflow_wired")
@pytest.mark.parametrize("ack", ["yes", "no"])
@pytest.mark.parametrize("channel", ["chat_request", "text_command"])
@pytest.mark.parametrize("credential_kind", ["phone_bearer", "pair_token", "legacy_key"])
async def test_node_ack_cannot_resolve_another_pending_review(
    request, monkeypatch, ack, channel, credential_kind
):
    from api import server, state as state_module

    rt, original, calls = request.getfixturevalue("taskflow_wired")
    orch = Orchestrator(
        skill_registry=original.skills, send_to_client=AsyncMock(),
        daemons={}, taskflows=rt,
    )
    orch.executor = original.executor
    rt._orchestrator = orch
    flow_id = goal(rt)
    command_impl = orch._handle_command_impl

    async def inert_model(session, text, context=None):
        await action(orch, session)
        return "Waiting for exact review"

    orch._handle_command_impl = inert_model
    await rt._run_flow(flow_id)
    pending = orch.tool_runner.list_pending()[0]
    assert calls == []
    orch._handle_command_impl = command_impl
    orch._call_llm_chat = AsyncMock(side_effect=AssertionError("No model fallback"))
    acknowledged = threading.Event()
    orch._send_text = AsyncMock(side_effect=lambda *args: acknowledged.set())
    brain = _state()
    brain.orchestrator, brain.memory, brain.taskflows = orch, None, rt
    token = _TEST_NODE_KEY if credential_kind == "legacy_key" else "inert-device-token"
    brain.device_pairing_store = SimpleNamespace(
        verify_device=lambda value: "foreign-device" if credential_kind == "pair_token" and value == token else None,
        verify_phone_bearer=lambda value: "foreign-device" if credential_kind == "phone_bearer" and value == token else None,
    )
    monkeypatch.setattr(server, "state", brain)
    monkeypatch.setattr(state_module, "state", brain)
    monkeypatch.setattr(server, "NODE_API_KEY", _TEST_NODE_KEY)
    # Even a broadcast binding to the execution SID must not confer authority.
    brain.sessions[pending["session_id"]] = object()
    app = FastAPI()
    app.websocket("/v1/node")(server.daemon_session)
    try:
        with TestClient(app) as client:
            with client.websocket_connect(f"/v1/node?api_key={token}") as socket:
                _register_node(socket, "foreign-phone", "phone")
                if channel == "chat_request":
                    _chat(socket, text=ack, session=pending["session_id"], reply="device-ack")
                    reply = socket.receive_json()
                    assert reply["type"] == "chat_response"
                else:
                    socket.send_json({"type": "text_command", "payload": {
                        "text": ack, "context": {
                            "paired_device_id": "claimed-owner", "owner_verified": True,
                            "source": "native_operator", "session_id": pending["session_id"],
                        },
                    }})
                    assert acknowledged.wait(3), "Acknowledgement was not consumed"
                assert calls == []
                assert orch.tool_runner.get_pending(pending["request_id"]) == pending
                orch._call_llm_chat.assert_not_awaited()
        assert not device_approval_authority_unavailable()
    finally:
        await orch.drain_background_tasks()


@pytest.mark.usefixtures("taskflow_wired")
async def test_node_child_keeps_restriction_after_parent_resets_and_operator_retains_authority(request):
    rt, orch, calls = request.getfixturevalue("taskflow_wired")
    flow_id = goal(rt)

    async def inert_model(session, text, context=None):
        await action(orch, session)
        return "Waiting for exact review"

    orch._handle_command_impl = inert_model
    await rt._run_flow(flow_id)
    pending = orch.tool_runner.list_pending()[0]
    release = asyncio.Event()

    async def child():
        await release.wait()
        return await orch.resolve_tool_approval_request(
            pending["request_id"], approved=True, session_id=pending["session_id"]
        )

    token = begin_node_approval_ingress()
    task = asyncio.create_task(child())
    # Retained here until awaited; no unowned production background task.
    end_node_approval_ingress(token)
    release.set()
    assert await task == {"status": "approval_device_authority_unavailable"}
    assert calls == []
    assert orch.tool_runner.get_pending(pending["request_id"]) == pending
    assert not device_approval_authority_unavailable()
    accepted = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )
    assert accepted["status"] == "approved"
    assert len(calls) == 1


async def test_node_resolver_refuses_before_pending_lookup():
    orch = Orchestrator.__new__(Orchestrator)
    token = begin_node_approval_ingress()
    try:
        for approved in (True, False):
            assert await orch.resolve_tool_approval_request(
                "unknown-or-private", approved=approved, session_id="claimed-owner",
                actor="native_operator",
            ) == {"status": "approval_device_authority_unavailable"}
    finally:
        end_node_approval_ingress(token)
