"""Audit 2026-08-12 — "FERAL remembers": the four tiers, end to end.

Each test here failed against the tree as it stood before the fix it
names, for the reason quoted from the real store at
``~/.feral/memory.db`` / ``~/.feral/sync_wal.db``.

Measured on that store:

* ``sync_wal`` held 16,184 operations. Every one was an ``insert``
  (episodes 14,807, relations 620, notes 386, entities 300, knowledge
  71) and every one carried ``synced_to = '[]'``.
* ``episodes`` held 12,300 rows of which 3,677 were forgotten, among
  them 133 ``user_command`` rows the user typed by hand.
* ``entities.embedding`` held 312 vectors at 1536 dims while the active
  provider was fastembed at 384.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
import types

import pytest

from memory.decay import DecayConfig, MemoryDecayService
from memory.store import MemoryStore
from memory.sync import SyncEngine, SyncWAL
from security.peer_roster import PeerRoster


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "remembers.db"))
    try:
        yield s
    finally:
        await s.drain_background_tasks()
        await s.aclose()


@pytest.fixture
async def synced_store(tmp_path):
    """A store with a real ``SyncEngine`` attached, WAL on disk."""
    s = MemoryStore(db_path=str(tmp_path / "remembers.db"))
    engine = SyncEngine("node-under-test", memory_store=s,
                        db_path=str(tmp_path / "sync_wal.db"))
    s._sync_engine = engine
    try:
        yield s, engine
    finally:
        await s.drain_background_tasks()
        await s.aclose()


def _wal_ops(engine: SyncEngine) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(engine._wal._db_path)
    try:
        return conn.execute(
            "SELECT table_name, op_type, row_id FROM sync_wal ORDER BY timestamp"
        ).fetchall()
    finally:
        conn.close()


# ── Tier 3 / CRDT: a deleted note must be announced ──────────────────
# Before the fix ``delete_note`` removed the row and logged nothing, so
# the note stayed readable on every peer forever.


async def test_note_delete_is_logged_to_the_sync_wal(synced_store):
    s, engine = synced_store
    note = await s.save("the ptarmigan prefers vermilion", tags=["audit"])
    assert ("notes", "insert", note["id"]) in _wal_ops(engine)

    assert await s.delete(note["id"]) is True

    assert ("notes", "delete", note["id"]) in _wal_ops(engine), (
        "deleting a note wrote no delete operation; the receiving side's "
        "op_type=='delete' branch is unreachable"
    )


async def test_note_delete_is_not_logged_when_the_row_was_not_there(synced_store):
    """A delete that changed nothing locally must not be announced."""
    s, engine = synced_store
    assert await s.delete("no-such-note") is False
    assert not [op for op in _wal_ops(engine) if op[1] == "delete"]


async def test_conversation_delete_is_logged_to_the_sync_wal(synced_store):
    s, engine = synced_store
    await s.conversation_save("conv-1", [{"role": "user", "content": "hi"}])
    await s.conversation_delete("conv-1")
    assert ("conversations", "delete", "conv-1") in _wal_ops(engine)


# ── Tier 2 / decay: a hard delete must be announced ──────────────────
# The sweep's hard delete is the one place FERAL destroys a memory. It
# logged nothing, so a peer holding the original insert re-sent it and
# the episode came back: get_changes_since selects purely on HLC and
# there were 14,807 episode inserts with nothing to counter them.


async def test_decay_hard_delete_is_logged_to_the_sync_wal(synced_store):
    s, engine = synced_store
    ep = await s.episode_save(
        session_id="audit", event_type="probe", summary="doomed episode",
    )
    long_ago = time.time() - (400 * 86400)
    conn = await s._conn()
    try:
        await conn.execute(
            "UPDATE episodes SET forgotten_at = ? WHERE id = ?", (long_ago, ep["id"]),
        )
        await conn.commit()
    finally:
        await s._release(conn)

    svc = MemoryDecayService(s, DecayConfig(retention_days=365.0))
    result = await svc.run_once()
    assert result["hard_deleted"] == 1

    assert ("episodes", "delete", ep["id"]) in _wal_ops(engine), (
        "the decay sweep destroyed an episode locally and announced "
        "nothing; a peer would resurrect it on the next handshake"
    )


async def test_decay_sweep_without_hard_deletes_logs_no_delete(synced_store):
    s, engine = synced_store
    await s.episode_save(session_id="audit", event_type="probe", summary="keeper")
    await MemoryDecayService(s, DecayConfig()).run_once()
    assert not [op for op in _wal_ops(engine) if op[1] == "delete"]


# ── Tier 2 / decay: forgotten must mean recoverable ──────────────────
# Every read path filters forgotten_at IS NULL and ``recall`` needs an
# id, so 3,677 episodes on the real store could be neither seen nor
# recalled.


async def test_forgotten_episodes_can_be_listed_with_their_ids(store):
    ep = await store.episode_save(
        session_id="audit", event_type="user_command",
        summary="remind me about the cerulean bicycle",
    )
    svc = MemoryDecayService(store, DecayConfig())
    await svc.forget(ep["id"])

    assert await store.episode_search("cerulean bicycle") == [], (
        "precondition: a forgotten episode is excluded from search"
    )

    listed = await svc.list_forgotten(limit=10)
    assert [row["id"] for row in listed] == [ep["id"]]
    assert listed[0]["summary"] == "remind me about the cerulean bicycle"

    # And the id it hands back is the one ``recall`` accepts.
    assert (await svc.recall(listed[0]["id"]))["ok"] is True
    assert [e["id"] for e in await store.episode_search("cerulean bicycle")] == [ep["id"]]


async def test_list_forgotten_filters_by_text_and_excludes_active(store):
    keep = await store.episode_save(
        session_id="audit", event_type="probe", summary="a quokka appears",
    )
    gone = await store.episode_save(
        session_id="audit", event_type="probe", summary="a ptarmigan appears",
    )
    svc = MemoryDecayService(store, DecayConfig())
    await svc.forget(gone["id"])

    assert [r["id"] for r in await svc.list_forgotten(query="ptarmigan")] == [gone["id"]]
    assert await svc.list_forgotten(query="quokka") == []
    assert keep["id"] not in [r["id"] for r in await svc.list_forgotten()]


# ── CRDT: per-peer delivery state was unwritable ─────────────────────
# SyncWAL.mark_synced had zero callers anywhere in the tree, so
# synced_to could only ever be '[]' — on 16,184 rows it was.


def test_mark_synced_many_records_every_op_in_one_pass(tmp_path):
    wal = SyncWAL(str(tmp_path / "wal.db"))
    engine = SyncEngine("node-a", db_path=str(tmp_path / "wal.db"))
    ids = [engine.log_operation("notes", "insert", f"n{i}", {"id": f"n{i}"})
           for i in range(3)]
    assert all(ids)

    op_ids = [r[0] for r in sqlite3.connect(str(tmp_path / "wal.db")).execute(
        "SELECT op_id FROM sync_wal"
    )]
    assert wal.mark_synced_many(op_ids, "peer-b") == 3
    # Idempotent: the same peer twice does not duplicate.
    assert wal.mark_synced_many(op_ids, "peer-b") == 0
    assert wal.mark_synced_many(op_ids, "peer-c") == 3

    rows = sqlite3.connect(str(tmp_path / "wal.db")).execute(
        "SELECT synced_to FROM sync_wal"
    ).fetchall()
    assert all(json.loads(r[0]) == ["peer-b", "peer-c"] for r in rows)


async def test_exchange_marks_the_ops_it_shipped_to_the_peer(tmp_path, monkeypatch):
    """A completed exchange must leave synced_to naming the peer."""
    wal_path = str(tmp_path / "wal.db")
    engine = SyncEngine("node-a", db_path=wal_path)
    # Replication is scoped and the default is ``private``, which
    # crosses to nobody, so an exchange that ships anything at all
    # needs a granted scope. The roster is per test: without the
    # explicit bind the engine falls back to the process global, which
    # is the operator's real ``~/.feral`` roster.
    roster = PeerRoster(db_path=str(tmp_path / "roster.db"))
    roster.grant_scope("peer-b", "audit-shared")
    engine.set_peer_roster(roster)
    engine.log_operation("notes", "insert", "n1", {"id": "n1"}, "audit-shared")

    class _FakeWS:
        def __init__(self):
            self._outbox = [
                json.dumps({
                    "type": "sync_ack", "vector_clock": {}, "node_id": "peer-b",
                }),
                json.dumps({"changes": []}),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, _payload):
            return None

        async def recv(self):
            return self._outbox.pop(0)

    fake_ws = _FakeWS()

    async def _connect(_uri, **_kwargs):
        return fake_ws

    monkeypatch.setitem(
        sys.modules, "websockets", types.SimpleNamespace(connect=_connect),
    )
    monkeypatch.setattr("memory.sync.build_client_ssl_context", lambda: None)

    result = await engine._handshake_and_exchange(
        "peer-b", {"address": "127.0.0.1", "port": 1},
        connect_timeout=1.0, handshake_timeout=1.0, attempt=1,
        started_at=time.time(),
    )
    assert result["success"] is True and result["sent"] == 1

    synced = [json.loads(r[0]) for r in sqlite3.connect(wal_path).execute(
        "SELECT synced_to FROM sync_wal"
    )]
    assert synced == [["peer-b"]], (
        "the exchange shipped an op and recorded no delivery; synced_to "
        "stayed '[]' exactly as it has on all 16,184 real rows"
    )


# ── Operator visibility: a stale vector column must show in `status` ──
# `feral memory status` said nothing while entities.embedding sat at
# 1536 dims against a 384-dim provider for months.


def test_memory_status_reports_a_stale_vector_column(tmp_path, monkeypatch, capsys):
    from cli import memory_cmd

    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, embedding BLOB)")
    # 1536 float32s, the OpenAI-era width, against a 384-dim provider.
    conn.execute(
        "INSERT INTO entities VALUES ('e1', ?)", (b"\x00" * (1536 * 4),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(memory_cmd, "feral_home", lambda: tmp_path)
    monkeypatch.setattr("config.loader.feral_data_home", lambda: tmp_path)

    class _Provider:
        provider_name = "fastembed"
        dimension = 384

    monkeypatch.setattr("memory.embeddings.EmbeddingProvider", _Provider)

    memory_cmd.cmd_memory("status", None)
    out = capsys.readouterr().out
    assert "STALE" in out, "status hid a stale vector column"
    assert "entities.embedding" in out
    assert "1536d x1" in out
    assert "feral memory reembed" in out


def test_memory_status_says_so_when_vectors_are_current(tmp_path, monkeypatch, capsys):
    from cli import memory_cmd

    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, embedding BLOB)")
    conn.execute("INSERT INTO entities VALUES ('e1', ?)", (b"\x00" * (384 * 4),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(memory_cmd, "feral_home", lambda: tmp_path)
    monkeypatch.setattr("config.loader.feral_data_home", lambda: tmp_path)

    class _Provider:
        provider_name = "fastembed"
        dimension = 384

    monkeypatch.setattr("memory.embeddings.EmbeddingProvider", _Provider)

    memory_cmd.cmd_memory("status", None)
    out = capsys.readouterr().out
    assert "STALE" not in out
    assert "OK, every column matches the provider" in out


# ── Operator visibility: forgotten episodes must be listable ─────────


def test_cli_forgotten_prints_ids_a_user_can_recall(tmp_path, monkeypatch, capsys):
    from cli import memory_cmd

    async def _seed() -> str:
        s = MemoryStore(db_path=str(tmp_path / "memory.db"))
        try:
            ep = await s.episode_save(
                session_id="audit", event_type="user_command",
                summary="book the flight to Reykjavik",
            )
            await MemoryDecayService(s, DecayConfig()).forget(ep["id"])
            return ep["id"]
        finally:
            await s.drain_background_tasks()
            await s.aclose()

    episode_id = asyncio.run(_seed())
    monkeypatch.setattr("config.loader.feral_data_home", lambda: tmp_path)

    memory_cmd.cmd_memory("forgotten", None)
    out = capsys.readouterr().out
    assert episode_id in out
    assert "book the flight to Reykjavik" in out
    assert "feral memory recall" in out
