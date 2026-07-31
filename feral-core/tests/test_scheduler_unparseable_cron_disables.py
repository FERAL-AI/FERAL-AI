"""An unparseable schedule must disable the job, never silently re-arm.

`_compute_next_run` used to end in `return from_time + 60.0`, so any
expression the parser did not recognise became "every 60 seconds". Two
routines written as "nightly at 9pm" matched no pattern and fired 4,170 and
4,130 times, each driving a full multi-agent orchestrator turn at ~20k
prompt tokens, which pinned `chat` at $9.99 in a single hour against a $10
cap. "daily 21:00" parses correctly (19 runs over 19 days), so the parser
works — the catch-all was the bug.
"""

from __future__ import annotations

import logging
import time

import pytest

from agents.scheduler import (
    CronService,
    JobType,
    _compute_next_run,
)

# Asserted as ValueError, not by importing UnparseableCronExpression, so this
# module still COLLECTS against a pre-fix scheduler and the regression shows
# up as a real assertion failure rather than an ImportError at collection.
# UnparseableCronExpression subclasses ValueError precisely so callers that
# already guard against bad input keep working; test_refusal_type below pins
# the concrete class.


@pytest.fixture()
def svc(tmp_path):
    service = CronService(db_path=str(tmp_path / "sched.db"))
    yield service
    service.close()


# ── The parser refuses to guess ─────────────────────────────────


def test_the_incident_expression_is_refused():
    """"nightly at 9pm" is the expression that cost $9.99 in an hour."""
    with pytest.raises(ValueError):
        _compute_next_run("nightly at 9pm", time.time())


def test_refusal_names_the_supported_forms():
    with pytest.raises(ValueError) as exc:
        _compute_next_run("nightly at 9pm", time.time())
    message = str(exc.value)
    assert "nightly at 9pm" in message
    assert "daily HH:MM" in message
    assert "every Nm" in message


def test_refusal_type():
    """The concrete class, imported here rather than at module scope."""
    from agents.scheduler import UnparseableCronExpression

    assert issubclass(UnparseableCronExpression, ValueError)
    with pytest.raises(UnparseableCronExpression) as exc:
        _compute_next_run("nightly at 9pm", time.time())
    assert exc.value.cron_expr == "nightly at 9pm"


def test_equivalent_valid_expression_still_parses():
    """The fix must not break the schedule the operator meant to write."""
    now = time.time()
    nxt = _compute_next_run("daily 21:00", now)
    assert nxt > now
    assert nxt - now <= 24 * 3600


# ── Macros the catch-all was hiding ─────────────────────────────
#
# Removing the catch-all exposed that these NEVER parsed: `0 * * * *`
# (minute set, hour `*`) and anything with a specific day-of-month or
# day-of-week fell straight through to `from_time + 60`. So every
# @hourly/@weekly/@monthly/@yearly routine had been firing once a MINUTE.
# The old `test_hourly_macro_equals_cron` passed only because both sides
# returned the same wrong +60s.


@pytest.mark.parametrize(
    "expr",
    ["@hourly", "0 * * * *", "@daily", "@weekly", "0 0 * * 0", "@monthly", "@yearly"],
)
def test_macros_are_not_once_a_minute(expr):
    """The shared symptom: every one of these returned from_time + 60.

    Asserts the CADENCE (gap between two consecutive runs), not the gap
    from now. The gap from now is legitimately short — @hourly is one
    second away at hh:59:59 — so only the interval between successive
    firings distinguishes "hourly" from "every minute". The tests below
    pin where each one actually lands.
    """
    first = _compute_next_run(expr, time.time())
    second = _compute_next_run(expr, first)
    cadence = second - first
    assert cadence > 61, (
        f"{expr} fires every {cadence:.0f}s — the 60s catch-all is back"
    )


def test_hourly_lands_on_the_hour():
    from datetime import datetime, timezone as _tz

    now = time.time()
    nxt = datetime.fromtimestamp(_compute_next_run("@hourly", now), tz=_tz.utc)
    assert (nxt.minute, nxt.second) == (0, 0)


def test_weekly_lands_on_sunday_midnight():
    from datetime import datetime, timezone as _tz

    now = time.time()
    nxt = datetime.fromtimestamp(_compute_next_run("@weekly", now), tz=_tz.utc)
    assert nxt.isoweekday() == 7, nxt          # Sunday
    assert (nxt.hour, nxt.minute) == (0, 0)


def test_monthly_lands_on_the_first():
    from datetime import datetime, timezone as _tz

    now = time.time()
    nxt = datetime.fromtimestamp(_compute_next_run("@monthly", now), tz=_tz.utc)
    assert nxt.day == 1
    assert (nxt.hour, nxt.minute) == (0, 0)


def test_yearly_lands_on_january_first():
    from datetime import datetime, timezone as _tz

    now = time.time()
    nxt = datetime.fromtimestamp(_compute_next_run("@yearly", now), tz=_tz.utc)
    assert (nxt.month, nxt.day) == (1, 1)


def test_hourly_macro_equals_its_cron_form():
    """Equal because both are correct now, not because both are broken."""
    now = time.time()
    assert abs(_compute_next_run("@hourly", now) - _compute_next_run("0 * * * *", now)) < 1.0


# ── Creation-time rejection ─────────────────────────────────────


def test_create_job_rejects_unparseable_schedule(svc):
    """Reject when the routine is written, not when the bill arrives."""
    with pytest.raises(ValueError):
        svc.create_job(JobType.SCHEDULED, "nightly at 9pm", "bad", {}, "s1")
    assert svc.list_jobs() == []


def test_create_job_accepts_valid_schedule(svc):
    job = svc.create_job(JobType.SCHEDULED, "daily 21:00", "good", {}, "s1")
    assert job.id is not None
    assert job.next_run > time.time()


# ── Run-time: disable, never re-arm ─────────────────────────────


def _job_with_bad_cron(svc) -> int:
    """Create a valid job, then corrupt its cron the way a pre-fix DB row
    would already be corrupted (those rows exist in the maintainer's
    cost.db and must not keep firing after upgrade)."""
    job = svc.create_job(JobType.SCHEDULED, "daily 21:00", "legacy bad row", {}, "s1")
    with svc._lock:
        svc._conn.execute(
            "UPDATE scheduled_jobs SET cron_expr = ? WHERE id = ?",
            ("nightly at 9pm", job.id),
        )
        svc._conn.commit()
    return job.id


def test_mark_completed_disables_instead_of_rearming(svc):
    job_id = _job_with_bad_cron(svc)
    svc.mark_completed(job_id)

    updated = svc.get_job(job_id)
    assert updated is not None
    assert updated.enabled is False, "job re-armed instead of being disabled"


def test_disabled_job_is_not_returned_as_due(svc):
    """The runaway loop: due -> run -> re-arm 60s later -> due again."""
    job_id = _job_with_bad_cron(svc)
    svc.mark_completed(job_id)

    with svc._lock:
        svc._conn.execute(
            "UPDATE scheduled_jobs SET next_run = ? WHERE id = ?",
            (time.time() - 1, job_id),
        )
        svc._conn.commit()

    assert [j.id for j in svc.get_due_jobs()] == []


def test_disabling_logs_loudly_with_the_fix(svc, caplog):
    caplog.set_level(logging.CRITICAL, logger="feral.scheduler")
    job_id = _job_with_bad_cron(svc)
    svc.mark_completed(job_id)

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "feral.scheduler.disabled_unparseable_cron" in m for m in messages
    ), messages
    assert any("DISABLED" in m for m in messages), messages


def test_valid_recurring_job_still_reschedules(svc):
    """The fix must not disable jobs that parse fine."""
    job = svc.create_job(JobType.SCHEDULED, "every 30m", "fine", {}, "s1")
    svc.mark_completed(job.id)

    updated = svc.get_job(job.id)
    assert updated is not None
    assert updated.enabled is True
    assert updated.run_count == 1
    assert updated.next_run > time.time()


def test_resume_refuses_unparseable_job(svc):
    job_id = _job_with_bad_cron(svc)
    svc.pause_job(job_id)

    assert svc.resume_job(job_id) is False
    updated = svc.get_job(job_id)
    assert updated is not None
    assert updated.enabled is False


def test_resume_still_works_for_valid_job(svc):
    job = svc.create_job(JobType.SCHEDULED, "every 30m", "fine", {}, "s1")
    svc.pause_job(job.id)

    assert svc.resume_job(job.id) is True
    updated = svc.get_job(job.id)
    assert updated is not None
    assert updated.enabled is True
