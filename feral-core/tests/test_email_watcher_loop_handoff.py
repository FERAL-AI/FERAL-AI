"""EmailWatcher / MQTTBridge background-task regressions.

D6  ``_process_message`` runs on the worker thread ``asyncio.to_thread``
    spawns for ``_connect_and_idle``. It called
    ``asyncio.get_event_loop()`` there, which on Python 3.11 raises
    ``RuntimeError: There is no current event loop in thread ...``. Both
    call sites swallowed it, so ``_on_email`` never fired — while
    ``_processed_count`` had already been bumped and the FETCH had
    already marked the message ``\\Seen``. Mail was consumed and lost
    with ``stats()`` reporting success.

D8  ``EmailWatcher.start`` and ``MQTTBridge.start`` discarded the handle
    from ``asyncio.create_task``. ``api/state.py`` then read
    ``getattr(watcher, "_task", None)`` "so shutdown cancels it", which
    always evaluated to ``None`` — neither loop was ever cancellable.
"""

from __future__ import annotations

import asyncio
import threading
from email.mime.text import MIMEText

import pytest

from integrations.email_watcher import EmailWatcher
from integrations.mqtt_bridge import MQTTBridge


def _raw_email(sender="alice@example.com", subject="Quarterly numbers"):
    msg = MIMEText("body text", "plain")
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Thu, 16 Apr 2026 12:00:00 +0000"
    msg["Message-ID"] = "<handoff@example.com>"
    return msg.as_bytes()


class FakeIMAP:
    """Minimal IMAP stand-in: FETCH is what marks a message ``\\Seen``."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.fetched: list[bytes] = []

    def fetch(self, msg_id, spec):
        self.fetched.append(msg_id)
        return "OK", [(b"1 (RFC822 {%d}" % len(self._raw), self._raw)]


@pytest.fixture
def configured_watcher(monkeypatch):
    monkeypatch.setenv("FERAL_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("FERAL_IMAP_USER", "me@example.com")
    monkeypatch.setenv("FERAL_IMAP_PASS", "app-password")
    monkeypatch.delenv("FERAL_EMAIL_VIP_SENDERS", raising=False)
    monkeypatch.delenv("FERAL_EMAIL_FILTER_SUBJECTS", raising=False)
    return EmailWatcher


# ── D6: the worker thread hands off to the captured loop ─────────────


async def test_on_email_fires_from_the_worker_thread(configured_watcher,
                                                     monkeypatch):
    """``_process_message`` must dispatch ``_on_email`` when called off
    the loop thread, exactly as the IMAP worker does."""
    delivered: list = []
    done = asyncio.Event()

    async def on_email(incoming):
        delivered.append(incoming)
        done.set()

    watcher = configured_watcher(on_email=on_email)
    # Do not let start() spawn the real IMAP loop; we drive
    # _process_message directly from a worker thread instead.
    monkeypatch.setattr(watcher, "_watch_loop", lambda: asyncio.sleep(0))
    assert await watcher.start() is True
    watcher._mail = FakeIMAP(_raw_email())

    await asyncio.to_thread(watcher._process_message, b"1")
    await asyncio.wait_for(done.wait(), timeout=5)

    assert len(delivered) == 1
    assert delivered[0].subject == "Quarterly numbers"
    assert watcher.stats()["processed"] == 1
    await watcher.stop()


async def test_process_message_does_not_consume_mail_without_a_loop(
    configured_watcher,
):
    """No captured loop means no way to deliver. Refuse *before* the
    FETCH, because FETCH marks the message read: silently burning it and
    counting it as processed is the exact bug."""
    watcher = configured_watcher(on_email=lambda incoming: asyncio.sleep(0))
    mail = FakeIMAP(_raw_email())
    watcher._mail = mail

    await asyncio.to_thread(watcher._process_message, b"1")

    assert mail.fetched == []
    assert watcher.stats()["processed"] == 0


# ── D8: background tasks are cancellable ─────────────────────────────


async def test_email_watcher_exposes_its_task(configured_watcher, monkeypatch):
    watcher = configured_watcher()

    async def _idle():
        await asyncio.sleep(3600)

    monkeypatch.setattr(watcher, "_watch_loop", _idle)
    assert await watcher.start() is True
    # api/state.py registers this handle for shutdown cancellation.
    assert isinstance(watcher._task, asyncio.Task)
    watcher._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher._task


async def test_mqtt_bridge_exposes_its_task(monkeypatch):
    pytest.importorskip("aiomqtt")
    bridge = MQTTBridge(broker_url="mqtt://localhost")

    async def _idle():
        await asyncio.sleep(3600)

    monkeypatch.setattr(bridge, "_subscribe_loop", _idle)
    assert await bridge.start() is True
    assert isinstance(bridge._task, asyncio.Task)
    bridge._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await bridge._task


async def test_captured_loop_is_the_running_loop(configured_watcher,
                                                 monkeypatch):
    watcher = configured_watcher()
    monkeypatch.setattr(watcher, "_watch_loop", lambda: asyncio.sleep(0))
    await watcher.start()
    assert watcher._loop is asyncio.get_running_loop()
    # The worker thread has no loop of its own — that is the whole point.
    thread_loops: list = []

    def _probe():
        try:
            thread_loops.append(asyncio.get_event_loop())
        except RuntimeError as exc:
            thread_loops.append(exc)

    t = threading.Thread(target=_probe)
    t.start()
    t.join()
    assert isinstance(thread_loops[0], RuntimeError)
    await watcher.stop()
