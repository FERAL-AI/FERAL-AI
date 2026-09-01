"""Peer departure: ``PeerListener.remove_service`` used to be ``pass``.

Three things followed from that empty body, and each one is a test here:

  * a peer that left the network stayed in ``SyncEngine._peers``
    forever, so the scheduler kept dialling a brain that had gone;
  * its ``asyncio.Lock`` in ``_peer_locks`` leaked, one per brain that
    had ever been on the LAN;
  * nothing recorded when it was last seen, anywhere that survives a
    restart, which is the "no membership, no liveness" gap
    ``MemoryStore.prune_tombstones`` names in its own docstring.

The listener classes were closures inside ``start_discovery``, so the
only way to reach them was to bring up real zeroconf on a real network.
That is a large part of why an empty method could sit there. They are at
module level now and take the engine explicitly, so arrival and
departure are directly drivable.
"""

from __future__ import annotations

import pytest

from memory.sync import AsyncPeerListener, PeerListener, SyncEngine
from security.peer_roster import PeerRoster


class _FakeServiceInfo:
    """The three attributes ``PeerListener._record`` actually reads."""

    def __init__(self, node_id: str, addr: bytes, port: int, name: str):
        self.properties = {b"node_id": node_id.encode()}
        self.addresses = [addr]
        self.port = port
        self.name = name


def _info(node_id: str, *, last_octet: int = 5, port: int = 8888) -> _FakeServiceInfo:
    return _FakeServiceInfo(
        node_id,
        bytes([10, 0, 0, last_octet]),
        port,
        f"feral-{node_id}._feral._tcp.local.",
    )


@pytest.fixture
def engine(tmp_path, monkeypatch):
    roster = PeerRoster(db_path=str(tmp_path / "peer_roster.db"))
    eng = SyncEngine(node_id="brain-local", db_path=str(tmp_path / "wal.db"))
    monkeypatch.setattr(SyncEngine, "_roster", staticmethod(lambda: roster))
    eng.test_roster = roster
    return eng


class TestArrival:
    def test_record_registers_the_peer(self, engine):
        PeerListener(engine)._record(_info("brain-b"))

        assert engine._peers["brain-b"]["address"] == "10.0.0.5"
        assert engine._peers["brain-b"]["port"] == 8888

    def test_record_remembers_the_service_name(self, engine):
        """``remove_service`` is handed the mDNS NAME and nothing else.
        Without this map a departure cannot be attributed to a peer at
        all, which is why the empty body was not obviously wrong."""
        info = _info("brain-b")
        PeerListener(engine)._record(info)

        assert engine._service_names[info.name] == "brain-b"

    def test_our_own_advertisement_is_ignored(self, engine):
        PeerListener(engine)._record(_info("brain-local"))
        assert engine._peers == {}
        assert engine._service_names == {}

    def test_arrival_stamps_last_seen_for_an_enrolled_peer(self, engine):
        grant = engine.test_roster.invite_peer("laptop")
        engine.test_roster.verify_peer(grant["secret"], node_id="brain-b")

        PeerListener(engine)._record(_info("brain-b"))

        row = engine.test_roster.list_peers()[0]
        assert row["last_seen"] is not None
        assert row["last_address"] == "10.0.0.5"


class TestDeparture:
    def test_remove_service_drops_the_peer(self, engine):
        info = _info("brain-b")
        listener = PeerListener(engine)
        listener._record(info)

        listener.remove_service(None, "_feral._tcp.local.", info.name)

        assert "brain-b" not in engine._peers

    def test_remove_service_releases_the_per_peer_lock(self, engine):
        """``_peer_locks`` is keyed by peer id and nothing ever removed
        an entry, so it grew by one for every brain that had ever
        appeared on the network."""
        import asyncio

        info = _info("brain-b")
        listener = PeerListener(engine)
        listener._record(info)
        engine._peer_locks["brain-b"] = asyncio.Lock()

        listener.remove_service(None, "_feral._tcp.local.", info.name)

        assert "brain-b" not in engine._peer_locks

    def test_remove_service_forgets_the_service_name_mapping(self, engine):
        info = _info("brain-b")
        listener = PeerListener(engine)
        listener._record(info)

        listener.remove_service(None, "_feral._tcp.local.", info.name)

        assert info.name not in engine._service_names

    def test_departure_is_persisted_in_the_roster(self, engine):
        """The membership fact outlives the process. An in-memory dict
        deletion answers "is it here now"; the roster answers "who is
        still a peer", which is what tombstone pruning needs."""
        grant = engine.test_roster.invite_peer("laptop")
        engine.test_roster.verify_peer(grant["secret"], node_id="brain-b")
        info = _info("brain-b")
        listener = PeerListener(engine)
        listener._record(info)
        assert engine.test_roster.active_peer_ids() == ["brain-b"]

        listener.remove_service(None, "_feral._tcp.local.", info.name)

        assert engine.test_roster.active_peer_ids() == []
        assert engine.test_roster.list_peers()[0]["departed_at"] is not None

    def test_departure_is_not_revocation(self, engine):
        """A peer that walked out of Wi-Fi range has not lost its grant.
        Conflating the two would force a re-invite every time a laptop
        closed its lid."""
        grant = engine.test_roster.invite_peer("laptop")
        engine.test_roster.verify_peer(grant["secret"], node_id="brain-b")
        info = _info("brain-b")
        listener = PeerListener(engine)
        listener._record(info)
        listener.remove_service(None, "_feral._tcp.local.", info.name)

        assert engine.test_roster.verify_peer(
            grant["secret"], node_id="brain-b"
        ) is not None
        assert engine.test_roster.active_peer_ids() == ["brain-b"]

    def test_unknown_service_name_is_a_no_op(self, engine):
        PeerListener(engine).remove_service(
            None, "_feral._tcp.local.", "feral-nobody._feral._tcp.local.",
        )
        assert engine._peers == {}

    def test_departure_is_idempotent(self, engine):
        info = _info("brain-b")
        listener = PeerListener(engine)
        listener._record(info)

        listener.remove_service(None, "_feral._tcp.local.", info.name)
        listener.remove_service(None, "_feral._tcp.local.", info.name)

        assert "brain-b" not in engine._peers

    def test_forget_peer_reports_whether_it_was_present(self, engine):
        info = _info("brain-b")
        PeerListener(engine)._record(info)

        assert engine.forget_peer("brain-b") is True
        assert engine.forget_peer("brain-b") is False

    def test_async_listener_shares_the_departure_path(self, engine):
        """``AsyncPeerListener`` overrides only the resolve; departure
        must not silently diverge between the two transports."""
        info = _info("brain-b")
        listener = AsyncPeerListener(engine)
        listener._record(info)

        listener.remove_service(None, "_feral._tcp.local.", info.name)

        assert "brain-b" not in engine._peers
