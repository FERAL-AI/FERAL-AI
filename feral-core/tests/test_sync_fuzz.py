"""
CRDT fuzzing tests for federated memory sync.

Verifies convergence under:
- Random operation ordering across nodes
- Conflicting concurrent writes (LWW semantics)
- Partial message delivery (10% drop)
- Network flap (disconnect / local ops / reconnect)
- 3-node transitive topology (A<->B<->C)
"""
import contextlib
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


@contextlib.asynccontextmanager
async def _engines(tmpdir: str, *node_ids: str):
    """Engines whose stores get closed.

    Needed now that convergence is asserted against the materialised
    tables: that opens each store's aiosqlite pool, and an unclosed
    pool is a live worker thread per connection for the rest of the
    session.
    """
    made = [_make_engine(tmpdir, n) for n in node_ids]
    try:
        yield made
    finally:
        for engine in made:
            await engine._memory.aclose()


async def _local_write(engine: SyncEngine, table, op_type, row_id, data) -> str:
    """A local write: the WAL entry AND the materialised row.

    ``SyncEngine.log_operation`` deliberately only appends to the WAL;
    the row itself is written by ``MemoryStore``, which calls
    ``_log_sync`` and its own INSERT together. A test that calls
    ``log_operation`` alone produces a node whose own writes are absent
    from its own tables, so asserting on those tables would compare two
    stores that hold nothing but each other's data. That is invisible
    while the assertion reads the op log, which is how it survived.
    """
    op = engine._build_operation(table, op_type, row_id, data)
    engine._wal.append(op)
    engine._vector_clock.update(engine.node_id, op.hlc)
    await engine._apply_to_memory(op)
    return op.hlc


def _random_op(node_id: str, tables=("notes", "knowledge"), op_types=("insert", "delete")):
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


# What the materialised tables are keyed and compared on. ``hlc_string``
# is the LWW winner's identity, so two nodes agreeing on it agree on
# which write survived; the content column is carried alongside so a
# node that materialised the right HLC with the wrong payload is caught
# too.
_STATE_TABLES = {
    "notes": "content",
    "knowledge": "object",
}


async def _final_state(engine: SyncEngine) -> dict:
    """The merged state as the USER sees it: the materialised rows.

    This used to be derived from the op log, which cannot fail. Both
    engines are handed the same operations, so a log-derived "state"
    agrees by construction no matter what the apply path does with
    those operations, and ``_apply_to_memory`` rejects plenty: any
    ``op_type`` outside insert/delete, any table outside
    ``_SYNC_ALLOWED_TABLES``, anything losing an HLC compare. The
    generator above was emitting ``op_type="update"`` ops that the
    apply path rejects outright, and this assertion passed on them for
    as long as it read the log.
    """
    store = engine._memory
    state: dict = {}
    conn = await store._conn()
    try:
        for table, content_col in _STATE_TABLES.items():
            async with conn.execute(
                f"SELECT id, {content_col} AS content, hlc_string FROM {table}"
            ) as cur:
                for row in await cur.fetchall():
                    state[(table, row["id"])] = (row["content"], row["hlc_string"])
    finally:
        await store._release(conn)
    return state


async def _sync_bidirectional(a: SyncEngine, b: SyncEngine):
    """A full bidirectional sync, through the same change-set selection
    the wire protocol uses.

    It used to hand over ``get_changes_since("0:0:")``, the entire WAL,
    in both directions. That skips the vector clock, the per-origin
    watermark and ``exclude_node`` entirely, so every defect in change
    -set selection was invisible here: shipping everything every time
    converges no matter how broken the selection logic is.
    """
    # Both change sets are computed before either is applied, which is
    # the ordering the handshake produces.
    ops_a = a._wal.get_changes_for_peer(b.get_vector_clock(), exclude_node=b.node_id)
    ops_b = b._wal.get_changes_for_peer(a.get_vector_clock(), exclude_node=a.node_id)
    await b.apply_remote_changes([op.to_dict() for op in ops_a])
    await a.apply_remote_changes([op.to_dict() for op in ops_b])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCRDTFuzzConvergence:
    """100 random CRDT ops, different ordering on each node, verify convergence."""

    async def test_random_ops_converge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            async with _engines(tmpdir, "node-a", "node-b") as (engine_a, engine_b):
                all_ops = []
                for _ in range(100):
                    table, op_type, row_id, data = _random_op(random.choice(["node-a", "node-b"]))
                    all_ops.append((table, op_type, row_id, data))

                random.shuffle(all_ops)
                for table, op_type, row_id, data in all_ops[:50]:
                    await _local_write(engine_a, table, op_type, row_id, data)
                for table, op_type, row_id, data in all_ops[50:]:
                    await _local_write(engine_b, table, op_type, row_id, data)

                await _sync_bidirectional(engine_a, engine_b)

                state_a = await _final_state(engine_a)
                state_b = await _final_state(engine_b)
                assert state_a == state_b, "States diverged after sync"


class TestConflictingWritersLWW:
    """Both nodes write to same key with different HLC timestamps — LWW wins."""

    async def test_lww_same_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            async with _engines(tmpdir, "node-a", "node-b") as (engine_a, engine_b):
                for i in range(20):
                    await _local_write(engine_a, "notes", "insert", "shared-key",
                                           {"id": "shared-key", "content": f"A-version-{i}", "tags": "[]", "importance": "normal", "source": "node-a"})
                    await _local_write(engine_b, "notes", "insert", "shared-key",
                                           {"id": "shared-key", "content": f"B-version-{i}", "tags": "[]", "importance": "normal", "source": "node-b"})

                await _sync_bidirectional(engine_a, engine_b)

                state_a = await _final_state(engine_a)
                state_b = await _final_state(engine_b)
                assert state_a == state_b, "LWW conflict resolution diverged"

                winner = state_a.get(("notes", "shared-key"))
                assert winner is not None, "shared-key missing after sync"


class TestPartialDelivery:
    """Drop random 10% of ops during sync — after re-sync, state converges."""

    async def test_partial_sync_then_full_converges(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            async with _engines(tmpdir, "node-a", "node-b") as (engine_a, engine_b):
                for _ in range(50):
                    table, op_type, row_id, data = _random_op("node-a")
                    await _local_write(engine_a, table, op_type, row_id, data)
                for _ in range(50):
                    table, op_type, row_id, data = _random_op("node-b")
                    await _local_write(engine_b, table, op_type, row_id, data)

                # A TRUNCATED exchange, not a randomly holed one.
                #
                # The old version dropped a random 10% of ops from the
                # middle and still expected convergence. No vector-clock
                # protocol can deliver that: a watermark says "I have
                # everything from this origin up to here", so applying
                # op 50 while missing op 7 puts op 7 permanently below
                # the watermark and the peer never offers it again. The
                # old test only passed because ``_sync_bidirectional``
                # re-sent the entire WAL every round regardless of any
                # watermark.
                #
                # What the wire can actually produce is a truncation:
                # frames arrive in HLC order over TCP, so a connection
                # that dies mid-exchange leaves a PREFIX applied. The
                # watermark then points exactly at the last op that
                # landed and the next handshake resumes from there,
                # which is the property worth testing.
                ops_a = engine_a.get_changes_since("0:0:")
                await engine_b.apply_remote_changes(ops_a[: int(len(ops_a) * 0.9)])

                ops_b = engine_b.get_changes_since("0:0:")
                await engine_a.apply_remote_changes(ops_b[: int(len(ops_b) * 0.9)])

                await _sync_bidirectional(engine_a, engine_b)

                state_a = await _final_state(engine_a)
                state_b = await _final_state(engine_b)
                assert state_a == state_b, "States diverged after partial-then-full sync"


class TestNetworkFlap:
    """Sync, disconnect, more local ops, reconnect, re-sync — must converge."""

    async def test_flap_converges(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            async with _engines(tmpdir, "node-a", "node-b") as (engine_a, engine_b):
                for _ in range(20):
                    t, o, r, d = _random_op("node-a")
                    await _local_write(engine_a, t, o, r, d)
                for _ in range(20):
                    t, o, r, d = _random_op("node-b")
                    await _local_write(engine_b, t, o, r, d)
                await _sync_bidirectional(engine_a, engine_b)

                for _ in range(30):
                    t, o, r, d = _random_op("node-a")
                    await _local_write(engine_a, t, o, r, d)
                for _ in range(30):
                    t, o, r, d = _random_op("node-b")
                    await _local_write(engine_b, t, o, r, d)

                await _sync_bidirectional(engine_a, engine_b)

                state_a = await _final_state(engine_a)
                state_b = await _final_state(engine_b)
                assert state_a == state_b, "States diverged after network flap"


class TestThreeNodeTopology:
    """A<->B<->C: writes on A and C, sync all, verify all 3 converge."""

    async def test_three_nodes_converge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            async with _engines(tmpdir, "node-a", "node-b", "node-c") as (
                engine_a, engine_b, engine_c,
            ):
                for _ in range(40):
                    t, o, r, d = _random_op("node-a")
                    await _local_write(engine_a, t, o, r, d)
                for _ in range(40):
                    t, o, r, d = _random_op("node-c")
                    await _local_write(engine_c, t, o, r, d)

                for _ in range(20):
                    t, o, r, d = _random_op("node-b")
                    await _local_write(engine_b, t, o, r, d)

                # A <-> B <-> C carries A's writes to C only on a second
                # pass through the relay, so the last round is not
                # redundant: it is what closes the line.
                await _sync_bidirectional(engine_a, engine_b)
                await _sync_bidirectional(engine_b, engine_c)
                await _sync_bidirectional(engine_a, engine_b)
                await _sync_bidirectional(engine_b, engine_c)

                state_a = await _final_state(engine_a)
                state_b = await _final_state(engine_b)
                state_c = await _final_state(engine_c)

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
