"""Memory of, and continuity across, external coding agent sessions.

Three things are proved here, and two of them need a real subprocess:

* **Compaction.** A turn's event stream is collapsed into something small
  enough to store and read back. The stream is not hypothetically large:
  a real opencode run produced 1026 events for one turn, so the digest
  tests build a stream of that size and assert on what survives, and in
  particular that a tool call whose outcome is unknown is never silently
  dropped.
* **Recall through the surfaces that already existed.** The record is an
  ordinary episode, so ``notes_memory__fused_timeline`` finds it without
  knowing anything about external agents. That test is the whole reason
  this feature did not get its own store, so it is pinned.
* **Continuity.** Two fake ACP agents are spawned as real subprocesses,
  one that advertises ``sessionCapabilities.resume`` and one that does
  not, and both are killed between turns. The resumable one is
  reattached; the other is restarted and the payload says so.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE))

from bridges import catalog  # noqa: E402
from bridges.continuity import SessionIndex, SessionRecord  # noqa: E402
from bridges.sessions import SessionRegistry  # noqa: E402
from memory import agent_activity  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from skills.impl.external_agent import ExternalAgentSkill  # noqa: E402
from skills.impl.timeline_fusion import timeline_fusion  # noqa: E402


# ----------------------------------------------------------------------
# A stand-in for bridges.acp.AcpEvent. Only attributes are read by the
# digest, deliberately, so this file does not need a live ACP session to
# exercise the compaction rules.
# ----------------------------------------------------------------------

class Event:
    def __init__(self, kind, *, text="", tool_call_id="", tool_name="",
                 title="", status="", raw=None):
        self.kind = kind
        self.text = text
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.title = title
        self.status = status
        self.raw = raw or {}


def a_real_sized_stream(workspace: str) -> list[Event]:
    """1026 events, the size one real opencode turn actually produced.

    Shaped like a real one too: mostly thought chunks, message chunks
    arriving one word at a time, and a handful of tool calls each of
    which reports its status three times.
    """
    events: list[Event] = []
    for index in range(500):
        events.append(Event("agent_thought_chunk", text=f"considering {index} "))
    for index in range(500):
        events.append(Event("agent_message_chunk", text=f"word{index} "))
    for index in range(8):
        call_id = f"tc-{index}"
        path = os.path.join(workspace, f"src/mod_{index}.py")
        events.append(Event(
            "tool_call", tool_call_id=call_id, tool_name="edit",
            title=f"Edit mod_{index}.py", status="pending",
            raw={"locations": [{"path": path}]},
        ))
        events.append(Event(
            "tool_call_update", tool_call_id=call_id, status="in_progress",
        ))
        events.append(Event(
            "tool_call_update", tool_call_id=call_id, status="completed",
        ))
    events.append(Event("plan", text="1. edit 2. test"))
    events.append(Event("some_future_variant_we_do_not_know"))
    return events


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    memory = MemoryStore(db_path=path)
    yield memory
    # Closed explicitly, or aiosqlite's worker thread outlives the loop
    # and prints a teardown traceback that has nothing to do with the test.
    memory.close()
    os.unlink(path)


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path / "repo")


class TestDigestCompaction:
    def test_a_thousand_events_collapse_to_a_readable_record(self, workspace):
        events = a_real_sized_stream(workspace)
        assert len(events) == 1026

        digest = agent_activity.digest_turn(
            agent_id="opencode",
            workspace_dir=workspace,
            session_handle="ext-1",
            events=events,
            prompt="refactor the module",
            status="completed",
            stop_reason="end_turn",
        )

        assert digest.counts["events"] == 1026
        # 500 thought chunks and 24 tool-call frames go in, 8 tool calls
        # come out. That collapse is the whole point.
        assert digest.counts["thoughts_dropped"] == 500
        assert digest.counts["tool_call_frames"] == 24
        assert len(digest.tool_calls) == 8
        assert all(tc.updates == 3 for tc in digest.tool_calls)
        assert all(tc.status == "completed" for tc in digest.tool_calls)

        # The stored body is bounded regardless of the input size.
        assert len(digest.detail()) <= agent_activity.detail_char_cap()

    def test_thoughts_are_dropped_but_counted_not_silently_lost(self, workspace):
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-1",
            events=[Event("agent_thought_chunk", text="secret reasoning")],
        )
        assert "secret reasoning" not in digest.detail()
        assert digest.counts["thoughts_dropped"] == 1

    def test_an_unfinished_tool_call_is_marked_never_dropped(self, workspace):
        """The lesson from qm's INTERRUPTED_TOOL_RESULT.

        "we do not know whether this happened" is a different fact from
        "this did not happen", and a summary that quietly omits the first
        reads as the second.
        """
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-1",
            events=[
                Event("tool_call", tool_call_id="tc-1", tool_name="bash",
                      title="rm -rf build", status="in_progress"),
            ],
            status="interrupted",
        )
        assert len(digest.tool_calls) == 1
        assert digest.tool_calls[0].interrupted is True
        assert agent_activity.INTERRUPTED_TOOL_CALL in digest.detail()
        assert "rm -rf build" in digest.detail()

    def test_an_overflowing_turn_keeps_the_unfinished_calls_first(self, workspace):
        events = []
        for index in range(agent_activity.MAX_TOOL_CALLS_KEPT + 10):
            events.append(Event("tool_call", tool_call_id=f"tc-{index}",
                                tool_name="read", status="completed"))
        events.append(Event("tool_call", tool_call_id="tc-unknown",
                            tool_name="bash", title="the risky one",
                            status="pending"))

        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-1", events=events,
        )
        assert len(digest.tool_calls) == agent_activity.MAX_TOOL_CALLS_KEPT
        assert digest.tool_calls[0].tool_call_id == "tc-unknown"

    def test_files_come_from_writes_locations_and_diffs_deduped(self, workspace):
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-1",
            written_paths=[os.path.join(workspace, "a.py")],
            events=[
                # Same file the client already wrote: must not appear twice.
                Event("tool_call", tool_call_id="tc-1", tool_name="edit",
                      status="completed",
                      raw={"locations": [{"path": os.path.join(workspace, "a.py")}]}),
                Event("tool_call", tool_call_id="tc-2", tool_name="edit",
                      status="completed",
                      raw={"content": [{"type": "diff",
                                        "path": os.path.join(workspace, "b.py")}]}),
                Event("tool_call", tool_call_id="tc-3", tool_name="read",
                      status="completed",
                      raw={"locations": [{"path": "/etc/hosts"}]}),
            ],
        )
        # Inside the workspace it is shown relative, outside it stays absolute.
        assert digest.files == ["a.py", "b.py", "/etc/hosts"]

    def test_a_refusal_survives_even_though_the_stream_never_mentions_it(
        self, workspace
    ):
        digest = agent_activity.digest_turn(
            agent_id="claude_code", workspace_dir=workspace,
            session_handle="ext-1", events=[],
            status="completed",
            permissions=[{"tool_name": "bash", "title": "rm -rf /",
                          "decision": "reject_once", "allowed": False}],
        )
        assert digest.refused is True
        assert "rm -rf /" in digest.detail()
        assert "a permission was refused" in digest.headline()

    def test_the_headline_reads_on_its_own_in_a_timeline(self, workspace):
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir="/home/u/projects/feral",
            session_handle="ext-1", status="completed",
            prompt="add a retry to the uploader",
            written_paths=["/home/u/projects/feral/up.py"],
            events=[],
        )
        headline = digest.headline()
        assert "opencode" in headline
        assert "feral" in headline
        assert "finished" in headline
        assert "1 file changed" in headline
        assert "add a retry to the uploader" in headline

    def test_a_long_message_keeps_its_head_and_its_tail(self, workspace):
        events = [Event("agent_message_chunk", text="START ")]
        events += [Event("agent_message_chunk", text="filler ") for _ in range(2000)]
        events.append(Event("agent_message_chunk", text=" END"))
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-1", events=events,
        )
        assert digest.text.startswith("START")
        assert digest.text.endswith("END")
        assert "[...]" in digest.text


class TestEpisodeStorage:
    async def test_a_turn_becomes_one_ordinary_episode(self, store, workspace):
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-abc", status="completed",
            prompt="fix the parser", events=[],
            written_paths=[os.path.join(workspace, "parser.py")],
        )
        saved = await agent_activity.record_turn(store, digest)
        assert saved and saved["id"]

        rows = await store.episode_recent(limit=5)
        assert len(rows) == 1
        row = rows[0]
        # No new columns, no new table: the existing episode fields are
        # used for what they already mean.
        assert row["event_type"] == agent_activity.EVENT_TYPE
        assert row["location"] == workspace
        assert row["participants"] == ["opencode"]
        assert row["session_id"] == "ext-abc"
        assert "parser.py" in row["detail"]

    async def test_recording_can_be_switched_off(self, store, workspace, monkeypatch):
        monkeypatch.setenv("FERAL_EXTERNAL_AGENT_MEMORY", "0")
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-abc", events=[],
        )
        assert await agent_activity.record_turn(store, digest) is None
        assert await store.episode_recent(limit=5) == []

    async def test_a_broken_store_never_fails_the_coding_turn(self, workspace):
        class Broken:
            async def episode_save(self, **_kwargs):
                raise RuntimeError("disk on fire")

        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-abc", events=[],
        )
        assert await agent_activity.record_turn(Broken(), digest) is None


class TestRecallAcrossAgents:
    async def _record(self, store, agent_id, workspace, prompt):
        digest = agent_activity.digest_turn(
            agent_id=agent_id, workspace_dir=workspace,
            session_handle=f"ext-{agent_id}", status="completed",
            prompt=prompt, events=[],
        )
        return await agent_activity.record_turn(store, digest)

    async def test_one_answer_covers_every_agent(self, store, tmp_path):
        repo_a, repo_b = str(tmp_path / "a"), str(tmp_path / "b")
        await self._record(store, "opencode", repo_a, "add the retry")
        await self._record(store, "claude_code", repo_a, "write the tests")
        await self._record(store, "codex", repo_b, "bump the deps")

        result = await agent_activity.recall(store)
        assert result["agents"] == ["claude_code", "codex", "opencode"]
        assert len(result["sessions"]) == 3
        assert sorted(result["by_agent"]) == ["claude_code", "codex", "opencode"]

    async def test_it_can_be_narrowed_to_one_repository(self, store, tmp_path):
        repo_a, repo_b = str(tmp_path / "a"), str(tmp_path / "b")
        await self._record(store, "opencode", repo_a, "add the retry")
        await self._record(store, "codex", repo_b, "bump the deps")

        result = await agent_activity.recall(store, workspace_dir=repo_a)
        assert [s["agent_id"] for s in result["sessions"]] == ["opencode"]

    async def test_a_window_excludes_older_turns(self, store, workspace):
        await self._record(store, "opencode", workspace, "yesterday's work")
        result = await agent_activity.recall(
            store, from_ts=time.time() + 3600, to_ts=time.time() + 7200
        )
        assert result["sessions"] == []

    async def test_other_episodes_are_not_swept_in(self, store, workspace):
        await store.episode_save(
            session_id="chat-1", event_type="conversation",
            summary="the user said hello",
        )
        await self._record(store, "opencode", workspace, "add the retry")
        result = await agent_activity.recall(store)
        assert len(result["sessions"]) == 1
        assert result["sessions"][0]["agent_id"] == "opencode"

    async def test_no_memory_store_degrades_rather_than_raising(self):
        result = await agent_activity.recall(None)
        assert result["degraded"] == "no_memory"
        assert result["sessions"] == []


class TestItRidesTheExistingMemorySurfaces:
    """The reason this feature has no store of its own.

    ``timeline_fusion`` predates external agents entirely and knows
    nothing about them. If an agent turn shows up in its card, then
    "what did the coding agent do in this repo yesterday" is answerable
    through the memory surface that was already there.
    """

    async def test_the_fused_timeline_shows_an_agent_turn(self, store, workspace):
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-abc", status="completed",
            prompt="add a retry to the uploader", events=[],
        )
        await agent_activity.record_turn(store, digest)

        fused = await timeline_fusion(
            query="what did the coding agent do",
            memory=store,
            from_ts=time.time() - 3600,
            to_ts=time.time() + 3600,
        )
        titles = [e["title"] for e in fused["entries"] if e["source"] == "episode"]
        assert any("opencode" in t and "uploader" in t for t in titles)

    async def test_hybrid_episode_search_finds_it_too(self, store, workspace):
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=workspace,
            session_handle="ext-abc", status="completed",
            prompt="rewrite the flaky websocket reconnect", events=[],
        )
        await agent_activity.record_turn(store, digest)
        hits = await store.episode_search("websocket", limit=5)
        assert any(h["event_type"] == agent_activity.EVENT_TYPE for h in hits)


class TestSessionIndex:
    def test_a_pointer_survives_the_process_that_made_it(self, tmp_path):
        index = SessionIndex(tmp_path / "sessions.json")
        index.remember(SessionRecord(
            handle="ext-1", agent_id="opencode", cwd="/repo",
            acp_session_id="sess-9", conversation_id="conv-1",
        ))
        # A second index object is a stand-in for a restarted process.
        reread = SessionIndex(tmp_path / "sessions.json")
        record = reread.get("ext-1")
        assert record is not None
        assert record.acp_session_id == "sess-9"

    def test_it_finds_the_last_session_for_an_agent_in_a_repo(self, tmp_path):
        index = SessionIndex(tmp_path / "sessions.json")
        index.remember(SessionRecord(
            handle="ext-old", agent_id="opencode", cwd="/repo",
            acp_session_id="s1", last_active=time.time() - 500,
        ))
        index.remember(SessionRecord(
            handle="ext-new", agent_id="opencode", cwd="/repo",
            acp_session_id="s2", last_active=time.time(),
        ))
        index.remember(SessionRecord(
            handle="ext-other", agent_id="codex", cwd="/repo",
            acp_session_id="s3", last_active=time.time(),
        ))
        assert index.find(agent_id="opencode", cwd="/repo").handle == "ext-new"
        assert index.find(agent_id="codex", cwd="/repo").handle == "ext-other"
        assert index.find(agent_id="opencode", cwd="/elsewhere") is None

    def test_a_conversation_id_wins_over_the_workspace_fallback(self, tmp_path):
        index = SessionIndex(tmp_path / "sessions.json")
        index.remember(SessionRecord(
            handle="ext-1", agent_id="opencode", cwd="/repo",
            acp_session_id="s1", conversation_id="conv-a",
        ))
        index.remember(SessionRecord(
            handle="ext-2", agent_id="opencode", cwd="/repo",
            acp_session_id="s2", conversation_id="conv-b",
        ))
        assert index.find(conversation_id="conv-a").handle == "ext-1"

    def test_stale_pointers_are_dropped_on_read(self, tmp_path):
        index = SessionIndex(tmp_path / "sessions.json", max_age=60)
        index.remember(SessionRecord(
            handle="ext-ancient", agent_id="opencode", cwd="/repo",
            acp_session_id="s1", last_active=time.time() - 3600,
        ))
        assert index.get("ext-ancient") is None

    def test_a_corrupt_index_costs_a_convenience_not_a_crash(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text("{not json at all")
        assert SessionIndex(path).list() == []

    def test_the_index_never_points_at_the_operators_real_home(
        self, tmp_path, monkeypatch
    ):
        """Safety pin: FERAL_HOME must be honoured, not bypassed."""
        from bridges import continuity

        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "fake-home"))
        monkeypatch.delenv("FERAL_EXTERNAL_AGENT_INDEX", raising=False)
        resolved = continuity.index_path()
        assert str(tmp_path / "fake-home") in str(resolved)
        assert ".feral" not in str(resolved).replace(str(tmp_path), "")


# ----------------------------------------------------------------------
# Real subprocesses. Both agents below are spawned for real and killed
# for real; nothing here is a mock of the protocol.
# ----------------------------------------------------------------------

AGENT_TEMPLATE = r'''
import json, os, sys, threading

OUT = sys.stdout
LOCK = threading.Lock()
RESUMABLE = __RESUMABLE__
MARKER = os.environ.get("ACP_MARKER", "")

def send(obj):
    with LOCK:
        OUT.write(json.dumps(obj) + "\n")
        OUT.flush()

def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})

def result(rid, payload):
    send({"jsonrpc": "2.0", "id": rid, "result": payload})

def mark(what, detail):
    if not MARKER:
        return
    with open(MARKER, "a") as fh:
        fh.write(json.dumps({"event": what, "detail": detail}) + "\n")

CAPS = {"loadSession": True, "sessionCapabilities": {"resume": {}, "list": {}}}
if not RESUMABLE:
    CAPS = {}

def run_turn(rid, params):
    sid = params.get("sessionId")
    prompt = ""
    for block in params.get("prompt") or []:
        prompt += block.get("text") or ""
    mark("prompt", prompt)
    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": "hmm "},
    }})
    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-1", "title": "Write notes.txt",
        "kind": "edit", "status": "pending",
        "locations": [{"path": "notes.txt"}],
    }})
    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-1", "status": "completed", "toolName": "write",
    }})
    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "done."},
    }})
    result(rid, {"stopReason": "end_turn"})

def handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        result(rid, {"protocolVersion": 1, "agentCapabilities": CAPS,
                     "agentInfo": {"name": "fake", "version": "0.1"}})
    elif method == "session/new":
        result(rid, {"sessionId": "sess-1"})
    elif method == "session/resume":
        mark("resume", params.get("sessionId"))
        result(rid, {"configOptions": []})
    elif method == "session/load":
        mark("load", params.get("sessionId"))
        result(rid, {"configOptions": []})
    elif method == "session/prompt":
        threading.Thread(target=run_turn, args=(rid, params), daemon=True).start()
    elif method == "session/cancel":
        pass
    elif method == "session/close":
        result(rid, {})
    elif method is not None and rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": "no such method"}})

for line in sys.stdin:
    line = line.strip()
    if line:
        handle(json.loads(line))
'''


@pytest.fixture
def agent_factory(tmp_path, monkeypatch):
    """Point the catalogue's ``opencode`` entry at a fake ACP agent."""
    marker = tmp_path / "marker.jsonl"

    def build(resumable: bool):
        script = tmp_path / f"agent_{resumable}.py"
        script.write_text(
            AGENT_TEMPLATE.replace("__RESUMABLE__", "True" if resumable else "False")
        )
        real_resolve = catalog.resolve

        def fake_resolve(agent_id, settings=None):
            if agent_id != "opencode":
                return real_resolve(agent_id, settings)
            return catalog.ResolvedAgent(
                spec=catalog.CATALOG["opencode"],
                available=True,
                binary_path=sys.executable,
                command=[sys.executable, "-u", str(script)],
            )

        monkeypatch.setattr(catalog, "resolve", fake_resolve)
        monkeypatch.setenv("ACP_MARKER", str(marker))
        # Spawned agents run in ``security.env_jail`` now, which drops
        # everything not on its allowlist, ``ACP_MARKER`` included. The
        # fake agent records what it was asked to do through that file,
        # so widen the jail the way an operator would rather than
        # disabling it and testing a path production does not take.
        monkeypatch.setenv("FERAL_ENV_JAIL_ALLOW", "ACP_MARKER")
        return marker

    return build


def marker_events(marker: Path) -> list[dict]:
    if not marker.exists():
        return []
    return [json.loads(line) for line in marker.read_text().splitlines() if line]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A skill with an isolated registry, index and memory store."""
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = MemoryStore(db_path=db)
    registry = SessionRegistry(index=SessionIndex(tmp_path / "sessions.json"))
    monkeypatch.setattr("skills.impl.external_agent._registry", lambda: registry)
    monkeypatch.setattr("skills.impl.external_agent._approval_manager", lambda: None)
    # ``raising=False`` so this fixture still builds against a build of
    # the skill that has no memory hook at all. That is what makes the
    # assertions below a real gate: without it, every test in this file
    # would error in setup rather than failing on the behaviour it pins.
    monkeypatch.setattr(
        "skills.impl.external_agent._memory", lambda: store, raising=False
    )
    yield ExternalAgentSkill(), registry, store
    store.close()
    os.unlink(db)


class TestTurnsAreRememberedForReal:
    async def test_a_completed_turn_lands_in_episodic_memory(
        self, wired, agent_factory, tmp_path
    ):
        skill, registry, store = wired
        agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        result = await skill.execute(
            "run_task",
            {"prompt": "write notes", "workspace_dir": str(repo),
             "wait_seconds": 30},
            {},
        )
        assert result["data"]["status"] == "completed"
        assert result["data"]["memory"]["recorded"] is True

        rows = await store.episode_recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["event_type"] == agent_activity.EVENT_TYPE
        # The thought chunk the agent emitted must not be in the record.
        assert "hmm" not in rows[0]["detail"]
        assert "Write notes.txt" in rows[0]["detail"]

        await skill.execute(
            "close_session",
            {"session_handle": result["data"]["session_handle"]}, {},
        )

    async def test_recall_activity_reports_it_back(
        self, wired, agent_factory, tmp_path
    ):
        skill, registry, store = wired
        agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await skill.execute(
            "run_task",
            {"prompt": "write notes", "workspace_dir": str(repo),
             "wait_seconds": 30},
            {},
        )
        recalled = await skill.execute(
            "recall_activity", {"window_label": "today"}, {}
        )
        assert recalled["success"] is True
        sessions = recalled["data"]["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["agent_id"] == "opencode"
        assert recalled["data"]["agents"] == ["opencode"]
        assert recalled["data"]["window"]["label"] == "today"

        await skill.execute(
            "close_session",
            {"session_handle": started["data"]["session_handle"]}, {},
        )

    async def test_the_event_list_in_the_tool_result_is_bounded(
        self, wired, agent_factory, tmp_path
    ):
        """A 1026-event turn must not be handed to the LLM verbatim."""
        from skills.impl import external_agent as module

        skill, registry, store = wired
        agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await skill.execute(
            "run_task",
            {"prompt": "write notes", "workspace_dir": str(repo),
             "wait_seconds": 30},
            {},
        )
        handle = started["data"]["session_handle"]
        managed = registry.get(handle)
        # Stuff the transcript, then re-render the payload.
        managed.turn_started_events = 0
        managed.session.transcript.extend(
            [managed.session.transcript[0]] * 1200
        )
        payload = skill._turn_payload(managed, "completed")
        assert len(payload["events"]) == module.MAX_EVENTS_RETURNED
        assert payload["events_total"] > module.MAX_EVENTS_RETURNED
        assert payload["events_omitted"] > 0

        await skill.execute("close_session", {"session_handle": handle}, {})

    async def test_closing_mid_turn_still_records_what_happened(
        self, wired, agent_factory, tmp_path
    ):
        skill, registry, store = wired
        agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await skill.execute(
            "run_task",
            {"prompt": "write notes", "workspace_dir": str(repo),
             "wait_seconds": 30},
            {},
        )
        handle = started["data"]["session_handle"]
        # Pretend the turn never finished, so close has to record it.
        managed = registry.get(handle)
        managed.turn_recorded = False

        closed = await skill.execute(
            "close_session", {"session_handle": handle, "cancel_first": True}, {}
        )
        assert closed["data"]["memory"]["recorded"] is True
        assert closed["data"]["memory"]["digest"]["status"] == "interrupted"


class TestContinuityAcrossADeadProcess:
    async def _first_turn(self, skill, repo):
        return await skill.execute(
            "run_task",
            {"prompt": "write notes", "workspace_dir": str(repo),
             "wait_seconds": 30},
            {},
        )

    async def test_a_resumable_agent_is_reattached_and_keeps_its_handle(
        self, wired, agent_factory, tmp_path
    ):
        skill, registry, store = wired
        marker = agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await self._first_turn(skill, repo)
        handle = started["data"]["session_handle"]
        assert started["data"]["continuity"]["reattached"] is False

        # Kill the subprocess the way a crash would.
        managed = registry.get(handle)
        managed.process.proc.kill()
        await managed.process.proc.wait()

        again = await skill.execute(
            "run_task",
            {"prompt": "and now the tests", "session_handle": handle,
             "wait_seconds": 30},
            {},
        )
        assert again["success"] is True
        # Same handle: an LLM holding it from an hour ago is not stranded.
        assert again["data"]["session_handle"] == handle
        continuity = again["data"]["continuity"]
        assert continuity["reattached"] is True
        assert continuity["mechanism"] == "resume"
        # And the agent really was asked to resume the prior session id.
        assert any(
            e["event"] == "resume" and e["detail"] == "sess-1"
            for e in marker_events(marker)
        )

        await skill.execute("close_session", {"session_handle": handle}, {})

    async def test_an_agent_that_cannot_resume_is_restarted_and_says_so(
        self, wired, agent_factory, tmp_path
    ):
        skill, registry, store = wired
        marker = agent_factory(resumable=False)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await self._first_turn(skill, repo)
        handle = started["data"]["session_handle"]
        managed = registry.get(handle)
        managed.process.proc.kill()
        await managed.process.proc.wait()

        again = await skill.execute(
            "run_task",
            {"prompt": "and now the tests", "session_handle": handle,
             "wait_seconds": 30},
            {},
        )
        continuity = again["data"]["continuity"]
        assert continuity["mechanism"] == "new"
        assert continuity["briefed_from_memory"] is True
        assert "restarted" in continuity["note"]
        # No resume was attempted against an agent that cannot do it.
        assert not any(e["event"] == "resume" for e in marker_events(marker))
        # The replacement was briefed with the previous turn's record.
        prompts = [e["detail"] for e in marker_events(marker) if e["event"] == "prompt"]
        assert any("recorded by FERAL" in p for p in prompts)
        assert any("Write notes.txt" in p for p in prompts)

        await skill.execute("close_session", {"session_handle": handle}, {})

    async def test_a_follow_up_without_a_handle_resumes_the_same_repo(
        self, wired, agent_factory, tmp_path
    ):
        skill, registry, store = wired
        agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await self._first_turn(skill, repo)
        handle = started["data"]["session_handle"]
        # Simulate a FERAL restart: the registry forgets, the index does not.
        await registry.close(handle, forget=False)

        again = await skill.execute(
            "run_task",
            {"prompt": "and now the tests", "workspace_dir": str(repo),
             "wait_seconds": 30},
            {},
        )
        assert again["data"]["session_handle"] == handle
        assert again["data"]["continuity"]["mechanism"] == "resume"

        await skill.execute("close_session", {"session_handle": handle}, {})

    async def test_fresh_session_opts_out_of_resuming(
        self, wired, agent_factory, tmp_path
    ):
        skill, registry, store = wired
        agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await self._first_turn(skill, repo)
        handle = started["data"]["session_handle"]
        await registry.close(handle, forget=False)

        again = await skill.execute(
            "run_task",
            {"prompt": "something unrelated", "workspace_dir": str(repo),
             "fresh_session": True, "wait_seconds": 30},
            {},
        )
        assert again["data"]["session_handle"] != handle
        assert again["data"]["continuity"]["reattached"] is False

        await skill.execute(
            "close_session",
            {"session_handle": again["data"]["session_handle"]}, {},
        )

    async def test_an_explicit_close_is_not_silently_resurrected(
        self, wired, agent_factory, tmp_path
    ):
        """The user said they were done, so a later task starts clean."""
        skill, registry, store = wired
        agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await self._first_turn(skill, repo)
        handle = started["data"]["session_handle"]
        await skill.execute("close_session", {"session_handle": handle}, {})

        again = await skill.execute(
            "run_task",
            {"prompt": "a new task", "workspace_dir": str(repo),
             "wait_seconds": 30},
            {},
        )
        assert again["data"]["session_handle"] != handle
        assert again["data"]["continuity"]["reattached"] is False

        await skill.execute(
            "close_session",
            {"session_handle": again["data"]["session_handle"]}, {},
        )

    async def test_a_handle_that_never_existed_is_still_a_404(self, wired):
        skill, registry, store = wired
        result = await skill.execute(
            "run_task", {"prompt": "go", "session_handle": "ext-invented"}, {}
        )
        assert result["success"] is False
        assert result["status_code"] == 404

    async def test_the_sweeper_leaves_a_resumable_pointer_behind(
        self, wired, agent_factory, tmp_path
    ):
        """An idle sweep is not the user saying they were done."""
        skill, registry, store = wired
        agent_factory(resumable=True)
        repo = tmp_path / "repo"
        repo.mkdir()

        started = await self._first_turn(skill, repo)
        handle = started["data"]["session_handle"]
        registry.idle_timeout = -1
        assert await registry.sweep() == [handle]
        assert registry.get(handle) is None
        assert registry.index.get(handle) is not None


# Both spellings are built rather than written out, because this file is
# itself in the list of sources that must not contain one. A literal here
# would make the gate fail on its own source, which is a false positive
# that would tempt the next person to weaken the check.
EM_DASH = chr(0x2014)
ESCAPED_EM_DASH = ("\\u%04x" % 0x2014).encode()


class TestNoEmDashes:
    """Checked as raw bytes, as an escape, and after a JSON round-trip."""

    TOUCHED = (
        "memory/agent_activity.py",
        "bridges/continuity.py",
        "bridges/acp.py",
        "bridges/sessions.py",
        "skills/impl/external_agent.py",
        "tests/test_external_agent_memory.py",
    )

    def test_the_sources_are_clean(self):
        for relative in self.TOUCHED:
            raw = (CORE / relative).read_bytes()
            assert EM_DASH.encode() not in raw, relative
            assert ESCAPED_EM_DASH not in raw, relative

    def test_the_check_can_actually_fail(self, tmp_path):
        """Without this, a bug in EM_DASH would make the gate a no-op."""
        decoy = tmp_path / "decoy.py"
        decoy.write_text(f"# a sentence {EM_DASH} with one in it\n")
        assert EM_DASH.encode() in decoy.read_bytes()
        escaped = tmp_path / "decoy.json"
        escaped.write_bytes(b'{"t": "a ' + ESCAPED_EM_DASH + b' b"}')
        assert ESCAPED_EM_DASH in escaped.read_bytes()
        # And the escape really does decode to the character we are banning.
        assert EM_DASH in json.loads(escaped.read_text())["t"]

    def test_the_manifest_is_clean_after_a_round_trip(self):
        path = CORE / "skills" / "manifests" / "external_agent.json"
        raw = path.read_bytes()
        assert EM_DASH.encode() not in raw
        assert ESCAPED_EM_DASH not in raw
        # An escaped one decodes back into the real character here.
        assert EM_DASH not in json.dumps(json.loads(raw.decode()))

    def test_a_stored_digest_carries_none_either(self, tmp_path):
        digest = agent_activity.digest_turn(
            agent_id="opencode", workspace_dir=str(tmp_path),
            session_handle="ext-1", status="completed",
            prompt="do the thing", events=[],
        )
        assert EM_DASH not in json.dumps(digest.to_dict())
        assert EM_DASH not in digest.detail()
