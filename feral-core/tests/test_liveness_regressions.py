"""Blocking / liveness regressions.

Five independent defects, each of which makes a subsystem stop doing its
job while every surface keeps reporting that it is fine:

D1  ``CronService._loop`` has no exception guard, so ONE raising job kills
    the scheduler thread. Every routine stops permanently, ``start()`` has
    already returned, and ``/api/routines`` keeps rendering the routines as
    enabled with a ``next_run`` receding into the past.

D2  The CuteBot serial ``_io_lock`` is unfair: an emergency ``halt`` queues
    behind a 1-second telemetry read and behind the very command it exists
    to abort. ``priority=2 if capability_id == "halt"`` never reaches the
    lock, so it changes nothing.

D3  The Whoop durable sync holds its lock across three vendor HTTP calls,
    and the throttle that is supposed to keep the user out of that window
    reads ``_last_sync_at`` before the window is claimed. "How did I
    sleep" therefore waits out a background sync and then runs a second
    full one.

D4  Cron turns run on a throwaway event loop that is closed the instant
    ``handle_command`` returns, killing every background task the turn
    scheduled. Compaction is the worst case: ``_compaction_inflight`` is
    set at the top of the task body and only cleared in its ``finally``,
    so a killed task leaves the flag True and that session never compacts
    again for the life of the process.

D6  ``conversation_append`` is a read-modify-write across two awaits with
    no lock, so concurrent appends overwrite each other.

Every test here asserts BEHAVIOUR (the thread is still alive and later
jobs fire; halt completes while telemetry is in flight; the user's query
returns without waiting for a sync; compaction finishes under a throwaway
loop; concurrent appends all persist), never code shape.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.scheduler import CronService, JobType  # noqa: E402


# ─────────────────────────────────────────────────────────────
# D1 — one raising job must not kill the scheduler thread
# ─────────────────────────────────────────────────────────────


def _force_due(svc: CronService, job_id: int, seconds_ago: float = 10.0) -> None:
    """Backdate a job's next_run so it is missed / due right now."""
    with svc._lock:
        svc._conn.execute(
            "UPDATE scheduled_jobs SET next_run = ? WHERE id = ?",
            (time.time() - seconds_ago, job_id),
        )
        svc._conn.commit()


@pytest.fixture
def cron(tmp_path):
    svc = CronService(db_path=str(tmp_path / "sched.db"))
    # The loop sleeps up to 30s between scans when nothing is near due.
    # Shrink the ceiling so a test that backdates a job sees the next scan
    # in about a second instead of half a minute. The 1s floor inside
    # ``_poll_interval`` still applies, so this changes nothing else.
    svc._MAX_POLL_SECONDS = 1.0
    yield svc
    try:
        svc.stop()
    finally:
        svc.close()


def test_a_raising_job_does_not_kill_the_scheduler_thread(cron):
    """Three jobs are due. The first one raises. The other two must fire,
    the thread must still be alive, and a job that comes due later must
    still fire."""
    fired: list[int] = []
    boom_on: set[int] = set()

    def callback(job):
        fired.append(job.id)
        if job.id in boom_on:
            # Exactly what ``record_run_start`` raises when the routines DB
            # is momentarily contended: a raw sqlite3 INSERT outside any
            # try in ``execute_routine_job``.
            raise sqlite3.OperationalError("database is locked")

    jobs = [
        cron.create_job(JobType.SCHEDULED, "every 10m", f"job-{i}", {}, "")
        for i in range(3)
    ]
    for job in jobs:
        _force_due(cron, job.id)
    boom_on.add(jobs[0].id)

    cron.start(callback)

    deadline = time.time() + 8.0
    while time.time() < deadline and len(fired) < 3:
        time.sleep(0.05)

    assert cron._thread is not None and cron._thread.is_alive(), (
        "the scheduler thread died on the first raising job; every routine "
        "on this brain has stopped permanently"
    )
    assert set(fired) == {j.id for j in jobs}, (
        f"only {fired} fired; a raising job swallowed the rest of the batch"
    )

    # A later due window must still produce work.
    later = cron.create_job(JobType.SCHEDULED, "every 10m", "later", {}, "")
    _force_due(cron, later.id)
    deadline = time.time() + 8.0
    while time.time() < deadline and later.id not in fired:
        time.sleep(0.05)
    assert later.id in fired, "the scheduler stopped firing jobs after the raise"


def test_a_dead_scheduler_is_detectable_and_restartable(cron):
    """A scheduler that is not running must say so, and must be able to
    come back — silently reporting healthy is how this went unnoticed."""
    cron.start(lambda job: None)
    assert cron.is_running() is True

    cron.stop()
    assert cron.is_running() is False

    fired: list[int] = []
    cron.start(lambda job: fired.append(job.id))
    assert cron.is_running() is True

    job = cron.create_job(JobType.SCHEDULED, "every 10m", "after-restart", {}, "")
    _force_due(cron, job.id)
    deadline = time.time() + 8.0
    while time.time() < deadline and job.id not in fired:
        time.sleep(0.05)
    assert job.id in fired, "a restarted scheduler did not resume firing"


def test_ensure_running_revives_a_scheduler_whose_thread_died(cron):
    """Belt and braces: even if the loop thread is gone, the read paths
    that report on routines must be able to bring it back."""
    fired: list[int] = []
    cron.start(lambda job: fired.append(job.id))

    # Simulate the historical failure: the loop thread is gone while the
    # service still believes it is scheduled.
    cron._stop.set()
    cron._thread.join(timeout=35.0)
    cron._stop.clear()
    assert cron.is_running() is False

    assert cron.ensure_running() is True
    assert cron.is_running() is True

    job = cron.create_job(JobType.SCHEDULED, "every 10m", "revived", {}, "")
    _force_due(cron, job.id)
    deadline = time.time() + 8.0
    while time.time() < deadline and job.id not in fired:
        time.sleep(0.05)
    assert job.id in fired, "ensure_running() did not actually restart the loop"


# ─────────────────────────────────────────────────────────────
# D2 — an emergency halt must preempt device I/O
# ─────────────────────────────────────────────────────────────


class _SerialBot:
    """QtBot stand-in that behaves like a real serial link: a blocking
    call, and a hard assertion that no two calls ever overlap on the port.
    """

    POLL_UNIT = 1.0

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._in_io = threading.Lock()
        self.overlaps = 0
        self.halted_at: float | None = None

    def _enter(self, name: str) -> None:
        if not self._in_io.acquire(blocking=False):
            self.overlaps += 1
            self._in_io.acquire()
        self.calls.append(name)

    def _exit(self) -> None:
        self._in_io.release()

    @staticmethod
    def available() -> bool:
        return True

    def status(self) -> dict:
        self._enter("status")
        try:
            time.sleep(0.01)
            return {
                "online": True, "mode": "idle", "state": "ok", "sonar_cm": 10.0,
                "line_left": False, "line_right": False, "battery": True,
            }
        finally:
            self._exit()

    def poll_events(self, seconds: float = 1.0) -> list[dict]:
        self._enter("poll_events")
        try:
            time.sleep(seconds)
            return []
        finally:
            self._exit()

    def execute(self, command: str, **params):
        self._enter(command)
        try:
            time.sleep(0.30)  # a slow closed-loop actuator command
            return {"ok": True, "command": command, **params}
        finally:
            self._exit()

    def halt(self):
        self._enter("halt")
        try:
            self.halted_at = time.monotonic()
            return {"ok": True, "command": "halt"}
        finally:
            self._exit()

    def close(self) -> None:
        pass


def _cutebot(bot):
    from hardware.adapters.cutebot import CuteBotAdapter

    adapter = CuteBotAdapter(bot=bot)
    adapter._connected = True
    return adapter


def _action(capability_id: str, **params):
    from hardware.protocol import HUPAction, HUPActionType

    return HUPAction(
        device_id="cutebot-usb-0",
        capability_id=capability_id,
        action_type=HUPActionType.EXECUTE,
        parameters=params,
    )


@pytest.mark.asyncio
async def test_halt_completes_while_a_telemetry_read_is_in_flight():
    """The telemetry loop calls ``poll_events(1.0)`` once a second, forever.
    An emergency stop must not sit behind a whole one-second serial read."""
    bot = _SerialBot()
    adapter = _cutebot(bot)

    telemetry = asyncio.create_task(adapter.poll_events(1.0))
    await asyncio.sleep(0.05)  # the read is genuinely in flight

    t0 = time.monotonic()
    result = await adapter.execute(_action("halt"))
    waited = time.monotonic() - t0

    await telemetry

    assert result.status == "success"
    assert "halt" in bot.calls
    assert waited < 0.40, (
        f"emergency halt waited {waited:.3f}s behind a 1s telemetry read; "
        "the io lock has no priority path"
    )
    assert bot.overlaps == 0, "halt raced another caller onto the serial port"


@pytest.mark.asyncio
async def test_halt_jumps_ahead_of_already_queued_commands():
    """``priority=2 if capability_id == 'halt'`` must actually mean
    something. A halt issued while a telemetry read holds the port and five
    normal commands are already queued must reach the device first, and
    within a bound a human would accept from an emergency stop."""
    bot = _SerialBot()
    adapter = _cutebot(bot)

    # The telemetry loop owns the port for a full second, exactly as
    # ``start_telemetry_loop`` does once a second forever.
    holder = asyncio.create_task(adapter.poll_events(1.0))
    await asyncio.sleep(0.05)

    queued = [
        asyncio.create_task(adapter.execute(_action("set_lights", r=1, g=2, b=3)))
        for _ in range(5)
    ]
    await asyncio.sleep(0.10)

    t0 = time.monotonic()
    halt = asyncio.create_task(adapter.execute(_action("halt")))
    await halt
    waited = time.monotonic() - t0
    await asyncio.gather(holder, *queued)

    order = [c for c in bot.calls if c != "status"]
    assert "halt" in order
    halt_index = order.index("halt")
    queued_indexes = [i for i, c in enumerate(order) if c == "set_lights"]
    assert waited < 0.50, (
        f"emergency halt took {waited:.3f}s with 1.0s of telemetry in flight "
        f"and {len(queued)} commands queued ahead of it"
    )
    assert halt_index < min(queued_indexes), (
        f"halt ran at position {halt_index} of {order}; it queued behind the "
        "commands it exists to abort"
    )
    assert bot.overlaps == 0, "halt raced another caller onto the serial port"


@pytest.mark.asyncio
async def test_normal_device_io_is_still_mutually_exclusive():
    """Preemption must not make the serial port unsafe."""
    bot = _SerialBot()
    adapter = _cutebot(bot)

    await asyncio.gather(
        adapter.poll_events(0.2),
        adapter.get_state(),
        adapter.execute(_action("drive", left=10, right=10)),
        adapter.get_state(),
        adapter.poll_events(0.2),
    )
    assert bot.overlaps == 0, "two callers were inside the serial port at once"


# ─────────────────────────────────────────────────────────────
# D3 — "how did I sleep" must not wait out a background sync
# ─────────────────────────────────────────────────────────────


class _SlowWhoop:
    """Vendor client whose every call costs real wall-clock time."""

    def __init__(self, delay: float = 0.30):
        self.connected = True
        self.delay = delay
        self.calls = 0

    async def _slow(self) -> dict:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {"success": True, "data": []}

    async def get_recovery(self):
        return await self._slow()

    async def get_sleep(self, days: int = 7):
        return await self._slow()

    async def get_cycles(self, days: int = 7):
        return await self._slow()


def _sync_service(whoop, **kw):
    from integrations.health_sync import WhoopDurableSync

    kw.setdefault("interval_s", 900.0)
    kw.setdefault("lookback_days", 7)
    return WhoopDurableSync(whoop=whoop, **kw)


@pytest.mark.asyncio
async def test_user_query_does_not_wait_for_an_inflight_background_sync():
    whoop = _SlowWhoop(0.30)          # 3 sequential calls ≈ 0.9s
    service = _sync_service(whoop)

    background = asyncio.create_task(service.sync_once())
    await asyncio.sleep(0.05)         # the background sync is in flight

    t0 = time.monotonic()
    await service.maybe_sync()
    waited = time.monotonic() - t0

    await background

    assert waited < 0.20, (
        f"the user's health query parked on the sync lock for {waited:.3f}s; "
        "this runs inside the turn, so it also stalls the next message"
    )


@pytest.mark.asyncio
async def test_the_throttle_actually_throttles_a_concurrent_sync():
    """``_last_sync_at`` used to be written only AFTER the vendor calls, so
    a user arriving mid-sync passed the throttle and ran a second full
    round-trip against the vendor."""
    whoop = _SlowWhoop(0.30)
    service = _sync_service(whoop)

    background = asyncio.create_task(service.sync_once())
    await asyncio.sleep(0.05)
    await service.maybe_sync()
    await background

    assert whoop.calls == 3, (
        f"{whoop.calls} vendor calls for one sync window; the user's query "
        "ran a second full sync on top of the background one"
    )


@pytest.mark.asyncio
async def test_health_summary_returns_promptly_during_a_background_sync():
    """The real user path: ``get_health_summary`` → ``_maybe_sync_durable``."""
    from integrations.health_platforms import HealthAggregator

    whoop = _SlowWhoop(0.30)
    service = _sync_service(whoop)
    aggregator = HealthAggregator(sync_provider=lambda: service)

    background = asyncio.create_task(service.sync_once())
    await asyncio.sleep(0.05)

    t0 = time.monotonic()
    await aggregator.get_health_summary()
    waited = time.monotonic() - t0

    await background

    assert waited < 0.20, (
        f'"how did I sleep" took {waited:.3f}s waiting on a mirror refresh '
        "the user did not ask for"
    )


# ─────────────────────────────────────────────────────────────
# D4 — a cron turn must not silently lose its background work
# ─────────────────────────────────────────────────────────────


def _bare_orchestrator(compact_impl):
    """An orchestrator with only the fields _maybe_auto_compact touches."""
    from unittest.mock import AsyncMock, MagicMock

    from agents.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.conversation_history = {}
    orch._turns_since_compaction = {}
    orch._compaction_inflight = {}
    orch._session_locks = {}
    orch._background_tasks = set()
    orch._last_turn_at = 0.0
    orch._owning_loop = None
    orch.llm = MagicMock()
    orch.memory = MagicMock()
    orch.memory.compact_session = AsyncMock(side_effect=compact_impl)
    return orch


class _CronOrchestrator:
    """Stands in for the orchestrator on the cron path: a turn that ends by
    scheduling compaction, exactly as ``_finish_turn`` does."""

    def __init__(self, inner):
        self._inner = inner
        self.turns = 0

    async def handle_command(self, session_id, prompt, context=None):
        self.turns += 1
        self._inner._maybe_auto_compact(session_id)
        return {"ok": True}

    async def drain_background_tasks(self, timeout: float = 5.0):
        await self._inner.drain_background_tasks(timeout=timeout)


@pytest.fixture
def cron_server_env(tmp_path):
    import api.server as server

    fd, cron_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    cron = CronService(db_path=cron_path)

    saved = {
        k: getattr(server.state, k, None)
        for k in ("cron_service", "scheduler", "orchestrator", "cron_cost_guard",
                  "skill_registry")
    }
    server.state.cron_service = cron
    server.state.scheduler = cron
    server.state.orchestrator = None
    server.state.cron_cost_guard = None

    yield {"cron": cron, "server": server}

    for k, v in saved.items():
        setattr(server.state, k, v)
    cron.close()
    os.unlink(cron_path)


def test_cron_turn_completes_the_compaction_it_scheduled(cron_server_env):
    """A routine's turn schedules compaction as a background task. Closing
    the turn's event loop the moment ``handle_command`` returns destroys
    that task mid-flight, leaving ``_compaction_inflight`` stuck True — so
    the session never compacts again for the life of the process."""
    server = cron_server_env["server"]
    cron = cron_server_env["cron"]
    sid = "routine-session"

    async def compact(session_id, history, llm=None):
        await asyncio.sleep(0.05)
        return {
            "compacted": True,
            "history": [{"role": "system", "content": "[summary]"}],
            "episode_id": "ep1",
        }

    inner = _bare_orchestrator(compact)
    inner.conversation_history[sid] = [
        {"role": "user", "content": f"turn {i}"} for i in range(40)
    ]
    inner._turns_since_compaction[sid] = 999
    server.state.orchestrator = _CronOrchestrator(inner)

    job = cron.create_job(
        JobType.CUSTOM, "every 30m", "nl", {"action_text": "check the plants"}, sid,
    )
    server.execute_routine_job(job)

    assert inner._compaction_inflight.get(sid) is False, (
        "the compaction task was destroyed when the cron turn's throwaway "
        "event loop closed; _compaction_inflight is stuck True and this "
        "session will never compact again"
    )
    assert inner.conversation_history[sid] == [
        {"role": "system", "content": "[summary]"}
    ], "the compaction never landed"


def test_cron_turn_runs_on_the_brains_own_loop_when_there_is_one(cron_server_env):
    """In production the orchestrator's owning loop is pinned at boot. A
    cron turn must run there, so the memory pool and every task it spawns
    keep their loop affinity, instead of on a per-job throwaway loop."""
    server = cron_server_env["server"]
    cron = cron_server_env["cron"]

    ready = threading.Event()
    box: dict = {}

    def _run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
    ready.wait(5.0)
    loop = box["loop"]

    class _Orch:
        _owning_loop = loop

        def __init__(self):
            self.ran_on = None

        async def handle_command(self, session_id, prompt, context=None):
            self.ran_on = asyncio.get_running_loop()
            return {"ok": True}

    orch = _Orch()
    server.state.orchestrator = orch

    job = cron.create_job(JobType.CUSTOM, "every 30m", "nl", {"prompt": "hi"}, "s")
    try:
        server.execute_routine_job(job)
        assert orch.ran_on is loop, (
            "the cron turn ran on a throwaway loop even though the brain's "
            "own loop was available"
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
        loop.close()


def test_a_contended_run_record_does_not_skip_the_routine(cron_server_env):
    """``record_run_start`` is a raw sqlite3 INSERT + commit that sat
    outside every try. One "database is locked" there took the whole
    dispatch out, and (before D1) the scheduler thread with it. History
    bookkeeping must never decide whether the routine runs."""
    server = cron_server_env["server"]
    cron = cron_server_env["cron"]

    def _boom(job_id):
        raise sqlite3.OperationalError("database is locked")

    cron.record_run_start = _boom

    ran: list[str] = []

    class _Orch:
        _owning_loop = None

        async def handle_command(self, session_id, prompt, context=None):
            ran.append(prompt)
            return {"ok": True}

    server.state.orchestrator = _Orch()
    job = cron.create_job(
        JobType.CUSTOM, "every 30m", "nl", {"prompt": "water the plants"}, "s",
    )
    server.execute_routine_job(job)

    assert ran == ["water the plants"], (
        "a contended run-history INSERT stopped the routine from running"
    )


# ─────────────────────────────────────────────────────────────
# D6 — concurrent conversation appends must all persist
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_conversation_appends_all_persist(tmp_path):
    from memory.store import MemoryStore

    store = MemoryStore(db_path=str(tmp_path / "mem.db"))
    cid = "voice:sess-1"
    await store.conversation_save(cid, [], title="seed")

    n = 20
    await asyncio.gather(*[
        store.conversation_append(cid, "user", f"msg-{i}") for i in range(n)
    ])

    convo = await store.conversation_get(cid)
    contents = sorted(m["content"] for m in convo["messages"])
    assert contents == sorted(f"msg-{i}" for i in range(n)), (
        f"{len(contents)} of {n} concurrent appends survived; "
        "conversation_append is a read-modify-write with no lock"
    )
    await store.aclose()
