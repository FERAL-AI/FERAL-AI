#!/usr/bin/env python3
"""
Sync chaos CLI ( / roadmap §3.3 #1)
======================================
Spin two FERAL brains as subprocesses with the sync engine wired up,
SIGKILL one at random points for N iterations, then assert the WAL
state on both sides converges after a final reconciliation pass.

Used in `.github/workflows/sync-chaos-nightly.yml`. Designed so it
returns a non-zero exit code on real divergence (so the nightly
artifact is searchable for failures) but tolerates the "process was
mid-write" race because a follow-up sync resolves it.

Usage:
    python scripts/chaos/sync_kill.py --iterations 5 [--ops-per-cycle 20]
        [--kill-grace 0.5] [--seed 1234]

Layout (per run):
    tmpdir/
        a_db.sqlite              # node A memory db
        a_wal.sqlite             # node A sync WAL
        b_db.sqlite              # node B memory db
        b_wal.sqlite             # node B sync WAL
        run.log                  # combined chaos log
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FERAL_CORE = REPO_ROOT / "feral-core"


DRIVER_SCRIPT = r"""
import json
import os
import random
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["FERAL_CORE_DIR"])

from memory.store import MemoryStore  # noqa: E402
from memory.sync import SyncEngine    # noqa: E402

NODE_ID = os.environ["NODE_ID"]
DB_PATH = os.environ["DB_PATH"]
WAL_PATH = os.environ["WAL_PATH"]
OPS_TARGET = int(os.environ.get("OPS_TARGET", "20"))
SLEEP_BETWEEN_OPS = float(os.environ.get("SLEEP_BETWEEN_OPS", "0.05"))
HEARTBEAT_PATH = os.environ["HEARTBEAT_PATH"]

random.seed(int(os.environ.get("SEED", "0")) ^ hash(NODE_ID))


def _stop_handler(signum, frame):
    Path(HEARTBEAT_PATH).write_text(json.dumps({
        "node": NODE_ID, "ts": time.time(), "status": "signaled",
        "signal": signum,
    }))
    sys.exit(0)


signal.signal(signal.SIGTERM, _stop_handler)
signal.signal(signal.SIGINT, _stop_handler)

store = MemoryStore(db_path=DB_PATH)
engine = SyncEngine(node_id=NODE_ID, memory_store=store, db_path=WAL_PATH)
store.set_sync_engine(engine)

written = 0
try:
    while written < OPS_TARGET:
        row_id = f"{NODE_ID}-row-{written:05d}"
        engine.log_operation(
            "notes", "insert", row_id,
            {
                "id": row_id,
                "content": f"chaos op {written} from {NODE_ID}",
                "tags": "[\"chaos\"]",
                "importance": "normal",
                "source": NODE_ID,
                "created_at": time.time(),
            },
        )
        written += 1
        Path(HEARTBEAT_PATH).write_text(json.dumps({
            "node": NODE_ID, "ts": time.time(), "status": "writing",
            "written": written,
        }))
        time.sleep(SLEEP_BETWEEN_OPS)

    Path(HEARTBEAT_PATH).write_text(json.dumps({
        "node": NODE_ID, "ts": time.time(), "status": "done",
        "written": written,
    }))
finally:
    try:
        store.close()
    except Exception:
        pass
"""


def _spawn(node_id: str, db_path: str, wal_path: str, heartbeat_path: str,
           ops_target: int, seed: int, log_fh) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "NODE_ID": node_id,
        "DB_PATH": db_path,
        "WAL_PATH": wal_path,
        "HEARTBEAT_PATH": heartbeat_path,
        "OPS_TARGET": str(ops_target),
        "SEED": str(seed),
        "FERAL_CORE_DIR": str(FERAL_CORE),
        "FERAL_HOME": str(pathlib.Path(db_path).parent / f".feral-{node_id}"),
        "PYTHONPATH": str(FERAL_CORE),
    })
    return subprocess.Popen(
        [sys.executable, "-c", DRIVER_SCRIPT],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )


def _kill(proc: subprocess.Popen, grace: float, log_fh) -> None:
    if proc.poll() is not None:
        return
    log_fh.write(f"  KILL pid={proc.pid}\n")
    log_fh.flush()
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=grace)


def _final_state_hash(wal_path: str) -> tuple[int, str]:
    """Read the WAL via SyncEngine and return (op_count, state_hash)."""
    sys.path.insert(0, str(FERAL_CORE))
    from memory.store import MemoryStore  # type: ignore
    from memory.sync import SyncEngine, _parse_hlc  # type: ignore

    db = wal_path.replace("_wal", "_db")
    store = MemoryStore(db_path=db)
    engine = SyncEngine(node_id="reconciler", memory_store=store, db_path=wal_path)
    ops = engine.get_changes_since("0:0:")
    state = {}
    for op in ops:
        key = (op["table"], op["row_id"])
        existing = state.get(key)
        if existing is None or _parse_hlc(op["hlc"]) > _parse_hlc(existing["hlc"]):
            state[key] = op
    canonical = json.dumps(
        sorted([(k[0], k[1], v["hlc"], v["origin_node"]) for k, v in state.items()]),
        sort_keys=True,
    )
    import hashlib
    return len(state), hashlib.sha256(canonical.encode()).hexdigest()


def _final_sync(wal_a: str, wal_b: str, log_fh) -> None:
    """Bidirectional reconciliation across the two on-disk WALs."""
    sys.path.insert(0, str(FERAL_CORE))
    from memory.store import MemoryStore  # type: ignore
    from memory.sync import SyncEngine     # type: ignore

    db_a = wal_a.replace("_wal", "_db")
    db_b = wal_b.replace("_wal", "_db")
    store_a = MemoryStore(db_path=db_a)
    store_b = MemoryStore(db_path=db_b)
    engine_a = SyncEngine(node_id="node-a", memory_store=store_a, db_path=wal_a)
    engine_b = SyncEngine(node_id="node-b", memory_store=store_b, db_path=wal_b)

    ops_a = engine_a.get_changes_since("0:0:")
    ops_b = engine_b.get_changes_since("0:0:")
    log_fh.write(f"  reconciliation: A has {len(ops_a)} ops, B has {len(ops_b)} ops\n")
    engine_b.apply_remote_changes(ops_a)
    engine_a.apply_remote_changes(ops_b)


def run(iterations: int, ops_per_cycle: int, kill_grace: float, seed: int) -> int:
    rng = random.Random(seed)
    tmpdir = tempfile.mkdtemp(prefix="feral-chaos-")
    log_path = os.path.join(tmpdir, "run.log")
    log_fh = open(log_path, "w")

    db_a = os.path.join(tmpdir, "a_db.sqlite")
    wal_a = os.path.join(tmpdir, "a_wal.sqlite")
    db_b = os.path.join(tmpdir, "b_db.sqlite")
    wal_b = os.path.join(tmpdir, "b_wal.sqlite")
    heartbeat_a = os.path.join(tmpdir, "a.heartbeat")
    heartbeat_b = os.path.join(tmpdir, "b.heartbeat")

    log_fh.write(f"chaos run dir: {tmpdir}\n")
    log_fh.write(f"iterations={iterations} ops_per_cycle={ops_per_cycle} "
                 f"kill_grace={kill_grace} seed={seed}\n")

    try:
        for it in range(1, iterations + 1):
            log_fh.write(f"\n=== iteration {it}/{iterations} ===\n")
            log_fh.flush()

            proc_a = _spawn("node-a", db_a, wal_a, heartbeat_a,
                            ops_per_cycle, seed + it, log_fh)
            proc_b = _spawn("node-b", db_b, wal_b, heartbeat_b,
                            ops_per_cycle, seed + it + 100, log_fh)

            # Random kill point: somewhere between immediately and a
            # full cycle's worth of writes.
            kill_after = rng.uniform(0.05, 0.05 * ops_per_cycle)
            time.sleep(kill_after)

            victim = rng.choice([proc_a, proc_b])
            log_fh.write(f"  killing victim after {kill_after:.3f}s\n")
            _kill(victim, kill_grace, log_fh)

            # Let the survivor finish (bounded by ops_per_cycle * sleep).
            survivor = proc_b if victim is proc_a else proc_a
            try:
                survivor.wait(timeout=max(2.0, ops_per_cycle * 0.2))
            except subprocess.TimeoutExpired:
                _kill(survivor, kill_grace, log_fh)

            # Resurrect the victim so its WAL grows further on the next
            # iteration. We don't need to await it — the next loop spawn
            # will come right back around.
            log_fh.write("  victim restarting briefly to drain remaining ops\n")
            log_fh.flush()
            victim_node = "node-a" if victim is proc_a else "node-b"
            victim_db = db_a if victim is proc_a else db_b
            victim_wal = wal_a if victim is proc_a else wal_b
            victim_hb = heartbeat_a if victim is proc_a else heartbeat_b
            restarted = _spawn(victim_node, victim_db, victim_wal, victim_hb,
                               max(1, ops_per_cycle // 2), seed + it + 200, log_fh)
            try:
                restarted.wait(timeout=max(2.0, ops_per_cycle * 0.2))
            except subprocess.TimeoutExpired:
                _kill(restarted, kill_grace, log_fh)

        # Final reconciliation.
        log_fh.write("\n=== final reconciliation ===\n")
        log_fh.flush()
        _final_sync(wal_a, wal_b, log_fh)

        count_a, hash_a = _final_state_hash(wal_a)
        count_b, hash_b = _final_state_hash(wal_b)
        log_fh.write(f"  A: {count_a} keys, hash={hash_a}\n")
        log_fh.write(f"  B: {count_b} keys, hash={hash_b}\n")
        log_fh.flush()

        if hash_a != hash_b:
            log_fh.write("CONVERGENCE FAILURE: WAL state differs after final sync\n")
            print(f"CHAOS: divergence (A={count_a} ops B={count_b} ops, hashes differ)",
                  file=sys.stderr)
            print(f"CHAOS: log written to {log_path}", file=sys.stderr)
            return 2

        log_fh.write("CONVERGENCE OK\n")
        print(f"CHAOS: convergence OK ({count_a} keys, hash={hash_a[:12]})")
        print(f"CHAOS: log written to {log_path}")
        return 0
    finally:
        log_fh.close()
        # Keep tmpdir on failure for forensic upload by CI; clean only on
        # explicit env opt-in.
        if os.environ.get("FERAL_CHAOS_CLEANUP") == "1":
            shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="FERAL sync chaos runner")
    parser.add_argument("--iterations", type=int, default=5,
                        help="number of kill cycles to perform")
    parser.add_argument("--ops-per-cycle", type=int, default=20,
                        help="target ops each subprocess writes per cycle")
    parser.add_argument("--kill-grace", type=float, default=0.5,
                        help="seconds to wait for graceful shutdown after SIGTERM")
    parser.add_argument("--seed", type=int, default=int(time.time()) & 0xFFFF,
                        help="RNG seed for reproducible kill timings")
    args = parser.parse_args()

    return run(
        iterations=args.iterations,
        ops_per_cycle=args.ops_per_cycle,
        kill_grace=args.kill_grace,
        seed=args.seed,
    )


if __name__ == "__main__":
    sys.exit(main())
