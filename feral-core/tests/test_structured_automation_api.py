"""Actual registered routes and SQLite, without scheduler callbacks or lifespan."""
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from agents.scheduler import CronService
from api.routes import timeline


@pytest.fixture
def schedule_client(tmp_path, monkeypatch):
    scheduler = CronService(db_path=str(tmp_path / "scheduler.db"))
    monkeypatch.setattr(timeline, "state", SimpleNamespace(scheduler=scheduler))
    app = FastAPI()
    app.include_router(timeline.router)
    with TestClient(app) as client:
        yield client, scheduler
    assert scheduler._thread is None and scheduler._callback is None
    scheduler._conn.close()


@pytest.mark.parametrize("minutes,action", [
    (1, "Review every unread email"),
    (60, "Summarize my daily notes and weekly plan"),
    (10_080, "Read every 2 minutes in the quoted example, then explain it — مرحبا"),
])
def test_explicit_interval_cannot_be_reinterpreted_from_action(schedule_client, monkeypatch, minutes, action):
    client, scheduler = schedule_client

    def forbidden_parser(*args, **kwargs):
        raise AssertionError("Structured scheduling must not invoke natural-language parsing")

    monkeypatch.setattr(scheduler, "create_from_natural_language", forbidden_parser)
    response = client.post("/api/automations", json={"interval_minutes": minutes, "action": action, "session_id": "exact-A"})
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["success"] and receipt["creation_mode"] == "explicit_interval"
    assert receipt["cron"] == f"every {minutes}m"
    row = scheduler.get_job(receipt["job_id"])
    assert row is not None
    composed = f"every {minutes} minutes, {action}"
    assert row.description == receipt["description"] == composed
    assert row.session_id == "exact-A" and row.enabled and row.recurring
    assert row.cron_expr == f"every {minutes}m" and row.run_count == 0
    assert row.payload == {"source": "natural_language", "action_text": composed, "original_text": composed}
    assert [item["id"] for item in client.get("/api/automations").json()["automations"]] == [row.id]
    assert client.delete(f"/api/automations/{row.id}").json()["success"]
    assert scheduler.get_job(row.id) is None


@pytest.mark.parametrize("changes", [
    {"interval_minutes": True}, {"interval_minutes": 60.0}, {"interval_minutes": "60"},
    {"interval_minutes": 0}, {"interval_minutes": 10_081}, {"interval_minutes": None},
    {"action": ""}, {"action": "  "}, {"action": ["task"]}, {"action": "task\x00"},
    {"action": "é" * 4001}, {"action": "\ud800"},
    {"session_id": " A"}, {"session_id": "A "}, {"session_id": ""},
    {"session_id": "A\nB"}, {"session_id": "\ud800"}, {"session_id": "A" * 1025},
    {"text": "daily 9am"}, {"auto_confirm": True}, {"owner_id": "foreign"},
])
def test_invalid_or_ambiguous_structured_request_creates_no_schedule(schedule_client, changes):
    client, scheduler = schedule_client
    body = {"interval_minutes": 60, "action": "Read my notes", "session_id": "A", **changes}
    response = client.post("/api/automations", content=json.dumps(body), headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert scheduler.list_automations() == []


@pytest.mark.parametrize("body", [
    {"action": "Read notes", "session_id": "A"},
    {"interval_minutes": 60, "session_id": "A"},
    {"interval_minutes": 60, "action": "Read notes"},
])
def test_incomplete_structured_body_does_not_fall_back_to_legacy(schedule_client, body):
    client, scheduler = schedule_client
    assert client.post("/api/automations", json=body).status_code == 422
    assert scheduler.list_automations() == []


def test_legacy_natural_language_contract_is_retained(schedule_client):
    client, scheduler = schedule_client
    response = client.post("/api/automations", json={"text": "every 10 minutes, read notes", "session_id": "legacy"})
    assert response.status_code == 200 and response.json()["success"]
    row = scheduler.get_job(response.json()["job_id"])
    assert row is not None and row.session_id == "legacy" and row.cron_expr == "every 10m"
    assert "creation_mode" not in response.json()


def test_storage_failure_is_redacted_and_not_automatically_retried(schedule_client, monkeypatch, caplog):
    client, scheduler = schedule_client
    calls = []

    def failed_write(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("PRIVATE_EXCEPTION_SENTINEL")

    monkeypatch.setattr(scheduler, "create_job", failed_write)
    response = client.post("/api/automations", json={"interval_minutes": 60, "action": "PRIVATE_ACTION_SENTINEL", "session_id": "A"})
    assert response.status_code == 200 and response.json()["success"] is False
    assert len(calls) == 1 and scheduler.list_automations() == []
    assert "PRIVATE_EXCEPTION_SENTINEL" not in response.text + caplog.text
    assert "PRIVATE_ACTION_SENTINEL" not in response.text + caplog.text
