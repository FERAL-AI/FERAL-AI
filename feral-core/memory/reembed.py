"""Re-embed every stored vector after the embedding provider changes.

Why this module exists
----------------------
``feral memory reembed`` used to be four inline blocks in
``cli/memory_cmd.py`` that knew about exactly one table,
``memory_chunks``. That is how the knowledge graph died in production on
this machine: an earlier run of that command re-embedded 11,999 chunks to
the local 384-dim fastembed model and never touched ``entities``, whose
312 rows still held 6144-byte (1536-dim, OpenAI-era) blobs. Every call to
``KnowledgeGraph.search_entities`` embedded the query at 384 dims,
compared it against 1536-dim stored vectors, and raised
:class:`~memory.embeddings.EmbeddingDimensionMismatch`. Measured on a copy
of the real store: 5 of 5 entity queries raised, and because
``memory/context_builder.py`` had no handler, 5 of 5 ``search_all`` calls
raised too, taking out the episode, note and knowledge tiers that had all
worked.

So the migration is a module, not a CLI block, and it is driven by a
registry plus a *discovery* pass. The discovery pass is the part that
matters: a hard-coded list is exactly what shipped the bug, so
:func:`scan_store` walks ``sqlite_master`` for every column that could
hold a vector and reports any it does not know how to migrate, instead of
silently covering "the two tables somebody remembered".

What it deliberately does not do
--------------------------------
It never invents vectors. A row whose source text is gone cannot be
re-embedded, so its stale blob is set to NULL: every reader filters on
``embedding IS NOT NULL``, so a NULL costs that one row's semantic recall,
while leaving the stale blob in place keeps the whole table's vector leg
raising forever.

It also refuses to run against a degraded provider. ``EmbeddingProvider``
falls back to a deterministic *hash* embedding when the real backend is
rate-limited or missing. Writing those into the store would succeed, would
match the expected dimension, and would quietly replace real semantics
with noise that nothing downstream can detect. Refusing is the only
honest option.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from memory.embeddings import EmbeddingProvider, vec_to_blob

logger = logging.getLogger("feral.memory.reembed")


# ─────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmbeddingTable:
    """A table whose rows carry an embedding this migration can rewrite.

    ``text_column`` is the column the vector was originally produced from,
    so re-embedding reproduces what the writer would have written today:
    ``memory_chunks`` embeds ``text_content`` (``MemoryStore._index_chunk``)
    and ``entities`` embeds ``name`` (``KnowledgeGraph.add_entity``). If a
    writer ever changes what it embeds, it has to change here too, or the
    migration will silently write a vector for different text.
    """

    table: str
    id_column: str
    text_column: str
    embedding_column: str = "embedding"
    description: str = ""
    # vec0 mirror kept in sync with this table, if any. Dropped and
    # rebuilt when its baked-in dimension no longer matches the provider.
    vec0_table: str = ""
    vec0_key_column: str = ""
    # Maps the row id to the vec0 primary key. ``None`` means "use the row
    # id as-is" (vec_chunks keys on the chunk id TEXT); vec_entities keys
    # on a 63-bit hash of the entity id because vec0 demands an INTEGER.
    vec0_key_fn: Optional[Callable[[str], object]] = None


def _entity_vec_rowid(entity_id: str) -> int:
    # Same hash KnowledgeGraph._entity_rowid computes. Imported lazily
    # rather than duplicated so the two cannot drift: a different hash
    # here would rebuild the index under keys the KG never looks up.
    from memory.knowledge_graph import KnowledgeGraph

    return KnowledgeGraph._entity_rowid(entity_id)


VECTOR_TABLES: tuple[EmbeddingTable, ...] = (
    EmbeddingTable(
        table="memory_chunks",
        id_column="id",
        text_column="text_content",
        description="episode + note chunks (MemoryStore hybrid search)",
        vec0_table="vec_chunks",
        vec0_key_column="chunk_id",
    ),
    EmbeddingTable(
        table="entities",
        id_column="id",
        text_column="name",
        description="knowledge-graph entities (KnowledgeGraph.search_entities)",
        vec0_table="vec_entities",
        vec0_key_column="entity_rowid",
        vec0_key_fn=_entity_vec_rowid,
    ),
)

# Any column whose name looks like it holds a vector. Deliberately wider
# than the registry: the point of the discovery pass is to notice a table
# the registry has not been taught about yet.
_VECTOR_COLUMN_RE = re.compile(r"embedding|embed_|_embed|vector", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────
# Scanning
# ─────────────────────────────────────────────────────────────


@dataclass
class ColumnScan:
    """One embedding-bearing column, and whether its width is current."""

    table: str
    column: str
    rows: int
    vectors: int
    widths: dict[int, int] = field(default_factory=dict)  # dims -> row count
    stale: int = 0
    registered: bool = False
    error: str = ""

    @property
    def healthy(self) -> bool:
        return self.stale == 0 and not self.error


@dataclass
class Vec0Scan:
    """A vec0 virtual table and the dimension baked into its DDL."""

    table: str
    declared_dim: Optional[int]
    matches: bool
    mirrors: str = ""  # source table from the registry, when known


@dataclass
class StoreScan:
    target_dim: int
    columns: list[ColumnScan] = field(default_factory=list)
    vec0: list[Vec0Scan] = field(default_factory=list)

    @property
    def stale_total(self) -> int:
        return sum(c.stale for c in self.columns)

    @property
    def unregistered_stale(self) -> list[ColumnScan]:
        """Stale vectors in a table the migration does not know how to fix.

        Returned rather than ignored so the caller can fail loudly. An
        unmigratable table is the bug this module exists to prevent.
        """
        return [c for c in self.columns if c.stale and not c.registered]


def _virtual_table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
    ).fetchall()
    return {r[0] for r in rows}


def _is_shadow_table(name: str, virtuals: Iterable[str]) -> bool:
    """True for the private storage tables fts5/vec0 create per virtual table.

    They carry real BLOB columns (``vec_entities_vector_chunks00.vectors``)
    and would otherwise show up in the discovery pass as unmigratable
    vector storage. They are owned by their virtual table; the virtual
    table is what gets migrated.
    """
    if name in virtuals:
        return False
    return any(name.startswith(v + "_") for v in virtuals)


_VEC0_DIM_RE = re.compile(r"FLOAT\s*\[\s*(\d+)\s*\]", re.IGNORECASE)


def vec0_declared_dim(create_sql: Optional[str]) -> Optional[int]:
    """Dimension a vec0 table was CREATEd with, parsed from its DDL.

    sqlite-vec bakes the dimension into the column type and offers no
    pragma to read it back, so ``sqlite_master.sql`` is the only record.
    Same trick as ``embeddings.SQLiteVecIndex._declared_dim``; kept here
    too because this module must work without the extension loaded, and
    that class needs the extension to construct.
    """
    if not create_sql:
        return None
    match = _VEC0_DIM_RE.search(create_sql)
    return int(match.group(1)) if match else None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def scan_store(conn: sqlite3.Connection, target_dim: int) -> StoreScan:
    """Enumerate every vector-bearing column and report its widths.

    Discovery, not a hard-coded pair. Anything that looks like it holds a
    vector is measured; whether the registry can migrate it is a separate
    flag on the result, so an unknown table shows up as a problem instead
    of as nothing at all.
    """
    scan = StoreScan(target_dim=target_dim)
    registered = {(t.table, t.embedding_column): t for t in VECTOR_TABLES}
    virtuals = _virtual_table_names(conn)

    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    for table in tables:
        if table.startswith("sqlite_"):
            continue
        if table in virtuals:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            if "vec0" in (sql or "").lower():
                declared = vec0_declared_dim(sql)
                mirrors = next(
                    (t.table for t in VECTOR_TABLES if t.vec0_table == table), ""
                )
                scan.vec0.append(
                    Vec0Scan(
                        table=table,
                        declared_dim=declared,
                        matches=(declared == target_dim),
                        mirrors=mirrors,
                    )
                )
            continue
        if _is_shadow_table(table, virtuals):
            continue
        try:
            cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.debug("PRAGMA table_info(%s) failed: %s", table, exc)
            continue
        for col in cols:
            name = col[1]
            if not _VECTOR_COLUMN_RE.search(name):
                continue
            entry = ColumnScan(
                table=table,
                column=name,
                rows=0,
                vectors=0,
                registered=(table, name) in registered,
            )
            try:
                entry.rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                widths = conn.execute(
                    f'SELECT length("{name}") AS w, COUNT(*) FROM "{table}" '
                    f'WHERE "{name}" IS NOT NULL GROUP BY w'
                ).fetchall()
                for width, count in widths:
                    dims = int(width) // 4
                    entry.widths[dims] = entry.widths.get(dims, 0) + int(count)
                    entry.vectors += int(count)
                    if dims != target_dim:
                        entry.stale += int(count)
            except sqlite3.Error as exc:
                entry.error = str(exc)
            scan.columns.append(entry)
    return scan


# ─────────────────────────────────────────────────────────────
# Migration
# ─────────────────────────────────────────────────────────────


def _stale_rows(
    conn: sqlite3.Connection, spec: EmbeddingTable, target_dim: int
) -> list[sqlite3.Row]:
    return conn.execute(
        f'SELECT "{spec.id_column}" AS rid, "{spec.text_column}" AS txt '
        f'FROM "{spec.table}" '
        f'WHERE "{spec.embedding_column}" IS NOT NULL '
        f'  AND length("{spec.embedding_column}") != ?',
        (target_dim * 4,),
    ).fetchall()


async def _reembed_table(
    conn: sqlite3.Connection,
    spec: EmbeddingTable,
    embedder: EmbeddingProvider,
    *,
    batch_size: int,
    on_progress: Callable[[str], None],
) -> dict:
    target_dim = embedder.dimension
    rows = _stale_rows(conn, spec, target_dim)
    result = {"table": spec.table, "stale": len(rows), "updated": 0, "nulled": 0}
    if not rows:
        return result

    on_progress(f"    {spec.table}: {len(rows)} stale vector(s)")

    # Rows with no text left cannot be re-embedded. NULL beats leaving the
    # stale blob: readers filter on IS NOT NULL, so a NULL loses one row's
    # semantic recall while a stale blob keeps the whole table's vector
    # leg raising.
    empty = [r for r in rows if not (r["txt"] or "").strip()]
    if empty:
        conn.executemany(
            f'UPDATE "{spec.table}" SET "{spec.embedding_column}" = NULL '
            f'WHERE "{spec.id_column}" = ?',
            [(r["rid"],) for r in empty],
        )
        conn.commit()
        result["nulled"] = len(empty)
        on_progress(
            f"    {spec.table}: {len(empty)} row(s) had no source text left; "
            f"their stale vectors were cleared"
        )

    todo = [r for r in rows if (r["txt"] or "").strip()]
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        vecs = await embedder.embed_batch([r["txt"] for r in batch])
        # Commit per batch, like the original single-table implementation:
        # an interrupted run must leave a partly migrated store the next
        # run finishes, never a half-written row.
        conn.executemany(
            f'UPDATE "{spec.table}" SET "{spec.embedding_column}" = ? '
            f'WHERE "{spec.id_column}" = ?',
            [(vec_to_blob(v), r["rid"]) for r, v in zip(batch, vecs)],
        )
        conn.commit()
        result["updated"] += len(batch)
        on_progress(f"    {spec.table}: {result['updated']}/{len(todo)}")
    return result


def _rebuild_vec0_mirrors(
    conn: sqlite3.Connection,
    scan: StoreScan,
    embedder: EmbeddingProvider,
    *,
    on_progress: Callable[[str], None],
) -> list[dict]:
    """Drop and repopulate vec0 mirrors whose baked-in dimension is stale.

    A vec0 table's dimension is fixed at CREATE time and
    ``CREATE VIRTUAL TABLE IF NOT EXISTS`` is a silent no-op against an
    existing one, so a mirror created at 1536 stays at 1536 forever: every
    insert is rejected and every search returns nothing.
    ``embeddings.SQLiteVecIndex`` already tells operators to "drop the
    table so it is rebuilt at the current dimension"; this does it for
    them, and repopulates from the freshly re-embedded source column so
    the store is never left with a live-but-empty index (which reads as
    "nothing matched" rather than as a missing index).

    Requires the sqlite-vec extension. Without it every vec0 table is
    already inert (``KnowledgeGraph._vec_entities_available`` and
    ``SQLiteVecIndex._use_vec`` are both False), so there is nothing to
    repair and nothing is touched.
    """
    from memory.embeddings import _try_load_sqlite_vec, sqlite_vec_available

    actions: list[dict] = []
    stale = [v for v in scan.vec0 if v.declared_dim is not None and not v.matches]
    if not stale:
        return actions
    if not sqlite_vec_available() or not _try_load_sqlite_vec(conn):
        for v in stale:
            on_progress(
                f"    {v.table}: built at {v.declared_dim}d, provider is "
                f"{scan.target_dim}d, but sqlite-vec will not load in this "
                f"interpreter so the index is inert. Nothing to do."
            )
            actions.append({"table": v.table, "action": "skipped_no_extension"})
        return actions

    for v in stale:
        spec = next((t for t in VECTOR_TABLES if t.vec0_table == v.table), None)
        conn.execute(f'DROP TABLE IF EXISTS "{v.table}"')
        conn.commit()
        if spec is None:
            on_progress(
                f"    {v.table}: dropped (built at {v.declared_dim}d); it is not "
                f"in the registry, so the owning code must recreate it"
            )
            actions.append({"table": v.table, "action": "dropped"})
            continue
        conn.execute(
            f'CREATE VIRTUAL TABLE "{v.table}" USING vec0('
            f'  {spec.vec0_key_column} '
            f'{"INTEGER" if spec.vec0_key_fn else "TEXT"} PRIMARY KEY,'
            f"  embedding FLOAT[{scan.target_dim}])"
        )
        rows = conn.execute(
            f'SELECT "{spec.id_column}" AS rid, "{spec.embedding_column}" AS emb '
            f'FROM "{spec.table}" WHERE "{spec.embedding_column}" IS NOT NULL'
        ).fetchall()
        payload = [
            ((spec.vec0_key_fn(r["rid"]) if spec.vec0_key_fn else r["rid"]), r["emb"])
            for r in rows
        ]
        conn.executemany(
            f'INSERT OR REPLACE INTO "{v.table}"({spec.vec0_key_column}, embedding) '
            f"VALUES (?, ?)",
            payload,
        )
        conn.commit()
        on_progress(
            f"    {v.table}: rebuilt at {scan.target_dim}d with {len(payload)} vector(s) "
            f"(was {v.declared_dim}d)"
        )
        actions.append(
            {"table": v.table, "action": "rebuilt", "vectors": len(payload)}
        )
    return actions


def _run_sync(coro) -> None:
    """Drive a coroutine from sync code, loop or no loop.

    ``reembed_store`` is a sync function because the CLI is sync, but it
    is also called from tests and could be called from a running brain,
    and ``asyncio.run`` raises inside a live event loop. Falling back to a
    worker thread keeps the one entry point usable from both worlds
    instead of growing a second async copy that can drift.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    import threading

    box: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # re-raised on the caller's thread
            box["exc"] = exc

    thread = threading.Thread(target=_worker, name="feral-reembed")
    thread.start()
    thread.join()
    if "exc" in box:
        raise box["exc"]


class DegradedProviderError(RuntimeError):
    """Refusing to re-embed with a provider that is not producing real vectors."""


def reembed_store(
    db_path: str,
    *,
    embedder: Optional[EmbeddingProvider] = None,
    dry_run: bool = False,
    batch_size: int = 64,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Bring every stored vector to the active provider's dimension.

    Returns a report dict. ``ok`` is False when something vector-shaped
    was left stale, which is the case the CLI must exit non-zero on: a
    migration that "succeeded" while a table stayed broken is precisely
    the failure this module was written to end.
    """
    say = on_progress or (lambda _msg: None)
    embedder = embedder or EmbeddingProvider()
    target_dim = embedder.dimension

    # check_same_thread=False because _run_sync may execute the embedding
    # pass on a worker thread when the caller already has an event loop.
    # Safe here and only here: that thread is joined before this function
    # touches the connection again, so the handle is never shared
    # concurrently, which is the actual thing sqlite3 is guarding against.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        scan = scan_store(conn, target_dim)
        report: dict = {
            "provider": embedder.provider_name,
            "target_dim": target_dim,
            "dry_run": dry_run,
            "scan": scan,
            "tables": [],
            "vec0": [],
            "ok": True,
        }
        if dry_run or scan.stale_total == 0:
            report["ok"] = not scan.unregistered_stale
            return report

        # A degraded provider serves the deterministic hash fallback,
        # which has the right dimension and no semantics. Writing it would
        # look like a successful migration and silently replace the
        # store's meaning with noise.
        if embedder.degraded or embedder.provider_name in ("none", "hash"):
            raise DegradedProviderError(
                f"embedding provider is {embedder.active_provider!r}"
                + (f" ({embedder.degrade_reason})" if embedder.degrade_reason else "")
                + ". Re-embedding now would write hash-fallback vectors over "
                "real ones. Fix the provider (or wait out the cooldown) and "
                "re-run."
            )

        async def _run() -> None:
            for spec in VECTOR_TABLES:
                if not _table_exists(conn, spec.table):
                    continue
                report["tables"].append(
                    await _reembed_table(
                        conn, spec, embedder,
                        batch_size=batch_size, on_progress=say,
                    )
                )

        _run_sync(_run())
        report["vec0"] = _rebuild_vec0_mirrors(conn, scan, embedder, on_progress=say)

        after = scan_store(conn, target_dim)
        report["scan_after"] = after
        report["ok"] = after.stale_total == 0
        return report
    finally:
        conn.close()
