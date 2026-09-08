"""Creating the same routine twice must not make two routines.

`feral_routines.create` had no duplicate check, and nothing upstream
stops a tool that keeps SUCCEEDING. `IterationBudget.observe` resets
every streak the moment a call succeeds:

    if success:
        self._last_sig = None
        self._streak = 0
        self._call_streak = 0
        return GUARD_OK

so the no-progress guard, the stop guard and the precondition guard all
watch failures and none of them sees a run of successful writes. The
only remaining bound is `DEFAULT_TOOL_LOOP_MAX_SECONDS`, fifteen
minutes of wall clock.

Measured on 2026-09-07: one turn called this endpoint 30 times and
wrote 29 near-identical routines into the operator's brain, each
reported as a success, none of them stopped. They had to be deleted by
hand.

Idempotency is the fix that does not require guessing a threshold. An
identical routine is a repeat, not a second intention. Two routines
that differ in schedule, description or action are two real routines
and are still both created, because a person genuinely may want the
same action on two schedules or two actions on one.
"""

from __future__ import annotations

import pytest

from agents.scheduler import CronService, JobType


@pytest.fixture
def scheduler(tmp_path):
    svc = CronService(db_path=str(tmp_path / "jobs.db"))
    yield svc
    svc.close()


def _same(svc, *, cron="daily 21:00", desc="spin the cutebot", payload=None):
    """What the skill compares. Kept in one place so the test and the
    implementation cannot drift apart silently."""
    payload = payload or {"skill_id": "cutebot", "endpoint": "spin"}
    for job in svc.list_jobs():
        if (
            getattr(job, "enabled", False)
            and (job.cron_expr or "") == cron
            and (job.description or "") == desc
            and (job.payload or {}) == payload
        ):
            return job
    return None


class TestTheDuplicateRule:
    def test_an_identical_routine_is_found_and_not_recreated(self, scheduler):
        p = {"skill_id": "cutebot", "endpoint": "spin"}
        scheduler.create_job(JobType.SCHEDULED, "daily 21:00", "spin the cutebot", p, "s1")
        assert _same(scheduler) is not None, "the duplicate check cannot see its own row"
        assert len(scheduler.list_jobs()) == 1

    def test_a_different_schedule_is_a_different_routine(self, scheduler):
        p = {"skill_id": "cutebot", "endpoint": "spin"}
        scheduler.create_job(JobType.SCHEDULED, "daily 21:00", "spin the cutebot", p, "s1")
        assert _same(scheduler, cron="daily 07:00") is None

    def test_a_different_action_is_a_different_routine(self, scheduler):
        scheduler.create_job(JobType.SCHEDULED, "daily 21:00", "spin the cutebot",
                             {"skill_id": "cutebot", "endpoint": "spin"}, "s1")
        assert _same(scheduler, payload={"skill_id": "cutebot", "endpoint": "halt"}) is None

    def test_a_different_description_is_a_different_routine(self, scheduler):
        p = {"skill_id": "cutebot", "endpoint": "spin"}
        scheduler.create_job(JobType.SCHEDULED, "daily 21:00", "spin the cutebot", p, "s1")
        assert _same(scheduler, desc="spin the cutebot twice") is None

    def test_a_disabled_routine_does_not_suppress_a_new_one(self, scheduler):
        """Turning a routine off is not the same as still having it."""
        p = {"skill_id": "cutebot", "endpoint": "spin"}
        job = scheduler.create_job(JobType.SCHEDULED, "daily 21:00", "spin the cutebot", p, "s1")
        scheduler.disable_job(job.id, "turned off by the operator")
        assert _same(scheduler) is None


def test_the_skill_actually_performs_this_check():
    """Pins the wiring. The rule is worthless if create never runs it."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "skills" / "impl" / "feral_routines.py").read_text()
    assert "duplicate_of_existing" in src, "create does not report a suppressed duplicate"
    assert "scheduler.list_jobs()" in src, "create never looks for an existing routine"
    idx_check = src.index("duplicate_of_existing")
    idx_create = src.index("scheduler.create_job(")
    assert idx_check < idx_create, "the duplicate check runs after the row is already written"
