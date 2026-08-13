"""FERAL Memory Decay — Ebbinghaus + SuperMemo SM-2 derivation.

The brain accumulates episodes forever otherwise. Beyond the engineering
cost (a multi-million-row ``episodes`` table is slow to FTS over), a
memory store that never forgets diverges from how humans actually
use memory: recently-rehearsed important facts stay sharp, while
trivial details fade. This module implements that fade.

Formula (cited inline in :meth:`MemoryDecayService.compute_decay`):

  age_hours      = (now - created_at)      / 3600
  idle_hours     = (now - last_accessed_at) / 3600  (falls back to age)
  base_decay     = exp(-decay_rate * age_hours)
  idle_penalty   = exp(-decay_rate * idle_hours * 0.5)
  access_boost   = log(1 + access_count) * access_boost_factor
  importance     = max(importance, 0.1) ** 0.5
  decay_factor   = min(base_decay * idle_penalty * importance * (1 + access_boost), 1.0)

The Ebbinghaus retention curve gives the ``exp(-rate * age)`` term.
SuperMemo SM-2 contributed the access-count-driven boost and the
importance-weighted reinforcement: rehearsed items decay slower, items
the operator marked important decay slower, items left alone for a
long time get an extra penalty on top of pure age.

Lifecycle:

* :meth:`run_once` is the single-shot sweep — recompute every active
  episode's ``decay_factor``, mark anything below ``forget_threshold``
  as forgotten, hard-delete anything whose ``forgotten_at`` is older
  than ``retention_days``. Returns a structured summary suitable for
  surfacing through ``GET /api/memory/stats``.

* :meth:`start` runs the sweep on a ``cadence_seconds`` cadence inside
  the asyncio event loop. :meth:`stop` cancels the task and awaits
  its termination.

The service is constructed by ``BrainState._boot_subsystems`` when
``settings.memory.decay.enabled`` is true; otherwise it never wakes.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from memory.store import MemoryStore

logger = logging.getLogger("feral.memory.decay")


def _metrics() -> dict:
    """Lazy accessor for the central Prometheus metric handles.

    The metrics live on ``observability.metrics.REGISTRY`` (defined
    there so test_metrics_registry.py can audit dashboard parity).
    Importing inline keeps decay.py importable when
    ``prometheus_client`` is missing — the metric calls then
    silently no-op.
    """
    try:
        from observability import metrics as _m

        return {
            "sweeps_total": _m.MEMORY_DECAY_SWEEPS_TOTAL,
            "sweep_duration_seconds": _m.MEMORY_DECAY_SWEEP_DURATION_SECONDS,
            "episodes_active": _m.MEMORY_EPISODES_ACTIVE,
            "episodes_forgotten": _m.MEMORY_EPISODES_FORGOTTEN,
            "episodes_hard_deleted_total": _m.MEMORY_EPISODES_HARD_DELETED_TOTAL,
        }
    except Exception:  # pragma: no cover — observability optional
        return {}


@dataclass(frozen=True)
class DecayConfig:
    """Tunables for :class:`MemoryDecayService`.

    Defaults mirror ``settings.memory.decay`` in
    ``config/loader.py``. Operators override via ``settings.json``.
    """

    enabled: bool = True
    cadence_seconds: float = 3600.0
    decay_rate: float = 0.001
    forget_threshold: float = 0.05
    access_boost_factor: float = 0.1
    retention_days: float = 365.0

    @classmethod
    def from_settings(cls, settings: dict) -> "DecayConfig":
        cfg = (settings.get("memory") or {}).get("decay") or {}
        return cls(
            enabled=bool(cfg.get("enabled", cls.enabled)),
            cadence_seconds=float(cfg.get("cadence_seconds", cls.cadence_seconds)),
            decay_rate=float(cfg.get("decay_rate", cls.decay_rate)),
            forget_threshold=float(cfg.get("forget_threshold", cls.forget_threshold)),
            access_boost_factor=float(cfg.get("access_boost_factor", cls.access_boost_factor)),
            retention_days=float(cfg.get("retention_days", cls.retention_days)),
        )


def compute_decay(
    *,
    now: float,
    created_at: float,
    last_accessed_at: float,
    access_count: int,
    importance: float,
    decay_rate: float,
    access_boost_factor: float,
) -> float:
    """Pure formula used by the sweep + the tests.

    Pulled out as a free function so the decay math is unit-testable
    without spinning up a MemoryStore. The inverse-time scaling is
    deliberate: ``decay_rate`` is "fraction lost per hour of age",
    so ``decay_rate=0.001`` gives ≈ 0.9 retention after 100 hours of
    untouched age (matching the documented Ebbinghaus reference).
    """
    age_hours = max(now - created_at, 0.0) / 3600.0
    # An episode never accessed since creation is still "as fresh as
    # when it was written" for the idle-penalty purposes — the idle
    # term must not double-count the age term.
    idle_seconds = now - last_accessed_at if last_accessed_at > 0 else now - created_at
    idle_hours = max(idle_seconds, 0.0) / 3600.0

    base_decay = math.exp(-decay_rate * age_hours)
    idle_penalty = math.exp(-decay_rate * idle_hours * 0.5)
    access_boost = math.log(1.0 + max(access_count, 0)) * access_boost_factor
    importance_keep = max(importance, 0.1) ** 0.5

    raw = base_decay * idle_penalty * importance_keep * (1.0 + access_boost)
    return min(max(raw, 0.0), 1.0)


_FORGOTTEN_COLUMNS = (
    "id, event_type, summary, importance, decay_factor, created_at, forgotten_at"
)


def forgotten_query(limit: int = 50, *, query: str = "") -> tuple[str, list]:
    """SQL + params listing forgotten episodes, newest-forgotten first.

    One definition, two callers: :meth:`MemoryDecayService.list_forgotten`
    runs it on the brain's async pool, ``feral memory forgotten`` runs it
    read-only against the database file so an operator can recover an
    episode without a running brain. Shared rather than duplicated
    because a recovery path that disagrees with the service about what
    "forgotten" means is worse than no recovery path.

    Deliberately LIKE and not FTS: ``episodes_fts`` is reached through
    the same joins that exclude forgotten rows, and a recovery tool must
    not depend on the index it is recovering from.
    """
    sql = (
        f"SELECT {_FORGOTTEN_COLUMNS} FROM episodes WHERE forgotten_at IS NOT NULL"
    )
    params: list = []
    if query:
        sql += " AND (summary LIKE ? OR detail LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    sql += " ORDER BY forgotten_at DESC, created_at DESC LIMIT ?"
    params.append(limit)
    return sql, params


def forgotten_row_to_dict(row) -> dict:
    """Shape one row from :func:`forgotten_query` for a caller."""
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "summary": row["summary"],
        "importance": row["importance"],
        "decay_factor": row["decay_factor"],
        "created_at": row["created_at"],
        "forgotten_at": row["forgotten_at"],
    }


class MemoryDecayService:
    """Background sweeper for episode decay + forgetting.

    Construction is cheap; the service holds a reference to the
    :class:`MemoryStore` and a config. Nothing happens until
    :meth:`start` is awaited inside the running event loop.

    The sweep is a single async transaction per pass: it pulls every
    active (``forgotten_at IS NULL``) episode, recomputes
    ``decay_factor`` in Python, writes back the changed rows in one
    ``UPDATE`` statement set, then runs the hard-delete pass. The
    work is bounded by the row count, not by the pool size, so a
    large brain stays responsive — the sweep runs on a single
    aiosqlite connection while the rest of the pool serves chat /
    voice / sync.

    Tests pin this contract:

    * Pure :func:`compute_decay` matches the documented formula.
    * Sweep updates ``decay_factor`` and sets ``forgotten_at`` for
      rows below ``forget_threshold``.
    * Access tracking (separate code path on ``MemoryStore``) bumps
      ``last_accessed_at`` + ``access_count``.
    * Default ``episode_search`` excludes forgotten rows.
    * ``include_forgotten=True`` includes them.
    * Hard-delete after ``retention_days`` past ``forgotten_at``.
    * Reruns are idempotent — running ``run_once`` twice in a row
      returns the same ``decay_factor`` for every row.
    * ``enabled=False`` skips the loop completely.
    * Concurrent ``episode_search`` during a sweep cannot deadlock
      (verified by the perf benchmark — the pool stays available).
    """

    def __init__(self, store: "MemoryStore", config: Optional[DecayConfig] = None):
        self.store = store
        self.config = config or DecayConfig()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin the background sweep loop. No-op if disabled or
        already running."""
        if not self.config.enabled:
            logger.info("MemoryDecayService disabled by settings; not starting")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="memory-decay-sweep")
        logger.info(
            "MemoryDecayService started: cadence=%.0fs, rate=%g, threshold=%g, retention=%g days",
            self.config.cadence_seconds,
            self.config.decay_rate,
            self.config.forget_threshold,
            self.config.retention_days,
        )

    async def stop(self) -> None:
        """Cancel the sweep task and await its termination. Safe to
        call when the service was never started."""
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
        """Run :meth:`run_once` every ``cadence_seconds`` until
        :meth:`stop` is called.

        Exceptions inside ``run_once`` are logged and the loop
        continues — a transient SQLite ``database is locked`` from a
        chaos test must not silently kill the sweeper for the rest
        of the process lifetime.
        """
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.exception("decay sweep failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.cadence_seconds
                )
            except asyncio.TimeoutError:
                pass

    # ── Single-shot sweep ───────────────────────────────────────────────

    async def run_once(self) -> dict:
        """One full sweep. Returns:

        ``{
            "ok": True,
            "scanned": N,
            "updated": M,
            "newly_forgotten": K,
            "hard_deleted": J,
            "duration_seconds": float,
        }``
        """
        if not self.config.enabled:
            return {"ok": False, "reason": "disabled"}

        t0 = time.perf_counter()
        now = time.time()
        retention_cutoff = now - (self.config.retention_days * 86400.0)

        conn = await self.store._conn()
        try:
            # 1) Recompute decay_factor + flag newly-forgotten rows.
            async with conn.execute(
                "SELECT id, created_at, last_accessed_at, access_count, importance, "
                "decay_factor, forgotten_at FROM episodes WHERE forgotten_at IS NULL"
            ) as cur:
                rows = await cur.fetchall()

            scanned = len(rows)
            new_factors: list[tuple[float, str]] = []
            newly_forgotten: list[str] = []
            for r in rows:
                eid = r["id"]
                new_decay = compute_decay(
                    now=now,
                    created_at=float(r["created_at"] or 0.0),
                    last_accessed_at=float(r["last_accessed_at"] or 0.0),
                    access_count=int(r["access_count"] or 0),
                    importance=float(r["importance"] or 0.5),
                    decay_rate=self.config.decay_rate,
                    access_boost_factor=self.config.access_boost_factor,
                )
                # Skip writes when the value hasn't moved meaningfully —
                # keeps WAL noise + sync log churn low on quiet brains.
                if abs(new_decay - float(r["decay_factor"] or 1.0)) >= 1e-4:
                    new_factors.append((new_decay, eid))
                if new_decay < self.config.forget_threshold:
                    newly_forgotten.append(eid)

            if new_factors:
                await conn.executemany(
                    "UPDATE episodes SET decay_factor = ? WHERE id = ?",
                    new_factors,
                )
            if newly_forgotten:
                await conn.executemany(
                    "UPDATE episodes SET forgotten_at = ? WHERE id = ? AND forgotten_at IS NULL",
                    [(now, eid) for eid in newly_forgotten],
                )

            # 2) Hard-delete past the retention window. Done in the same
            # transaction so a crash mid-sweep doesn't strand
            # forgotten-but-undeleted rows. FTS shadow rows go too.
            async with conn.execute(
                "SELECT id, rowid FROM episodes WHERE forgotten_at IS NOT NULL AND forgotten_at < ?",
                (retention_cutoff,),
            ) as cur:
                doomed = await cur.fetchall()
            hard_deleted = len(doomed)
            if doomed:
                ids = [d["id"] for d in doomed]
                rowids = [d["rowid"] for d in doomed]
                placeholders = ",".join("?" * len(ids))
                await conn.execute(
                    f"DELETE FROM episodes_fts WHERE rowid IN ({','.join('?' * len(rowids))})",
                    rowids,
                )
                await conn.execute(
                    f"DELETE FROM memory_chunks WHERE source_table = 'episodes' AND source_id IN ({placeholders})",
                    ids,
                )
                await conn.execute(
                    f"DELETE FROM episodes WHERE id IN ({placeholders})",
                    ids,
                )

            await conn.commit()

            # Announce the hard delete so it replicates. Without this, a
            # peer that still holds the original ``insert`` operation
            # re-sends it on the next handshake and the episode this
            # brain spent a year deciding to forget comes straight back:
            # ``get_changes_since`` selects purely on HLC, and the WAL on
            # the real store held 14,807 episode inserts and zero deletes
            # (audit 2026-08-12), so nothing could ever counter them.
            # Emitted after the commit so a delete that did not land
            # locally is never announced, and per-id rather than as a
            # batch because ``SyncOperation`` is keyed on one row_id.
            for eid in (d["id"] for d in doomed):
                await self.store._log_sync_async(
                    "episodes", "delete", eid, {"id": eid},
                )

            # 3) Refresh the active/forgotten gauges. Two cheap counts
            # vs the alternative of computing them in step 1 (which
            # would either need a second pass or row-by-row gauge
            # updates).
            async with conn.execute(
                "SELECT COUNT(*) AS n FROM episodes WHERE forgotten_at IS NULL"
            ) as cur:
                active = (await cur.fetchone())["n"]
            async with conn.execute(
                "SELECT COUNT(*) AS n FROM episodes WHERE forgotten_at IS NOT NULL"
            ) as cur:
                forgotten = (await cur.fetchone())["n"]
        finally:
            await self.store._release(conn)

        duration = time.perf_counter() - t0
        m = _metrics()
        try:
            if "episodes_active" in m:
                m["episodes_active"].set(active)
            if "episodes_forgotten" in m:
                m["episodes_forgotten"].set(forgotten)
            if hard_deleted and "episodes_hard_deleted_total" in m:
                m["episodes_hard_deleted_total"].inc(hard_deleted)
            if "sweeps_total" in m:
                m["sweeps_total"].inc()
            if "sweep_duration_seconds" in m:
                m["sweep_duration_seconds"].observe(duration)
        except Exception as exc:  # pragma: no cover — metrics never break the sweep
            logger.debug("decay metrics emit failed: %s", exc)

        logger.debug(
            "decay sweep: scanned=%d updated=%d newly_forgotten=%d hard_deleted=%d duration=%.3fs",
            scanned, len(new_factors), len(newly_forgotten), hard_deleted, duration,
        )
        return {
            "ok": True,
            "scanned": scanned,
            "updated": len(new_factors),
            "newly_forgotten": len(newly_forgotten),
            "hard_deleted": hard_deleted,
            "active": active,
            "forgotten": forgotten,
            "duration_seconds": duration,
        }

    # ── Operator surface ────────────────────────────────────────────────

    async def forget(self, episode_id: str) -> dict:
        """Mark a single episode as forgotten *now*, regardless of its
        current decay_factor. Operator-driven escape hatch for
        privacy / mistaken-input cases.
        """
        conn = await self.store._conn()
        try:
            await conn.execute(
                "UPDATE episodes SET forgotten_at = ?, decay_factor = 0.0 "
                "WHERE id = ? AND forgotten_at IS NULL",
                (time.time(), episode_id),
            )
            await conn.commit()
            async with conn.execute(
                "SELECT id, forgotten_at FROM episodes WHERE id = ?", (episode_id,)
            ) as cur:
                row = await cur.fetchone()
        finally:
            await self.store._release(conn)
        if row is None:
            return {"ok": False, "reason": "not_found", "id": episode_id}
        return {"ok": True, "id": episode_id, "forgotten_at": row["forgotten_at"]}

    async def list_forgotten(self, limit: int = 50, *, query: str = "") -> list[dict]:
        """Forgotten episodes, newest-forgotten first, with their ids.

        :meth:`recall` takes an episode id, and until this existed there
        was no way to obtain one. Every read path on ``MemoryStore``
        filters ``forgotten_at IS NULL`` by default and the CLI exposed
        no ``include_forgotten`` switch, so on the real store of
        2026-08-12 the 3,677 forgotten episodes (30% of 12,300, among
        them 133 ``user_command`` rows the user typed by hand) were
        excluded from search and unreachable by recall. That is the
        difference between "forgotten" and "lost", and the promise this
        tier makes is the former.

        ``query`` is an optional substring filter over summary/detail.
        See :func:`forgotten_query` for why it is LIKE and not FTS.
        """
        sql, params = forgotten_query(limit, query=query)
        conn = await self.store._conn()
        try:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        finally:
            await self.store._release(conn)
        return [forgotten_row_to_dict(r) for r in rows]

    async def recall(self, episode_id: str) -> dict:
        """Reverse a ``forget`` (clears ``forgotten_at`` and bumps
        ``last_accessed_at`` so the freshly-recalled row decays slowly
        again). Hard-deleted rows cannot be recalled — they're gone.
        """
        now = time.time()
        conn = await self.store._conn()
        try:
            await conn.execute(
                "UPDATE episodes SET forgotten_at = NULL, last_accessed_at = ?, "
                "access_count = access_count + 1, decay_factor = 1.0 "
                "WHERE id = ?",
                (now, episode_id),
            )
            await conn.commit()
            async with conn.execute(
                "SELECT id, forgotten_at FROM episodes WHERE id = ?", (episode_id,)
            ) as cur:
                row = await cur.fetchone()
        finally:
            await self.store._release(conn)
        if row is None:
            return {"ok": False, "reason": "not_found_or_hard_deleted", "id": episode_id}
        return {"ok": True, "id": episode_id, "forgotten_at": row["forgotten_at"]}

    async def stats(self) -> dict:
        """Snapshot the active / forgotten counts + sweep cadence
        config for the dashboard / metrics route."""
        conn = await self.store._conn()
        try:
            async with conn.execute(
                "SELECT "
                " (SELECT COUNT(*) FROM episodes WHERE forgotten_at IS NULL) AS active, "
                " (SELECT COUNT(*) FROM episodes WHERE forgotten_at IS NOT NULL) AS forgotten"
            ) as cur:
                row = await cur.fetchone()
        finally:
            await self.store._release(conn)
        return {
            "enabled": self.config.enabled,
            "cadence_seconds": self.config.cadence_seconds,
            "decay_rate": self.config.decay_rate,
            "forget_threshold": self.config.forget_threshold,
            "retention_days": self.config.retention_days,
            "episodes_active": row["active"],
            "episodes_forgotten": row["forgotten"],
        }
