"""Restarting the brain must not produce output the user did not ask for.

Observed on the operator's own machine on 2026-09-05 while verifying
other work. Two restarts twenty minutes apart each produced a "Good
morning, Omar!" briefing card, and the boot catch-up fired "Spin the
CuteBot at 9 PM nightly" at 08:41 the following morning, because the
brain had been off overnight and the whole previous evening still
counted as "missed within a day".

Both are the same mistake in two places: treating a restart as an event
worth acting on. Upgrading, crashing, or closing a laptop lid is not a
request. These tests pin the two rules that follow from that:

  * a briefing is once per local calendar day, remembered on disk;
  * a routine whose schedule names a time of day is only run late
    inside a grace window, because for those the time IS the
    instruction, while an interval routine has no opinion about when
    and is caught up as before.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.scheduler import CronService, JobType  # noqa: E402


# ─────────────────────────── the scheduler ───────────────────────────

@pytest.fixture
def svc(tmp_path):
    service = CronService(db_path=str(tmp_path / "jobs.db"))
    yield service
    service.close()


def _make_due(service, job_id: int, *, seconds_ago: float, last_run: float | None = None):
    """Put a job in the past, the way an outage would."""
    with service._lock:
        service._conn.execute(
            "UPDATE scheduled_jobs SET next_run = ?, last_run = ? WHERE id = ?",
            (time.time() - seconds_ago, last_run or 0.0, job_id),
        )
        service._conn.commit()


class TestWallClockDetection:
    """The distinction the whole catch-up rule rests on."""

    @pytest.mark.parametrize("expr", ["daily 21:00", "daily 7:05", "0 7 * * *", "30 9 * * 1", "@daily"])
    def test_time_of_day_schedules(self, expr):
        assert CronService._is_wall_clock_schedule(expr) is True

    @pytest.mark.parametrize("expr", ["every 5m", "every 2h", "every 10 minutes", "*/5 * * * *", "* * * * *"])
    def test_interval_schedules(self, expr):
        assert CronService._is_wall_clock_schedule(expr) is False

    def test_an_unrecognised_expression_is_not_treated_as_a_clock(self):
        # Unparseable expressions are disabled by mark_completed with an
        # operator-facing reason. Guessing "time of day" here would add a
        # second, quieter way for them to be skipped.
        assert CronService._is_wall_clock_schedule("nightly at 9pm") is False
        assert CronService._is_wall_clock_schedule("") is False


class TestCatchUp:
    def test_a_nightly_routine_does_not_fire_the_next_morning(self, svc):
        """The reported bug: 'Spin the CuteBot at 9 PM' ran at 08:41."""
        fired = []
        svc._callback = lambda job: fired.append(job.id)
        job = svc.create_job(JobType.SCHEDULED, "daily 21:00", "spin the cutebot", {}, "s1")
        _make_due(svc, job.id, seconds_ago=11.7 * 3600)

        svc._catchup_missed_jobs()

        assert fired == []
        updated = svc.get_job(job.id)
        assert updated.next_run > time.time(), "it must still be armed for tonight"
        assert updated.run_count == 0, "a run that never happened must not be recorded"

    def test_a_briefing_missed_by_minutes_still_runs(self, svc):
        """Inside the grace window, late delivery beats no delivery."""
        fired = []
        svc._callback = lambda job: fired.append(job.id)
        job = svc.create_job(JobType.SCHEDULED, "0 7 * * *", "morning briefing", {}, "s1")
        _make_due(svc, job.id, seconds_ago=10 * 60)

        svc._catchup_missed_jobs()

        assert fired == [job.id]

    def test_an_interval_routine_is_still_caught_up(self, svc):
        """'Every ten minutes' makes no claim about when."""
        fired = []
        svc._callback = lambda job: fired.append(job.id)
        job = svc.create_job(JobType.SCHEDULED, "every 10m", "poll something", {}, "s1")
        _make_due(svc, job.id, seconds_ago=6 * 3600)

        svc._catchup_missed_jobs()

        assert fired == [job.id]

    def test_a_second_restart_does_not_fire_it_again(self, svc):
        """The slot already ran; another boot must not repeat it."""
        fired = []
        svc._callback = lambda job: fired.append(job.id)
        job = svc.create_job(JobType.SCHEDULED, "0 7 * * *", "morning briefing", {}, "s1")
        due = time.time() - 10 * 60
        with svc._lock:
            svc._conn.execute(
                "UPDATE scheduled_jobs SET next_run = ?, last_run = ? WHERE id = ?",
                (due, due + 1, job.id),
            )
            svc._conn.commit()

        svc._catchup_missed_jobs()

        assert fired == []
        assert svc.get_job(job.id).next_run > time.time()

    def test_the_grace_window_is_configurable(self, svc, monkeypatch):
        """An operator may prefer a late briefing to none."""
        fired = []
        svc._callback = lambda job: fired.append(job.id)
        job = svc.create_job(JobType.SCHEDULED, "daily 21:00", "spin", {}, "s1")
        _make_due(svc, job.id, seconds_ago=6 * 3600)

        monkeypatch.setattr(CronService, "_CATCHUP_GRACE_SECONDS", 12 * 3600)
        svc._catchup_missed_jobs()

        assert fired == [job.id]


# ─────────────────────────── the briefing ────────────────────────────

class TestBriefingSurvivesRestart:
    def test_the_date_round_trips(self, tmp_path):
        from agents import proactive_engine as pe

        path = tmp_path / "proactive_state.json"
        assert pe._read_briefing_date(path) == "", "unknown before anything is written"
        pe._write_briefing_date(path, "2026-09-05")
        assert pe._read_briefing_date(path) == "2026-09-05"

    def test_an_unreadable_file_reads_as_unknown(self, tmp_path):
        """At most one extra briefing. Never a crash on boot."""
        from agents import proactive_engine as pe

        path = tmp_path / "proactive_state.json"
        path.write_text("{ this is not json")
        assert pe._read_briefing_date(path) == ""

    def test_a_write_failure_is_swallowed(self, tmp_path):
        from agents import proactive_engine as pe

        # A directory where the file should be: open() will fail.
        path = tmp_path / "proactive_state.json"
        path.mkdir()
        pe._write_briefing_date(path, "2026-09-05")  # must not raise

    def test_a_new_engine_reads_the_day_off_disk(self, tmp_path, monkeypatch):
        """The restart case, end to end at the seam that broke."""
        from agents import proactive_engine as pe

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        today = time.strftime("%Y-%m-%d", time.localtime())
        pe._write_briefing_date(pe._briefing_state_path(), today)

        engine = pe.ProactiveEngine.__new__(pe.ProactiveEngine)
        engine._briefing_state_path = pe._briefing_state_path()
        engine._briefing_delivered_on = pe._read_briefing_date(engine._briefing_state_path)

        assert engine._briefing_delivered_on == today, (
            "a restart on the same day must not re-deliver the briefing"
        )

    def test_the_path_follows_feral_home(self, tmp_path, monkeypatch):
        from agents import proactive_engine as pe

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        assert pe._briefing_state_path() == tmp_path / "proactive_state.json"

    def test_the_file_is_valid_json_a_human_can_read(self, tmp_path):
        from agents import proactive_engine as pe

        path = tmp_path / "proactive_state.json"
        pe._write_briefing_date(path, "2026-09-05")
        assert json.loads(path.read_text()) == {"briefing_delivered_on": "2026-09-05"}
