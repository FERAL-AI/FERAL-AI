"""PR 2 (v2026.5.34) D12 — SyncScheduler tests.

Pins the scheduler contract: backoff progression, per-peer locking,
heartbeat reconnect path, peer mutation, and ``enabled=false``
short-circuit.

The scheduler talks to a stub SyncEngine so these tests stay
hermetic — no network, no mDNS, no WebSocket handshake.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from memory.sync_scheduler import (
    PeerStatus,
    SchedulerConfig,
    SyncScheduler,
)


class _StubEngine:
    """Minimal SyncEngine surface the scheduler needs.

    ``sync_with_peer`` is driven by a script the test injects; the
    rest of the engine surface (``_peers``, ``_wal``) is stubbed to
    keep PeerStatus/metric emission code paths reachable.
    """

    def __init__(self):
        self._peers: dict[str, dict] = {}
        self._wal = type("WAL", (), {"db_path": ""})()  # zero-byte WAL
        self._calls: list[tuple[str, str]] = []  # (peer_id, passphrase)
        self._script: dict[str, list[dict]] = {}

    def script(self, peer_id: str, *results: dict) -> None:
        """Queue results that sync_with_peer will pop per call."""
        self._script[peer_id] = list(results)

    async def sync_with_peer(self, peer_id: str) -> dict:
        # Signature mirrors the real SyncEngine.sync_with_peer, which is
        # keyword-only after peer_id and has no passphrase. This double
        # used to declare passphrase="", which the engine has never
        # accepted, so the whole scheduled-sync path raised TypeError in
        # production while this file stayed green. See AUDIT-FIXES F-01
        # and tests/test_sync_scheduler_engine_contract.py, which asserts
        # this signature against the real one so it cannot drift again.
        self._calls.append((peer_id, ""))
        if peer_id not in self._script or not self._script[peer_id]:
            return {"success": False, "reason": "no_script"}
        return self._script[peer_id].pop(0)


# ── 1. Disabled config short-circuits start ─────────────────────────────


@pytest.mark.asyncio
async def test_disabled_short_circuits():
    engine = _StubEngine()
    sched = SyncScheduler(engine, SchedulerConfig(enabled=False))
    await sched.start()
    assert sched._task is None
    await sched.stop()


# ── 2. Successful sync clears state, emits ops counters ─────────────────


@pytest.mark.asyncio
async def test_successful_sync_clears_state():
    engine = _StubEngine()
    engine._peers["peer-1"] = {"address": "127.0.0.1:8002"}
    engine.script("peer-1", {"success": True, "sent": 3, "received": 5})

    sched = SyncScheduler(engine, SchedulerConfig(
        enabled=True, cadence_seconds=0.05, peer_timeout_seconds=5.0,
    ))
    result = await sched.sync_one_peer_now("peer-1")
    assert result["ok"]
    assert result["sent"] == 3
    assert result["received"] == 5
    status = sched.peer_status()["peer-1"]
    assert status["consecutive_failures"] == 0
    assert status["ops_sent"] == 3
    assert status["ops_received"] == 5
    assert status["backoff_remaining_seconds"] == 0.0


# ── 3. Failure path → backoff doubles per consecutive failure ───────────


@pytest.mark.asyncio
async def test_backoff_exponential_progression():
    engine = _StubEngine()
    engine._peers["peer-fail"] = {"address": "127.0.0.1:8003"}
    # Three failures in a row → backoff_initial → x2 → x4
    engine.script(
        "peer-fail",
        {"success": False, "reason": "auth"},
        {"success": False, "reason": "auth"},
        {"success": False, "reason": "auth"},
    )

    sched = SyncScheduler(engine, SchedulerConfig(
        enabled=True, backoff_initial_seconds=2.0, backoff_max_seconds=60.0,
    ))

    await sched.sync_one_peer_now("peer-fail")
    s1 = sched.peer_status()["peer-fail"]
    assert s1["consecutive_failures"] == 1
    assert pytest.approx(s1["backoff_remaining_seconds"], abs=0.5) == 2.0

    await sched.sync_one_peer_now("peer-fail")
    s2 = sched.peer_status()["peer-fail"]
    assert s2["consecutive_failures"] == 2
    assert pytest.approx(s2["backoff_remaining_seconds"], abs=0.5) == 4.0

    await sched.sync_one_peer_now("peer-fail")
    s3 = sched.peer_status()["peer-fail"]
    assert s3["consecutive_failures"] == 3
    assert pytest.approx(s3["backoff_remaining_seconds"], abs=0.5) == 8.0


# ── 4. Backoff caps at backoff_max_seconds ──────────────────────────────


@pytest.mark.asyncio
async def test_backoff_cap():
    engine = _StubEngine()
    engine._peers["peer-stuck"] = {"address": "127.0.0.1:8004"}
    engine.script("peer-stuck", *[{"success": False, "reason": "x"} for _ in range(8)])

    sched = SyncScheduler(engine, SchedulerConfig(
        enabled=True, backoff_initial_seconds=2.0, backoff_max_seconds=10.0,
    ))

    for _ in range(8):
        await sched.sync_one_peer_now("peer-stuck")
    status = sched.peer_status()["peer-stuck"]
    assert status["backoff_remaining_seconds"] <= 10.5  # cap + tiny slack


# ── 5. Successful sync after failures resets backoff ───────────────────


@pytest.mark.asyncio
async def test_success_resets_backoff():
    engine = _StubEngine()
    engine._peers["peer-flap"] = {"address": "127.0.0.1:8005"}
    engine.script(
        "peer-flap",
        {"success": False, "reason": "transient"},
        {"success": False, "reason": "transient"},
        {"success": True, "sent": 1, "received": 2},
    )

    sched = SyncScheduler(engine, SchedulerConfig(
        enabled=True, backoff_initial_seconds=1.0, backoff_max_seconds=60.0,
    ))
    await sched.sync_one_peer_now("peer-flap")
    await sched.sync_one_peer_now("peer-flap")
    assert sched.peer_status()["peer-flap"]["consecutive_failures"] == 2

    await sched.sync_one_peer_now("peer-flap")
    status = sched.peer_status()["peer-flap"]
    assert status["consecutive_failures"] == 0
    assert status["backoff_remaining_seconds"] == 0.0


# ── 6. Per-peer lock prevents overlapping sync_with_peer ───────────────


@pytest.mark.asyncio
async def test_per_peer_lock_prevents_overlap():
    """Two parallel sync_one_peer_now for the same peer — second
    one returns 'already_syncing' instead of overlapping the first."""
    engine = _StubEngine()
    engine._peers["peer-slow"] = {"address": "127.0.0.1:8006"}

    # Custom slow sync_with_peer.
    blocker = asyncio.Event()

    async def slow_sync(peer_id, passphrase=""):
        await blocker.wait()
        return {"success": True, "sent": 1, "received": 1}

    engine.sync_with_peer = slow_sync

    sched = SyncScheduler(engine, SchedulerConfig(enabled=True))
    # Manually arm the lock by entering and holding it
    status = sched._peers.setdefault("peer-slow", PeerStatus(peer_id="peer-slow"))
    status.lock = asyncio.Lock()
    await status.lock.acquire()
    try:
        result = await sched.sync_one_peer_now("peer-slow")
        assert not result["ok"]
        assert result["reason"] == "already_syncing"
    finally:
        status.lock.release()


# ── 7. Heartbeat misses accumulate, threshold triggers backoff ─────────


@pytest.mark.asyncio
async def test_heartbeat_misses_threshold_triggers_backoff():
    engine = _StubEngine()
    sched = SyncScheduler(engine, SchedulerConfig(
        enabled=True, heartbeat_miss_threshold=3,
        backoff_initial_seconds=5.0, backoff_max_seconds=300.0,
    ))

    await sched.heartbeat_miss("peer-x")
    await sched.heartbeat_miss("peer-x")
    status = sched._peers["peer-x"]
    assert status.consecutive_heartbeat_misses == 2
    assert status.backoff_until <= time.time()  # not yet backed off

    await sched.heartbeat_miss("peer-x")
    status = sched._peers["peer-x"]
    assert status.consecutive_heartbeat_misses == 3
    assert status.backoff_until > time.time()  # now in backoff


# ── 8. Heartbeat reconnect clears miss counter + triggers immediate sync ─


@pytest.mark.asyncio
async def test_heartbeat_reconnect_clears_and_resyncs():
    engine = _StubEngine()
    engine._peers["peer-rc"] = {"address": "127.0.0.1:8007"}
    engine.script("peer-rc", {"success": True, "sent": 2, "received": 0})

    sched = SyncScheduler(engine, SchedulerConfig(enabled=True))
    # Simulate stale state
    status = sched._peers.setdefault("peer-rc", PeerStatus(peer_id="peer-rc"))
    status.consecutive_heartbeat_misses = 5
    status.backoff_until = time.time() + 100

    await sched.heartbeat_reconnect("peer-rc")
    # Yield so the immediate-resync task can run.
    await asyncio.sleep(0.05)

    status = sched._peers["peer-rc"]
    assert status.consecutive_heartbeat_misses == 0
    assert status.backoff_until <= time.time()
    # The script was consumed — one success call.
    assert any(call[0] == "peer-rc" for call in engine._calls)


# ── 9. Peer mutation — add, list, remove ───────────────────────────────


@pytest.mark.asyncio
async def test_peer_add_list_remove():
    engine = _StubEngine()
    sched = SyncScheduler(engine, SchedulerConfig(enabled=True))

    sched.add_peer("192.168.1.99:8002")
    listing = sched.list_peers()
    assert any(p["peer_id"] == "192.168.1.99:8002" for p in listing)
    assert "192.168.1.99:8002" in engine._peers  # mirrored into engine

    sched.remove_peer("192.168.1.99:8002")
    listing_after = sched.list_peers()
    assert not any(p["peer_id"] == "192.168.1.99:8002" for p in listing_after)
    assert "192.168.1.99:8002" not in engine._peers


# ── 10. sync_all_peers_now drives every known peer in parallel ─────────


@pytest.mark.asyncio
async def test_sync_all_peers_now_parallel():
    engine = _StubEngine()
    engine._peers["p1"] = {"address": "1"}
    engine._peers["p2"] = {"address": "2"}
    engine._peers["p3"] = {"address": "3"}
    engine.script("p1", {"success": True, "sent": 1, "received": 1})
    engine.script("p2", {"success": True, "sent": 2, "received": 2})
    engine.script("p3", {"success": False, "reason": "oops"})

    sched = SyncScheduler(engine, SchedulerConfig(enabled=True))
    results = await sched.sync_all_peers_now()
    by_peer = {r.get("peer_id"): r for r in results if "peer_id" in r}
    assert by_peer["p1"]["ok"] is True
    assert by_peer["p2"]["ok"] is True
    assert by_peer["p3"]["ok"] is False


# ── 11. Timeout maps to failure with reason='timeout' ──────────────────


@pytest.mark.asyncio
async def test_timeout_recorded_as_failure():
    engine = _StubEngine()
    engine._peers["peer-hang"] = {"address": "x"}

    async def hang_forever(peer_id, passphrase=""):
        await asyncio.sleep(30)
        return {"success": True}

    engine.sync_with_peer = hang_forever

    sched = SyncScheduler(engine, SchedulerConfig(
        enabled=True, peer_timeout_seconds=0.1,
    ))
    result = await sched.sync_one_peer_now("peer-hang")
    assert not result["ok"]
    assert result["reason"] == "timeout"


# ── 12. SchedulerConfig.from_settings honours defaults and overrides ───


def test_scheduler_config_from_settings():
    cfg_defaults = SchedulerConfig.from_settings({})
    assert cfg_defaults.enabled is True
    assert cfg_defaults.cadence_seconds == 30.0

    cfg_overrides = SchedulerConfig.from_settings({
        "memory": {
            "sync": {
                "enabled": False,
                "cadence_seconds": 5,
                "backoff_initial_seconds": 1,
                "backoff_max_seconds": 7,
                "heartbeat_interval_seconds": 9,
                "heartbeat_miss_threshold": 11,
            }
        }
    })
    assert cfg_overrides.enabled is False
    assert cfg_overrides.cadence_seconds == 5.0
    assert cfg_overrides.backoff_initial_seconds == 1.0
    assert cfg_overrides.backoff_max_seconds == 7.0
    assert cfg_overrides.heartbeat_interval_seconds == 9.0
    assert cfg_overrides.heartbeat_miss_threshold == 11
