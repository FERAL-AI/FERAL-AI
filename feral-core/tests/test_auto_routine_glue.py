"""L5 — manifest cron → CronService auto-routine glue + boot order + dedupe."""
from __future__ import annotations

import os
import tempfile

import pytest

from agents.scheduler import CronService
from models.skill_manifest import (
    BrandProfile,
    CronDefinition,
    FlowStep,
    SkillEndpoint,
    SkillFlow,
    SkillManifest,
)
from skills.registry import SkillRegistry


@pytest.fixture
def cron():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    svc = CronService(db_path=path)
    yield svc
    svc.close()
    os.unlink(path)


def _manifest_with_cron():
    return SkillManifest(
        skill_id="calendar_google",
        brand=BrandProfile(name="Calendar", primary_color="#4285F4"),
        description="cal",
        endpoints=[
            SkillEndpoint(id="get_today", method="PYTHON", url="python://calendar_google/get_today", description="today"),
        ],
        crons=[CronDefinition(id="morning_briefing", schedule="0 7 * * *", endpoint_id="get_today")],
    )


def _auto_jobs(cron):
    return [j for j in cron.list_jobs() if j.description.startswith("[auto] ")]


def test_set_cron_service_rescan_registers_manifest_cron(cron):
    """Mirrors real boot order: skills loaded before the CronService exists."""
    reg = SkillRegistry()
    reg.register(_manifest_with_cron())  # cron service not wired yet → no job
    assert _auto_jobs(cron) == [] and len(cron.list_jobs()) == 0

    reg.set_cron_service(cron)  # wiring triggers the rescan

    jobs = _auto_jobs(cron)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.cron_expr == "0 7 * * *"
    assert job.payload["skill"] == "calendar_google"
    assert job.payload["endpoint"] == "get_today"
    # next_run is in the future.
    import time
    assert job.next_run > time.time()


def test_register_after_wiring_creates_one_job(cron):
    reg = SkillRegistry()
    reg.set_cron_service(cron)
    reg.register(_manifest_with_cron())
    assert len(_auto_jobs(cron)) == 1


def test_dedupe_no_duplicate_on_repeat_scan(cron):
    reg = SkillRegistry()
    reg.set_cron_service(cron)
    reg.register(_manifest_with_cron())
    # Simulate a second boot / repeated wiring.
    reg._rescan_auto_routines()
    reg.set_cron_service(cron)
    assert len(_auto_jobs(cron)) == 1


def test_cron_flow_id_routes_to_taskflow_steps(cron):
    reg = SkillRegistry()
    reg.set_cron_service(cron)
    manifest = SkillManifest(
        skill_id="pipe_skill",
        brand=BrandProfile(name="Pipe", primary_color="#111"),
        description="pipe",
        endpoints=[
            SkillEndpoint(id="a", method="PYTHON", url="python://pipe_skill/a", description="a"),
            SkillEndpoint(id="b", method="PYTHON", url="python://pipe_skill/b", description="b"),
        ],
        flows=[SkillFlow(id="pipeline", description="ab", steps=[FlowStep(endpoint_id="a"), FlowStep(endpoint_id="b")])],
        crons=[CronDefinition(id="run_pipe", schedule="daily 09:00", flow_id="pipeline")],
    )
    reg.register(manifest)
    jobs = [j for j in _auto_jobs(cron) if j.payload.get("flow_id")]
    assert len(jobs) == 1
    payload = jobs[0].payload
    assert payload["flow_id"] == "pipeline"
    assert payload["steps"] == [
        {"type": "skill.invoke", "skill_id": "pipe_skill", "endpoint": "a"},
        {"type": "skill.invoke", "skill_id": "pipe_skill", "endpoint": "b"},
    ]
