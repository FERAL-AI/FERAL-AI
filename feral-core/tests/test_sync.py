"""
Tests for federated memory sync — HLC, WAL, and 2-node exchange.
"""
import os
import tempfile

from memory.hlc import HybridLogicalClock, HLCTimestamp
from memory.sync import SyncEngine, SyncWAL, SyncOperation, VectorClock


class TestHLC:
    def test_now_increments(self):
        hlc = HybridLogicalClock("node-a")
        t1 = hlc.now()
        t2 = hlc.now()
        assert t2.to_string() > t1.to_string()

    def test_receive_updates_clock(self):
        hlc_a = HybridLogicalClock("node-a")
        hlc_b = HybridLogicalClock("node-b")
        t_a = hlc_a.now()
        t_b = hlc_b.receive(t_a)
        assert t_b.to_string() >= t_a.to_string()

    def test_different_nodes_produce_different_timestamps(self):
        hlc_a = HybridLogicalClock("node-a")
        hlc_b = HybridLogicalClock("node-b")
        t_a = hlc_a.now()
        t_b = hlc_b.now()
        assert t_a.to_string() != t_b.to_string()

    def test_timestamp_string_roundtrip(self):
        hlc = HybridLogicalClock("test-node")
        ts = hlc.now()
        s = ts.to_string()
        assert "test-node" in s


class TestVectorClock:
    def test_empty_clock(self):
        vc = VectorClock()
        assert vc.to_dict() == {}

    def test_update(self):
        vc = VectorClock()
        vc.update("node-a", "1704067200000:0:node-a")
        d = vc.to_dict()
        assert "node-a" in d

    def test_later_hlc_wins(self):
        vc = VectorClock()
        vc.update("node-a", "1000:0:node-a")
        vc.update("node-a", "5000:0:node-a")
        assert vc.to_dict()["node-a"] == "5000:0:node-a"


class TestSyncWAL:
    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            wal = SyncWAL(db_path)
            op = SyncOperation(
                op_id="op-1", table="notes", op_type="upsert",
                row_id="r1", data={"key": "test", "value": "hello"},
                hlc="1704067200000:0:node-a", origin_node="node-a",
            )
            wal.append(op)
            ops = wal.get_changes_since("")
            assert len(ops) >= 1
            assert ops[0].op_id == "op-1"

    def test_get_changes_since_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            wal = SyncWAL(db_path)
            for i in range(5):
                wal.append(SyncOperation(
                    op_id=f"op-{i}", table="notes", op_type="upsert",
                    row_id=f"r{i}", data={"key": f"k{i}"},
                    hlc=f"{1000 + i}:0:node-a", origin_node="node-a",
                ))
            ops = wal.get_changes_since("1002:0:node-a")
            from memory.sync import _parse_hlc
            threshold = _parse_hlc("1002:0:node-a")
            assert all(_parse_hlc(op.hlc) > threshold for op in ops)


class TestSyncEngine:
    def test_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "memory.db")
            wal_path = os.path.join(tmpdir, "wal.db")
            from memory.store import MemoryStore
            store = MemoryStore(db_path=db_path)
            engine = SyncEngine(node_id="test-node", memory_store=store, db_path=wal_path)
            assert engine.node_id == "test-node"
            assert engine.stats["node_id"] == "test-node"

    def test_log_and_retrieve(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "memory.db")
            wal_path = os.path.join(tmpdir, "wal.db")
            from memory.store import MemoryStore
            store = MemoryStore(db_path=db_path)
            engine = SyncEngine(node_id="test-node", memory_store=store, db_path=wal_path)
            engine.log_operation("notes", "upsert", "r1", {"key": "fact1", "value": "test"})
            ops = engine.get_changes_since("")
            assert len(ops) >= 1

    async def test_two_engines_exchange(self):
        """Simulate 2-node sync — verify WAL exchange + materialization work.

        v2026.5.34 (PR 2 D12): apply_remote_changes is async, and the
        op_type contract is "insert" (the legacy "upsert" was never a
        real production value — log_operation only emits "insert" /
        "delete"). Exchanged ops now flow through HLC LWW and the
        async pool.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            from memory.store import MemoryStore
            db_a = os.path.join(tmpdir, "a.db")
            db_b = os.path.join(tmpdir, "b.db")
            wal_a = os.path.join(tmpdir, "wal_a.db")
            wal_b = os.path.join(tmpdir, "wal_b.db")
            store_a = MemoryStore(db_path=db_a)
            store_b = MemoryStore(db_path=db_b)
            try:
                engine_a = SyncEngine(node_id="node-a", memory_store=store_a, db_path=wal_a)
                engine_b = SyncEngine(node_id="node-b", memory_store=store_b, db_path=wal_b)

                engine_a.log_operation("notes", "insert", "a1", {"id": "a1", "content": "hello from A"})
                engine_a.log_operation("notes", "insert", "a2", {"id": "a2", "content": "second from A"})
                engine_b.log_operation("notes", "insert", "b1", {"id": "b1", "content": "hello from B"})

                ops_from_a = engine_a.get_changes_since("")
                ops_from_b = engine_b.get_changes_since("")

                assert len(ops_from_a) == 2
                assert len(ops_from_b) == 1

                applied_b = await engine_b.apply_remote_changes(ops_from_a)
                applied_a = await engine_a.apply_remote_changes(ops_from_b)

                assert applied_b == 2
                assert applied_a == 1

                all_b_ops = engine_b.get_changes_since("")
                origins = {op["origin_node"] for op in all_b_ops}
                assert "node-a" in origins
            finally:
                await store_a.aclose()
                await store_b.aclose()

    def test_export_import_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from memory.store import MemoryStore
            db = os.path.join(tmpdir, "test.db")
            wal = os.path.join(tmpdir, "wal.db")
            store = MemoryStore(db_path=db)
            engine = SyncEngine(node_id="bundle-node", memory_store=store, db_path=wal)
            engine.log_operation("notes", "upsert", "r1", {"key": "bundled", "value": "data"})
            bundle = engine.export_to_bundle()
            assert "node_id" in bundle
            assert "operations" in bundle
            assert len(bundle["operations"]) >= 1
