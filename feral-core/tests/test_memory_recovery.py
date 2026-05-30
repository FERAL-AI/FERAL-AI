"""
Recovery test for federated memory sync ( / roadmap §3.3 #1).

Models the "kill brain mid-apply" failure: peer A produces 100 episodes
worth of operations, peer B starts ingesting them, gets killed at the
midpoint, restarts, and re-syncs. The post-recovery state must:

  1. converge to A's full state (all 100 episodes present),
  2. contain no duplicates (dedup keyed by op_id),
  3. preserve HLC monotonicity (the merged WAL, sorted by HLC, must
     never go backwards).
"""
from __future__ import annotations

import os
import tempfile

import pytest

from memory.store import MemoryStore
from memory.sync import SyncEngine, SyncOperation, _parse_hlc


pytestmark = pytest.mark.chaos


def _make_pair(tmpdir: str) -> tuple[SyncEngine, SyncEngine]:
    db_a = os.path.join(tmpdir, "a.db")
    db_b = os.path.join(tmpdir, "b.db")
    wal_a = os.path.join(tmpdir, "a_wal.db")
    wal_b = os.path.join(tmpdir, "b_wal.db")
    store_a = MemoryStore(db_path=db_a)
    store_b = MemoryStore(db_path=db_b)
    engine_a = SyncEngine(node_id="node-a", memory_store=store_a, db_path=wal_a)
    engine_b = SyncEngine(node_id="node-b", memory_store=store_b, db_path=wal_b)
    store_a.set_sync_engine(engine_a)
    store_b.set_sync_engine(engine_b)
    return engine_a, engine_b


def _restart_b(tmpdir: str) -> SyncEngine:
    """Re-instantiate B against the same on-disk paths (simulates fresh process)."""
    db_b = os.path.join(tmpdir, "b.db")
    wal_b = os.path.join(tmpdir, "b_wal.db")
    store_b = MemoryStore(db_path=db_b)
    engine_b = SyncEngine(node_id="node-b", memory_store=store_b, db_path=wal_b)
    store_b.set_sync_engine(engine_b)
    return engine_b


class TestKillBrainMidApply:
    """Kill peer B at byte 50% of the WS chunk; restart; verify convergence."""

    async def test_resyncs_with_no_duplicates_and_hlc_monotonic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine_a, engine_b = _make_pair(tmpdir)

            # Peer A produces 100 episodes-worth of ops via its sync engine.
            for i in range(100):
                engine_a.log_operation(
                    "episodes", "insert", f"ep-{i:03d}",
                    {
                        "id": f"ep-{i:03d}",
                        "session_id": "sess-recovery",
                        "event_type": "observation",
                        "summary": f"episode {i}",
                        "detail": f"detail body for episode {i}",
                        "importance": 0.5,
                        "created_at": 1700_000_000.0 + i,
                    },
                )

            ops_from_a = engine_a.get_changes_since("0:0:")
            assert len(ops_from_a) == 100

            # B applies the first 50% of the chunk, then "the process dies".
            half = len(ops_from_a) // 2
            applied_first_half = await engine_b.apply_remote_changes(ops_from_a[:half])
            assert applied_first_half == half

            # Snapshot B's pre-crash state for monotonicity comparison.
            pre_crash_ops = engine_b.get_changes_since("0:0:")
            pre_crash_hlcs = [op["hlc"] for op in pre_crash_ops]

            # ── crash ── (drop the in-memory engine; on-disk WAL persists)
            await engine_b._memory.aclose()
            del engine_b

            # ── restart ──
            engine_b = _restart_b(tmpdir)

            # On restart, B sees A's full 100 ops (re-sync delivers the
            # entire chunk, not just the missing tail — this is the
            # realistic recovery path because the killed B never ACKed
            # its half-applied position).
            # v2026.5.34 (PR 2 D12): the first half's ops now have a
            # row already on disk with an hlc_string. The LWW gate
            # short-circuits them as "not strictly greater" so apply
            # returns ``half`` for the new ops; the existing rows are
            # already at the right version.
            replay_count = await engine_b.apply_remote_changes(ops_from_a)
            assert replay_count == half, (
                f"expected {half} newly-applied ops post-restart, got {replay_count}"
            )

            # 1) No duplicates: WAL is keyed on op_id (INSERT OR REPLACE),
            #    so re-applying the first half overwrote rather than
            #    duplicated. Final WAL count == 100.
            final_ops = engine_b.get_changes_since("0:0:")
            assert len(final_ops) == 100, (
                f"expected 100 ops after recovery, got {len(final_ops)}"
            )
            ids = {op["op_id"] for op in final_ops}
            assert len(ids) == 100, f"duplicate op_ids in WAL: {len(final_ops) - len(ids)}"

            # And no duplicate row_ids in the materialized episodes table —
            # _apply_to_memory uses INSERT OR IGNORE, so the same ep-{i}
            # never lands twice.
            row_ids = [op["row_id"] for op in final_ops]
            assert len(set(row_ids)) == 100, "duplicate episode row_ids"

            # 2) Materialized episodes table on B has all 100 entries.
            #    (apply_remote_changes already routed inserts via
            #    _apply_to_memory.)
            recent = await engine_b._memory.episode_recent(limit=200, session_id="sess-recovery")
            assert len(recent) == 100, (
                f"expected 100 episodes materialized, got {len(recent)}"
            )

            # 3) HLC monotonicity: sorting B's WAL by HLC reproduces the
            #    same ordering A produced. Any out-of-order re-apply
            #    would break the chain.
            sorted_b_hlcs = sorted(
                (op["hlc"] for op in final_ops),
                key=_parse_hlc,
            )
            for prev, curr in zip(sorted_b_hlcs, sorted_b_hlcs[1:]):
                assert _parse_hlc(curr) >= _parse_hlc(prev), (
                    f"HLC went backwards: {prev!r} -> {curr!r}"
                )

            # The pre-crash prefix must be a prefix of the post-recovery
            # ordering (causal continuity — B's earlier observations are
            # still present and in the same relative order).
            sorted_pre = sorted(pre_crash_hlcs, key=_parse_hlc)
            assert sorted_pre == sorted_b_hlcs[: len(sorted_pre)], (
                "pre-crash prefix not preserved after recovery"
            )

            # 4) Convergence with A: WAL state matches.
            ops_from_a_final = engine_a.get_changes_since("0:0:")
            a_index = {op["op_id"]: op for op in ops_from_a_final}
            b_index = {op["op_id"]: op for op in final_ops}
            assert set(a_index.keys()) == set(b_index.keys()), "A and B WALs diverge"
            # And every op pulled from A is byte-for-byte present in B.
            for op_id, a_op in a_index.items():
                b_op = b_index[op_id]
                assert a_op["hlc"] == b_op["hlc"]
                assert a_op["row_id"] == b_op["row_id"]
                assert a_op["origin_node"] == b_op["origin_node"]
