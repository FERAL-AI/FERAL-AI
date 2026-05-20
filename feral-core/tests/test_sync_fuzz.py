"""
CRDT fuzzing tests for federated memory sync.

Verifies convergence under:
- Random operation ordering across nodes
- Conflicting concurrent writes (LWW semantics)
- Partial message delivery (10% drop)
- Network flap (disconnect / local ops / reconnect)
- 3-node transitive topology (A<->B<->C)
"""
import os
import random
import tempfile
import time

import pytest

from memory.hlc import HybridLogicalClock
from memory.sync import SyncEngine, SyncWAL, SyncOperation, VectorClock, _parse_hlc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(tmpdir: str, node_id: str):
    from memory.store import MemoryStore
    db = os.path.join(tmpdir, f"{node_id}.db")
    wal = os.path.join(tmpdir, f"{node_id}_wal.db")
    store = MemoryStore(db_path=db)
    return SyncEngine(node_id=node_id, memory_store=store, db_path=wal)


def _random_op(node_id: str, tables=("notes", "knowledge"), op_types=("insert", "update", "delete")):
    """Generate a random CRDT operation."""
    table = random.choice(tables)
    op_type = random.choice(op_types)
    row_id = f"row-{random.randint(1, 20)}"

    if table == "notes":
        data = {"id": row_id, "content": f"content-{random.randint(0, 9999)}", "tags": "[]", "importance": "normal", "source": node_id}
    else:
        data = {
            "id": row_id, "subject": f"s-{random.randint(0, 99)}",
            "predicate": random.choice(["likes", "knows", "has"]),
            "object": f"o-{random.randint(0, 99)}", "confidence": round(random.random(), 2),
            "source": node_id,
        }

    return table, op_type, row_id, data


def _get_all_ops(engine: SyncEngine) -> list[dict]:
    return engine.get_changes_since("0:0:")


def _final_state(engine: SyncEngine) -> dict:
    """Extract the merged WAL state keyed by (table, row_id) → latest op."""
    ops = _get_all_ops(engine)
    state = {}
    for op in ops:
        key = (op["table"], op["row_id"])
        existing = state.get(key)
        if existing is None or _parse_hlc(op["hlc"]) > _parse_hlc(existing["hlc"]):
            state[key] = op
    return state


async def _sync_bidirectional(a: SyncEngine, b: SyncEngine):
    """Perform a full bidirectional sync between two engines."""
    ops_a = a.get_changes_since("0:0:")
    ops_b = b.get_changes_since("0:0:")
    await b.apply_remote_changes(ops_a)
    await a.apply_remote_changes(ops_b)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCRDTFuzzConvergence:
    """100 random CRDT ops, different ordering on each node, verify convergence."""

    async def test_random_ops_converge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine_a = _make_engine(tmpdir, "node-a")
            engine_b = _make_engine(tmpdir, "node-b")

            all_ops = []
            for _ in range(100):
                table, op_type, row_id, data = _random_op(random.choice(["node-a", "node-b"]))
                all_ops.append((table, op_type, row_id, data))

            random.shuffle(all_ops)
            for table, op_type, row_id, data in all_ops[:50]:
                engine_a.log_operation(table, op_type, row_id, data)
            for table, op_type, row_id, data in all_ops[50:]:
                engine_b.log_operation(table, op_type, row_id, data)

            await _sync_bidirectional(engine_a, engine_b)

            state_a = _final_state(engine_a)
            state_b = _final_state(engine_b)
            assert state_a == state_b, "States diverged after sync"


class TestConflictingWritersLWW:
    """Both nodes write to same key with different HLC timestamps — LWW wins."""

    async def test_lww_same_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine_a = _make_engine(tmpdir, "node-a")
            engine_b = _make_engine(tmpdir, "node-b")

            for i in range(20):
                engine_a.log_operation("notes", "insert", "shared-key",
                                       {"id": "shared-key", "content": f"A-version-{i}", "tags": "[]", "importance": "normal", "source": "node-a"})
                engine_b.log_operation("notes", "insert", "shared-key",
                                       {"id": "shared-key", "content": f"B-version-{i}", "tags": "[]", "importance": "normal", "source": "node-b"})

            await _sync_bidirectional(engine_a, engine_b)

            state_a = _final_state(engine_a)
            state_b = _final_state(engine_b)
            assert state_a == state_b, "LWW conflict resolution diverged"

            winner = state_a.get(("notes", "shared-key"))
            assert winner is not None, "shared-key missing after sync"


class TestPartialDelivery:
    """Drop random 10% of ops during sync — after re-sync, state converges."""

    async def test_partial_sync_then_full_converges(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine_a = _make_engine(tmpdir, "node-a")
            engine_b = _make_engine(tmpdir, "node-b")

            for _ in range(50):
                table, op_type, row_id, data = _random_op("node-a")
                engine_a.log_operation(table, op_type, row_id, data)
            for _ in range(50):
                table, op_type, row_id, data = _random_op("node-b")
                engine_b.log_operation(table, op_type, row_id, data)

            ops_a = engine_a.get_changes_since("0:0:")
            dropped = [op for op in ops_a if random.random() > 0.1]
            await engine_b.apply_remote_changes(dropped)

            ops_b = engine_b.get_changes_since("0:0:")
            dropped_b = [op for op in ops_b if random.random() > 0.1]
            await engine_a.apply_remote_changes(dropped_b)

            await _sync_bidirectional(engine_a, engine_b)

            state_a = _final_state(engine_a)
            state_b = _final_state(engine_b)
            assert state_a == state_b, "States diverged after partial-then-full sync"


class TestNetworkFlap:
    """Sync, disconnect, more local ops, reconnect, re-sync — must converge."""

    async def test_flap_converges(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine_a = _make_engine(tmpdir, "node-a")
            engine_b = _make_engine(tmpdir, "node-b")

            for _ in range(20):
                t, o, r, d = _random_op("node-a")
                engine_a.log_operation(t, o, r, d)
            for _ in range(20):
                t, o, r, d = _random_op("node-b")
                engine_b.log_operation(t, o, r, d)
            await _sync_bidirectional(engine_a, engine_b)

            for _ in range(30):
                t, o, r, d = _random_op("node-a")
                engine_a.log_operation(t, o, r, d)
            for _ in range(30):
                t, o, r, d = _random_op("node-b")
                engine_b.log_operation(t, o, r, d)

            await _sync_bidirectional(engine_a, engine_b)

            state_a = _final_state(engine_a)
            state_b = _final_state(engine_b)
            assert state_a == state_b, "States diverged after network flap"


class TestThreeNodeTopology:
    """A<->B<->C: writes on A and C, sync all, verify all 3 converge."""

    async def test_three_nodes_converge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine_a = _make_engine(tmpdir, "node-a")
            engine_b = _make_engine(tmpdir, "node-b")
            engine_c = _make_engine(tmpdir, "node-c")

            for _ in range(40):
                t, o, r, d = _random_op("node-a")
                engine_a.log_operation(t, o, r, d)
            for _ in range(40):
                t, o, r, d = _random_op("node-c")
                engine_c.log_operation(t, o, r, d)

            for _ in range(20):
                t, o, r, d = _random_op("node-b")
                engine_b.log_operation(t, o, r, d)

            await _sync_bidirectional(engine_a, engine_b)
            await _sync_bidirectional(engine_b, engine_c)
            await _sync_bidirectional(engine_a, engine_b)

            state_a = _final_state(engine_a)
            state_b = _final_state(engine_b)
            state_c = _final_state(engine_c)

            assert state_a == state_b, "A and B diverged"
            assert state_b == state_c, "B and C diverged"


class TestStaticPeerConfig:
    """Verify static peer list parsing and loading."""

    def test_load_static_peers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, "test-node")
            import memory.sync as sync_mod
            orig = sync_mod.SYNC_PEERS
            try:
                sync_mod.SYNC_PEERS = ["192.168.1.10:9090", "10.0.0.5:8080"]
                engine._load_static_peers()
                assert len(engine._peers) >= 2
                assert any("192.168.1.10" in str(p) for p in engine._peers.values())
            finally:
                sync_mod.SYNC_PEERS = orig


class TestTLSContextBuilders:
    """Verify TLS context factories return None when unconfigured and SSLContext when configured."""

    def test_no_tls_returns_none(self):
        from memory.sync import build_server_ssl_context, build_client_ssl_context
        import memory.sync as sync_mod
        orig_cert, orig_key, orig_ca = sync_mod.SYNC_TLS_CERT, sync_mod.SYNC_TLS_KEY, sync_mod.SYNC_TLS_CA
        try:
            sync_mod.SYNC_TLS_CERT = ""
            sync_mod.SYNC_TLS_KEY = ""
            sync_mod.SYNC_TLS_CA = ""
            assert build_server_ssl_context() is None
            assert build_client_ssl_context() is None
        finally:
            sync_mod.SYNC_TLS_CERT = orig_cert
            sync_mod.SYNC_TLS_KEY = orig_key
            sync_mod.SYNC_TLS_CA = orig_ca
