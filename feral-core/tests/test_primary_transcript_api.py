"""Phase 9 (audit-r10 overhaul) — primary session transcript API.

Operator complaint:
   "chat stops after a single answer instead of continuing"

Root cause: when the iOS app backgrounded, the WebSocket tore down
and any in-flight brain replies were dropped. On resume the chat
appeared stuck. Phase 9 exposes the live `conversation_history`
for the primary session so the iOS app can reconcile its local
transcript against the brain's truth on `scenePhase: .active`.

Three concerns under test:
1. Happy path — populated history returns mapped messages with
   monotonic ts_ms positions.
2. `since_ms` filter — incremental polling only returns NEW turns.
3. Content shape tolerance — OpenAI vision content arrays
   (`[{"type": "text", "text": "..."}]`) flatten to plain strings
   so the iOS client never sees the structured shape.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_state(history: dict[str, list[dict]]):
    """Bare-minimum stand-in for `BrainState` for the API route."""
    state = SimpleNamespace()
    state.primary_session_id = "primary-test"
    state.orchestrator = SimpleNamespace(conversation_history=history)
    return state


def _mount_router(state):
    """Return a TestClient. Caller is responsible for the
    `with patch(...)` lifetime around it — context managers don't
    survive function returns."""
    from api.routes.sessions import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_primary_transcript_returns_user_and_assistant_turns():
    state = _make_state({
        "primary-test": [
            {"role": "system", "content": "you are FERAL"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "tool", "content": "tool result"},
            {"role": "user", "content": "and again"},
            {"role": "assistant", "content": "still here"},
        ],
    })

    with patch("api.routes.sessions.state", state):
        client = _mount_router(state)
        resp = client.get("/api/sessions/primary/transcript")

    assert resp.status_code == 200
    body = resp.json()
    assert body["primary_session_id"] == "primary-test"
    assert body["count"] == 4
    assert [m["role"] for m in body["messages"]] == [
        "user", "assistant", "user", "assistant"
    ]
    assert [m["text"] for m in body["messages"]] == [
        "hello", "hi there", "and again", "still here"
    ]
    # ts_ms is the position in the original history (1-based) so
    # `since_ms` semantics are stable across calls.
    assert body["messages"][0]["ts_ms"] == 2
    assert body["messages"][-1]["ts_ms"] == 6


def test_primary_transcript_since_ms_returns_only_new_turns():
    state = _make_state({
        "primary-test": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "follow up"},
            {"role": "assistant", "content": "sure"},
        ],
    })

    with patch("api.routes.sessions.state", state):
        client = _mount_router(state)
        # Pretend client last saw ts_ms=2 (the first assistant reply).
        resp = client.get("/api/sessions/primary/transcript", params={"since_ms": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert [m["text"] for m in body["messages"]] == ["follow up", "sure"]


def test_primary_transcript_flattens_openai_vision_content():
    state = _make_state({
        "primary-test": [
            {"role": "user", "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "there"},
            ]},
            {"role": "assistant", "content": "Hi!"},
        ],
    })

    with patch("api.routes.sessions.state", state):
        client = _mount_router(state)
        resp = client.get("/api/sessions/primary/transcript")

    body = resp.json()
    assert body["messages"][0]["text"] == "Hello there"


def test_primary_transcript_limit_clamps_to_500_and_returns_tail():
    big = [{"role": "user", "content": f"msg {i}"} for i in range(600)]
    state = _make_state({"primary-test": big})

    with patch("api.routes.sessions.state", state):
        client = _mount_router(state)
        resp = client.get("/api/sessions/primary/transcript", params={"limit": 9999})

    body = resp.json()
    # Hard-clamped to 500 inside the handler.
    assert body["count"] == 500
    # Tail: last message returned is the original last entry.
    assert body["messages"][-1]["text"] == "msg 599"


def test_primary_transcript_handles_empty_history():
    state = _make_state({})

    with patch("api.routes.sessions.state", state):
        client = _mount_router(state)
        resp = client.get("/api/sessions/primary/transcript")

    body = resp.json()
    assert body["primary_session_id"] == "primary-test"
    assert body["messages"] == []
    assert body["count"] == 0


def test_generic_session_transcript_is_isolated_per_thread():
    """RC fix (chat thread switching): each UI thread reads ITS OWN
    orchestrator session transcript, never the primary's. Two distinct
    sessions must not bleed into each other."""
    state = _make_state({
        "primary-test": [
            {"role": "user", "content": "primary q"},
            {"role": "assistant", "content": "primary a"},
        ],
        "thread-B": [
            {"role": "user", "content": "thread B q"},
            {"role": "assistant", "content": "thread B a"},
        ],
    })

    with patch("api.routes.sessions.state", state):
        client = _mount_router(state)
        resp_b = client.get("/api/sessions/thread-B/transcript")
        resp_primary = client.get("/api/sessions/primary/transcript")

    body_b = resp_b.json()
    assert body_b["session_id"] == "thread-B"
    assert [m["text"] for m in body_b["messages"]] == ["thread B q", "thread B a"]
    # Primary literal route still resolves (not shadowed by the {sid} route)
    # and returns only the primary thread's turns.
    body_primary = resp_primary.json()
    assert [m["text"] for m in body_primary["messages"]] == ["primary q", "primary a"]


def test_generic_session_transcript_unknown_session_is_empty():
    state = _make_state({"primary-test": [{"role": "user", "content": "hi"}]})
    with patch("api.routes.sessions.state", state):
        client = _mount_router(state)
        resp = client.get("/api/sessions/does-not-exist/transcript")
    body = resp.json()
    assert body["session_id"] == "does-not-exist"
    assert body["messages"] == []
    assert body["count"] == 0
