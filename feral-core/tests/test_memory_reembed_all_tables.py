"""`feral memory reembed` must cover EVERY table that stores a vector.

Why this exists
---------------
The command shipped as an inline block in ``cli/memory_cmd.py`` that knew
about one table, ``memory_chunks``. Run on this machine's real store it
migrated 11,999 chunks to the local 384-dim model and left the 312 rows of
``entities`` holding 6144-byte (1536-dim, OpenAI-era) blobs. From that
moment ``KnowledgeGraph.search_entities`` raised
``EmbeddingDimensionMismatch`` on every single call, and because
``memory/context_builder.py`` had no handler the exception escaped through
``MemoryStore.search_all`` into the ``memory.search`` RPC, the taskflow
``memory.search`` step and ``/internal/memory/search``.

So the pins here are:

  * the migration is driven by a registry, and it covers ``entities``;
  * it *discovers* vector-shaped columns rather than trusting the
    registry, so a table nobody remembered shows up as a problem instead
    of as nothing at all (a hard-coded list is what caused the bug);
  * fts5 / vec0 shadow tables are not mistaken for user data;
  * it refuses to run against a degraded provider, because the hash
    fallback produces vectors of the RIGHT width and no meaning, which
    would look like a successful migration;
  * entity search actually answers afterwards.

``memory.reembed`` is imported inside each test rather than at module
scope on purpose: against the unfixed source the module does not exist,
and a module-scope import would turn every test in this file into one
collection error instead of individual failures that name what is broken.
"""

from __future__ import annotations

import sqlite3
import time

import numpy as np
import pytest

from memory.embeddings import vec_to_blob
from memory.store import MemoryStore

DIM = 8          # the "current" provider
STALE_DIM = 24   # whatever wrote the store before it


class _NoOpVecIndex:
    backend_id = "noop"
    indexed = False

    async def search_cosine(self, query_vec, limit=20):
        return []

    def upsert(self, chunk_id, embedding):
        pass

    def count(self):
        return 0


class _StubEmbedder:
    """Deterministic ``DIM``-wide vectors, no model download, no network."""

    provider_name = "stub_local"
    provider_mode = "stub_local"
    active_provider = "stub_local"
    degrade_reason = None
    degraded = False
    available = True
    dimension = DIM

    def _vec_for(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.normal(size=DIM).astype(np.float32)
        return v / np.linalg.norm(v)

    async def embed(self, text: str) -> np.ndarray:
        return self._vec_for(text)

    def embed_sync(self, text: str):
        return self._vec_for(text)

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self._vec_for(t) for t in texts]


class _DegradedEmbedder(_StubEmbedder):
    provider_name = "hash"
    active_provider = "fallback:hash"
    degraded = True
    degrade_reason = "rate limited"


def _seed_stale_store(db_path: str) -> None:
    """A store shaped exactly like the reporter's: chunks migrated,
    entities left behind at the old provider's width."""
    conn = sqlite3.connect(db_path)
    now = time.time()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO memory_chunks "
            "(id, source_table, source_id, chunk_index, text_content, embedding, created_at) "
            "VALUES ('c1', 'episodes', 'e1', 0, 'a chunk about sailing', ?, ?)",
            (vec_to_blob(np.ones(DIM, dtype=np.float32)), now),
        )
        for eid, name in (
            ("ent1", "FERAL"),
            ("ent2", "Ada Lovelace"),
            ("ent3", "sqlite"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO entities "
                "(id, name, entity_type, embedding, metadata, mention_count, created_at, updated_at) "
                "VALUES (?, ?, 'thing', ?, '{}', 1, ?, ?)",
                (eid, name, vec_to_blob(np.ones(STALE_DIM, dtype=np.float32)), now, now),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def stale_store(tmp_path):
    db = str(tmp_path / "memory.db")
    store = MemoryStore(db_path=db, vec_index=_NoOpVecIndex())
    store._embedder = _StubEmbedder()
    if store._kg is not None:
        store._kg._embedder = _StubEmbedder()
    _seed_stale_store(db)
    yield store
    store.close()


def _widths(db_path: str, table: str) -> dict[int, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0] // 4: row[1]
            for row in conn.execute(
                f"SELECT length(embedding), COUNT(*) FROM {table} "
                f"WHERE embedding IS NOT NULL GROUP BY 1"
            )
        }
    finally:
        conn.close()


# ── the headline regression ───────────────────────────────────────────


def test_reembed_covers_entities_not_just_memory_chunks(stale_store, tmp_path, monkeypatch):
    """The exact defect: a migration that reports success while the
    knowledge graph stays unusable."""
    import memory.embeddings as emb_mod

    from cli.memory_cmd import cmd_memory

    db = str(tmp_path / "memory.db")
    # Patch the resolver rather than FERAL_HOME: conftest's env-leak
    # detector runs before monkeypatch unwinds setenv and reports it.
    monkeypatch.setattr("config.loader.feral_data_home", lambda: tmp_path)
    monkeypatch.setattr(emb_mod, "EmbeddingProvider", _StubEmbedder)
    try:
        import memory.reembed as reembed_mod

        monkeypatch.setattr(reembed_mod, "EmbeddingProvider", _StubEmbedder)
    except ImportError:
        pass  # unfixed source: the CLI still has to migrate entities

    assert _widths(db, "entities") == {STALE_DIM: 3}, "fixture must start stale"

    cmd_memory("reembed", None)

    assert _widths(db, "memory_chunks") == {DIM: 1}
    assert _widths(db, "entities") == {DIM: 3}, (
        "entities were left at the old provider's dimension, so every "
        "KnowledgeGraph.search_entities call still raises"
    )


@pytest.mark.asyncio
async def test_entity_search_answers_after_reembed(stale_store, tmp_path, monkeypatch):
    """End to end: the search that was dead returns rows afterwards."""
    import memory.embeddings as emb_mod

    from cli.memory_cmd import cmd_memory
    from memory.embeddings import EmbeddingDimensionMismatch

    # Patch the resolver rather than FERAL_HOME: conftest's env-leak
    # detector runs before monkeypatch unwinds setenv and reports it.
    monkeypatch.setattr("config.loader.feral_data_home", lambda: tmp_path)
    monkeypatch.setattr(emb_mod, "EmbeddingProvider", _StubEmbedder)
    try:
        import memory.reembed as reembed_mod

        monkeypatch.setattr(reembed_mod, "EmbeddingProvider", _StubEmbedder)
    except ImportError:
        pass

    # Before: the vector leg cannot run at all.
    with pytest.raises(EmbeddingDimensionMismatch):
        from memory.embeddings import cosine_similarity_bulk

        cosine_similarity_bulk(
            np.ones(DIM, dtype=np.float32),
            [vec_to_blob(np.ones(STALE_DIM, dtype=np.float32))],
        )

    cmd_memory("reembed", None)

    # The store cached nothing about entities, but the KG re-reads the
    # table on every call, so a fresh search is enough.
    hits = await stale_store._kg.search_entities("FERAL", limit=5)
    assert [h["name"] for h in hits], "entity search still returns nothing"
    assert stale_store._kg.vector_leg_error is None


# ── discovery: the registry is not trusted on its own ─────────────────


def test_scan_reports_every_vector_column_and_its_width(stale_store, tmp_path):
    from memory.reembed import scan_store

    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    conn.row_factory = sqlite3.Row
    try:
        scan = scan_store(conn, DIM)
    finally:
        conn.close()

    by_table = {c.table: c for c in scan.columns}
    assert by_table["entities"].widths == {STALE_DIM: 3}
    assert by_table["entities"].stale == 3
    assert by_table["entities"].registered is True
    assert by_table["memory_chunks"].widths == {DIM: 1}
    assert by_table["memory_chunks"].stale == 0
    assert scan.stale_total == 3


def test_scan_flags_a_vector_table_the_registry_never_heard_of(stale_store, tmp_path):
    """A hard-coded table list is what shipped the bug. An unknown table
    holding stale vectors must surface, not vanish."""
    from memory.reembed import scan_store

    db = str(tmp_path / "memory.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE plugin_notes (id TEXT PRIMARY KEY, embedding BLOB)")
        conn.execute(
            "INSERT INTO plugin_notes VALUES ('p1', ?)",
            (vec_to_blob(np.ones(STALE_DIM, dtype=np.float32)),),
        )
        conn.commit()
        scan = scan_store(conn, DIM)
    finally:
        conn.close()

    unknown = {c.table for c in scan.unregistered_stale}
    assert unknown == {"plugin_notes"}


def test_scan_does_not_mistake_fts_or_vec0_shadow_tables_for_data(stale_store, tmp_path):
    """fts5 and vec0 keep private tables with BLOB columns
    (``vec_entities_vector_chunks00.vectors``). They belong to their
    virtual table and must not be reported as unmigratable storage."""
    from memory.reembed import scan_store

    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    conn.row_factory = sqlite3.Row
    try:
        scan = scan_store(conn, DIM)
    finally:
        conn.close()

    assert {c.table for c in scan.columns} == {"entities", "memory_chunks"}


def test_vec0_declared_dimension_is_read_from_the_ddl():
    """sqlite-vec bakes the dimension into the column type and offers no
    pragma to read it back, so the DDL is the only record of it."""
    from memory.reembed import vec0_declared_dim

    assert vec0_declared_dim(
        "CREATE VIRTUAL TABLE vec_entities USING vec0("
        "entity_rowid INTEGER PRIMARY KEY, embedding FLOAT[1536])"
    ) == 1536
    assert vec0_declared_dim(None) is None
    assert vec0_declared_dim("CREATE TABLE t (x)") is None


# ── refusals ──────────────────────────────────────────────────────────


def test_reembed_refuses_a_degraded_provider(stale_store, tmp_path):
    """The hash fallback emits vectors of the right width and no meaning.
    Writing them would look like a successful migration and would replace
    the store's semantics with noise nothing downstream can detect."""
    from memory.reembed import DegradedProviderError, reembed_store

    with pytest.raises(DegradedProviderError) as excinfo:
        reembed_store(str(tmp_path / "memory.db"), embedder=_DegradedEmbedder())
    assert "hash" in str(excinfo.value)
    assert _widths(str(tmp_path / "memory.db"), "entities") == {STALE_DIM: 3}


def test_dry_run_writes_nothing(stale_store, tmp_path):
    from memory.reembed import reembed_store

    report = reembed_store(
        str(tmp_path / "memory.db"), embedder=_StubEmbedder(), dry_run=True
    )
    assert report["scan"].stale_total == 3
    assert _widths(str(tmp_path / "memory.db"), "entities") == {STALE_DIM: 3}


def test_rows_with_no_source_text_are_cleared_not_left_stale(stale_store, tmp_path):
    """A vector that cannot be rebuilt must not stay: readers filter on
    ``embedding IS NOT NULL``, so NULL costs one row's recall while a
    stale blob keeps the whole table's vector leg raising."""
    from memory.reembed import reembed_store

    db = str(tmp_path / "memory.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE entities SET name = '' WHERE id = 'ent3'")
        conn.commit()
    finally:
        conn.close()

    report = reembed_store(db, embedder=_StubEmbedder())
    entities = next(t for t in report["tables"] if t["table"] == "entities")
    assert entities["nulled"] == 1
    assert entities["updated"] == 2
    assert _widths(db, "entities") == {DIM: 2}
    assert report["ok"] is True


def test_report_is_not_ok_while_anything_stays_stale(stale_store, tmp_path):
    """`ok` is what the CLI exits non-zero on. A migration that reports
    success over a still-broken table is the failure being fixed."""
    from memory.reembed import reembed_store

    db = str(tmp_path / "memory.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE plugin_notes (id TEXT PRIMARY KEY, embedding BLOB)")
        conn.execute(
            "INSERT INTO plugin_notes VALUES ('p1', ?)",
            (vec_to_blob(np.ones(STALE_DIM, dtype=np.float32)),),
        )
        conn.commit()
    finally:
        conn.close()

    report = reembed_store(db, embedder=_StubEmbedder())
    assert report["ok"] is False
    assert _widths(db, "entities") == {DIM: 3}  # what it could fix, it fixed


def test_reembed_is_idempotent(stale_store, tmp_path):
    from memory.reembed import reembed_store

    db = str(tmp_path / "memory.db")
    first = reembed_store(db, embedder=_StubEmbedder())
    second = reembed_store(db, embedder=_StubEmbedder())
    assert first["ok"] and second["ok"]
    assert second["tables"] == []
    assert _widths(db, "entities") == {DIM: 3}
