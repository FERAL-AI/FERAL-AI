"""Actual SQLite occurrence fencing with inert callbacks and fault injection.

A callback return is not evidence of a purchase, message, or physical effect.
No OS kill or app/service launch is simulated by these tests.
"""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest

from agents.scheduler import CronService, JobType
from tests.test_runtime_routine_authorization import wired as _wired

wired = _wired


@pytest.fixture
def service(tmp_path):
    svc = CronService(db_path=str(tmp_path / "scheduler.db"))
    yield svc
    if svc._conn is not None:
        svc.close()


def due(svc, *, recurring=True):
    job = svc.create_job(JobType.SCHEDULED, "every 1m", "inert occurrence", {},
                         "fixture-owner", recurring=recurring)
    svc._conn.execute("UPDATE scheduled_jobs SET next_run = ? WHERE id = ?", (time.time()-1, job.id))
    svc._conn.commit()
    return svc.get_job(job.id)


def reopen(svc):
    path = svc._db_path
    svc.close()
    return CronService(db_path=path)


def test_returned_callback_bookkeeping_failure_reopens_without_replay(service):
    job = due(service)
    effects = []

    def callback(j):
        run = service.record_run_start(j.id)
        effects.append(j.id)
        service.record_run_finish(run, "success", {"inert": True})

    service._callback = callback
    with patch.object(service, "mark_completed", side_effect=RuntimeError("inert bookkeeping failure")):
        assert service._fire(job) is True
    occurrence = service.get_occurrences(job.id)[0]
    assert occurrence["status"] == "callback_returned" and occurrence["rearmed_at"] is None
    assert service.get_runs(job.id)[0]["status"] == "success"
    recovered = reopen(service)
    try:
        recovered._callback = lambda j: effects.append(j.id)
        recovered._catchup_missed_jobs()
        assert effects == [job.id]
        row = recovered.get_occurrences(job.id)[0]
        assert row["occurrence_id"] == occurrence["occurrence_id"]
        assert row["scheduled_for"] == job.next_run and row["rearmed_at"] is not None
        assert recovered.get_job(job.id).run_count == 1
        assert recovered.get_job(job.id).next_run > time.time()
        assert len(recovered.get_runs(job.id)) == 1
    finally:
        recovered.close()


def test_claim_committed_then_interruption_blocks_after_reopen(service):
    job = due(service)
    state, identity, _ = service._claim_occurrence(job)
    assert state == "claimed"
    recovered = reopen(service)
    effects = []
    try:
        recovered._callback = lambda j: effects.append(j.id)
        recovered._catchup_missed_jobs()
        recovered._tick()
        assert effects == []
        assert recovered.get_occurrences(job.id)[0]["occurrence_id"] == identity
        assert recovered.get_occurrences(job.id)[0]["status"] == "claimed"
        assert recovered.get_job(job.id).run_count == 0
    finally:
        recovered.close()


def test_process_loss_fixture_after_inert_effect_never_replays(service):
    job = due(service)
    effects = []

    def interrupted(j):
        effects.append(j.id)
        raise SystemExit("simulated method-boundary process loss; not an OS kill")

    service._callback = interrupted
    with pytest.raises(SystemExit):
        service._fire(job)
    assert service.get_occurrences(job.id)[0]["status"] == "claimed"
    recovered = reopen(service)
    try:
        recovered._callback = lambda j: effects.append(j.id)
        recovered._catchup_missed_jobs()
        assert effects == [job.id]
    finally:
        recovered.close()


def test_callback_exception_records_unknown_and_blocks_future_slot(service):
    job = due(service)
    effects = []

    def uncertain(j):
        effects.append(j.id)
        raise RuntimeError("inert uncertain effect")

    service._callback = uncertain
    assert service._fire(job) is False
    assert service.get_occurrences(job.id)[0]["status"] == "outcome_unknown"
    service._conn.execute("UPDATE scheduled_jobs SET next_run = ? WHERE id = ?", (time.time()-0.25, job.id))
    service._conn.commit()
    assert service._fire(service.get_job(job.id)) is False
    assert effects == [job.id] and len(service.get_occurrences(job.id)) == 1
    recovered = reopen(service)
    try:
        recovered._callback = lambda j: effects.append(j.id)
        recovered._catchup_missed_jobs()
        assert effects == [job.id]
    finally:
        recovered.close()


def test_normal_next_slot_runs_once_and_legacy_callback_argument_unchanged(service):
    job = due(service)
    received = []
    service._callback = received.append
    assert service._fire(job) is True
    first = service.get_occurrences(job.id)[0]
    assert received[0].id == job.id and received[0].session_id == "fixture-owner"
    following = service.get_job(job.id)
    with patch("agents.scheduler.time.time", return_value=following.next_run+1):
        assert service._fire(following) is True
    assert len(received) == 2
    rows = service.get_occurrences(job.id)
    assert len(rows) == 2 and all(row["status"] == "callback_returned" for row in rows)
    assert len({row["occurrence_id"] for row in rows}) == 2
    assert {row["scheduled_for"] for row in rows} == {job.next_run, following.next_run}
    assert first["rearmed_at"] is not None
    assert service.get_job(job.id).run_count == 2


def test_claim_insert_failure_dispatches_zero_and_rolls_back(service):
    job = due(service)
    service._conn.execute("CREATE TRIGGER abort_claim BEFORE INSERT ON routine_occurrences BEGIN SELECT RAISE(ABORT, 'inert'); END")
    service._conn.commit()
    effects = []
    service._callback = lambda j: effects.append(j.id)
    assert service._fire(job) is False
    assert effects == [] and service.get_occurrences(job.id) == []
    assert service.get_job(job.id).next_run == job.next_run
    assert not service._conn.in_transaction


def test_bookkeeping_and_occurrence_completion_marker_are_atomic(service):
    job = due(service)
    effects = []
    service._callback = lambda j: effects.append(j.id)
    service._conn.execute("CREATE TRIGGER abort_rearmed BEFORE UPDATE OF rearmed_at ON routine_occurrences BEGIN SELECT RAISE(ABORT, 'inert'); END")
    service._conn.commit()
    assert service._fire(job) is True
    assert effects == [job.id]
    assert service.get_job(job.id).run_count == 0
    assert service.get_job(job.id).next_run == job.next_run
    assert service.get_occurrences(job.id)[0]["rearmed_at"] is None
    service._conn.execute("DROP TRIGGER abort_rearmed")
    service._conn.commit()
    recovered = reopen(service)
    try:
        recovered._callback = lambda j: effects.append(j.id)
        recovered._catchup_missed_jobs()
        assert effects == [job.id] and recovered.get_job(job.id).run_count == 1
        row = recovered.get_occurrences(job.id)[0]
        recovered.mark_completed(job.id, occurrence_id=row["occurrence_id"])
        assert recovered.get_job(job.id).run_count == 1
    finally:
        recovered.close()


def test_finish_persistence_failure_retains_claim_instead_of_replaying(service):
    job = due(service)
    effects = []
    service._callback = lambda j: effects.append(j.id)
    service._conn.execute("CREATE TRIGGER abort_finish BEFORE UPDATE OF status ON routine_occurrences BEGIN SELECT RAISE(ABORT, 'inert'); END")
    service._conn.commit()
    assert service._fire(job) is False
    assert service.get_occurrences(job.id)[0]["status"] == "claimed"
    assert service._fire(job) is False and effects == [job.id]
    assert service.get_job(job.id).run_count == 0


def test_changed_schedule_is_not_overwritten_by_old_completion(service):
    job = due(service)
    replacement = time.time()+300

    def changed(j):
        service._conn.execute("UPDATE scheduled_jobs SET next_run = ? WHERE id = ?", (replacement, j.id))
        service._conn.commit()

    service._callback = changed
    assert service._fire(job) is True
    assert service.get_job(job.id).next_run == replacement
    assert service.get_job(job.id).run_count == 0
    assert service.get_occurrences(job.id)[0]["rearmed_at"] is not None


def test_stale_disabled_and_deleted_snapshots_do_not_dispatch(service):
    job = due(service)
    service._callback = lambda _: pytest.fail("stale job dispatched")
    service.pause_job(job.id)
    assert service._fire(job) is False
    service.delete_job(job.id)
    assert service._fire(job) is False
    assert service.get_occurrences(job.id) == []


def test_one_shot_recovery_disables_without_second_callback(service):
    job = due(service, recurring=False)
    effects = []
    service._callback = lambda j: effects.append(j.id)
    with patch.object(service, "mark_completed", side_effect=RuntimeError("inert")):
        assert service._fire(job)
    recovered = reopen(service)
    try:
        recovered._callback = lambda j: effects.append(j.id)
        recovered._catchup_missed_jobs()
        assert effects == [job.id] and recovered.get_job(job.id).enabled is False
        assert recovered.get_job(job.id).run_count == 1
    finally:
        recovered.close()


def test_competing_sqlite_claimants_do_not_dispatch_twice(service):
    job = due(service)
    other = CronService(db_path=service._db_path)
    entered, release = Event(), Event()
    effects = []

    def first(j):
        effects.append(j.id)
        entered.set()
        assert release.wait(2)

    service._callback = first
    other._callback = lambda j: effects.append(j.id)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(service._fire, job)
            assert entered.wait(1)
            assert other._fire(job) is False
            release.set()
            assert task.result(timeout=2) is True
        assert effects == [job.id]
        assert len(service.get_occurrences(job.id)) == 1
    finally:
        release.set()
        other.close()


def test_blocked_polling_logs_once_without_new_rows(service, caplog):
    job = due(service)
    service._claim_occurrence(job)
    service._callback = lambda _: pytest.fail("claimed slot replayed")
    for _ in range(10):
        service._tick()
    logs = [row for row in caplog.records if "unfinished occurrence requires reconciliation" in row.getMessage()]
    assert len(logs) == 1 and len(service.get_occurrences(job.id)) == 1


def test_legacy_direct_mark_completed_does_not_invent_callback_evidence(service):
    job = due(service)
    service.mark_completed(job.id)
    assert service.get_job(job.id).run_count == 1
    assert service.get_occurrences(job.id) == []


def test_unique_due_slot_cannot_be_inserted_twice(service):
    job = due(service)
    service._claim_occurrence(job)
    original = service.get_occurrences(job.id)[0]
    with pytest.raises(sqlite3.IntegrityError):
        service._conn.execute(
            "INSERT INTO routine_occurrences (occurrence_id, job_id, scheduled_for, input_sha256, claimed_at, status) VALUES ('other', ?, ?, 'inert', ?, 'claimed')",
            (job.id, original["scheduled_for"], time.time()),
        )
    service._conn.rollback()
    assert len(service.get_occurrences(job.id)) == 1


def test_old_claim_does_not_get_moved_by_catchup_grace(service):
    job = due(service)
    service._conn.execute("UPDATE scheduled_jobs SET next_run = ? WHERE id = ?", (time.time()-172800, job.id))
    service._conn.commit()
    job = service.get_job(job.id)
    service._claim_occurrence(job)
    service._callback = lambda _: pytest.fail("old claim replayed")
    service._catchup_missed_jobs()
    assert service.get_job(job.id).next_run == job.next_run
    assert service.get_job(job.id).run_count == 0


def test_old_returned_callback_recovers_bookkeeping_without_late_dispatch(service):
    job = due(service)
    effects = []
    service._callback = lambda j: effects.append(j.id)
    with patch.object(service, "mark_completed", side_effect=RuntimeError("inert")):
        service._fire(job)
    with patch("agents.scheduler.time.time", return_value=time.time()+172800):
        service._catchup_missed_jobs()
    assert effects == [job.id] and service.get_job(job.id).run_count == 1
    assert service.get_occurrences(job.id)[0]["rearmed_at"] is not None


@pytest.mark.parametrize("cron", ["every 1m", "daily 09:00"])
@pytest.mark.parametrize("status", ["claimed", "outcome_unknown"])
def test_tick_keeps_aged_unfinished_occurrence_identity(service, cron, status):
    job = due(service)
    service._conn.execute("UPDATE scheduled_jobs SET cron_expr = ? WHERE id = ?", (cron, job.id))
    service._conn.commit()
    job = service.get_job(job.id)
    _, identity, _ = service._claim_occurrence(job)
    if status == "outcome_unknown":
        service._finish_occurrence(identity, status)
    service._callback = lambda _: pytest.fail("unfinished occurrence replayed")
    with patch("agents.scheduler.time.time", return_value=time.time()+172800):
        service._tick()
    current = service.get_job(job.id)
    assert current.next_run == job.next_run and current.run_count == 0
    occurrence = service.get_occurrences(job.id)[0]
    assert occurrence["occurrence_id"] == identity and occurrence["status"] == status
    assert occurrence["rearmed_at"] is None


@pytest.mark.parametrize("cron", ["every 1m", "daily 09:00"])
def test_tick_recovers_aged_returned_callback_bookkeeping(service, cron):
    job = due(service)
    service._conn.execute("UPDATE scheduled_jobs SET cron_expr = ? WHERE id = ?", (cron, job.id))
    service._conn.commit()
    job = service.get_job(job.id)
    effects = []
    service._callback = lambda j: effects.append(j.id)
    with patch.object(service, "mark_completed", side_effect=RuntimeError("inert")):
        service._fire(job)
    identity = service.get_occurrences(job.id)[0]["occurrence_id"]
    with patch("agents.scheduler.time.time", return_value=time.time()+172800):
        service._tick()
    assert effects == [job.id] and service.get_job(job.id).run_count == 1
    occurrence = service.get_occurrences(job.id)[0]
    assert occurrence["occurrence_id"] == identity and occurrence["rearmed_at"] is not None


def test_future_slot_is_not_dispatched_early(service):
    job = service.create_job(JobType.SCHEDULED, "every 1m", "future", {}, "fixture-owner")
    service._callback = lambda _: pytest.fail("future slot dispatched")
    assert service._fire(job) is False
    assert service.get_occurrences(job.id) == []


def test_actual_routine_callback_and_central_dispatch_survive_bookkeeping_reopen(wired, monkeypatch):
    import api.server as server
    svc = wired.cron
    job = svc.create_job(JobType.SCHEDULED, "every 1m", "actual inert route",
                         {"skill": "notes_memory", "endpoint": "search_notes",
                          "args": {"value": "exact fixture"}}, "routine-owner")
    svc._conn.execute("UPDATE scheduled_jobs SET next_run = ? WHERE id = ?", (time.time()-1, job.id))
    svc._conn.commit()
    job = svc.get_job(job.id)
    svc._callback = server.execute_routine_job
    with patch.object(svc, "mark_completed", side_effect=RuntimeError("inert")):
        assert svc._fire(job) is True
    assert len(wired.seen) == 1 and svc.get_runs(job.id)[0]["status"] == "success"
    recovered = reopen(svc)
    try:
        monkeypatch.setattr(server.state, "cron_service", recovered)
        recovered._callback = server.execute_routine_job
        recovered._catchup_missed_jobs()
        assert len(wired.seen) == 1 and len(recovered.get_runs(job.id)) == 1
        assert recovered.get_job(job.id).run_count == 1
    finally:
        recovered.close()


def test_additive_schema_keeps_legacy_rows_without_inventing_old_effect_receipts(service):
    job = due(service)
    run = service.record_run_start(job.id)
    service.record_run_finish(run, "success", {"old_fixture": True})
    service._conn.execute("DROP TABLE routine_occurrences")
    service._conn.commit()
    recovered = reopen(service)
    try:
        assert recovered.get_job(job.id).id == job.id
        assert recovered.get_runs(job.id)[0]["status"] == "success"
        assert recovered.get_occurrences(job.id) == []
    finally:
        recovered.close()
