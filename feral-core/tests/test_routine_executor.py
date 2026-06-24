"""L2 — routine executor wiring + cron/taskflow safety pre-flight.

Covers each dispatch branch of ``api.server.execute_routine_job``:
skill-invoke (success + DENY), workflow_id (pack instantiate), flow_id+steps
(ad-hoc TaskFlow), prompt (orchestrator), plus the mirrored DENY pre-flight in
``TaskFlowRuntime.skill.invoke``.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from agents.scheduler import CronService, JobType
from agents.taskflow import TaskFlowRuntime
from agents.persona_loader import WorkflowPackManifest, WorkflowStep
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from skills.base import BaseSkill
from skills.registry import SkillRegistry
from skills.impl import register_instance

import api.server as server


class _RecordingSkill(BaseSkill):
    def __init__(self, skill_id):
        super().__init__(skill_id=skill_id)
        self.calls = []

    async def execute(self, endpoint_id, args, vault):
        self.calls.append((endpoint_id, args))
        return {"success": True, "status_code": 200, "data": {"ran": endpoint_id}, "error": None}


def _manifest(skill_id, endpoint_id, safety_tier):
    return SkillManifest(
        skill_id=skill_id,
        brand=BrandProfile(name=skill_id, primary_color="#111"),
        description=f"test skill {skill_id}",
        endpoints=[
            SkillEndpoint(
                id=endpoint_id,
                method="PYTHON",
                url=f"python://{skill_id}/{endpoint_id}",
                description="test endpoint",
                safety_tier=safety_tier,
            )
        ],
    )


@pytest.fixture
def env():
    fd, cron_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    fd, flow_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    cron = CronService(db_path=cron_path)
    taskflows = TaskFlowRuntime(db_path=flow_path)

    reg = SkillRegistry()
    safe = _RecordingSkill("safe_skill")
    danger = _RecordingSkill("danger_skill")
    reg.register(_manifest("safe_skill", "ping", "safe"))
    reg.register(_manifest("danger_skill", "wipe", "deny"))
    register_instance("safe_skill", safe)
    register_instance("danger_skill", danger)

    pack = WorkflowPackManifest(
        workflow_id="demo_pack",
        name="Demo Pack",
        steps=[WorkflowStep(type="noop")],
    )

    saved = {
        k: getattr(server.state, k, None)
        for k in ("cron_service", "skill_registry", "taskflows", "workflow_packs", "orchestrator", "cron_cost_guard")
    }
    server.state.cron_service = cron
    server.state.skill_registry = reg
    server.state.taskflows = taskflows
    server.state.workflow_packs = {"demo_pack": pack}
    server.state.orchestrator = None
    server.state.cron_cost_guard = None

    yield {"cron": cron, "taskflows": taskflows, "safe": safe, "danger": danger, "reg": reg}

    for k, v in saved.items():
        setattr(server.state, k, v)
    cron.close()
    os.unlink(cron_path)
    os.unlink(flow_path)


def _latest_run(cron, job_id):
    runs = cron.get_runs(job_id, limit=1)
    return runs[0] if runs else None


def test_skill_branch_runs_and_records(env):
    cron = env["cron"]
    job = cron.create_job(JobType.SCHEDULED, "every 30m", "safe", {"skill": "safe_skill", "endpoint": "ping", "args": {"x": 1}}, "")
    server.execute_routine_job(job)
    assert env["safe"].calls == [("ping", {"x": 1})]
    run = _latest_run(cron, job.id)
    assert run["status"] == "success"


def test_skill_branch_deny_is_skipped(env):
    cron = env["cron"]
    job = cron.create_job(JobType.SCHEDULED, "every 30m", "danger", {"skill": "danger_skill", "endpoint": "wipe"}, "")
    server.execute_routine_job(job)
    # DENY → skill must NOT have executed.
    assert env["danger"].calls == []
    run = _latest_run(cron, job.id)
    assert run["status"] == "skipped"
    assert "denied by safety policy" in (run["error"] or "")


def test_workflow_id_branch_instantiates_pack(env):
    cron = env["cron"]
    job = cron.create_job(JobType.SCHEDULED, "every 30m", "wf", {"workflow_id": "demo_pack"}, "")
    server.execute_routine_job(job)
    run = _latest_run(cron, job.id)
    assert run["status"] == "success"
    assert run["result"].get("flow_id")
    flows = env["taskflows"].list_flows()
    assert any(f["id"] == run["result"]["flow_id"] for f in flows)


def test_workflow_id_unknown_pack_records_error(env):
    cron = env["cron"]
    job = cron.create_job(JobType.SCHEDULED, "every 30m", "wf", {"workflow_id": "nope"}, "")
    server.execute_routine_job(job)
    run = _latest_run(cron, job.id)
    assert run["status"] == "error"
    assert "Unknown workflow pack" in (run["error"] or "")


def test_flow_id_inline_steps_branch(env):
    cron = env["cron"]
    payload = {"flow_id": "adhoc", "steps": [{"type": "noop"}, {"type": "noop"}]}
    job = cron.create_job(JobType.SCHEDULED, "every 30m", "adhoc", payload, "")
    server.execute_routine_job(job)
    run = _latest_run(cron, job.id)
    assert run["status"] == "success"
    assert run["result"].get("flow_id")


def test_action_text_routes_to_prompt(env):
    cron = env["cron"]
    captured = {}

    class _Orch:
        async def handle_command(self, session_id, prompt, context=None):
            captured["prompt"] = prompt
            captured["context"] = context
            return {"ok": True}

    server.state.orchestrator = _Orch()
    job = cron.create_job(JobType.CUSTOM, "every 30m", "nl", {"action_text": "follow the line"}, "")
    server.execute_routine_job(job)
    assert captured["prompt"] == "follow the line"
    assert captured["context"]["source"] == "cron"
    run = _latest_run(cron, job.id)
    assert run["status"] == "success"


@pytest.mark.asyncio
async def test_taskflow_skill_invoke_deny(env):
    taskflows = env["taskflows"]
    taskflows._skill_registry = env["reg"]
    await taskflows.start()
    try:
        flow = taskflows.create_flow(
            session_id="s",
            title="deny flow",
            steps=[{"type": "skill.invoke", "skill_id": "danger_skill", "endpoint": "wipe"}],
        )
        fid = flow["id"]
        deadline = time.time() + 5
        latest = flow
        while time.time() < deadline:
            latest = taskflows.get_flow(fid)
            if latest and latest["status"] in ("failed", "completed"):
                break
            await asyncio.sleep(0.1)
        assert latest["status"] == "failed"
        assert env["danger"].calls == []
        step = latest["steps"][0]
        assert "denied by safety policy" in (step["error"] or "")
    finally:
        await taskflows.stop()
