"""Actual approval/child/SQLite boundaries, with inert jobs and notifications."""

import asyncio
import copy
import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from agents.chat_turns import (
    observe_tool_result,
    ChatTurnError,
    claim_task_origin_transfer,
)
from agents.orchestrator import Orchestrator
from agents.refusal_handler import RefusalHandler
from agents.tool_runner import ToolRunner
from api.routes import approvals
from models.skill_manifest import SkillManifest
from security.exec_approvals import ApprovalManager
from skills.executor import SkillExecutor
from skills.registry import SkillRegistry
from tests.test_taskflow_origin_handoff import rig as _rig
from tests.test_browser_resource_approval import wired as _browser_wired

rig = _rig
browser_wired = _browser_wired


@pytest_asyncio.fixture
async def wired_origin(rig, monkeypatch):
    brain, manager, _ = rig
    registry = SkillRegistry()
    manifest = Path(__file__).parents[1] / "skills/manifests/task.json"
    registry.register(SkillManifest.model_validate(json.loads(manifest.read_text())))
    orch = Orchestrator.__new__(Orchestrator)
    orch.skills, orch.memory, orch.taskflows = registry, None, brain.taskflows
    orch._session_surfaces, orch._active_turns, orch._mcp_client = (
        {"thread-A": "http_api"},
        {},
        None,
    )
    orch._context_checkpoints = None
    orch.refusal_handler = RefusalHandler(orch)
    orch.executor = SkillExecutor()
    orch.tool_runner = ToolRunner(
        orch, approval_manager=ApprovalManager(db_path=":memory:")
    )
    orch.tool_runner._autonomy_mode = "strict"
    for name in (
        "_emit_tool_start",
        "_emit_tool_result",
        "_try_genui_for_result",
        "_send_text",
        "_push_approval_resolved",
    ):
        setattr(orch, name, AsyncMock())
    orch._summarize_action_result = lambda *_: "inert stored task"
    brain.orchestrator = orch
    brain.tool_runner = orch.tool_runner
    brain.taskflows._orchestrator = orch
    brain.sessions["thread-A"] = object()
    monkeypatch.setattr(approvals, "state", brain)
    yield brain, manager, orch
    await orch.executor.client.aclose()


async def turn(wired, run, terms="inert input"):
    brain, manager, _ = wired
    output = []

    async def wrapped():
        output.append(await run())
        return "inert processing complete"

    accepted = await manager.submit(
        owner=brain.sessions["thread-A"],
        session_id="thread-A",
        request_id=str(uuid4()),
        terms={"text": terms},
        run=wrapped,
        emit=AsyncMock(),
    )
    await manager._live[("thread-A", accepted["turn_id"])].task
    return output[0], accepted


async def propose(wired, args=None, call="original-call", surface="http_api"):
    _, _, orch = wired

    async def run():
        pending = await orch.tool_runner.execute_tool_call_for_llm(
            "thread-A",
            {
                "id": call,
                "name": "background_task__start",
                "args": args or {"goal": "inert task"},
            },
            [],
            surface=surface,
        )
        observe_tool_result("thread-A", pending)
        return pending

    return await turn(wired, run)


async def approve_http(pending):
    app = FastAPI()
    app.include_router(approvals.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inert.local"
    ) as client:
        return await client.post(
            f"/api/approvals/{pending['request_id']}/approve",
            json={"session_id": "thread-A"},
        )


async def test_native_shape_closed_origin_exact_once_no_standing_grant(wired_origin):
    brain, manager, orch = wired_origin
    pending, original = await propose(wired_origin)
    assert not manager._live
    receipt = await brain.memory.chat_turn_get(
        session_id="thread-A", request_id=original["request_id"]
    )
    assert (
        "status" not in receipt and receipt["processing_outcome"] == "awaiting_approval"
    )
    app = FastAPI()
    app.include_router(approvals.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inert.local"
    ) as client:
        row = (await client.get("/api/approvals")).json()["approvals"][0]
    assert row["approval_scope"] == {"contract_version": 1, "kind": "exact_request"}
    assert not {
        "task_origin_required",
        "origin",
        "turn_id",
        "owner",
        "task_origin",
    }.intersection(row)
    response = await approve_http(pending)
    assert response.status_code == 200
    result = response.json()
    assert result["approval_scope"] == {"contract_version": 1, "kind": "exact_request"}
    handoff = result["result"]["data"]["handoff"]
    assert handoff["accepted"] and handoff["replay_protected"]
    assert handoff["origin"]["request_id"] == original["request_id"]
    assert handoff["origin"]["turn_id"] == original["turn_id"]
    assert handoff["origin"]["tool_call_id"] == "original-call"
    assert handoff["origin"]["surface"] == "http_api"
    assert not orch.tool_runner._approval_mgr.check_approval(
        "background_task__start", "thread-A"
    )[0]
    assert (await approve_http(pending)).status_code == 404
    assert len(brain.taskflows.list_flows()) == 1
    changed, _ = await propose(
        wired_origin, {"goal": "different task"}, call="different-call"
    )
    assert changed["status"] == "pending_approval"


async def test_same_owner_yes_keeps_original_origin(wired_origin):
    brain, _, orch = wired_origin
    pending, original = await propose(wired_origin)
    handled, yes = await turn(
        wired_origin,
        lambda: orch._maybe_handle_pending_tool_approval_text("thread-A", "yes"),
        "yes",
    )
    assert handled
    origin = brain.taskflows.list_flows()[0]["context"]["task_origin"]
    assert origin["request_id"] == original["request_id"] != yes["request_id"]
    assert origin["turn_id"] == original["turn_id"] != yes["turn_id"]
    assert origin["tool_call_id"] == "original-call" != pending["request_id"]
    assert not orch.tool_runner._approval_mgr.check_approval(
        "background_task__start", "thread-A"
    )[0]


@pytest.mark.parametrize(
    "damage",
    [
        "owner",
        "store",
        "runtime",
        "runner",
        "generation",
        "cancel",
        "args",
        "missing",
        "foreign",
        "approval_id",
        "failed",
    ],
)
async def test_stale_or_inconsistent_original_creates_nothing(
    wired_origin, monkeypatch, damage
):
    brain, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    captured = orch.tool_runner._pending_task_origins[pending["request_id"]]
    if damage == "owner":
        brain.sessions["thread-A"] = object()
    elif damage == "store":
        monkeypatch.setattr(brain, "memory", object())
    elif damage == "runtime":
        monkeypatch.setattr(brain, "taskflows", object())
    elif damage == "runner":
        monkeypatch.setattr(
            brain, "orchestrator", SimpleNamespace(tool_runner=object())
        )
    elif damage == "generation":
        brain._native_agent_turn_generation = 1
    elif damage == "cancel":
        captured.audit.cancel_requested = True
    elif damage == "args":
        pending["args"]["goal"] = "unreviewed mutation"
    else:
        receipt = await brain.memory.chat_turn_get(
            session_id="thread-A", request_id=captured.request_id
        )
        if damage == "missing":
            receipt = None
        elif damage == "foreign":
            receipt["turn_id"] = str(uuid4())
        elif damage == "approval_id":
            receipt["approval_request_ids"] = []
        elif damage == "failed":
            receipt["processing_outcome"] = "cancelled"
        monkeypatch.setattr(
            brain.memory, "chat_turn_get", AsyncMock(return_value=receipt)
        )
    outcome = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id="thread-A"
    )
    assert outcome["status"] in {"origin_unavailable", "origin_superseded"}
    assert captured.runtime.list_flows() == []
    assert not orch.tool_runner._approval_mgr.check_approval(
        "background_task__start", "thread-A"
    )[0]


async def test_unsettled_keeps_pending_then_same_request_works(
    wired_origin, monkeypatch
):
    brain, _, orch = wired_origin
    pending, original = await propose(wired_origin)
    real_read = brain.memory.chat_turn_get
    monkeypatch.setattr(
        brain.memory,
        "chat_turn_get",
        AsyncMock(return_value={**original, "status": "running"}),
    )
    outcome = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id="thread-A"
    )
    assert outcome["status"] == "origin_unsettled"
    response = await approve_http(pending)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "origin_unsettled"
    assert orch.tool_runner.get_pending(pending["request_id"])
    assert brain.taskflows.list_flows() == []
    monkeypatch.setattr(brain.memory, "chat_turn_get", real_read)
    assert (await approve_http(pending)).status_code == 200


@pytest.mark.parametrize("mode", ["hybrid", "loose"])
async def test_reviewed_task_keeps_origin_after_autonomy_change(wired_origin, mode):
    brain, _, orch = wired_origin
    pending, original = await propose(wired_origin)
    orch.tool_runner._autonomy_mode = mode
    response = await approve_http(pending)
    assert response.status_code == 200 and response.json()["result"]["success"]
    assert (
        brain.taskflows.list_flows()[0]["context"]["task_origin"]["request_id"]
        == original["request_id"]
    )
    assert not orch.tool_runner._approval_mgr.check_approval(
        "background_task__start", "thread-A"
    )[0]


@pytest.mark.parametrize(
    "metadata",
    [{"taskflow": {}}, {"browser_resource": None}, {"task_origin_required": False}],
)
async def test_invalid_required_scope_metadata_never_becomes_session(
    wired_origin, metadata
):
    _, _, orch = wired_origin
    legacy = orch.tool_runner.enforce_safety(
        "background_task__start", {"goal": "legacy"}, "thread-A", "http_api"
    )
    assert orch.tool_runner.approval_scope_for({**legacy, **metadata}) is None


async def test_duplicate_approvers_create_one_task(wired_origin):
    brain, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    results = await asyncio.gather(
        *(
            orch.resolve_tool_approval_request(
                pending["request_id"], approved=True, session_id="thread-A"
            )
            for _ in range(2)
        )
    )
    assert sum(row["status"] == "approved" for row in results) == 1
    assert len(brain.taskflows.list_flows()) == 1


async def test_pending_reuse_requires_exact_original_call(wired_origin):
    _, _, orch = wired_origin

    async def run():
        one = await orch.tool_runner.execute_tool_call_for_llm(
            "thread-A",
            {"id": "call1", "name": "background_task__start", "args": {"goal": "same"}},
            [],
            surface="http_api",
        )
        same = await orch.tool_runner.execute_tool_call_for_llm(
            "thread-A",
            {"id": "call1", "name": "background_task__start", "args": {"goal": "same"}},
            [],
            surface="http_api",
        )
        other = await orch.tool_runner.execute_tool_call_for_llm(
            "thread-A",
            {"id": "call2", "name": "background_task__start", "args": {"goal": "same"}},
            [],
            surface="http_api",
        )
        assert one["request_id"] == same["request_id"] != other["request_id"]
        observe_tool_result("thread-A", one)
        observe_tool_result("thread-A", other)
        return one

    await turn(wired_origin, run)


async def test_private_scope_expiry_and_unrelated_legacy_compatible(wired_origin):
    _, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    pending["expires_at"] = 1
    assert orch.tool_runner.approval_scope_for(pending) is None
    assert orch.tool_runner.get_pending(pending["request_id"]) is None
    assert pending["request_id"] not in orch.tool_runner._pending_task_origins
    legacy = orch.tool_runner.enforce_safety(
        "background_task__start", {"goal": "legacy"}, "thread-A", "http_api"
    )
    assert orch.tool_runner.approval_scope_for(legacy) == {
        "contract_version": 1,
        "kind": "session",
    }
    rejected = await orch.resolve_tool_approval_request(
        legacy["request_id"], approved=False, session_id="thread-A"
    )
    assert (
        rejected["status"] == "rejected"
        and rejected["approval_scope"]["kind"] == "session"
    )


async def test_copied_backing_context_cannot_claim(wired_origin, monkeypatch):
    brain, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    from skills.impl.background_task import BackgroundTaskSkill

    original_execute = BackgroundTaskSkill.execute

    async def controlled(self, endpoint_id, args, vault):
        if endpoint_id == "start":

            async def copied():
                with pytest.raises(ChatTurnError):
                    claim_task_origin_transfer(args)

            await asyncio.create_task(copied())
        return await original_execute(self, endpoint_id, args, vault)

    monkeypatch.setattr(BackgroundTaskSkill, "execute", controlled)
    response = await approve_http(pending)
    assert response.status_code == 200 and response.json()["result"]["success"]
    assert len(brain.taskflows.list_flows()) == 1


async def test_mixed_expired_live_public_queue_prunes_private_state(wired_origin):
    _, _, orch = wired_origin
    expired, _ = await propose(wired_origin)
    expired["expires_at"] = 1
    live, _ = await propose(wired_origin, {"goal": "live task"}, call="live-call")
    app = FastAPI()
    app.include_router(approvals.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inert.local"
    ) as client:
        response = await client.get("/api/approvals")
    assert response.status_code == 200
    assert [row["request_id"] for row in response.json()["approvals"]] == [
        live["request_id"]
    ]
    assert expired["request_id"] not in orch.tool_runner._pending_task_origins
    assert expired["request_id"] not in orch.tool_runner._pending_scope_kinds
    assert (
        orch.tool_runner.latest_pending_for_session("thread-A")["request_id"]
        == live["request_id"]
    )


async def test_stale_origin_can_be_revoked_without_live_context_authority(wired_origin):
    brain, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    brain.sessions["thread-A"] = object()
    assert orch.tool_runner.approval_scope_for(pending) is None
    app = FastAPI()
    app.include_router(approvals.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inert.local"
    ) as client:
        response = await client.post(
            f"/api/approvals/{pending['request_id']}/reject",
            json={"session_id": "thread-A"},
        )
    assert response.status_code == 200
    assert response.json()["approval_scope"] == {
        "contract_version": 1,
        "kind": "exact_request",
    }
    assert orch.tool_runner.get_pending(pending["request_id"]) is None
    assert brain.taskflows.list_flows() == []
    assert not orch.tool_runner._approval_mgr.check_approval(
        "background_task__start", "thread-A"
    )[0]


async def test_stale_authentic_task_and_live_legacy_queue_remain_reviewable(
    wired_origin,
):
    brain, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    brain.sessions["thread-A"] = object()
    legacy = orch.tool_runner.enforce_safety(
        "background_task__start", {"goal": "inert legacy"}, "legacy-session", "http_api"
    )
    app = FastAPI()
    app.include_router(approvals.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inert.local"
    ) as client:
        response = await client.get("/api/approvals")
        assert response.status_code == 200
        payload = response.json()
        by_id = {row["request_id"]: row for row in payload["approvals"]}
        assert by_id[pending["request_id"]]["approval_available"] is False
        assert by_id[pending["request_id"]]["approval_scope"]["kind"] == "exact_request"
        assert by_id[legacy["request_id"]]["approval_available"] is True
        assert by_id[legacy["request_id"]]["approval_scope"]["kind"] == "session"
        fixture_path = os.environ.get("FERAL_OVERSIGHT_STALE_SYNTHETIC_FIXTURE")
        if fixture_path:
            Path(fixture_path).write_text(json.dumps(payload))
        refused = await client.post(
            f"/api/approvals/{pending['request_id']}/approve",
            json={"session_id": "thread-A"},
        )
        assert refused.status_code == 409
        rejected = await client.post(
            f"/api/approvals/{pending['request_id']}/reject",
            json={"session_id": "thread-A"},
        )
        assert rejected.status_code == 200
    assert brain.taskflows.list_flows() == []


async def test_mutated_or_missing_capsule_is_not_historical_review(wired_origin):
    _, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    origin = orch.tool_runner._pending_task_origins[pending["request_id"]]
    altered = copy.deepcopy(pending)
    altered["args"]["goal"] = "unreviewed"
    assert orch.tool_runner.approval_review_for(altered) is None
    orch.tool_runner._pending_task_origins.pop(pending["request_id"])
    assert orch.tool_runner.approval_review_for(pending) is None
    assert origin.static_matches(pending)


async def test_replaced_browser_request_cannot_block_current_public_queue(
    browser_wired,
):
    from tests.test_browser_resource_approval import (
        pending as browser_pending,
        resource,
    )

    orch, selected, actions = browser_wired
    stale = await browser_pending(browser_wired)
    selected[0] = resource("target-B")
    live = await browser_pending(browser_wired)
    app = FastAPI()
    app.include_router(approvals.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inert.local"
    ) as client:
        response = await client.get("/api/approvals")
        assert response.status_code == 200
        rows = response.json()["approvals"]
        assert [row["request_id"] for row in rows] == [live["request_id"]]
        assert (
            rows[0]["approval_scope"]["kind"] == "exact_request"
            and rows[0]["approval_available"]
        )
        refused = await client.post(
            f"/api/approvals/{stale['request_id']}/approve",
            json={"session_id": "owner"},
        )
        assert refused.status_code == 404
    assert orch.tool_runner.get_pending(stale["request_id"]) is None
    assert stale["request_id"] not in orch.tool_runner._pending_scope_kinds
    assert actions["browser"].calls == []
    assert not orch.tool_runner._approval_mgr.check_approval("browser__click", "owner")[
        0
    ]


async def test_original_surface_not_replaced_by_native(wired_origin):
    brain, _, orch = wired_origin
    pending, _ = await propose(wired_origin, surface="phone_actuator")
    # Preserve the original call surface even if subsequent UI routing changes.
    orch._session_surfaces["thread-A"] = "http_api"
    response = await approve_http(pending)
    assert response.status_code == 200
    assert (
        response.json()["result"]["data"]["handoff"]["origin"]["surface"]
        == "phone_actuator"
    )
    assert len(brain.taskflows.list_flows()) == 1


async def test_raw_origin_cannot_skip_durable_validation(wired_origin):
    _, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    origin = orch.tool_runner._pending_task_origins[pending["request_id"]]
    assert (
        orch.tool_runner.approve_pending(
            pending["request_id"],
            session_id="thread-A",
            exact_once=True,
            validated_task_origin=origin,
        )
        is None
    )
    assert orch.tool_runner.get_pending(pending["request_id"])
    assert (await approve_http(pending)).status_code == 200


@pytest.mark.parametrize("change_generation", [False, True])
async def test_real_managed_revision_allowed_generation_recovery_refused(
    wired_origin, change_generation
):
    brain, _, orch = wired_origin
    from agents.runtime_context_checkpoint import RuntimeContextCoordinator

    locks = {}
    coordinator = RuntimeContextCoordinator(
        brain.memory,
        history={},
        lock_for=lambda sid: locks.setdefault(sid, asyncio.Lock()),
        image_call_ids=lambda sid: frozenset(),
        clear_images=lambda sid: None,
    )
    orch._context_checkpoints = coordinator
    await coordinator.attach("thread-A", checkpoint_version=1)
    pending, _ = await propose(wired_origin)
    before = coordinator.review_generation("thread-A")
    if change_generation:
        with pytest.raises(ValueError):
            async with coordinator.write_scope("thread-A"):
                raise ValueError("inert interrupted context")
        fence = (await brain.memory.runtime_checkpoint_read("thread-A")).record.fence
        await coordinator.recover("thread-A", fence, active_turn=lambda: False)
        assert coordinator.review_generation("thread-A") != before
        outcome = await orch.resolve_tool_approval_request(
            pending["request_id"], approved=True, session_id="thread-A"
        )
        assert outcome["status"] in {"stale_context", "origin_superseded"}
        assert brain.taskflows.list_flows() == []
    else:
        async with coordinator.write_scope("thread-A"):
            coordinator.history["thread-A"] = [
                {"role": "user", "content": "inert later input"}
            ]
        assert coordinator.review_generation("thread-A") == before
        response = await approve_http(pending)
        assert response.status_code == 200 and response.json()["result"]["success"]


async def test_real_dispatch_rechecks_hard_policy_after_approval(
    wired_origin, monkeypatch
):
    brain, _, orch = wired_origin
    pending, _ = await propose(wired_origin)
    monkeypatch.setattr(
        orch.tool_runner,
        "policy_for",
        lambda *a, **k: SimpleNamespace(
            level="deny",
            deny_reason="fixture hard denial",
            sources={"surface_deny": True},
        ),
    )
    result = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id="thread-A"
    )
    assert result["result"]["status"] == "PermissionOutcome::Deny"
    assert brain.taskflows.list_flows() == []
    assert not orch.tool_runner._approval_mgr.check_approval(
        "background_task__start", "thread-A"
    )[0]


async def test_sql_worker_cancel_checks_origin_without_loop_task_lookup(
    wired_origin, monkeypatch
):
    brain, _, orch = wired_origin
    from api.routes import taskflows

    pending, _ = await propose(wired_origin)
    runtime = brain.taskflows
    competing = sqlite3.connect(runtime._db_path)
    competing.execute("BEGIN IMMEDIATE")
    entered = threading.Event()
    actual_create = runtime.create_flow

    def create(**kwargs):
        entered.set()
        return actual_create(**kwargs)

    monkeypatch.setattr(runtime, "create_flow", create)
    caller = asyncio.create_task(
        orch.resolve_tool_approval_request(
            pending["request_id"], approved=True, session_id="thread-A"
        )
    )
    try:
        for _ in range(300):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        competing.rollback()
        if taskflows._CREATION_WORKERS:
            await asyncio.wait_for(
                asyncio.gather(
                    *list(taskflows._CREATION_WORKERS), return_exceptions=True
                ),
                3,
            )
        assert runtime.list_flows() == []
        assert not taskflows._CREATION_WORKERS
    finally:
        competing.close()
        if not caller.done():
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
