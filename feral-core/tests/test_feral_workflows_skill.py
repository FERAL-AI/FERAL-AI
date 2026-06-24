"""L3 — feral_workflows as a first-class LLM tool."""
from __future__ import annotations

import os
import tempfile

import pytest

from agents.taskflow import TaskFlowRuntime
from agents.persona_loader import WorkflowPackManifest, WorkflowStep
from skills.registry import SkillRegistry
import skills.impl.feral_workflows as feral_workflows


@pytest.fixture
def taskflows():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    rt = TaskFlowRuntime(db_path=path)
    pack = WorkflowPackManifest(
        workflow_id="demo_pack",
        name="Demo Pack",
        steps=[WorkflowStep(type="noop"), WorkflowStep(type="noop")],
    )
    feral_workflows.set_overrides(taskflows=rt, packs={"demo_pack": pack})
    yield rt
    feral_workflows.set_overrides(None, None)
    os.unlink(path)


@pytest.fixture
def skill():
    return feral_workflows.FeralWorkflowsSkill()


def test_tool_registers():
    reg = SkillRegistry()
    reg.load_builtin_skills()
    assert "feral_workflows" in reg.skills
    names = {t["function"]["name"] for t in reg.get_all_tools()}
    assert {"feral_workflows__create", "feral_workflows__list", "feral_workflows__get",
            "feral_workflows__cancel", "feral_workflows__instantiate_pack"} <= names


@pytest.mark.asyncio
async def test_create_queues_flow(taskflows, skill):
    res = await skill.execute(
        "create",
        {"title": "two step", "steps": [{"type": "noop"}, {"type": "note.save", "content": "x"}], "session_id": "s1"},
        {},
    )
    assert res["success"] is True
    flow = res["data"]["flow"]
    assert flow["status"] == "queued"
    assert len(flow["steps"]) == 2
    assert taskflows.get_flow(flow["id"]) is not None


@pytest.mark.asyncio
async def test_create_rejects_empty_steps(taskflows, skill):
    res = await skill.execute("create", {"title": "empty", "steps": []}, {})
    assert res["success"] is False
    assert res["field"] == "steps"


@pytest.mark.asyncio
async def test_list_and_cancel(taskflows, skill):
    created = await skill.execute("create", {"title": "c", "steps": [{"type": "sleep", "seconds": 60}], "session_id": "s2"}, {})
    fid = created["data"]["flow"]["id"]

    listed = await skill.execute("list", {"session_id": "s2"}, {})
    assert listed["success"] is True
    assert any(f["id"] == fid for f in listed["data"]["flows"])

    got = await skill.execute("get", {"flow_id": fid}, {})
    assert got["success"] is True
    assert got["data"]["flow"]["id"] == fid

    cancelled = await skill.execute("cancel", {"flow_id": fid}, {})
    assert cancelled["success"] is True
    assert cancelled["data"]["flow"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_instantiate_pack(taskflows, skill):
    res = await skill.execute("instantiate_pack", {"workflow_id": "demo_pack", "session_id": "s3"}, {})
    assert res["success"] is True
    assert res["data"]["workflow_id"] == "demo_pack"
    flow = res["data"]["flow"]
    assert len(flow["steps"]) == 2
    assert taskflows.get_flow(flow["id"]) is not None


@pytest.mark.asyncio
async def test_instantiate_unknown_pack(taskflows, skill):
    res = await skill.execute("instantiate_pack", {"workflow_id": "nope"}, {})
    assert res["success"] is False
    assert res["status_code"] == 404
