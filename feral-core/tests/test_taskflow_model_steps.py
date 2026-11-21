"""Goal-created steps exercise real TaskFlow/policy, with an inert model boundary."""

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio

from agents.orchestrator import Orchestrator
from agents.taskflow import TaskFlowRuntime, model_step_for
from agents.tool_runner import ToolRunner
from api.routes.taskflows import _start_task
from models.skill_manifest import (
    BrandProfile,
    EndpointParam,
    SkillEndpoint,
    SkillManifest,
)
from security.exec_approvals import ApprovalManager
from skills.registry import SkillRegistry


@pytest_asyncio.fixture
async def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FERAL_AUTONOMY", "hybrid")
    registry = SkillRegistry()
    registry.register(
        SkillManifest(
            skill_id="notes_memory",
            brand=BrandProfile(name="Inert fixture"),
            description="Inert fixture",
            endpoints=[
                SkillEndpoint(
                    id=name,
                    method="PYTHON",
                    url="",
                    description=name,
                    safety_tier=tier,
                    read_only_hint=read_only,
                    params=[EndpointParam(name="value", type="string", required=True)],
                )
                for name, tier, read_only in (
                    ("save_note", "confirm", False),
                    ("search_notes", "safe", True),
                )
            ],
        )
    )
    orch = Orchestrator.__new__(Orchestrator)
    orch.skills, orch.memory, orch._mcp_client = registry, None, None
    orch._session_surfaces, orch._active_turns, orch._session_locks = {}, {}, {}
    orch._context_checkpoints = None
    orch._w17_cancel_subsessions_nowait = lambda _: None
    orch.send = AsyncMock(return_value=True)
    orch._emit_tool_start, orch._try_genui_for_result = AsyncMock(), AsyncMock()
    orch._push_approval_resolved = AsyncMock()
    orch._summarize_action_result = lambda *_: "Inert dispatcher receipt"
    orch._send_text = AsyncMock()
    orch.tool_runner = ToolRunner(orch, approval_manager=ApprovalManager())
    calls = []

    async def execute(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "data": {"nonce": "verified-fixture"},
            "tool_outcome_verified": True,
        }

    orch.executor = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    rt = TaskFlowRuntime(
        db_path=str(tmp_path / "flows.db"), skill_registry=registry, orchestrator=orch
    )
    rt._supervisor = SimpleNamespace(paused=False)
    orch.taskflows = rt
    yield rt, orch, calls
    await rt.stop()
    rt._conn.close()


def goal(rt, prompts=None, accepted_goal="inert goal"):
    origin = {
        "contract_version": 1,
        "source": "tracked_chat_turn",
        "owner_verified": True,
        "session_id": "origin-A",
        "request_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "tool_call_id": "launch-call",
        "surface": "http_api",
    }
    result = _start_task(
        {"goal": accepted_goal, "subtasks": prompts or ["perform inert task"]},
        origin,
        lambda: None,
        rt,
    )
    assert result["ok"] is True and result["handoff"]["durable"] is True
    return result["flow_id"]


async def action(
    orch, session, call_id="model-action-1", value="exact terms", endpoint="save_note"
):
    call = {
        "id": call_id,
        "name": f"notes_memory__{endpoint}",
        "args": {"value": value},
    }
    return await orch.tool_runner.execute_tool_call_for_llm(
        session, call, [], surface="http_api"
    )


async def ask(rt, orch, flow_id):
    prompts = []

    async def model_boundary(session, text, context=None):
        prompts.append(text)
        result = await action(orch, session)
        return (
            "Please approve"
            if result.get("status") == "pending_approval"
            else "Grounded answer " + result["data"]["nonce"]
        )

    orch._handle_command_impl = model_boundary
    await rt._run_flow(flow_id)
    return prompts, orch.tool_runner.list_pending()[0]


async def test_goal_review_binds_before_publish_dispatches_once_then_continues(wired):
    rt, orch, calls = wired
    flow_id = goal(rt, ["first task", "following task"])
    published = []

    async def notify(session, tool, pending):
        flow, step = rt._approval_step(pending)
        assert flow["status"] == step["status"] == "waiting"
        assert (
            step["step_index"] == 0
            and pending["taskflow"]["model_call_id"] == "model-action-1"
        )
        published.append(pending["request_id"])

    orch.tool_runner._notify_user_of_pending_approval = notify
    prompts, pending = await ask(rt, orch, flow_id)
    assert calls == [] and published == [pending["request_id"]]
    waiting = rt.read_origin_receipt("origin-A", flow_id)
    assert (
        waiting["processing_outcome"] == "awaiting_approval"
        and waiting["steps_completed"] == 0
    )
    assert rt.get_flow(flow_id)["steps"][1]["status"] == "pending"
    outcome = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )
    assert outcome["status"] == "approved" and len(calls) == 1
    assert rt.get_flow(flow_id)["current_step"] == 0
    await rt._run_flow(flow_id, single_step=True)
    assert len(calls) == 1
    assert "Recorded actions:" in prompts[1] and prompts[1] != prompts[0]
    assert '"args"' not in prompts[1] and '"resource"' not in prompts[1]
    row = rt.read_origin_receipt("origin-A", flow_id)
    assert row["steps_completed"] == 1 and row["action_outcome"] == "not_asserted"

    async def next_action(session, text, context=None):
        await action(orch, session, call_id="model-action-2")
        return "needs separate approval"

    orch._handle_command_impl = next_action
    orch.tool_runner._notify_user_of_pending_approval = AsyncMock()
    await rt._run_flow(flow_id)
    assert len(calls) == 1 and len(orch.tool_runner.list_pending()) == 1
    assert (
        rt.read_origin_receipt("origin-A", flow_id)["processing_outcome"]
        == "awaiting_approval"
    )


@pytest.mark.parametrize(
    "mode", ["empty", "failure", "unknown", "provider_error", "exhausted"]
)
async def test_incomplete_model_step_never_advances(wired, mode):
    rt, orch, calls = wired
    flow_id = goal(rt, ["first", "never next"])

    async def model_boundary(session, text, context=None):
        scope = model_step_for(session, orch)
        if mode == "empty":
            return ""
        if mode in {"provider_error", "exhausted"}:
            scope.error = True
        else:
            scope.observe_result(
                {"id": "not-dispatched"},
                {
                    "success": False,
                    "status": "outcome_unknown" if mode == "unknown" else "failed",
                },
            )
        return "Untrusted final prose"

    orch._handle_command_impl = model_boundary
    await rt._run_flow(flow_id)
    row = rt.get_flow(flow_id)
    assert row["status"] == ("waiting" if mode == "unknown" else "failed")
    assert (
        row["current_step"] == 0
        and row["steps"][1]["status"] == "pending"
        and calls == []
    )


async def test_changed_identity_args_cannot_reuse_receipt_or_dispatch(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )

    async def changed(session, text, context=None):
        result = await action(orch, session, value="changed terms")
        assert result["error_code"] == "task_action_identity_invalid"
        return "Cannot reuse changed action"

    orch._handle_command_impl = changed
    await rt._run_flow(flow_id)
    assert rt.get_flow(flow_id)["status"] == "failed" and len(calls) == 1


async def test_new_identity_same_terms_requires_another_exact_review(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )

    async def another(session, text, context=None):
        result = await action(orch, session, call_id="distinct-action")
        assert result["status"] == "pending_approval"
        return "another explicit review"

    orch._handle_command_impl = another
    await rt._run_flow(flow_id)
    assert len(calls) == 1 and rt.get_flow(flow_id)["status"] == "waiting"
    new = orch.tool_runner.list_pending()[0]
    assert new["request_id"] != pending["request_id"]


async def test_restart_after_receipt_continues_without_repeating_effect(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )
    reopened = TaskFlowRuntime(
        db_path=rt._db_path, skill_registry=orch.skills, orchestrator=orch
    )
    try:
        orch.taskflows = reopened
        reopened._recover_after_restart()
        await reopened._run_flow(flow_id)
        assert (
            reopened.read_origin_receipt("origin-A", flow_id)["processing_outcome"]
            == "completed"
        )
        assert len(calls) == 1
    finally:
        await reopened.stop()
        reopened._conn.close()
        orch.taskflows = rt


async def test_cancel_pending_goal_does_not_affect_another_flow(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    other = goal(rt)
    rt.cancel_flow(flow_id)
    result = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )
    assert result["status"] == "not_found" and calls == []

    async def answer(session, text, context=None):
        return "Other task completed"

    orch._handle_command_impl = answer
    await rt._run_flow(other)
    assert (
        rt.get_flow(other)["status"] == "completed"
        and rt.get_flow(flow_id)["status"] == "cancelled"
    )


async def test_restart_during_dispatch_never_replays_or_advances(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    assert rt.prepare_approved_dispatch(pending)
    reopened = TaskFlowRuntime(
        db_path=rt._db_path, skill_registry=orch.skills, orchestrator=orch
    )
    try:
        reopened._recover_after_restart()
        assert (
            reopened.read_origin_receipt("origin-A", flow_id)["processing_outcome"]
            == "outcome_unknown"
        )
        await reopened._run_flow(flow_id)
        reopened.resume_flow(flow_id)
        assert calls == [] and reopened.get_flow(flow_id)["current_step"] == 0
    finally:
        await reopened.stop()
        reopened._conn.close()
        await rt.finish_approved_dispatch(pending, uncertain=True)


@pytest.mark.parametrize(
    "mode",
    ["approval", "provider_failure", "budget", "empty", "tool_only", "parser_failure"],
)
async def test_actual_orchestrator_loop_processing_contract(wired, mode):
    """Only provider replies/UI routing are inert; the real command loop runs."""
    import json
    from agents.llm_provider import LLMProvider

    rt, old, calls = wired
    orch = Orchestrator(
        skill_registry=old.skills,
        send_to_client=AsyncMock(return_value=True),
        daemons={},
        taskflows=rt,
    )
    rt._orchestrator = orch
    orch.executor = old.executor
    orch._multi_agent_enabled = False
    orch._route_prompt = AsyncMock(return_value=list(old.skills.skills.values()))
    orch._ensure_core_skills = lambda skills: skills
    orch._build_system_prompt = AsyncMock(return_value="Inert model-step fixture")
    orch._force_tool_for_query = lambda *args: None
    responses = []
    if mode == "approval":
        responses.append(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "actual-loop-call",
                                    "type": "function",
                                    "function": {
                                        "name": "notes_memory__save_note",
                                        "arguments": json.dumps(
                                            {"value": "exact terms"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
    elif mode == "provider_failure":
        responses.append(
            {
                "error": "Inert provider failure",
                "error_code": "fixture_error",
                "choices": [],
            }
        )
    elif mode == "budget":
        responses.append(
            {
                "budget_exceeded": {
                    "call_site": "chat",
                    "cap_dollars": 1,
                    "current_dollars": 2,
                    "reset_at": 1,
                },
                "choices": [],
            }
        )
    elif mode == "tool_only":
        orch._max_iterations = 1
        responses.append(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "actual-read-call",
                                    "type": "function",
                                    "function": {
                                        "name": "notes_memory__search_notes",
                                        "arguments": json.dumps(
                                            {"value": "exact terms"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
    else:
        responses.append({"choices": [{"message": {"content": ""}}]})

    async def provider(**kwargs):
        return responses[0]

    orch.llm = SimpleNamespace(
        available=True,
        model_name="inert",
        extract_response=lambda data: LLMProvider.extract_response(None, data),
    )
    orch._call_llm_chat = provider
    orch._direct_execute = AsyncMock()
    if mode == "parser_failure":

        def invalid_parser(data):
            raise ValueError("Inert parser exception")

        orch.llm.extract_response = invalid_parser
    flow_id = goal(rt, ["first", "second must wait"])
    try:
        await rt._run_flow(flow_id)
        row = rt.get_flow(flow_id)
        assert row["current_step"] == 0 and row["steps"][1]["status"] == "pending"
        assert row["status"] == ("waiting" if mode == "approval" else "failed")
        if mode == "approval":
            pending = orch.tool_runner.list_pending()[0]
            assert rt._approval_step(pending)[1]["status"] == "waiting" and calls == []
            accepted = await orch.resolve_tool_approval_request(
                pending["request_id"], approved=True, session_id=pending["session_id"]
            )
            assert accepted["status"] == "approved" and len(calls) == 1
            responses[0] = {
                "choices": [{"message": {"content": "Grounded verified-fixture"}}]
            }
            await rt._run_flow(flow_id, single_step=True)
            receipt = rt.read_origin_receipt("origin-A", flow_id)
            assert (
                receipt["steps_completed"] == 1
                and receipt["result_text"] == "Grounded verified-fixture"
            )
            assert receipt["action_outcome"] == "not_asserted" and len(calls) == 1
        elif mode == "tool_only":
            assert (
                len(calls) == 1
            )  # Already executed read does not imply goal completion.
        else:
            assert calls == []
        if mode == "parser_failure":
            orch._direct_execute.assert_not_awaited()
    finally:
        await orch.drain_background_tasks()
        rt._orchestrator = old


async def test_foreign_session_cannot_resolve_goal_review(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    result = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id="foreign-session"
    )
    assert result["status"] == "session_mismatch" and calls == []
    assert (
        rt.read_origin_receipt("origin-A", flow_id)["processing_outcome"]
        == "awaiting_approval"
    )
    assert rt.read_origin_receipt("foreign-session", flow_id) is None


async def test_changed_model_call_binding_cannot_dispatch(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    orch.tool_runner._pending_approvals[pending["request_id"]]["taskflow"][
        "model_call_id"
    ] = "changed-model-call"
    result = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )
    assert result["status"] == "not_found" and calls == []
    assert rt.get_flow(flow_id)["current_step"] == 0


async def test_immediate_confirmation_during_publish_keeps_continuation_checkpoint(
    wired,
):
    rt, orch, calls = wired
    flow_id = goal(rt)

    async def notify(session, tool, pending):
        result = await orch.resolve_tool_approval_request(
            pending["request_id"], approved=True, session_id=session
        )
        assert result["status"] == "approved"

    orch.tool_runner._notify_user_of_pending_approval = notify
    prompts = []

    async def model_boundary(session, text, context=None):
        prompts.append(text)
        await action(orch, session)
        return "initial review text"

    orch._handle_command_impl = model_boundary
    await rt._run_flow(flow_id)
    assert (
        rt.get_flow(flow_id)["status"] == "queued"
        and rt.get_flow(flow_id)["current_step"] == 0
    )
    assert len(calls) == 1
    await rt._run_flow(flow_id)
    assert rt.get_flow(flow_id)["status"] == "completed" and len(calls) == 1
    assert "Recorded actions:" in prompts[-1]


async def test_cancel_active_model_dispatch_keeps_unknown_and_chat_usable(wired):
    import asyncio

    rt, orch, calls = wired
    flow_id = goal(rt, ["running", "must not run"])
    entered = asyncio.Event()

    async def delayed(**kwargs):
        calls.append(kwargs)
        entered.set()
        await asyncio.Event().wait()

    orch.executor.execute = AsyncMock(side_effect=delayed)

    async def model_boundary(session, text, context=None):
        if session == "foreground-chat":
            return "Independent chat still responds"
        await action(orch, session, endpoint="search_notes")
        return "must not become a result"

    orch._handle_command_impl = model_boundary
    await rt.start()
    await asyncio.wait_for(entered.wait(), 3)
    assert (
        await orch.handle_command("foreground-chat", "hello")
        == "Independent chat still responds"
    )
    rt.cancel_flow(flow_id)

    async def settled():
        while rt.get_flow(flow_id)["steps"][0]["status"] != "outcome_unknown":
            await asyncio.sleep(0.01)

    await asyncio.wait_for(settled(), 3)
    await rt.stop()
    receipt = rt.read_origin_receipt("origin-A", flow_id)
    assert (
        receipt["processing_outcome"] == "outcome_unknown"
        and receipt["status"] == "cancelled"
    )
    rt.resume_flow(flow_id)
    assert len(calls) == 1 and rt.get_flow(flow_id)["steps"][1]["status"] == "pending"


async def test_fresh_process_lost_pending_review_cannot_dispatch_or_resume(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    fresh = Orchestrator(
        skill_registry=orch.skills, send_to_client=AsyncMock(), daemons={}
    )
    reopened = TaskFlowRuntime(
        db_path=rt._db_path, skill_registry=orch.skills, orchestrator=fresh
    )
    fresh.taskflows = reopened
    fresh.executor = orch.executor
    fresh.handle_command = AsyncMock(return_value="must not run")
    try:
        reopened._recover_after_restart()
        result = await fresh.resolve_tool_approval_request(
            pending["request_id"], approved=True, session_id=pending["session_id"]
        )
        assert result["status"] == "not_found" and calls == []
        reopened.resume_flow(flow_id)
        await reopened._run_flow(flow_id)
        fresh.handle_command.assert_not_awaited()
        assert (
            reopened.read_origin_receipt("origin-A", flow_id)["processing_outcome"]
            == "awaiting_approval"
        )
    finally:
        await reopened.stop()
        reopened._conn.close()
        await fresh.drain_background_tasks()


async def test_large_live_result_is_unchanged_but_receipt_projection_is_bounded(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    image = "data:image/png;base64," + "AAAA" * 25000
    orch.executor.execute = AsyncMock(
        return_value={"success": True, "data": {"image_base64": image}}
    )

    async def model_boundary(session, text, context=None):
        result = await action(orch, session, endpoint="search_notes")
        assert result["data"]["image_base64"] == image
        record = model_step_for(session, orch).actions["model-action-1"]
        import json

        assert len(json.dumps(record).encode()) <= 65536
        assert (
            record["result"]["success"] is True
            and record["result"]["_receipt_projection"] is True
        )
        return "Observed live fixture image"

    orch._handle_command_impl = model_boundary
    await rt._run_flow(flow_id)
    assert rt.get_flow(flow_id)["status"] == "completed"
    assert orch.executor.execute.await_count == 1


async def test_structured_credentials_never_enter_model_receipt_or_continuation(wired):
    import json

    rt, orch, calls = wired
    flow_id = goal(rt)
    secret = "inert-secret-nonce"
    orch.executor.execute = AsyncMock(
        return_value={
            "success": True,
            "data": {
                "nonce": "verified-fixture",
                "access_token": secret,
                "nested": [
                    {"client_secret": secret, "Authorization": "Bearer " + secret}
                ],
            },
        }
    )
    prompts, pending = await ask(rt, orch, flow_id)
    await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )
    assert secret not in json.dumps(rt.get_flow(flow_id))
    await rt._run_flow(flow_id)
    assert rt.get_flow(flow_id)["status"] == "completed" and secret not in prompts[-1]
    assert orch.executor.execute.await_count == 1
    other_id = goal(rt)

    async def model_boundary(session, text, context=None):
        result = await action(orch, session, endpoint="search_notes")
        assert secret not in json.dumps(result)
        return "Credential-free processing result"

    orch._handle_command_impl = model_boundary
    await rt._run_flow(other_id)
    assert rt.get_flow(other_id)["status"] == "completed" and secret not in json.dumps(
        rt.get_flow(other_id)
    )


async def test_explicit_resume_renews_fresh_process_review_without_replaying_goal(
    wired,
):
    rt, old, calls = wired
    flow_id = goal(rt)
    _, old_pending = await ask(rt, old, flow_id)
    fresh = Orchestrator(
        skill_registry=old.skills, send_to_client=AsyncMock(), daemons={}
    )
    reopened = TaskFlowRuntime(
        db_path=rt._db_path, skill_registry=old.skills, orchestrator=fresh
    )
    fresh.taskflows, fresh.executor = reopened, old.executor
    fresh._try_genui_for_result, fresh._push_approval_resolved = (
        AsyncMock(),
        AsyncMock(),
    )
    fresh._summarize_action_result = lambda *_: "Inert receipt"
    fresh._send_text = AsyncMock()
    publications = []

    async def notify(session, tool, pending):
        assert reopened._approval_step(pending)[1]["status"] == "waiting"
        publications.append(pending["request_id"])

    fresh.tool_runner._notify_user_of_pending_approval = notify

    async def continuation(session, text, context=None):
        assert "Recorded actions:" in text
        result = await action(fresh, session)
        assert result["data"]["nonce"] == "verified-fixture"
        return "Fresh-process grounded answer"

    fresh._handle_command_impl = continuation
    try:
        reopened._recover_after_restart()
        assert fresh.tool_runner.list_pending() == []
        renewed = reopened.resume_flow(flow_id)
        assert renewed["review_renewal"]["status"] == "waiting" and calls == []
        pending = fresh.tool_runner.list_pending()[0]
        assert (
            pending["request_id"] != old_pending["request_id"]
            and pending["expires_at"] > pending["created_at"]
        )
        await asyncio.wait_for(asyncio.gather(*reopened._review_publications), 3)
        assert publications == [pending["request_id"]]
        old_result = await fresh.resolve_tool_approval_request(
            old_pending["request_id"],
            approved=True,
            session_id=old_pending["session_id"],
        )
        assert old_result["status"] == "not_found" and calls == []
        approved = await fresh.resolve_tool_approval_request(
            pending["request_id"], approved=True, session_id=pending["session_id"]
        )
        assert approved["status"] == "approved" and len(calls) == 1
        await reopened._run_flow(flow_id)
        receipt = reopened.read_origin_receipt("origin-A", flow_id)
        assert (
            receipt["processing_outcome"] == "completed"
            and receipt["result_text"] == "Fresh-process grounded answer"
        )
        assert len(calls) == 1
    finally:
        await reopened.stop()
        reopened._conn.close()
        await fresh.drain_background_tasks()


async def test_explicit_review_renewal_refuses_changed_resource(wired, monkeypatch):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    orch.tool_runner.deny_pending(
        pending["request_id"], session_id=pending["session_id"]
    )
    monkeypatch.setattr(orch.tool_runner, "_browser_tool", lambda tool: True)
    monkeypatch.setattr(
        orch.tool_runner,
        "_capture_browser_resource",
        lambda *args: {"generation": "different"},
    )
    renewed = rt.resume_flow(flow_id)
    assert renewed["review_renewal"]["error_code"] == "task_review_resource_changed"
    assert orch.tool_runner.list_pending() == [] and calls == []


async def test_nested_delegation_cannot_hide_unbound_approval(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)

    async def model_boundary(session, text, context=None):
        result = await orch.tool_runner.spawn_subagents(
            session, {"tasks": ["inert child action"]}
        )
        assert result["error_code"] == "task_child_ownership_unavailable"
        return "must not imply completion"

    orch._handle_command_impl = model_boundary
    await rt._run_flow(flow_id)
    assert rt.get_flow(flow_id)["status"] == "failed" and calls == []
    assert orch.tool_runner.list_pending() == []


async def test_first_model_subtask_receives_global_constraints_before_action(wired):
    import json
    from agents.llm_provider import LLMProvider

    rt, old, calls = wired
    flow_id = goal(
        rt,
        ["Save the chosen fixture note"],
        accepted_goal="Only save the red option; budget nonce 5824",
    )
    orch = Orchestrator(
        skill_registry=old.skills, send_to_client=AsyncMock(), daemons={}, taskflows=rt
    )
    rt._orchestrator = orch
    orch.executor = old.executor
    orch._multi_agent_enabled = False
    orch._route_prompt = AsyncMock(return_value=list(old.skills.skills.values()))
    orch._ensure_core_skills = lambda skills: skills
    orch._build_system_prompt = AsyncMock(return_value="Inert first-step fixture")
    orch._force_tool_for_query = lambda *args: None
    orch.llm = SimpleNamespace(
        available=True,
        model_name="inert",
        extract_response=lambda data: LLMProvider.extract_response(None, data),
    )
    orch.learner = SimpleNamespace(on_message=AsyncMock())
    orch._save_episode_async = Mock()
    seen = []

    async def provider(**kwargs):
        messages = str(kwargs["messages"])
        seen.append(messages)
        assert "Only save the red option; budget nonce 5824" in messages
        assert "Save the chosen fixture note" in messages
        assert "accepted_task_goal" in messages and '"completed_steps": []' in messages
        return {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "first-constraint-call",
                                "type": "function",
                                "function": {
                                    "name": "notes_memory__save_note",
                                    "arguments": json.dumps({"value": "red"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }

    orch._call_llm_chat = provider
    try:
        await rt._run_flow(flow_id)
        pending = orch.tool_runner.list_pending()
        assert len(seen) == 1 and len(pending) == 1 and calls == []
        assert pending[0]["args"] == {"value": "red"}
        assert (
            rt.read_origin_receipt("origin-A", flow_id)["processing_outcome"]
            == "awaiting_approval"
        )
        await orch.drain_background_tasks()
        orch.learner.on_message.assert_not_awaited()
        assert not any(
            call.kwargs.get("event_type") == "user_command"
            for call in orch._save_episode_async.call_args_list
        )
    finally:
        await orch.drain_background_tasks()


async def test_foreground_user_turn_still_reaches_learner(wired):
    _, old, _ = wired
    orch = Orchestrator(
        skill_registry=old.skills, send_to_client=AsyncMock(), daemons={}
    )
    orch.learner = SimpleNamespace(on_message=AsyncMock())
    orch._finalize_turn(
        "ordinary-chat", {"text": "I prefer red", "user_recorded": True}
    )
    await orch.drain_background_tasks()
    orch.learner.on_message.assert_awaited_once_with(
        "ordinary-chat", "user", "I prefer red"
    )


async def test_fresh_model_second_step_receives_persisted_goal_and_result(wired):
    from agents.llm_provider import LLMProvider

    rt, old, calls = wired
    flow_id = goal(rt, ["First fixture step", "Second fixture step"])

    async def first(session, text, context=None):
        return "Persisted-first-step-nonce-8347"

    old._handle_command_impl = first
    await rt._run_flow(flow_id, single_step=True)
    fresh = Orchestrator(
        skill_registry=old.skills, send_to_client=AsyncMock(), daemons={}
    )
    reopened = TaskFlowRuntime(
        db_path=rt._db_path, skill_registry=old.skills, orchestrator=fresh
    )
    fresh.taskflows = reopened
    fresh._multi_agent_enabled = False
    fresh._route_prompt = AsyncMock(return_value=[])
    fresh._ensure_core_skills = lambda skills: skills
    fresh._build_system_prompt = AsyncMock(return_value="Inert fresh process")
    fresh._force_tool_for_query = lambda *args: None
    fresh.llm = SimpleNamespace(
        available=True,
        model_name="inert",
        extract_response=lambda data: LLMProvider.extract_response(None, data),
    )
    seen = []

    async def provider(**kwargs):
        seen.append(kwargs["messages"])
        text = str(kwargs["messages"])
        assert (
            "inert goal" in text
            and "Persisted-first-step-nonce-8347" in text
            and "Second fixture step" in text
        )
        assert (
            "accepted_task_goal" in text
            and "persisted_processing_result" in text
            and "not_asserted" in text
        )
        return {
            "choices": [{"message": {"content": "Fresh step used persisted context"}}]
        }

    fresh._call_llm_chat = provider
    try:
        reopened._recover_after_restart()
        assert fresh.conversation_history == {}
        await reopened._run_flow(flow_id)
        assert (
            len(seen) == 1
            and reopened.read_origin_receipt("origin-A", flow_id)["steps_completed"]
            == 2
        )
        assert calls == []
    finally:
        await reopened.stop()
        reopened._conn.close()
        await fresh.drain_background_tasks()


async def test_unknown_review_resume_never_renews_permission(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    assert rt.prepare_approved_dispatch(pending)
    await rt.finish_approved_dispatch(pending, uncertain=True)
    rt.resume_flow(flow_id)
    assert rt.get_flow(flow_id)["steps"][0]["status"] == "outcome_unknown"
    assert len(orch.tool_runner.list_pending()) == 1 and calls == []
    assert not rt._review_publications


async def test_nested_background_launch_is_not_completed_child_work(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)

    async def model_boundary(session, text, context=None):
        result = await orch.tool_runner.execute_tool_call_for_llm(
            session,
            {
                "id": "child-task",
                "name": "background_task__start",
                "args": {"goal": "inert child"},
            },
            [],
            surface="http_api",
        )
        assert result["error_code"] == "task_action_identity_invalid"
        return "must not imply delegated completion"

    orch._handle_command_impl = model_boundary
    await rt._run_flow(flow_id)
    assert (
        rt.get_flow(flow_id)["status"] == "failed"
        and len(rt.list_flows()) == 1
        and calls == []
    )


async def test_historical_model_review_cannot_keep_broader_missing_origin_scope(wired):
    rt, orch, calls = wired
    flow_id = goal(rt)
    _, pending = await ask(rt, orch, flow_id)
    rt._conn.execute("UPDATE taskflows SET origin_surface=NULL WHERE id=?", (flow_id,))
    rt._conn.commit()
    assert rt.execution_surface_for_flow(flow_id) == "cron"
    assert rt._approval_step(pending) == (None, None)
    renewed = rt.resume_flow(flow_id)
    assert renewed["review_renewal"]["status"] == "refused"
    result = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id=pending["session_id"]
    )
    assert result["status"] == "not_found" and calls == []
