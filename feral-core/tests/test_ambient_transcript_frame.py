"""The wire half: ack, idempotency, and surviving a brain that dies.

The phone drops a transcript once the brain acks it, so the ack is a
promise about durability. These tests hold that promise and the
idempotency that makes a lost ack cost a resend rather than a duplicate
episode.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import api.server as server


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, obj):
        self.sent.append(obj)


class FakeLLM:
    available = True

    async def chat(self, messages, **kwargs):
        if "Output ONLY valid JSON" in messages[0]["content"]:
            return {"choices": [{"message": {"content": (
                '{"summary":"Talked to Noah about the SDK.","people":["Noah"],'
                '"topics":["sdk"],"commitments":[{"text":"send Noah the SDK by Friday",'
                '"due_iso":"2026-08-21"}]}'
            )}}]}
        return {"choices": [{"message": {"content": "segment"}}]}

    @staticmethod
    def extract_response(response):
        return response["choices"][0]["message"]["content"], None


@pytest.fixture
def brain(tmp_path, monkeypatch):
    """A state stub with the collaborators the handler actually touches."""
    from agents.intent_compiler import IntentCompiler
    from memory.store import MemoryStore

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setattr(
        server, "_ambient_db_path", lambda: tmp_path / "ambient_transcripts.db",
    )

    tasks: list = []
    state = server.state
    monkeypatch.setattr(state, "memory", MemoryStore(db_path=str(tmp_path / "m.db")), raising=False)
    monkeypatch.setattr(state, "intent_compiler",
                        IntentCompiler(llm=None, db_path=str(tmp_path / "i.db")), raising=False)
    monkeypatch.setattr(state, "orchestrator", type("O", (), {"llm": FakeLLM()})(), raising=False)
    monkeypatch.setattr(state, "register_background_task", tasks.append, raising=False)
    monkeypatch.setattr(state, "bind_session_to_daemon", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(state, "primary_session_id", "sess-1", raising=False)
    return {"state": state, "tasks": tasks}


def _frame(text="Hey Noah. I'll send Noah the SDK by Friday.", **over):
    payload = {
        "transcript_id": "t-1",
        "text": text,
        "started_at": time.time() - 86400,
        "source": "glasses_mic",
        "speakers": ["Noah"],
    }
    payload.update(over)
    return {"payload": payload}


async def _send(ws, raw):
    """asyncio_mode = "auto", so tests are async and share one loop.

    Calling asyncio.run more than once per test closed the loop the
    deferred summarization tasks were created on.
    """
    await server._handle_ambient_transcript(ws, "node-a", "dev-1", raw)


class TestTheAck:
    async def test_an_ack_is_sent(self, brain):
        """Without one the phone has no signal to stop resending; today
        only chat_request gets a response at all."""
        ws = FakeWS()
        await _send(ws, _frame())
        assert ws.sent, "no frame was sent back"
        assert ws.sent[0]["type"] == "ambient_transcript_ack"

    async def test_the_ack_carries_the_transcript_id(self, brain):
        ws = FakeWS()
        await _send(ws, _frame())
        assert ws.sent[0]["payload"]["transcript_id"] == "t-1"

    async def test_the_ack_is_sent_before_summarization_runs(self, brain):
        """Acking on summary completion would mean a brain restarted
        mid-drain loses transcripts the phone has already dropped."""
        ws = FakeWS()
        await _send(ws, _frame())
        assert ws.sent[0]["payload"]["accepted"] is True
        assert brain["tasks"], "summarization was not deferred"

    async def test_a_resend_is_marked_duplicate(self, brain):
        first, second = FakeWS(), FakeWS()
        await _send(first, _frame())
        await _send(second, _frame())
        assert first.sent[0]["payload"]["duplicate"] is False
        assert second.sent[0]["payload"]["duplicate"] is True

    async def test_an_unidentified_node_gets_an_error_not_an_ack(self, brain):
        ws = FakeWS()
        await server._handle_ambient_transcript(ws, "", "dev-1", _frame())
        assert ws.sent[0]["type"] == "error"

    async def test_empty_text_is_refused(self, brain):
        ws = FakeWS()
        await _send(ws, _frame(text="   "))
        assert ws.sent[0]["type"] == "error"


class TestIdempotency:
    async def test_only_the_first_send_schedules_work(self, brain):
        """episode_save mints a fresh uuid4 per call and has no dedupe,
        so a resend past the gate writes a second episode."""
        for _ in range(3):
            await _send(FakeWS(), _frame())
        assert len(brain["tasks"]) == 1

    async def test_one_episode_after_three_sends(self, brain):
        for _ in range(3):
            await _send(FakeWS(), _frame())
        await asyncio.gather(*brain["tasks"])
        episodes = await brain["state"].memory.episode_recent(limit=20)
        ambient = [e for e in episodes if e["event_type"] == "ambient_conversation"]
        assert len(ambient) == 1

    async def test_distinct_transcripts_are_both_kept(self, brain):
        await _send(FakeWS(), _frame(transcript_id="t-1"))
        await _send(FakeWS(), _frame(transcript_id="t-2", text="Different chat entirely."))
        assert len(brain["tasks"]) == 2


class TestDurability:
    async def test_the_text_is_on_disk_before_the_ack(self, brain):
        """The gate stores the transcript, not just its id. The phone
        drops its copy on the ack, so ours is the only one left."""
        await _send(FakeWS(), _frame())
        pending = server._ambient_pending()
        assert pending, "nothing was persisted"
        assert "Noah" in pending[0]["payload"]["text"]

    async def test_an_unprocessed_transcript_is_resumable(self, brain):
        """A brain that died mid-summarize must finish from its own copy,
        because no resend is coming."""
        await _send(FakeWS(), _frame())
        brain["tasks"].clear()          # simulate the process dying here
        pending = server._ambient_pending()
        assert len(pending) == 1

        await server._resume_ambient_backlog()
        episodes = await brain["state"].memory.episode_recent(limit=20)
        assert [e for e in episodes if e["event_type"] == "ambient_conversation"]
        assert server._ambient_pending() == [], "still marked unprocessed after a resume"

    async def test_a_processed_transcript_is_not_resumed_again(self, brain):
        await _send(FakeWS(), _frame())
        await asyncio.gather(*brain["tasks"])
        await server._resume_ambient_backlog()
        episodes = await brain["state"].memory.episode_recent(limit=20)
        ambient = [e for e in episodes if e["event_type"] == "ambient_conversation"]
        assert len(ambient) == 1


class TestWhatTheUserGetsBack:
    async def test_the_conversation_is_recallable_by_the_other_persons_name(self, brain):
        await _send(FakeWS(), _frame())
        await asyncio.gather(*brain["tasks"])
        episodes = await brain["state"].memory.episode_recent(limit=20)
        ambient = [e for e in episodes if e["event_type"] == "ambient_conversation"][0]
        assert "Noah" in ambient["summary"]

    async def test_it_is_dated_when_it_happened_not_when_it_arrived(self, brain):
        """The phone queues while the brain is off, so a transcript
        normally lands hours or days late and timeline recall filters on
        created_at alone."""
        await _send(FakeWS(), _frame())
        await asyncio.gather(*brain["tasks"])
        episodes = await brain["state"].memory.episode_recent(limit=20)
        ambient = [e for e in episodes if e["event_type"] == "ambient_conversation"][0]
        assert time.time() - ambient["created_at"] > 80000

    async def test_the_promise_reaches_the_briefing(self, brain):
        """The brief does not read memory, so the episode alone would
        never surface what the user said they would do."""
        await _send(FakeWS(), _frame())
        await asyncio.gather(*brain["tasks"])
        agenda = [a["action"] for a in brain["state"].intent_compiler.get_today_actions()]
        assert "send Noah the SDK by Friday" in agenda

    async def test_a_resent_transcript_does_not_double_the_promise(self, brain):
        await _send(FakeWS(), _frame())
        await _send(FakeWS(), _frame())
        await asyncio.gather(*brain["tasks"])
        agenda = brain["state"].intent_compiler.get_today_actions()
        assert len(agenda) == 1
