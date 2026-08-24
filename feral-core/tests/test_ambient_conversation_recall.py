"""The agent could not see recorded conversations, and did not know it.

Reported 2026-08-22. Asked "is there any ambient conversation
recorded?", the agent answered "Right now, I don't have any ambient
audio recording active. I only pick up what's directly spoken here."
At that moment the brain held four transcripts, digests generated, and
commitments already extracted from one of them.

It was not a failed search. No tool existed for the question, so the
only thing it could consult was its own idea of what it is, and it
guessed wrong about itself. That is worse than an empty result: an
empty result is a fact about the world, this was a confident false
claim about a capability. The ADHD case this feature exists for is
"what did I say I'd do", and the brain had the answer and denied
holding it.

Four separate holes, one per section below.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

import api.server as srv
from agents.ambient_transcript import (
    EVENT_TYPE,
    TranscriptOutcome,
    _operator_identity_block,
    load_operator_identity,
)
from memory.ambient_conversations import (
    commitments_from_conversations,
    list_conversations,
    search_conversations,
)


@pytest.fixture()
def feral_home(tmp_path, monkeypatch):
    """An isolated FERAL_HOME. Never the operator's real one."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    import config.loader as loader

    monkeypatch.setattr(loader, "feral_home", lambda: tmp_path, raising=False)
    return tmp_path


def _seed(tid, summary, people, topics, commitments, *, processed=True):
    srv._ambient_store(
        tid, node_id="glasses-1", device_id="phone-1", session_id="s1",
        payload={"text": f"transcript body for {tid}"}, owner_key="",
    )
    if processed:
        outcome = TranscriptOutcome(
            summary=summary, detail=f"full transcript of {tid}",
            people=people, topics=topics, commitments=commitments,
        )
        srv._ambient_mark_processed(
            tid, f"ep-{tid}", json.dumps(asdict(outcome), default=str),
        )


@pytest.fixture()
def four_conversations(feral_home):
    """The state the brain was actually in when it denied having any."""
    _seed("t1", "Talked with Noah about the investor round closing Friday.",
          ["Noah"], ["investors"],
          [{"text": "send Noah the deck", "due_iso": "2026-08-29"}])
    _seed("t2", "Standup with Priya about the pairing bug.",
          ["Priya"], ["engineering"],
          [{"text": "write up the pairing repro", "due_iso": ""}])
    _seed("t3", "Coffee with Sam, mostly personal.", ["Sam"], ["personal"], [])
    _seed("t4", "", [], [], [], processed=False)
    return feral_home


# ── 1. the question is answerable at all ────────────────────────────


class TestConversationsAreVisible:

    def test_all_four_are_found(self, four_conversations):
        assert list_conversations()["total"] == 4

    def test_an_unsummarized_transcript_is_not_invisible(self, four_conversations):
        """`pending` HAS been recorded.

        Reporting only summarized ones would reproduce the same bug one
        layer down: the honest answer to "is anything recorded" is yes
        while it is still being processed.
        """
        listing = list_conversations()
        assert listing["pending"] == 1
        assert {c["status"] for c in listing["conversations"]} == {"ready", "pending"}

    def test_the_digest_fields_come_back(self, four_conversations):
        """summary, people, topics and commitments are what makes an
        answer groundable rather than asserted."""
        conv = next(
            c for c in list_conversations()["conversations"]
            if c["transcript_id"] == "t1"
        )
        assert "investor round" in conv["summary"]
        assert conv["people"] == ["Noah"]
        assert conv["topics"] == ["investors"]
        assert conv["commitments"][0]["text"] == "send Noah the deck"

    def test_nothing_recorded_is_a_clear_answer_not_a_crash(self, feral_home):
        """No store at all is the normal state on a fresh brain.

        It must read as "nothing recorded", never as an error: a tool
        that errors here pushes the model straight back to guessing,
        which is the defect being fixed.
        """
        listing = list_conversations()
        assert listing["total"] == 0
        assert listing["conversations"] == []
        assert listing["note"], "must say WHY it is empty"

    def test_full_transcripts_are_withheld_by_default(self, four_conversations):
        listing = list_conversations()
        assert all("detail" not in c for c in listing["conversations"])
        with_detail = list_conversations(include_detail=True)
        assert all("detail" in c for c in with_detail["conversations"])


class TestSearch:

    def test_finds_a_conversation_by_person(self, four_conversations):
        found = search_conversations("Noah")
        assert found["matched"] == 1
        assert found["conversations"][0]["people"] == ["Noah"]

    def test_finds_by_topic_and_by_commitment_text(self, four_conversations):
        assert search_conversations("pairing")["matched"] == 1
        assert search_conversations("deck")["matched"] == 1

    def test_a_miss_is_a_miss_and_says_what_it_searched(self, four_conversations):
        miss = search_conversations("zzzznotathing")
        assert miss["matched"] == 0
        assert miss["total_searched"] == 4, (
            "the model must be able to say 'I looked at 4 and found none'"
        )

    def test_an_empty_query_lists_rather_than_erroring(self, four_conversations):
        assert search_conversations("")["total"] == 4


class TestCommitments:
    """The whole point: 'what did I say I'd do'."""

    def test_every_spoken_commitment_is_reachable(self, four_conversations):
        out = commitments_from_conversations()
        assert out["count"] == 2
        assert {c["text"] for c in out["commitments"]} == {
            "send Noah the deck", "write up the pairing repro",
        }

    def test_each_commitment_carries_its_source(self, four_conversations):
        """So the answer can be grounded, not asserted."""
        c = next(
            x for x in commitments_from_conversations()["commitments"]
            if x["text"] == "send Noah the deck"
        )
        assert c["transcript_id"] == "t1"
        assert c["due_iso"] == "2026-08-29"
        assert "Noah" in c["people"]
        assert c["from_conversation"]

    def test_only_conversations_with_commitments(self, four_conversations):
        assert list_conversations(with_commitments_only=True)["returned"] == 2


# ── 2. the model can actually call it ───────────────────────────────


class TestTheSkillDispatch:
    """A helper the model cannot reach is the same as no helper."""

    @staticmethod
    def _call(endpoint, args):
        from skills.impl.notes_memory import NotesMemorySkill

        return asyncio.run(NotesMemorySkill().execute(endpoint, args, {}))

    def test_list_conversations_is_dispatched(self, four_conversations):
        r = self._call("list_conversations", {"limit": 5})
        assert r["success"] is True
        assert r["data"]["total"] == 4

    def test_search_conversations_is_dispatched(self, four_conversations):
        r = self._call("search_conversations", {"query": "pairing"})
        assert r["data"]["matched"] == 1

    def test_conversation_commitments_is_dispatched(self, four_conversations):
        r = self._call("conversation_commitments", {})
        assert r["data"]["count"] == 2

    def test_the_manifest_declares_them(self):
        """A python:// endpoint with no manifest entry is not offered to
        the model at all."""
        import json as _json
        from pathlib import Path

        manifest = _json.loads(
            (Path(__file__).resolve().parents[1]
             / "skills" / "manifests" / "notes.json").read_text()
        )
        ids = {e["id"] for e in manifest["endpoints"]}
        assert {
            "list_conversations", "search_conversations",
            "conversation_commitments",
        } <= ids

    def test_every_declared_endpoint_has_a_handler(self):
        """The manifest and the dispatch table must not drift.

        Read the dispatch table, do NOT call every endpoint. An earlier
        version of this test invoked each one with empty args, which
        executed `fused_timeline` for real against the process-global
        `api.state.state` -- calendar and health included -- and hung
        the next test in file order for its full timeout. A test for
        "is this name wired up" has no business running the work.
        """
        import inspect
        import json as _json
        from pathlib import Path

        from skills.impl.notes_memory import NotesMemorySkill

        manifest = _json.loads(
            (Path(__file__).resolve().parents[1]
             / "skills" / "manifests" / "notes.json").read_text()
        )
        source = inspect.getsource(NotesMemorySkill.execute)
        for endpoint in manifest["endpoints"]:
            assert f'"{endpoint["id"]}"' in source, (
                f"{endpoint['id']} is declared in the manifest but is not "
                "in the dispatch table"
            )


# ── 3. the prompt says the capability exists ────────────────────────


class TestTheSystemPromptStatesTheCapability:
    """Even with a tool, the model volunteered a wrong capability claim
    rather than checking. It has to be told that looking is the honest
    move."""

    @staticmethod
    def _prompt_source() -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "agents" / "identity_loader.py"
        ).read_text()

    def test_ambient_capture_is_declared(self):
        src = self._prompt_source()
        assert "## Ambient Conversation Capture" in src
        assert "glasses record real conversations" in src

    def test_it_names_the_tools_to_call(self):
        src = self._prompt_source()
        for tool in (
            "notes_memory__list_conversations",
            "notes_memory__search_conversations",
            "notes_memory__conversation_commitments",
        ):
            assert tool in src

    def test_it_forbids_answering_from_introspection(self):
        src = self._prompt_source()
        assert "LOOK, don't introspect" in src
        assert "I don't record audio" in src

    def test_it_says_pending_still_counts(self):
        assert "still being summarized" in self._prompt_source()


# ── 4. whose speech trains the self-model ───────────────────────────


class TestOverheardSpeechDoesNotRewriteTheOperator:
    """Every AboutMe pattern is first person. On a chat episode that "I"
    is the operator; on an overheard conversation it is whoever was
    talking, and their preferences, family and home town were being
    filed under the operator's identity at 0.5 confidence."""

    OVERHEARD = (
        "I prefer tea over coffee. My wife Sarah handles the invoices. "
        "I live in Lisbon and I work as a tax accountant."
    )

    @staticmethod
    def _save_and_count(tmp_path, event_type: str) -> int:
        from agents.about_me import AboutMeStore
        from memory.store import MemoryStore

        async def _run() -> int:
            store = MemoryStore(db_path=str(tmp_path / f"m_{event_type}.db"))
            about_me = AboutMeStore(db_path=str(tmp_path / f"a_{event_type}.db"))
            store.set_about_me_store(about_me)
            try:
                await store.episode_save(
                    session_id="s", event_type=event_type,
                    summary=TestOverheardSpeechDoesNotRewriteTheOperator.OVERHEARD,
                    detail=TestOverheardSpeechDoesNotRewriteTheOperator.OVERHEARD,
                )
                await asyncio.sleep(0.6)  # the extractor is fire-and-forget
                return len(about_me.list())
            finally:
                # aclose, not close, and not nothing.
                #
                # The extractor is scheduled with create_task on THIS
                # loop and the aiosqlite pool holds worker threads bound
                # to it. Letting asyncio.run() tear the loop down with
                # both still live left those threads parked on a dead
                # loop, and the next test in the file order
                # (test_manifest_param_defaults) hung for its full
                # 300 s timeout. Reproduced by running just those two
                # files together; this is what fixed it.
                await store.aclose()

        return asyncio.run(_run())

    def test_typed_chat_still_builds_the_profile(self, tmp_path):
        """The fix must not disable the feature for the case it is for."""
        assert self._save_and_count(tmp_path, "conversation") > 0

    def test_overheard_speech_does_not(self, tmp_path):
        assert self._save_and_count(tmp_path, EVENT_TYPE) == 0, (
            "a stranger's wife, city and job must not become the "
            "operator's profile because a device was listening"
        )

    def test_the_excluded_types_match_the_writer(self):
        from memory.store import _NO_SELF_MODEL_EVENT_TYPES

        assert EVENT_TYPE in _NO_SELF_MODEL_EVENT_TYPES


# ── the summariser knows who the operator is ────────────────────────


class TestTheSummariserKnowsTheOperator:
    """agents/ambient_transcript.py referenced identity zero times, so
    the reduce pass decided which promises belong to "the user" without
    being told who that is. Chat has always known."""

    def test_user_md_is_read(self, feral_home):
        (feral_home / "USER.md").write_text("# Omar\nFounder of Theora.\n")
        assert "Omar" in load_operator_identity()

    def test_a_missing_user_md_is_not_an_error(self, feral_home):
        assert _operator_identity_block("") == ""

    def test_the_unfilled_template_counts_as_no_profile(self, feral_home):
        """IdentityWorkspace scaffolds USER.md on first construction, so
        on a fresh install the file EXISTS and says "Tell your agent
        about yourself here".

        Passing that under the heading WHO THE USER IS is worse than
        passing nothing: it asserts a wrong answer instead of leaving
        the question open. identity/workspace.py:207 applies the same
        test when building the system prompt.
        """
        from identity.workspace import DEFAULT_USER_MD, IdentityWorkspace

        IdentityWorkspace(home_dir=str(feral_home))  # scaffolds the file
        assert (feral_home / "USER.md").read_text().strip() == DEFAULT_USER_MD.strip()
        assert load_operator_identity() == ""
        assert _operator_identity_block(None) == ""

    def test_the_block_names_the_operator_and_disambiguates(self):
        block = _operator_identity_block("# Omar\nFounder of Theora.")
        assert "Omar" in block
        assert "THE USER means this person" in block
        assert "belongs in the summary" in block

    def test_the_block_is_bounded(self):
        """USER.md is operator-authored prose of unbounded length; this
        is one line of context in a summarization prompt."""
        block = _operator_identity_block("x" * 50_000)
        assert len(block) < 3_000

    def test_it_reaches_the_reduce_prompt(self, feral_home):
        (feral_home / "USER.md").write_text("# Omar\nFounder of Theora.\n")

        class _LLM:
            available = True

            def __init__(self):
                self.prompts = []

            async def chat(self, *, messages, **kwargs):
                self.prompts.append(messages[0]["content"])
                if "Schema:" in messages[0]["content"]:
                    body = json.dumps({
                        "summary": "s", "people": [], "topics": [],
                        "commitments": [],
                    })
                else:
                    body = "segment"
                return {"choices": [{"message": {"content": body}}]}

            def extract_response(self, response):
                return response["choices"][0]["message"]["content"], None

        from agents.ambient_transcript import summarize_transcript

        llm = _LLM()
        asyncio.run(summarize_transcript(
            "Noah said he would wire the funds. I said I would send the deck.",
            llm=llm,
        ))
        reduce_prompt = next(p for p in llm.prompts if "Schema:" in p)
        assert "WHO THE USER IS" in reduce_prompt
        assert "Omar" in reduce_prompt
