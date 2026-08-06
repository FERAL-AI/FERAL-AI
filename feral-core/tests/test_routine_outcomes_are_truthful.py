"""A routine that did not act must not record a success.

Two defects in execute_routine_job, both found in the live run history
rather than by reading:

1. Falling off the end of the dispatch chain recorded ``success`` with
   "No skill or prompt configured; routine logged." The outcome was wrong
   and so was the diagnosis. The usual way to reach that line is a routine
   that DOES configure a skill whose id is not in the registry: the skill
   branch declines to run it and execution continues past every other
   branch. On this install one routine collected 4,765 of those greens
   without ever acting.

2. ``JobType.TRIGGERED`` routines are created by skills/registry.py with
   cron_expr "every 1m" and the firing condition stashed in the payload,
   and nothing anywhere reads that condition. The action therefore ran
   once a minute, unconditionally. The two on this install had 4,766 runs
   each since 2026-06-24. One was a Hue read that failed DNS 4,763 times.
   The other was messaging_sms.telegram_send gated on
   "heart_rate_bpm > 160 && inferred_state == 'stressed'": a send, which
   stayed inert only because that skill is not installed. Registering it
   would have started a stress alert every sixty seconds forever, on a
   condition that was never evaluated, let alone true.

The equality in that guard is itself pinned below. JobType subclasses str,
but ``str(JobType.TRIGGERED)`` is "JobType.TRIGGERED", so the obvious
string test silently never matches and the guard is a no-op.
"""

from __future__ import annotations

import json

import pytest

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


def _last_run(svc, job_id):
    row = svc._conn.execute(
        "SELECT status, result, error FROM routine_runs WHERE job_id = ? "
        "ORDER BY started_at DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    assert row is not None, "no run was recorded"
    return row["status"], json.loads(row["result"]), row["error"]


class TestTriggeredRoutinesDoNotFireBlind:
    def test_a_triggered_routine_does_not_run_its_action(self, svc, brain):
        from api.server import execute_routine_job

        job = svc.create_job(
            JobType.TRIGGERED,
            "every 1m",
            "[auto] messaging_sms: trigger on high_stress_alert",
            {
                "skill": "messaging_sms",
                "endpoint": "telegram_send",
                "trigger_event": "high_stress_alert",
                "condition": "biometric.heart_rate_bpm > 160",
            },
            "",
        )
        execute_routine_job(job)

        status, result, error = _last_run(svc, job.id)
        assert status == "skipped", f"triggered routine ran anyway: {status}"
        assert result["reason"] == "trigger_conditions_not_evaluated"
        assert "biometric.heart_rate_bpm > 160" in result["condition"]
        assert error

    def test_the_action_is_never_dispatched(self, svc, brain, monkeypatch):
        """The point of the skip is that the send does not happen."""
        from api import server as mod

        dispatched = []

        class Registry:
            def get_skill(self, sid):
                dispatched.append(sid)
                raise AssertionError("a triggered routine dispatched its skill")

        brain.skill_registry = Registry()
        monkeypatch.setattr(mod, "state", brain)

        job = svc.create_job(
            JobType.TRIGGERED, "every 1m", "trigger",
            {"skill": "messaging_sms", "endpoint": "telegram_send",
             "condition": "never true"},
            "",
        )
        mod.execute_routine_job(job)

        assert dispatched == []

    def test_the_job_type_comparison_actually_matches(self):
        """str(JobType.TRIGGERED) is "JobType.TRIGGERED". A guard written as
        str(...).endswith("triggered") is always False, so this pins the
        property the guard relies on rather than the guard's spelling."""
        assert str(JobType.TRIGGERED) != "triggered"
        assert JobType.TRIGGERED.value == "triggered"
        assert getattr(JobType.TRIGGERED, "value", JobType.TRIGGERED) == "triggered"

    def test_scheduled_routines_are_unaffected(self, svc, brain):
        """Only TRIGGERED is gated; a normal cron routine still runs its
        dispatch chain and reports on it."""
        from api.server import execute_routine_job

        job = svc.create_job(
            JobType.SCHEDULED, "0 7 * * *", "morning", {"skill": "nope", "endpoint": "x"}, "",
        )
        execute_routine_job(job)

        status, _, _ = _last_run(svc, job.id)
        assert status != "skipped"


class TestFallingOffTheEndIsNotSuccess:
    def test_an_unregistered_skill_is_an_error_naming_the_skill(self, svc, brain):
        """The 4,765-green case. The old code called this success and blamed
        a missing configuration that was in fact present."""
        from api.server import execute_routine_job

        job = svc.create_job(
            JobType.SCHEDULED, "0 7 * * *", "morning",
            {"skill": "messaging_sms", "endpoint": "telegram_send"}, "",
        )
        execute_routine_job(job)

        status, result, error = _last_run(svc, job.id)
        assert status == "error", f"a routine that did nothing reported {status}"
        assert "messaging_sms" in error
        assert "No skill or prompt configured" not in json.dumps(result)

    def test_a_genuinely_empty_routine_says_so(self, svc, brain):
        from api.server import execute_routine_job

        job = svc.create_job(JobType.SCHEDULED, "0 7 * * *", "empty", {}, "")
        execute_routine_job(job)

        status, _, error = _last_run(svc, job.id)
        assert status == "error"
        assert "no skill, prompt, workflow or flow" in error

    def test_a_skill_without_an_endpoint_is_distinguished(self, svc, brain):
        from api.server import execute_routine_job

        job = svc.create_job(
            JobType.SCHEDULED, "0 7 * * *", "no endpoint", {"skill": "messaging_sms"}, "",
        )
        execute_routine_job(job)

        status, _, error = _last_run(svc, job.id)
        assert status == "error"
        assert "without an endpoint" in error

    def test_the_failure_is_logged_at_warning(self, svc, brain, caplog):
        from api.server import execute_routine_job

        job = svc.create_job(
            JobType.SCHEDULED, "0 7 * * *", "morning", {"skill": "ghost", "endpoint": "x"}, "",
        )
        with caplog.at_level("WARNING"):
            execute_routine_job(job)

        assert any("ghost" in r.getMessage() for r in caplog.records), caplog.text
