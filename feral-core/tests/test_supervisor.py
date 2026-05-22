"""Supervisor tests — single oversight seat for all orchestrator entry points."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.supervisor import (
    Supervisor,
    SupervisorBlocked,
    SupervisorStore,
)


pytestmark = pytest.mark.no_auto_feral_home


# ── Store ────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return SupervisorStore(db_path=str(tmp_path / "sup.db"))


def test_store_roundtrip(store):
    from agents.supervisor import SupervisorEvent
    import time

    ev = SupervisorEvent(
        event_id="e1",
        ts=time.time(),
        source="web",
        kind="command",
        session_id="s1",
        actor="user",
        payload_hash="h",
        payload_summary="hello world",
        decision="allowed",
        latency_ms=7,
    )
    store.insert(ev)
    rows = store.recent(limit=10)
    assert len(rows) == 1
    assert rows[0]["source"] == "web"
    assert rows[0]["payload_summary"] == "hello world"


def test_store_filters(store):
    from agents.supervisor import SupervisorEvent
    import time

    for i, src in enumerate(["web", "voice", "cron"]):
        store.insert(SupervisorEvent(
            event_id=f"e{i}",
            ts=time.time() + i,
            source=src,
            kind="command",
            session_id="s",
            actor="user",
            payload_hash="h",
            payload_summary="x",
            decision="allowed",
            latency_ms=1,
        ))
    web_only = store.recent(source="web")
    assert len(web_only) == 1
    assert web_only[0]["source"] == "web"


# ── Supervisor wrap ──────────────────────────────────────────────


@pytest.fixture
def supervisor(tmp_path):
    store = SupervisorStore(db_path=str(tmp_path / "sup.db"))
    return Supervisor(store=store)


@pytest.mark.asyncio
async def test_wrap_audits_every_call(supervisor):
    orch = MagicMock()
    orch.handle_command = AsyncMock(return_value="ok")
    orch.handle_command_stream = AsyncMock(return_value="stream-ok")
    orch.handle_ui_event = AsyncMock(return_value={"handled": True})

    supervisor.wrap(orch)

    await orch.handle_command("sess-1", "hello", context={"source": "web"})
    await orch.handle_command_stream("sess-2", "stream", context={"source": "voice"})
    await orch.handle_ui_event(
        session_id="sess-3",
        action_id="confirm",
        event="tap",
        value=None,
        app_id="",
        screen_id="",
    )

    rows = supervisor.recent(limit=10)
    assert len(rows) == 3
    sources = {r["source"] for r in rows}
    assert sources == {"web", "voice", "web"} or "web" in sources


@pytest.mark.asyncio
async def test_pause_blocks_every_call(supervisor):
    """Lane 08 WS6 — paused supervisor returns None and emits a
    structured ``refusal`` WS frame instead of raising
    ``SupervisorBlocked``. The pre-WS6 contract used to surface as a
    500 from the WS handler.
    """
    inner = AsyncMock(return_value="ok")
    orch = MagicMock()
    orch.handle_command = inner
    orch.send = AsyncMock()
    supervisor.wrap(orch)

    supervisor.set_paused(True)
    result = await orch.handle_command("sess", "msg", context={"source": "web"})
    assert result is None

    rows = supervisor.recent()
    assert rows[0]["decision"] == "denied"
    inner.assert_not_called()

    # WS6 — refusal frame emitted via orch.send.
    sent = orch.send.call_args_list
    assert len(sent) == 1
    msg = sent[0].args[1]
    dumped = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
    assert dumped["type"] == "refusal"
    assert dumped["payload"]["reason"] == "supervisor_paused"


@pytest.mark.asyncio
async def test_policy_gate_denied(supervisor):
    """Lane 08 WS6 — policy-gate denial returns None and emits a
    structured ``refusal`` frame instead of raising.
    """
    orch = MagicMock()
    orch.handle_command = AsyncMock(return_value="ok")
    orch.send = AsyncMock()

    def gate(event):
        return "denied" if "dangerous" in event.payload_summary else "allowed"

    supervisor.policy_gate = gate
    supervisor.wrap(orch)

    result = await orch.handle_command("sess", "dangerous act", context={"source": "web"})
    assert result is None

    # Allowed call goes through.
    await orch.handle_command("sess", "hello", context={"source": "web"})
    rows = supervisor.recent()
    assert rows[0]["decision"] == "allowed"
    assert rows[1]["decision"] == "denied"

    # Exactly one refusal frame for the denied call.
    refusal_calls = [
        c for c in orch.send.call_args_list
        if getattr(c.args[1], "type", "") == "refusal"
    ]
    assert len(refusal_calls) == 1
    payload = refusal_calls[0].args[1].model_dump()["payload"]
    assert payload["reason"] == "policy_denied"


@pytest.mark.asyncio
async def test_policy_gate_queued(supervisor):
    inner = AsyncMock(return_value="real")
    orch = MagicMock()
    orch.handle_command = inner

    supervisor.policy_gate = lambda e: "queued"
    supervisor.wrap(orch)

    result = await orch.handle_command("sess", "x", context={"source": "twin"})
    assert result["queued"] is True
    inner.assert_not_called()


@pytest.mark.asyncio
async def test_broadcaster_called_with_event_frame(supervisor):
    orch = MagicMock()
    orch.handle_command = AsyncMock(return_value="ok")
    frames = []

    def broadcaster(frame):
        frames.append(frame)
        return None

    supervisor.broadcaster = broadcaster
    supervisor.wrap(orch)
    await orch.handle_command("sess", "hi", context={"source": "web"})
    assert len(frames) == 1
    assert frames[0]["type"] == "supervisor_event"
    assert frames[0]["payload"]["source"] == "web"


def test_record_for_non_orchestrator_sources(supervisor):
    ev = supervisor.record(
        source="proactive",
        kind="alert",
        session_id="s1",
        actor="system",
        payload={"summary": "sleep anomaly"},
    )
    assert ev.source == "proactive"
    rows = supervisor.recent(source="proactive")
    assert len(rows) == 1


# ── REST surface ─────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    store = SupervisorStore(db_path=str(tmp_path / "sup.db"))
    supervisor = Supervisor(store=store)
    mock = MagicMock()
    mock.supervisor = supervisor
    with patch("api.state.state", mock), patch("api.routes.supervisor.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), supervisor


def test_rest_events_empty(client):
    c, _ = client
    r = c.get("/api/supervisor/events")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "events": []}


def test_rest_record_then_list(client):
    c, sup = client
    r = c.post("/api/supervisor/record", json={
        "source": "cron",
        "kind": "routine",
        "payload": "morning briefing",
    })
    assert r.status_code == 200
    r2 = c.get("/api/supervisor/events?source=cron")
    assert r2.status_code == 200
    events = r2.json()["events"]
    assert len(events) == 1
    assert events[0]["source"] == "cron"
    assert events[0]["payload_summary"] == "morning briefing"


def test_rest_pause_toggles_supervisor(client):
    c, sup = client
    r = c.post("/api/supervisor/pause", json={"paused": True})
    assert r.status_code == 200
    assert r.json()["paused"] is True
    assert sup.paused is True
    c.post("/api/supervisor/pause", json={"paused": False})
    assert sup.paused is False


@pytest.mark.asyncio
async def test_wrap_audits_handle_daemon_result(supervisor):
    """handle_daemon_result is a real orchestrator entry point and must
    flow through the Supervisor audit log too — not just the chat path."""
    inner = AsyncMock(return_value={"ok": True})
    orch = MagicMock()
    orch.handle_daemon_result = inner
    supervisor.wrap(orch)

    await orch.handle_daemon_result("node-abc", {"skill_id": "x"}, session_id="s1")

    rows = supervisor.recent(limit=5)
    assert len(rows) == 1
    assert rows[0]["kind"] == "handle_daemon_result"
    assert rows[0]["session_id"] == "s1"
    inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrap_covers_all_four_entry_points(supervisor):
    """Every public orchestrator entry point must be wrapped by Supervisor."""
    orch = MagicMock()
    orch.handle_command = AsyncMock(return_value=None)
    orch.handle_command_stream = AsyncMock(return_value=None)
    orch.handle_ui_event = AsyncMock(return_value=None)
    orch.handle_daemon_result = AsyncMock(return_value=None)
    supervisor.wrap(orch)

    wrapped_keys = set(supervisor._orig.keys())
    assert wrapped_keys == {
        "handle_command", "handle_command_stream",
        "handle_ui_event", "handle_daemon_result",
    }


def test_cron_context_source_lands_in_audit_log(supervisor):
    """When cron passes source=cron in context, the audit row must
    reflect that — not the default "web"."""
    supervisor.record(
        source="cron",
        kind="command",
        session_id="routine-42",
        actor="system",
        payload="briefing",
    )
    rows = supervisor.recent(source="cron")
    assert len(rows) == 1
    assert rows[0]["source"] == "cron"
    assert rows[0]["actor"] == "system"


def test_proactive_automation_landing(supervisor):
    """ProactiveEngine._execute_automation records through Supervisor."""
    ev = supervisor.record(
        source="proactive",
        kind="automation",
        actor="system",
        payload={"trigger_id": "t1", "action_type": "set_scene"},
        decision="allowed",
        detail={"payload": {"scene": "calming"}},
    )
    assert ev.source == "proactive"
    rows = supervisor.recent(source="proactive")
    assert len(rows) == 1
    assert rows[0]["actor"] == "system"


def test_rest_stats_reports_paused_and_sources(client):
    c, sup = client
    sup.record(source="web", kind="command", payload="x")
    sup.record(source="voice", kind="command", payload="x")
    r = c.get("/api/supervisor/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["by_source"].get("web") == 1
    assert body["paused"] is False
