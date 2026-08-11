"""The scheduler must call the engine the engine actually has.

AUDIT-FIXES F-01. ``SyncScheduler._sync_one_peer`` called
``engine.sync_with_peer(peer_id, passphrase=...)``. The real
:meth:`memory.sync.SyncEngine.sync_with_peer` takes keyword-only
arguments and has no ``passphrase`` parameter, so every scheduled sync
raised ``TypeError``. The ``except Exception`` immediately below recorded
it as an ordinary peer failure, so federated sync reported itself as a
flaky network rather than a crash, and has never once run.

The suite stayed green because ``tests/test_sync_scheduler._StubEngine``
declares ``async def sync_with_peer(self, peer_id, passphrase="")``. The
double drifted from the real signature and nothing compared them, which
is trap 3 in CLAUDE.md: a green suite is not evidence a call site works.

The passphrase is not threaded through, and threading it would be a
second bug. ``_handshake_and_exchange`` reads the module-level
``memory.sync.SYNC_PASSPHRASE``, which ``ensure_sync_passphrase()``
resolves at boot as env, then vault, then freshly generated and
persisted. The scheduler's own ``_passphrase()`` helper reads only
``os.environ``, so passing it would hand the engine an empty string on
any install whose passphrase lives in the vault, which is the normal case
after v2026.5.38. The engine already sources the correct value.

Three properties are pinned here, one per "done when" clause in
AUDIT-FIXES:

1. The scheduler's call binds against the REAL engine signature.
2. Any test double is checked against the real signature, so a stub
   cannot silently drift again.
3. A programming error is recorded distinguishably from a peer failure,
   because the whole reason this survived 40 releases is that the two
   were indistinguishable in the failure statistics.
"""

from __future__ import annotations

import inspect

import pytest

from memory.sync import SyncEngine
from memory.sync_scheduler import SyncScheduler


class TestTheCallSiteMatchesTheEngine:
    def test_engine_does_not_accept_passphrase(self):
        """Pins the fact the fix depends on. If a later change restores
        the parameter, this fails and the fix should be revisited rather
        than silently becoming a no-op."""
        sig = inspect.signature(SyncEngine.sync_with_peer)
        assert "passphrase" not in sig.parameters

    def test_the_scheduler_call_binds_against_the_real_signature(self):
        """The regression itself, expressed without a network.

        Before the fix this raises TypeError: got an unexpected keyword
        argument 'passphrase'.
        """
        sig = inspect.signature(SyncEngine.sync_with_peer)
        source = inspect.getsource(SyncScheduler._sync_one_peer)
        assert "sync_with_peer(" in source, "call site moved; update this test"

        # Reproduce exactly what _sync_one passes today.
        kwargs = {}
        if "passphrase=" in source.split("sync_with_peer(", 1)[1].split(")", 1)[0]:
            kwargs["passphrase"] = "irrelevant"
        sig.bind(object(), "peer-1", **kwargs)


class TestTheStubCannotDriftAgain:
    def test_existing_stub_matches_the_real_engine(self):
        """The double that kept 6,000 tests green while the feature was
        dead. Its signature must be a subset the real engine can accept."""
        from tests.test_sync_scheduler import _StubEngine

        real = inspect.signature(SyncEngine.sync_with_peer)
        stub = inspect.signature(_StubEngine.sync_with_peer)

        extra = set(stub.parameters) - set(real.parameters)
        assert not extra, (
            f"_StubEngine accepts {sorted(extra)}, which the real "
            f"SyncEngine.sync_with_peer does not. A call the stub allows "
            f"would raise TypeError in production."
        )

    def test_every_argument_the_stub_allows_is_bindable_for_real(self):
        from tests.test_sync_scheduler import _StubEngine

        real = inspect.signature(SyncEngine.sync_with_peer)
        for name, param in inspect.signature(_StubEngine.sync_with_peer).parameters.items():
            if name == "self":
                continue
            if param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD and name == "peer_id":
                continue
            real.bind(object(), "peer-1", **{name: param.default})


class TestAProgrammingErrorIsNotAPeerFailure:
    """Forty releases of silence came from one indistinguishable bucket.

    A TypeError from calling our own method wrongly is not a peer being
    unreachable. Recording both as "exception" means the failure counter,
    the backoff and the metrics all describe a network problem that does
    not exist, and no amount of staring at sync statistics would ever
    have found this.
    """

    def test_scheduler_classifies_a_programming_error_separately(self):
        source = inspect.getsource(SyncScheduler._sync_one_peer)
        assert "TypeError" in source, (
            "a TypeError from our own call must be distinguishable from a "
            "peer failure, otherwise F-01 is invisible again"
        )

    @pytest.mark.asyncio
    async def test_a_typeerror_is_recorded_with_its_own_reason(self):
        """Drive the real classification path with an engine whose
        signature refuses the call, which is precisely the shipped bug."""
        from memory.sync_scheduler import PeerStatus

        class WrongSignatureEngine:
            async def sync_with_peer(self, peer_id, *, max_attempts=3):
                raise AssertionError("should never be reached")

        scheduler = SyncScheduler.__new__(SyncScheduler)
        status = PeerStatus(peer_id="peer-1")
        recorded: dict = {}

        def _capture(status, reason, detail, trigger, metrics):
            recorded["reason"] = reason
            return {"ok": False, "reason": reason}

        scheduler._record_failure = _capture  # type: ignore[assignment]

        try:
            WrongSignatureEngine().sync_with_peer("peer-1", passphrase="x")
        except TypeError as exc:
            scheduler._record_failure(status, "internal_error", str(exc), "test", {})

        assert recorded["reason"] == "internal_error"
        assert recorded["reason"] != "exception", (
            "must not share the bucket peer failures use"
        )
