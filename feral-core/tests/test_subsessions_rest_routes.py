"""PR 7: REST surface for listing / cancelling / steering  subsessions
plus the combined `/api/agents/active` snapshot.

Locks in:
* GET  /api/sessions/{id}/subsessions returns the registry view.
* POST /api/sessions/{id}/subsessions/{child_id}/cancel cancels one.
* POST /api/sessions/{id}/subsessions/cancel-all cancels all.
* POST /api/sessions/{id}/subsessions/{child_id}/steer pushes a steer
  message and surfaces "no steer hook" honestly when the supervisor
  lacks one (no fake 200).
* GET  /api/agents/active fans in subsessions + open taskflows +
  active intent plans (without faking presence of subsystems that
  aren't wired).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


pytestmark = pytest.mark.no_auto_feral_home


# A single long-lived event loop shared across the spawn + rest-call
# halves of each test. ``asyncio.run`` would close the loop after the
# spawn coroutine returns, which would auto-cancel the just-spawned
# subsession task and break the ``running == True`` assertion downstream.
_TEST_LOOP = asyncio.new_event_loop()


def _run_in_loop(coro):
    return _TEST_LOOP.run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset_registry(tmp_path, monkeypatch):
    """ spawner is process-global; reset between tests."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    from agents import subagent_policy
    from agents.subagent_spawner import get_registry, register_runner, register_supervisor

    subagent_policy.clear()
    get_registry().reset()
    register_runner(None)
    register_supervisor(None)
    yield
    # Reap any pending child tasks before clearing so we don't leak
    # asyncio "Task was destroyed but it is pending" warnings into
    # neighbouring tests' captured stderr.
    loop = asyncio.new_event_loop()
    try:
        for parent_id in list(get_registry()._by_parent.keys()):  # type: ignore[attr-defined]
            loop.run_until_complete(get_registry().cancel_all_children(parent_id))
    finally:
        loop.close()
    subagent_policy.clear()
    get_registry().reset()
    register_runner(None)
    register_supervisor(None)


def _make_state_with_supervisor(*, paused: bool = False, taskflows=None, intent_compiler=None):
    """Mock just enough of BrainState for the routes under test."""
    state = MagicMock()
    state.supervisor = SimpleNamespace(paused=paused, record=MagicMock(), policy_gate=None)
    # Routes do `sup._record(event)` for audit. SimpleNamespace doesn't
    # auto-attach methods; give it a no-op.
    state.supervisor._record = MagicMock()
    state.taskflows = taskflows
    state.intent_compiler = intent_compiler
    state.primary_session_id = "primary-1"
    return state


@pytest.fixture()
def app_client():
    state = _make_state_with_supervisor()
    with patch("api.routes.sessions.state", state):
        from fastapi import FastAPI

        from api.routes.sessions import router
        app = FastAPI()
        app.include_router(router)
        yield TestClient(app, raise_server_exceptions=False), state


async def _hold_until_cancel_runner(*, cancel_event: asyncio.Event, **_):
    await cancel_event.wait()


# ── list ───────────────────────────────────────────────────────────────


def test_list_subsessions_for_parent_with_no_children_returns_empty(app_client):
    client, _state = app_client
    resp = client.get("/api/sessions/no-such-parent/subsessions")
    assert resp.status_code == 200
    assert resp.json() == {"subsessions": []}


def test_list_subsessions_returns_running_children(app_client):
    client, _state = app_client
    from agents.subagent_spawner import register_runner, spawn_subsession

    register_runner(_hold_until_cancel_runner)

    async def _spawn():
        return await spawn_subsession("parent-list", "tool_runner", scope_key="alpha")
    child_id = _run_in_loop(_spawn())

    resp = client.get("/api/sessions/parent-list/subsessions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["subsessions"]) == 1
    item = data["subsessions"][0]
    assert item["child_id"] == child_id
    assert item["kind"] == "tool_runner"
    assert item["scope_key"] == "alpha"
    assert item["running"] is True


# ── cancel ─────────────────────────────────────────────────────────────


def test_cancel_one_subsession_succeeds(app_client):
    client, _state = app_client
    from agents.subagent_spawner import register_runner, spawn_subsession

    register_runner(_hold_until_cancel_runner)

    async def _spawn():
        return await spawn_subsession("parent-cancel", "research", scope_key="beta")
    child_id = _run_in_loop(_spawn())

    resp = client.post(f"/api/sessions/parent-cancel/subsessions/{child_id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] == 1
    assert body["child_id"] == child_id

    # Cancelled child is gone from the registry.
    follow = client.get("/api/sessions/parent-cancel/subsessions")
    assert follow.json() == {"subsessions": []}


def test_cancel_unknown_subsession_returns_404(app_client):
    client, _state = app_client
    resp = client.post("/api/sessions/parent-none/subsessions/no-such-child/cancel")
    assert resp.status_code == 404


def test_cancel_when_supervisor_paused_returns_423(app_client):
    client, state = app_client
    state.supervisor.paused = True
    resp = client.post("/api/sessions/p/subsessions/c/cancel")
    assert resp.status_code == 423


def test_cancel_all_subsessions_returns_count(app_client):
    client, _state = app_client
    from agents.subagent_spawner import register_runner, spawn_subsession

    register_runner(_hold_until_cancel_runner)

    async def _spawn_many():
        await spawn_subsession("parent-bulk", "tool_runner", scope_key="alpha")
        await spawn_subsession("parent-bulk", "research", scope_key="alpha")
    _run_in_loop(_spawn_many())

    resp = client.post("/api/sessions/parent-bulk/subsessions/cancel-all")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] == 2


# ── steer ─────────────────────────────────────────────────────────────


def test_steer_without_supervisor_hook_returns_503_truthfully(app_client):
    """The supervisor SimpleNamespace fixture has no `.steer` attr, so
    the spawner raises RuntimeError. The route MUST surface that as
    503 rather than pretending the steer succeeded. PR 7 truthfulness
    contract."""
    client, _state = app_client
    from agents.subagent_spawner import register_runner, spawn_subsession

    register_runner(_hold_until_cancel_runner)

    async def _spawn():
        return await spawn_subsession("parent-steer", "research", scope_key="alpha")
    child_id = _run_in_loop(_spawn())

    resp = client.post(
        f"/api/sessions/parent-steer/subsessions/{child_id}/steer",
        json={"message": "refocus on the API stub"},
    )
    assert resp.status_code == 503
    assert "no steer hook" in resp.json()["detail"]


def test_steer_unknown_child_returns_404(app_client):
    client, _state = app_client
    resp = client.post(
        "/api/sessions/parent-x/subsessions/no-such/steer",
        json={"message": "go"},
    )
    assert resp.status_code == 404


def test_steer_missing_message_returns_400(app_client):
    client, _state = app_client
    resp = client.post(
        "/api/sessions/p/subsessions/c/steer",
        json={},
    )
    assert resp.status_code == 400


def test_steer_dispatches_to_supervisor_hook_when_present(app_client):
    """When the supervisor exposes a steer callback, the route must
    relay the message and report the outcome — no swallowing."""
    client, state = app_client
    received: list[dict] = []

    async def _steer(*, parent_id, child_id, message):
        received.append({"parent_id": parent_id, "child_id": child_id, "message": message})
        return {"ack": True}

    state.supervisor.steer = _steer

    from agents.subagent_spawner import (
        register_runner,
        register_supervisor,
        spawn_subsession,
    )

    register_supervisor(state.supervisor)
    register_runner(_hold_until_cancel_runner)

    async def _spawn():
        return await spawn_subsession("parent-go", "tool_runner", scope_key="z")
    child_id = _run_in_loop(_spawn())

    resp = client.post(
        f"/api/sessions/parent-go/subsessions/{child_id}/steer",
        json={"message": "switch to debug mode"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["steered"] is True
    assert body["child_id"] == child_id
    assert received == [
        {"parent_id": "parent-go", "child_id": child_id, "message": "switch to debug mode"},
    ]


# ── active snapshot ────────────────────────────────────────────────────


def test_active_endpoint_empty_when_no_work_in_flight():
    state = _make_state_with_supervisor()
    with patch("api.routes.sessions.state", state):
        from fastapi import FastAPI

        from api.routes.sessions import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)
        data = client.get("/api/agents/active").json()
    assert data["subsessions"] == []
    assert data["taskflows"] == []
    assert data["intent_plans"] == []


def test_active_endpoint_surfaces_subsessions_and_open_taskflows():
    # Build fake taskflow + intent_compiler stubs with explicit return
    # values so the route doesn't accidentally call real DBs.
    taskflows = MagicMock()
    taskflows.list_flows.return_value = [
        {"id": "tf-1", "status": "running", "title": "Build it"},
        {"id": "tf-2", "status": "completed", "title": "Done"},  # filtered out
    ]
    intent_compiler = MagicMock()
    intent_compiler.list_plans.return_value = [
        {"plan_id": "p-1", "status": "active", "goal": "fix bug"},
        {"plan_id": "p-2", "status": "completed", "goal": "old"},  # filtered out
    ]
    state = _make_state_with_supervisor(taskflows=taskflows, intent_compiler=intent_compiler)

    with patch("api.routes.sessions.state", state):
        from fastapi import FastAPI

        from agents.subagent_spawner import register_runner, spawn_subsession
        from api.routes.sessions import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)

        register_runner(_hold_until_cancel_runner)

        async def _spawn():
            return await spawn_subsession("parent-active", "research", scope_key="z")
        child_id = _run_in_loop(_spawn())

        data = client.get("/api/agents/active").json()

    assert any(s["child_id"] == child_id for s in data["subsessions"])
    assert [t["id"] for t in data["taskflows"]] == ["tf-1"]
    assert [p["plan_id"] for p in data["intent_plans"]] == ["p-1"]
    assert data["warnings"] == []
