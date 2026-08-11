"""A fire-and-forget task must survive a garbage collection.

AUDIT-FIXES F-06. ``asyncio.create_task(...)`` whose result nobody stores
is not a style problem, it is a correctness problem. The event loop keeps
only a weak reference to a task; the strong references come from whatever
the task is currently attached to. A task that is suspended on a future
which is itself reachable only *through* that task forms a cycle with no
external root, and ``gc.collect()`` destroys the whole cycle. CPython
prints "Task was destroyed but it is pending!" and the remaining steps of
the coroutine never run.

``asyncio.ensure_future`` is the same defect wearing a different name and
is covered here too.

The behavioural tests below reproduce exactly that, against real
production call sites, using no mocking of asyncio itself:

  * the coroutine records that it started, then awaits a future it created,
  * the only *strong* reference to that future is the task's own frame
    (the test holds it through a ``WeakValueDictionary``),
  * the test drops every local reference to the task and calls
    ``gc.collect()``,
  * if the site kept a reference, the future is still alive and can be
    completed, and the coroutine's second half runs;
  * if the site did not, the future is gone with the task and the side
    effect never happens.

Verified on CPython 3.11: both tests fail against the pre-fix source,
reporting the task as collected mid-flight.

``test_no_unreferenced_create_task`` is the guard that stops new ones
appearing, in the same shape as ``tests/test_double_contracts.py``.
"""

from __future__ import annotations

import ast
import asyncio
import gc
import pathlib
import types
import weakref

import pytest

FERAL_CORE = pathlib.Path(__file__).resolve().parent.parent


# ── The collectible-coroutine probe ──────────────────────────────────────


class GcProbe:
    """A coroutine that can only finish if its task outlives ``gc.collect()``.

    ``phases`` records progress. The future the body awaits is published
    only through a ``WeakValueDictionary``, so the test's own handle on it
    does not keep the task's cycle rooted. That is what makes the
    collection deterministic rather than incidental.
    """

    def __init__(self) -> None:
        self.phases: list[str] = []
        self._weak: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

    async def body(self, *args, **kwargs) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._weak["fut"] = fut
        self.phases.append("started")
        await fut
        self.phases.append("finished")

    async def collect_and_release(self) -> bool:
        """Force a collection, then try to complete the probe.

        Returns True if the task survived and ran to completion.
        """
        assert self.phases == ["started"], (
            f"probe never reached its await: {self.phases}"
        )
        gc.collect()
        fut = self._weak.get("fut")
        if fut is None:
            return False  # the task was collected mid-flight
        fut.set_result(None)
        for _ in range(4):
            await asyncio.sleep(0)
        return self.phases == ["started", "finished"]


async def test_gc_probe_detects_an_unreferenced_task():
    """The probe must be able to fail, or the tests below prove nothing."""
    probe = GcProbe()
    asyncio.create_task(probe.body())  # deliberately unreferenced
    await asyncio.sleep(0)
    assert await probe.collect_and_release() is False


async def test_gc_probe_passes_when_a_reference_is_held():
    probe = GcProbe()
    held: set[asyncio.Task] = set()
    task = asyncio.create_task(probe.body())
    held.add(task)
    task.add_done_callback(held.discard)
    del task
    await asyncio.sleep(0)
    assert await probe.collect_and_release() is True


# ── Real call site 1: messaging channel startup (user-facing) ────────────


class _FakeChannelManager:
    def __init__(self, start_channel):
        self._channels: dict = {}
        self.start_channel = start_channel


class _FakeConfig:
    def save_credentials(self, creds):
        return None


async def test_channel_startup_task_survives_gc(monkeypatch):
    """POST /api/config/credentials must not lose the channel it started.

    The route answers ``{"ok": true, "keys_saved": [...]}`` and returns,
    dropping its frame. Before F-06 nothing else referenced the
    ``start_channel`` coroutine, so a user who pasted a valid Telegram
    token was told it was saved while the channel never came up.

    ``register_background_task`` here is the *real* ``BrainState`` method
    bound to the stand-in state, so this exercises the production registry
    rather than a reimplementation of it.
    """
    from api.routes import config as config_route
    from api.state import BrainState

    probe = GcProbe()

    class _State:
        pass

    fake = _State()
    fake.config = _FakeConfig()
    fake.vault = None
    fake.orchestrator = None
    fake.provider_catalog = None
    fake.skill_executor = None
    fake._background_tasks = set()
    fake.register_background_task = types.MethodType(
        BrainState.register_background_task, fake
    )
    fake.channel_manager = _FakeChannelManager(probe.body)

    monkeypatch.setattr(config_route, "state", fake)
    monkeypatch.delenv("FERAL_TELEGRAM_BOT_TOKEN", raising=False)

    result = await config_route.save_credentials(
        {"FERAL_TELEGRAM_BOT_TOKEN": "123456:abcdef"}
    )
    assert result["ok"] is True
    del result

    await asyncio.sleep(0)
    assert await probe.collect_and_release() is True, (
        "the Telegram channel-start task was garbage-collected mid-startup"
    )


# ── Real call site 2: supervisor event broadcast ─────────────────────────


async def test_supervisor_broadcast_task_survives_gc(tmp_path):
    """A supervisor_event scheduled from sync code must reach the client."""
    import time

    from agents.supervisor import Supervisor, SupervisorEvent, SupervisorStore

    probe = GcProbe()
    sup = Supervisor(
        store=SupervisorStore(db_path=str(tmp_path / "sup.db")),
        broadcaster=lambda payload: probe.body(payload),
    )
    sup._record(
        SupervisorEvent(
            event_id="e1",
            ts=time.time(),
            source="web",
            kind="command",
            session_id="s1",
            actor="user",
            payload_hash="h",
            payload_summary="F-06",
            decision="allowed",
            latency_ms=1,
        )
    )

    await asyncio.sleep(0)
    assert await probe.collect_and_release() is True, (
        "the supervisor_event broadcast was garbage-collected mid-flight"
    )


# ── Real call site 3: Discord gateway connect (asyncio.ensure_future) ────


async def test_discord_gateway_task_survives_gc(monkeypatch):
    """The channel's own connect task must outlive ``start()``.

    This is the layer underneath the config-route site above and it uses
    ``asyncio.ensure_future``, which the F-06 citation list did not mention
    but which schedules a Task by the same route and is referenced by the
    loop just as weakly. ``start()`` returns immediately and logs "Discord
    channel started (Gateway mode)", so a collected connect task presents
    to the user as a channel that reports healthy and never delivers a
    message.
    """
    from channels.base import DiscordChannel

    probe = GcProbe()
    ch = DiscordChannel({"bot_token": "fake-token", "enabled": True})
    monkeypatch.setattr(ch, "_gateway_connect", probe.body)

    await ch.start()
    await asyncio.sleep(0)
    assert await probe.collect_and_release() is True, (
        "the Discord gateway connect task was garbage-collected after start()"
    )
    await ch._http.aclose()


# ── Real call site 4: sync scheduler heartbeat reconnect ─────────────────


async def test_sync_scheduler_reconnect_task_survives_gc():
    """A heartbeat-triggered re-sync must not vanish while on the network.

    Not in the original F-06 citation list; found by the AST sweep below.
    """
    from memory.sync_scheduler import SyncScheduler

    probe = GcProbe()
    sched = SyncScheduler.__new__(SyncScheduler)
    sched._peers = {}
    sched._bg_tasks = set()
    sched._sync_one_peer = lambda peer_id, trigger="manual": probe.body()

    status = sched._peers.setdefault(
        "peer-1", _peer_status("peer-1")
    )
    status.consecutive_heartbeat_misses = 3

    await sched.heartbeat_reconnect("peer-1")
    await asyncio.sleep(0)
    assert await probe.collect_and_release() is True, (
        "the heartbeat re-sync task was garbage-collected mid-flight"
    )


def _peer_status(peer_id: str):
    from memory.sync_scheduler import PeerStatus

    return PeerStatus(peer_id=peer_id)


# ── The guard: no new unreferenced create_task ───────────────────────────
#
# Same trick as tests/test_double_contracts.py. A call is flagged when it
# is the whole of an expression statement, which is precisely "the Task
# object was produced and immediately dropped". Every other position
# (assignment, ``await``, ``return``, an argument to a registrar) keeps a
# reference alive for at least as long as the enclosing expression, so
# none of them is flagged.

_SKIP_DIR_PARTS = ("/build/", "/dist/", "/node_modules/", "/tests/", "/.git/")


#
# ``ensure_future`` is included deliberately. It was not in the F-06
# citation list, but it schedules a Task by exactly the same route and the
# loop references it exactly as weakly. Thirteen production sites had it,
# including the Discord gateway and Slack socket-mode connects, which are
# the layer underneath the channel-startup sites the finding does cite.

_SCHEDULING_NAMES = frozenset({"create_task", "ensure_future"})


def _create_task_base(node: ast.AST) -> str | None:
    """Return the receiver name if ``node`` schedules an unheld task."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _SCHEDULING_NAMES:
        base = func.value
        if isinstance(base, ast.Name):
            return base.id
        if isinstance(base, ast.Attribute):
            return base.attr
        return "<expr>"
    if isinstance(func, ast.Name) and func.id in _SCHEDULING_NAMES:
        return "<bare>"
    return None


def _iter_source_files():
    for path in FERAL_CORE.rglob("*.py"):
        text = str(path)
        # build/lib is a complete duplicate of the source tree; see trap 1
        # in CLAUDE.md. Counting it inflates every total by ~38%.
        if any(part in text for part in _SKIP_DIR_PARTS):
            continue
        yield path


def _unreferenced_create_tasks() -> list[str]:
    found: list[str] = []
    for path in _iter_source_files():
        try:
            source = path.read_text(errors="ignore")
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            if _create_task_base(node.value) is None:
                continue
            rel = path.relative_to(FERAL_CORE)
            snippet = lines[node.lineno - 1].strip()
            found.append(f"{rel}:{node.lineno}  {snippet}")
    return sorted(found)


def test_no_unreferenced_create_task():
    """Every create_task result must be kept, awaited, or handed to a registrar.

    Use ``state.register_background_task(...)`` where the object can reach
    it, otherwise a ``set[asyncio.Task]`` with an
    ``add_done_callback(the_set.discard)`` so the set stays bounded. See
    ``agents/orchestrator.py:_track_background_task`` and
    ``memory/store.py`` for the two worked examples.
    """
    offenders = _unreferenced_create_tasks()
    assert not offenders, (
        "asyncio task(s) created without retaining a reference; the loop "
        "holds tasks only weakly, so these can be collected mid-flight "
        "(AUDIT-FIXES F-06):\n  " + "\n  ".join(offenders)
    )


def test_the_guard_can_actually_see_a_violation(tmp_path):
    """The scanner must flag the shape it exists to flag."""
    for broken in ("asyncio.create_task(g())", "asyncio.ensure_future(g())"):
        tree = ast.parse(f"async def f():\n    {broken}\n")
        stmts = [n for n in ast.walk(tree) if isinstance(n, ast.Expr)]
        assert len(stmts) == 1
        assert _create_task_base(stmts[0].value) == "asyncio", broken

    # ...and must not flag the fixed shapes.
    for fixed in (
        "t = asyncio.create_task(g())",
        "await asyncio.create_task(g())",
        "return asyncio.create_task(g())",
        "state.register_background_task(asyncio.create_task(g()))",
        "self._track_bg_task(loop.create_task(g()))",
        "self._track_bg_task(asyncio.ensure_future(g()))",
    ):
        sub = ast.parse(f"async def f():\n    {fixed}\n")
        flagged = [
            n for n in ast.walk(sub)
            if isinstance(n, ast.Expr) and _create_task_base(n.value)
        ]
        assert not flagged, fixed


@pytest.mark.parametrize(
    "module_path,attr",
    [
        ("agents.orchestrator", "_background_tasks"),
        ("api.state", "_background_tasks"),
    ],
)
def test_reference_mechanisms_still_exist(module_path, attr):
    """The two sanctioned mechanisms must not be renamed out from under F-06."""
    import importlib

    mod = importlib.import_module(module_path)
    source = pathlib.Path(mod.__file__).read_text()
    assert attr in source
