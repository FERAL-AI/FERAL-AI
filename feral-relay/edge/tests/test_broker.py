"""The broker is tested through real sockets and fake WebSockets.

The client side is a genuine loopback TCP connection on an ephemeral
port, because the thing most worth proving is that bytes written by a
real socket come out the other end intact, in order, with the buffered
ClientHello in front of them. Nothing here is asserted by inspecting the
broker's internals when it could be asserted by pushing bytes through
it.

The brain side is a fake WebSocket. Every failure worth testing is a
socket dying at an awkward moment, and a fake makes those failures
happen on demand instead of by timeout.

No test sleeps waiting for something that has an observable outcome, and
every deadline under test is configured down to a fraction of a second
so a broken timeout fails the suite rather than slowing it down.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from feral_relay_edge.broker import (
    CLOSE_UNAUTHORIZED,
    CLOSE_UNKNOWN_STREAM,
    TunnelBroker,
    canonical_payload,
    derive_relay_id,
)
from feral_relay_edge.sni import peek_sni

pytestmark = pytest.mark.asyncio


# ── a WebSocket we can break on purpose ────────────────────────────

_CLOSED = object()


class FakeWebSocket:
    """Duck-typed control or stream channel.

    Deliberately not a mock: it queues real frames in real order, so an
    assertion about what the brain received is an assertion about what
    would have gone down a wire.
    """

    def __init__(self) -> None:
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.outbound: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.close_code = None
        self.close_reason = None

    async def send(self, data):
        if self.closed.is_set():
            raise ConnectionResetError("send on a closed socket")
        await self.outbound.put(data)

    async def recv(self):
        item = await self.inbound.get()
        if item is _CLOSED:
            # Put it back: a socket that closed once stays closed, and a
            # second recv must fail the same way rather than hang.
            await self.inbound.put(_CLOSED)
            raise ConnectionResetError("recv on a closed socket")
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed.is_set():
            return
        self.close_code = code
        self.close_reason = reason
        self.closed.set()
        await self.inbound.put(_CLOSED)

    def feed(self, message) -> None:
        self.inbound.put_nowait(message)

    async def next_out(self, timeout: float = 2.0):
        return await asyncio.wait_for(self.outbound.get(), timeout)


# ── identities and frames ──────────────────────────────────────────


class Identity:
    def __init__(self) -> None:
        self._private = ed25519.Ed25519PrivateKey.generate()
        raw = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public_key_b64 = base64.b64encode(raw).decode("ascii")
        self.relay_id = derive_relay_id(raw)

    def hello(self, *, relay_id: str = None, ts: float = None, nonce: str = None) -> str:
        """A hello signed exactly the way registration signs one."""
        body = {
            "type": "hello",
            "relay_id": self.relay_id if relay_id is None else relay_id,
            "public_key": self.public_key_b64,
            "ts": time.time() if ts is None else ts,
            "nonce": uuid.uuid4().hex if nonce is None else nonce,
        }
        body["signature"] = base64.b64encode(
            self._private.sign(canonical_payload(body))
        ).decode("ascii")
        return json.dumps(body)


def client_hello(host: str, padding: int = 0) -> bytes:
    """A minimal but structurally valid ClientHello carrying ``host``."""
    raw_host = host.encode("ascii")
    name = b"\x00" + len(raw_host).to_bytes(2, "big") + raw_host
    server_name_list = len(name).to_bytes(2, "big") + name
    ext = b"\x00\x00" + len(server_name_list).to_bytes(2, "big") + server_name_list
    if padding:
        # extension 21 is padding, and it makes the hello big enough to
        # arrive in more than one read.
        ext += b"\x00\x15" + padding.to_bytes(2, "big") + b"\x00" * padding
    exts = len(ext).to_bytes(2, "big") + ext
    body = (
        b"\x03\x03" + b"\x00" * 32 + b"\x00"
        + b"\x00\x02\x00\x2f" + b"\x01\x00" + exts
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


async def assert_closed(reader: asyncio.StreamReader, timeout: float = 3.0) -> None:
    """Assert the edge hung up on us.

    A peer that closed while we were still writing shows up as a reset
    rather than a clean EOF, depending on how much was in flight. Both
    mean the same thing here, and only accepting one of them would make
    the test depend on kernel buffering.
    """
    try:
        assert await asyncio.wait_for(reader.read(), timeout) == b""
    except (ConnectionResetError, BrokenPipeError):
        pass


async def until(predicate, timeout: float = 2.0, message: str = "condition") -> None:
    """Poll rather than sleep a fixed amount, so a slow machine passes
    and a broken one still fails."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {message}")


# ── the rig ────────────────────────────────────────────────────────


class Rig:
    """A broker listening on loopback, with brains we can attach."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("stream_deadline", 1.0)
        kwargs.setdefault("peek_timeout", 2.0)
        self.broker = TunnelBroker(**kwargs)
        self.identity = Identity()
        self.tasks: list[asyncio.Task] = []
        self.sockets: list[FakeWebSocket] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.connections: list[asyncio.Task] = []
        self.server = None
        self.port = 0

    @property
    def relay_id(self) -> str:
        return self.identity.relay_id

    @property
    def host(self) -> str:
        return f"{self.relay_id}.{self.broker.base_domain}"

    async def start(self) -> None:
        # Wrapped rather than handed straight to start_server so the rig
        # can wait for every connection handler at teardown. An
        # unawaited handler outliving its test would make the next
        # test's task accounting lie.
        async def serve(reader, writer):
            self.connections.append(asyncio.current_task())
            await self.broker.handle_tcp(reader, writer)

        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def spawn_control(self, ws: FakeWebSocket) -> asyncio.Task:
        self.sockets.append(ws)
        task = asyncio.create_task(self.broker.handle_control(ws))
        self.tasks.append(task)
        return task

    async def connect_brain(self, identity: Identity = None) -> FakeWebSocket:
        identity = identity or self.identity
        ws = FakeWebSocket()
        self.spawn_control(ws)
        ws.feed(identity.hello())
        # Waits for this socket specifically. Waiting for "somebody is
        # registered" would return instantly on a reconnect and hand
        # back a connection the broker has not processed yet.
        await until(
            lambda: getattr(
                self.broker.registry.lookup_control(identity.relay_id), "ws", None
            )
            is ws,
            message="this brain to register",
        )
        return ws

    async def open_client(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        return reader, writer

    def dial_stream(self, stream_id: str) -> tuple[FakeWebSocket, asyncio.Task]:
        ws = FakeWebSocket()
        self.sockets.append(ws)
        task = asyncio.create_task(self.broker.handle_stream(ws, stream_id))
        self.tasks.append(task)
        return ws, task

    async def route(self, payload: bytes = None):
        """Drive one connection all the way to a live splice."""
        control = await self.connect_brain()
        reader, writer = await self.open_client()
        hello = payload if payload is not None else client_hello(self.host)
        writer.write(hello)
        await writer.drain()
        frame = json.loads(await control.next_out())
        stream, task = self.dial_stream(frame["stream_id"])
        return control, reader, writer, stream, task, hello, frame

    async def aclose(self) -> None:
        # Shut down the way a real deployment does: close the sockets
        # and let the handlers notice. Cancellation is the backstop, not
        # the mechanism, so a handler that only exits when cancelled
        # shows up as a hang here rather than passing quietly.
        for writer in self.writers:
            try:
                writer.close()
            except Exception:
                pass
        for socket in self.sockets:
            await socket.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

        outstanding = [t for t in self.tasks + self.connections if not t.done()]
        if outstanding:
            await asyncio.wait(outstanding, timeout=2)
        for task in outstanding:
            task.cancel()
        if outstanding:
            await asyncio.wait(outstanding, timeout=2)


@pytest_asyncio.fixture
async def rig():
    made: list[Rig] = []

    async def _make(**kwargs) -> Rig:
        made.append(Rig(**kwargs))
        await made[-1].start()
        return made[-1]

    yield _make
    for one in made:
        await one.aclose()


# ── the invariant the whole design rests on ────────────────────────


async def test_the_edge_never_terminates_tls():
    """If this fails, the edge can read user traffic.

    The brain holds the certificate and the private key. An ``ssl``
    import in the broker means somebody wrapped a client socket, and the
    entire privacy property of this design is gone. Cheap to assert, and
    the failure it catches is not one you would notice in a functional
    test.
    """
    source = (Path(__file__).resolve().parents[1] / "feral_relay_edge" / "broker.py").read_text()
    assert "import ssl" not in source
    assert "ssl." not in source


async def test_the_fixture_client_hello_is_one_the_parser_accepts():
    """A test rig that builds bytes the parser rejects would make every
    routing test below pass for the wrong reason."""
    host = "olgw5bbcyqd7w3ijq2ipceylpxwx5qxx.relay.feral.sh"
    outcome = peek_sni(client_hello(host))
    assert outcome.status == "found"
    assert outcome.host == host


# ── control channel authentication ─────────────────────────────────


class TestControlAuthentication:
    async def test_a_valid_hello_registers_the_brain(self, rig):
        r = await rig()
        await r.connect_brain()
        assert r.broker.registry.lookup_control(r.relay_id) is not None

    async def test_a_hello_with_a_forged_signature_is_refused(self, rig):
        r = await rig()
        ws = FakeWebSocket()
        task = r.spawn_control(ws)
        body = json.loads(r.identity.hello())
        body["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        ws.feed(json.dumps(body))

        assert await asyncio.wait_for(task, 2) is None
        assert r.broker.registry.lookup_control(r.relay_id) is None
        assert ws.close_code == CLOSE_UNAUTHORIZED

    async def test_a_hello_altered_after_signing_is_refused(self, rig):
        """The signature covers the body, so changing any signed field
        must invalidate it. If it does not, the fields are decorative."""
        r = await rig()
        ws = FakeWebSocket()
        task = r.spawn_control(ws)
        body = json.loads(r.identity.hello())
        body["nonce"] = "swapped-after-signing"
        ws.feed(json.dumps(body))

        assert await asyncio.wait_for(task, 2) is None

    async def test_a_hello_claiming_another_brains_relay_id_is_refused(self, rig):
        """The check that makes the scheme work. A valid signature only
        proves the sender holds a key; without deriving the id from that
        key, an attacker signs a body claiming a victim's id and takes
        over their routing."""
        r = await rig()
        victim = Identity()
        ws = FakeWebSocket()
        task = r.spawn_control(ws)
        ws.feed(r.identity.hello(relay_id=victim.relay_id))

        assert await asyncio.wait_for(task, 2) is None
        assert r.broker.registry.lookup_control(victim.relay_id) is None
        assert ws.close_code == CLOSE_UNAUTHORIZED

    async def test_a_replayed_hello_is_refused(self, rig):
        """A captured hello is otherwise good for the whole skew window,
        and replaying one hijacks a relay id's routing."""
        r = await rig()
        frame = r.identity.hello()
        first = FakeWebSocket()
        task_one = r.spawn_control(first)
        first.feed(frame)
        await until(
            lambda: r.broker.registry.lookup_control(r.relay_id) is not None,
            message="the first hello to be accepted",
        )

        second = FakeWebSocket()
        task_two = r.spawn_control(second)
        second.feed(frame)

        assert await asyncio.wait_for(task_two, 2) is None
        assert second.close_code == CLOSE_UNAUTHORIZED
        assert r.broker.registry.lookup_control(r.relay_id) is not None
        assert not task_one.done()

    async def test_a_hello_from_a_wildly_wrong_clock_is_refused(self, rig):
        r = await rig(max_clock_skew=300)
        ws = FakeWebSocket()
        task = r.spawn_control(ws)
        ws.feed(r.identity.hello(ts=time.time() - 4000))

        assert await asyncio.wait_for(task, 2) is None

    @pytest.mark.parametrize(
        "field", ["relay_id", "public_key", "ts", "nonce", "signature"]
    )
    async def test_a_hello_missing_a_field_is_refused(self, rig, field):
        r = await rig()
        ws = FakeWebSocket()
        task = r.spawn_control(ws)
        body = json.loads(r.identity.hello())
        del body[field]
        ws.feed(json.dumps(body))

        assert await asyncio.wait_for(task, 2) is None

    @pytest.mark.parametrize(
        "frame",
        [
            "not json at all",
            "[]",
            '"a string"',
            '{"type":"open","stream_id":"x"}',
            "{}",
        ],
    )
    async def test_a_first_frame_that_is_not_a_hello_is_refused(self, rig, frame):
        r = await rig()
        ws = FakeWebSocket()
        task = r.spawn_control(ws)
        ws.feed(frame)

        assert await asyncio.wait_for(task, 2) is None

    async def test_a_binary_first_frame_is_refused(self, rig):
        """Blurring control frames and stream frames is how a splice
        ends up interpreting user bytes."""
        r = await rig()
        ws = FakeWebSocket()
        task = r.spawn_control(ws)
        ws.feed(r.identity.hello().encode("utf-8"))

        assert await asyncio.wait_for(task, 2) is None

    async def test_a_socket_that_never_says_hello_is_disconnected(self, rig):
        """An unauthenticated peer must not be able to hold a file
        descriptor open by saying nothing."""
        r = await rig(hello_timeout=0.05)
        ws = FakeWebSocket()
        task = r.spawn_control(ws)

        assert await asyncio.wait_for(task, 2) is None
        assert ws.close_code == CLOSE_UNAUTHORIZED

    async def test_a_reconnect_displaces_and_closes_the_previous_socket(self, rig):
        r = await rig()
        first = await r.connect_brain()
        second = await r.connect_brain()

        assert second is not first
        assert first.closed.is_set()
        assert r.broker.registry.lookup_control(r.relay_id).ws is second

    async def test_the_control_socket_closing_unregisters_the_brain(self, rig):
        r = await rig()
        ws = await r.connect_brain()

        await ws.close()

        await until(
            lambda: r.broker.registry.lookup_control(r.relay_id) is None,
            message="the brain to be unregistered",
        )


# ── routing and the splice ─────────────────────────────────────────


class TestRouting:
    async def test_bytes_flow_end_to_end_with_the_client_hello_first(self, rig):
        """The one that matters.

        The buffered ClientHello was consumed off the client's socket to
        find the SNI. If it does not arrive at the brain first and
        intact, the handshake dies on someone's laptop with no useful
        error anywhere.
        """
        r = await rig()
        control, reader, writer, stream, _, hello, frame = await r.route()

        assert frame["type"] == "open"
        assert frame["deadline"] == r.broker.stream_deadline
        assert await stream.next_out() == hello

        # Client to brain, in order, across many writes.
        expected = bytearray()
        for i in range(50):
            chunk = bytes([i % 256]) * 977
            expected.extend(chunk)
            writer.write(chunk)
        await writer.drain()

        received = bytearray()
        while len(received) < len(expected):
            received.extend(await stream.next_out())
        assert bytes(received) == bytes(expected)

        # Brain to client, same deal.
        downward = bytearray()
        for i in range(20):
            chunk = bytes([255 - (i % 256)]) * 1301
            downward.extend(chunk)
            stream.feed(bytes(chunk))
        got = await asyncio.wait_for(reader.readexactly(len(downward)), 2)
        assert got == bytes(downward)

    async def test_serve_tcp_binds_and_routes(self, rig):
        """The rig starts its own listener so it can wait for handlers
        at teardown, which leaves the entry point the process actually
        calls untested unless it is exercised here."""
        r = await rig()
        control = await r.connect_brain()
        server = await r.broker.serve_tcp("127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            hello = client_hello(r.host)
            writer.write(hello)
            await writer.drain()
            frame = json.loads(await control.next_out())
            stream, task = r.dial_stream(frame["stream_id"])
            assert await stream.next_out() == hello

            writer.close()
            assert await asyncio.wait_for(task, 3) is True
            assert r.broker.registry.active_streams(r.relay_id) == 0
        finally:
            server.close()
            await server.wait_closed()

    async def test_a_hello_split_across_reads_is_buffered_whole(self, rig):
        """A ClientHello does not have to arrive in one segment, and a
        prefix forwarded on its own would corrupt the handshake."""
        r = await rig()
        control = await r.connect_brain()
        reader, writer = await r.open_client()
        hello = client_hello(r.host, padding=4000)

        for offset in range(0, len(hello), 700):
            writer.write(hello[offset:offset + 700])
            await writer.drain()
            await asyncio.sleep(0.005)

        frame = json.loads(await control.next_out())
        stream, _ = r.dial_stream(frame["stream_id"])
        assert await stream.next_out() == hello

    async def test_an_unknown_relay_id_is_closed_and_does_not_hang(self, rig):
        """Holding the socket in the hope a brain shows up just makes
        the client's timeout ours."""
        r = await rig()
        stranger = Identity()
        reader, writer = await r.open_client()
        writer.write(client_hello(f"{stranger.relay_id}.relay.feral.sh"))
        await writer.drain()

        await assert_closed(reader)

    @pytest.mark.parametrize(
        "host",
        [
            "www.example.com",
            "relay.feral.sh",
            "notanid.relay.feral.sh",
            "olgw5bbcyqd7w3ijq2ipceylpxwx5qxx.relay.feral.sh.evil.com",
            "sub.olgw5bbcyqd7w3ijq2ipceylpxwx5qxx.relay.feral.sh",
            "olgw5bbcyqd7w3ijq2ipceylpxwx5qxx1.relay.feral.sh",
        ],
    )
    async def test_a_host_that_is_not_a_relay_id_is_refused(self, rig, host):
        """This value picks whose tunnel a connection joins, so anything
        that is not the exact shape of an issued id is refused rather
        than looked up."""
        r = await rig()
        control = await r.connect_brain()
        reader, writer = await r.open_client()
        writer.write(client_hello(host))
        await writer.drain()

        await assert_closed(reader)
        assert control.outbound.empty()

    async def test_a_brain_that_never_dials_back_frees_the_slot(self, rig):
        r = await rig(stream_deadline=0.15)
        control = await r.connect_brain()
        reader, writer = await r.open_client()
        writer.write(client_hello(r.host))
        await writer.drain()
        json.loads(await control.next_out())  # the open we will ignore

        await assert_closed(reader)
        assert r.broker.registry.active_streams(r.relay_id) == 0

    @pytest.mark.parametrize(
        "first_bytes",
        [
            b"GET / HTTP/1.1\r\nHost: relay.feral.sh\r\n\r\n",
            b"SSH-2.0-OpenSSH_9.0\r\n",
            b"\x16\xff\xff\x00\x05hello",
            b"\x16\x03\x01\xff\xff",
        ],
        ids=["http", "ssh", "bogus-version", "impossible-length"],
    )
    async def test_junk_first_bytes_are_closed_immediately(self, rig, first_bytes):
        r = await rig()
        control = await r.connect_brain()
        reader, writer = await r.open_client()
        writer.write(first_bytes)
        await writer.drain()

        await assert_closed(reader)
        assert control.outbound.empty()

    async def test_a_client_hello_without_sni_is_closed(self, rig):
        r = await rig()
        control = await r.connect_brain()
        reader, writer = await r.open_client()
        body = b"\x03\x03" + b"\x00" * 32 + b"\x00" + b"\x00\x02\x00\x2f" + b"\x01\x00"
        handshake = b"\x01" + len(body).to_bytes(3, "big") + body
        writer.write(b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake)
        await writer.drain()

        await assert_closed(reader)
        assert control.outbound.empty()

    async def test_a_client_that_never_finishes_a_hello_is_cut_off(self, rig):
        """Unbounded buffering is a file descriptor and a buffer per
        attacker packet, held for as long as they feel like it."""
        r = await rig(max_peek_bytes=4096, peek_timeout=5.0)
        await r.connect_brain()
        reader, writer = await r.open_client()

        # A record promising 16383 bytes it never delivers: always
        # need_more, never final.
        writer.write(b"\x16\x03\x01\x3f\xff")
        try:
            for _ in range(64):  # 32KB, well past the 4KB ceiling
                writer.write(b"\x00" * 512)
                await writer.drain()
                await asyncio.sleep(0)
        except (ConnectionResetError, BrokenPipeError):
            pass

        # Cut off by the ceiling, not by the peek timeout, which is 5s.
        await assert_closed(reader)

    async def test_a_client_that_says_nothing_is_cut_off(self, rig):
        r = await rig(peek_timeout=0.1)
        await r.connect_brain()
        reader, _ = await r.open_client()

        await assert_closed(reader)

    async def test_the_stream_cap_refuses_further_connections(self, rig):
        """One client in a loop must not be able to make a laptop dial
        back without limit."""
        r = await rig(stream_limit=1)
        control, _, _, stream, _, _, _ = await r.route()
        await stream.next_out()  # the splice is live and holding the slot

        reader, writer = await r.open_client()
        writer.write(client_hello(r.host))
        await writer.drain()

        await assert_closed(reader)
        assert control.outbound.empty()


# ── the stream channel ─────────────────────────────────────────────


class TestStreamChannel:
    async def test_a_stream_id_that_was_never_issued_is_refused(self, rig):
        r = await rig()
        await r.connect_brain()
        ws, task = r.dial_stream(uuid.uuid4().hex)

        assert await asyncio.wait_for(task, 2) is False
        assert ws.close_code == CLOSE_UNKNOWN_STREAM

    async def test_a_stream_id_cannot_be_used_twice(self, rig):
        """A second dial must not attach itself to a splice that is
        already carrying somebody's traffic."""
        r = await rig()
        control, reader, writer, stream, _, hello, frame = await r.route()
        assert await stream.next_out() == hello

        impostor, task = r.dial_stream(frame["stream_id"])

        assert await asyncio.wait_for(task, 2) is False
        assert impostor.close_code == CLOSE_UNKNOWN_STREAM

        # The original splice is untouched.
        writer.write(b"still mine")
        await writer.drain()
        assert await stream.next_out() == b"still mine"

    async def test_a_stream_dialled_after_the_deadline_is_refused(self, rig):
        r = await rig(stream_deadline=0.1)
        control = await r.connect_brain()
        reader, writer = await r.open_client()
        writer.write(client_hello(r.host))
        await writer.drain()
        frame = json.loads(await control.next_out())

        await assert_closed(reader)  # client gave up
        ws, task = r.dial_stream(frame["stream_id"])

        assert await asyncio.wait_for(task, 2) is False
        assert ws.close_code == CLOSE_UNKNOWN_STREAM


# ── failures in the middle of a live splice ────────────────────────


class TestMidSpliceFailures:
    async def test_the_control_channel_dropping_closes_the_tcp_side(self, rig):
        """A brain that vanishes takes its streams with it. Without
        this the client hangs on a tunnel whose far end is gone."""
        r = await rig()
        control, reader, writer, stream, task, hello, _ = await r.route()
        assert await stream.next_out() == hello

        await control.close()

        await assert_closed(reader)
        assert await asyncio.wait_for(task, 3) is True
        assert stream.closed.is_set()

    async def test_the_stream_channel_dropping_closes_the_tcp_side(self, rig):
        r = await rig()
        control, reader, writer, stream, task, hello, _ = await r.route()
        assert await stream.next_out() == hello

        await stream.close()

        await assert_closed(reader)

    async def test_the_client_disconnecting_closes_the_stream_and_leaks_nothing(
        self, rig
    ):
        """A cancelled task that is never awaited is how a long-running
        server accumulates them until it falls over."""
        r = await rig()
        baseline = len(asyncio.all_tasks())
        control, reader, writer, stream, task, hello, _ = await r.route()
        assert await stream.next_out() == hello
        stream.feed(b"traffic in flight")

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        assert await asyncio.wait_for(task, 3) is True
        assert stream.closed.is_set()
        assert r.broker.registry.active_streams(r.relay_id) == 0
        await until(
            lambda: len(asyncio.all_tasks()) <= baseline + 1,
            timeout=3,
            message="every task belonging to the splice to finish",
        )

    async def test_a_dead_control_socket_fails_the_connection_rather_than_hanging(
        self, rig
    ):
        """The control channel can die between the lookup and the open
        frame, and that window is real on a flaky laptop link."""
        r = await rig()
        control = await r.connect_brain()
        reader, writer = await r.open_client()
        # Closed but not yet unregistered: exactly the race.
        control.closed.set()
        writer.write(client_hello(r.host))
        await writer.drain()

        await assert_closed(reader)
