"""Real local grant routes, TaskFlow, runner and selected CDP; fake wire only."""
import asyncio
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from agents.orchestrator import Orchestrator
from agents.taskflow import TaskFlowRuntime
from api.job_chrome_grants import JobChromeError, JobChromeGrants, task_browser_scope
from api.routes import existing_chrome as routes
from api.routes.approvals import router as approvals_router
from api.state import state
from security.approval_ingress import PairedDevicePrincipal
from security.exec_approvals import ApprovalManager
from security.trust_ledger import TrustLedger
from skills.call_context import bind_context
from skills.impl import SKILL_IMPLEMENTATIONS, get_implementation
from skills.impl.existing_chrome import ExistingChromeError
from skills.registry import SkillRegistry
from tests.test_existing_chrome_connection import fixture as _chrome_fixture, owner

chrome_fixture = _chrome_fixture
pytestmark = [pytest.mark.asyncio, pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]
HEADERS = {"X-FERAL-Browser-View": "native-v1"}


@pytest_asyncio.fixture
async def wired(chrome_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "on")
    monkeypatch.setattr(state, "_native_agent_turn_generation", 0, raising=False)
    connector, sockets, _, _ = chrome_fixture
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
    runtime = TaskFlowRuntime(db_path=str(tmp_path / "jobs.db"), skill_registry=registry, orchestrator=orch)
    orch.taskflows = runtime
    live, current, clock = [True], [state], [100.0]
    principal = PairedDevicePrincipal(str(uuid4()), lambda: live[0])
    for key, value in {"orchestrator": orch, "skill_registry": registry, "taskflows": runtime,
                       "browser": connector.controller}.items():
        monkeypatch.setattr(state, key, value)
    monkeypatch.setitem(SKILL_IMPLEMENTATIONS, "browser", SKILL_IMPLEMENTATIONS.get("browser"))
    web = get_implementation("web_actions")
    if web is not None:
        monkeypatch.setattr(web, "_browser", web._browser)
    state._bind_browser_bridge("browser")
    orch._browser_resource_supplier = lambda: state.browser.approval_binding()
    manager = JobChromeGrants(lambda: current[0], clock=lambda: clock[0])
    monkeypatch.setattr(routes, "job_chrome_grants", manager)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(approvals_router)
    bundle = SimpleNamespace(connector=connector, socket=sockets[0], runtime=runtime, orch=orch,
        live=live, current=current, clock=clock, principal=principal, manager=manager, app=app)
    try:
        yield bundle
    finally:
        await connector.disconnect()
        await orch.executor.close()
        await runtime._http.aclose()
        runtime._conn.close()


def http(wired, peer="127.0.0.1", headers=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wired.app, client=(peer, 21000)),
                             base_url="http://fixture.local", headers=HEADERS if headers is None else headers,
                             trust_env=False)


def create(wired, *, endpoint="get_page_info", args=None, session="job-session", principal=True):
    return wired.runtime.create_flow(session_id=session, title="Synthetic phone job",
        steps=[{"type": "skill.invoke", "skill_id": "browser", "endpoint": endpoint, "args": args or {}}],
        handoff_key=uuid4().hex + uuid4().hex, terms_digest="a" * 64,
        origin_session_id="phone-shared", origin_surface="brain_host",
        source_principal=wired.principal if principal else None,
        creation_guard=wired.principal.require_current if principal else None)


def base(wired):
    resource = wired.connector.controller.approval_binding()
    return {"session_id": "owner-a", "connection_id": resource["connection_id"]}


async def review(wired, flow, tools=None):
    async with http(wired) as client:
        listing = await client.post("/api/browser/existing/job-grants/tasks", json=base(wired))
        assert listing.status_code == 200
        row = next(t for t in listing.json()["tasks"] if t["flow_id"] == flow["id"])
        body = {**base(wired), "target_id": "page-a", "flow_id": flow["id"], "step_id": row["step_id"],
                "flow_terms_digest": row["flow_terms_digest"], "allowed_tools": tools or ["browser__get_page_info"],
                "expires_in_seconds": 300}
        result = await client.post("/api/browser/existing/job-grants/review", json=body)
        assert result.status_code == 200, result.text
        return result.json(), body


async def grant(wired, flow, tools=None):
    pending, _ = await review(wired, flow, tools)
    async with http(wired) as client:
        response = await client.post(f"/api/browser/existing/job-grants/{pending['review_id']}/confirm",
            json={"session_id": "owner-a", "review_token": pending["review_token"]})
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["grant_id"] == pending["scope"]["grant_id"]
    assert receipt["browser_resource"] == wired.connector.controller.approval_binding()
    return receipt


def page_packets(wired):
    return [p for p in wired.socket.sent if p.get("sessionId") == "session-page-a" and
            p["method"] not in {"Page.enable", "DOM.enable", "Runtime.enable"}]


async def test_registered_grant_runs_existing_task_path_without_reassigning_tab(wired):
    flow = create(wired)
    original = wired.connector.controller.approval_binding()
    before = len(page_packets(wired))
    receipt = await grant(wired, flow)
    assert receipt["success"] is True and receipt["execution_session_id"] == "job-session"
    assert receipt["action_permission"] == "separate_review_required"
    assert wired.runtime.get_flow(flow["id"])["status"] == "queued"
    assert len(page_packets(wired)) == before  # Grant review/confirm is not execution.
    await wired.runtime._run_flow(flow["id"])
    final = wired.runtime.get_flow(flow["id"])
    assert final["status"] == "completed", final
    assert len(page_packets(wired)) > before
    assert wired.connector.controller.approval_binding() == original
    assert original["owner_session_id"] == "owner-a"  # Job SID remained distinct.


async def test_grant_never_substitutes_for_exact_action_approval(wired):
    flow = create(wired, endpoint="press_key", args={"key": "Tab"})
    await grant(wired, flow, ["browser__press_key"])
    await wired.runtime._run_flow(flow["id"])
    waiting = wired.runtime.get_flow(flow["id"])
    assert waiting["status"] == "waiting", waiting
    pending = next(iter(wired.orch.tool_runner._pending_approvals.values()))
    assert pending["browser_resource"]["owner_session_id"] == "owner-a"
    assert pending["browser_job_grant"]["flow_id"] == flow["id"]
    assert not [p for p in page_packets(wired) if p["method"] == "Input.dispatchKeyEvent"]
    async with http(wired) as client:
        result = await client.post(f"/api/approvals/{pending['request_id']}/approve", json={"session_id": "job-session"})
    assert result.status_code == 200, result.text
    assert result.json()["result"]["success"] is True, result.json()
    assert [p for p in page_packets(wired) if p["method"] == "Input.dispatchKeyEvent"]


@pytest.mark.parametrize("headers,peer", [({}, "127.0.0.1"), (HEADERS, "192.0.2.1"),
    ({**HEADERS, "Origin": "https://example.invalid"}, "127.0.0.1"),
    ({**HEADERS, "X-Forwarded-For": "127.0.0.1"}, "127.0.0.1")])
async def test_grant_control_plane_remains_local_only(wired, headers, peer):
    async with http(wired, peer=peer, headers=headers) as client:
        result = await client.post("/api/browser/existing/job-grants/tasks", json=base(wired))
    assert result.status_code == 403
    assert not wired.manager._reviews and not wired.manager._grants


async def test_one_use_review_exact_owner_and_no_hidden_ttl_extension(wired):
    flow = create(wired)
    pending, _ = await review(wired, flow)
    wired.clock[0] += 20
    path = f"/api/browser/existing/job-grants/{pending['review_id']}/confirm"
    async with http(wired) as client:
        wrong = await client.post(path, json={"session_id": "foreign", "review_token": pending["review_token"]})
        assert wrong.status_code == 403
        result = await client.post(path, json={"session_id": "owner-a", "review_token": pending["review_token"]})
        assert result.status_code == 200
        assert result.json()["expires_at"] == pending["scope"]["expires_at"]
        duplicate = await client.post(path, json={"session_id": "owner-a", "review_token": pending["review_token"]})
        assert duplicate.status_code == 409


@pytest.mark.parametrize("drift", ["expiry", "revoke", "credential", "runtime", "runner", "step_terms", "cancel", "target", "agent_generation", "paused", "private_owner"])
async def test_drift_prevents_new_selected_commands(wired, drift):
    flow = create(wired)
    receipt = await grant(wired, flow)
    before = len(page_packets(wired))
    if drift == "expiry":
        wired.clock[0] += 301
    elif drift == "revoke":
        async with http(wired) as client:
            result = await client.post(f"/api/browser/existing/job-grants/{receipt['grant_id']}/revoke", json=base(wired))
            assert result.status_code == 200 and result.json()["status"] == "revoked"
    elif drift == "credential":
        wired.live[0] = False
    elif drift == "runtime":
        wired.current[0] = SimpleNamespace(browser=state.browser, orchestrator=wired.orch)
    elif drift == "runner":
        old = wired.orch.tool_runner
        wired.orch.tool_runner = copy.copy(old)
    elif drift == "step_terms":
        with wired.runtime._lock:
            wired.runtime._conn.execute("UPDATE taskflow_steps SET payload_json=? WHERE id=?", (json.dumps({"skill_id": "browser", "endpoint": "get_page_info", "args": {"changed": True}}), flow["steps"][0]["id"]))
            wired.runtime._conn.commit()
    elif drift == "cancel":
        wired.runtime.cancel_flow(flow["id"])
    elif drift == "agent_generation":
        state._native_agent_turn_generation += 1
    elif drift == "paused":
        wired.runtime._supervisor = SimpleNamespace(paused=True)
    elif drift == "private_owner":
        wired.runtime._job_chrome_principals[flow["id"]] = PairedDevicePrincipal(wired.principal.device_id, lambda: True)
    else:
        with owner():
            await wired.connector.select("page-b")
        state.browser = wired.connector.controller
    await wired.runtime._run_flow(flow["id"])
    assert len(page_packets(wired)) == before


async def test_inherited_sibling_and_public_grant_id_have_no_authority(wired):
    flow = create(wired)
    await grant(wired, flow)
    before = len(page_packets(wired))
    # Simulate the runner's claimed live step, then probe a copied child context.
    with wired.runtime._lock:
        wired.runtime._conn.execute("UPDATE taskflows SET status='running' WHERE id=?", (flow["id"],))
        wired.runtime._conn.execute("UPDATE taskflow_steps SET status='running' WHERE id=?", (flow["steps"][0]["id"],))
        wired.runtime._conn.commit()
    current = wired.runtime.get_flow(flow["id"])
    step = current["steps"][0]
    lane = (flow["id"], step["id"])
    assert wired.runtime._claim_effect_lane(lane)
    try:
        with task_browser_scope(wired.runtime, current, step), bind_context(session_id="job-session", tool_name="browser__get_page_info"):
            async def sibling():
                with pytest.raises(ExistingChromeError):
                    await wired.connector.controller._cdp.send_command("Runtime.evaluate", {"expression": "1"})
            await asyncio.create_task(sibling())
        with bind_context(session_id="job-session", tool_name="browser__get_page_info"):
            with pytest.raises(ExistingChromeError):
                await wired.connector.controller._cdp.send_command("Runtime.evaluate", {"expression": "1"})
    finally:
        wired.runtime._release_effect_lane(lane)
    assert len(page_packets(wired)) == before


async def test_revocation_while_reply_waits_fences_followup_and_keeps_effect_unknown(wired):
    flow = create(wired, endpoint="press_key", args={"key": "Tab"})
    receipt = await grant(wired, flow, ["browser__press_key"])
    await wired.runtime._run_flow(flow["id"])
    pending = next(iter(wired.orch.tool_runner._pending_approvals.values()))
    wired.socket.held_method = "Input.dispatchKeyEvent"
    async with http(wired) as client:
        task = asyncio.create_task(client.post(f"/api/approvals/{pending['request_id']}/approve", json={"session_id": "job-session"}))
        await asyncio.wait_for(wired.socket.arrived.wait(), 2)
        first = wired.socket.held[0]
        revoke = await client.post(f"/api/browser/existing/job-grants/{receipt['grant_id']}/revoke", json=base(wired))
        assert revoke.status_code == 200
        wired.socket.reply(first)
        result = await task
    assert result.status_code == 200
    assert result.json()["result"].get("success") is not True
    assert len(wired.socket.held) == 1  # No second input after revocation.


async def test_reselection_a_b_a_and_restart_do_not_recreate_grants(wired):
    flow = create(wired)
    receipt = await grant(wired, flow)
    with owner():
        await wired.connector.select("page-b")
        await wired.connector.select("page-a")
    state.browser = wired.connector.controller
    assert state.browser.approval_binding()["connection_id"] != receipt["browser_resource"]["connection_id"]
    fresh = JobChromeGrants(lambda: state)
    with owner():
        with pytest.raises(JobChromeError):
            fresh.status(receipt["grant_id"], "owner-a", receipt["browser_resource"]["connection_id"])
    before = len(page_packets(wired))
    await wired.runtime._run_flow(flow["id"])
    assert len(page_packets(wired)) == before


async def test_legacy_private_claim_and_invented_tools_cannot_obtain_grant(wired):
    create(wired, principal=False)
    async with http(wired) as client:
        tasks = await client.post("/api/browser/existing/job-grants/tasks", json=base(wired))
    assert tasks.json()["tasks"] == []
    valid = create(wired)
    pending, body = await review(wired, valid)
    for changes in [{"allowed_tools": ["browser__invented"]}, {"allowed_tools": ["browser__get_page_info"] * 2},
                    {"flow_terms_digest": "f" * 64}, {"step_id": True}, {"expires_in_seconds": True},
                    {"device_id": wired.principal.device_id}]:
        async with http(wired) as client:
            result = await client.post("/api/browser/existing/job-grants/review", json={**body, **changes})
        assert result.status_code in {409, 422}, result.text
    assert len(wired.manager._reviews) == 1 and not wired.manager._grants


async def test_uncertain_confirm_readback_and_revoke_never_mint_authority(wired):
    flow = create(wired)
    pending, _ = await review(wired, flow)
    grant_id = pending["scope"]["grant_id"]
    before = len(page_packets(wired))
    async with http(wired) as client:
        status = await client.post(f"/api/browser/existing/job-grants/{grant_id}/status", json=base(wired))
        assert status.status_code == 200 and status.json()["status"] == "unavailable"
        revoked = await client.post(f"/api/browser/existing/job-grants/{grant_id}/revoke", json=base(wired))
        assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
        confirmed = await client.post(f"/api/browser/existing/job-grants/{pending['review_id']}/confirm",
            json={"session_id": "owner-a", "review_token": pending["review_token"]})
        assert confirmed.status_code == 409
    assert not wired.manager._grants and len(page_packets(wired)) == before


@pytest.mark.parametrize("endpoint,args", [("get_page_info", {}), ("press_key", {"key": "Tab"})])
async def test_actual_model_step_records_grant_and_retains_action_review(wired, endpoint, args):
    flow = wired.runtime.create_flow(session_id="job-session", title="Synthetic model job",
        steps=[{"type": "llm.chat", "prompt": "Use the synthetic selected browser"}],
        handoff_key=uuid4().hex + uuid4().hex, terms_digest="b" * 64,
        origin_session_id="phone-shared", origin_surface="brain_host",
        source_principal=wired.principal, creation_guard=wired.principal.require_current)
    receipt = await grant(wired, flow, ["browser__" + endpoint])
    async def inert_model(session, prompt, context=None):
        result = await wired.orch.tool_runner.execute_tool_call_for_llm(session,
            {"id": "synthetic-model-call", "name": "browser__" + endpoint, "args": args}, [], surface="brain_host")
        if result.get("status") == "pending_approval":
            return "Awaiting exact action approval"
        assert result.get("success") is True, result
        return "Synthetic browser result received"
    wired.orch._handle_command_impl = inert_model
    await wired.runtime._run_flow(flow["id"])
    after = wired.runtime.get_flow(flow["id"])
    if endpoint == "press_key":
        assert after["status"] == "waiting", after
        pending = next(iter(wired.orch.tool_runner._pending_approvals.values()))
        assert pending["taskflow"]["model_call_id"] == "synthetic-model-call"
        assert pending["browser_job_grant"] == receipt
        assert not [p for p in page_packets(wired) if p["method"] == "Input.dispatchKeyEvent"]
        async with http(wired) as client:
            result = await client.post(f"/api/approvals/{pending['request_id']}/approve", json={"session_id": "job-session"})
        assert result.status_code == 200 and result.json()["result"]["success"] is True, result.text
    else:
        assert after["status"] == "completed", after
    persisted = wired.runtime.get_flow(flow["id"])["steps"][0]["result"]["model_actions"]
    assert persisted["synthetic-model-call"]["terms"]["browser_job_grant"] == receipt
    assert wired.connector.controller.approval_binding()["owner_session_id"] == "owner-a"


async def test_native_fixture_uses_actual_route_receipts(wired):
    flow = create(wired)
    async with http(wired) as client:
        tasks = await client.post("/api/browser/existing/job-grants/tasks", json=base(wired))
    pending, body = await review(wired, flow)
    async with http(wired) as client:
        confirm = await client.post(f"/api/browser/existing/job-grants/{pending['review_id']}/confirm",
            json={"session_id": "owner-a", "review_token": pending["review_token"]})
        grant_id = pending["scope"]["grant_id"]
        status = await client.post(f"/api/browser/existing/job-grants/{grant_id}/status", json=base(wired))
        revoke = await client.post(f"/api/browser/existing/job-grants/{grant_id}/revoke", json=base(wired))
    assert tasks.status_code == confirm.status_code == status.status_code == revoke.status_code == 200
    assert confirm.json() == status.json()
    assert revoke.json() == {**confirm.json(), "status": "revoked"}
    output = os.environ.get("FERAL_SYNTHETIC_CHROME_UI_FIXTURE")
    if output:
        Path(output).write_text(json.dumps({"tasks": tasks.json(), "review_request": body,
            "review": pending, "confirm": confirm.json(), "status": status.json(), "revoke": revoke.json(),
            "taskflow": flow}, indent=2) + "\n")


async def test_pending_reviews_and_confirmation_capacity_are_bounded(wired):
    flow = create(wired)
    seed = await grant(wired, flow)
    seed_grant = wired.manager._grants[seed["grant_id"]]
    # Fill the existing manager with captured, active objects rather than JSON authority.
    for _ in range(126):
        captured = {**seed_grant, "grant_id": str(uuid4())}
        wired.manager._grants[captured["grant_id"]] = captured
    first, body = await review(wired, create(wired))
    second, _ = await review(wired, create(wired))
    async with http(wired) as client:
        one = await client.post(f"/api/browser/existing/job-grants/{first['review_id']}/confirm",
            json={"session_id": "owner-a", "review_token": first["review_token"]})
        two = await client.post(f"/api/browser/existing/job-grants/{second['review_id']}/confirm",
            json={"session_id": "owner-a", "review_token": second["review_token"]})
    assert one.status_code == 200 and two.status_code == 429
    assert len(wired.manager._grants) == 128
    # Expiry releases retained capacity but grants are never reconstructed or replayed.
    wired.clock[0] += 301
    async with http(wired) as client:
        expired = await client.post(f"/api/browser/existing/job-grants/{second['review_id']}/confirm",
            json={"session_id": "owner-a", "review_token": second["review_token"]})
        assert expired.status_code == 409
        for _ in range(64):
            result = await client.post("/api/browser/existing/job-grants/review", json=body)
            assert result.status_code == 200
        excess = await client.post("/api/browser/existing/job-grants/review", json=body)
        assert excess.status_code == 429
    assert len(wired.manager._reviews) == 64 and not wired.manager._grants
