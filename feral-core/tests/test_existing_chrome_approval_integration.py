"""Registered approval routes reach the real selected-Chrome action boundary.

Only the WebSocket peer is synthetic. No browser process/account is accessed.
"""
from unittest.mock import AsyncMock

import httpx
import pytest_asyncio
from fastapi import FastAPI

from agents.orchestrator import Orchestrator
from api.routes.approvals import router as approvals_router
from api.routes.tools import router as tools_router
from api.state import state
from security.exec_approvals import ApprovalManager
from security.trust_ledger import TrustLedger
from skills.call_context import current_context
from skills.impl import SKILL_IMPLEMENTATIONS, get_implementation
from skills.registry import SkillRegistry
from tests.test_existing_chrome_connection import fixture as _chrome_fixture, owner

chrome_fixture = _chrome_fixture


@pytest_asyncio.fixture
async def registered_chrome(chrome_fixture, monkeypatch):
    connector, sockets, _endpoints, _profile = chrome_fixture
    with owner():
        await connector.connect(True)
        await connector.select("page-a")
    registry = SkillRegistry()
    registry.load_builtin_skills()
    orch = Orchestrator(skill_registry=registry, send_to_client=AsyncMock(), daemons={},
                        memory=None, vision_buffer=None, perception=None, learner=None,
                        approval_manager=ApprovalManager(db_path=":memory:"))
    orch.tool_runner._autonomy_mode = "strict"
    orch.tool_runner._trust = TrustLedger(persist=False)
    orch._send_text = AsyncMock()
    orch._try_genui_for_result = AsyncMock()
    monkeypatch.setattr(state, "orchestrator", orch)
    monkeypatch.setattr(state, "skill_registry", registry)
    monkeypatch.setattr(state, "browser", connector.controller)
    # Restore both registrations/injection after using the actual production bridge.
    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, "browser", SKILL_IMPLEMENTATIONS.get("browser"))
    web_actions = get_implementation("web_actions")
    if web_actions is not None:
        monkeypatch.setattr(web_actions, "_browser", web_actions._browser)
    state._bind_browser_bridge("browser")
    orch._browser_resource_supplier = lambda: state.browser.approval_binding()
    app = FastAPI()
    app.include_router(tools_router)
    app.include_router(approvals_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield client, orch, connector, sockets[0]
        finally:
            await connector.disconnect()
            await orch.executor.close()


BODY = {"tool_name": "browser__click_at", "args": {"x": 17, "y": 23},
        "session_id": "owner-a", "confirm": True}


def input_packets(socket):
    return [packet for packet in socket.sent if packet["method"] == "Input.dispatchMouseEvent"]


async def queue(client, connector, socket):
    assert current_context().session_id != "owner-a"
    result = await client.post("/api/tools/execute", json=BODY)
    assert result.status_code == 200
    pending = result.json()
    assert pending["status"] == "pending_approval"
    assert pending["session_id"] == "owner-a"
    # This exact live metadata read intentionally has no task action context.
    assert pending["browser_resource"] == connector.controller.approval_binding()
    assert input_packets(socket) == []
    return pending


async def test_registered_exact_resource_approval_outside_action_context_executes_once(registered_chrome):
    client, orch, connector, socket = registered_chrome
    pending = await queue(client, connector, socket)
    inbox = (await client.get("/api/approvals")).json()["approvals"]
    row = next(row for row in inbox if row["request_id"] == pending["request_id"])
    assert row["session_id"] == "owner-a"
    assert orch.tool_runner.pending_context_valid(orch.tool_runner.get_pending(row["request_id"]))
    path = f"/api/approvals/{row['request_id']}/approve"
    foreign = await client.post(path, json={"session_id": "foreign"})
    assert foreign.status_code == 409 and input_packets(socket) == []
    assert current_context().session_id != "owner-a"
    approved = await client.post(path, json={"session_id": row["session_id"]})
    assert approved.status_code == 200
    receipt = approved.json()
    assert receipt["status"] == "approved" and receipt["result"]["success"] is True
    packets = input_packets(socket)
    assert len(packets) == 2  # One click: one press and one release.
    assert [packet["params"]["type"] for packet in packets] == ["mousePressed", "mouseReleased"]
    assert all(packet["sessionId"] == "session-page-a" for packet in packets)
    assert all(packet["params"]["x"] == 17 and packet["params"]["y"] == 23 for packet in packets)
    assert not orch.tool_runner._approval_mgr.check_approval(BODY["tool_name"], "owner-a")[0]
    duplicate = await client.post(path, json={"session_id": row["session_id"]})
    assert duplicate.status_code == 404 and len(input_packets(socket)) == 2


async def test_registered_reselection_A_B_A_cannot_reuse_original_approval(registered_chrome):
    client, orch, connector, socket = registered_chrome
    pending = await queue(client, connector, socket)
    original = dict(pending["browser_resource"])
    with owner():
        await connector.select("page-b")
        await connector.select("page-a")
    state.browser = connector.controller  # Same production publication reference.
    replacement = connector.controller.approval_binding()
    assert replacement["target_id"] == original["target_id"]
    assert replacement["connection_id"] != original["connection_id"]
    assert not orch.tool_runner.pending_context_valid(orch.tool_runner.get_pending(pending["request_id"]))
    refused = await client.post(f"/api/approvals/{pending['request_id']}/approve", json={"session_id": "owner-a"})
    assert refused.status_code == 404
    assert input_packets(socket) == []
    assert not orch.tool_runner._approval_mgr.check_approval(BODY["tool_name"], "owner-a")[0]
