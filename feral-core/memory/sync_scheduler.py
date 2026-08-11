"""FERAL Sync Scheduler — periodic peer reconciliation.

The :class:`SyncScheduler` walks every known peer on a
``settings.memory.sync.cadence_seconds`` cadence, drives a full
bidirectional sync via :class:`SyncEngine.sync_with_peer`, and tracks
per-peer health (last-success, lag, failure streak, backoff). It's the
moving piece that turns mDNS discovery into actually-replicated memory.

Why a separate class and not a method on SyncEngine? The engine owns
the transport (handshake, WAL exchange, materialization). The scheduler
owns *when* to sync, *which* peer to talk to next, and *how aggressively*
to back off when a peer is unreachable. Splitting them keeps the engine
testable in isolation (the LWW gate works without any networking) and
keeps the scheduler swappable (an operator can write a custom policy
without forking the engine).

Lifecycle:

* :meth:`start` kicks the asyncio loop. No-op when
  ``settings.memory.sync.enabled`` is false.
* :meth:`stop` cancels the loop task.
* :meth:`sync_all_peers_now` triggers an immediate sync of every peer
  outside the cadence (used by CLI / HTTP).
* :meth:`peer_status` returns the per-peer health snapshot for
  observability (driven from the same dict that updates Prometheus).

Backoff:

Each peer carries a ``backoff_until`` timestamp. On every failure we
double the wait (starting at ``backoff_initial_seconds``, capping at
``backoff_max_seconds``); on every success we reset to zero. The
scheduler's main loop skips a peer whose backoff_until is still in the
future, so a single dead peer doesn't slow the rest of the cluster.

Per-peer asyncio.Lock prevents two overlapping ``sync_with_peer``
attempts against the same peer (a chaos-killed retry + a manual ``feral
sync now`` call from a different code path). Locks are lazy-created
because asyncio.Lock binds to the running event loop on first ``await``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from memory.sync import SyncEngine

logger = logging.getLogger("feral.memory.sync_scheduler")


def _metrics() -> dict:
    """Lazy accessor for the central D12 sync metrics. See decay.py
    for the rationale — observability is optional, metrics never
    break the sync loop."""
    try:
        from observability import metrics as _m

        return {
            "attempts": _m.SYNC_ATTEMPTS_TOTAL,
            "ops_sent": _m.SYNC_OPS_SENT_TOTAL,
            "ops_received": _m.SYNC_OPS_RECEIVED_TOTAL,
            "lag": _m.SYNC_LAG_SECONDS,
            "wal_size": _m.SYNC_WAL_SIZE_BYTES,
            "heartbeat_misses": _m.SYNC_HEARTBEAT_MISSES_TOTAL,
            "active_peers": _m.SYNC_ACTIVE_PEERS,
        }
    except Exception:  # pragma: no cover
        return {}


@dataclass(frozen=True)
class SchedulerConfig:
    """Tunables for :class:`SyncScheduler`.

    Defaults mirror ``settings.memory.sync`` in ``config/loader.py``.
    """

    enabled: bool = True
    cadence_seconds: float = 30.0
    peer_timeout_seconds: float = 10.0
    backoff_initial_seconds: float = 5.0
    backoff_max_seconds: float = 300.0
    heartbeat_interval_seconds: float = 15.0
    heartbeat_miss_threshold: int = 3

    @classmethod
    def from_settings(cls, settings: dict) -> "SchedulerConfig":
        cfg = (settings.get("memory") or {}).get("sync") or {}
        return cls(
            enabled=bool(cfg.get("enabled", cls.enabled)),
            cadence_seconds=float(cfg.get("cadence_seconds", cls.cadence_seconds)),
            peer_timeout_seconds=float(cfg.get("peer_timeout_seconds", cls.peer_timeout_seconds)),
            backoff_initial_seconds=float(cfg.get("backoff_initial_seconds", cls.backoff_initial_seconds)),
            backoff_max_seconds=float(cfg.get("backoff_max_seconds", cls.backoff_max_seconds)),
            heartbeat_interval_seconds=float(cfg.get("heartbeat_interval_seconds", cls.heartbeat_interval_seconds)),
            heartbeat_miss_threshold=int(cfg.get("heartbeat_miss_threshold", cls.heartbeat_miss_threshold)),
        )


@dataclass
class PeerStatus:
    """Per-peer health record. Mutated by the scheduler; surfaced by
    :meth:`SyncScheduler.peer_status`."""

    peer_id: str
    last_success: float = 0.0
    last_attempt: float = 0.0
    last_error: str = ""
    consecutive_failures: int = 0
    consecutive_heartbeat_misses: int = 0
    backoff_until: float = 0.0
    ops_sent: int = 0
    ops_received: int = 0
    lock: Optional[asyncio.Lock] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "peer_id": self.peer_id,
            "last_success": self.last_success,
            "last_attempt": self.last_attempt,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_heartbeat_misses": self.consecutive_heartbeat_misses,
            "backoff_remaining_seconds": max(0.0, self.backoff_until - now),
            "lag_seconds": max(0.0, now - self.last_success) if self.last_success else None,
            "ops_sent": self.ops_sent,
            "ops_received": self.ops_received,
        }


class SyncScheduler:
    """Periodic peer reconciliation orchestrator. See the module
    docstring for the operating contract."""

    def __init__(self, engine: "SyncEngine", config: Optional[SchedulerConfig] = None):
        self.engine = engine
        self.config = config or SchedulerConfig()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._peers: dict[str, PeerStatus] = {}
        # Manual peer additions (e.g. via CLI / HTTP) live alongside
        # the engine's mDNS-discovered peers. We mirror into the
        # engine's _peers dict so :meth:`SyncEngine.sync_with_peer`
        # can find them too.
        self._manual_peers: dict[str, str] = {}
        # AUDIT-FIXES F-06 (not in the original citation list; found by the
        # AST sweep). Strong references to the per-peer sync tasks kicked
        # off by the cadence tick and by heartbeat reconnect. The loop keeps
        # tasks only weakly, so a sync suspended on network I/O could be
        # collected: the peer's PeerStatus is left mid-flight, nothing
        # records a failure, and the operator sees a peer that never syncs.
        # Discard-on-done bounds the set to peers currently syncing.
        self._bg_tasks: set[asyncio.Task] = set()

    def _track_bg_task(self, task: asyncio.Task) -> asyncio.Task:
        """Hold a strong reference to a fire-and-forget sync. See F-06."""
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("SyncScheduler disabled by settings; not starting")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="sync-scheduler")
        logger.info(
            "SyncScheduler started: cadence=%.0fs, backoff=%.0f→%.0fs, heartbeat=%.0fs/%dx",
            self.config.cadence_seconds,
            self.config.backoff_initial_seconds,
            self.config.backoff_max_seconds,
            self.config.heartbeat_interval_seconds,
            self.config.heartbeat_miss_threshold,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def _loop(self) -> None:
        """Main scheduler loop. Walks every known peer once per
        ``cadence_seconds``, skipping peers in backoff."""
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                logger.exception("scheduler tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.cadence_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        """One scheduler pass over all known peers."""
        engine_peers = list(self.engine._peers.keys()) if self.engine and self.engine._peers else []
        manual_peers = list(self._manual_peers.keys())
        peer_ids = list({*engine_peers, *manual_peers})

        self._emit_active_peers_gauge(len(peer_ids))
        self._emit_wal_size_gauge()

        now = time.time()
        for peer_id in peer_ids:
            status = self._peers.setdefault(peer_id, PeerStatus(peer_id=peer_id))
            if status.backoff_until > now:
                logger.debug(
                    "scheduler: skipping %s (backoff %.1fs left)",
                    peer_id, status.backoff_until - now,
                )
                continue
            # Per-peer lock — kick off the sync without blocking other
            # peers, but never let two overlap for the same peer.
            self._track_bg_task(
                asyncio.create_task(
                    self._sync_one_peer(peer_id, trigger="cadence"),
                    name=f"sync-{peer_id}",
                )
            )

    # ── Per-peer sync ───────────────────────────────────────────────────

    async def _sync_one_peer(self, peer_id: str, trigger: str = "manual") -> dict:
        """Sync exactly one peer. Updates :class:`PeerStatus` + emits
        metrics. Returns a status dict ready for HTTP / CLI."""
        status = self._peers.setdefault(peer_id, PeerStatus(peer_id=peer_id))
        if status.lock is None:
            status.lock = asyncio.Lock()
        if status.lock.locked():
            logger.debug("scheduler: %s already syncing, skipping", peer_id)
            return {"ok": False, "reason": "already_syncing"}

        async with status.lock:
            status.last_attempt = time.time()
            m = _metrics()
            try:
                # No passphrase argument: SyncEngine.sync_with_peer is
                # keyword-only and has never had one. Passing it raised
                # TypeError on every scheduled sync, and the broad handler
                # below filed that as an ordinary peer failure, so the
                # feature reported a flaky network for about 40 releases
                # while never once running. See AUDIT-FIXES F-01.
                #
                # It is not threaded through either, because the engine
                # already has the better value. _handshake_and_exchange
                # reads memory.sync.SYNC_PASSPHRASE, which
                # ensure_sync_passphrase() resolves at boot as env, then
                # vault, then freshly generated and persisted. This module's
                # own _passphrase() reads os.environ alone, so handing it
                # over would send an empty passphrase on any install whose
                # secret lives in the vault, which is the normal case.
                result = await asyncio.wait_for(
                    self.engine.sync_with_peer(peer_id),
                    timeout=self.config.peer_timeout_seconds + 5.0,  # generous outer cap
                )
            except asyncio.TimeoutError as exc:
                return self._record_failure(status, "timeout", str(exc), trigger, m)
            except TypeError as exc:
                # Calling our own engine wrongly is not a peer being
                # unreachable. Sharing the "exception" bucket is what made
                # F-01 invisible: the failure counter, the backoff and the
                # metrics all described a network problem that did not
                # exist. Logged at exception level so the traceback names
                # the call site rather than only the message.
                logger.exception(
                    "scheduler: sync_with_peer rejected our arguments for "
                    "peer=%s. This is a bug in FERAL, not a peer failure.",
                    peer_id,
                )
                return self._record_failure(status, "internal_error", str(exc), trigger, m)
            except Exception as exc:
                return self._record_failure(status, "exception", str(exc), trigger, m)

            if not result.get("success"):
                return self._record_failure(
                    status, result.get("reason", "unknown"),
                    result.get("error", ""), trigger, m,
                )

            sent = int(result.get("sent", 0) or 0)
            received = int(result.get("received", 0) or 0)
            status.last_success = time.time()
            status.consecutive_failures = 0
            status.consecutive_heartbeat_misses = 0
            status.backoff_until = 0.0
            status.last_error = ""
            status.ops_sent += sent
            status.ops_received += received

            try:
                if "attempts" in m:
                    m["attempts"].labels(peer=peer_id, status="success").inc()
                if "ops_sent" in m and sent:
                    m["ops_sent"].labels(peer=peer_id).inc(sent)
                if "ops_received" in m and received:
                    m["ops_received"].labels(peer=peer_id).inc(received)
                if "lag" in m:
                    m["lag"].labels(peer=peer_id).set(0.0)
            except Exception as exc:  # pragma: no cover
                logger.debug("metric emit failed: %s", exc)

            logger.info(
                "sync ok (%s): peer=%s sent=%d received=%d trigger=%s",
                trigger, peer_id, sent, received, trigger,
            )
            return {"ok": True, "peer_id": peer_id, "sent": sent, "received": received, "trigger": trigger}

    def _record_failure(self, status: PeerStatus, reason: str, detail: str, trigger: str, metrics: dict) -> dict:
        """Bump the failure counter, compute next backoff, emit metrics."""
        status.consecutive_failures += 1
        status.last_error = f"{reason}:{detail[:200]}"
        backoff = min(
            self.config.backoff_initial_seconds * (2 ** (status.consecutive_failures - 1)),
            self.config.backoff_max_seconds,
        )
        status.backoff_until = time.time() + backoff
        try:
            if "attempts" in metrics:
                metrics["attempts"].labels(peer=status.peer_id, status=reason).inc()
            if "lag" in metrics and status.last_success:
                metrics["lag"].labels(peer=status.peer_id).set(
                    time.time() - status.last_success
                )
        except Exception as exc:  # pragma: no cover
            logger.debug("metric emit failed: %s", exc)
        logger.warning(
            "sync failed (%s): peer=%s reason=%s failures=%d backoff=%.1fs",
            trigger, status.peer_id, reason, status.consecutive_failures, backoff,
        )
        return {
            "ok": False,
            "peer_id": status.peer_id,
            "reason": reason,
            "detail": detail,
            "consecutive_failures": status.consecutive_failures,
            "backoff_seconds": backoff,
        }

    # ── Heartbeat (D12) ─────────────────────────────────────────────────

    async def heartbeat_miss(self, peer_id: str) -> None:
        """Called by the transport layer when a heartbeat ping does
        not get a reply within ``heartbeat_interval_seconds``. After
        ``heartbeat_miss_threshold`` consecutive misses, the peer is
        marked stale and bumped into the backoff path."""
        status = self._peers.setdefault(peer_id, PeerStatus(peer_id=peer_id))
        status.consecutive_heartbeat_misses += 1
        m = _metrics()
        try:
            if "heartbeat_misses" in m:
                m["heartbeat_misses"].labels(peer=peer_id).inc()
        except Exception as exc:  # pragma: no cover
            logger.debug("metric emit failed: %s", exc)
        if status.consecutive_heartbeat_misses >= self.config.heartbeat_miss_threshold:
            backoff = min(
                self.config.backoff_initial_seconds * (2 ** status.consecutive_failures),
                self.config.backoff_max_seconds,
            )
            status.backoff_until = time.time() + backoff
            logger.info(
                "heartbeat: %s reached %d misses → backoff %.1fs",
                peer_id, status.consecutive_heartbeat_misses, backoff,
            )

    async def heartbeat_reconnect(self, peer_id: str) -> None:
        """Called when a previously-stale peer answers again. Clears
        the heartbeat-miss counter and triggers an immediate sync so
        the brain catches up without waiting for the next cadence
        tick."""
        status = self._peers.setdefault(peer_id, PeerStatus(peer_id=peer_id))
        if status.consecutive_heartbeat_misses == 0 and status.backoff_until <= time.time():
            return
        status.consecutive_heartbeat_misses = 0
        status.backoff_until = 0.0
        logger.info("heartbeat: %s reconnected → immediate re-sync", peer_id)
        self._track_bg_task(
            asyncio.create_task(
                self._sync_one_peer(peer_id, trigger="heartbeat_reconnect"),
                name=f"sync-{peer_id}-rc",
            )
        )

    # ── Operator surface ────────────────────────────────────────────────

    async def sync_all_peers_now(self) -> list[dict]:
        """Trigger immediate sync against every known peer. Used by
        ``feral sync now`` and ``POST /api/sync/now``."""
        engine_peers = list(self.engine._peers.keys()) if self.engine._peers else []
        peers = list({*engine_peers, *self._manual_peers.keys()})
        results = await asyncio.gather(
            *(self._sync_one_peer(p, trigger="manual") for p in peers),
            return_exceptions=True,
        )
        out = []
        for r in results:
            if isinstance(r, Exception):
                out.append({"ok": False, "reason": "exception", "detail": str(r)})
            else:
                out.append(r)
        return out

    async def sync_one_peer_now(self, peer_id: str) -> dict:
        """Trigger immediate sync against one peer (``feral sync now <peer>``)."""
        return await self._sync_one_peer(peer_id, trigger="manual_single")

    def peer_status(self) -> dict:
        """Snapshot of all known peers' health for the dashboard."""
        return {pid: status.to_dict() for pid, status in self._peers.items()}

    def add_peer(self, peer_addr: str) -> dict:
        """Manually add a peer by ``host:port``. Mirrors the addition
        into the engine's static-peer mechanism so
        :meth:`SyncEngine.sync_with_peer` can resolve it."""
        peer_id = peer_addr
        self._manual_peers[peer_id] = peer_addr
        if self.engine and self.engine._peers is not None:
            # Inject so the engine's _peers dict carries this address —
            # sync_with_peer resolves through it.
            self.engine._peers.setdefault(peer_id, {"address": peer_addr, "source": "manual"})
        self._peers.setdefault(peer_id, PeerStatus(peer_id=peer_id))
        logger.info("scheduler: added manual peer %s", peer_addr)
        return {"ok": True, "peer_id": peer_id}

    def remove_peer(self, peer_id: str) -> dict:
        self._manual_peers.pop(peer_id, None)
        self._peers.pop(peer_id, None)
        if self.engine and self.engine._peers is not None:
            self.engine._peers.pop(peer_id, None)
        logger.info("scheduler: removed peer %s", peer_id)
        return {"ok": True, "peer_id": peer_id}

    def list_peers(self) -> list[dict]:
        engine_peers = self.engine._peers if self.engine and self.engine._peers else {}
        out = []
        seen = set()
        for pid, info in engine_peers.items():
            seen.add(pid)
            out.append({
                "peer_id": pid,
                "address": (info or {}).get("address", ""),
                "source": (info or {}).get("source", "mdns"),
                **self._peers.get(pid, PeerStatus(peer_id=pid)).to_dict(),
            })
        for pid, addr in self._manual_peers.items():
            if pid in seen:
                continue
            out.append({
                "peer_id": pid,
                "address": addr,
                "source": "manual",
                **self._peers.get(pid, PeerStatus(peer_id=pid)).to_dict(),
            })
        return out

    # ── Gauge emitters ──────────────────────────────────────────────────

    def _emit_active_peers_gauge(self, count: int) -> None:
        m = _metrics()
        try:
            if "active_peers" in m:
                m["active_peers"].set(count)
        except Exception as exc:  # pragma: no cover
            logger.debug("metric emit failed: %s", exc)

    def _emit_wal_size_gauge(self) -> None:
        m = _metrics()
        if "wal_size" not in m:
            return
        try:
            wal_path = getattr(self.engine._wal, "db_path", "")
            if wal_path and os.path.exists(wal_path):
                m["wal_size"].set(os.path.getsize(wal_path))
        except Exception as exc:  # pragma: no cover
            logger.debug("wal_size emit failed: %s", exc)


# _passphrase() was removed with the call site that was its only caller.
# It read os.environ alone, while the engine resolves env, then vault,
# then a freshly generated secret. Leaving a helper here that returns the
# weaker value is an invitation to thread it back into sync_with_peer,
# which is the bug in AUDIT-FIXES F-01. The engine owns this value.
