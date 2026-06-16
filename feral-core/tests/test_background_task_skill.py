"""RC fix (long-horizon tasks): the agent-facing background task surface.

TaskFlowRuntime is a persistent, restart-safe background engine, but it
was constructed without the orchestrator (so ``llm.chat`` steps failed)
and there was no convenience surface for the LLM to launch a task from a
plain goal. These tests pin the new ``/internal/task/*`` endpoints and
the ``task`` skill manifest.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeTaskflows:
    def __init__(self):
        self.created = []
        self._flows = {}

    def create_flow(self, *, session_id, title, steps, context=None):
        flow_id = f"flow{len(self.created)}"
        flow = {
            "id": flow_id,
            "session_id": session_id,
            "title": title,
            "status": "queued",
            "current_step": 0,
            "steps": [
                {"step_index": i, "step_type": s["type"], "status": "pending"}
                for i, s in enumerate(steps)
            ],
            "context": context or {},
        }
        self.created.append({"session_id": session_id, "title": title, "steps": steps})
        self._flows[flow_id] = flow
        return flow

    def get_flow(self, flow_id):
        return self._flows.get(flow_id)

    def list_flows(self, *, limit=50, **_):
        return list(self._flows.values())[:limit]


def _client(taskflows):
    state = SimpleNamespace(taskflows=taskflows, primary_session_id="primary-test")
    app = FastAPI()
    from api.routes.taskflows import router
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), state


def test_start_background_task_from_goal_creates_llm_chat_step():
    tf = _FakeTaskflows()
    client, state = _client(tf)
    with patch("api.routes.taskflows.state", state):
        resp = client.post("/internal/task/start", json={"goal": "research competitors"})
    body = resp.json()
    assert body["ok"] is True
    assert body["steps"] == 1
    assert tf.created[0]["steps"] == [{"type": "llm.chat", "prompt": "research competitors"}]
    # Empty session -> dedicated taskflow session (no live-chat pollution).
    assert tf.created[0]["session_id"] == ""


def test_start_background_task_from_subtasks_creates_sequential_steps():
    tf = _FakeTaskflows()
    client, state = _client(tf)
    with patch("api.routes.taskflows.state", state):
        resp = client.post(
            "/internal/task/start",
            json={"subtasks": ["step one", "step two", "step three"], "title": "Big job"},
        )
    body = resp.json()
    assert body["ok"] is True
    assert body["steps"] == 3
    assert [s["prompt"] for s in tf.created[0]["steps"]] == ["step one", "step two", "step three"]
    assert all(s["type"] == "llm.chat" for s in tf.created[0]["steps"])


def test_start_background_task_requires_goal_or_subtasks():
    tf = _FakeTaskflows()
    client, state = _client(tf)
    with patch("api.routes.taskflows.state", state):
        resp = client.post("/internal/task/start", json={})
    body = resp.json()
    assert body["ok"] is False
    assert "goal" in body["error"]


def test_background_task_status_reports_progress():
    tf = _FakeTaskflows()
    client, state = _client(tf)
    with patch("api.routes.taskflows.state", state):
        started = client.post("/internal/task/start", json={"subtasks": ["a", "b"]}).json()
        flow_id = started["flow_id"]
        # mark one step completed
        tf.get_flow(flow_id)["steps"][0]["status"] = "completed"
        resp = client.get("/internal/task/status", params={"flow_id": flow_id})
    body = resp.json()
    assert body["steps_total"] == 2
    assert body["steps_completed"] == 1


def test_task_manifest_is_valid_and_exposes_start():
    manifest_path = Path(__file__).resolve().parents[1] / "skills" / "manifests" / "task.json"
    data = json.loads(manifest_path.read_text())
    assert data["skill_id"] == "background_task"
    endpoint_ids = {e["id"] for e in data["endpoints"]}
    assert {"start", "status", "list"} <= endpoint_ids
    start = next(e for e in data["endpoints"] if e["id"] == "start")
    assert start["url"].endswith("/internal/task/start")
