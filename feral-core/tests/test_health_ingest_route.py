"""Operator report 2026-06-07 — iOS HealthKitAdapter spammed
``healthkit ingest failed: ingest failed HTTP 401`` warnings on
every poll cycle. The iOS SDK's ``BrainHTTP.IngestKind.healthKit``
POSTs to ``/api/health/ingest`` but the brain never declared the
route AND the path was missing from the phone-bearer POST allowlist,
so every request was rejected by ``APIKeyMiddleware`` with 401 before
the FastAPI router even saw it.

Fix in this PR:
* ``api/routes/dashboard.py`` registers
  ``POST /api/health/ingest`` to bridge HealthKit samples into the
  brain's memory store via ``state.memory.save``.
* ``api/server.py`` adds ``/api/health/ingest`` to
  ``_PHONE_BEARER_POST`` so the iOS phone bearer is accepted.

These tests pin:

* The path is on ``_PHONE_BEARER_POST`` so future drift is caught
  at boot by the allowlist coherence invariant.
* The route persists samples via ``memory.save`` and returns
  ``{"persisted": N, "skipped": M}`` so the iOS Devices tab can
  render "Synced N / M samples" without a follow-up query.
* Bad bodies (non-JSON, missing ``samples``, non-list ``samples``)
  return 400 — the route is permissive about per-sample shape but
  strict about the envelope so a typo in a future iOS build can't
  silently push a malformed batch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def app_with_route(monkeypatch):
    """Build a minimal FastAPI app with the dashboard router mounted
    plus a stub ``state.memory.save`` so the route's memory write can
    be verified without booting the full brain."""
    fake_memory = MagicMock()
    fake_memory.save = AsyncMock(return_value=None)

    mock_state = MagicMock()
    mock_state.memory = fake_memory

    # ``api.routes.dashboard`` does ``from api.state import state``
    # at import time, which binds the dashboard module's ``state``
    # name to the singleton at that moment. Patching ``api.state.state``
    # alone wouldn't redirect lookups inside the route — the dashboard
    # module already holds its own reference. Patch both so the route
    # reads our mock at call time.
    monkeypatch.setattr("api.state.state", mock_state)
    from api.routes import dashboard as dashboard_module
    monkeypatch.setattr(dashboard_module, "state", mock_state)

    app = FastAPI()
    app.include_router(dashboard_module.router)
    return app, fake_memory


def _client(app):
    return TestClient(app, raise_server_exceptions=False)


def test_health_ingest_path_is_on_phone_bearer_post_allowlist():
    """v2026.6.7 fix: the ingest path is allowlisted so iOS phone
    bearers are accepted. Without this entry the middleware rejects
    every request with 401 before the route can run — exactly the
    symptom in the operator's iOS log."""
    from api.server import _PHONE_BEARER_POST

    assert _PHONE_BEARER_POST.matches("/api/health/ingest"), (
        "/api/health/ingest must be in _PHONE_BEARER_POST so iOS "
        "HealthKit ingest stops 401-spamming the brain"
    )


def test_health_ingest_persists_samples_to_memory(app_with_route):
    """Each sample becomes a memory record so the memory tool surface
    can answer "what was my heart rate this morning?" from history."""
    app, fake_memory = app_with_route
    body = {
        "source": "ios.healthkit",
        "ingested_at": "2026-06-07T15:00:00Z",
        "samples": [
            {
                "event_type": "heart_rate",
                "bpm": 72,
                "source": "apple_healthkit",
                "pipeline": "Apple Health",
                "sample_source": "Apple Watch",
            },
            {
                "event_type": "spo2",
                "current": 98,
                "source": "apple_healthkit",
                "pipeline": "Apple Health",
                "sample_source": "Apple Watch",
            },
        ],
    }

    r = _client(app).post("/api/health/ingest", json=body)

    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["persisted"] == 2
    assert payload["skipped"] == 0
    assert fake_memory.save.await_count == 2

    # The first call's content line includes the bpm value + unit so
    # the memory tool surfaces a human-readable record. Tags include
    # ``health`` + the event_type so query routing finds it.
    first_call = fake_memory.save.await_args_list[0]
    assert "72" in first_call.kwargs["content"]
    assert "bpm" in first_call.kwargs["content"]
    assert "health" in first_call.kwargs["tags"]
    assert "heart_rate" in first_call.kwargs["tags"]


def test_health_ingest_skips_malformed_samples(app_with_route):
    """The envelope is strict; per-sample shape is permissive. A
    sample missing ``event_type`` is counted as ``skipped`` so the
    iOS UI can show "Synced 1 / 2 samples — 1 invalid" without
    failing the whole batch."""
    app, fake_memory = app_with_route
    body = {
        "samples": [
            {"event_type": "heart_rate", "bpm": 70},
            {"bpm": 72},  # missing event_type → skipped
            "not-a-dict",  # wrong type → skipped
        ],
    }

    r = _client(app).post("/api/health/ingest", json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["persisted"] == 1
    assert payload["skipped"] == 2
    assert fake_memory.save.await_count == 1


def test_health_ingest_rejects_non_object_body(app_with_route):
    app, _ = app_with_route
    r = _client(app).post("/api/health/ingest", json=[])
    assert r.status_code == 400
    assert "JSON object" in r.json().get("error", "")


def test_health_ingest_rejects_missing_samples_field(app_with_route):
    app, _ = app_with_route
    r = _client(app).post("/api/health/ingest", json={"source": "ios.healthkit"})
    assert r.status_code == 400
    assert "samples" in r.json().get("error", "")


def test_health_ingest_handles_memory_save_failure(app_with_route, monkeypatch):
    """If ``memory.save`` raises for one sample, that sample is
    counted as ``skipped`` and the rest of the batch still lands.
    Defensive contract pins so a transient memory hiccup doesn't
    fail the whole HealthKit poll."""
    app, fake_memory = app_with_route

    call_count = {"n": 0}

    async def flaky_save(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("memory write race")
        return None

    fake_memory.save = AsyncMock(side_effect=flaky_save)

    body = {
        "samples": [
            {"event_type": "heart_rate", "bpm": 70},
            {"event_type": "heart_rate", "bpm": 72},
        ],
    }
    r = _client(app).post("/api/health/ingest", json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["persisted"] == 1
    assert payload["skipped"] == 1
