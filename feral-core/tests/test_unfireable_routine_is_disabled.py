"""A routine that can never succeed must stop, not retry every minute.

Refusing to fire a JobType.TRIGGERED routine stopped the ACTION but not the
POLL. The row stayed enabled with cron_expr "every 1m", so the scheduler
re-armed it every sixty seconds, every one of those runs recorded a
non-success, and the user got the routine_stalled alert over and over:

    "A routine has stopped working
     '[auto] smart_home_hue: trigger on sleep_detected' has run 54 times
     without succeeding once. It is still enabled and still firing.
     Last error: triggered routines are not fired: no evaluator for the
     condition, so firing on the 1m poll would run the action uncondit
     (1 other routine(s) are failing too.)"

The refusal is permanent for these rows: no future tick can make an
unevaluated condition evaluated, so every retry is guaranteed to skip. A
guaranteed-failing retry loop with a nag attached is not a safe resting
state, and neither is deleting the user's routine behind his back.

So the routine is DISABLED, with the reason stored on the row where
/api/routines, the Routines page and the routines skill can all show it,
and the user is told once. The condition itself is still watched, by
agents/trigger_conditions.py on the proactive loop, which notifies and
never dispatches the declared action.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from agents.proactive_engine import Priority, ProactiveEngine
from agents.scheduler import CronService, JobType


@pytest.fixture
def svc(tmp_path):
    return CronService(db_path=str(tmp_path / "jobs.db"))


@pytest.fixture
def brain(monkeypatch, svc):
    """Minimal state for execute_routine_job, with no skills registered."""
    from api import server as mod

    s = type("S", (), {})()
    s.cron_service = svc
    s.skill_registry = None
    s.orchestrator = None
    s.taskflows = None
    monkeypatch.setattr(mod, "state", s)
    return s


def _triggered(svc, desc="[auto] smart_home_hue: trigger on sleep_detected"):
    return svc.create_job(
        JobType.TRIGGERED,
        "every 1m",
        desc,
        {
            "skill": "smart_home_hue",
            "endpoint": "get_states",
            "trigger_event": "sleep_detected",
            "condition": "biometric.inferred_state == 'sleeping'",
        },
        "",
    )


class TestTheRetryLoopEnds:
    def test_the_routine_is_disabled_after_the_refusal(self, svc, brain):
        from api.server import execute_routine_job

        job = _triggered(svc)
        assert svc.get_job(job.id).enabled is True

        execute_routine_job(job)

        assert svc.get_job(job.id).enabled is False, (
            "the routine is still enabled, so the scheduler will fire it "
            "again in sixty seconds and skip it again, forever"
        )

    def test_it_is_no_longer_due(self, svc, brain):
        """The concrete loop: get_due_jobs is what the scheduler polls."""
        from api.server import execute_routine_job

        job = _triggered(svc)

        # Before: due once its minute is up, which is how it got 54 runs.
        svc._conn.execute(
            "UPDATE scheduled_jobs SET next_run = ? WHERE id = ?",
            (time.time() - 3600, job.id),
        )
        svc._conn.commit()
        assert [j.id for j in svc.get_due_jobs() if j.id == job.id] == [job.id]

        execute_routine_job(job)
        # mark_completed is what the scheduler runs after every callback; it
        # must not undo the disable by re-arming the row.
        svc.mark_completed(job.id)
        svc._conn.execute(
            "UPDATE scheduled_jobs SET next_run = ? WHERE id = ?",
            (time.time() - 3600, job.id),
        )
        svc._conn.commit()

        assert [j.id for j in svc.get_due_jobs() if j.id == job.id] == []

    def test_the_reason_names_the_condition_and_is_stored(self, svc, brain):
        from api.server import execute_routine_job

        job = _triggered(svc)
        execute_routine_job(job)

        reason = svc.get_job(job.id).disabled_reason
        assert reason, "disabled with no recorded reason is a silent disable"
        assert "biometric.inferred_state == 'sleeping'" in reason
        assert "resume" in reason.lower() or "delete" in reason.lower(), (
            "the reason has to tell the user what he can do about it"
        )

    def test_the_run_is_still_recorded_as_skipped(self, svc, brain):
        """Disabling must not cost the run history its explanation."""
        from api.server import execute_routine_job

        job = _triggered(svc)
        execute_routine_job(job)

        runs = svc.get_runs(job.id, limit=5)
        assert runs and runs[0]["status"] == "skipped"

    def test_a_user_paused_routine_records_no_reason(self, svc):
        """pause_job is the user's own decision; inventing an explanation for
        it would put words in his mouth in the UI."""
        job = svc.create_job(JobType.SCHEDULED, "every 10m", "mine", {}, "")
        svc.pause_job(job.id)
        assert svc.get_job(job.id).disabled_reason == ""

    def test_resuming_clears_the_reason(self, svc, brain):
        from api.server import execute_routine_job

        job = _triggered(svc)
        execute_routine_job(job)
        assert svc.get_job(job.id).disabled_reason

        assert svc.resume_job(job.id) is True
        resumed = svc.get_job(job.id)
        assert resumed.enabled is True
        assert resumed.disabled_reason == "", (
            "a stale reason on an enabled routine explains a state that no "
            "longer exists"
        )


class TestTheNagStops:
    """The user's actual complaint was the repeated alert."""

    def _stalled(self, svc):
        msgs = []
        ProactiveEngine(cron_service=svc)._check_stalled_routines(msgs)
        return msgs

    def _skipped_runs(self, svc, job_id, n):
        now = time.time() - 86400
        for i in range(n):
            svc._conn.execute(
                "INSERT INTO routine_runs (job_id, started_at, finished_at, "
                "status, result, error) VALUES (?, ?, ?, 'skipped', '{}', ?)",
                (job_id, now + i, now + i,
                 "triggered routines are not fired: no evaluator for the condition"),
            )
        svc._conn.commit()

    def test_a_still_enabled_unfireable_routine_would_nag(self, svc):
        """Pins that the alert fires for exactly this shape while enabled, so
        the test below is proving the disable silenced it and not that the
        alert never fires."""
        job = _triggered(svc)
        self._skipped_runs(svc, job.id, 54)

        msgs = self._stalled(svc)
        assert len(msgs) == 1
        assert "54 times" in msgs[0].body

    def test_once_disabled_the_stalled_alert_is_silent(self, svc, brain):
        from api.server import execute_routine_job

        job = _triggered(svc)
        self._skipped_runs(svc, job.id, 54)
        execute_routine_job(job)

        assert self._stalled(svc) == [], (
            "the routine is off and costing nothing; repeating the alert is "
            "the nag the user asked us to stop"
        )


class TestTheUserIsToldOnce:
    def _notices(self, engine, svc):
        msgs = []
        engine._check_auto_disabled_routines(msgs)
        return msgs

    def test_an_auto_disabled_routine_is_announced(self, svc, brain):
        from api.server import execute_routine_job

        job = _triggered(svc)
        execute_routine_job(job)

        engine = ProactiveEngine(cron_service=svc)
        msgs = self._notices(engine, svc)

        assert len(msgs) == 1
        assert msgs[0].trigger_id == "routine_auto_disabled"
        assert msgs[0].priority is Priority.IMPORTANT
        assert "smart_home_hue" in msgs[0].body
        assert "biometric.inferred_state == 'sleeping'" in msgs[0].body

    def test_it_is_announced_exactly_once(self, svc, brain):
        """A notice that repeats every tick is the nag wearing a new hat."""
        from api.server import execute_routine_job

        job = _triggered(svc)
        execute_routine_job(job)

        engine = ProactiveEngine(cron_service=svc)
        assert len(self._notices(engine, svc)) == 1
        assert self._notices(engine, svc) == []

    def test_the_one_shot_survives_a_restart(self, svc, brain):
        """disabled_notified is a column, not an in-memory set: a fresh engine
        (a restarted brain) must not re-announce the same disabling."""
        from api.server import execute_routine_job

        job = _triggered(svc)
        execute_routine_job(job)

        assert len(self._notices(ProactiveEngine(cron_service=svc), svc)) == 1
        assert self._notices(ProactiveEngine(cron_service=svc), svc) == []

    def test_a_user_paused_routine_is_not_announced(self, svc):
        job = svc.create_job(JobType.SCHEDULED, "every 10m", "mine", {}, "")
        svc.pause_job(job.id)

        assert self._notices(ProactiveEngine(cron_service=svc), svc) == []

    def test_several_disablings_collapse_into_one_message(self, svc, brain):
        from api.server import execute_routine_job

        a = _triggered(svc, "[auto] smart_home_hue: trigger on sleep_detected")
        b = _triggered(svc, "[auto] messaging_sms: trigger on high_stress_alert")
        execute_routine_job(a)
        execute_routine_job(b)

        engine = ProactiveEngine(cron_service=svc)
        msgs = self._notices(engine, svc)

        assert len(msgs) == 1
        assert "1 other routine(s) were turned off too." in msgs[0].body
        # Both are accounted for, so neither comes back on the next tick.
        assert self._notices(engine, svc) == []


class TestTheColumnsMigrateOntoAnExistingDatabase:
    """Every install that has this bug already has a scheduled_jobs table."""

    def test_an_old_database_gains_the_columns(self, tmp_path):
        db = str(tmp_path / "old.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE scheduled_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                cron_expr TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                session_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_run REAL,
                next_run REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                run_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO scheduled_jobs (job_type, cron_expr, description, "
            "payload, session_id, created_at, next_run) "
            "VALUES ('triggered', 'every 1m', 'legacy', '{}', '', ?, ?)",
            (time.time(), time.time()),
        )
        conn.commit()
        conn.close()

        svc = CronService(db_path=db)
        cols = {
            r[1] for r in svc._conn.execute("PRAGMA table_info(scheduled_jobs)")
        }
        assert "disabled_reason" in cols
        assert "disabled_notified" in cols

        legacy = svc.list_jobs()[0]
        assert legacy.disabled_reason == ""
        assert legacy.disabled_notified == 0
        assert svc.disable_job(legacy.id, "because") is True
        assert svc.get_job(legacy.id).disabled_reason == "because"
