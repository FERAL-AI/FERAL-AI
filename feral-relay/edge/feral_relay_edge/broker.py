"""The relay edge: route encrypted bytes to a brain without reading them.

A brain runs on somebody's laptop behind NAT, so it dials out and holds
a control WebSocket open. The certificate and private key for
``<relay_id>.relay.feral.sh`` live on that laptop, never here. When a
client connects to this edge on 443 we read the one field that is sent
before encryption starts, the SNI, use it to pick a brain, ask that
brain to dial back a stream, and then copy bytes in both directions
without understanding any of them.

**The edge never terminates TLS.** There is no ``ssl`` import in this
module and there must never be one for a client connection. The moment
the edge holds a private key for a user's hostname it becomes able to
read their traffic, and the entire point of this design is that it
cannot. The handshake completes between the client and the brain, and we
are a pipe.

Two things in the splice are easy to get wrong and fatal if you do:

1. **The buffered ClientHello goes first.** We had to consume those
   bytes off the socket to find the SNI. They are the first thing the
   brain's TLS stack needs. Forward them before anything else or the
   handshake dies on a laptop somewhere with no useful error.
2. **Everything is bounded.** Every wait has a deadline and every buffer
   has a ceiling, because every byte and every connection arrives from
   an unauthenticated stranger. A missing timeout here is not a slow
   path, it is a way to pin one file descriptor per attacker packet.

The WebSocket objects this module works with are duck-typed: anything
with ``await send(data)``, ``await recv()`` and ``await close(code,
reason)`` works. That is deliberate. Binding to one WebSocket library
would make every failure test need a real server, and the failures are
the part worth testing.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional, Tuple

from .registry import (
    ControlConnection,
    RelayNotConnected,
    StreamLimitReached,
    StreamSlot,
    TunnelRegistry,
    UnknownStream,
)
from .sni import peek_sni

logger = logging.getLogger(__name__)


def _load_control_plane():
    """Import the control plane's identity code, wherever it lives.

    The hello frames a brain sends here are signed exactly as the
    registration endpoint's are, and they must be verified by the same
    code. Re-implementing the derivation or the signature check on this
    side is precisely the drift that ``identity.py`` warns about: the
    two sides would agree in every test we wrote and disagree on some
    input we did not, and the binding between a relay id and a key would
    quietly stop meaning anything.

    The edge and the control plane are separate deployables that are not
    installed into each other's environments, so the import is attempted
    normally first and falls back to the sibling checkout. Failing to
    find it raises, loudly, at import time. An edge that cannot verify
    signatures must not start and accept hellos anyway.
    """
    try:
        return (
            importlib.import_module("feral_relay_cp.identity"),
            importlib.import_module("feral_relay_cp.registration"),
        )
    except ModuleNotFoundError:
        pass

    control_root = Path(__file__).resolve().parents[2] / "control"
    if not (control_root / "feral_relay_cp" / "identity.py").is_file():
        raise ImportError(
            "cannot locate feral_relay_cp; the edge must verify hellos with "
            f"the control plane's own code, and it is not at {control_root}"
        )
    sys.path.insert(0, str(control_root))
    return (
        importlib.import_module("feral_relay_cp.identity"),
        importlib.import_module("feral_relay_cp.registration"),
    )


_identity, _registration = _load_control_plane()

# Re-exported so callers and tests use the same implementation the
# verification path uses, rather than importing a second copy of it.
derive_relay_id = _identity.derive_relay_id
verify_signature = _identity.verify_signature
canonical_payload = _registration.canonical_payload

#: Base32 alphabet, lowercase, as ``derive_relay_id`` emits it.
_RELAY_ID_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz234567")

#: 20 bytes of digest, base32, no padding. Any other length is not an id
#: this edge ever issued.
_RELAY_ID_LENGTH = 32

# Close codes in the private-use range. Distinct values so an operator
# reading a brain's logs can tell an auth failure from a bad stream id
# without correlating timestamps against the edge.
CLOSE_UNAUTHORIZED = 4401
CLOSE_UNKNOWN_STREAM = 4404
CLOSE_PROTOCOL = 4400


async def _await_within(awaitable: Any, timeout: float) -> Any:
    """Await something with a deadline, without swallowing a cancel.

    :func:`asyncio.wait_for` returns the inner result when a
    cancellation lands in the same tick the awaited thing completes,
    which discards the cancellation and leaves this coroutine running
    after a shutdown asked it to stop. That is a connection handler that
    ignores its own cancel, and it only reproduces under exactly the
    load where you would least like to find it.

    Raises :class:`asyncio.TimeoutError` on the deadline, so callers
    read the same as they would with ``wait_for``.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        task.cancel()
        raise
    if not done:
        task.cancel()
        raise asyncio.TimeoutError()
    return task.result()


class _NonceCache:
    """Remembers hello nonces for as long as they could still be used.

    The registration endpoint has a database for this; the edge does
    not, and doing without would leave a captured hello replayable for
    the whole clock-skew window. That is not a small hole: a replayed
    hello registers a control channel for somebody else's relay id, and
    routing follows the newest registration.

    Bounded, because it is fed by unauthenticated peers. Entries are
    pruned by expiry, and a hard capacity backstop drops the oldest if
    pruning is somehow not keeping up, on the reasoning that a
    replayable window is worse than losing the ability to grow forever.
    """

    def __init__(self, ttl: float, capacity: int = 8192) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._seen: dict = {}

    def check_and_record(self, relay_id: str, nonce: str, now: float) -> bool:
        """True if this nonce is fresh. Records it as a side effect."""
        self._prune(now)
        key = (relay_id, nonce)
        if key in self._seen:
            return False
        self._seen[key] = now + self._ttl
        return True

    def _prune(self, now: float) -> None:
        expired = [key for key, deadline in self._seen.items() if deadline <= now]
        for key in expired:
            del self._seen[key]
        while len(self._seen) > self._capacity:
            # dicts preserve insertion order, so this is the oldest.
            self._seen.pop(next(iter(self._seen)))


class TunnelBroker:
    """The asyncio server: peek, look up, open, splice."""

    def __init__(
        self,
        registry: Optional[TunnelRegistry] = None,
        *,
        base_domain: str = "relay.feral.sh",
        stream_deadline: float = 10.0,
        hello_timeout: float = 10.0,
        peek_timeout: float = 10.0,
        max_peek_bytes: int = 1 << 16,
        chunk_size: int = 1 << 16,
        max_clock_skew: float = 300.0,
        stream_limit: int = 32,
        wall_clock=time.time,
    ) -> None:
        self.registry = registry or TunnelRegistry(
            stream_limit=stream_limit, stream_ttl=stream_deadline
        )
        self.base_domain = base_domain.strip(".").lower()
        self.stream_deadline = stream_deadline
        self.hello_timeout = hello_timeout
        self.peek_timeout = peek_timeout
        self.max_peek_bytes = max_peek_bytes
        self.chunk_size = chunk_size
        self.max_clock_skew = max_clock_skew
        # Wall clock, not monotonic: this one is compared against a
        # timestamp a brain on another machine put in a signed payload.
        self._wall_clock = wall_clock
        self._nonces = _NonceCache(ttl=max_clock_skew * 2)

    # ── control channel ────────────────────────────────────────────

    async def handle_control(self, ws: Any) -> Optional[ControlConnection]:
        """Authenticate a brain and hold its control channel open.

        Returns the connection that was registered, or ``None`` if the
        hello was refused, so a caller driving this directly can tell
        the two apart.
        """
        try:
            raw = await _await_within(ws.recv(), self.hello_timeout)
        except asyncio.TimeoutError:
            # An unauthenticated socket that says nothing is either a
            # scan or a stuck client. Either way it does not get to hold
            # a file descriptor open indefinitely.
            await self._close_quietly(ws, CLOSE_UNAUTHORIZED, "no hello")
            return None
        except Exception:
            return None

        relay_id = self._authenticate(raw)
        if relay_id is None:
            await self._close_quietly(ws, CLOSE_UNAUTHORIZED, "hello refused")
            return None

        connection = ControlConnection(
            relay_id=relay_id, ws=ws, connected_at=self._wall_clock()
        )
        displaced = self.registry.register_control(connection)
        if displaced is not None:
            # Closed rather than left to time out, so the brain does not
            # keep two sockets it believes are both live.
            await self._close_quietly(displaced.ws, CLOSE_PROTOCOL, "replaced")

        logger.info("control channel up for %s", relay_id)
        try:
            # The edge drives this protocol; a brain has nothing it must
            # say after the hello. This loop exists so that the socket
            # dying is noticed here, in the one place that knows how to
            # tear the brain's streams down with it.
            while True:
                await ws.recv()
        except Exception:
            pass
        finally:
            for slot in self.registry.unregister_control(connection):
                logger.info(
                    "control channel for %s went away, dropping stream %s",
                    relay_id,
                    slot.stream_id,
                )
            logger.info("control channel down for %s", relay_id)
        return connection

    def _authenticate(self, raw: Any) -> Optional[str]:
        """Return the relay id a hello proves ownership of, or None.

        Order matters. The signature is checked before the nonce is
        recorded, unlike the registration endpoint, because that store
        is a database behind an authenticated-by-signature HTTP handler
        while this cache is in memory and fed by anyone who can open a
        socket. Recording first would let a stranger burn a nonce and
        lock out the brain whose hello they captured.
        """
        if isinstance(raw, (bytes, bytearray)):
            # A hello is JSON text. Accepting a binary frame here would
            # blur the line between the control protocol and the stream
            # protocol, which is the only thing keeping the splice from
            # ever interpreting user bytes.
            return None
        try:
            hello = json.loads(raw)
        except Exception:
            return None
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            return None

        for field in ("relay_id", "public_key", "ts", "nonce", "signature"):
            if not hello.get(field):
                return None

        try:
            ts = float(hello["ts"])
        except (TypeError, ValueError):
            return None
        if abs(self._wall_clock() - ts) > self.max_clock_skew:
            return None

        public_key_b64 = str(hello["public_key"]).strip()
        try:
            payload = canonical_payload(hello)
        except Exception:
            return None
        if not verify_signature(payload, hello["signature"], public_key_b64):
            return None

        # A valid signature only proves the sender holds a key. This is
        # the check that proves the id they are claiming is the one that
        # key is entitled to, and without it a signed hello could claim
        # any relay id at all.
        try:
            derived = derive_relay_id(base64.b64decode(public_key_b64, validate=True))
        except Exception:
            return None
        if str(hello["relay_id"]).strip().lower() != derived:
            return None

        if not self._nonces.check_and_record(
            derived, str(hello["nonce"]), self._wall_clock()
        ):
            return None

        # Registered under the derived id rather than the sent one, so a
        # case variant cannot occupy a second entry for the same brain.
        return derived

    # ── stream channel ─────────────────────────────────────────────

    async def handle_stream(self, ws: Any, stream_id: str) -> bool:
        """Hand a dialled-back stream to the connection waiting for it.

        Returns whether the stream was accepted. The handler stays put
        until the splice ends, because in every WebSocket server
        returning from the handler closes the socket, and closing it
        mid-splice would cut the connection it is carrying.
        """
        try:
            slot = self.registry.claim_stream(stream_id)
        except UnknownStream:
            # Never issued, already used, or expired. Refused without
            # saying which, so this cannot be used to probe for live
            # stream ids.
            await self._close_quietly(ws, CLOSE_UNKNOWN_STREAM, "unknown stream")
            return False

        if not slot.attach(ws):
            # The TCP side gave up between the claim and now.
            self.registry.release_stream(stream_id)
            await self._close_quietly(ws, CLOSE_UNKNOWN_STREAM, "too late")
            return False

        try:
            await slot.closed.wait()
        finally:
            self.registry.release_stream(stream_id)
            await self._close_quietly(ws, 1000, "stream finished")
        return True

    # ── the client side ────────────────────────────────────────────

    async def handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Route one inbound TCP connection. Closes it, whatever happens."""
        slot: Optional[StreamSlot] = None
        try:
            peeked = await self._peek(reader)
            if peeked is None:
                return
            buffered, host = peeked

            relay_id = self.relay_id_for_host(host)
            if relay_id is None:
                logger.info("refusing connection for unroutable host %s", host)
                return

            try:
                slot = self.registry.reserve_stream(relay_id)
            except RelayNotConnected:
                # Nothing to route to. Closing immediately is the honest
                # answer; holding the socket in the hope a brain shows
                # up would just make the client's timeout ours.
                logger.info("no brain connected for %s", relay_id)
                return
            except StreamLimitReached:
                logger.warning("stream cap reached for %s", relay_id)
                return

            try:
                await slot.connection.ws.send(
                    json.dumps(
                        {
                            "type": "open",
                            "stream_id": slot.stream_id,
                            "deadline": self.stream_deadline,
                        }
                    )
                )
            except Exception:
                # The control socket died between the lookup and the
                # send. Its own handler will unregister it; we just fail
                # this connection.
                logger.info("control channel for %s failed on open", relay_id)
                return

            stream_ws = await slot.wait_attached(self.stream_deadline)
            if stream_ws is None:
                logger.info(
                    "brain %s did not dial back for %s within %ss",
                    relay_id,
                    slot.stream_id,
                    self.stream_deadline,
                )
                return

            await self._splice(reader, writer, stream_ws, buffered, slot)
        except Exception:
            logger.exception("connection failed")
        finally:
            # Releasing wakes the stream handler, which closes the
            # WebSocket. Both sides of a splice die together whichever
            # one died first.
            if slot is not None:
                self.registry.release_stream(slot.stream_id)
            await self._close_writer(writer)

    async def _peek(
        self, reader: asyncio.StreamReader
    ) -> Optional[Tuple[bytes, str]]:
        """Buffer until the SNI is readable. ``None`` means give up.

        Bounded twice over. A client that dribbles bytes forever without
        completing a ClientHello is cut off by the byte ceiling, and one
        that stops sending entirely is cut off by the deadline. Without
        both, an attacker holds a file descriptor and a buffer per
        connection for as long as they feel like it.
        """
        buffered = bytearray()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.peek_timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.info("no ClientHello within the peek deadline")
                return None
            try:
                chunk = await _await_within(reader.read(self.chunk_size), remaining)
            except (asyncio.TimeoutError, ConnectionError):
                return None
            if not chunk:
                return None  # client hung up before saying anything useful

            buffered.extend(chunk)
            if len(buffered) > self.max_peek_bytes:
                logger.info("client exceeded the ClientHello buffer ceiling")
                return None

            # Re-parsing the whole buffer on each read is quadratic in
            # principle and irrelevant in practice: the buffer is capped
            # at 64KB and a real ClientHello arrives in one or two
            # reads. A resumable parser would be a second place for the
            # bounds checks to be wrong.
            outcome = peek_sni(bytes(buffered))
            if outcome.status == "found" and outcome.host:
                return bytes(buffered), outcome.host
            if outcome.is_final:
                # not_tls, malformed or no_sni. All final: more bytes
                # cannot change the answer, so stop buffering now.
                logger.info("closing connection: SNI peek says %s", outcome.status)
                return None

    def relay_id_for_host(self, host: str) -> Optional[str]:
        """Extract the relay id from ``<relay_id>.relay.feral.sh``.

        Strict on purpose. This value selects whose tunnel a connection
        is spliced into, so anything that is not the exact shape of an
        id this system issues is refused rather than looked up.
        """
        suffix = "." + self.base_domain
        if not host.endswith(suffix):
            return None
        label = host[: -len(suffix)]
        if len(label) != _RELAY_ID_LENGTH:
            return None
        if not set(label) <= _RELAY_ID_ALPHABET:
            return None
        return label

    # ── the splice ─────────────────────────────────────────────────

    async def _splice(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ws: Any,
        buffered: bytes,
        slot: StreamSlot,
    ) -> None:
        """Copy bytes both ways until any end of it stops.

        The buffered ClientHello is sent first and on its own. Those
        bytes were consumed from the client's socket to find the SNI,
        and the brain's TLS stack cannot begin without them.
        """
        try:
            await ws.send(buffered)
        except Exception:
            # The stream socket died between being attached and being
            # used. Nothing unusual, and nothing to splice.
            logger.info("stream channel failed before the buffered hello landed")
            return

        up = asyncio.create_task(self._pump_tcp_to_ws(reader, ws))
        down = asyncio.create_task(self._pump_ws_to_tcp(ws, writer))
        # The third waiter is what makes a control channel dropping
        # mid-splice close this TCP connection: the registry releases
        # the slot, which sets this event.
        aborted = asyncio.create_task(slot.closed.wait())

        try:
            await asyncio.wait({up, down, aborted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (up, down, aborted):
                task.cancel()
            # Awaited, not just cancelled, so no task outlives the
            # connection it belonged to. A cancelled-but-unawaited task
            # is how a long-running server accumulates them.
            await asyncio.gather(up, down, aborted, return_exceptions=True)

    async def _pump_tcp_to_ws(self, reader: asyncio.StreamReader, ws: Any) -> None:
        """Client to brain. Ends on EOF or on either side failing."""
        try:
            while True:
                data = await reader.read(self.chunk_size)
                if not data:
                    return  # client closed; the splice is over
                await ws.send(data)
        except Exception:
            # Any failure on either side ends the whole splice. A
            # half-open TLS connection is of no use to anyone, and
            # keeping one alive leaks the other direction's task.
            return

    async def _pump_ws_to_tcp(self, ws: Any, writer: asyncio.StreamWriter) -> None:
        """Brain to client. Ends on close or on either side failing."""
        try:
            while True:
                message = await ws.recv()
                if not isinstance(message, (bytes, bytearray, memoryview)):
                    # A spliced stream is raw TLS. A text frame means
                    # the far end is confused about which socket it is
                    # on, and forwarding it would corrupt the record
                    # stream.
                    return
                writer.write(bytes(message))
                await writer.drain()
        except Exception:
            return

    # ── plumbing ───────────────────────────────────────────────────

    async def serve_tcp(self, host: str = "0.0.0.0", port: int = 443) -> asyncio.Server:
        """Start listening. The caller owns the returned server."""
        return await asyncio.start_server(self.handle_tcp, host, port)

    async def _close_quietly(self, ws: Any, code: int, reason: str) -> None:
        """Close a WebSocket without letting the failure become ours.

        Called from cleanup paths, where the socket is often already
        dead. Raising here would replace the error we are handling with
        a less interesting one.
        """
        try:
            await ws.close(code, reason)
        except TypeError:
            try:
                await ws.close()
            except Exception:
                pass
        except Exception:
            pass

    async def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        try:
            if not writer.is_closing():
                writer.close()
            await writer.wait_closed()
        except Exception:
            pass
