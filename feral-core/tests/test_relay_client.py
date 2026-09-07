"""Tests for the brain-side relay tunnel client.

No real network and no real edge: every test runs a local ``websockets``
server standing in for the edge, and a local TCP server standing in for
whatever the tunnel terminates into. What is proved here is the wire
contract (a hello the control plane would actually accept), the failure
behaviour (retry, report, contain), and the one security property the
design rests on: tunnelled traffic lands on ``untrusted_app`` and is
therefore denied the loopback authentication bypass.

This has never been run against a real relay edge.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import random
import socket
import sys
import time
from pathlib import Path

import pytest
import websockets

from services import relay_client as rc
from services.relay_client import (
    LocalListenerUnavailable,
    RelayClient,
    UntrustedTunnelListener,
    backoff_delay,
    build_hello_frame,
    canonical_hello_payload,
    relay_status,
)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_relay_status():
    rc.reset_relay_status()
    yield
    rc.reset_relay_status()


def _test_identity():
    """A throwaway Ed25519 identity, so no test touches the real vault."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    from security.brain_identity import derive_relay_id

    key = ed25519.Ed25519PrivateKey.generate()
    pub_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key_b64 = base64.b64encode(pub_raw).decode("ascii")

    def sign(payload: bytes) -> str:
        return base64.b64encode(key.sign(payload)).decode("ascii")

    return derive_relay_id(pub_raw), public_key_b64, sign


async def _wait_until(predicate, timeout: float = 5.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} was not met within {timeout}s")


def _closed_port() -> int:
    """A port nothing is listening on, for connection-refused paths."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FakeEdge:
    """A local stand-in for the relay edge, speaking the agreed protocol."""

    def __init__(self, *, drop_first_controls: int = 0):
        self.url = ""
        self.hellos: list[dict] = []
        self.control_sockets: list = []
        self.drop_first_controls = drop_first_controls
        self._server = None
        self._pending: dict[str, asyncio.Future] = {}
        self.control_opened = 0

    async def start(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        port = list(self._server.sockets)[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @staticmethod
    def _request_path(ws) -> str:
        """The requested path, across both ``websockets`` server APIs.

        ``websockets.serve`` changed what it hands the handler in 14.0:
        the legacy implementation passes a ``WebSocketServerProtocol``
        carrying ``.path``, the asyncio implementation (the default from
        14.0 on) passes a ``ServerConnection`` where the path lives at
        ``.request.path`` and ``.path`` does not exist at all.

        Reading only ``.path`` made every FakeEdge connection look like
        an unknown path under the pinned ``websockets==15.0.1``, so the
        edge closed with 1008 and no hello was ever recorded. Locally,
        where an older ``websockets`` resolves ``serve`` to the legacy
        implementation, the same code passed. Ask both, newest first.
        """
        request = getattr(ws, "request", None)
        path = getattr(request, "path", None)
        if path is None:
            path = getattr(ws, "path", None)
        return path or ""

    async def _handle(self, ws):
        path = self._request_path(ws)
        if path == rc.CONTROL_PATH:
            await self._control(ws)
        elif path.startswith("/v1/stream/"):
            await self._stream(ws, path.rsplit("/", 1)[-1])
        else:  # pragma: no cover - defensive
            await ws.close(code=1008, reason="unknown path")

    async def _control(self, ws):
        self.control_opened += 1
        raw = await ws.recv()
        self.hellos.append(json.loads(raw))
        if self.control_opened <= self.drop_first_controls:
            await ws.close(code=1012, reason="edge restarting")
            return
        self.control_sockets.append(ws)
        try:
            async for _ in ws:
                pass
        except Exception:
            pass
        finally:
            with contextlib.suppress(ValueError):
                self.control_sockets.remove(ws)

    async def _stream(self, ws, stream_id: str):
        fut = self._pending.get(stream_id)
        if fut is not None and not fut.done():
            fut.set_result(ws)
        with contextlib.suppress(Exception):
            await ws.wait_closed()

    async def open_stream(self, stream_id: str, *, deadline: float = 5.0):
        """Ask the brain to dial a stream; resolves to the edge-side socket."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[stream_id] = fut
        await self.control_sockets[-1].send(
            json.dumps({"type": "open", "stream_id": stream_id, "deadline": deadline})
        )
        return fut

    async def send_raw(self, payload):
        await self.control_sockets[-1].send(payload)


class EchoServer:
    """A local TCP echo server: the stand-in for the tunnel's terminus."""

    def __init__(self):
        self.port = 0
        self._server = None

    async def start(self) -> int:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    writer.close()

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


@contextlib.asynccontextmanager
async def running_client(**kwargs):
    client = RelayClient(**kwargs)
    try:
        await client.start()
        yield client
    finally:
        await client.shutdown()


# ─────────────────────────────────────────────
# The hello frame / wire contract
# ─────────────────────────────────────────────

def _load_control_plane_registration():
    """Import the control plane's registration module, or None.

    It lives in a sibling package that is not on feral-core's path, so
    it is loaded by file. Skipping when it is absent is deliberate:
    feral-core must remain testable without the relay repo checked out,
    but when it is present the byte-identity of the signed payload is
    the single most important thing to assert.
    """
    root = Path(__file__).resolve().parents[2] / "feral-relay" / "control"
    if not (root / "feral_relay_cp" / "registration.py").exists():
        return None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import feral_relay_cp.registration as registration
        return registration
    except Exception:
        return None


def test_canonical_payload_is_byte_identical_to_the_control_plane():
    """A mismatch here means every signature fails verification."""
    registration = _load_control_plane_registration()
    if registration is None:
        pytest.skip("feral-relay control plane not available")

    body = {
        "relay_id": "abcdefghijklmnopqrstuvwxyz234567",
        "public_key": "Zm9vYmFyYmF6",
        "ts": 1770000000.5,
        "nonce": "9f2c" * 8,
        # Unsigned fields must not leak into the signed bytes on either side.
        "type": "hello",
        "signature": "ignored",
    }
    assert canonical_hello_payload(body) == registration.canonical_payload(body)


def test_hello_frame_is_accepted_by_the_control_plane_registration():
    """End-to-end proof that the frame we send is one the edge accepts."""
    registration = _load_control_plane_registration()
    if registration is None:
        pytest.skip("feral-relay control plane not available")

    relay_id, public_key, sign = _test_identity()
    frame = build_hello_frame(relay_id=relay_id, public_key=public_key, sign=sign)

    class _Store:
        def __init__(self):
            self.records = {}
            self.nonces = set()

        def get(self, rid):
            return self.records.get(rid)

        def put(self, record):
            self.records[record.relay_id] = record

        def seen_nonce(self, rid, nonce):
            return (rid, nonce) in self.nonces

        def remember_nonce(self, rid, nonce, expires_at):
            self.nonces.add((rid, nonce))

    record = registration.register(frame, _Store())
    assert record.relay_id == relay_id


def test_hello_signature_verifies_against_the_advertised_public_key():
    from security import brain_identity

    relay_id, public_key, sign = _test_identity()
    frame = build_hello_frame(relay_id=relay_id, public_key=public_key, sign=sign)

    assert frame["type"] == "hello"
    assert set(frame) == {"type", "relay_id", "public_key", "ts", "nonce", "signature"}
    assert brain_identity.verify(
        canonical_hello_payload(frame), frame["signature"], public_key
    )


def test_each_hello_carries_a_fresh_nonce_and_timestamp():
    """A cached hello would be refused by the control plane as a replay."""
    relay_id, public_key, sign = _test_identity()
    frames = [
        build_hello_frame(relay_id=relay_id, public_key=public_key, sign=sign)
        for _ in range(5)
    ]
    assert len({f["nonce"] for f in frames}) == 5
    assert all(abs(f["ts"] - time.time()) < 60 for f in frames)


def test_client_uses_brain_identity_when_none_is_injected(monkeypatch):
    from security import brain_identity

    relay_id, public_key, sign = _test_identity()
    monkeypatch.setattr(brain_identity, "relay_id", lambda config=None: relay_id)
    monkeypatch.setattr(brain_identity, "public_key_b64", lambda: public_key)
    monkeypatch.setattr(brain_identity, "sign", sign)

    client = RelayClient("wss://edge.invalid", local_port=_closed_port())
    frame = client.hello_frame()
    assert frame["relay_id"] == relay_id
    assert frame["public_key"] == public_key
    assert brain_identity.verify(
        canonical_hello_payload(frame), frame["signature"], public_key
    )


# ─────────────────────────────────────────────
# Backoff
# ─────────────────────────────────────────────

def test_backoff_is_full_jitter_and_capped():
    assert backoff_delay(1, base=1.0, cap=60.0, rand=lambda: 1.0) == 1.0
    assert backoff_delay(4, base=1.0, cap=60.0, rand=lambda: 1.0) == 8.0
    assert backoff_delay(20, base=1.0, cap=60.0, rand=lambda: 1.0) == 60.0
    assert backoff_delay(5, base=1.0, cap=60.0, rand=lambda: 0.0) == 0.0

    rng = random.Random(1234)
    for attempt in range(1, 25):
        delay = backoff_delay(attempt, base=1.0, cap=60.0, rand=rng.random)
        assert 0.0 <= delay <= 60.0


# ─────────────────────────────────────────────
# Failure modes
# ─────────────────────────────────────────────

async def test_unreachable_edge_retries_without_blocking_boot():
    """Brain boot must not wait on a relay that is down."""
    dead = f"ws://127.0.0.1:{_closed_port()}"
    echo = EchoServer()
    await echo.start()
    relay_id, public_key, sign = _test_identity()

    started = time.monotonic()
    async with running_client(
        edge_url=dead,
        local_port=echo.port,
        relay_id=relay_id,
        public_key=public_key,
        sign=sign,
        backoff_base=0.01,
        backoff_cap=0.05,
        connect_timeout=1.0,
    ):
        assert time.monotonic() - started < 1.0, "start() blocked on the edge"
        await _wait_until(lambda: relay_status()["attempts"] >= 2,
                          what="retry attempts")
        status = relay_status()
        assert status["state"] == "reconnecting"
        assert status["last_error"]
        assert status["since"] is not None
    await echo.stop()


async def test_control_drop_reconnects_resends_hello_and_resumes_opens():
    edge = FakeEdge(drop_first_controls=1)
    await edge.start()
    echo = EchoServer()
    await echo.start()
    relay_id, public_key, sign = _test_identity()

    async with running_client(
        edge_url=edge.url,
        local_port=echo.port,
        relay_id=relay_id,
        public_key=public_key,
        sign=sign,
        backoff_base=0.01,
        backoff_cap=0.05,
    ) as client:
        await _wait_until(lambda: len(edge.hellos) >= 2, what="second hello")
        await _wait_until(lambda: client.connected.is_set(), what="reconnect")

        # Re-sent, not replayed.
        assert edge.hellos[0]["nonce"] != edge.hellos[1]["nonce"]
        assert edge.hellos[0]["signature"] != edge.hellos[1]["signature"]

        # Opens are served again on the fresh control connection.
        fut = await edge.open_stream("stream-after-reconnect")
        stream_ws = await asyncio.wait_for(fut, timeout=5)
        await stream_ws.send(b"still here")
        assert await asyncio.wait_for(stream_ws.recv(), timeout=5) == b"still here"

    await echo.stop()
    await edge.stop()


async def test_refused_local_socket_is_reported_and_control_survives():
    edge = FakeEdge()
    await edge.start()
    relay_id, public_key, sign = _test_identity()
    dead_local = _closed_port()

    async with running_client(
        edge_url=edge.url,
        local_port=dead_local,
        relay_id=relay_id,
        public_key=public_key,
        sign=sign,
        backoff_base=0.01,
        backoff_cap=0.05,
    ) as client:
        await _wait_until(lambda: client.connected.is_set(), what="control up")

        fut = await edge.open_stream("stream-refused")
        stream_ws = await asyncio.wait_for(fut, timeout=5)

        # The brain closes this stream and nothing else.
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(stream_ws.recv(), timeout=5)

        await _wait_until(
            lambda: "local listener refused" in (relay_status()["last_error"] or ""),
            what="refusal reported",
        )
        assert client.connected.is_set(), "control connection was torn down"
        assert relay_status()["state"] == "connected"
        assert edge.control_opened == 1, "the client reconnected when it should not"

        # And it keeps accepting opens afterwards.
        fut2 = await edge.open_stream("stream-refused-2")
        await asyncio.wait_for(fut2, timeout=5)

    await edge.stop()


async def test_malformed_control_frames_are_ignored_and_the_client_stays_up():
    edge = FakeEdge()
    await edge.start()
    echo = EchoServer()
    await echo.start()
    relay_id, public_key, sign = _test_identity()

    async with running_client(
        edge_url=edge.url,
        local_port=echo.port,
        relay_id=relay_id,
        public_key=public_key,
        sign=sign,
        backoff_base=0.01,
        backoff_cap=0.05,
    ) as client:
        await _wait_until(lambda: client.connected.is_set(), what="control up")

        for junk in (
            "this is not json",
            "[1, 2, 3]",
            '"just a string"',
            json.dumps({"type": "open"}),                      # no stream_id
            json.dumps({"type": "open", "stream_id": "   "}),  # blank stream_id
            json.dumps({"type": "open", "stream_id": 42}),     # wrong type
            json.dumps({"type": "unheard-of"}),
            json.dumps({"no_type": True}),
            b"\x00\xff\xfe binary garbage",
        ):
            await edge.send_raw(junk)

        # Still connected, and a good frame still works.
        await asyncio.sleep(0.1)
        assert client.connected.is_set()
        assert edge.control_opened == 1

        fut = await edge.open_stream("stream-after-junk")
        stream_ws = await asyncio.wait_for(fut, timeout=5)
        await stream_ws.send(b"ok")
        assert await asyncio.wait_for(stream_ws.recv(), timeout=5) == b"ok"

    await echo.stop()
    await edge.stop()


async def test_missing_untrusted_listener_fails_loudly():
    """No listener means no tunnel. There is deliberately no fallback."""
    edge = FakeEdge()
    await edge.start()
    relay_id, public_key, sign = _test_identity()

    client = RelayClient(
        edge_url=edge.url,
        relay_id=relay_id,
        public_key=public_key,
        sign=sign,
    )
    with pytest.raises(LocalListenerUnavailable):
        await client.start()

    assert relay_status()["state"] == "failed"
    assert "main port" in (relay_status()["last_error"] or "")
    assert edge.hellos == [], "dialled the edge with nowhere to terminate"

    await client.shutdown()
    await edge.stop()


async def test_stopped_listener_refuses_rather_than_falling_back():
    listener = UntrustedTunnelListener()
    await listener.start()
    await listener.stop()

    client = RelayClient("ws://127.0.0.1:1", listener=listener)
    with pytest.raises(LocalListenerUnavailable):
        client.local_target()


async def test_refuses_to_terminate_into_the_brains_main_port(monkeypatch):
    """The one configuration that would publish an unauthenticated API."""
    import config.runtime as runtime

    echo = EchoServer()
    await echo.start()
    # Patched rather than set through ``FERAL_PORT`` so this test does
    # not leave an environment variable behind for the rest of the
    # suite; the guard's contract is "whatever ``brain_port()`` says".
    monkeypatch.setattr(runtime, "brain_port", lambda: echo.port)

    client = RelayClient("ws://127.0.0.1:1", local_port=echo.port)
    with pytest.raises(LocalListenerUnavailable) as excinfo:
        await client.start()
    assert "main port" in str(excinfo.value)
    assert relay_status()["state"] == "failed"

    await echo.stop()


async def test_shutdown_leaves_no_orphaned_tasks():
    edge = FakeEdge()
    await edge.start()
    echo = EchoServer()
    await echo.start()
    relay_id, public_key, sign = _test_identity()

    before = {t for t in asyncio.all_tasks() if not t.done()}

    client = RelayClient(
        edge_url=edge.url,
        local_port=echo.port,
        relay_id=relay_id,
        public_key=public_key,
        sign=sign,
        backoff_base=0.01,
        backoff_cap=0.05,
    )
    await client.start()
    await _wait_until(lambda: client.connected.is_set(), what="control up")

    fut = await edge.open_stream("stream-shutdown")
    stream_ws = await asyncio.wait_for(fut, timeout=5)
    await stream_ws.send(b"in flight")
    assert await asyncio.wait_for(stream_ws.recv(), timeout=5) == b"in flight"

    await client.shutdown()
    await asyncio.sleep(0.05)

    leaked = {
        t for t in asyncio.all_tasks()
        if not t.done() and t not in before and t is not asyncio.current_task()
        and (t.get_name() or "").startswith("feral-relay")
    }
    assert not leaked, f"orphaned relay tasks: {[t.get_name() for t in leaked]}"
    assert relay_status()["state"] == "stopped"

    await echo.stop()
    await edge.stop()


# ─────────────────────────────────────────────
# Byte integrity
# ─────────────────────────────────────────────

async def test_bytes_survive_the_round_trip_intact_and_in_order():
    edge = FakeEdge()
    await edge.start()
    echo = EchoServer()
    await echo.start()
    relay_id, public_key, sign = _test_identity()

    payload = random.Random(20260805).randbytes(512 * 1024)

    async with running_client(
        edge_url=edge.url,
        local_port=echo.port,
        relay_id=relay_id,
        public_key=public_key,
        sign=sign,
        backoff_base=0.01,
        backoff_cap=0.05,
    ) as client:
        await _wait_until(lambda: client.connected.is_set(), what="control up")
        fut = await edge.open_stream("stream-bytes")
        stream_ws = await asyncio.wait_for(fut, timeout=5)

        async def _send_all():
            for offset in range(0, len(payload), 8192):
                await stream_ws.send(payload[offset:offset + 8192])

        sender = asyncio.create_task(_send_all())
        received = bytearray()
        while len(received) < len(payload):
            chunk = await asyncio.wait_for(stream_ws.recv(), timeout=10)
            received.extend(chunk if isinstance(chunk, bytes) else chunk.encode())
        await sender

        assert len(received) == len(payload)
        assert bytes(received) == payload

    await echo.stop()
    await edge.stop()


# ─────────────────────────────────────────────
# The security property this module exists for
# ─────────────────────────────────────────────

async def test_tunnel_listener_serves_untrusted_app_and_not_app():
    import api.server as server
    from config.runtime import brain_port

    listener = UntrustedTunnelListener()
    try:
        port = await listener.start()
        assert listener.app is server.untrusted_app
        assert listener.app is not server.app
        assert isinstance(listener.app, server.UntrustedTransport)
        assert listener.app.app is server.app
        assert listener._server.config.proxy_headers is False
        assert port and port != brain_port()
        assert listener.host == "127.0.0.1"
    finally:
        await listener.stop()
    assert not listener.running


def test_untrusted_transport_denies_the_loopback_bypass():
    """The contrast the tunnel depends on, asserted directly.

    Same request, same (loopback) client, two apps: the trusted one
    waves it through to routing and 404s, the untrusted one demands
    credentials. If this ever stops holding, terminating a tunnel
    anywhere on this machine publishes the whole API.
    """
    from starlette.testclient import TestClient
    import api.server as server
    from security import session_auth

    # Not used as context managers on purpose: entering one runs the
    # app's lifespan, which boots the brain's background services inside
    # the test process. Routing and middleware, which is all this
    # asserts on, do not need startup to have run.
    probe = "/api/__relay_probe_should_not_exist__"
    assert TestClient(server.app).get(probe).status_code == 404
    assert TestClient(server.untrusted_app).get(probe).status_code == 401

    scope = {"type": "websocket", "path": "/v1/session"}
    seen: dict = {}

    async def _record(scope, receive, send):
        seen.update(scope)

    asyncio.run(server.UntrustedTransport(_record)(scope, None, None))
    assert seen[session_auth.TRUSTED_TRANSPORT_SCOPE_KEY] is True
    assert session_auth.transport_is_trusted(seen) is False


async def test_tunnelled_http_request_is_denied_the_loopback_bypass():
    """The real path: bytes in at the edge, 401 out of the local app.

    A phone reaching the brain through the relay arrives on 127.0.0.1.
    Without the untrusted transport it would inherit the dashboard's
    complete exemption from authentication, so this asserts on the
    status line of an actual response pulled back through the tunnel.
    """
    edge = FakeEdge()
    await edge.start()
    listener = UntrustedTunnelListener()
    await listener.start()
    relay_id, public_key, sign = _test_identity()

    try:
        async with running_client(
            edge_url=edge.url,
            listener=listener,
            relay_id=relay_id,
            public_key=public_key,
            sign=sign,
            backoff_base=0.01,
            backoff_cap=0.05,
        ) as client:
            await _wait_until(lambda: client.connected.is_set(), what="control up")
            fut = await edge.open_stream("stream-http")
            stream_ws = await asyncio.wait_for(fut, timeout=5)

            request = (
                "GET /api/__relay_probe_should_not_exist__ HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{listener.port}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            await stream_ws.send(request)

            response = bytearray()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                while b"\r\n\r\n" not in response or len(response) < 32:
                    chunk = await asyncio.wait_for(stream_ws.recv(), timeout=10)
                    response.extend(chunk if isinstance(chunk, bytes) else chunk.encode())

            head = bytes(response).split(b"\r\n", 1)[0]
            assert head.startswith(b"HTTP/1.1 401"), (
                f"tunnelled request was not challenged: {bytes(response)[:200]!r}"
            )
    finally:
        await listener.stop()
        await edge.stop()


# ─────────────────────────────────────────────
# Reported state
# ─────────────────────────────────────────────

async def test_status_reports_connected_then_reconnecting():
    edge = FakeEdge()
    await edge.start()
    echo = EchoServer()
    await echo.start()
    relay_id, public_key, sign = _test_identity()

    assert relay_status() == {
        "state": "stopped", "last_error": None, "since": None, "attempts": 0,
    }

    async with running_client(
        edge_url=edge.url,
        local_port=echo.port,
        relay_id=relay_id,
        public_key=public_key,
        sign=sign,
        backoff_base=0.01,
        backoff_cap=0.05,
        connect_timeout=1.0,
    ) as client:
        await _wait_until(lambda: client.connected.is_set(), what="control up")
        status = client.status()
        assert set(status) == {"state", "last_error", "since", "attempts"}
        assert status["state"] == "connected"
        assert status["attempts"] == 0
        assert status["last_error"] is None
        assert isinstance(status["since"], float)

        connected_since = status["since"]
        await edge.stop()

        await _wait_until(lambda: relay_status()["state"] == "reconnecting",
                          what="reconnecting state")
        dropped = relay_status()
        assert dropped["attempts"] >= 1
        assert dropped["last_error"]
        assert dropped["since"] >= connected_since

        # ``since`` marks when the state was entered, not the last retry,
        # so an operator can tell a blip from an outage.
        entered = dropped["since"]
        await _wait_until(lambda: relay_status()["attempts"] >= dropped["attempts"] + 2,
                          what="further retries")
        assert relay_status()["since"] == entered

    await echo.stop()


async def test_exhausted_retries_report_failed():
    dead = f"ws://127.0.0.1:{_closed_port()}"
    echo = EchoServer()
    await echo.start()

    async with running_client(
        edge_url=dead,
        local_port=echo.port,
        relay_id="r", public_key="p", sign=lambda payload: "sig",
        backoff_base=0.001,
        backoff_cap=0.005,
        max_attempts=3,
        connect_timeout=1.0,
    ):
        await _wait_until(lambda: relay_status()["state"] == "failed",
                          what="failed state")
        assert relay_status()["attempts"] == 3
        assert relay_status()["last_error"]

    await echo.stop()
