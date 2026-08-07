"""Nothing has ever watched whether a routine achieves anything.

record_run_finish has always written every outcome to routine_runs, and
no code path has ever read it back to raise. On the install where this
was found, one routine failed DNS resolution 4,824 times out of 4,824
over six weeks while still enabled and still firing every minute, and
another failed 23 out of 23 against a calendar that was never connected.
The user discovered both from a manual database query during an audit.

That is the failure the user described in his own words: a routine that
fires at something it is not connected to, wastes the run, and nobody is
told. A scheduled task is a promise the machine made. Breaking it every
sixty seconds for six weeks in silence is worse than never offering.

Fired at IMPORTANT so it escalates past the browser to a human who is not
looking at a screen, and rate limited hard, because the target is the
slow silent death of something set up and forgotten, not a flaky morning.
"""

from __future__ import annotations

import time

import pytest

from agents.proactive_engine import Priority, ProactiveEngine
from agents.scheduler import CronService, JobType


@pytest.fixture
def svc(tmp_path):
    return CronService(db_path=str(tmp_path / "jobs.db"))


def _job(svc, description="nightly thing", enabled=True):
    job = svc.create_job(
        JobType.SCHEDULED, "0 7 * * *", description, {"prompt": "x"}, "",
    )
    if not enabled:
        svc._conn.execute("UPDATE scheduled_jobs SET enabled = 0 WHERE id = ?", (job.id,))
        svc._conn.commit()
    return job


def _runs(svc, job_id, n, status, error=None, age_days=1.0):
    now = time.time() - age_days * 86400
    for i in range(n):
        svc._conn.execute(
            "INSERT INTO routine_runs (job_id, started_at, finished_at, status, result, error) "
            "VALUES (?, ?, ?, ?, '{}', ?)",
            (job_id, now + i, now + i, status, error),
        )
    svc._conn.commit()


def _check(svc):
    msgs = []
    ProactiveEngine(cron_service=svc)._check_stalled_routines(msgs)
    return msgs


class TestItNoticesADeadRoutine:
    def test_a_routine_that_never_succeeds_is_reported(self, svc):
        job = _job(svc, "[auto] smart_home_hue: trigger on sleep_detected")
        _runs(svc, job.id, 60, "error", error="[Errno 8] nodename nor servname provided")

        msgs = _check(svc)

        assert len(msgs) == 1
        assert msgs[0].trigger_id == "routine_stalled"
        assert "60 times" in msgs[0].body
        assert "smart_home_hue" in msgs[0].body

    def test_it_is_important_enough_to_escalate(self, svc):
        """SUGGESTION would be delivered only to an open browser tab, which
        is exactly where these failures went unseen for six weeks."""
        job = _job(svc)
        _runs(svc, job.id, 60, "error")
        assert _check(svc)[0].priority is Priority.IMPORTANT

    def test_the_real_error_is_carried_to_the_user(self, svc):
        """"It failed" sends someone hunting. The DNS error names the fix."""
        job = _job(svc)
        _runs(svc, job.id, 60, "error", error="[Errno 8] nodename nor servname provided")
        assert "Errno 8" in _check(svc)[0].body

    def test_additional_broken_routines_are_counted(self, svc):
        a, b = _job(svc, "first"), _job(svc, "second")
        _runs(svc, a.id, 60, "error")
        _runs(svc, b.id, 30, "error")

        body = _check(svc)[0].body

        assert "1 other routine" in body


class TestItStaysQuietWhenItShould:
    def test_a_working_routine_is_not_reported(self, svc):
        job = _job(svc)
        _runs(svc, job.id, 60, "success")
        assert _check(svc) == []

    def test_one_success_among_many_failures_is_not_a_stall(self, svc):
        """The trigger is for something that never works, not something
        unreliable. Anything else nags."""
        job = _job(svc)
        _runs(svc, job.id, 59, "error")
        _runs(svc, job.id, 1, "success")
        assert _check(svc) == []

    def test_a_handful_of_failures_is_not_yet_a_stall(self, svc):
        job = _job(svc)
        _runs(svc, job.id, 3, "error")
        assert _check(svc) == []

    def test_a_disabled_routine_is_not_reported(self, svc):
        """Already turned off, so there is nothing to tell anyone."""
        job = _job(svc, enabled=False)
        _runs(svc, job.id, 60, "error")
        assert _check(svc) == []

    def test_old_failures_outside_the_window_are_ignored(self, svc):
        """A routine that broke last year and was left alone is not news
        today."""
        job = _job(svc)
        _runs(svc, job.id, 60, "error", age_days=90)
        assert _check(svc) == []

    def test_the_cooldown_stops_it_repeating(self, svc):
        job = _job(svc)
        _runs(svc, job.id, 60, "error")

        engine = ProactiveEngine(cron_service=svc)
        first, second = [], []
        engine._check_stalled_routines(first)
        engine._record_fire("routine_stalled")
        engine._check_stalled_routines(second)

        assert len(first) == 1 and second == []


class TestItCannotBreakTheEngine:
    def test_no_cron_service_is_a_no_op(self):
        msgs = []
        ProactiveEngine()._check_stalled_routines(msgs)
        assert msgs == []

    def test_a_failing_query_warns_rather_than_raising(self, svc, caplog):
        """A watcher that cannot watch is the exact silence this exists to
        end, so it must not fail quietly."""
        class Broken:
            class _conn:
                @staticmethod
                def execute(*a, **k):
                    raise RuntimeError("database is locked")

        msgs = []
        with caplog.at_level("WARNING"):
            ProactiveEngine(cron_service=Broken())._check_stalled_routines(msgs)

        assert msgs == []
        assert "database is locked" in caplog.text
