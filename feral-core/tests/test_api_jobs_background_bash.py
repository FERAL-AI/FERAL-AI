"""/api/jobs must report backgrounded shell commands.

`coding_tools__bash` with `run_in_background` starts a real detached
process, holds a StayAwake assertion for its lifetime, and outlives the
turn that started it. It was absent from the /api/jobs aggregator
entirely, so a build or test run kicked off from chat appeared nowhere:
the "what is the brain doing right now" endpoint said nothing was
happening while a process group was running.

These tests drive a real background job through the real skill rather
than stubbing the job table, because the two defects worth catching here
(the store not being reachable from the route, and the clock mismatch
below) both survive a mocked store.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.jobs import router as jobs_router
from security.sandbox_policy import SandboxPolicy
from skills.call_context import bind_context
from skills.impl import get_implementation
from skills.registry import SkillRegistry

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture(scope="module")
def registry():
    reg = SkillRegistry()
    reg.load_builtin_skills()
    return reg


@pytest.fixture
def granted_cwd(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    SandboxPolicy.load_default().grant_folder(str(d), mode="readwrite")
    return str(d)


@pytest.fixture
def jobs_client():
    app = FastAPI()
    app.include_router(jobs_router)
    return TestClient(app, raise_server_exceptions=False)


def _start_job(skill, cwd, session_id, command="sleep 30"):
    async def go():
        with bind_context(session_id=session_id, tool_name="coding_tools__bash"):
            return await skill.execute(
                "bash",
                {"command": command, "run_in_background": True, "cwd": cwd},
                {},
            )
    started = asyncio.run(go())
    assert started.get("status_code") == 202, started
    return (started.get("data") or {})


def _cleanup(skill, session_id):
    """Reap the real processes these tests start.

    `clear_session` is a coroutine. Calling it without awaiting raises no
    error, returns a coroutine object that is quietly discarded, and
    leaves a real `sleep 30` process group running for the rest of the
    suite. The first version of this helper did exactly that.
    """
    try:
        asyncio.run(skill.clear_session(session_id))
    except Exception:
        pass


def test_a_running_background_job_appears_in_api_jobs(registry, granted_cwd, jobs_client):
    """The route reaches the same skill instance the tool writes to.

    `_background_bash_jobs` finds the job store through
    `skills.impl.get_implementation`. If that ever returns a different
    object than the one `execute` mutates, the aggregator quietly reports
    an empty list, which is the failure it exists to prevent.
    """
    skill = registry.get_skill("coding_tools")
    assert skill is get_implementation("coding_tools"), (
        "the route reads a different instance than the tool writes to"
    )
    data = _start_job(skill, granted_cwd, "bg-visible")
    try:
        body = jobs_client.get("/api/jobs").json()
        assert body["degraded"] == {}, body["degraded"]
        mine = [i for i in body["items"] if i["kind"] == "background_bash"]
        assert mine, "a running background job is missing from /api/jobs"
        row = next(i for i in mine if i["detail"]["pid"] == data["pid"])
        assert row["status"] == "running"
        assert row["context_session_id"] == "bg-visible"
        assert "sleep 30" in row["name"]
        assert row["detail"]["cwd"] == granted_cwd
    finally:
        _cleanup(skill, "bg-visible")


def test_started_at_is_wall_clock_not_monotonic(registry, granted_cwd, jobs_client):
    """The one number that silently breaks the merged list.

    `_BackgroundJob.started_at` is `time.monotonic()`, because the pruner
    measures job age with it. Every other aggregator reports wall-clock
    epoch and the combiner sorts on that. Passing the monotonic value
    through unconverted is not a visible error: it is a plausible small
    float that sorts every background job below everything else forever.
    """
    skill = registry.get_skill("coding_tools")
    data = _start_job(skill, granted_cwd, "bg-clock")
    try:
        body = jobs_client.get("/api/jobs").json()
        row = next(
            i for i in body["items"]
            if i["kind"] == "background_bash" and i["detail"]["pid"] == data["pid"]
        )
        started = row["started_at"]
        assert abs(started - time.time()) < 30, (
            f"started_at {started} is not wall-clock epoch; monotonic is "
            f"{time.monotonic()} and time.time() is {time.time()}"
        )
    finally:
        _cleanup(skill, "bg-clock")


def test_background_jobs_sort_alongside_the_other_kinds(registry, granted_cwd, jobs_client):
    """A job started just now must not land at the bottom of the list.

    This is the visible symptom of the clock bug, asserted directly: the
    combiner sorts by -started_at, so a monotonic value would put a job
    that started one second ago below every other source.
    """
    skill = registry.get_skill("coding_tools")
    data = _start_job(skill, granted_cwd, "bg-sort")
    try:
        items = jobs_client.get("/api/jobs").json()["items"]
        stamped = [i for i in items if i.get("started_at")]
        assert stamped, "nothing reported a start time"
        newest = max(stamped, key=lambda i: i["started_at"])
        assert newest["kind"] == "background_bash"
        assert newest["detail"]["pid"] == data["pid"]
    finally:
        _cleanup(skill, "bg-sort")


def test_kind_filter_selects_background_bash(registry, granted_cwd, jobs_client):
    skill = registry.get_skill("coding_tools")
    _start_job(skill, granted_cwd, "bg-filter")
    try:
        body = jobs_client.get("/api/jobs?kind=background_bash").json()
        assert body["items"], "kind filter returned nothing"
        assert {i["kind"] for i in body["items"]} == {"background_bash"}
        assert set(body["counts_by_kind"]) == {"background_bash"}
    finally:
        _cleanup(skill, "bg-filter")


def test_no_background_jobs_is_not_a_degraded_source(jobs_client):
    """An idle system reports an empty list, not a failure."""
    body = jobs_client.get("/api/jobs").json()
    assert "background_bash" not in body["degraded"], body["degraded"]


def test_a_finished_job_is_not_reported_as_work_in_progress(
    registry, granted_cwd, jobs_client
):
    """The store keeps finished jobs; this endpoint must not list them.

    `_prune_background_jobs` deliberately retains completed jobs so their
    output stays readable (BG_FINISHED_TTL_SEC, BG_FINISHED_RETENTION).
    The first version of this aggregator walked the whole store, so a
    `true` that exited immediately kept showing up as active work for the
    entire retention window and an idle brain reported jobs running. It
    was caught by an unrelated test asserting an idle system reports
    nothing, only once the full suite ran in one process.
    """
    skill = registry.get_skill("coding_tools")

    async def scenario():
        # Start and observe in ONE loop. The supervisor only marks a
        # handle finished while its loop is running, so starting the job
        # in one asyncio.run() and checking after it closed leaves the
        # job permanently "running" and the test proves nothing.
        with bind_context(session_id="bg-finished", tool_name="coding_tools__bash"):
            started = await skill.execute(
                "bash",
                {"command": "true", "run_in_background": True, "cwd": granted_cwd},
                {},
            )
        assert started.get("status_code") == 202, started
        pid = (started.get("data") or {})["pid"]

        for _ in range(50):  # up to ~5s
            if any(j.finished for j in skill._bg_jobs.values() if j.pid == pid):
                break
            await asyncio.sleep(0.1)

        assert any(j.finished for j in skill._bg_jobs.values() if j.pid == pid), (
            "the job never finished, so this test proves nothing"
        )
        # The record is still in the store; that is the point. Only the
        # endpoint's view of it should have changed.
        assert any(j.pid == pid for j in skill._bg_jobs.values()), (
            "the store dropped the record, so this is not testing the filter"
        )
        return pid

    pid = asyncio.run(scenario())

    items = jobs_client.get("/api/jobs?kind=background_bash").json()["items"]
    assert all(i["detail"]["pid"] != pid for i in items), (
        "a finished background job is still being reported as active work"
    )


def test_cancellable_via_does_not_name_a_route_that_does_not_exist(
    registry, granted_cwd, jobs_client
):
    """Killing a background job is a tool call, not an HTTP route.

    `cancellable_via` is a promise a client will act on, so claiming a
    route here would produce a button that 404s.
    """
    skill = registry.get_skill("coding_tools")
    _start_job(skill, granted_cwd, "bg-cancel")
    try:
        items = jobs_client.get("/api/jobs?kind=background_bash").json()["items"]
        assert all(i["cancellable_via"] is None for i in items)
    finally:
        _cleanup(skill, "bg-cancel")
