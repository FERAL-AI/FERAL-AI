"""Actual persisted workflow creators retain surface denials; sinks are inert."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents.orchestrator import Orchestrator
from models.skill_manifest import (
    BrandProfile,
    EndpointParam,
    SkillEndpoint,
    SkillManifest,
)
from security.safety_resolver import resolve_policy
from tests.test_taskflow_dispatch_policy import wired as _policy_wired

wired = _policy_wired


def shell_step(runtime):
    runtime._skill_registry.register(
        SkillManifest(
            skill_id="desktop_control",
            brand=BrandProfile(name="Inert surface fixture"),
            description="No shell executor",
            endpoints=[
                SkillEndpoint(
                    id="shell_command",
                    method="PYTHON",
                    url="",
                    description="Inert hard-denied name",
                    safety_tier="confirm",
                    read_only_hint=False,
                    params=[
                        EndpointParam(name="command", type="string", required=True)
                    ],
                )
            ],
        )
    )
    return {
        "type": "skill.invoke",
        "skill_id": "desktop_control",
        "endpoint": "shell_command",
        "args": {"command": "INERT_ONLY"},
    }


@pytest.mark.parametrize("surface", ["http_api", "cron", "phone_actuator"])
async def test_creation_owned_surface_cannot_be_overridden_by_context(wired, surface):
    rt, orch, seen = wired
    step = shell_step(rt)
    orch.tool_runner._autonomy_mode = "loose"
    flow = rt.create_flow(
        session_id="owner",
        title="inert",
        steps=[step],
        origin_surface=surface,
        context={"task_origin": {"surface": "local_cli", "owner_verified": True}},
    )
    assert (
        resolve_policy(
            "desktop_control__shell_command",
            step["args"],
            surface=surface,
            registry=rt._skill_registry,
        ).level
        == "deny"
    )
    await rt._run_flow(flow["id"])
    assert seen == [] and rt.get_flow(flow["id"])["status"] == "failed"
    assert orch.tool_runner.list_pending() == []


async def test_raw_rest_fixed_http_surface_refuses_forged_local_claim(
    wired, monkeypatch
):
    from api.routes import taskflows

    rt, orch, seen = wired
    monkeypatch.setattr(taskflows.state, "taskflows", rt)
    orch.tool_runner._autonomy_mode = "loose"
    flow = await taskflows.create_taskflow(
        {
            "session_id": "owner",
            "steps": [shell_step(rt)],
            "context": {
                "task_origin": {
                    "source": "tracked_chat_turn",
                    "surface": "local_cli",
                    "owner_verified": True,
                }
            },
        }
    )
    assert rt.origin_surface_for_flow(flow["id"]) == "http_api"
    await rt._run_flow(flow["id"])
    assert seen == [] and rt.get_flow(flow["id"])["status"] == "failed"


@pytest.mark.parametrize("branch", ["inline", "pack"])
async def test_actual_routine_flow_branches_keep_cron_hard_denial(
    wired, monkeypatch, branch
):
    from api import server
    from api.routes import personas

    rt, orch, seen = wired
    orch.tool_runner._autonomy_mode = "loose"
    step = shell_step(rt)
    monkeypatch.setattr(server.state, "taskflows", rt)
    monkeypatch.setattr(
        server.state,
        "cron_service",
        SimpleNamespace(
            record_run_start=lambda _: 1, record_run_finish=lambda *a: None
        ),
    )
    if branch == "pack":
        monkeypatch.setattr(
            personas.state,
            "workflow_packs",
            {
                "inert-pack": SimpleNamespace(
                    name="inert", steps=[SimpleNamespace(model_dump=lambda: step)]
                )
            },
        )
        payload = {"workflow_id": "inert-pack"}
    else:
        payload = {"flow_id": "inline", "steps": [step]}
    server.execute_routine_job(
        SimpleNamespace(
            id="inert-routine",
            job_type="cron",
            description="inert",
            session_id="owner",
            payload=payload,
        )
    )
    flow = rt.list_flows()[0]
    assert rt.origin_surface_for_flow(flow["id"]) == "cron"
    await rt._run_flow(flow["id"])
    assert seen == [] and rt.get_flow(flow["id"])["status"] == "failed"


async def test_legacy_unowned_flow_keeps_reads_but_cannot_manufacture_review(wired):
    rt, orch, seen = wired
    read = rt.create_flow(
        session_id="owner",
        title="old read",
        steps=[
            {
                "type": "skill.invoke",
                "skill_id": "notes_memory",
                "endpoint": "search_notes",
                "args": {"value": "inert"},
            }
        ],
    )
    await rt._run_flow(read["id"])
    assert len(seen) == 1 and seen[0][0].surface == "cron"
    confirm = rt.create_flow(
        session_id="owner",
        title="old write",
        steps=[
            {
                "type": "skill.invoke",
                "skill_id": "notes_memory",
                "endpoint": "save_note",
                "args": {"value": "inert"},
            }
        ],
        context={"task_origin": {"surface": "local_cli"}},
    )
    orch.tool_runner._autonomy_mode = "loose"
    await rt._run_flow(confirm["id"])
    assert len(seen) == 1 and rt.get_flow(confirm["id"])["status"] == "failed"
    assert orch.tool_runner.list_pending() == []


async def test_changed_reviewed_surface_refuses_execution(wired):
    rt, orch, seen = wired
    flow = rt.create_flow(
        session_id="owner",
        title="known local",
        steps=[
            {
                "type": "skill.invoke",
                "skill_id": "notes_memory",
                "endpoint": "save_note",
                "args": {"value": "inert"},
            }
        ],
        origin_surface="local_cli",
    )
    await rt._run_flow(flow["id"])
    pending = orch.tool_runner.list_pending()[0]
    rt._conn.execute(
        "UPDATE taskflows SET origin_surface='cron' WHERE id=?", (flow["id"],)
    )
    rt._conn.commit()
    outcome = await orch.resolve_tool_approval_request(
        pending["request_id"], approved=True, session_id="owner"
    )
    assert outcome["status"] == "not_found" and seen == []


@pytest.mark.parametrize("surface", ["local_cli", "websocket", "http_api"])
async def test_actual_composer_preserves_known_runtime_surface(wired, surface):
    rt, old, seen = wired
    orch = Orchestrator(
        skill_registry=old.skills, send_to_client=AsyncMock(), daemons={}, taskflows=rt
    )
    orch.executor = old.executor
    rt._orchestrator = orch
    try:
        await orch.handle_command(
            "composer-owner",
            "Run my fixture workflow",
            context={
                "surface": surface,
                "taskflow": {
                    "steps": [
                        {
                            "type": "skill.invoke",
                            "skill_id": "notes_memory",
                            "endpoint": "search_notes",
                            "args": {"value": "inert"},
                        }
                    ],
                    "context": {"task_origin": {"surface": "local_cli"}},
                },
            },
        )
        flow = rt.list_flows()[0]
        assert (
            rt.origin_surface_for_flow(flow["id"]) == surface
            and rt.origin_session_for_flow(flow["id"]) == "composer-owner"
        )
        await rt._run_flow(flow["id"])
        assert len(seen) == 1 and seen[0][0].surface == surface
    finally:
        await orch.drain_background_tasks()


@pytest.mark.parametrize("surface", ["http_api", "cron", None])
@pytest.mark.parametrize(
    "tool", ["notes_memory__search_notes", "desktop_control__shell_command"]
)
async def test_actual_model_steps_use_creation_scope_for_reads_and_hard_denials(
    wired, surface, tool
):
    import json
    from agents.llm_provider import LLMProvider

    rt, old, seen = wired
    shell_step(rt)
    flow = rt.create_flow(
        session_id="model-owner",
        title="inert",
        steps=[{"type": "llm.chat", "prompt": "Inert model fixture"}],
        origin_surface=surface,
    )
    orch = Orchestrator(
        skill_registry=old.skills, send_to_client=AsyncMock(), daemons={}, taskflows=rt
    )
    rt._orchestrator = orch
    orch.executor = old.executor
    orch._multi_agent_enabled = False
    orch._route_prompt = AsyncMock(return_value=list(old.skills.skills.values()))
    orch._ensure_core_skills = lambda skills: skills
    orch._build_system_prompt = AsyncMock(return_value="Inert surface fixture")
    orch._force_tool_for_query = lambda *args: None
    orch.tool_runner._autonomy_mode = "loose"
    orch.llm = SimpleNamespace(
        available=True,
        model_name="inert",
        extract_response=lambda data: LLMProvider.extract_response(None, data),
    )
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "model-surface-call",
                                "type": "function",
                                "function": {
                                    "name": tool,
                                    "arguments": json.dumps(
                                        {"command": "INERT_ONLY"}
                                        if tool.startswith("desktop_control")
                                        else {"value": "inert"}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "Inert processing finished"}}]},
    ]

    async def provider(**kwargs):
        return responses.pop(0)

    orch._call_llm_chat = provider
    try:
        await rt._run_flow(flow["id"])
        if tool.startswith("desktop_control"):
            assert seen == [] and rt.get_flow(flow["id"])["status"] == "failed"
        else:
            assert len(seen) == 1 and seen[0][0].surface == (surface or "cron")
            assert rt.get_flow(flow["id"])["status"] == "completed"
    finally:
        await orch.drain_background_tasks()
