"""FERAL Email Watcher — monitors inbox via IMAP IDLE for real-time email processing.

Hardened: OAuth2 XOAUTH2, IDLE re-issue every 29 min, polling fallback,
testable _parse_message / _extract_body helpers.
"""
import asyncio
import base64
import email
import imaplib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from email.header import decode_header
from typing import Optional, Callable, Awaitable

logger = logging.getLogger("feral.integrations.email")


@dataclass
class IncomingEmail:
    sender: str
    subject: str
    body: str
    date: str
    message_id: str
    has_attachments: bool
    attachment_names: list[str] = field(default_factory=list)


def _format_xoauth2(user: str, token: str) -> str:
    """Build an XOAUTH2 SASL string for IMAP AUTHENTICATE."""
    auth_string = f"user={user}\x01auth=Bearer {token}\x01\x01"
    return base64.b64encode(auth_string.encode()).decode()


class EmailWatcher:
    """Watches an IMAP inbox for new emails and routes them to the orchestrator."""

    IDLE_TIMEOUT_SECONDS = 29 * 60  # re-issue IDLE before Gmail's 30-min limit
    POLL_INTERVAL_SECONDS = 60

    def __init__(self, on_email: Optional[Callable[[IncomingEmail], Awaitable]] = None):
        self._imap_host = os.getenv("FERAL_IMAP_HOST", "")
        self._imap_port = int(os.getenv("FERAL_IMAP_PORT", "993"))
        self._imap_user = os.getenv("FERAL_IMAP_USER", "")
        self._imap_pass = os.getenv("FERAL_IMAP_PASS", "")
        self._oauth_token = os.getenv("FERAL_IMAP_OAUTH_TOKEN", "")
        self._on_email = on_email
        self._running = False
        self._mail: Optional[imaplib.IMAP4_SSL] = None
        self._task: Optional[asyncio.Task] = None
        # The brain's event loop, captured in start(). The IMAP work
        # runs on an asyncio.to_thread worker which has no loop of its
        # own, so every hand-off back to the brain goes through this.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # B12 — strong references to the handler tasks dispatched from the
        # worker thread. The loop holds tasks only WEAKLY, so a task that
        # is suspended on a future reachable solely through its own frame
        # is an unrooted cycle and ``gc.collect()`` destroys it. That
        # matters more here than almost anywhere else in the codebase:
        # by the time the task is scheduled the FETCH has already marked
        # the message ``\Seen``, ``_processed_count`` has already been
        # bumped, and IMAP will not redeliver. A collected task is a
        # permanently lost email that ``stats()`` reports as processed.
        # Same mechanism as ``Orchestrator._track_background_task`` and
        # ``BrainState.register_background_task``: a set plus a discard
        # callback, so it stays bounded.
        self._dispatched_tasks: set[asyncio.Task] = set()
        self._processed_count = 0
        self._idle_supported: Optional[bool] = None
        self._vip_senders = [
            s.strip()
            for s in os.getenv("FERAL_EMAIL_VIP_SENDERS", "").split(",")
            if s.strip()
        ]
        self._filter_subjects = [
            s.strip()
            for s in os.getenv("FERAL_EMAIL_FILTER_SUBJECTS", "").split(",")
            if s.strip()
        ]

    @property
    def configured(self) -> bool:
        has_auth = bool(self._imap_pass) or bool(self._oauth_token)
        return bool(self._imap_host and self._imap_user and has_auth)

    async def start(self) -> bool:
        if not self.configured:
            logger.info(
                "Email watcher: not configured (set FERAL_IMAP_HOST, FERAL_IMAP_USER, "
                "and FERAL_IMAP_PASS or FERAL_IMAP_OAUTH_TOKEN)"
            )
            return False
        self._running = True
        self._loop = asyncio.get_running_loop()
        # Keep the handle: api/state.py registers it so shutdown can
        # cancel the loop.
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("Email watcher started: %s@%s (oauth=%s)",
                     self._imap_user, self._imap_host, bool(self._oauth_token))
        return True

    # ── B12: cross-thread dispatch that keeps its tasks ──────────────

    def _spawn_tracked(self, coro) -> None:
        """Create a handler task and hold a strong reference to it.

        Runs ON the brain's event loop (it is the callback handed to
        ``call_soon_threadsafe``), never on the IMAP worker thread.

        This replaces passing ``asyncio.create_task`` itself as the
        callback. That older shape dropped the Task the loop built, and
        it was invisible to the AST guard in
        ``tests/test_background_task_references.py`` because
        ``asyncio.create_task`` appeared as a bare name rather than a
        call node.
        """
        try:
            task = asyncio.ensure_future(coro)
        except Exception:
            logger.exception("Email watcher: could not schedule handler")
            coro.close()
            return
        self._dispatched_tasks.add(task)
        task.add_done_callback(self._on_dispatched_done)

    def _on_dispatched_done(self, task: asyncio.Task) -> None:
        """Unbook the task and say so when the handler failed.

        The failure log is not decoration. The handler's only durable
        write swallows its own errors at ``logger.debug``, so without
        this an exception inside the handler left no trace anywhere
        while the mail was already consumed from the server.
        """
        self._dispatched_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Email watcher: handler failed after the message was "
                "already marked read: %s", exc, exc_info=exc,
            )

    def _dispatch(self, coro) -> None:
        """Hand ``coro`` to the brain's loop from the IMAP worker thread.

        Caller must have checked ``self._loop`` is not None; that check
        happens once, before the FETCH, precisely so mail is never
        consumed with no way to deliver it.
        """
        loop = self._loop
        if loop is None:  # pragma: no cover - guarded at the call site
            coro.close()
            return
        loop.call_soon_threadsafe(self._spawn_tracked, coro)

    async def stop(self):
        self._running = False
        if self._mail:
            try:
                self._mail.logout()
            except Exception:
                pass
        # B12 — drain handlers that are still in flight. The messages
        # they describe are already gone from the server, so abandoning
        # them at shutdown loses exactly the mail this fix exists to
        # keep. Bounded, so a wedged handler cannot stall shutdown.
        pending = [t for t in list(self._dispatched_tasks) if not t.done()]
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Email watcher: %d email handler(s) did not finish within 10s",
                len(pending),
            )

    async def _watch_loop(self):
        while self._running:
            try:
                await asyncio.to_thread(self._connect_and_idle)
            except Exception as e:
                if self._running:
                    logger.warning("Email watcher error: %s — reconnecting in 30s", e)
                    await asyncio.sleep(30)

    def _connect_and_idle(self):
        self._mail = imaplib.IMAP4_SSL(self._imap_host, self._imap_port)

        if self._oauth_token:
            auth_string = _format_xoauth2(self._imap_user, self._oauth_token)
            self._mail.authenticate("XOAUTH2", lambda _: auth_string.encode())
            logger.debug("Authenticated via XOAUTH2")
        else:
            self._mail.login(self._imap_user, self._imap_pass)

        self._mail.select("INBOX")
        self._idle_supported = self._check_idle_support()

        while self._running:
            status, data = self._mail.search(None, "UNSEEN")
            if status == "OK" and data[0]:
                msg_ids = data[0].split()
                for msg_id in msg_ids[-5:]:
                    self._process_message(msg_id)

            if self._idle_supported:
                self._do_idle_wait()
            else:
                logger.debug("IDLE not supported, polling every %ds", self.POLL_INTERVAL_SECONDS)
                time.sleep(self.POLL_INTERVAL_SECONDS)

    def _check_idle_support(self) -> bool:
        """Return True if the server advertises IDLE capability."""
        try:
            _typ, caps = self._mail.capability()
            cap_str = b" ".join(caps).decode().upper()
            return "IDLE" in cap_str
        except Exception:
            return False

    def _do_idle_wait(self):
        """Issue IDLE, wait up to IDLE_TIMEOUT_SECONDS, then DONE."""
        try:
            tag = self._mail._new_tag().decode()
            self._mail.send(f"{tag} IDLE\r\n".encode())
            self._mail.readline()

            self._mail.sock.settimeout(self.IDLE_TIMEOUT_SECONDS)
            try:
                self._mail.readline()
            except (TimeoutError, OSError):
                pass
            finally:
                self._mail.sock.settimeout(None)

            self._mail.send(b"DONE\r\n")
            self._mail.readline()
        except Exception as e:
            logger.debug("IDLE failed, falling back to poll: %s", e)
            self._idle_supported = False
            time.sleep(self.POLL_INTERVAL_SECONDS)

    def _process_message(self, msg_id: bytes):
        # Runs on the to_thread worker. Check the captured loop *before*
        # the FETCH, because FETCH marks the message \Seen: with no way
        # to deliver, fetching would consume the mail and drop it while
        # stats() reported it as processed.
        loop = self._loop
        if loop is None:
            logger.error(
                "Email watcher: no event loop captured (start() not run on "
                "the brain loop) — refusing to fetch %s rather than marking "
                "it read and dropping it", msg_id,
            )
            return
        try:
            status, data = self._mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                return

            raw = data[0][1]
            incoming = self._parse_message(raw)
            if incoming is None:
                return

            if self._vip_senders and not any(
                vip.lower() in incoming.sender.lower() for vip in self._vip_senders
            ):
                if self._filter_subjects and not any(
                    f.lower() in incoming.subject.lower() for f in self._filter_subjects
                ):
                    return

            self._processed_count += 1

            is_vip = (
                any(vip.lower() in incoming.sender.lower() for vip in self._vip_senders)
                if self._vip_senders else False
            )

            try:
                from api.state import state
            except ImportError as exc:
                # Standalone watcher (no brain process): the operator's
                # on_email callback below still runs.
                logger.debug("Email watcher: brain state unavailable (%s) — "
                             "skipping email_received fan-out", exc)
            else:
                if state.orchestrator:
                    for sid in list(state.sessions.keys()):
                        self._dispatch(
                            state.orchestrator._emit_brain_event(sid, "email_received", {
                                "from": incoming.sender,
                                "subject": incoming.subject,
                                "vip": is_vip,
                            })
                        )

            if self._on_email:
                self._dispatch(self._on_email(incoming))
        except Exception as e:
            logger.warning("Failed to process email %s: %s", msg_id, e)

    @staticmethod
    def _parse_message(raw: bytes) -> Optional[IncomingEmail]:
        """Parse raw RFC-822 bytes into an IncomingEmail. Exposed for unit testing."""
        try:
            msg = email.message_from_bytes(raw)
        except Exception:
            return None

        sender = EmailWatcher._decode_header(msg.get("From", ""))
        subject = EmailWatcher._decode_header(msg.get("Subject", ""))
        date = msg.get("Date", "")
        message_id = msg.get("Message-ID", "")
        body = EmailWatcher._extract_body(msg)

        attachment_names: list[str] = []
        has_attachments = False
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                has_attachments = True
                fname = part.get_filename()
                if fname:
                    attachment_names.append(EmailWatcher._decode_header(fname))

        return IncomingEmail(
            sender=sender,
            subject=subject,
            body=body[:5000],
            date=date,
            message_id=message_id,
            has_attachments=has_attachments,
            attachment_names=attachment_names,
        )

    @staticmethod
    def _decode_header(value: str) -> str:
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(str(part))
        return " ".join(decoded)

    @staticmethod
    def _extract_body(msg: email.message.Message) -> str:
        """Extract the best text body from a MIME message. Exposed for unit testing."""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = part.get_content_disposition()
                if disp == "attachment":
                    continue
                if ctype == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(errors="replace")
                elif ctype == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        text = payload.decode(errors="replace")
                        text = re.sub(r"<[^>]+>", " ", text)
                        text = re.sub(r"\s+", " ", text).strip()
                        return text
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(errors="replace")
                if msg.get_content_type() == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()
                return text
        return ""

    def stats(self) -> dict:
        return {
            "configured": self.configured,
            "running": self._running,
            "host": self._imap_host,
            "user": self._imap_user,
            "processed": self._processed_count,
            "vip_senders": self._vip_senders,
            "oauth": bool(self._oauth_token),
            "idle_supported": self._idle_supported,
        }
