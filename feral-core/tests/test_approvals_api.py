"""REST coverage for execution-approval inbox routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.orchestrator import Orchestrator
from api.routes.approvals import router as approvals_router
from security.exec_approvals import ApprovalManager

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def orchestrator() -> Orchestrator:
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    orch = Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
        approval_manager=ApprovalManager(db_path=":memory:"),
    )
    orch._send_text = AsyncMock()
    orch._try_genui_for_result = AsyncMock()
    orch._execute_tool_call_for_llm = AsyncMock(
        return_value={"success": True, "data": {"note": "executed"}},
    )
    return orch


@pytest.fixture
def approvals_client(orchestrator: Orchestrator):
    app = FastAPI()
    app.include_router(approvals_router)
    fake_state = SimpleNamespace(orchestrator=orchestrator)
    with patch("api.routes.approvals.state", fake_state):
        yield TestClient(app, raise_server_exceptions=False), orchestrator


def _new_pending(orchestrator: Orchestrator, session_id: str, tool: str = "browser__navigate") -> dict:
    pending = orchestrator.tool_runner.enforce_safety(
        tool,
        {"url": "https://example.com"},
        session_id=session_id,
    )
    assert pending is not None
    assert pending.get("status") == "pending_approval"
    return pending


def test_list_pending_approvals_and_session_filter(approvals_client):
    client, orch = approvals_client
    _new_pending(orch, "s1")
    _new_pending(orch, "s2")

    all_rows = client.get("/api/approvals")
    assert all_rows.status_code == 200
    payload = all_rows.json()
    assert payload["count"] == 2

    s1_rows = client.get("/api/approvals?session_id=s1")
    assert s1_rows.status_code == 200
    body = s1_rows.json()
    assert body["count"] == 1
    assert body["approvals"][0]["session_id"] == "s1"


def test_approve_pending_request_executes_tool(approvals_client):
    client, orch = approvals_client
    pending = _new_pending(orch, "s-approve")

    r = client.post(
        f"/api/approvals/{pending['request_id']}/approve",
        json={"session_id": "s-approve"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["status"] == "approved"
    assert body["request_id"] == pending["request_id"]
    orch._execute_tool_call_for_llm.assert_awaited_once()
    assert orch.tool_runner.get_pending(pending["request_id"]) is None


def test_reject_pending_request_does_not_execute_tool(approvals_client):
    client, orch = approvals_client
    pending = _new_pending(orch, "s-reject")

    r = client.post(
        f"/api/approvals/{pending['request_id']}/reject",
        json={"session_id": "s-reject"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["status"] == "rejected"
    orch._execute_tool_call_for_llm.assert_not_awaited()
    assert orch.tool_runner.get_pending(pending["request_id"]) is None


def test_approve_with_session_mismatch_returns_409(approvals_client):
    client, orch = approvals_client
    pending = _new_pending(orch, "s1")

    r = client.post(
        f"/api/approvals/{pending['request_id']}/approve",
        json={"session_id": "s2"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "session_mismatch"
    assert detail["pending_session_id"] == "s1"
    assert orch.tool_runner.get_pending(pending["request_id"]) is not None


def test_unknown_approval_returns_404(approvals_client):
    client, _orch = approvals_client
    r = client.post("/api/approvals/does-not-exist/approve")
    assert r.status_code == 404


def test_rest_bound_dispatch_to_native_shaped_approval_executes_once(monkeypatch):
    from api.routes import tools
    from api.state import state
    from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
    from skills.base import BaseSkill
    from skills.call_context import current_context
    from skills.impl import SKILL_IMPLEMENTATIONS
    from skills.registry import SkillRegistry

    class RecordingAction(BaseSkill):
        def __init__(self):
            super().__init__(skill_id="rest_bound_contract")
            self.calls = []

        async def execute(self, endpoint_id, args, vault):
            self.calls.append((endpoint_id, dict(args), current_context().session_id))
            return {"success": True, "data": {"verified": True}}

    reg = SkillRegistry()
    reg.register(SkillManifest(
        skill_id="rest_bound_contract", brand=BrandProfile(name="Synthetic action", primary_color="#111"),
        description="Isolated approval contract", endpoints=[SkillEndpoint(
            id="write", method="PYTHON", url="python://rest_bound_contract/write",
            description="Synthetic mutation", safety_tier="confirm",
        )],
    ))
    action = RecordingAction()
    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, "rest_bound_contract", action)
    orch = Orchestrator(skill_registry=reg, send_to_client=AsyncMock(), daemons={}, memory=None,
                        vision_buffer=None, perception=None, learner=None,
                        approval_manager=ApprovalManager(db_path=":memory:"))
    orch.tool_runner._autonomy_mode = "strict"
    orch._send_text = AsyncMock()
    orch._try_genui_for_result = AsyncMock()
    monkeypatch.setattr(state, "orchestrator", orch)
    monkeypatch.setattr(state, "skill_registry", reg)
    fake_state = SimpleNamespace(orchestrator=orch)
    monkeypatch.setattr("api.routes.approvals.state", fake_state)
    app = FastAPI()
    app.include_router(tools.router)
    app.include_router(approvals_router)
    with patch.dict("os.environ", {"FERAL_TOOL_CALL_CONTEXT": "on"}), TestClient(app) as client:
        dispatch = {"tool_name": "rest_bound_contract__write", "confirm": True}
        invalid = client.post("/api/tools/execute", json=dispatch).json()
        assert invalid["error_code"] == "context_invalid_session"
        assert orch.tool_runner.list_pending() == [] and action.calls == []
        with patch.dict("os.environ", {"FERAL_TOOL_CALL_CONTEXT": "off"}):
            disabled = client.post("/api/tools/execute", json={**dispatch, "session_id": "real-caller"}).json()
            assert disabled["error_code"] == "context_binding_disabled"
            assert orch.tool_runner.list_pending() == [] and action.calls == []
        pending = client.post("/api/tools/execute", json={**dispatch, "session_id": "real-caller"}).json()
        assert pending["status"] == "pending_approval" and pending["session_id"] == "real-caller"
        assert action.calls == []
        row = client.get("/api/approvals").json()["approvals"][0]
        assert row["request_id"] == pending["request_id"] and row["session_id"] == "real-caller"
        path = f"/api/approvals/{row['request_id']}/approve"
        foreign = client.post(path, json={"session_id": "foreign-caller"})
        assert foreign.status_code == 409 and action.calls == []
        assert orch.tool_runner.get_pending(row["request_id"]) is not None
        approved = client.post(path, json={"session_id": row["session_id"]})
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["result"]["success"] is True
        assert action.calls == [("write", {}, "real-caller")]
        assert client.post(path, json={"session_id": row["session_id"]}).status_code == 404
        assert len(action.calls) == 1


def test_legacy_empty_session_cannot_be_approved_by_native_body(approvals_client):
    client, orch = approvals_client
    pending = _new_pending(orch, "")
    response = client.post(f"/api/approvals/{pending['request_id']}/approve", json={"session_id": ""})
    assert response.status_code == 422
    orch._execute_tool_call_for_llm.assert_not_awaited()
    assert orch.tool_runner.get_pending(pending["request_id"]) is not None


def test_list_surfaces_policy_sources(approvals_client):
    """The inbox must return the field ToolRunner records for it.

    `enforce_safety` attaches `policy_sources` to every pending row with a
    comment saying renderers use it to answer "why are we asking?", but the
    GET projection listed each field by hand and omitted it. The data was
    therefore unreachable over HTTP, and the only client that reads it
    rendered an empty explanation on every row with no error anywhere.

    Comparing against the runner's own copy rather than a literal keeps
    this honest if the resolver's output shape changes.
    """
    client, orch = approvals_client
    pending = _new_pending(orch, "sess-sources")
    recorded = pending.get("policy_sources")
    assert recorded, "enforce_safety should record policy_sources on a pending row"

    r = client.get("/api/approvals")
    assert r.status_code == 200
    rows = r.json()["approvals"]
    row = next(x for x in rows if x["request_id"] == pending["request_id"])

    assert "policy_sources" in row, "GET /api/approvals dropped policy_sources"
    assert row["policy_sources"] == recorded


def test_policy_sources_is_json_safe(approvals_client):
    """Whatever the resolver puts in there has to survive serialisation.

    `sources` is a `dict[str, Any]` and mixes a nested dict (`manifest`),
    strings (`danger_map`) and booleans (`surface_deny`). Passing it
    through unchanged is only safe while every value is JSON-encodable, so
    this pins that rather than assuming it.
    """
    import json

    client, orch = approvals_client
    _new_pending(orch, "sess-json")
    row = client.get("/api/approvals").json()["approvals"][0]
    json.dumps(row["policy_sources"])  # raises if anything in there is not serialisable


def test_every_listed_field_reaches_the_client(approvals_client):
    """A hand-written projection drops fields silently; name what it owes.

    This is the guard for the whole defect class, not just this instance:
    the bug was a maintainer adding a field to the pending row and the
    route's literal dict not growing to match.
    """
    client, orch = approvals_client
    _new_pending(orch, "sess-fields")
    row = client.get("/api/approvals").json()["approvals"][0]
    expected = {
        "request_id", "session_id", "tool_name", "args",
        "safety_level", "created_at", "status", "policy_sources",
    }
    assert expected <= set(row), f"missing from the response: {expected - set(row)}"
