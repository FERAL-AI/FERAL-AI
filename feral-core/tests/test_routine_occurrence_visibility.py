"""Actual isolated scheduler evidence exposed without dispatch or replay."""
import json
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.scheduler import CronService, JobType


@pytest.fixture
def service(tmp_path):
    scheduler = CronService(db_path=str(tmp_path / "routine.db"))
    yield scheduler
    scheduler.close()


def due(service):
    job = service.create_job(
        JobType.SCHEDULED, "every 1m", "inert fixture", {"private": "fixture-private"}, "owner",
    )
    service._conn.execute("UPDATE scheduled_jobs SET next_run=? WHERE id=?", (time.time()-1, job.id))
    service._conn.commit()
    return service.get_job(job.id)


def client(service, monkeypatch):
    from api.routes import routines, jobs
    mock = SimpleNamespace(scheduler=service, cron_service=service)
    monkeypatch.setattr(routines, "state", mock)
    monkeypatch.setattr(jobs, "state", mock)
    app = FastAPI()
    app.include_router(routines.router)
    app.include_router(jobs.router)
    return TestClient(app)


def test_detail_exposes_reopened_claim_without_starting_scheduler(service, monkeypatch):
    job = due(service)
    _, identity, _ = service._claim_occurrence(job)
    path = service._db_path
    service.close()
    reopened = CronService(db_path=path)
    try:
        monkeypatch.setattr(reopened, "ensure_running", lambda: pytest.fail("inspection started scheduler"))
        with client(reopened, monkeypatch) as api:
            body = api.get(f"/api/routines/{job.id}").json()
        state = body["dispatch"]
        assert state["tracking_available"] and state["reconciliation_required"]
        assert state["dispatch_state"] == "reconciliation_required"
        assert state["occurrence"]["occurrence_id"] == identity
        assert body["runs"] == []
        assert "fixture-private" not in json.dumps(state)
        assert "input_sha256" not in state["occurrence"]
        assert reopened.get_job(job.id).run_count == 0
    finally:
        reopened.close()


def test_old_unresolved_occurrence_cannot_hide_behind_recent_history(service):
    job = due(service)
    _, identity, _ = service._claim_occurrence(job)
    for i in range(201):
        service._conn.execute(
            "INSERT INTO routine_occurrences VALUES(?,1,?,?,?,?,'callback_returned',?,?)",
            (f"history-{i}", job.id, job.next_run-i-1, "fixture-digest", time.time()+i+1, time.time(), time.time()),
        )
    service._conn.commit()
    assert all(row["occurrence_id"] != identity for row in service.get_occurrences(job.id, limit=200))
    assert service.get_dispatch_state(job.id)["occurrence"]["occurrence_id"] == identity


def test_polling_a_recovered_claim_does_not_report_the_old_action_as_running(service):
    job = due(service)
    service._claim_occurrence(job)
    service._running_jobs.add(job.id)
    assert service.get_dispatch_state(job.id)["dispatch_state"] == "reconciliation_required"


def test_inflight_callback_is_not_a_verified_outcome(service):
    job = due(service)
    entered, release = Event(), Event()
    service._callback = lambda _: (entered.set(), release.wait(5))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service._fire, job)
        try:
            assert entered.wait(2)
            state = service.get_dispatch_state(job.id)
            assert state["dispatch_state"] == "in_progress"
            assert state["occurrence"]["status"] == "claimed"
            assert state["reconciliation_required"] is False
        finally:
            release.set()
        assert future.result(timeout=3)
    assert service.get_dispatch_state(job.id)["dispatch_state"] == "scheduled"


def test_bookkeeping_pending_does_not_claim_external_completion(service):
    job = due(service)
    service._callback = lambda _: None
    with patch.object(service, "mark_completed", side_effect=RuntimeError("fixture bookkeeping")):
        service._fire(job)
    state = service.get_dispatch_state(job.id)
    assert state["dispatch_state"] == "bookkeeping_pending"
    assert state["occurrence"]["status"] == "callback_returned"
    assert "verified" not in state
    service._conn.execute("UPDATE scheduled_jobs SET next_run=? WHERE id=?", (time.time()+300, job.id))
    service._conn.commit()
    assert service.get_dispatch_state(job.id)["dispatch_state"] == "scheduled"


@pytest.mark.parametrize("enabled", [True, False])
def test_jobs_keeps_unresolved_occurrence_visible_even_when_disabled(service, monkeypatch, enabled):
    job = due(service)
    service._claim_occurrence(job)
    service._conn.execute("UPDATE scheduled_jobs SET enabled=?, next_run=? WHERE id=?", (int(enabled), time.time()+7200, job.id))
    service._conn.commit()
    with client(service, monkeypatch) as api:
        body = api.get("/api/jobs?kind=routine").json()
    item = body["items"][0]
    assert item["status"] == "reconciliation_required"
    assert item["detail"]["enabled"] is enabled
    assert item["cancellable_via"] is None
    assert item["progress"] is None


def test_tracking_failure_is_degraded_not_an_idle_or_scheduled_system(service, monkeypatch):
    due(service)
    monkeypatch.setattr(service, "get_dispatch_state", lambda _: (_ for _ in ()).throw(RuntimeError("fixture tracking unavailable")))
    with client(service, monkeypatch) as api:
        body = api.get("/api/jobs?kind=routine").json()
    assert "routine" in body["degraded"]
    assert body["items"] == []


def test_legacy_detail_has_truthful_unavailable_tracking(service, monkeypatch):
    job = due(service)
    monkeypatch.setattr(service, "get_dispatch_state", None)
    with client(service, monkeypatch) as api:
        body = api.get(f"/api/routines/{job.id}").json()
    assert body["dispatch"] == {
        "tracking_available": False, "dispatch_state": "unavailable",
        "reconciliation_required": None, "occurrence": None,
    }


@pytest.mark.parametrize("status", ["claimed", "outcome_unknown", "callback_returned"])
def test_deletion_cannot_hide_unfinished_occurrence(service, monkeypatch, status):
    job = due(service)
    service._claim_occurrence(job)
    service._conn.execute("UPDATE routine_occurrences SET status=? WHERE job_id=?", (status, job.id))
    service._conn.commit()
    assert service.delete_job(job.id) is False
    with client(service, monkeypatch) as api:
        response = api.delete(f"/api/routines/{job.id}")
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "routine_action_pending"
        expected = "bookkeeping_pending" if status == "callback_returned" else "reconciliation_required"
        assert api.get(f"/api/routines/{job.id}").json()["dispatch"]["dispatch_state"] == expected
        assert api.get("/api/jobs?kind=routine").json()["items"][0]["status"] == expected


def test_skill_deletion_conflict_is_not_reported_as_missing(service):
    from skills.impl.feral_routines import FeralRoutinesSkill
    job = due(service)
    service._claim_occurrence(job)
    result = FeralRoutinesSkill()._mutate(service, {"routine_id": job.id}, "delete")
    assert result["success"] is False and result["status_code"] == 409
    assert result["reason"] == "routine_action_pending"
