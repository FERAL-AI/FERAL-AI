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


# ── Rows restored from the boot snapshot are not replayed ──────────────
#
# The primary session receives no turns once every web thread binds to its
# own session, so its snapshot was re-saved unchanged on each shutdown and
# restored on each boot. Both clients merge this endpoint's output into the
# open thread, so a July 30 exchange ("What date should I schedule it
# for...", "10 am PST") appeared as a new message after every restart, in
# 21 threads.


def _restored_state():
    restored_q = {"role": "assistant", "content": "What date should I schedule it for?"}
    restored_a = {"role": "user", "content": "10 am PST"}
    live = {"role": "user", "content": "check if my cutebot is connected"}
    state = _make_state({"primary-test": [restored_q, restored_a, live]})
    state.restored_history_rows = {"primary-test": [restored_q, restored_a]}
    return state, restored_q, restored_a, live


def test_rows_restored_from_snapshot_are_not_replayed():
    state, *_ = _restored_state()
    with patch("api.routes.sessions.state", state):
        body = _mount_router(state).get("/api/sessions/primary/transcript").json()
    assert [m["text"] for m in body["messages"]] == ["check if my cutebot is connected"]
    # Position is unchanged, so since_ms polling is unaffected.
    assert body["messages"][0]["ts_ms"] == 3


def test_restored_rows_stay_hidden_after_trim_and_compaction():
    state, restored_q, restored_a, live = _restored_state()
    summary = {"role": "system", "content": "[Session Summary]\n..."}
    # Same objects, reshaped the way the orchestrator trims and compacts.
    state.orchestrator.conversation_history["primary-test"] = [summary, restored_a, live]
    with patch("api.routes.sessions.state", state):
        body = _mount_router(state).get("/api/sessions/primary/transcript").json()
    assert [m["text"] for m in body["messages"]] == ["check if my cutebot is connected"]


def test_a_live_turn_repeating_old_text_is_still_delivered():
    """Identity, not content: saying the same words again is a real turn."""
    state, restored_q, restored_a, live = _restored_state()
    again = {"role": "user", "content": "10 am PST"}
    state.orchestrator.conversation_history["primary-test"].append(again)
    with patch("api.routes.sessions.state", state):
        body = _mount_router(state).get("/api/sessions/primary/transcript").json()
    assert [m["text"] for m in body["messages"]] == [
        "check if my cutebot is connected", "10 am PST",
    ]


def test_boot_hydration_records_what_it_restored():
    from api.state import BrainState

    rows = [
        {"role": "assistant", "content": "What date should I schedule it for?"},
        {"role": "user", "content": "10 am PST"},
    ]
    fake = SimpleNamespace(
        session_snapshot=SimpleNamespace(load=lambda: {
            "session_id": "primary-test", "conversation_history": rows,
        }),
        orchestrator=SimpleNamespace(conversation_history={}),
        memory=None,
        primary_session_id="primary-test",
        restored_history_rows={},
    )
    # Bind the real new guard rather than bypassing checkpoint ownership in
    # this deliberately minimal legacy-hydration fixture.
    fake._primary_context_uses_checkpoints = BrainState._primary_context_uses_checkpoints.__get__(fake)
    BrainState._hydrate_primary_thread_from_snapshot(fake)
    restored = fake.orchestrator.conversation_history["primary-test"]
    assert len(restored) == 2
    assert {id(r) for r in fake.restored_history_rows["primary-test"]} == {
        id(r) for r in restored
    }
