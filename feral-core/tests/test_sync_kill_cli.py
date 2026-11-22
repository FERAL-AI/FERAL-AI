"""The chaos CLI must complete reconciliation before checking durable state."""
import asyncio
import importlib.util
import io
from pathlib import Path
import sqlite3

import pytest

from memory.store import MemoryStore
from memory.sync import SyncEngine

pytestmark = pytest.mark.chaos


@pytest.fixture
def chaos_cli():
    path = Path(__file__).resolve().parents[2] / "scripts/chaos/sync_kill.py"
    spec = importlib.util.spec_from_file_location("sync_kill_cli_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(tmp_path, node):
    store = MemoryStore(db_path=str(tmp_path / f"{node}_db.sqlite"))
    engine = SyncEngine(node_id=node, memory_store=store,
                        db_path=str(tmp_path / f"{node}_wal.sqlite"))
    engine.log_operation("notes", "insert", node,
                         {"id": node, "content": f"synthetic {node}", "tags": "[]"})
    store.close()
    return str(tmp_path / f"{node}_wal.sqlite")


def test_reconciliation_materializes_both_nodes_and_is_idempotent(tmp_path, monkeypatch, chaos_cli):
    monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
    wal_a, wal_b = _seed(tmp_path, "a"), _seed(tmp_path, "b")
    assert chaos_cli._final_state_hash(wal_a) != chaos_cli._final_state_hash(wal_b)

    asyncio.run(chaos_cli._final_sync(wal_a, wal_b, io.StringIO()))
    count_a, hash_a = chaos_cli._final_state_hash(wal_a)
    assert (count_a, hash_a) == chaos_cli._final_state_hash(wal_b)
    assert count_a == 2
    for node, peer in (("a", "b"), ("b", "a")):
        with sqlite3.connect(tmp_path / f"{node}_db.sqlite") as connection:
            assert connection.execute("SELECT id,content FROM notes").fetchall() == [
                (peer, f"synthetic {peer}")]

    asyncio.run(chaos_cli._final_sync(wal_a, wal_b, io.StringIO()))
    assert chaos_cli._final_state_hash(wal_a) == (2, hash_a)
    assert chaos_cli._final_state_hash(wal_b) == (2, hash_a)


def test_reconciliation_closes_async_memory_pools_on_failure(tmp_path, monkeypatch, chaos_cli):
    monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
    wal_a, wal_b = _seed(tmp_path, "a"), _seed(tmp_path, "b")
    closed = []
    original_close = MemoryStore.aclose

    async def close(store):
        closed.append(store.db_path)
        await original_close(store)

    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic apply failure")

    monkeypatch.setattr(MemoryStore, "aclose", close)
    monkeypatch.setattr(SyncEngine, "apply_remote_changes", fail)
    with pytest.raises(RuntimeError, match="synthetic apply failure"):
        asyncio.run(chaos_cli._final_sync(wal_a, wal_b, io.StringIO()))
    assert set(closed) == {str(tmp_path / "a_db.sqlite"), str(tmp_path / "b_db.sqlite")}
