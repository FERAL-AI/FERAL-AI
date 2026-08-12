"""``knowledge_fts`` must index the live ``knowledge`` table, not the corpse.

Why this exists
---------------
Found on this machine's real store: ``knowledge`` 0 rows,
``knowledge__deprecated`` 29 rows, ``knowledge_fts`` 29 rows, and all
three FTS triggers bound to ``knowledge__deprecated``.

The mechanism is exact. ``migrate_knowledge_to_kg`` ends with
``ALTER TABLE knowledge RENAME TO knowledge__deprecated``; SQLite rewrites
every trigger that referenced the old name so it follows the renamed
table. ``_init_db`` then recreates an empty ``knowledge``, but its
``CREATE TRIGGER IF NOT EXISTS`` statements are no-ops because triggers
with those names already exist. The live table ends up with no FTS
triggers at all and the index keeps being maintained from the deprecated
rows.

That is not just a stale index. ``_knowledge_search_flat`` joins
``knowledge_fts.rowid`` to ``knowledge.rowid``, and a recreated table
restarts its rowids at 1, so the first row written on the legacy path
(``memory.kg.unified = false``, kept for chaos/recovery) inherits a
deprecated row's search terms: a query matching text the new row does not
contain returns it anyway. ``test_stale_index_returns_the_wrong_row`` pins
that specific wrong answer, because "stale index" undersells it.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest

from memory.store import MemoryStore


def _triggers(db_path: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' "
                "AND name IN ('knowledge_ai', 'knowledge_ad', 'knowledge_fts_update')"
            )
        }
    finally:
        conn.close()


def _rows(db_path: str, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def _make_broken_store(db_path: str) -> None:
    """Reproduce the on-disk state the F1 migration used to leave behind:
    the rename, its trigger rewrite, and the FTS rows that came with it."""
    store = MemoryStore(db_path=db_path)
    store.close()

    conn = sqlite3.connect(db_path)
    now = time.time()
    try:
        for i, (subject, predicate, obj) in enumerate(
            [
                ("user", "favourite_colour", "cerulean"),
                ("user", "employer", "FERAL"),
            ],
            start=1,
        ):
            conn.execute(
                "INSERT INTO knowledge (id, subject, predicate, object, confidence, "
                "source, created_at, updated_at, kg_migrated_at) "
                "VALUES (?, ?, ?, ?, 1.0, 'user', ?, ?, ?)",
                (f"k{i}", subject, predicate, obj, now, now, now),
            )
        # The rename SQLite performs, triggers and all.
        conn.execute("ALTER TABLE knowledge RENAME TO knowledge__deprecated")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def broken_db(tmp_path):
    db = str(tmp_path / f"knowledge-{uuid.uuid4().hex[:6]}.db")
    _make_broken_store(db)
    return db


def test_the_reproduction_is_faithful(broken_db):
    """Guard the fixture itself: if SQLite ever stops rewriting triggers
    on rename, the tests below would pass for the wrong reason."""
    assert _triggers(broken_db) == {
        "knowledge_ai": "knowledge__deprecated",
        "knowledge_ad": "knowledge__deprecated",
        "knowledge_fts_update": "knowledge__deprecated",
    }
    assert _rows(broken_db, "SELECT COUNT(*) FROM knowledge_fts")[0][0] == 2


def test_boot_rebinds_the_fts_triggers_to_the_live_table(broken_db):
    store = MemoryStore(db_path=broken_db)
    try:
        assert _triggers(broken_db) == {
            "knowledge_ai": "knowledge",
            "knowledge_ad": "knowledge",
            "knowledge_fts_update": "knowledge",
        }
    finally:
        store.close()


def test_boot_rebuilds_the_index_from_the_live_table(broken_db):
    store = MemoryStore(db_path=broken_db)
    try:
        fts = _rows(broken_db, "SELECT COUNT(*) FROM knowledge_fts")[0][0]
        live = _rows(broken_db, "SELECT COUNT(*) FROM knowledge")[0][0]
        assert fts == live == 0
        # The deprecated rows are still on disk; nothing was destroyed.
        assert _rows(broken_db, "SELECT COUNT(*) FROM knowledge__deprecated")[0][0] == 2
    finally:
        store.close()


def test_repair_is_idempotent(broken_db):
    MemoryStore(db_path=broken_db).close()
    conn = sqlite3.connect(broken_db)
    try:
        conn.execute(
            "INSERT INTO knowledge (id, subject, predicate, object, confidence, "
            "source, created_at, updated_at) VALUES "
            "('k9', 'user', 'city', 'Cairo', 1.0, 'user', 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()
    MemoryStore(db_path=broken_db).close()
    assert _rows(broken_db, "SELECT COUNT(*) FROM knowledge_fts")[0][0] == 1
    assert _triggers(broken_db)["knowledge_ai"] == "knowledge"


@pytest.mark.asyncio
async def test_new_rows_are_searchable_on_the_legacy_path(broken_db, monkeypatch):
    """The user-visible symptom: with the triggers on the corpse, a row
    written to the live table was never indexed, so FTS could not find it."""
    store = MemoryStore(db_path=broken_db)
    monkeypatch.setattr(store, "_kg_unified_enabled", lambda: False)
    try:
        await store.knowledge_store("user", "city", "Cairo")
        hits = await store.knowledge_search("Cairo", limit=5)
        assert [h["object"] for h in hits] == ["Cairo"]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_stale_index_returns_the_wrong_row(broken_db, monkeypatch):
    """The sharp edge: a recreated table restarts rowids at 1, so a stale
    index entry pointed at deprecated row 1 resolves to the FIRST NEW ROW.
    A query for the deprecated row's text must not return it."""
    store = MemoryStore(db_path=broken_db)
    monkeypatch.setattr(store, "_kg_unified_enabled", lambda: False)
    try:
        await store.knowledge_store("user", "city", "Cairo")
        # 'cerulean' only ever existed in the deprecated table.
        hits = await store.knowledge_search("cerulean", limit=5)
        assert hits == [], (
            "the FTS index answered from the deprecated table and returned "
            f"an unrelated live row: {hits}"
        )
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_migration_no_longer_leaves_the_index_on_the_corpse(tmp_path):
    """Fix the cause, not only the symptom: a fresh migration must not
    recreate the state the repair exists to clean up."""
    db = str(tmp_path / "fresh.db")
    store = MemoryStore(db_path=db)
    try:
        # Write straight to the flat table so the migration has a row to
        # port, then let it run with its default (unified) settings.
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO knowledge (id, subject, predicate, object, confidence, "
                "source, created_at, updated_at) VALUES "
                "('k1', 'user', 'employer', 'FERAL', 1.0, 'user', 0, 0)"
            )
            conn.commit()
        finally:
            conn.close()

        result = await store.migrate_knowledge_to_kg()
        assert result["deprecated"] is True
    finally:
        await store.aclose()

    # No trigger may still be bound to the deprecated table, and the index
    # must not be left holding its rows.
    leftovers = {
        name: tbl for name, tbl in _triggers(db).items() if tbl != "knowledge"
    }
    assert leftovers == {}, f"triggers followed the table into deprecation: {leftovers}"
    assert _rows(db, "SELECT COUNT(*) FROM knowledge_fts")[0][0] == 0
