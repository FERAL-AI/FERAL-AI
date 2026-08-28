"""Consolidation redesign acceptance (F1-F7).

Every test here asserts BEHAVIOUR of the consolidation pipeline, not
the shape of the code, and every one of them failed against the
pre-redesign implementation. The seven defects:

F1  The map stage summarised chunks with ``for chunk in chunks: await
    llm.chat(...)``. The chunks are independent by construction, so one
    compaction cost 2-10 strictly SEQUENTIAL generations.
F2  KG extraction during compaction ran over ``conversation_text[:3000]``.
    At a 20-turn threshold the graph saw roughly the first 3-6 messages
    of each window and every entity after that was invisible to the
    graph permanently.
F3  Per-message content was truncated to ``content[:500]`` before
    summarisation. Leading-N truncation is the worst available choice
    (arXiv:2210.16732): salience is anti-correlated with position near
    the cut, so the tail of every long tool result was thrown away.
F4  Chunking sliced the concatenated transcript at raw 6000-character
    offsets: mid-message, mid-word, mid-tool-result.
F5  There was no reduce step. ``"\\n\\n".join(summaries)[:16000]``
    silently dropped the TAIL chunk summaries on a long session. And
    the injected ``[Session Summary]`` system message was fed straight
    back into the NEXT compaction, so summaries got re-summarised
    (arXiv:2608.22752, "the compaction cliff": 53% retention after one
    round, 10% after five).
F6  The only trigger was a turn counter. Idle was never consulted, and
    a bare idle trigger would have STARVED a session that is never
    quiet, so the ladder is backlog-soft OR idle-debounce OR
    deadline-hard, whichever fires first.
F7  Provenance was ``time_range`` (always ``[0.0, 0.0]`` because chat
    history carries no ``meta.created_at``) plus ``source_turn_ids``,
    a list of POSITIONAL INDICES into a list the compaction then
    discarded, buried in an HTML comment that nothing read.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.orchestrator import Orchestrator  # noqa: E402
from memory import context_builder  # noqa: E402
from memory.context_builder import chunk_messages, compact_session, llm_summarize  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Doubles
# ─────────────────────────────────────────────────────────────────────


class RecordingLLM:
    """An LLM double that records every prompt and measures overlap.

    ``max_concurrent`` is the peak number of in-flight ``chat`` calls,
    which is the only honest way to tell a parallel map from a
    sequential one.
    """

    available = True

    def __init__(self, *, delay: float = 0.02, reply=None):
        self.prompts: list[str] = []
        self.delay = delay
        self._reply = reply or (lambda prompt: "summary")
        self.inflight = 0
        self.max_concurrent = 0

    async def chat(self, messages, tools=None):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        self.inflight += 1
        self.max_concurrent = max(self.max_concurrent, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            return {"text": self._reply(prompt)}
        finally:
            self.inflight -= 1

    @staticmethod
    def extract_response(response):
        return response["text"], None


def _messages(n: int, *, chars: int = 300, prefix: str = "msg") -> list[dict]:
    """n messages of a known, uniform size with a unique body each."""
    out = []
    for i in range(n):
        body = f"{prefix}-{i:03d}-" + ("abcdefghij" * ((chars // 10) + 1))
        out.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": body[:chars],
        })
    return out


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "consolidation.db"))
    try:
        yield s
    finally:
        await s.aclose()


# ─────────────────────────────────────────────────────────────────────
# F1: the map stage runs in parallel, under a bounded limit
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f1_map_stage_is_parallel_not_sequential():
    """A transcript that produces several chunks must not issue several
    strictly sequential generations."""
    msgs = _messages(60, chars=600)
    llm = RecordingLLM(delay=0.05)

    await llm_summarize(messages=msgs, llm=llm, max_chars=100_000)

    assert llm.max_concurrent > 1, (
        "the map stage issued its generations one at a time "
        f"(peak concurrency {llm.max_concurrent}); the chunks are "
        "independent and must be gathered"
    )


@pytest.mark.asyncio
async def test_f1_map_concurrency_is_bounded():
    """Bounded, because firing ten generations at a local model at once
    is how you evict its KV cache and make the whole thing slower."""
    msgs = _messages(200, chars=600)
    llm = RecordingLLM(delay=0.02)

    await llm_summarize(messages=msgs, llm=llm, max_chars=1_000_000)

    assert llm.max_concurrent <= context_builder.MAP_CONCURRENCY, (
        f"peak concurrency {llm.max_concurrent} exceeded the declared "
        f"bound {context_builder.MAP_CONCURRENCY}"
    )
    assert context_builder.MAP_CONCURRENCY <= 4, (
        "the bound must stay small enough for a single-slot local model"
    )


@pytest.mark.asyncio
async def test_f1_parallel_map_is_faster_than_sequential_would_be():
    """Wall clock, not structure: 4+ chunks at 0.1s each must not take
    the sequential 0.4s+."""
    msgs = _messages(80, chars=600)
    llm = RecordingLLM(delay=0.1)

    t0 = time.perf_counter()
    await llm_summarize(messages=msgs, llm=llm, max_chars=1_000_000)
    elapsed = time.perf_counter() - t0

    n_map_calls = max(1, len(chunk_messages(msgs)))
    sequential = n_map_calls * 0.1
    assert elapsed < sequential * 0.8, (
        f"{n_map_calls} chunks took {elapsed:.2f}s; a sequential map "
        f"would take {sequential:.2f}s and this is not meaningfully faster"
    )


# ─────────────────────────────────────────────────────────────────────
# F2: the knowledge graph sees the WHOLE window, not the first 3000 chars
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f2_entity_late_in_the_window_reaches_the_graph(store):
    """An entity mentioned after the first 3000 characters used to be
    invisible to the graph forever."""
    seen: list[str] = []

    async def fake_extract(text, llm=None, source=None):
        seen.append(text)
        return []

    kg = MagicMock()
    kg.extract_and_store = fake_extract
    store._kg = kg

    history = _messages(30, chars=500)
    # The entity lands well past character 3000 of the concatenation.
    history[24]["content"] = "The project codename is Zanzibar and it ships Friday."

    await compact_session(store, "f2-sess", history, llm=None, preserve_last_n=3)

    joined = "\n".join(seen)
    assert "Zanzibar" in joined, (
        "an entity 12000 characters into the window never reached "
        "extract_and_store; the [:3000] cap made it permanently "
        "invisible to the knowledge graph"
    )


@pytest.mark.asyncio
async def test_f2_extraction_runs_per_segment(store):
    """Per segment, not over one truncated blob."""
    seen: list[str] = []

    async def fake_extract(text, llm=None, source=None):
        seen.append(text)
        return []

    kg = MagicMock()
    kg.extract_and_store = fake_extract
    store._kg = kg

    history = _messages(60, chars=600)
    await compact_session(store, "f2-seg", history, llm=None, preserve_last_n=3)

    assert len(seen) > 1, (
        f"extraction ran {len(seen)} time(s) over the whole window; "
        "it must run per segment"
    )


# ─────────────────────────────────────────────────────────────────────
# F3: a long message's TAIL survives
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f3_long_message_tail_reaches_the_model():
    """Leading-N truncation loses ~80% of what a summary needs
    (arXiv:2210.16732), and salience is anti-correlated with position
    near the cut. The tail is the part that gets thrown away."""
    long_body = "HEADMARK " + ("filler words here. " * 400) + " TAILMARK"
    assert len(long_body) > 5000
    history = [
        {"role": "user", "content": "short opener"},
        {"role": "assistant", "content": long_body},
        *_messages(6, chars=100),
    ]
    llm = RecordingLLM()

    await llm_summarize(messages=history, llm=llm, max_chars=100_000)

    joined = "\n".join(llm.prompts)
    assert "HEADMARK" in joined
    assert "TAILMARK" in joined, (
        "the tail of a 7KB message never reached the summariser; "
        "content[:500] threw it away"
    )


@pytest.mark.asyncio
async def test_f3_no_500_char_per_message_cap():
    """A 2000-character message is not a pathological case and must
    survive whole."""
    body = "X" * 1200 + "ENDOFMESSAGE"
    history = [
        {"role": "user", "content": body},
        *_messages(4, chars=100),
    ]
    llm = RecordingLLM()

    await llm_summarize(messages=history, llm=llm, max_chars=100_000)

    joined = "\n".join(llm.prompts)
    assert body in joined, "a 1.2KB message was truncated before summarisation"


# ─────────────────────────────────────────────────────────────────────
# F4: chunks land on message boundaries
# ─────────────────────────────────────────────────────────────────────


def test_f4_chunks_never_split_a_message():
    """Raw character offsets cut mid-message, mid-word, mid-tool-result."""
    msgs = _messages(40, chars=400)  # ~16KB, several chunks
    chunks = chunk_messages(msgs)

    assert len(chunks) > 1, "test needs a multi-chunk transcript"
    joined = "\n".join(chunks)
    for m in msgs:
        body = m["content"]
        assert joined.count(body) == 1, (
            f"message {body[:20]!r} was split across chunks or duplicated"
        )
        # And it must be intact inside exactly one chunk.
        assert any(body in c for c in chunks), (
            f"message {body[:20]!r} does not appear intact in any chunk"
        )


def test_f4_every_chunk_line_starts_at_a_message_boundary():
    msgs = _messages(40, chars=400)
    for chunk in chunk_messages(msgs):
        first = chunk.split("\n", 1)[0]
        assert first.startswith("["), (
            f"chunk begins mid-message: {first[:60]!r}"
        )


@pytest.mark.asyncio
async def test_f4_prompts_contain_only_whole_messages():
    """The behaviour that matters: what the model is shown."""
    msgs = _messages(40, chars=400)
    llm = RecordingLLM()

    await llm_summarize(messages=msgs, llm=llm, max_chars=1_000_000)

    map_prompts = [p for p in llm.prompts if "conversation segment" in p]
    for m in msgs:
        hits = [p for p in map_prompts if m["content"] in p]
        assert len(hits) == 1, (
            f"message {m['content'][:20]!r} appeared whole in "
            f"{len(hits)} prompts; chunking cut it in half"
        )


# ─────────────────────────────────────────────────────────────────────
# F5: a real reduce, and no re-summarising of summaries
# ─────────────────────────────────────────────────────────────────────


def _segment_marker(text: str) -> str:
    """The id of the first message in a rendered segment."""
    for line in text.splitlines():
        if line.startswith("[") and "msg-" in line:
            return line.split("msg-", 1)[1][:3]
    return "?"


def _echoing_reply(prompt: str) -> str:
    """Map: name the segment and bulk it out past the budget.
    Reduce: echo every segment marker it was shown."""
    if "conversation segment" in prompt:
        return f"SEGMENT-{_segment_marker(prompt)} " + ("y" * 9000)
    import re
    return " ".join(re.findall(r"SEGMENT-\d+", prompt)) + " merged"


@pytest.mark.asyncio
async def test_f5_tail_chunk_summary_is_not_chopped():
    """``"\\n\\n".join(summaries)[:max_chars]`` dropped the last chunks
    outright. Every chunk must be represented in the result."""
    msgs = _messages(80, chars=600)
    llm = RecordingLLM(delay=0.0, reply=_echoing_reply)
    chunks = chunk_messages(msgs)
    assert len(chunks) >= 3, "test needs at least three chunks"

    result = await llm_summarize(messages=msgs, llm=llm, max_chars=16000)

    # The marker of the LAST chunk is the one head-truncation destroys.
    last_marker = _segment_marker(chunks[-1])
    assert last_marker != "?"
    assert f"SEGMENT-{last_marker}" in result, (
        "the tail chunk's summary was chopped off the end of the "
        "consolidated result"
    )
    assert len(result) <= 16000


@pytest.mark.asyncio
async def test_f5_tail_survives_even_when_the_reduce_fails():
    """The reduce is a model call and model calls fail. The fallback
    must still be lossless in the "every segment is represented" sense,
    which a head slice is not."""
    msgs = _messages(80, chars=600)

    def reply(prompt: str) -> str:
        if "conversation segment" not in prompt:
            raise RuntimeError("reduce generation unavailable")
        return f"SEGMENT-{_segment_marker(prompt)} " + ("y" * 9000)

    llm = RecordingLLM(delay=0.0, reply=reply)
    chunks = chunk_messages(msgs)
    result = await llm_summarize(messages=msgs, llm=llm, max_chars=16000)

    assert len(result) <= 16000
    for chunk in chunks:
        marker = _segment_marker(chunk)
        assert f"SEGMENT-{marker}" in result, (
            f"segment {marker} vanished from the consolidated result"
        )


@pytest.mark.asyncio
async def test_f5_reduce_step_exists():
    """A multi-chunk map that overflows the budget gets an actual reduce
    generation, not a slice."""
    msgs = _messages(80, chars=600)
    llm = RecordingLLM(delay=0.0, reply=lambda p: "z" * 9000)

    await llm_summarize(messages=msgs, llm=llm, max_chars=16000)

    reduce_prompts = [p for p in llm.prompts if "conversation segment" not in p]
    assert reduce_prompts, "no reduce generation was issued"


@pytest.mark.asyncio
async def test_f5_a_previous_summary_is_never_re_summarised(store):
    """The compaction cliff. A ``[Session Summary]`` system message the
    previous compaction injected must never be handed back to the
    summariser as raw material."""
    llm = RecordingLLM(delay=0.0, reply=lambda p: "first pass summary")

    history = _messages(30, chars=300)
    r1 = await compact_session(store, "f5-sess", history, llm=llm, preserve_last_n=3)
    assert r1["compacted"]

    # Second round: the summary message from round one is now in the
    # transcript, and more raw turns have arrived behind it.
    history2 = list(r1["history"]) + _messages(20, chars=300, prefix="later")
    llm.prompts.clear()
    r2 = await compact_session(store, "f5-sess", history2, llm=llm, preserve_last_n=3)
    assert r2["compacted"]

    for prompt in llm.prompts:
        assert "[Session Summary]" not in prompt, (
            "round two fed round one's summary back to the model; "
            "that is the compaction cliff (53% retention after one "
            "round, 10% after five)"
        )


@pytest.mark.asyncio
async def test_f5_consolidated_turns_are_watermarked(store):
    """The injected summary message declares what it consolidated, so
    re-derivation can always go back to the raw turns."""
    llm = RecordingLLM(delay=0.0, reply=lambda p: "a summary")
    history = _messages(30, chars=300)

    result = await compact_session(store, "f5-wm", history, llm=llm, preserve_last_n=3)

    summary_msg = result["history"][0]
    wm = summary_msg.get(context_builder.WATERMARK_KEY)
    assert isinstance(wm, dict), (
        "the injected summary message carries no watermark, so the next "
        "compaction cannot tell a summary from a raw turn"
    )
    assert wm.get("episode_id") == result["episode_id"]
    assert wm.get("turn_count") == len(history) - 3


@pytest.mark.asyncio
async def test_f5_summary_without_a_watermark_is_still_not_re_summarised(store):
    """Transcripts predating watermarks, and any path that round-trips
    messages through JSON and drops unknown keys, still must not feed a
    summary back to the summariser."""
    llm = RecordingLLM(delay=0.0, reply=lambda p: "fresh summary")
    history = [
        {"role": "system", "content": "[Session Summary]\nLEGACY SUMMARY TEXT"},
        *_messages(20, chars=300),
    ]

    await compact_session(store, "f5-legacy", history, llm=llm, preserve_last_n=3)

    for prompt in llm.prompts:
        assert "LEGACY SUMMARY TEXT" not in prompt


@pytest.mark.asyncio
async def test_f5_carried_summaries_are_rederived_from_raw_turns(store):
    """Carrying prior summaries forever is unbounded, so the oldest are
    collapsed. The collapse reads the RAW TURN ROWS via the watermark,
    which is the whole reason the watermark records episode ids."""
    raw_ids = []
    for i in range(6):
        text = f"ORIGINALTURN{i} the crane was repainted"
        ep = await store.episode_save(
            session_id="f5-rd", event_type="user_command",
            summary=text, detail=text,
        )
        raw_ids.append(ep["id"])

    def wm_message(n, ids):
        return {
            "role": "system",
            "content": f"[Session Summary]\nOLDSUMMARY{n}",
            context_builder.WATERMARK_KEY: {
                "episode_id": f"cmp{n}",
                "turn_count": len(ids),
                "source_episode_ids": ids,
                "derived_from": "raw_turns",
            },
        }

    history = [
        wm_message(0, raw_ids[:2]),
        wm_message(1, raw_ids[2:4]),
        wm_message(2, raw_ids[4:]),
        *_messages(20, chars=300),
    ]
    llm = RecordingLLM(delay=0.0, reply=lambda p: "merged summary")

    result = await compact_session(store, "f5-rd", history, llm=llm, preserve_last_n=3)

    blocks = [m for m in result["history"] if context_builder.is_consolidated(m)]
    assert len(blocks) <= context_builder.MAX_CARRIED_SUMMARIES, (
        f"{len(blocks)} summary blocks are riding along; carrying them "
        "forever is unbounded"
    )

    joined = "\n".join(llm.prompts)
    assert "ORIGINALTURN0" in joined, (
        "the collapse never went back to the raw turns; it can only have "
        "re-summarised a summary"
    )
    for n in range(3):
        assert f"OLDSUMMARY{n}" not in joined, (
            f"OLDSUMMARY{n} was fed back to the model"
        )


# ─────────────────────────────────────────────────────────────────────
# F6: backlog-soft OR idle-debounce OR deadline-hard
# ─────────────────────────────────────────────────────────────────────


def _orchestrator(compact_impl=None):
    """A bare orchestrator with just the fields the trigger touches."""
    async def _default(session_id, history, llm=None):
        return {"compacted": True, "history": [{"role": "system", "content": "[s]"}]}

    orch = Orchestrator.__new__(Orchestrator)
    orch.conversation_history = {}
    orch._turns_since_compaction = {}
    orch._compaction_inflight = {}
    orch._pending_since = {}
    orch._session_last_turn_at = {}
    orch._consolidation_task = None
    orch._consolidation_stop = None
    orch._session_locks = {}
    orch._background_tasks = set()
    orch._last_turn_at = 0.0
    orch.llm = MagicMock()
    orch.memory = MagicMock()
    orch.memory.compact_session = AsyncMock(side_effect=compact_impl or _default)
    orch._track_background_task = lambda t: orch._background_tasks.add(t)
    return orch


def _settings(**overrides):
    cfg = {"enabled": True, "turns_threshold": 20}
    cfg.update(overrides)
    return lambda: {"memory": {"compaction": cfg}}


@pytest.mark.asyncio
async def test_f6_backlog_threshold_still_fires(monkeypatch):
    """The existing config key keeps working; nobody's settings break."""
    import config.loader as loader
    monkeypatch.setattr(loader, "load_settings", _settings(turns_threshold=5))

    orch = _orchestrator()
    orch.conversation_history["s"] = _messages(20, chars=50)
    for _ in range(5):
        orch._maybe_auto_compact("s")
    await asyncio.gather(*list(orch._background_tasks))

    assert orch.memory.compact_session.await_count == 1


@pytest.mark.asyncio
async def test_f6_continuous_traffic_still_consolidates_by_deadline(monkeypatch):
    """THE starvation test. A session that is never idle, and whose
    backlog never reaches the soft bound, must still consolidate.

    Idle alone starves: this is the failure mode the W3C
    requestIdleCallback spec calls out normatively, and why RocksDB,
    Postgres autovacuum, Linux writeback and Go's GC all race a
    background trigger against a forced deadline.
    """
    import config.loader as loader
    monkeypatch.setattr(loader, "load_settings", _settings(
        turns_threshold=10_000,     # backlog bound unreachable
        idle_seconds=3600.0,        # idle never fires
        max_pending_seconds=0.05,   # hard deadline
        min_turns=2,
    ))

    orch = _orchestrator()
    orch.conversation_history["s"] = _messages(20, chars=50)

    # Continuous traffic: a turn every 10ms, never a quiet moment.
    for _ in range(12):
        orch._maybe_auto_compact("s")
        await asyncio.sleep(0.01)

    await asyncio.gather(*list(orch._background_tasks))
    assert orch.memory.compact_session.await_count >= 1, (
        "a session with unbroken traffic never consolidated; the idle "
        "trigger starved it and no deadline escalation fired"
    )


@pytest.mark.asyncio
async def test_f6_idle_session_consolidates_without_reaching_the_backlog(monkeypatch):
    """Consolidate when quiet, well before the backlog bound."""
    import config.loader as loader
    monkeypatch.setattr(loader, "load_settings", _settings(
        turns_threshold=10_000,
        idle_seconds=0.05,
        max_pending_seconds=3600.0,
        min_turns=2,
    ))

    orch = _orchestrator()
    orch.conversation_history["s"] = _messages(20, chars=50)
    for _ in range(3):
        orch._maybe_auto_compact("s")

    assert orch.memory.compact_session.await_count == 0, "fired too early"

    await asyncio.sleep(0.08)
    orch._consolidation_tick()
    await asyncio.gather(*list(orch._background_tasks))

    assert orch.memory.compact_session.await_count == 1, (
        "a quiet session with pending turns never consolidated"
    )


@pytest.mark.asyncio
async def test_f6_idle_tick_does_not_fire_on_a_busy_session(monkeypatch):
    """The debounce half of the ladder: a session that just spoke is not
    idle and must not be consolidated by the idle path."""
    import config.loader as loader
    monkeypatch.setattr(loader, "load_settings", _settings(
        turns_threshold=10_000,
        idle_seconds=3600.0,
        max_pending_seconds=3600.0,
        min_turns=2,
    ))

    orch = _orchestrator()
    orch.conversation_history["s"] = _messages(20, chars=50)
    for _ in range(3):
        orch._maybe_auto_compact("s")
    orch._consolidation_tick()
    await asyncio.gather(*list(orch._background_tasks))

    assert orch.memory.compact_session.await_count == 0


@pytest.mark.asyncio
async def test_f6_scheduler_loop_starts_and_stops(monkeypatch):
    """The background cadence exists and is stoppable, in the shape of
    MemoryDecayService._loop."""
    import config.loader as loader
    monkeypatch.setattr(loader, "load_settings", _settings(
        turns_threshold=10_000,
        idle_seconds=0.02,
        max_pending_seconds=3600.0,
        min_turns=2,
        scheduler_cadence_seconds=0.02,
    ))

    orch = _orchestrator()
    orch.conversation_history["s"] = _messages(20, chars=50)
    await orch.start_consolidation_scheduler()
    try:
        for _ in range(3):
            orch._maybe_auto_compact("s")
        for _ in range(40):
            if orch.memory.compact_session.await_count:
                break
            await asyncio.sleep(0.02)
    finally:
        await orch.stop_consolidation_scheduler()

    assert orch.memory.compact_session.await_count >= 1, (
        "the background consolidation loop never fired"
    )


@pytest.mark.asyncio
async def test_f6_a_ghost_session_does_not_retry_forever(monkeypatch):
    """The tick iterates the backlog dict, so a session whose transcript
    was evicted must stop being a candidate rather than being retried on
    every cadence for the life of the process."""
    import config.loader as loader
    monkeypatch.setattr(loader, "load_settings", _settings(
        turns_threshold=3, idle_seconds=3600.0, max_pending_seconds=3600.0,
    ))

    orch = _orchestrator()
    orch.conversation_history["s"] = _messages(20, chars=50)
    for _ in range(3):
        orch._maybe_auto_compact("s")

    # The transcript is evicted before the scheduled task runs.
    orch.conversation_history.pop("s")
    await asyncio.gather(*list(orch._background_tasks))

    assert "s" not in orch._turns_since_compaction, (
        "the evicted session is still a compaction candidate"
    )
    orch._background_tasks.clear()
    orch._consolidation_tick()
    assert not orch._background_tasks, "the ghost session was rescheduled"


# ─────────────────────────────────────────────────────────────────────
# F7: provenance that resolves to real rows
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f7_source_turn_id_resolves_to_a_real_row(store):
    """PR #224 made per-turn ``user_command`` / ``assistant_reply``
    episodes durable, so there are real row ids to point at. Positional
    indices into a discarded list are not provenance."""
    history = []
    saved = []
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        text = f"durable turn {i} about the harbour crane"
        ep = await store.episode_save(
            session_id="f7-sess",
            event_type="user_command" if role == "user" else "assistant_reply",
            summary=text[:200],
            detail=text,
        )
        saved.append(ep["id"])
        history.append({"role": role, "content": text})

    result = await compact_session(store, "f7-sess", history, llm=None, preserve_last_n=3)
    assert result["compacted"]

    rows = await store.compaction_sources(result["episode_id"])
    assert rows, "the compaction recorded no queryable provenance"

    resolved = {r["source_episode_id"] for r in rows}
    assert resolved & set(saved[:7]), (
        "no source turn id resolved to one of the durable turn episodes"
    )
    # And the join actually produced the source row, not a dangling id.
    first = rows[0]
    assert first.get("detail"), "provenance row does not resolve to a real episode"
    assert first.get("session_id") == "f7-sess"


@pytest.mark.asyncio
async def test_f7_time_range_is_real_when_turns_are_durable(store):
    """``time_range`` was always [0.0, 0.0] because ordinary chat
    history carries no ``meta.created_at``."""
    t0 = time.time() - 500
    history = []
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        text = f"timed turn {i}"
        await store.episode_save(
            session_id="f7-time",
            event_type="user_command" if role == "user" else "assistant_reply",
            summary=text, detail=text, created_at=t0 + i,
        )
        history.append({"role": role, "content": text})

    result = await compact_session(store, "f7-time", history, llm=None, preserve_last_n=3)
    tr = result["time_range"]

    assert tr != [0.0, 0.0] and tr != (0.0, 0.0), (
        "time_range is still the placeholder; nothing resolved the turns "
        "to their real timestamps"
    )
    assert abs(tr[0] - t0) < 1.0


@pytest.mark.asyncio
async def test_f7_provenance_is_queryable_by_source(store):
    """Both directions: given a turn, which consolidations covered it."""
    history = []
    ids = []
    for i in range(10):
        text = f"queryable turn {i}"
        ep = await store.episode_save(
            session_id="f7-rev", event_type="user_command",
            summary=text, detail=text,
        )
        ids.append(ep["id"])
        history.append({"role": "user", "content": text})

    result = await compact_session(store, "f7-rev", history, llm=None, preserve_last_n=3)
    covering = await store.consolidations_for_turn(ids[0])
    assert result["episode_id"] in {c["compaction_episode_id"] for c in covering}
