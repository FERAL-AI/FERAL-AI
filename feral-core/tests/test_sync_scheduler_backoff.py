"""Sync scheduler: backoff overflow, bounded fan-out, eviction.

``_record_failure`` computed ``initial * (2 ** (failures - 1))`` with an
int exponent. Past roughly 1,024 consecutive failures the int no longer
converts to float and Python raises ``OverflowError: int too large to
convert to float`` (reproduced: ``5.0 * 2 ** 1100``). The exception
escaped ``_sync_one_peer``, so ``backoff_until`` never advanced: a second
brain on the operator's machine logged 717,708 of those tracebacks and
dialled the operator brain every 30 s cadence tick, which showed up as
81 to 133 simultaneous inbound ``/sync`` handlers. mDNS never removed the
peer because ``remove_service`` only fires for a peer that announces its
departure, and this one was alive, only unreachable.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from memory.sync_scheduler import (
    BACKOFF_EXPONENT_CAP,
    PeerStatus,
    SchedulerConfig,
    SyncScheduler,
)


class _Engine:
    """Stub SyncEngine surface. ``sync_with_peer`` mirrors the real
    keyword-only signature (see test_sync_scheduler_engine_contract)."""

    def __init__(self, *, sleep: float = 0.0, fail: bool = False):
        self._peers: dict[str, dict] = {}
        self._wal = None
        self._memory = None
        self.sleep = sleep
        self.fail = fail
        self.inflight = 0
        self.max_inflight = 0
        self.calls = 0
        self.forgotten: list[tuple[str, str]] = []

    async def sync_with_peer(self, peer_id: str) -> dict:
        self.calls += 1
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.sleep:
                await asyncio.sleep(self.sleep)
            if self.fail:
                return {"success": False, "reason": "unreachable", "error": "refused"}
            return {"success": True, "sent": 0, "received": 0}
        finally:
            self.inflight -= 1

    def forget_peer(self, peer_id: str, *, reason: str = "departure") -> bool:
        self.forgotten.append((peer_id, reason))
        return self._peers.pop(peer_id, None) is not None


def _scheduler(engine, **cfg) -> SyncScheduler:
    sched = SyncScheduler(engine, SchedulerConfig(enabled=True, **cfg))
    # Keep the periodic GC sweeps out of these tests.
    sched._last_tombstone_gc = time.time()
    sched._last_wal_gc = time.time()
    return sched


# ── Overflow ─────────────────────────────────────────────────────────────


def test_the_old_formula_overflows():
    """Documents the defect being fixed, so the clamp is not removed as
    a no-op by someone reading the new code without this history."""
    with pytest.raises(OverflowError):
        5.0 * (2 ** 1100)


def test_backoff_seconds_is_capped_at_any_streak():
    cfg = SchedulerConfig(backoff_initial_seconds=5.0, backoff_max_seconds=300.0)
    assert cfg.backoff_seconds(1) == 5.0
    assert cfg.backoff_seconds(2) == 10.0
    assert cfg.backoff_seconds(7) == 300.0
    assert cfg.backoff_seconds(5000) == 300.0
    assert cfg.backoff_seconds(10 ** 9) == 300.0
    assert cfg.backoff_seconds(0) == 5.0
    # The clamp itself never lowers a value the cap would have allowed.
    wide = SchedulerConfig(backoff_initial_seconds=1.0, backoff_max_seconds=float("inf"))
    assert wide.backoff_seconds(BACKOFF_EXPONENT_CAP + 1) == 2.0 ** BACKOFF_EXPONENT_CAP
    assert wide.backoff_seconds(BACKOFF_EXPONENT_CAP + 500) == 2.0 ** BACKOFF_EXPONENT_CAP


def test_record_failure_at_5000_failures_sets_backoff_max_without_raising():
    engine = _Engine()
    sched = _scheduler(engine, backoff_initial_seconds=5.0, backoff_max_seconds=300.0,
                       evict_after_failures=0)
    status = PeerStatus(peer_id="peer-stuck", consecutive_failures=4999)
    before = time.time()
    result = sched._record_failure(status, "unreachable", "refused", "cadence", {})
    assert status.consecutive_failures == 5000
    assert result["backoff_seconds"] == 300.0
    assert status.backoff_until >= before + 300.0 - 0.01
    assert result["ok"] is False


def test_metrics_exception_cannot_abort_the_bookkeeping():
    class _Boom:
        def labels(self, **kw):
            raise RuntimeError("metrics backend down")

    engine = _Engine()
    sched = _scheduler(engine, backoff_initial_seconds=5.0, backoff_max_seconds=300.0)
    status = PeerStatus(peer_id="p", consecutive_failures=2, last_success=time.time() - 60)
    before = time.time()
    sched._record_failure(status, "x", "y", "cadence", {"attempts": _Boom(), "lag": _Boom()})
    assert status.consecutive_failures == 3
    assert status.backoff_until >= before + 20.0 - 0.01


def test_heartbeat_miss_uses_the_clamped_backoff():
    engine = _Engine()
    sched = _scheduler(engine, backoff_initial_seconds=5.0, backoff_max_seconds=300.0,
                       heartbeat_miss_threshold=1)
    sched._peers["p"] = PeerStatus(peer_id="p", consecutive_failures=5000)
    asyncio.run(sched.heartbeat_miss("p"))
    assert sched._peers["p"].backoff_until <= time.time() + 300.0 + 0.01


# ── Bounded fan-out ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_over_50_peers_keeps_at_most_4_syncs_in_flight():
    engine = _Engine(sleep=0.02)
    for i in range(50):
        engine._peers[f"peer-{i}"] = {"address": f"10.0.0.{i}", "source": "mdns"}
    sched = _scheduler(engine, max_concurrent_syncs=4, evict_after_failures=0)

    await sched._tick()
    assert sched._bg_tasks, "tick scheduled no syncs"
    await asyncio.gather(*list(sched._bg_tasks))

    assert engine.calls == 50
    assert engine.max_inflight <= 4, f"{engine.max_inflight} syncs ran at once"
    assert engine.max_inflight >= 2, "semaphore should still allow parallelism"


@pytest.mark.asyncio
async def test_max_concurrent_syncs_comes_from_settings():
    cfg = SchedulerConfig.from_settings({"memory": {"sync": {"max_concurrent_syncs": 2, "evict_after_failures": 7}}})
    assert cfg.max_concurrent_syncs == 2
    assert cfg.evict_after_failures == 7
    assert SchedulerConfig.from_settings({}).max_concurrent_syncs == 4
    assert SchedulerConfig.from_settings({}).evict_after_failures == 20
    # Never zero: a zero-permit semaphore would stop every sync.
    assert SchedulerConfig.from_settings({"memory": {"sync": {"max_concurrent_syncs": 0}}}).max_concurrent_syncs == 1


# ── Eviction ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peer_is_forgotten_after_n_consecutive_failures():
    engine = _Engine(fail=True)
    engine._peers["peer-dead"] = {"address": "10.0.0.9", "source": "mdns"}
    sched = _scheduler(engine, backoff_initial_seconds=0.0, backoff_max_seconds=0.0,
                       evict_after_failures=3)

    for _ in range(2):
        result = await sched.sync_one_peer_now("peer-dead")
        assert result["evicted"] is False
    assert engine.forgotten == []
    assert "peer-dead" in engine._peers

    result = await sched.sync_one_peer_now("peer-dead")
    assert result["evicted"] is True
    assert engine.forgotten and engine.forgotten[0][0] == "peer-dead"
    assert "unreachable" in engine.forgotten[0][1]
    assert "peer-dead" not in engine._peers
    assert "peer-dead" not in sched.peer_status()


@pytest.mark.asyncio
async def test_manual_peers_are_never_evicted():
    engine = _Engine(fail=True)
    sched = _scheduler(engine, backoff_initial_seconds=0.0, backoff_max_seconds=0.0,
                       evict_after_failures=2)
    sched.add_peer("192.168.1.5:9090")
    for _ in range(5):
        result = await sched.sync_one_peer_now("192.168.1.5:9090")
        assert result["evicted"] is False
    assert engine.forgotten == []
    assert "192.168.1.5:9090" in engine._peers


@pytest.mark.asyncio
async def test_eviction_disabled_with_zero():
    engine = _Engine(fail=True)
    engine._peers["p"] = {"address": "10.0.0.1"}
    sched = _scheduler(engine, backoff_initial_seconds=0.0, backoff_max_seconds=0.0,
                       evict_after_failures=0)
    for _ in range(30):
        await sched.sync_one_peer_now("p")
    assert engine.forgotten == []
    assert sched.peer_status()["p"]["consecutive_failures"] == 30


def test_engine_without_forget_peer_is_tolerated():
    class _Bare:
        def __init__(self):
            self._peers = {"p": {"address": "x"}}

    engine = _Bare()
    sched = SyncScheduler(engine, SchedulerConfig(evict_after_failures=1))
    status = PeerStatus(peer_id="p")
    sched._peers["p"] = status
    sched._record_failure(status, "x", "y", "t", {})
    assert "p" not in engine._peers
    assert "p" not in sched._peers
