"""IMAP work in integrations/email.py must run off the event loop.

On 2026-09-02 at 09:41:00.907 the executor ran ``email__get_unread_count``.
The next log line from any logger was at 09:44:02.571, 181 seconds later;
every HTTP request to the brain timed out in between and the UI reported
the brain offline. ``get_unread_count`` called the synchronous
``_imap_connect`` (``imaplib.IMAP4_SSL(host, port)`` with no timeout, then
``login``) directly from a coroutine, and then ``select`` / ``search`` /
``logout`` the same way. ``list_inbox``, ``read_email`` and
``summarize_inbox`` had the same shape; ``search`` already used
``asyncio.to_thread`` and the module's own comment on ``probe_app_password``
says "Blocking, call via asyncio.to_thread".

These tests drive the real endpoints against a fake ``imaplib.IMAP4_SSL``
and check two things: the loop keeps ticking while the IMAP conversation
runs, and the socket is opened with a timeout so a hung server cannot pin
even the worker thread forever.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from integrations.email import IMAP_TIMEOUT_SECONDS, EmailIntegration


class _FakeVault:
    def __init__(self):
        self.values: dict[str, str] = {}

    def store(self, key_name: str, value: str, stored_by: str = "user") -> None:
        self.values[key_name] = value

    def retrieve(self, key_name: str, requester: str = "executor"):
        return self.values.get(key_name)


class _FakeOAuth:
    def __init__(self, vault):
        self._vault = vault

    def is_connected(self, provider_id: str) -> bool:
        return False


class _RecordingIMAP:
    """imaplib.IMAP4_SSL stand-in. Records the constructor call and the
    thread it ran on; optionally sleeps in ``__init__`` to imitate a slow
    TLS handshake the way a real hung server would."""

    constructed: list[dict] = []
    connect_delay: float = 0.0

    def __init__(self, host, port, timeout=None):
        type(self).constructed.append({
            "host": host,
            "port": port,
            "timeout": timeout,
            "thread": threading.get_ident(),
        })
        if self.connect_delay:
            time.sleep(self.connect_delay)

    def login(self, user, password):
        return ("OK", [b"ok"])

    def select(self, mailbox):
        return ("OK", [b"3"])

    def search(self, charset, *criteria):
        return ("OK", [b"1 2 3"])

    def fetch(self, mid, spec):
        raw = (
            b"From: sender@example.com\r\nSubject: hello\r\n"
            b"Date: Mon, 01 Sep 2026 10:00:00 +0000\r\n\r\nbody text\r\n"
        )
        return ("OK", [(b"1 (RFC822 {%d})" % len(raw), raw)])

    def logout(self):
        return ("BYE", [b"bye"])


def _make_email() -> EmailIntegration:
    vault = _FakeVault()
    email = EmailIntegration(oauth_manager=_FakeOAuth(vault))
    email.store_app_password("me@gmail.com", "abcdefghijklmnop")
    email._imap_host = "imap.example.test"
    email._imap_user = "me@gmail.com"
    email._imap_pass = "abcdefghijklmnop"
    assert email._use_imap
    return email


@pytest.fixture(autouse=True)
def _patch_imap(monkeypatch):
    _RecordingIMAP.constructed = []
    _RecordingIMAP.connect_delay = 0.0
    monkeypatch.setattr("integrations.email.imaplib.IMAP4_SSL", _RecordingIMAP)
    yield
    _RecordingIMAP.constructed = []
    _RecordingIMAP.connect_delay = 0.0


@pytest.mark.asyncio
async def test_get_unread_count_keeps_the_loop_responsive():
    """A 1 s IMAP connect must not stall a 50 ms ticker on the same loop."""
    _RecordingIMAP.connect_delay = 1.0
    email = _make_email()

    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.05)

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await email.get_unread_count()
    finally:
        stop.set()
        await ticker_task

    assert result["success"] is True
    assert result["data"] == {"unread": 3, "source": "imap"}
    # 1 s of blocking connect at 50 ms per tick is ~20 ticks when the
    # loop is free and exactly 1 when it is frozen (the pre-fix shape).
    assert ticks >= 10, f"loop ticked only {ticks} times during the IMAP call"


@pytest.mark.asyncio
async def test_imap_socket_is_opened_with_a_timeout():
    email = _make_email()
    await email.get_unread_count()
    assert _RecordingIMAP.constructed, "IMAP4_SSL was never constructed"
    assert _RecordingIMAP.constructed[0]["timeout"] == IMAP_TIMEOUT_SECONDS == 15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda e: e.get_unread_count(), id="get_unread_count"),
        pytest.param(lambda e: e.list_inbox(max_results=2), id="list_inbox"),
        pytest.param(lambda e: e.read_email(message_id="1"), id="read_email"),
        pytest.param(lambda e: e.summarize_inbox(max_emails=2), id="summarize_inbox"),
        pytest.param(lambda e: e.search(query="hello"), id="search"),
    ],
)
async def test_every_imap_endpoint_connects_off_the_loop_thread(call):
    """Each ``if self._use_imap`` branch opens its socket on a worker thread."""
    email = _make_email()
    loop_thread = threading.get_ident()
    result = await call(email)
    assert result["success"] is True, result
    assert _RecordingIMAP.constructed, "IMAP4_SSL was never constructed"
    for record in _RecordingIMAP.constructed:
        assert record["thread"] != loop_thread, (
            "IMAP4_SSL was constructed on the event-loop thread"
        )
        assert record["timeout"] == IMAP_TIMEOUT_SECONDS


def test_probe_app_password_uses_the_same_timeout(monkeypatch):
    monkeypatch.setattr("integrations.email.smtplib.SMTP", _RaisingSMTP)
    EmailIntegration.probe_app_password("me@gmail.com", "abcd efgh ijkl mnop")
    assert _RecordingIMAP.constructed[0]["timeout"] == IMAP_TIMEOUT_SECONDS


class _RaisingSMTP:
    def __init__(self, *args, **kwargs):
        raise OSError("smtp not under test")
