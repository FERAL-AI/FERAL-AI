"""L1 — feral_routines as a first-class LLM tool.

Verifies the tool registers, that `create` produces a real CronService job
with a correct next_run (incl. natural-language schedule normalization), and
that list/delete/pause/resume delegate correctly.
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from agents.scheduler import CronService
from skills.registry import SkillRegistry
import skills.impl.feral_routines as feral_routines


@pytest.fixture
def scheduler():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    svc = CronService(db_path=path)
    feral_routines.set_scheduler_override(svc)
    yield svc
    feral_routines.set_scheduler_override(None)
    svc.close()
    os.unlink(path)


@pytest.fixture
def skill():
    return feral_routines.FeralRoutinesSkill()


def test_tool_registers_in_registry():
    reg = SkillRegistry()
    reg.load_builtin_skills()
    assert "feral_routines" in reg.skills
    tool_names = {t["function"]["name"] for t in reg.get_all_tools()}
    assert "feral_routines__create" in tool_names
    assert "feral_routines__list" in tool_names
    assert "feral_routines__delete" in tool_names
    assert "feral_routines__pause" in tool_names
    assert "feral_routines__resume" in tool_names


@pytest.mark.asyncio
async def test_create_interval_routine_next_run(scheduler, skill):
    before = time.time()
    res = await skill.execute("create", {"cron_expr": "every 30m", "description": "ping"}, {})
    assert res["success"] is True
    routine = res["data"]["routine"]
    assert routine["cron_expr"] == "every 30m"
    # next_run is ~30 minutes out (interval schedule).
    assert 1700 <= (routine["next_run"] - before) <= 1900
    # A real job landed in the CronService store.
    jobs = scheduler.list_jobs()
    assert any(j.id == routine["id"] for j in jobs)


@pytest.mark.asyncio
async def test_create_normalizes_natural_language(scheduler, skill):
    res = await skill.execute(
        "create",
        {"cron_expr": "every day at 5pm", "prompt": "follow the line and report"},
        {},
    )
    assert res["success"] is True
    routine = res["data"]["routine"]
    # NL → canonical daily form.
    assert routine["cron_expr"] == "daily 17:00"
    # prompt payload survives for the executor branch.
    assert routine["payload"].get("prompt") == "follow the line and report"


@pytest.mark.asyncio
async def test_create_skill_payload(scheduler, skill):
    res = await skill.execute(
        "create",
        {
            "cron_expr": "0 7 * * *",
            "skill_id": "calendar_google",
            "endpoint_id": "get_today",
            "args": {"days_ahead": 1},
        },
        {},
    )
    assert res["success"] is True
    payload = res["data"]["routine"]["payload"]
    assert payload["skill"] == "calendar_google"
    assert payload["endpoint"] == "get_today"
    assert payload["args"] == {"days_ahead": 1}


@pytest.mark.asyncio
async def test_create_requires_schedule(scheduler, skill):
    res = await skill.execute("create", {"description": "no schedule"}, {})
    assert res["success"] is False
    assert res["status_code"] == 400
    assert res["field"] == "cron_expr"


@pytest.mark.asyncio
async def test_list_delete_pause_resume(scheduler, skill):
    created = await skill.execute("create", {"cron_expr": "every 15m", "session_id": "s1"}, {})
    rid = created["data"]["routine"]["id"]

    listed = await skill.execute("list", {}, {})
    assert listed["success"] is True
    assert listed["data"]["count"] >= 1
    assert any(r["id"] == rid for r in listed["data"]["routines"])

    paused = await skill.execute("pause", {"routine_id": rid}, {})
    assert paused["success"] is True
    assert scheduler.get_job(rid).enabled is False

    resumed = await skill.execute("resume", {"routine_id": rid}, {})
    assert resumed["success"] is True
    assert scheduler.get_job(rid).enabled is True

    deleted = await skill.execute("delete", {"routine_id": rid}, {})
    assert deleted["success"] is True
    assert scheduler.get_job(rid) is None


@pytest.mark.asyncio
async def test_mutate_requires_id(scheduler, skill):
    res = await skill.execute("delete", {}, {})
    assert res["success"] is False
    assert res["field"] == "routine_id"
