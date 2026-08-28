"""Retrieval-correctness regressions across the memory tiers.

Every test here asserts on BEHAVIOUR (what a query returns, what a table
holds after a write), never on the shape of the code, because each of
these defects was originally found by measuring a real store and every
one of them was invisible from reading the call site.

The corpus is deliberately built from real English sentences and scored
with whatever embedding provider the environment actually has, because
the C1/C2 defects only exist in an anisotropic embedding space. A stub
embedder that spreads vectors over the sphere would make a raw cosine
floor look like it works.

Covered:

  C1  entity search thresholded a RAW cosine at 0.3, so a nonsense query
      returned five entities while every other tier correctly returned
      none.
  C2  the indexed vector path applied NO relevance floor at all when the
      corpus was too small to centre.
  C3  the entity FTS leg scored ``1/(1+abs(rank))``; FTS5 rank is BM25
      (negative, more negative is better) so ``abs()`` reversed it.
  C5  ``_heuristic_extract`` INSERTed relations without a dedup gate, so
      re-extracting the same sentence grew the relations table.
  C6  ``INSERT OR REPLACE INTO episodes`` orphaned rows in episodes_fts
      because SQLite does not fire delete triggers for REPLACE.
  C7  the compaction context builder read keys the extractor does not
      return, so ``key_entities`` was always empty.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.embeddings import vec_to_blob  # noqa: E402
from memory.store import _MIN_CHUNKS_FOR_CENTERING, MemoryStore  # noqa: E402

pytestmark = pytest.mark.asyncio

NONSENSE = "asdfgh zxcvbn qwerty"

#: Unrelated, ordinary sentences. Nothing here answers ``NONSENSE``, and
#: the wifi line is the one the original report saw leak out of the
#: entity tier.
FACTS = [
    "the wifi password is stored in 1password",
    "CuteBot lights turn on at sunset",
    "heart rate averaged 62 bpm last night",
    "the relay forwards messages between brains",
    "coffee grinder setting is 14 clicks",
    "the greenhouse thermostat holds 19 degrees",
    "recycling is collected on tuesday mornings",
    "the spare key lives under the third flowerpot",
]


def _corpus(n: int) -> list[str]:
    return [f"{FACTS[i % len(FACTS)]} (note {i})" for i in range(n)]


async def _drain(store: MemoryStore, timeout: float = 60.0) -> None:
    """Block until the embed queue has flushed every enqueued chunk."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if store._embed_queue.pending == 0:
            # The worker pops before it writes, so give the in-flight
            # item a beat to land in memory_chunks / the vec index.
            await asyncio.sleep(0.2)
            if store._embed_queue.pending == 0:
                return
        await asyncio.sleep(0.05)
    raise AssertionError("embed queue did not drain")


# ── C1: entity vector leg thresholded raw cosine ────────────────────


async def _kg_with_entities(tmp_path, count: int):
    """A graph with ``count`` entity rows, written straight to the table.

    ``add_entity`` links by embedding similarity above 0.85, which folds
    every "... (note i)" variant back onto its first sibling: calling it
    240 times here leaves 8 entities, below the centring minimum, and the
    test would then be exercising the small-graph fallback instead of the
    defect. The rows are written directly and pushed into the same vec0
    index ``add_entity`` would have used, so ``search_entities`` runs its
    normal indexed path over a graph of the size under test.
    """
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    kg = store.kg
    assert kg is not None

    names = _corpus(count)
    vectors = [await store._embedder.embed(n) for n in names]
    now = time.time()
    with sqlite3.connect(str(db_path)) as raw:
        raw.executemany(
            "INSERT INTO entities (id, name, entity_type, embedding, metadata, "
            "mention_count, created_at, updated_at) "
            "VALUES (?, ?, 'thing', ?, '{}', 1, ?, ?)",
            [
                (f"ent{i:05d}", name, vec_to_blob(vec), now, now)
                for i, (name, vec) in enumerate(zip(names, vectors))
            ],
        )
    for i, vec in enumerate(vectors):
        await kg._vec_upsert_entity(f"ent{i:05d}", vec)
    return store, kg


async def test_nonsense_query_returns_no_entities(tmp_path):
    """A query with no answer in the graph must return nothing.

    Raw cosine in an anisotropic space cannot express "nothing matches":
    on the reporter's store this query came back with five entities at
    0.40-0.45, including a note reading "the wifi password is stored in
    1password", while the episode, note and knowledge tiers all
    correctly returned zero.
    """
    store, kg = await _kg_with_entities(tmp_path, _MIN_CHUNKS_FOR_CENTERING + 40)
    hits = await kg.search_entities(NONSENSE, limit=10)
    assert hits == [], (
        f"nonsense query returned {len(hits)} entities: "
        f"{[(h['name'], round(h['score'], 3)) for h in hits]}"
    )


async def test_nonsense_query_leaks_nothing_through_search_all(tmp_path):
    """The entity tier feeds ``search_all`` and the LLM context builder,
    so the leak has to be closed at the tier the aggregator reads."""
    store, kg = await _kg_with_entities(tmp_path, _MIN_CHUNKS_FOR_CENTERING + 40)
    results = await store.search_all(NONSENSE, limit=10)
    assert results == [], (
        f"search_all returned {len(results)} rows for a nonsense query: "
        f"{[(r.get('type'), str(r.get('content'))[:50]) for r in results]}"
    )


async def test_real_entity_still_recalled(tmp_path):
    """The floor must not be a blanket reject: a real question whose
    answer IS in the graph still has to come back, otherwise the C1 fix
    is just a mute button."""
    store, kg = await _kg_with_entities(tmp_path, _MIN_CHUNKS_FOR_CENTERING + 40)
    hits = await kg.search_entities("where is the wifi password kept", limit=10)
    assert any("wifi password" in h["name"] for h in hits), (
        f"real query lost its answer; got {[h['name'] for h in hits]}"
    )


# ── C2: indexed path applied no floor below the centring minimum ────


async def _small_episode_store(tmp_path) -> MemoryStore:
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    store.start_background_tasks()
    for text in _corpus(30):
        await store.episode_save(
            session_id="s", event_type="chat", summary=text, detail="",
        )
    await _drain(store)
    return store


async def test_small_store_applies_a_relevance_floor(tmp_path):
    """Below ``_MIN_CHUNKS_FOR_CENTERING`` the indexed path returned
    ``{cid: 1.0}`` for every candidate, i.e. no floor at all, not even
    the raw one the numpy branch falls back to. Measured on a
    30-episode store, the nonsense query returned 5 hits at the maximum
    possible score. Every new install and every demo shipped that way.
    """
    store = await _small_episode_store(tmp_path)
    hits = await store.episode_search_hybrid(NONSENSE, limit=10)
    assert hits == [], (
        f"nonsense query returned {len(hits)} episodes on a 30-episode store: "
        f"{[(h['summary'], round(h.get('relevance_score', 0), 3)) for h in hits]}"
    )


async def test_small_store_still_recalls_a_real_episode(tmp_path):
    """Guard for the above: the small-store floor must not kill recall."""
    store = await _small_episode_store(tmp_path)
    hits = await store.episode_search_hybrid(
        "where is the wifi password kept", limit=10,
    )
    assert any("wifi password" in h["summary"] for h in hits), (
        f"real query lost its answer; got {[h['summary'] for h in hits]}"
    )


# ── C3: FTS5 rank sign folded away by abs() ─────────────────────────


async def test_entity_fts_ordering_is_best_first(tmp_path):
    """FTS5 ``rank`` is BM25: negative, and more negative is better.
    ``1/(1 + abs(rank))`` maps the BEST match to the SMALLEST score, so
    the ordering came back exactly reversed. Same defect store.py
    documents having already found and fixed for episodes.

    The vector leg is neutralised so the FTS leg alone decides the
    order; otherwise its 0.7 weight would mask the sign.
    """
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    kg = store.kg
    # Names sharing the query term with very different BM25 strength:
    # the shortest, most term-dense document is the best match. Written
    # straight through sqlite3 (the AFTER INSERT trigger still populates
    # entities_fts) so ``add_entity``'s embedding-similarity linking
    # cannot merge two of them and flatten the ranking.
    names = [
        "quasar",
        "quasar survey of the northern sky region",
        "annual report on funding for the deep field survey of the northern "
        "sky covering quasar candidates and many other astronomical objects",
    ]
    with sqlite3.connect(str(db_path)) as raw:
        for i, name in enumerate(names):
            raw.execute(
                "INSERT INTO entities (id, name, entity_type, metadata, "
                "mention_count, created_at, updated_at) "
                "VALUES (?, ?, 'thing', '{}', 1, ?, ?)",
                (f"ent{i}", name, time.time(), time.time()),
            )

    conn = await kg._conn()
    try:
        async with conn.execute(
            """SELECT e.name, rank FROM entities_fts f
               JOIN entities e ON f.rowid = e.rowid
               WHERE entities_fts MATCH 'quasar' ORDER BY rank""",
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await kg._release(conn)
    bm25_order = [r["name"] for r in rows]
    assert len(bm25_order) == 3, f"FTS did not match all three: {bm25_order}"

    async def _no_vectors(query_vec, limit):
        return {}

    kg._vec_search_candidates = _no_vectors

    hits = await kg.search_entities("quasar", limit=10)
    got = [h["name"] for h in hits]
    assert got == bm25_order, (
        f"entity FTS ordering is not BM25 order.\n  BM25: {bm25_order}\n  got:  {got}"
    )


# ── C5: heuristic extraction duplicated relations ───────────────────


async def test_heuristic_extract_does_not_duplicate_relations(tmp_path):
    """``add_relation`` dedups on (source, relation_type, target) and
    blends confidence; ``_heuristic_extract`` did a bare INSERT with a
    fresh uuid4, so the same sentence extracted three times produced
    three rows per predicate. Entities dedup fine, relations did not.
    The heuristic path runs whenever the LLM is unavailable OR its JSON
    fails to parse, so this grows linearly on a degraded local model."""
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    kg = store.kg
    text = "My name is Alice. I live in Berlin. I work at Acme."

    for _ in range(3):
        await kg._heuristic_extract(text)

    conn = await kg._conn()
    try:
        async with conn.execute(
            "SELECT relation_type, COUNT(*) AS n FROM relations "
            "GROUP BY source_id, relation_type, target_id HAVING n > 1"
        ) as cur:
            dupes = await cur.fetchall()
        async with conn.execute("SELECT COUNT(*) FROM relations") as cur:
            total = (await cur.fetchone())[0]
    finally:
        await kg._release(conn)

    assert not dupes, (
        "duplicate relation triples after 3 identical extractions: "
        f"{[(d['relation_type'], d['n']) for d in dupes]} (total rows={total})"
    )
    assert total == 3, f"expected 3 distinct relations, got {total}"


async def test_heuristic_extract_still_stores_relations(tmp_path):
    """Guard: dedup must not become "store nothing"."""
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    kg = store.kg
    results = await kg._heuristic_extract("My name is Alice. I live in Berlin.")
    preds = {r["relation"] for r in results}
    assert {"is_named", "lives_in"} <= preds, results
    neighborhood = await kg.get_entity_neighborhood("user")
    assert neighborhood.get("relations"), neighborhood


# ── C6: INSERT OR REPLACE orphaned episodes_fts rows ────────────────


async def test_replace_episode_leaves_no_orphan_fts_rows(tmp_path):
    """SQLite does not fire AFTER DELETE triggers for REPLACE-induced
    deletes unless ``recursive_triggers`` is on (it is off by default),
    so every sync re-delivery of an episode left the previous text in
    episodes_fts forever, including text the decay hard-delete path
    deliberately purges. Reads are correct today only because the
    search query inner-joins episodes_fts to episodes on rowid."""
    from memory.sync import SyncEngine, SyncOperation

    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    engine = SyncEngine(node_id="node-a", memory_store=store, db_path=str(db_path))

    for i in range(3):
        op = SyncOperation(
            op_id=f"op{i}",
            table="episodes",
            op_type="insert",
            row_id="ep-1",
            data={
                "id": "ep-1",
                "session_id": "s",
                "event_type": "chat",
                "summary": f"revision {i} of the superseded secret text",
                "detail": "",
                "importance": 0.5,
                "created_at": time.time(),
            },
            hlc=f"{1000 + i}:0:node-b",
            origin_node="node-b",
        )
        assert await engine._apply_to_memory(op), f"sync op {i} was rejected"

    conn = sqlite3.connect(str(db_path))
    try:
        ep_rows = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        fts_rows = conn.execute("SELECT COUNT(*) FROM episodes_fts").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM episodes_fts f "
            "WHERE NOT EXISTS (SELECT 1 FROM episodes e WHERE e.rowid = f.rowid)"
        ).fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM episodes_fts WHERE summary LIKE '%revision 0%'"
        ).fetchone()[0]
        current = conn.execute(
            "SELECT COUNT(*) FROM episodes_fts WHERE summary LIKE '%revision 2%'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert ep_rows == 1, f"expected 1 episode row, got {ep_rows}"
    assert orphans == 0, f"{orphans} orphaned episodes_fts rows after 3 replaces"
    assert fts_rows == 1, f"expected 1 fts row, got {fts_rows}"
    assert stale == 0, "superseded episode text is still in the FTS index"
    assert current == 1, "the surviving episode is not in the FTS index"


async def test_replaced_episode_is_still_searchable(tmp_path):
    """The other half: after the replace, the CURRENT text must be
    findable. A fix that drops the FTS row entirely would pass the
    orphan assertion and silently halve recall."""
    from memory.sync import SyncEngine, SyncOperation

    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    engine = SyncEngine(node_id="node-a", memory_store=store, db_path=str(db_path))

    for i, summary in enumerate(["kayak trip to the fjord", "sailing trip to the fjord"]):
        op = SyncOperation(
            op_id=f"op{i}", table="episodes", op_type="insert", row_id="ep-1",
            data={
                "id": "ep-1", "session_id": "s", "event_type": "chat",
                "summary": summary, "detail": "", "importance": 0.5,
                "created_at": time.time(),
            },
            hlc=f"{2000 + i}:0:node-b", origin_node="node-b",
        )
        assert await engine._apply_to_memory(op)

    hits = await store.episode_search_hybrid("sailing fjord", limit=10)
    assert [h["summary"] for h in hits] == ["sailing trip to the fjord"], hits
    assert not await store.episode_search_hybrid("kayak", limit=10), (
        "superseded text is still retrievable"
    )


async def test_episode_update_keeps_fts_in_step(tmp_path):
    """The trigger half of the C6 fix, exercised directly and on a
    connection the store did not open, so it pins the SCHEMA rather
    than any Python-side write path."""
    db_path = tmp_path / "memory.db"
    MemoryStore(db_path=str(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO episodes (id, session_id, event_type, summary, detail, created_at) "
            "VALUES ('e1','s','chat','original text','',?)",
            (time.time(),),
        )
        conn.execute("UPDATE episodes SET summary = 'replacement text' WHERE id = 'e1'")
        conn.commit()
        rows = conn.execute("SELECT summary FROM episodes_fts").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["replacement text"], rows


async def test_episode_fts_update_trigger_reaches_existing_databases(tmp_path):
    """A schema change is worthless if it only lands on fresh installs.
    Open a store, close it, reopen it, and the trigger must be there."""
    db_path = tmp_path / "memory.db"
    MemoryStore(db_path=str(db_path))
    # Second open == an "existing database" from the schema's point of view.
    MemoryStore(db_path=str(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'episodes'"
            )
        }
    finally:
        conn.close()
    assert any("update" in n.lower() or n.endswith("_au") for n in names), (
        f"no AFTER UPDATE trigger on episodes; found {sorted(names)}"
    )


async def test_existing_orphans_are_swept_on_open(tmp_path):
    """The trigger only stops NEW orphans. A store that ran the old sync
    path is already carrying old ones, and what they hold is text that was
    supposed to be gone, so boot has to clear them."""
    db_path = tmp_path / "memory.db"
    MemoryStore(db_path=str(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO episodes (id, session_id, event_type, summary, detail, created_at) "
            "VALUES ('e1','s','chat','live text','',?)",
            (time.time(),),
        )
        # Rowids no episode owns: exactly what INSERT OR REPLACE left behind.
        conn.execute(
            "INSERT INTO episodes_fts(rowid, summary, detail) "
            "VALUES (9001, 'purged secret text', '')"
        )
        conn.execute(
            "INSERT INTO episodes_fts(rowid, summary, detail) "
            "VALUES (9002, 'another purged secret', '')"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM episodes_fts").fetchone()[0] == 3
    finally:
        conn.close()

    MemoryStore(db_path=str(db_path))  # reopen == boot

    conn = sqlite3.connect(str(db_path))
    try:
        remaining = [r[0] for r in conn.execute("SELECT summary FROM episodes_fts")]
    finally:
        conn.close()
    assert remaining == ["live text"], remaining


# ── C7: key_entities read keys the extractor never returns ──────────


async def test_compaction_reports_key_entities(tmp_path):
    """``extract_and_store`` returns ``{id, source, relation, target,
    confidence}`` (LLM path) or ``{source, relation, target}``
    (heuristic path). The builder read ``name``/``entity``/``subject``,
    none of which exist, so ``key_entities`` was ALWAYS empty while
    being displayed by ``agents/orchestrator.py`` and
    ``cli/memory_cmd.py`` and documented as a returned field by
    ``api/routes/memory.py``."""
    from memory.context_builder import compact_session

    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    history = [
        {"role": "user", "content": "My name is Alice."},
        {"role": "assistant", "content": "Nice to meet you."},
        {"role": "user", "content": "I live in Berlin."},
        {"role": "assistant", "content": "Good city."},
        {"role": "user", "content": "I work at Acme."},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "Thanks."},
    ]
    result = await compact_session(
        store=store,
        session_id="s1",
        history=history,
        llm=None,
        preserve_last_n=2,
        promote_to_episode=False,
    )
    names = result.get("key_entities")
    assert names, f"key_entities is empty: {sorted(result)}"
    lowered = {n.lower() for n in names}
    assert "alice" in lowered, f"extracted entities missing from {names}"
    assert "user" in lowered, f"relation subject missing from {names}"


# ── C8: the same REPLACE orphan on notes + knowledge ────────────────
#
# C6 was fixed for ``episodes`` only. ``notes`` and ``knowledge`` already
# had their AFTER UPDATE triggers (``notes_fts_update`` /
# ``knowledge_fts_update``), but ``memory/sync.py`` still wrote them with
# ``INSERT OR REPLACE``, and REPLACE fires neither the delete trigger
# (recursive_triggers is off) nor the update trigger, so the orphan
# accrued on exactly the same mechanism.


async def _replay_note(engine, i: int, content: str) -> None:
    from memory.sync import SyncOperation

    op = SyncOperation(
        op_id=f"n{i}", table="notes", op_type="insert", row_id="note-1",
        data={
            "id": "note-1", "content": content, "tags": "[]",
            "importance": "normal", "source": "sync",
            "created_at": time.time(),
        },
        hlc=f"{1000 + i}:0:node-b", origin_node="node-b",
    )
    assert await engine._apply_to_memory(op), f"sync op {i} was rejected"


async def _replay_knowledge(engine, i: int, obj: str) -> None:
    from memory.sync import SyncOperation

    op = SyncOperation(
        op_id=f"k{i}", table="knowledge", op_type="insert", row_id="kn-1",
        data={
            "id": "kn-1", "subject": "alice", "predicate": "lives_in",
            "object": obj, "confidence": 1.0, "source": "sync",
            "created_at": time.time(),
        },
        hlc=f"{1000 + i}:0:node-b", origin_node="node-b",
    )
    assert await engine._apply_to_memory(op), f"sync op {i} was rejected"


async def test_replace_note_leaves_no_orphan_fts_rows(tmp_path):
    """Three sync re-deliveries of one note id must leave one note row
    and exactly one notes_fts row holding the CURRENT text."""
    from memory.sync import SyncEngine

    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    engine = SyncEngine(node_id="node-a", memory_store=store, db_path=str(db_path))

    for i in range(3):
        await _replay_note(engine, i, f"revision {i} of the superseded secret text")

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        fts_rows = conn.execute("SELECT COUNT(*) FROM notes_fts").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM notes_fts f "
            "WHERE NOT EXISTS (SELECT 1 FROM notes n WHERE n.rowid = f.rowid)"
        ).fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM notes_fts WHERE content LIKE '%revision 0%'"
        ).fetchone()[0]
        current = conn.execute(
            "SELECT COUNT(*) FROM notes_fts WHERE content LIKE '%revision 2%'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert rows == 1, f"expected 1 note row, got {rows}"
    assert orphans == 0, f"{orphans} orphaned notes_fts rows after 3 replaces"
    assert fts_rows == 1, f"expected 1 fts row, got {fts_rows}"
    assert stale == 0, "superseded note text is still in the FTS index"
    assert current == 1, "the surviving note is not in the FTS index"


async def test_replace_knowledge_leaves_no_orphan_fts_rows(tmp_path):
    """Same defect, knowledge tier."""
    from memory.sync import SyncEngine

    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    engine = SyncEngine(node_id="node-a", memory_store=store, db_path=str(db_path))

    for i, obj in enumerate(["berlin", "hamburg", "munich"]):
        await _replay_knowledge(engine, i, obj)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        fts_rows = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM knowledge_fts f "
            "WHERE NOT EXISTS (SELECT 1 FROM knowledge k WHERE k.rowid = f.rowid)"
        ).fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM knowledge_fts WHERE object = 'berlin'"
        ).fetchone()[0]
        current = conn.execute(
            "SELECT COUNT(*) FROM knowledge_fts WHERE object = 'munich'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert rows == 1, f"expected 1 knowledge row, got {rows}"
    assert orphans == 0, f"{orphans} orphaned knowledge_fts rows after 3 replaces"
    assert fts_rows == 1, f"expected 1 fts row, got {fts_rows}"
    assert stale == 0, "superseded knowledge text is still in the FTS index"
    assert current == 1, "the surviving fact is not in the FTS index"


async def test_replaced_note_and_fact_are_still_searchable(tmp_path):
    """The other half: a fix that just drops the FTS row would pass the
    orphan assertion and silently halve recall on both tiers."""
    from memory.sync import SyncEngine

    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path=str(db_path))
    engine = SyncEngine(node_id="node-a", memory_store=store, db_path=str(db_path))

    await _replay_note(engine, 0, "kayak trip to the fjord")
    await _replay_note(engine, 1, "sailing trip to the fjord")
    await _replay_knowledge(engine, 0, "berlin")
    await _replay_knowledge(engine, 1, "munich")

    conn = sqlite3.connect(str(db_path))
    try:
        note_hits = [
            r[0] for r in conn.execute(
                "SELECT n.content FROM notes_fts f JOIN notes n ON n.rowid = f.rowid "
                "WHERE notes_fts MATCH 'sailing'"
            )
        ]
        stale_note = conn.execute(
            "SELECT COUNT(*) FROM notes_fts f JOIN notes n ON n.rowid = f.rowid "
            "WHERE notes_fts MATCH 'kayak'"
        ).fetchone()[0]
        fact_hits = [
            r[0] for r in conn.execute(
                "SELECT k.object FROM knowledge_fts f "
                "JOIN knowledge k ON k.rowid = f.rowid "
                "WHERE knowledge_fts MATCH 'munich'"
            )
        ]
    finally:
        conn.close()

    assert note_hits == ["sailing trip to the fjord"], note_hits
    assert stale_note == 0, "superseded note text is still retrievable"
    assert fact_hits == ["munich"], fact_hits


async def test_existing_note_and_knowledge_orphans_are_swept_on_open(tmp_path):
    """The write-path fix only stops NEW orphans. Stores that already ran
    the old sync path carry text that was supposed to be gone."""
    db_path = tmp_path / "memory.db"
    MemoryStore(db_path=str(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO notes (id, content, tags, created_at, updated_at) "
            "VALUES ('n1','live note','[]',?,?)", (now, now),
        )
        conn.execute(
            "INSERT INTO knowledge (id, subject, predicate, object, created_at, updated_at) "
            "VALUES ('k1','alice','lives_in','live fact',?,?)", (now, now),
        )
        conn.execute(
            "INSERT INTO notes_fts(rowid, content, tags) "
            "VALUES (9001, 'purged secret note', '[]')"
        )
        conn.execute(
            "INSERT INTO knowledge_fts(rowid, subject, predicate, object) "
            "VALUES (9002, 'alice', 'lives_in', 'purged secret fact')"
        )
        conn.commit()
    finally:
        conn.close()

    MemoryStore(db_path=str(db_path))  # reopen == boot

    conn = sqlite3.connect(str(db_path))
    try:
        notes_left = [r[0] for r in conn.execute("SELECT content FROM notes_fts")]
        facts_left = [r[0] for r in conn.execute("SELECT object FROM knowledge_fts")]
    finally:
        conn.close()
    assert notes_left == ["live note"], notes_left
    assert facts_left == ["live fact"], facts_left
