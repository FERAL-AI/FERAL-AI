"""Scheduler fuzz tests — cron parsing, concurrent writer rejection, missed-job catch-up."""
import os
import time
import threading
from unittest.mock import MagicMock

import pytest

from agents.scheduler import (
    CronService,
    JobType,
    FileLock,
    UnparseableCronExpression,
    _compute_next_run,
)


# ─── Cron string fuzzing ────────────────────────────────────────


_VALID_CRONS = [
    "every 1m",
    "every 5m",
    "every 30m",
    "every 60m",
    "every 1h",
    "every 12h",
    "daily 00:00",
    "daily 09:30",
    "daily 23:59",
    "*/5 * * * *",
    "*/15 * * * *",
    "0 */2 * * *",
    "0 */6 * * *",
    "30 8 * * *",
    "0 12 * * *",
    "@hourly",
    "@daily",
    "@weekly",
    "@monthly",
    "@yearly",
]

_INVALID_CRONS = [
    "",
    "   ",
    "not a cron",
    "every",
    "every xm",
    "* * *",
    "60 25 * * *",
    "abc def ghi jkl mno",
]


class TestCronFuzz:
    @pytest.mark.parametrize("cron", _VALID_CRONS)
    def test_valid_cron_parses(self, cron):
        now = time.time()
        nxt = _compute_next_run(cron, now)
        assert nxt > now, f"Expected next_run > now for cron={cron!r}"

    @pytest.mark.parametrize("cron", _INVALID_CRONS)
    def test_invalid_cron_is_refused(self, cron):
        """Unparseable crons must raise, never silently re-arm.

        This test used to assert the opposite — that a bad expression
        "falls back" to now + 60s. That fallback is what made two routines
        written as "nightly at 9pm" fire 4,170 and 4,130 times, each a full
        orchestrator turn, pinning chat spend at the $10 cap in an hour.
        """
        now = time.time()
        with pytest.raises(UnparseableCronExpression):
            _compute_next_run(cron, now)

    def test_cron_macros_resolve(self):
        now = time.time()
        for macro in ("@yearly", "@monthly", "@weekly", "@daily", "@hourly"):
            nxt = _compute_next_run(macro, now)
            assert nxt > now, f"Macro {macro} should schedule in the future"

    def test_daily_macro_equals_midnight(self):
        now = time.time()
        daily = _compute_next_run("@daily", now)
        midnight = _compute_next_run("0 0 * * *", now)
        assert abs(daily - midnight) < 1.0

    def test_hourly_macro_equals_cron(self):
        now = time.time()
        hourly = _compute_next_run("@hourly", now)
        cron = _compute_next_run("0 * * * *", now)
        assert abs(hourly - cron) < 1.0


# ─── File lock & concurrent writer rejection ────────────────────


class TestFileLock:
    def test_acquire_and_release(self, tmp_path):
        lock_path = str(tmp_path / "test.lock")
        lock = FileLock(lock_path)
        assert lock.acquire() is True
        lock.release()

    def test_concurrent_lock_rejected(self, tmp_path):
        lock_path = str(tmp_path / "concurrent.lock")
        lock1 = FileLock(lock_path)
        lock2 = FileLock(lock_path)

        assert lock1.acquire() is True
        assert lock2.acquire() is False

        lock1.release()
        assert lock2.acquire() is True
        lock2.release()

    def test_concurrent_writers_thread_safety(self, tmp_path):
        """Two threads try to acquire the same file lock; only one succeeds at a time."""
        lock_path = str(tmp_path / "thread.lock")
        results = {"t1": None, "t2": None}

        def try_lock(name):
            fl = FileLock(lock_path)
            results[name] = fl.acquire()
            time.sleep(0.2)
            fl.release()

        t1 = threading.Thread(target=try_lock, args=("t1",))
        t2 = threading.Thread(target=try_lock, args=("t2",))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join()
        t2.join()

        assert results["t1"] is True
        assert results["t2"] is False

    def test_context_manager(self, tmp_path):
        lock_path = str(tmp_path / "ctx.lock")
        with FileLock(lock_path):
            lock2 = FileLock(lock_path)
            assert lock2.acquire() is False

    def test_context_manager_raises_on_failure(self, tmp_path):
        lock_path = str(tmp_path / "fail.lock")
        lock1 = FileLock(lock_path)
        lock1.acquire()
        with pytest.raises(RuntimeError):
            with FileLock(lock_path):
                pass
        lock1.release()


# ─── Missed-job catch-up ────────────────────────────────────────


class TestMissedJobCatchup:
    @pytest.fixture()
    def svc(self, tmp_path):
        db = str(tmp_path / "test_catchup.db")
        return CronService(db_path=db)

    def test_catchup_fires_missed_jobs(self, svc):
        """Jobs missed within the last day should be caught up."""
        fired = []
        svc._callback = lambda job: fired.append(job.id)

        job = svc.create_job(JobType.SCHEDULED, "every 5m", "missed test", {}, "s1")

        now = time.time()
        with svc._lock:
            svc._conn.execute(
                "UPDATE scheduled_jobs SET next_run = ? WHERE id = ?",
                (now - 3600, job.id),
            )
            svc._conn.commit()

        svc._catchup_missed_jobs()
        assert job.id in fired

    def test_catchup_skips_old_jobs(self, svc):
        """Jobs missed more than 1 day ago should NOT be caught up."""
        fired = []
        svc._callback = lambda job: fired.append(job.id)

        job = svc.create_job(JobType.SCHEDULED, "every 5m", "old miss", {}, "s1")

        now = time.time()
        two_days_ago = now - 2 * 86400
        with svc._lock:
            svc._conn.execute(
                "UPDATE scheduled_jobs SET next_run = ? WHERE id = ?",
                (two_days_ago, job.id),
            )
            svc._conn.commit()

        svc._catchup_missed_jobs()
        assert job.id not in fired

    def test_catchup_marks_completed(self, svc):
        """After catch-up, the job's next_run should be rescheduled."""
        svc._callback = lambda job: None

        job = svc.create_job(JobType.SCHEDULED, "every 10m", "reschedule", {}, "s1")

        now = time.time()
        with svc._lock:
            svc._conn.execute(
                "UPDATE scheduled_jobs SET next_run = ? WHERE id = ?",
                (now - 600, job.id),
            )
            svc._conn.commit()

        svc._catchup_missed_jobs()

        updated = svc.get_job(job.id)
        assert updated is not None
        assert updated.next_run > now
        assert updated.run_count == 1


# ─── CronService with file lock integration ─────────────────────


class TestCronServiceFileLock:
    def test_service_has_file_lock(self, tmp_path):
        db = str(tmp_path / "locked.db")
        svc = CronService(db_path=db)
        assert svc._file_lock is not None
        assert svc._file_lock._lock_path == db + ".lock"
        svc.close()

    def test_service_operations_with_lock(self, tmp_path):
        db = str(tmp_path / "ops.db")
        svc = CronService(db_path=db)
        job = svc.create_job(JobType.CUSTOM, "every 1m", "locked op", {}, "s1")
        assert job.id is not None
        jobs = svc.list_jobs()
        assert len(jobs) >= 1
        svc.close()
