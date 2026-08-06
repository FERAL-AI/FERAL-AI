"""Who is reachable right now, and which streams are in flight.

The edge cannot answer "where does this connection go" from a database.
By the time the ClientHello has been read the only useful answer is a
live socket held open by a brain that dialled out minutes ago. This
module is that answer and nothing else: it holds no configuration, opens
nothing, and performs no IO, so the broker's failure handling can be
exercised without a network.

Three rules shape everything here.

* **Bookkeeping never awaits.** A single-threaded event loop cannot
  interleave another coroutine between two statements that contain no
  await point, so every mutator below is atomic by construction and
  needs no lock. A lock would not fix the failure that actually bites,
  which is a check-then-act sequence split across two calls, so where
  such a sequence exists it is collapsed into one method instead:
  :meth:`TunnelRegistry.reserve_stream` looks up the connection, checks
  the cap and inserts the slot as one step. Two connections arriving in
  the same tick therefore cannot both observe 31 streams and both make
  it 32.

* **A slot belongs to the connection it was opened on, not to the relay
  id.** A brain whose control socket drops and reconnects gets a new
  connection object for the same id. If cleanup were keyed by id alone,
  the dying connection's teardown would tear down its replacement's
  streams, and a brain would lose every stream each time its control
  channel flapped. Every removal is therefore identity-checked.

* **A reservation expires on its own.** The broker releases slots it
  finishes with, but a broker task that dies between reserving and
  splicing must not permanently consume part of a brain's budget.
  Reservations carry a deadline and are swept when the cap is next
  consulted, which is exactly the moment the leak would matter.

The concurrency cap exists because a stream reservation costs the brain
a real socket. Without it, one client opening connections in a loop
would have the edge ask a laptop to dial back thousands of times, and
the denial of service would land on the user rather than on us.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

#: How many streams one brain may have reserved or spliced at once.
#: A brain serving a handful of browser tabs uses a few; 32 leaves room
#: for a page fanning out to many origins without letting one client
#: exhaust a laptop.
DEFAULT_STREAM_LIMIT = 32

#: How long a reservation stays valid while waiting for the brain to
#: dial back. Matches the deadline the edge advertises in its ``open``
#: frame, so the two sides give up at the same time.
DEFAULT_STREAM_TTL_SECONDS = 10.0


class RegistryError(Exception):
    """A request the registry refuses, with a reason worth logging."""


class RelayNotConnected(RegistryError):
    """No brain holds a control channel for this relay id right now."""


class StreamLimitReached(RegistryError):
    """This brain already has as many streams as it is allowed."""


class UnknownStream(RegistryError):
    """The stream id was never issued, was already used, or has expired.

    Deliberately one exception for all three. Telling a caller which of
    the three it was would let an unauthenticated peer probe for live
    stream ids by watching the difference.
    """


class ControlConnection:
    """A brain's live control channel.

    Compared by identity, never by relay id. Two connections claiming
    the same id exist for real during a reconnect, and the distinction
    between them is the whole reason cleanup does not evict the wrong
    one.
    """

    __slots__ = ("relay_id", "ws", "connected_at")

    def __init__(self, relay_id: str, ws: Any, connected_at: float = 0.0) -> None:
        self.relay_id = relay_id
        self.ws = ws
        self.connected_at = connected_at

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ControlConnection {self.relay_id} at {id(self):#x}>"


class StreamSlot:
    """One reserved place in a brain's concurrency budget.

    Created when the edge decides to ask a brain for a stream, and alive
    until the splice ends. It carries the two synchronisation points the
    broker needs: the waiting TCP side blocks on :meth:`wait_attached`
    until the brain dials back, and the stream handler blocks on
    :attr:`closed` so its WebSocket stays open for as long as the splice
    is running.
    """

    __slots__ = (
        "stream_id",
        "relay_id",
        "connection",
        "expires_at",
        "closed",
        "_attached",
        "_claimed",
        "_released",
    )

    def __init__(
        self,
        stream_id: str,
        relay_id: str,
        connection: ControlConnection,
        expires_at: float,
    ) -> None:
        self.stream_id = stream_id
        self.relay_id = relay_id
        self.connection = connection
        self.expires_at = expires_at
        # Both are created here rather than lazily so that a caller
        # cannot observe a slot whose synchronisation points do not
        # exist yet. This requires a running loop, which is where the
        # broker always is.
        self.closed = asyncio.Event()
        self._attached: asyncio.Future = asyncio.get_running_loop().create_future()
        self._claimed = False
        self._released = False

    @property
    def claimed(self) -> bool:
        return self._claimed

    @property
    def released(self) -> bool:
        return self._released

    def attach(self, channel: Any) -> bool:
        """Hand the dialled-back stream to whoever is waiting for it.

        Returns ``False`` if the slot is already settled, which happens
        when the TCP side gave up a moment ago or when a second dial
        arrives for the same stream id. The caller must close the
        channel in that case: silently dropping it would leave the brain
        holding a socket the edge is never going to read.
        """
        if self._released or self._attached.done():
            return False
        self._attached.set_result(channel)
        return True

    async def wait_attached(self, timeout: float) -> Optional[Any]:
        """Block until the brain dials back. ``None`` means give up.

        ``None`` covers both the deadline passing and the slot being
        released underneath us, because the caller's response to the two
        is identical: close the client connection. Collapsing them keeps
        the caller from having to catch a cancellation, which is a
        BaseException and too easy to catch too widely.

        Uses :func:`asyncio.wait` rather than :func:`asyncio.wait_for`
        for two reasons. It leaves the future alone on timeout, so the
        future's state is decided only by :meth:`attach` or a release
        and a brain dialling back on the deadline does not race against
        the timeout's cancellation. And ``wait_for`` returns the inner
        result when a cancellation lands in the same tick the future
        resolves, which discards the cancellation and leaves the caller
        running after something asked it to stop.
        """
        done, _ = await asyncio.wait({self._attached}, timeout=timeout)
        if not done:
            return None
        return self._attached.result()

    def _mark_claimed(self) -> None:
        self._claimed = True

    def _release(self) -> None:
        """End the slot. Idempotent, because both ends call it."""
        if self._released:
            return
        self._released = True
        if not self._attached.done():
            # Resolve rather than cancel: a waiter gets None back and
            # takes the give-up path instead of having a CancelledError
            # injected into it.
            self._attached.set_result(None)
        self.closed.set()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "released" if self._released else ("claimed" if self._claimed else "pending")
        return f"<StreamSlot {self.stream_id} {self.relay_id} {state}>"


class TunnelRegistry:
    """The live map of relay id to control channel, plus stream slots."""

    def __init__(
        self,
        *,
        stream_limit: int = DEFAULT_STREAM_LIMIT,
        stream_ttl: float = DEFAULT_STREAM_TTL_SECONDS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if stream_limit < 1:
            raise ValueError("stream_limit must be at least 1")
        self.stream_limit = stream_limit
        self.stream_ttl = stream_ttl
        # Monotonic by default: reservation deadlines must not move when
        # the host's wall clock is stepped by NTP.
        self._now = time_source
        self._controls: Dict[str, ControlConnection] = {}
        self._slots: Dict[str, StreamSlot] = {}
        self._by_relay: Dict[str, Dict[str, StreamSlot]] = {}

    # ── control channels ───────────────────────────────────────────

    def register_control(self, connection: ControlConnection) -> Optional[ControlConnection]:
        """Make this connection the one that receives ``open`` frames.

        Returns the connection it displaced, if any, so the caller can
        close it. Displacing rather than refusing is deliberate: a brain
        whose network dropped has no way to tell us its old socket is
        dead, and refusing the new one would leave it unroutable until
        the stale socket's keepalive finally noticed, which can be
        minutes.

        The displaced connection's own streams are left alone here. They
        are torn down by :meth:`unregister_control` when its handler
        finishes, which is the point at which we actually know it is
        gone.
        """
        previous = self._controls.get(connection.relay_id)
        self._controls[connection.relay_id] = connection
        return previous if previous is not connection else None

    def unregister_control(self, connection: ControlConnection) -> List[StreamSlot]:
        """Remove this connection and release the slots it owned.

        Identity-checked. A stale connection finishing its teardown after
        its replacement has registered must not evict the replacement,
        and must not release the replacement's streams. It does still
        release its own, because a stream whose control channel is gone
        has nobody left to notice it.
        """
        current = self._controls.get(connection.relay_id)
        if current is connection:
            del self._controls[connection.relay_id]

        released: List[StreamSlot] = []
        owned = self._by_relay.get(connection.relay_id)
        if owned:
            for slot in list(owned.values()):
                if slot.connection is connection:
                    self._drop(slot)
                    slot._release()
                    released.append(slot)
        return released

    def lookup_control(self, relay_id: str) -> Optional[ControlConnection]:
        return self._controls.get(relay_id)

    def connected_relay_ids(self) -> List[str]:
        return list(self._controls)

    # ── stream slots ───────────────────────────────────────────────

    def reserve_stream(
        self,
        relay_id: str,
        *,
        ttl: Optional[float] = None,
    ) -> StreamSlot:
        """Allocate a stream id for a connection we are about to route.

        Look-up, cap check and insertion happen with no await point
        between them, so concurrent connections to the same brain cannot
        both pass a cap check that only one of them should have passed.
        """
        connection = self._controls.get(relay_id)
        if connection is None:
            raise RelayNotConnected(relay_id)

        # Sweeping here rather than on a timer means an abandoned
        # reservation is reclaimed exactly when its slot is contended,
        # and never needs a background task that could itself die. The
        # per-relay table is fetched after the sweep, never before: a
        # sweep that empties it removes it, and a reference taken
        # earlier would be an orphan that the registry no longer reads.
        self._sweep_relay(relay_id)
        owned = self._by_relay.setdefault(relay_id, {})

        if len(owned) >= self.stream_limit:
            raise StreamLimitReached(relay_id)

        stream_id = uuid.uuid4().hex
        slot = StreamSlot(
            stream_id=stream_id,
            relay_id=relay_id,
            connection=connection,
            expires_at=self._now() + (self.stream_ttl if ttl is None else ttl),
        )
        self._slots[stream_id] = slot
        owned[stream_id] = slot
        return slot

    def claim_stream(self, stream_id: str) -> StreamSlot:
        """Take a reservation for a brain that has just dialled back.

        Single use. A stream id that was never issued, has already been
        claimed, has been released, or is past its deadline all raise
        :class:`UnknownStream`, because a replayed or guessed id must not
        be able to attach itself to somebody else's connection.
        """
        slot = self._slots.get(stream_id)
        if slot is None or slot.released or slot.claimed:
            raise UnknownStream(stream_id)
        if self._now() >= slot.expires_at:
            # Expired reservations are dropped on sight so a late dial
            # cannot resurrect one whose TCP side has already gone.
            self._drop(slot)
            slot._release()
            raise UnknownStream(stream_id)
        slot._mark_claimed()
        return slot

    def release_stream(self, stream_id: str) -> bool:
        """Free a slot and wake anything waiting on it. Idempotent.

        Both ends of a splice call this, and a splice can end from
        either end, so being called twice is the normal case rather than
        a bug.
        """
        slot = self._slots.get(stream_id)
        if slot is None:
            return False
        self._drop(slot)
        slot._release()
        return True

    def active_streams(self, relay_id: str) -> int:
        """Slots counting against the cap: reserved and spliced alike.

        A claimed stream still occupies the brain's budget, so counting
        only pending reservations would let the cap be exceeded by
        exactly the number of live streams, which is the number that
        matters.
        """
        return len(self._by_relay.get(relay_id, ()))

    def get_slot(self, stream_id: str) -> Optional[StreamSlot]:
        return self._slots.get(stream_id)

    def sweep_expired(self) -> List[StreamSlot]:
        """Reclaim reservations nobody came back for. Safety net only."""
        reclaimed: List[StreamSlot] = []
        for relay_id in list(self._by_relay):
            reclaimed.extend(self._sweep_relay(relay_id))
        return reclaimed

    # ── internals ──────────────────────────────────────────────────

    def _sweep_relay(self, relay_id: str) -> List[StreamSlot]:
        """Drop one relay's unclaimed slots that are past their deadline.

        Claimed slots are never swept. They are live traffic, and a long
        lived connection is not a leak.
        """
        owned = self._by_relay.get(relay_id)
        if not owned:
            return []
        now = self._now()
        expired = [
            slot
            for slot in owned.values()
            if not slot.claimed and now >= slot.expires_at
        ]
        for slot in expired:
            self._drop(slot)
            slot._release()
        return expired

    def _drop(self, slot: StreamSlot) -> None:
        self._slots.pop(slot.stream_id, None)
        owned = self._by_relay.get(slot.relay_id)
        if owned is not None:
            owned.pop(slot.stream_id, None)
            if not owned:
                del self._by_relay[slot.relay_id]
