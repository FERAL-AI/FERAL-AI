"""GET /api/memory/recent_summary, the phone's view of the last day.

The phone rebuilt a recap from /api/sessions/primary/transcript, which only
ever holds the primary thread, so nothing said by voice or in another chat
thread reached it. This endpoint reads conversation episodes across every
session and has a model write one short digest.

Pinned here: it only reads conversation (screen captures are about 90% of a
day's episodes), it makes no model call when there is nothing to summarise,
it caches one call per window, and a failure is reported and never cached.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.memory as memory_routes


class _Memory:
    def __init__(self, episodes):
        self.episodes = episodes
        self.calls = []

    async def episode_recent(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.episodes)


class _LLM:
    def __init__(self, text="You checked whether the CuteBot was connected.", boom=False):
        self.text, self.boom, self.prompts = text, boom, []

    async def chat(self, messages, tools=None):
        self.prompts.append(messages[0]["content"])
        if self.boom:
            raise RuntimeError("provider down")
        return {"text": self.text}

    def extract_response(self, response):
        return response["text"], []


def _episodes():
    now = time.time()
    return [  # newest first, as episode_recent returns them
        {"id": "ep-3", "session_id": "voice-phone", "event_type": "assistant_reply",
         "summary": "The CuteBot is not connected.", "created_at": now - 60},
        {"id": "ep-2", "session_id": "primary", "event_type": "user_command",
         "summary": "is my cutebot connected", "created_at": now - 120},
        {"id": "ep-1", "session_id": "thread-b", "event_type": "user_command",
         "summary": "what can you do", "created_at": now - 3600},
    ]


@pytest.fixture(autouse=True)
def _fresh_cache():
    memory_routes._recent_summary_cache.clear()
    yield
    memory_routes._recent_summary_cache.clear()


def _get(state, path="/api/memory/recent_summary"):
    app = FastAPI()
    app.include_router(memory_routes.router)
    with patch("api.routes.memory.state", state):
        return TestClient(app).get(path)


def test_reads_conversation_only_across_sessions():
    memory = _Memory(_episodes())
    state = SimpleNamespace(memory=memory, orchestrator=SimpleNamespace(llm=_LLM()))
    body = _get(state).json()

    call = memory.calls[0]
    assert set(call["event_types"]) == {"user_command", "assistant_reply", "ambient_conversation"}
    assert call["since"] == pytest.approx(time.time() - 24 * 3600, abs=5)
    assert body["episode_count"] == 3
    assert body["sessions"] == 3
    assert body["summary"] == "You checked whether the CuteBot was connected."


def test_prompt_reads_oldest_first_with_roles():
    llm = _LLM()
    state = SimpleNamespace(memory=_Memory(_episodes()), orchestrator=SimpleNamespace(llm=llm))
    _get(state)
    prompt = llm.prompts[0]
    assert prompt.index("user: what can you do") < prompt.index("user: is my cutebot connected")
    assert "assistant: The CuteBot is not connected." in prompt


def test_no_conversation_means_no_model_call():
    llm = _LLM()
    state = SimpleNamespace(memory=_Memory([]), orchestrator=SimpleNamespace(llm=llm))
    body = _get(state).json()
    assert body["summary"] == ""
    assert body["episode_count"] == 0
    assert llm.prompts == []


def test_one_model_call_per_window_until_a_new_episode_lands():
    llm = _LLM()
    memory = _Memory(_episodes())
    state = SimpleNamespace(memory=memory, orchestrator=SimpleNamespace(llm=llm))
    first = _get(state).json()
    second = _get(state).json()
    assert len(llm.prompts) == 1
    assert (first["cached"], second["cached"]) == (False, True)

    memory.episodes.insert(0, {"id": "ep-4", "session_id": "primary",
                               "event_type": "user_command", "summary": "thanks",
                               "created_at": time.time()})
    third = _get(state).json()
    assert len(llm.prompts) == 2
    assert third["cached"] is False


def test_a_failed_generation_is_reported_and_not_cached():
    llm = _LLM(boom=True)
    state = SimpleNamespace(memory=_Memory(_episodes()), orchestrator=SimpleNamespace(llm=llm))
    body = _get(state).json()
    assert body["summary"] == ""
    assert "failed" in body["error"]
    _get(state)
    assert len(llm.prompts) == 2, "a failure must not be served from cache"


def test_hours_is_clamped():
    memory = _Memory([])
    state = SimpleNamespace(memory=memory, orchestrator=SimpleNamespace(llm=_LLM()))
    body = _get(state, "/api/memory/recent_summary?hours=500").json()
    assert body["hours"] == 72


def test_phone_bearer_may_read_it():
    import api.server as server
    assert "/api/memory/recent_summary" in server._PHONE_BEARER_GET._literals
