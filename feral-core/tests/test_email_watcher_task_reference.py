"""B12: an inbound email handler must not be garbage-collected mid-flight.

``EmailWatcher._process_message`` dispatched the handler with

    loop.call_soon_threadsafe(asyncio.create_task, self._on_email(incoming))

and discarded the Task the callback returned. The event loop holds tasks
only weakly (AUDIT-FIXES F-06), so a task suspended on a future that is
reachable only through its own frame is an unrooted cycle and
``gc.collect()`` destroys it.

Email is the worst surface for that defect because the work is already
irreversible by the time the task is scheduled:

  * ``_process_message`` FETCHes at :180, which marks the message
    ``\\Seen`` on the server,
  * ``_processed_count += 1`` has already fired, so ``stats()`` reports
    the mail as handled,
  * the handler's only durable write (``api/state.py`` ``_note_email``)
    swallows its own failures at ``logger.debug``,
  * and IMAP will not redeliver a seen message.

So a collected task is a permanently lost email that the brain reports as
processed. The module's own comment at :166-172 names this exact hazard
and then guards only the ``loop is None`` leg of it.

The ``except`` at :225 does not help either: it wraps the ``to_thread``
worker body, which finishes the moment ``call_soon_threadsafe`` queues the
callback. Nothing the coroutine does afterwards is inside it.

WHY THE EXISTING GUARD MISSED THIS: ``tests/test_background_task_references.py``
AST-scans for ``create_task``/``ensure_future`` *call* nodes. Here
``asyncio.create_task`` is passed as a bare NAME to be called later by the
loop, so it is not an ``ast.Call`` and the scanner cannot see it. The last
test below closes that hole.
"""

from __future__ import annotations

import ast
import asyncio
import gc
import pathlib
import weakref
from email.mime.text import MIMEText

import pytest

from integrations.email_watcher import EmailWatcher

FERAL_CORE = pathlib.Path(__file__).resolve().parent.parent


class GcProbe:
    """A coroutine that can only finish if its task outlives ``gc.collect()``.

    Same construction as ``tests/test_background_task_references.py``: the
    future the body awaits is published only through a
    ``WeakValueDictionary``, so the test's handle does not keep the task's
    cycle rooted and the collection is deterministic rather than lucky.
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


def _raw_email(sender="alice@example.com", subject="Invoice due"):
    msg = MIMEText("body text", "plain")
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Thu, 16 Apr 2026 12:00:00 +0000"
    msg["Message-ID"] = "<b12@example.com>"
    return msg.as_bytes()


class FakeIMAP:
    """FETCH is what marks a message ``\\Seen``; record that it happened."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.fetched: list[bytes] = []

    def fetch(self, msg_id, spec):
        self.fetched.append(msg_id)
        return "OK", [(b"1 (RFC822 {%d}" % len(self._raw), self._raw)]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("FERAL_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("FERAL_IMAP_USER", "me@example.com")
    monkeypatch.setenv("FERAL_IMAP_PASS", "app-password")
    monkeypatch.delenv("FERAL_EMAIL_VIP_SENDERS", raising=False)
    monkeypatch.delenv("FERAL_EMAIL_FILTER_SUBJECTS", raising=False)


async def _drive_one_message(watcher) -> None:
    """Run ``_process_message`` exactly as the IMAP worker thread does,
    then let the loop pick up the cross-thread callback."""
    watcher._mail = FakeIMAP(_raw_email())
    await asyncio.to_thread(watcher._process_message, b"1")
    for _ in range(6):
        await asyncio.sleep(0)


async def test_the_on_email_handler_task_survives_gc(configured, monkeypatch):
    """The headline case: the FETCH already consumed the mail, so the
    handler task being collected is a silently lost email."""
    probe = GcProbe()
    watcher = EmailWatcher(on_email=probe.body)
    monkeypatch.setattr(watcher, "_watch_loop", lambda: asyncio.sleep(0))
    assert await watcher.start() is True

    await _drive_one_message(watcher)
    assert watcher.stats()["processed"] == 1, "the mail was counted as handled"

    assert await probe.collect_and_release() is True, (
        "the inbound-email handler task was garbage-collected mid-flight; "
        "the message is already marked \\Seen and IMAP will not redeliver it"
    )
    await watcher.stop()


async def test_the_brain_event_fanout_task_survives_gc(configured, monkeypatch):
    """The sibling site at :214. Same shape, same weak reference: the
    ``email_received`` brain event never reaches the session."""
    import types

    probe = GcProbe()
    watcher = EmailWatcher(on_email=None)
    monkeypatch.setattr(watcher, "_watch_loop", lambda: asyncio.sleep(0))
    assert await watcher.start() is True

    fake_orch = types.SimpleNamespace(_emit_brain_event=probe.body)
    from api.state import state as real_state

    monkeypatch.setattr(real_state, "orchestrator", fake_orch, raising=False)
    monkeypatch.setattr(real_state, "sessions", {"sess-1": object()}, raising=False)

    await _drive_one_message(watcher)

    assert await probe.collect_and_release() is True, (
        "the email_received brain-event task was garbage-collected"
    )
    await watcher.stop()


async def test_dispatched_tasks_are_tracked_and_the_set_self_cleans(
    configured, monkeypatch,
):
    """The reference must be held in a bounded set, per the repo pattern:
    a set that only ever grows is the other half of this bug class."""
    seen: list = []

    async def on_email(incoming):
        seen.append(incoming)

    watcher = EmailWatcher(on_email=on_email)
    monkeypatch.setattr(watcher, "_watch_loop", lambda: asyncio.sleep(0))
    assert await watcher.start() is True

    await _drive_one_message(watcher)
    for _ in range(6):
        await asyncio.sleep(0)

    assert len(seen) == 1
    assert watcher._dispatched_tasks == set(), (
        "completed tasks must be discarded, or the set grows without bound"
    )
    await watcher.stop()


async def test_stop_drains_in_flight_handlers(configured, monkeypatch):
    """A handler still running at shutdown must be waited for, not
    abandoned; the mail is already gone from the server."""
    landed: list = []

    async def on_email(incoming):
        await asyncio.sleep(0.01)
        landed.append(incoming)

    watcher = EmailWatcher(on_email=on_email)
    monkeypatch.setattr(watcher, "_watch_loop", lambda: asyncio.sleep(0))
    assert await watcher.start() is True

    await _drive_one_message(watcher)
    await watcher.stop()

    assert landed, "stop() abandoned an in-flight email handler"


# ── the guard the existing AST scan cannot express ───────────────────

_SKIP_DIR_PARTS = ("/build/", "/dist/", "/node_modules/", "/tests/", "/.git/")
_SCHEDULING_NAMES = frozenset({"create_task", "ensure_future"})


def _scheduler_passed_as_a_callback(node: ast.AST) -> bool:
    """True when a scheduling function is handed to another call as a bare
    reference, e.g. ``loop.call_soon_threadsafe(asyncio.create_task, coro)``.

    ``tests/test_background_task_references.py`` looks for ``ast.Call``
    nodes, so this shape -- which schedules a Task just as weakly, only
    later and on another thread -- was invisible to it.
    """
    if not isinstance(node, ast.Call):
        return False
    for arg in list(node.args) + [kw.value for kw in node.keywords]:
        if isinstance(arg, ast.Attribute) and arg.attr in _SCHEDULING_NAMES:
            return True
        if isinstance(arg, ast.Name) and arg.id in _SCHEDULING_NAMES:
            return True
    return False


def test_no_scheduler_passed_as_a_bare_callback():
    offenders: list[str] = []
    for path in FERAL_CORE.rglob("*.py"):
        if any(part in str(path) for part in _SKIP_DIR_PARTS):
            continue
        try:
            source = path.read_text(errors="ignore")
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if _scheduler_passed_as_a_callback(node):
                offenders.append(
                    f"{path.relative_to(FERAL_CORE)}:{node.lineno}  "
                    f"{lines[node.lineno - 1].strip()}"
                )
    assert not offenders, (
        "create_task/ensure_future handed to another call as a bare "
        "callback; the Task it eventually builds is dropped and the loop "
        "holds it only weakly (B12 / AUDIT-FIXES F-06):\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_that_guard_can_see_a_violation():
    """The scanner must flag the shape it exists to flag, and not the fix."""
    broken = ast.parse(
        "loop.call_soon_threadsafe(asyncio.create_task, coro())"
    ).body[0]
    assert _scheduler_passed_as_a_callback(broken.value)

    fixed = ast.parse("loop.call_soon_threadsafe(self._spawn_tracked, coro())").body[0]
    assert not _scheduler_passed_as_a_callback(fixed.value)
