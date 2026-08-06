"""The registry decides who a connection is spliced into, so it is
tested for the things that would silently route or leak.

The failures worth writing down here are not "does a dict store a
value". They are the reconnect that evicts its own replacement, the cap
that two coroutines both slip past in the same tick, and the reservation
that nobody ever comes back for and that consumes part of a user's
budget until the process restarts.
"""

from __future__ import annotations

import asyncio

import pytest

from feral_relay_edge.registry import (
    ControlConnection,
    RelayNotConnected,
    StreamLimitReached,
    TunnelRegistry,
    UnknownStream,
)

pytestmark = pytest.mark.asyncio

RELAY = "olgw5bbcyqd7w3ijq2ipceylpxwx5qxx"
OTHER = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class FakeClock:
    """Deadlines are tested by moving time, never by sleeping."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_registry(**kwargs) -> tuple[TunnelRegistry, FakeClock]:
    clock = FakeClock()
    return TunnelRegistry(time_source=clock, **kwargs), clock


def connect(registry: TunnelRegistry, relay_id: str = RELAY) -> ControlConnection:
    connection = ControlConnection(relay_id=relay_id, ws=object())
    registry.register_control(connection)
    return connection


# ── control channels ───────────────────────────────────────────────


class TestControlChannels:
    async def test_a_registered_connection_is_found_by_its_relay_id(self):
        registry, _ = make_registry()
        connection = connect(registry)
        assert registry.lookup_control(RELAY) is connection

    async def test_an_unconnected_relay_id_has_no_connection(self):
        registry, _ = make_registry()
        assert registry.lookup_control(RELAY) is None

    async def test_a_reconnect_displaces_the_previous_connection(self):
        """A brain whose network dropped cannot tell us its old socket
        is dead. Refusing the new one would leave it unroutable until
        the stale socket's keepalive noticed, which can be minutes."""
        registry, _ = make_registry()
        first = connect(registry)
        second = ControlConnection(relay_id=RELAY, ws=object())

        displaced = registry.register_control(second)

        assert displaced is first
        assert registry.lookup_control(RELAY) is second

    async def test_a_stale_connection_unregistering_does_not_evict_its_replacement(self):
        """The bug this prevents: brain reconnects, then the old
        socket's handler finishes its teardown a moment later and
        removes the entry the new socket just installed. The brain is
        connected and unroutable, and nothing logs an error."""
        registry, _ = make_registry()
        stale = connect(registry)
        fresh = ControlConnection(relay_id=RELAY, ws=object())
        registry.register_control(fresh)

        registry.unregister_control(stale)

        assert registry.lookup_control(RELAY) is fresh

    async def test_a_stale_connection_releases_only_its_own_streams(self):
        registry, _ = make_registry()
        stale = connect(registry)
        stale_slot = registry.reserve_stream(RELAY)

        fresh = ControlConnection(relay_id=RELAY, ws=object())
        registry.register_control(fresh)
        fresh_slot = registry.reserve_stream(RELAY)

        released = registry.unregister_control(stale)

        assert [slot.stream_id for slot in released] == [stale_slot.stream_id]
        assert stale_slot.closed.is_set()
        assert not fresh_slot.closed.is_set()
        assert registry.get_slot(fresh_slot.stream_id) is fresh_slot

    async def test_dropping_a_control_channel_releases_its_streams(self):
        """This is what closes the TCP side when a brain vanishes
        mid-splice. Without it the client hangs on a tunnel whose far
        end no longer exists."""
        registry, _ = make_registry()
        connection = connect(registry)
        slot = registry.reserve_stream(RELAY)
        slot_two = registry.reserve_stream(RELAY)

        released = registry.unregister_control(connection)

        assert {s.stream_id for s in released} == {slot.stream_id, slot_two.stream_id}
        assert slot.closed.is_set() and slot_two.closed.is_set()
        assert registry.active_streams(RELAY) == 0

    async def test_dropping_a_control_channel_wakes_anything_waiting_on_it(self):
        registry, _ = make_registry()
        connection = connect(registry)
        slot = registry.reserve_stream(RELAY)
        waiter = asyncio.create_task(slot.wait_attached(5))
        await asyncio.sleep(0)

        registry.unregister_control(connection)

        assert await asyncio.wait_for(waiter, 1) is None


# ── reservations ───────────────────────────────────────────────────


class TestReservation:
    async def test_reserving_for_a_relay_with_no_brain_is_refused(self):
        registry, _ = make_registry()
        with pytest.raises(RelayNotConnected):
            registry.reserve_stream(RELAY)

    async def test_stream_ids_are_unique(self):
        registry, _ = make_registry(stream_limit=64)
        connect(registry)
        ids = {registry.reserve_stream(RELAY).stream_id for _ in range(64)}
        assert len(ids) == 64

    async def test_the_cap_refuses_the_thirty_third_stream(self):
        """A reservation costs the brain a real socket. Uncapped, one
        client in a loop would have a laptop dial back thousands of
        times and the denial of service lands on the user."""
        registry, _ = make_registry(stream_limit=32)
        connect(registry)
        for _ in range(32):
            registry.reserve_stream(RELAY)

        with pytest.raises(StreamLimitReached):
            registry.reserve_stream(RELAY)

    async def test_the_cap_is_per_relay_id(self):
        registry, _ = make_registry(stream_limit=1)
        connect(registry, RELAY)
        connect(registry, OTHER)
        registry.reserve_stream(RELAY)

        registry.reserve_stream(OTHER)  # must not be affected by RELAY

        with pytest.raises(StreamLimitReached):
            registry.reserve_stream(RELAY)

    async def test_the_cap_counts_spliced_streams_not_only_pending_ones(self):
        """Counting only unclaimed reservations would let the cap be
        exceeded by exactly the number of live streams."""
        registry, _ = make_registry(stream_limit=1)
        connect(registry)
        slot = registry.reserve_stream(RELAY)
        registry.claim_stream(slot.stream_id)

        assert registry.active_streams(RELAY) == 1
        with pytest.raises(StreamLimitReached):
            registry.reserve_stream(RELAY)

    async def test_releasing_frees_a_slot_under_the_cap(self):
        registry, _ = make_registry(stream_limit=1)
        connect(registry)
        slot = registry.reserve_stream(RELAY)

        assert registry.release_stream(slot.stream_id) is True

        registry.reserve_stream(RELAY)  # would raise if the slot leaked

    async def test_concurrent_reservations_cannot_exceed_the_cap(self):
        """Two connections landing in the same tick must not both pass
        a cap check only one of them should have passed."""
        registry, _ = make_registry(stream_limit=5)
        connect(registry)

        async def attempt():
            await asyncio.sleep(0)
            try:
                registry.reserve_stream(RELAY)
                return True
            except StreamLimitReached:
                return False

        results = await asyncio.gather(*(attempt() for _ in range(40)))

        assert sum(results) == 5
        assert registry.active_streams(RELAY) == 5

    async def test_an_abandoned_reservation_is_reclaimed_when_the_cap_bites(self):
        """A broker task that dies between reserving and splicing must
        not consume part of a brain's budget until the process
        restarts."""
        registry, clock = make_registry(stream_limit=1, stream_ttl=10)
        connect(registry)
        abandoned = registry.reserve_stream(RELAY)

        clock.advance(11)
        fresh = registry.reserve_stream(RELAY)

        assert fresh.stream_id != abandoned.stream_id
        assert abandoned.closed.is_set()
        assert registry.active_streams(RELAY) == 1

    async def test_sweeping_never_reclaims_a_live_stream(self):
        """A long-lived spliced connection is not a leak."""
        registry, clock = make_registry(stream_ttl=10)
        connect(registry)
        slot = registry.reserve_stream(RELAY)
        registry.claim_stream(slot.stream_id)

        clock.advance(3600)

        assert registry.sweep_expired() == []
        assert registry.get_slot(slot.stream_id) is slot
        assert not slot.closed.is_set()


# ── claiming ───────────────────────────────────────────────────────


class TestClaim:
    async def test_a_stream_id_that_was_never_issued_is_refused(self):
        registry, _ = make_registry()
        connect(registry)
        with pytest.raises(UnknownStream):
            registry.claim_stream("deadbeef" * 4)

    async def test_a_stream_id_cannot_be_claimed_twice(self):
        """Otherwise a second dial attaches itself to a splice that is
        already carrying somebody's traffic."""
        registry, _ = make_registry()
        connect(registry)
        slot = registry.reserve_stream(RELAY)
        registry.claim_stream(slot.stream_id)

        with pytest.raises(UnknownStream):
            registry.claim_stream(slot.stream_id)

    async def test_a_released_stream_cannot_be_claimed(self):
        registry, _ = make_registry()
        connect(registry)
        slot = registry.reserve_stream(RELAY)
        registry.release_stream(slot.stream_id)

        with pytest.raises(UnknownStream):
            registry.claim_stream(slot.stream_id)

    async def test_an_expired_reservation_cannot_be_claimed(self):
        """A late dial must not resurrect a stream whose client has
        already been closed."""
        registry, clock = make_registry(stream_ttl=10)
        connect(registry)
        slot = registry.reserve_stream(RELAY)

        clock.advance(10.1)

        with pytest.raises(UnknownStream):
            registry.claim_stream(slot.stream_id)
        assert registry.active_streams(RELAY) == 0

    async def test_a_reservation_claimed_on_the_deadline_survives(self):
        registry, clock = make_registry(stream_ttl=10)
        connect(registry)
        slot = registry.reserve_stream(RELAY)

        clock.advance(9.9)

        assert registry.claim_stream(slot.stream_id) is slot


# ── slot synchronisation ───────────────────────────────────────────


class TestSlotSynchronisation:
    async def test_a_waiter_receives_the_channel_the_brain_dialled(self):
        registry, _ = make_registry()
        connect(registry)
        slot = registry.reserve_stream(RELAY)
        channel = object()

        waiter = asyncio.create_task(slot.wait_attached(5))
        await asyncio.sleep(0)
        assert slot.attach(channel) is True

        assert await asyncio.wait_for(waiter, 1) is channel

    async def test_a_waiter_that_times_out_gives_up_rather_than_hanging(self):
        registry, _ = make_registry()
        connect(registry)
        slot = registry.reserve_stream(RELAY)

        assert await slot.wait_attached(0.01) is None

    async def test_a_second_dial_for_the_same_slot_is_refused(self):
        registry, _ = make_registry()
        connect(registry)
        slot = registry.reserve_stream(RELAY)

        assert slot.attach(object()) is True
        assert slot.attach(object()) is False

    async def test_attaching_to_a_released_slot_is_refused(self):
        """The caller has to know, because a channel accepted here and
        never read leaves the brain holding a dead socket."""
        registry, _ = make_registry()
        connect(registry)
        slot = registry.reserve_stream(RELAY)
        registry.release_stream(slot.stream_id)

        assert slot.attach(object()) is False

    async def test_releasing_twice_is_not_an_error(self):
        """Both ends of a splice release, and either end may die
        first."""
        registry, _ = make_registry()
        connect(registry)
        slot = registry.reserve_stream(RELAY)

        assert registry.release_stream(slot.stream_id) is True
        assert registry.release_stream(slot.stream_id) is False
        slot._release()  # the slot's own path, also idempotent
        assert slot.closed.is_set()

    async def test_a_timed_out_waiter_does_not_stop_a_later_attach_from_reporting(self):
        """The deadline race: the brain dials back at the exact moment
        the TCP side gives up. Whichever way it lands, exactly one side
        owns the channel and the other is told so."""
        registry, _ = make_registry()
        connect(registry)
        slot = registry.reserve_stream(RELAY)

        assert await slot.wait_attached(0.01) is None
        # The waiter timing out must not have cancelled the future, or
        # attach() below would raise instead of answering.
        assert slot.attach(object()) is True
        registry.release_stream(slot.stream_id)
        assert slot.attach(object()) is False
