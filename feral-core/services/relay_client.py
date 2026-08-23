"""The brain half of the relay tunnel: dial out, stay dialled, report state.

A brain lives behind NAT. There is no inbound port, no router rule and no
public IP, so a phone on cellular cannot open a connection to it. The
only thing that works from behind NAT is an *outbound* connection held
open, which is what this module is: the brain dials the relay edge and
keeps a control socket up, and the edge asks it to open a stream
whenever a phone arrives. Nothing about this requires the operator to
touch their router, which is the entire point.

Two properties are load-bearing and are enforced here rather than
documented somewhere else:

**1. A tunnel must never terminate into the main app.**
``APIKeyMiddleware.dispatch`` and the ``/v1/session`` websocket both
grant loopback a complete exemption from authentication, on the
reasonable assumption that a peer on 127.0.0.1 is the operator's own
dashboard. A tunnel breaks that assumption: it terminates on this
machine, so a phone (or anyone who reaches the edge) presents as
127.0.0.1. Pointing the tunnel at the brain's main port would therefore
publish an unauthenticated chat socket and an unauthenticated API to the
internet. So the tunnel terminates into :class:`UntrustedTunnelListener`,
a second uvicorn serving ``api.server:untrusted_app``, the pure-ASGI
wrapper that stamps ``feral.untrusted`` into every scope and turns both
bypasses off. There is deliberately no fallback to the main port: if the
untrusted listener is not up, tunnelling fails loudly instead of
degrading into the dangerous configuration.

**2. Connection state is reported, not merely retried.**
The iOS client's old behaviour was to retry silently, which is
indistinguishable from working right up until someone notices their
phone has not synced for a day. Every transition is written to a
module-level status record readable through :func:`relay_status`, so
``GET /api/access/status``, ``feral doctor`` and the web UI can all say
"reconnecting, 6 attempts, connection refused" instead of nothing. This
module does not import the API layer to do that; the accessor is plain
and the API layer reads it.

Wire protocol, fixed and shared with the edge implementation:

1. ``wss://<edge>/v1/tunnel`` is the control connection, held open.
2. First frame is ``{"type":"hello", relay_id, public_key, ts, nonce,
   signature}``, the signature covering the canonical JSON of the four
   signed fields. Those bytes must be identical to the ones
   ``feral_relay_cp.registration.canonical_payload`` reconstructs, or
   every signature fails verification; see :func:`canonical_hello_payload`.
3. The edge sends ``{"type":"open","stream_id":"<uuid>","deadline":10}``
   when a phone connects.
4. The brain dials ``wss://<edge>/v1/stream/<stream_id>`` and pipes raw
   binary between that socket and a local TCP connection.

This has never been run against a real edge. It is exercised end to end
against a local websockets server in ``tests/test_relay_client.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import secrets
import time
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger("feral.services.relay_client")

CONTROL_PATH = "/v1/tunnel"
STREAM_PATH = "/v1/stream/{stream_id}"

#: Retry ceiling. A brain whose edge is down should keep trying forever
#: (the operator's phone may come back long after the outage), but not
#: faster than once a minute once it is clear the edge is not coming
#: straight back.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

#: Read size for the local socket. Matches the usual TCP window growth
#: point; larger chunks buy nothing once the websocket is the bottleneck.
CHUNK_BYTES = 64 * 1024

#: Frame ceilings. The control channel carries small JSON, so anything
#: large on it is a bug or an attempt to exhaust memory on a machine
#: that is usually someone's laptop. The stream channel carries bulk
#: data and is capped generously rather than not at all.
CONTROL_MAX_FRAME_BYTES = 64 * 1024
STREAM_MAX_FRAME_BYTES = 8 * 1024 * 1024

#: How long a stream may take to establish when the edge does not say.
DEFAULT_STREAM_DEADLINE_SECONDS = 10.0

STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_FAILED = "failed"
#: Not in the reported set the API contract names, because it is not a
#: connection state: it is the absence of one. Reported so that "the
#: relay was never turned on" cannot be misread as "the relay is broken".
STATE_STOPPED = "stopped"


class RelayClientError(RuntimeError):
    """Base for relay configuration errors that must stop the tunnel."""


class LocalListenerUnavailable(RelayClientError):
    """There is no untrusted listener to terminate the tunnel into.

    Raised rather than falling back to the brain's main port. The
    fallback is the one outcome that must never happen: it would expose
    the loopback-exempt API and chat socket to the internet, and it
    would do so silently, at exactly the moment the operator believes
    they have configured remote access correctly.
    """


# ─────────────────────────────────────────────
# Reported state
# ─────────────────────────────────────────────

_status: dict = {
    "state": STATE_STOPPED,
    "last_error": None,
    "since": None,
    "attempts": 0,
}


def relay_status() -> dict:
    """The current relay connection state, as a plain dict.

    Deliberately a module-level function returning a copy rather than a
    live object: callers (the access-status route, ``feral doctor``, the
    web UI) get a snapshot they cannot mutate, and this module stays
    free of any import of the API layer. The API layer mirrors this onto
    ``state.relay``; the mirror is a cache and this is the source.
    """
    return dict(_status)


def reset_relay_status() -> None:
    """Test seam. Returns the reported state to "never started"."""
    _status.update(
        {"state": STATE_STOPPED, "last_error": None, "since": None, "attempts": 0}
    )


def _record_status(state: str, *, error: Optional[str] = None,
                   attempts: Optional[int] = None) -> dict:
    """Write a transition, refreshing ``since`` only when state changes.

    ``since`` answers "how long has it been like this", so a failed
    retry inside a reconnect run must not reset it. Operators use that
    number to tell a blip from an outage.
    """
    if _status["state"] != state:
        _status["since"] = time.time()
    _status["state"] = state
    if attempts is not None:
        _status["attempts"] = attempts
    if error is not None or state == STATE_CONNECTED:
        _status["last_error"] = error
    return dict(_status)


# ─────────────────────────────────────────────
# Hello frame
# ─────────────────────────────────────────────

def canonical_hello_payload(body: dict) -> bytes:
    """The exact bytes signed in the hello frame.

    Byte-identical to ``feral_relay_cp.registration.canonical_payload``
    on purpose, and kept as its own function so the equality can be
    asserted in a test rather than assumed. Sorted keys, no whitespace,
    and only the four signed fields: adding a field to the frame without
    adding it here would leave that field unauthenticated, which is how
    a signed protocol quietly stops protecting anything.
    """
    return json.dumps(
        {
            "relay_id": body["relay_id"],
            "public_key": body["public_key"],
            "ts": body["ts"],
            "nonce": body["nonce"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def build_hello_frame(*, relay_id: str, public_key: str,
                      sign: Callable[[bytes], str],
                      now: Optional[float] = None) -> dict:
    """Build and sign a hello frame.

    A fresh ``ts`` and ``nonce`` on every call, never cached. The
    control plane refuses a request whose ``ts`` is more than
    ``MAX_CLOCK_SKEW_SECONDS`` from its clock and refuses a nonce it has
    already seen, so replaying a stored hello on reconnect would be
    rejected as a replay: re-sending hello means re-signing it.
    """
    body = {
        "relay_id": relay_id,
        "public_key": public_key,
        "ts": time.time() if now is None else now,
        "nonce": secrets.token_hex(16),
    }
    frame = dict(body)
    frame["type"] = "hello"
    frame["signature"] = sign(canonical_hello_payload(body))
    return frame


# ─────────────────────────────────────────────
# The local terminus
# ─────────────────────────────────────────────

class UntrustedTunnelListener:
    """A loopback uvicorn serving ``untrusted_app``, and only that.

    This is where tunnel traffic is allowed to land. ``untrusted_app``
    is the pure-ASGI wrapper around the same FastAPI app the brain
    serves normally, differing in one respect that matters: it stamps
    ``feral.untrusted`` into every scope, including websocket scopes,
    so ``APIKeyMiddleware`` and the ``/v1/session`` handler both decline
    to hand out the loopback exemption. A remote peer arriving through
    the tunnel presents as 127.0.0.1 and would otherwise inherit the
    local dashboard's complete exemption from authentication.

    Bound on an ephemeral port because nothing else needs to find it.
    The tunnel is the only client, it learns the port from this object,
    and a port nobody advertises is one fewer thing on the machine for a
    local process to stumble into.

    Runs with ``lifespan="off"`` deliberately. The app object is shared
    with the brain's primary listener, which owns its lifecycle; running
    startup a second time would double-initialise background services,
    and stopping this listener would otherwise tear down the brain's.
    """

    def __init__(self, host: str = "127.0.0.1", *, startup_timeout: float = 10.0):
        self.host = host
        self._startup_timeout = startup_timeout
        self._server = None
        self._task: Optional[asyncio.Task] = None
        self._port: Optional[int] = None
        self._app = None

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def app(self):
        """The ASGI app being served. Asserted on in tests."""
        return self._app

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done() and self._port)

    async def start(self) -> int:
        """Bind and serve, returning the ephemeral port."""
        if self.running:
            return int(self._port)

        # Imported here, not at module scope, so that importing this
        # module (from the CLI, from doctor, from a test) does not drag
        # in the entire API surface. The dependency is one-directional
        # by design: the API layer may read this module's status, this
        # module only ever reaches for the app object.
        from api.server import untrusted_app, untrusted_uvicorn_app
        import uvicorn

        self._app = untrusted_app
        config = uvicorn.Config(
            untrusted_uvicorn_app,
            host=self.host,
            port=0,
            lifespan="off",
            log_level="warning",
            access_log=False,
            # The ASGI entrypoint already composes raw-peer capture outside
            # the loopback-only forwarded-header rewrite.
            proxy_headers=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(
            self._server.serve(), name="feral-relay-untrusted-listener"
        )

        deadline = time.monotonic() + self._startup_timeout
        while not getattr(self._server, "started", False):
            if self._task.done():
                # Surface the real bind error rather than a timeout.
                await self._task
                raise LocalListenerUnavailable(
                    "the untrusted tunnel listener exited during startup"
                )
            if time.monotonic() > deadline:
                await self.stop()
                raise LocalListenerUnavailable(
                    f"the untrusted tunnel listener did not start within "
                    f"{self._startup_timeout}s"
                )
            await asyncio.sleep(0.01)

        self._port = int(self._server.servers[0].sockets[0].getsockname()[1])
        logger.info(
            "relay: untrusted tunnel listener on %s:%d (serving untrusted_app)",
            self.host, self._port,
        )
        return self._port

    async def stop(self) -> None:
        """Stop serving and leave no task behind."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
            except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                logger.debug("relay: untrusted listener stop: %s", exc)
        self._task = None
        self._server = None
        self._port = None


# ─────────────────────────────────────────────
# The client
# ─────────────────────────────────────────────

def _ws_connect():
    """Return the websockets client callable available in this install.

    Prefers the ``websockets.asyncio`` implementation and falls back to
    the legacy one, because the legacy API is deprecated upstream and a
    dependency bump should not silently break remote access. Both accept
    the same keywords used here and both yield an object supporting
    ``send``/``recv``/``close`` and async iteration.
    """
    try:
        from websockets.asyncio.client import connect
        return connect
    except ImportError:  # pragma: no cover - older websockets
        import websockets
        return websockets.connect


def backoff_delay(attempt: int, *, base: float = BACKOFF_BASE_SECONDS,
                  cap: float = BACKOFF_CAP_SECONDS,
                  rand: Callable[[], float] = random.random) -> float:
    """Full jitter, capped. Same shape as the sync engine's peer retry.

    Full jitter rather than exponential-with-a-fixed-delay because every
    brain on a relay that just restarted would otherwise retry in
    lockstep and knock it over again on the first attempt after it comes
    back. Sampling uniformly below the ceiling spreads the herd.
    """
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return rand() * ceiling


class RelayClient:
    """Holds the control connection open and serves the streams it asks for.

    Constructed with an edge URL and a local terminus. The terminus is
    normally an :class:`UntrustedTunnelListener`; a bare ``local_port``
    is accepted for tests and for an operator running the untrusted app
    themselves, and is refused if it points at the brain's own port.
    """

    def __init__(
        self,
        edge_url: str,
        *,
        listener: Optional[UntrustedTunnelListener] = None,
        local_host: str = "127.0.0.1",
        local_port: Optional[int] = None,
        relay_id: Optional[str] = None,
        public_key: Optional[str] = None,
        sign: Optional[Callable[[bytes], str]] = None,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_cap: float = BACKOFF_CAP_SECONDS,
        max_attempts: Optional[int] = None,
        connect_timeout: float = 10.0,
    ):
        self.edge_url = edge_url.rstrip("/")
        self._listener = listener
        self._local_host = local_host
        self._local_port = local_port
        self._relay_id = relay_id
        self._public_key = public_key
        self._sign = sign
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._max_attempts = max_attempts
        self._connect_timeout = connect_timeout

        self._control_task: Optional[asyncio.Task] = None
        self._stream_tasks: set[asyncio.Task] = set()
        self._active_streams: set[str] = set()
        self._stopping = False
        self._control_ws = None

        #: Set while the control connection is up and hello has been
        #: sent. Exposed so callers (and tests) can wait for readiness
        #: without polling the status dict.
        self.connected = asyncio.Event()

    # -- identity ---------------------------------------------------

    def _identity(self) -> Tuple[str, str, Callable[[bytes], str]]:
        """Resolve relay id, public key and signer, injectable for tests."""
        if self._relay_id and self._public_key and self._sign:
            return self._relay_id, self._public_key, self._sign
        from security import brain_identity

        return (
            self._relay_id or brain_identity.relay_id(),
            self._public_key or brain_identity.public_key_b64(),
            self._sign or brain_identity.sign,
        )

    def hello_frame(self) -> dict:
        relay_id, public_key, sign = self._identity()
        return build_hello_frame(relay_id=relay_id, public_key=public_key, sign=sign)

    # -- local terminus ---------------------------------------------

    def local_target(self) -> Tuple[str, int]:
        """Where tunnelled bytes are allowed to land.

        Raises rather than guessing. The only guess available would be
        the brain's main port, and terminating a tunnel there is the
        exact failure this module exists to prevent, so an absent
        listener is a hard error and not a fallback.
        """
        if self._listener is not None:
            if not self._listener.running:
                raise LocalListenerUnavailable(
                    "the untrusted tunnel listener is not running. Refusing "
                    "to tunnel: the only other local target is the brain's "
                    "main port, which exempts loopback from authentication."
                )
            return self._listener.host, int(self._listener.port)

        if not self._local_port:
            raise LocalListenerUnavailable(
                "no untrusted tunnel listener and no local_port. Refusing to "
                "tunnel rather than fall back to the brain's main port, "
                "which would publish an unauthenticated API and chat socket."
            )

        from config.runtime import brain_port

        if int(self._local_port) == int(brain_port()):
            raise LocalListenerUnavailable(
                f"local_port {self._local_port} is the brain's main port. "
                "That listener serves the trusted app, which grants loopback "
                "a complete exemption from authentication, and a tunnel "
                "terminates on loopback. Serve untrusted_app instead."
            )
        return self._local_host, int(self._local_port)

    # -- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        """Validate the local terminus, then dial in the background.

        Returns as soon as the control task is spawned. A relay edge
        that is down, slow or wrong must never hold up brain boot: the
        brain is fully usable on the LAN without a tunnel, so the tunnel
        retries in the background and says so through
        :func:`relay_status`.
        """
        if self._control_task is not None and not self._control_task.done():
            return

        # Checked before dialling so a misconfiguration is a startup
        # error the operator sees, not a stream failure they discover
        # when their phone will not connect.
        try:
            self.local_target()
        except LocalListenerUnavailable as exc:
            _record_status(STATE_FAILED, error=str(exc), attempts=0)
            logger.error("relay: refusing to start: %s", exc)
            raise

        self._stopping = False
        self.connected.clear()
        _record_status(STATE_RECONNECTING, error=None, attempts=0)
        self._control_task = asyncio.create_task(
            self._control_loop(), name="feral-relay-control"
        )

    async def shutdown(self) -> None:
        """Stop everything and leave no orphaned task.

        Streams are cancelled before the control connection so an
        in-flight pump cannot re-register work against a socket that is
        about to close.
        """
        self._stopping = True

        for task in list(self._stream_tasks):
            task.cancel()
        if self._stream_tasks:
            await asyncio.gather(*list(self._stream_tasks), return_exceptions=True)
        self._stream_tasks.clear()
        self._active_streams.clear()

        ws, self._control_ws = self._control_ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

        task, self._control_task = self._control_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.connected.clear()
        _record_status(STATE_STOPPED, error=_status.get("last_error"))

    # -- control connection -------------------------------------------

    async def _control_loop(self) -> None:
        """Dial, hello, serve opens, and on any failure back off and repeat.

        One loop for the first dial and every reconnect, so a brain that
        boots before its network is up follows exactly the same path as
        one whose edge restarted, and neither has an untested branch.
        """
        connect = _ws_connect()
        url = f"{self.edge_url}{CONTROL_PATH}"
        attempts = 0

        while not self._stopping:
            try:
                ws = await asyncio.wait_for(
                    connect(url, max_size=CONTROL_MAX_FRAME_BYTES),
                    timeout=self._connect_timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any dial failure retries
                attempts += 1
                _record_status(STATE_RECONNECTING, error=f"{type(exc).__name__}: {exc}",
                               attempts=attempts)
                logger.warning("relay: control dial failed (attempt %d): %s", attempts, exc)
                if not await self._sleep_before_retry(attempts):
                    return
                continue

            self._control_ws = ws
            try:
                async with ws:
                    # Re-signed on every dial. A stored hello would be
                    # refused by the control plane as a replayed nonce.
                    await ws.send(json.dumps(self.hello_frame()))
                    attempts = 0
                    self.connected.set()
                    _record_status(STATE_CONNECTED, error=None, attempts=0)
                    logger.info("relay: control connection up (%s)", url)

                    async for raw in ws:
                        await self._handle_control_frame(raw)

                reason = "control connection closed by the edge"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any drop reconnects
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                self._control_ws = None
                self.connected.clear()

            if self._stopping:
                return

            attempts += 1
            _record_status(STATE_RECONNECTING, error=reason, attempts=attempts)
            logger.warning("relay: control connection lost (attempt %d): %s",
                           attempts, reason)
            if not await self._sleep_before_retry(attempts):
                return

    async def _sleep_before_retry(self, attempts: int) -> bool:
        """Back off. False means give up and report failure."""
        if self._max_attempts is not None and attempts >= self._max_attempts:
            _record_status(
                STATE_FAILED,
                error=_status.get("last_error") or "retries exhausted",
                attempts=attempts,
            )
            logger.error("relay: giving up after %d attempts", attempts)
            return False
        try:
            await asyncio.sleep(
                backoff_delay(attempts, base=self._backoff_base, cap=self._backoff_cap)
            )
        except asyncio.CancelledError:
            raise
        return not self._stopping

    async def _handle_control_frame(self, raw: Any) -> None:
        """Act on one control frame, or ignore it and stay up.

        Every rejection path here is a log line and a return. A control
        connection that dies on a frame it did not expect is a tunnel
        that one bad deploy on the edge can take down for every brain at
        once, and reconnecting would not help because the next frame
        would be just as bad.
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", errors="replace")
        try:
            frame = json.loads(raw)
        except Exception:
            logger.warning("relay: ignoring unparseable control frame (%d bytes)",
                           len(raw) if raw is not None else 0)
            return
        if not isinstance(frame, dict):
            logger.warning("relay: ignoring non-object control frame: %r", type(frame))
            return

        kind = frame.get("type")
        if kind != "open":
            logger.debug("relay: ignoring control frame of type %r", kind)
            return

        stream_id = frame.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id.strip():
            logger.warning("relay: ignoring open frame with no usable stream_id")
            return
        stream_id = stream_id.strip()

        if stream_id in self._active_streams:
            logger.warning("relay: ignoring duplicate open for stream %s", stream_id)
            return

        try:
            deadline = float(frame.get("deadline") or DEFAULT_STREAM_DEADLINE_SECONDS)
        except (TypeError, ValueError):
            deadline = DEFAULT_STREAM_DEADLINE_SECONDS
        if deadline <= 0:
            deadline = DEFAULT_STREAM_DEADLINE_SECONDS

        self._active_streams.add(stream_id)
        task = asyncio.create_task(
            self._serve_stream(stream_id, deadline), name=f"feral-relay-stream-{stream_id}"
        )
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_finished(stream_id))

    def _stream_finished(self, stream_id: str) -> Callable[[asyncio.Task], None]:
        def _done(task: asyncio.Task) -> None:
            self._stream_tasks.discard(task)
            self._active_streams.discard(stream_id)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.warning("relay: stream %s ended in error: %s", stream_id, exc)
        return _done

    # -- streams -------------------------------------------------------

    async def _serve_stream(self, stream_id: str, deadline: float) -> None:
        """Pipe one stream between the edge and the local untrusted app.

        A failure here is reported and contained. The control connection
        is not touched: one phone failing to connect, or one local
        socket refusing, must not disconnect the brain from the relay
        and take every other stream down with it.
        """
        connect = _ws_connect()
        url = f"{self.edge_url}{STREAM_PATH.format(stream_id=stream_id)}"
        ws = None
        reader = writer = None

        try:
            host, port = self.local_target()
        except LocalListenerUnavailable as exc:
            _record_status(_status["state"], error=str(exc))
            logger.error("relay: cannot serve stream %s: %s", stream_id, exc)
            return

        try:
            ws = await asyncio.wait_for(
                connect(url, max_size=STREAM_MAX_FRAME_BYTES), timeout=deadline
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _record_status(_status["state"],
                           error=f"stream dial failed: {type(exc).__name__}: {exc}")
            logger.warning("relay: stream %s could not reach the edge: %s", stream_id, exc)
            return

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=deadline
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        except Exception as exc:  # noqa: BLE001
            # The local listener refused or is gone. Report it, close
            # this stream, and leave the control connection alone.
            _record_status(
                _status["state"],
                error=f"local listener refused stream: {type(exc).__name__}: {exc}",
            )
            logger.error(
                "relay: stream %s could not reach the untrusted listener at "
                "%s:%s (%s). The stream is refused; not falling back to any "
                "other local port.", stream_id, host, port, exc,
            )
            with contextlib.suppress(Exception):
                await ws.close(code=1011, reason="local listener unavailable")
            return

        logger.info("relay: stream %s open -> %s:%d", stream_id, host, port)
        try:
            await self._pump(ws, reader, writer)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            with contextlib.suppress(Exception):
                await ws.close()
            logger.debug("relay: stream %s closed", stream_id)

    async def _pump(self, ws, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter) -> None:
        """Copy bytes both ways until either side ends.

        Whichever direction finishes first ends the stream, and the
        other is cancelled and awaited. Leaving the second direction
        running would leak a task per phone connection, which on a
        long-lived brain is a slow memory leak that only shows up after
        the operator has stopped watching.
        """
        to_local = asyncio.create_task(self._ws_to_tcp(ws, writer))
        to_edge = asyncio.create_task(self._tcp_to_ws(reader, ws))
        try:
            done, pending = await asyncio.wait(
                {to_local, to_edge}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logger.debug("relay: pump ended: %s", exc)
        except asyncio.CancelledError:
            for task in (to_local, to_edge):
                task.cancel()
            await asyncio.gather(to_local, to_edge, return_exceptions=True)
            raise

    @staticmethod
    async def _ws_to_tcp(ws, writer: asyncio.StreamWriter) -> None:
        async for message in ws:
            if isinstance(message, str):
                # The tunnel is binary, but a text frame still carries
                # payload bytes. Encoding rather than dropping it means
                # a peer that mislabels a frame corrupts nothing.
                message = message.encode("utf-8")
            writer.write(message)
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.write_eof()

    @staticmethod
    async def _tcp_to_ws(reader: asyncio.StreamReader, ws) -> None:
        while True:
            data = await reader.read(CHUNK_BYTES)
            if not data:
                break
            await ws.send(data)
        with contextlib.suppress(Exception):
            await ws.close()

    # -- reporting -----------------------------------------------------

    def status(self) -> dict:
        """This client's view of the reported state. Same dict as the accessor."""
        return relay_status()
