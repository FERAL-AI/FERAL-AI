"""Resource review survives tab/profile replacement and executor awaits."""
import asyncio
import copy
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from agents.orchestrator import Orchestrator
from agents.tool_runner import current_browser_resource_binding
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from security.exec_approvals import ApprovalManager
from security.trust_ledger import TrustLedger
from skills.base import BaseSkill
from skills.call_context import bind_context
from skills.executor import SkillExecutor
from skills.impl import SKILL_IMPLEMENTATIONS
from skills.registry import SkillRegistry


def resource(target="target-A", owner="owner"):
    return {"connection_id": str(uuid4()), "target_id": target, "owner_session_id": owner}


class RecordingBrowser(BaseSkill):
    def __init__(self, name):
        super().__init__(name)
        self.calls = []
        self.hold = None
        self.entered = asyncio.Event()

    async def execute(self, endpoint_id, args, vault):
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        binding = current_browser_resource_binding()
        self.calls.append((endpoint_id, copy.deepcopy(args), binding))
        return {"success": True, "data": {"synthetic": True}}


class BoundaryExecutor(SkillExecutor):
    before = None

    async def execute(self, *args, **kwargs):
        if self.before is not None:
            self.before()
        return await super().execute(*args, **kwargs)


@pytest_asyncio.fixture
async def wired(monkeypatch):
    registry = SkillRegistry()
    actions = {}
    for name in ("browser", "web_actions", "notes_memory"):
        registry.register(SkillManifest(
            skill_id=name, brand=BrandProfile(name="Synthetic resource contract"),
            description="No browser or external effects", endpoints=[SkillEndpoint(
                id="click", method="PYTHON", url="", description="Synthetic action",
                requires_user_approval=True, safety_tier="confirm",
            )],
        ))
        actions[name] = RecordingBrowser(name)
        monkeypatch.setitem(SKILL_IMPLEMENTATIONS, name, actions[name])
    orch = Orchestrator(skill_registry=registry, send_to_client=AsyncMock(), daemons={},
                        memory=None, vision_buffer=None, perception=None, learner=None,
                        approval_manager=ApprovalManager(db_path=":memory:"))
    orch.executor = BoundaryExecutor()
    orch._send_text = AsyncMock()
    orch._try_genui_for_result = AsyncMock()
    orch.tool_runner._autonomy_mode = "strict"
    orch.tool_runner._trust = TrustLedger(persist=False)
    selected = [resource()]
    orch._browser_resource_supplier = lambda: selected[0]
    from api.state import state
    monkeypatch.setattr(state, "orchestrator", orch)
    monkeypatch.setattr(state, "skill_registry", registry)
    yield orch, selected, actions
    await orch.executor.close()


def call(name="browser"):
    return {"name": f"{name}__click", "args": {}, "id": "synthetic-call"}


async def pending(wired, name="browser"):
    orch, _selected, actions = wired
    result = await orch.tool_runner.execute_tool_call_for_llm("owner", call(name), [])
    assert result["status"] == "pending_approval"
    assert actions[name].calls == []
    return result


@pytest.mark.parametrize("name", ["browser", "web_actions"])
async def test_resource_bound_exact_approval_runs_once_without_standing_grant(wired, name):
    orch, selected, actions = wired
    queued = await pending(wired, name)
    assert queued["browser_resource"] == selected[0]
    accepted = orch.tool_runner.approve_pending(queued["request_id"], session_id="owner")
    approval = accepted["approval"]  # Normal resource approvals also become exact.
    result = await orch.tool_runner.execute_tool_call_for_llm("owner", call(name), [], approval=approval)
    assert result["success"] is True and len(actions[name].calls) == 1
    assert actions[name].calls[0][2] == selected[0]
    duplicate = await orch.tool_runner.execute_tool_call_for_llm("owner", call(name), [], approval=approval)
    assert duplicate["error_code"] == "invalid_approval" and len(actions[name].calls) == 1
    assert not orch.tool_runner._approval_mgr.check_approval(call(name)["name"], "owner")[0]


@pytest.mark.parametrize("drift", ["target", "reconnect", "A-B-A", "foreign", "regular", "removed"])
async def test_pending_cannot_authorize_replaced_resource(wired, drift):
    orch, selected, actions = wired
    queued = await pending(wired)
    original = copy.deepcopy(selected[0])
    if drift == "target":
        selected[0] = {**original, "target_id": "target-B"}
    elif drift == "reconnect":
        selected[0] = {**original, "connection_id": str(uuid4())}
    elif drift == "A-B-A":
        selected[0] = resource("target-B")
        selected[0] = resource("target-A")
    elif drift == "foreign":
        selected[0] = {**original, "owner_session_id": "foreign"}
    elif drift == "regular":
        selected[0] = None
    else:
        del orch._browser_resource_supplier
    assert not orch.tool_runner.pending_context_valid(queued)
    assert orch.tool_runner.approve_pending(queued["request_id"], session_id="owner") is None
    assert actions["browser"].calls == []


@pytest.mark.parametrize("mode", ["strict", "loose"])
async def test_swap_after_exact_approval_refuses_before_executor_even_loose(wired, mode):
    orch, selected, actions = wired
    queued = await pending(wired)
    accepted = orch.tool_runner.approve_pending(queued["request_id"], session_id="owner")
    orch.tool_runner._autonomy_mode = mode
    selected[0] = resource()
    result = await orch.tool_runner.execute_tool_call_for_llm("owner", call(), [], approval=accepted["approval"])
    assert result["error_code"] == "browser_resource_changed" and actions["browser"].calls == []


@pytest.mark.parametrize("exact", [True, False])
async def test_actual_executor_rechecks_binding_after_dispatch_await(wired, exact):
    orch, selected, actions = wired
    approval = None
    if exact:
        queued = await pending(wired)
        approval = orch.tool_runner.approve_pending(queued["request_id"], session_id="owner")["approval"]
    else:
        orch.tool_runner._autonomy_mode = "loose"
    orch.executor.before = lambda: selected.__setitem__(0, resource())
    result = await orch.tool_runner.execute_tool_call_for_llm("owner", call(), [], approval=approval)
    assert result.get("success") is not True and actions["browser"].calls == []


@pytest.mark.parametrize("exact", [True, False])
async def test_backing_task_cannot_use_latest_resource_after_its_await(wired, exact):
    orch, selected, actions = wired
    approval = None
    if exact:
        queued = await pending(wired)
        approval = orch.tool_runner.approve_pending(queued["request_id"], session_id="owner")["approval"]
    else:
        orch.tool_runner._autonomy_mode = "loose"
    action = actions["browser"]
    action.hold = asyncio.Event()
    task = asyncio.create_task(orch.tool_runner.execute_tool_call_for_llm("owner", call(), [], approval=approval))
    await asyncio.wait_for(action.entered.wait(), timeout=2)
    selected[0] = resource()
    action.hold.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.get("success") is not True and action.calls == []


async def test_standing_approval_does_not_cover_active_browser_resource(wired):
    orch, _selected, actions = wired
    orch.tool_runner.grant_session_approval(call()["name"], "owner")
    result = await pending(wired)
    assert result["browser_resource"] and actions["browser"].calls == []


@pytest.mark.parametrize("bad", [{}, {"connection_id": "bad", "target_id": "A", "owner_session_id": "owner"},
    {"connection_id": str(uuid4()), "target_id": "a/b", "owner_session_id": "owner"},
    {"connection_id": str(uuid4()), "target_id": "A", "owner_session_id": "foreign"},
    {"connection_id": str(uuid4()), "target_id": "A", "owner_session_id": "owner", "secret": "not-accepted"}])
async def test_malformed_or_foreign_supplier_fails_closed(wired, bad):
    orch, selected, actions = wired
    selected[0] = bad
    orch.tool_runner._autonomy_mode = "loose"
    result = await orch.tool_runner.execute_tool_call_for_llm("owner", call(), [])
    assert result["error_code"] == "browser_resource_changed"
    assert orch.tool_runner.list_pending() == [] and actions["browser"].calls == []
    assert "not-accepted" not in str(result)


async def test_supplier_exception_is_redacted_and_nonbrowser_unchanged(wired):
    orch, _selected, actions = wired
    def unavailable():
        raise RuntimeError("private-account-diagnostic")
    orch._browser_resource_supplier = unavailable
    orch.tool_runner._autonomy_mode = "loose"
    result = await orch.tool_runner.execute_tool_call_for_llm("owner", call(), [])
    assert result["error_code"] == "browser_resource_changed" and "private-account" not in str(result)
    regular = await orch.tool_runner.execute_tool_call_for_llm("owner", call("notes_memory"), [])
    assert regular["success"] is True and len(actions["notes_memory"].calls) == 1


async def test_explicit_noncallable_supplier_fails_closed(wired):
    orch, _selected, actions = wired
    orch._browser_resource_supplier = "not-a-callable"
    orch.tool_runner._autonomy_mode = "loose"
    result = await orch.tool_runner.execute_tool_call_for_llm("owner", call(), [])
    assert result["error_code"] == "browser_resource_changed" and actions["browser"].calls == []


async def test_pending_binding_does_not_share_source_dictionary(wired):
    orch, selected, _actions = wired
    queued = await pending(wired)
    original = copy.deepcopy(queued["browser_resource"])
    selected[0]["target_id"] = "target-B"
    assert queued["browser_resource"] == original
    assert not orch.tool_runner.pending_context_valid(queued)


@pytest.mark.parametrize("supplier_absent", [True, False])
async def test_regular_browser_legacy_pending_remains_compatible(wired, supplier_absent):
    orch, selected, _actions = wired
    if supplier_absent:
        del orch._browser_resource_supplier
    else:
        selected[0] = None
    queued = await pending(wired)
    assert "browser_resource" not in queued and orch.tool_runner.pending_context_valid(queued)
    selected[0] = resource()
    orch._browser_resource_supplier = lambda: selected[0]
    assert not orch.tool_runner.pending_context_valid(queued)


async def test_binding_copy_and_retained_child_cannot_reuse_finished_scope(wired):
    orch, selected, _actions = wired
    original = copy.deepcopy(selected[0])
    release = asyncio.Event()
    async def retained():
        await release.wait()
        return current_browser_resource_binding()
    with bind_context(session_id="owner", tool_name=call()["name"]), orch.tool_runner.browser_resource_scope(call()["name"], "owner"):
        observed = current_browser_resource_binding()
        observed["target_id"] = "forged"
        assert current_browser_resource_binding() == original
        child = asyncio.create_task(retained())
    release.set()
    with pytest.raises(RuntimeError, match="no longer current"):
        await child


async def test_http_pending_resource_approval_bridge_and_swap_refusal(wired):
    from api.routes.approvals import router as approvals_router
    from api.routes.tools import router as tools_router
    orch, selected, actions = wired
    app = FastAPI()
    app.include_router(tools_router)
    app.include_router(approvals_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"tool_name": call()["name"], "args": {}, "session_id": "owner", "confirm": True}
        queued = (await client.post("/api/tools/execute", json=body)).json()
        assert queued["status"] == "pending_approval" and queued["browser_resource"] == selected[0]
        assert actions["browser"].calls == []
        path = f"/api/approvals/{queued['request_id']}/approve"
        assert (await client.post(path, json={"session_id": "foreign"})).status_code == 409
        approved = (await client.post(path, json={"session_id": "owner"})).json()
        assert approved["status"] == "approved" and approved["result"]["success"] is True
        assert len(actions["browser"].calls) == 1
        assert not orch.tool_runner._approval_mgr.check_approval(call()["name"], "owner")[0]
        queued = (await client.post("/api/tools/execute", json=body)).json()
        selected[0] = resource()
        stale = await client.post(f"/api/approvals/{queued['request_id']}/approve", json={"session_id": "owner"})
        assert stale.json().get("status") != "approved" and len(actions["browser"].calls) == 1


async def test_http_auto_resource_capture_reaches_backing_task(wired):
    from api.routes.tools import router
    orch, selected, actions = wired
    orch.tool_runner._autonomy_mode = "loose"
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/tools/execute", json={"tool_name": call()["name"], "session_id": "owner", "confirm": True})
    assert response.json()["success"] is True
    assert actions["browser"].calls[0][2] == selected[0]


@pytest.mark.parametrize("boundary", ["unchanged", "executor", "reviewed"])
async def test_resolved_auto_policy_preserves_resource_fences(wired, boundary):
    orch, selected, actions = wired
    approval = None
    if boundary == "reviewed":
        queued = await pending(wired)
        approval = orch.tool_runner.approve_pending(queued["request_id"], session_id="owner")["approval"]
    endpoint = orch.skills.skills["browser"].endpoints[0]
    endpoint.requires_user_approval = False
    endpoint.safety_tier = "safe"
    endpoint.read_only_hint = True
    orch.tool_runner._autonomy_mode = "hybrid"
    assert orch.tool_runner.policy_for(call()["name"], {}).level == "auto"
    if boundary == "executor":
        orch.executor.before = lambda: selected.__setitem__(0, resource())
    elif boundary == "reviewed":
        selected[0] = resource()
    result = await orch.tool_runner.execute_tool_call_for_llm("owner", call(), [], approval=approval)
    if boundary == "unchanged":
        assert result["success"] is True and actions["browser"].calls[0][2] == selected[0]
    else:
        assert result.get("success") is not True and actions["browser"].calls == []
