"""
FERAL Hybrid Logical Clock — Distributed causal ordering
===========================================================
HLC provides ordering guarantees without a central clock server.
Each event gets a (wall_clock_ms, counter, node_id) tuple that
respects both physical time and causality.

Used by the SyncEngine for conflict-free replication.

Clock-drift safety
------------------
HLC's correctness bound is ``|l.e - pt.e| <= epsilon`` (Kulkarni,
Demirbas, Madeppa, Avva & Leone, *Logical Physical Clocks and
Consistent Snapshots in Globally Distributed Databases*, Corollary 1),
where ``epsilon`` is the clock-synchronisation uncertainty. That bound
is **assumed, not enforced** by the base algorithm: its proof rests on
"we cannot have two events e and f such that e hb f and pt.e > pt.f +
epsilon due to clock synchronization constraints".

A peer whose wall clock is wrong violates that premise directly, and
Theorem 2 (``l.f >= pt.f``) guarantees the bad value is never walked
back: once adopted, the logical clock stays pinned in the future until
real time catches up. Downstream that poisons every last-writer-wins
comparison, because the poisoned timestamp beats every honest one.

So we implement the remedy the paper itself prescribes (section 4):

1. A deliberately loose bound ``MAX_CLOCK_DRIFT_MS`` (the paper's
   ``Delta``, "even on the order of seconds depending on the
   application semantics") on how far the logical clock may run ahead
   of physical time.
2. Reject rather than absorb: "we simply ignore reception of messages
   that cause l value to diverge too much from pt".
3. Local self-stabilisation: "we take the physical clock as the
   authority, and reset l and c values to pt and 0 respectively".
4. A bounded counter so a corrupted ``c`` cannot grow without limit.
5. Log the offending entry and surface a counter, so an operator can
   alert on it rather than discovering it as silent data loss.

The bound is loose on purpose. It exists to turn "permanently poisoned"
into "wrong by at most Delta", not to police ordinary NTP jitter.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on junk."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d must be positive, using default %d", name, value, default)
        return default
    return value


# How far the logical clock may run ahead of physical time before we
# treat the source as untrustworthy. Five minutes masks suspend/resume,
# containers booting without NTP, and bad-but-plausible sync, while
# still rejecting the cases that actually cause silent data loss
# (a clock set to next year, 2038, or epoch garbage).
DEFAULT_MAX_CLOCK_DRIFT_MS = 5 * 60 * 1000

# Ceiling on the logical counter. Reaching this means either a
# pathological number of events inside one millisecond or memory
# corruption; both are better handled by resetting to physical time.
DEFAULT_MAX_COUNTER = 1_000_000

MAX_CLOCK_DRIFT_MS = _env_int("FERAL_SYNC_MAX_CLOCK_DRIFT_MS", DEFAULT_MAX_CLOCK_DRIFT_MS)
MAX_COUNTER = _env_int("FERAL_SYNC_MAX_HLC_COUNTER", DEFAULT_MAX_COUNTER)


@dataclass(frozen=True, order=True)
class HLCTimestamp:
    """Immutable HLC timestamp — comparable and serializable."""
    wall_ms: int
    counter: int
    node_id: str = ""

    def to_tuple(self) -> tuple[int, int, str]:
        return (self.wall_ms, self.counter, self.node_id)

    def to_string(self) -> str:
        return f"{self.wall_ms}:{self.counter}:{self.node_id}"

    @staticmethod
    def from_string(s: str) -> "HLCTimestamp":
        parts = s.split(":", 2)
        return HLCTimestamp(
            wall_ms=int(parts[0]),
            counter=int(parts[1]),
            node_id=parts[2] if len(parts) > 2 else "",
        )

    @staticmethod
    def zero() -> "HLCTimestamp":
        return HLCTimestamp(wall_ms=0, counter=0, node_id="")


class ClockDriftRejection(RuntimeError):
    """A remote timestamp was too far ahead of local physical time.

    Raised only by :meth:`HybridLogicalClock.receive_strict`. The
    ordinary :meth:`HybridLogicalClock.receive` path rejects silently
    (log + counter) so that one bad peer cannot halt sync for the rest.
    """

    def __init__(self, remote: HLCTimestamp, physical_ms: int, max_drift_ms: int):
        self.remote = remote
        self.physical_ms = physical_ms
        self.max_drift_ms = max_drift_ms
        super().__init__(
            f"remote HLC {remote.to_string()} is {remote.wall_ms - physical_ms}ms "
            f"ahead of local physical time (max {max_drift_ms}ms)"
        )


class HybridLogicalClock:
    """
    Per-node HLC instance.

    Guarantees:
    - Monotonically increasing timestamps
    - Respects causality: send(ts) < receive(ts)
    - Tracks physical time when possible
    - Never adopts a remote clock more than ``max_drift_ms`` ahead of
      local physical time, and self-stabilises if it ever gets there
    """

    def __init__(self, node_id: str, max_drift_ms: Optional[int] = None,
                 max_counter: Optional[int] = None):
        self.node_id = node_id
        self.max_drift_ms = MAX_CLOCK_DRIFT_MS if max_drift_ms is None else max_drift_ms
        self.max_counter = MAX_COUNTER if max_counter is None else max_counter
        self._wall_ms: int = 0
        self._counter: int = 0
        # Observability. The paper's guidance is to "log the offending
        # entries for inspection and raise an exception to notify the
        # administrator"; we log, count, and let callers alert.
        self.drift_rejections: int = 0
        self.stabilizations: int = 0
        self.last_rejection: Optional[HLCTimestamp] = None

    # ---- internals -------------------------------------------------

    @staticmethod
    def _physical_ms() -> int:
        return int(time.time() * 1000)

    def is_within_drift(self, remote: HLCTimestamp, physical_ms: Optional[int] = None) -> bool:
        """True when ``remote`` is close enough to local physical time to trust.

        Callers that must decide whether to *accept an operation* (not
        merely whether to advance the clock) should gate on this before
        the value reaches a last-writer-wins comparison. Advancing the
        clock and applying the op are separate decisions, and only
        gating the former still lets a poisoned timestamp win LWW.
        """
        pt = self._physical_ms() if physical_ms is None else physical_ms
        return remote.wall_ms <= pt + self.max_drift_ms

    def _stabilize(self, physical: int) -> bool:
        """Reset to physical time if the logical clock ran away.

        Covers state restored from disk, memory corruption, and any
        pre-fix timestamp already absorbed before this guard existed.
        """
        if self._wall_ms > physical + self.max_drift_ms:
            logger.error(
                "hlc.stabilize node=%s logical=%d physical=%d drift=%dms "
                "exceeds max %dms, resetting to physical time",
                self.node_id, self._wall_ms, physical,
                self._wall_ms - physical, self.max_drift_ms,
            )
            self._wall_ms = physical
            self._counter = 0
            self.stabilizations += 1
            return True

        if self._counter > self.max_counter:
            logger.error(
                "hlc.counter_overflow node=%s counter=%d exceeds max %d, "
                "resetting to physical time",
                self.node_id, self._counter, self.max_counter,
            )
            self._wall_ms = physical
            self._counter = 0
            self.stabilizations += 1
            return True

        return False

    def _tick_local(self, physical: int) -> None:
        """Advance as if this were a local event."""
        if physical > self._wall_ms:
            self._wall_ms = physical
            self._counter = 0
        else:
            self._counter += 1

    # ---- public API ------------------------------------------------

    def now(self) -> HLCTimestamp:
        """Generate a new timestamp for a local event."""
        physical = self._physical_ms()
        self._stabilize(physical)
        self._tick_local(physical)

        return HLCTimestamp(
            wall_ms=self._wall_ms,
            counter=self._counter,
            node_id=self.node_id,
        )

    def receive(self, remote: HLCTimestamp) -> HLCTimestamp:
        """
        Update the clock after receiving a message from another node.
        Ensures the new timestamp is greater than both local and remote.

        A remote timestamp more than ``max_drift_ms`` ahead of local
        physical time is **ignored** rather than adopted: it is logged,
        counted in ``drift_rejections``, and the clock advances as for
        a local event. This is the paper's "ignore out of bounds
        messages" prevention action, and it is what stops one peer with
        a wrong clock from pinning every other node in the future.
        """
        physical = self._physical_ms()
        self._stabilize(physical)

        if not self.is_within_drift(remote, physical):
            self.drift_rejections += 1
            self.last_rejection = remote
            logger.error(
                "hlc.drift_rejected node=%s remote_node=%s remote_wall=%d "
                "physical=%d drift=%dms exceeds max %dms, remote clock "
                "ignored (set FERAL_SYNC_MAX_CLOCK_DRIFT_MS to tune)",
                self.node_id, remote.node_id or "<unknown>", remote.wall_ms,
                physical, remote.wall_ms - physical, self.max_drift_ms,
            )
            self._tick_local(physical)
            return HLCTimestamp(
                wall_ms=self._wall_ms,
                counter=self._counter,
                node_id=self.node_id,
            )

        if physical > self._wall_ms and physical > remote.wall_ms:
            self._wall_ms = physical
            self._counter = 0
        elif remote.wall_ms > self._wall_ms:
            self._wall_ms = remote.wall_ms
            self._counter = remote.counter + 1
        elif self._wall_ms > remote.wall_ms:
            self._counter += 1
        else:
            # wall_ms are equal
            self._counter = max(self._counter, remote.counter) + 1

        return HLCTimestamp(
            wall_ms=self._wall_ms,
            counter=self._counter,
            node_id=self.node_id,
        )

    def receive_strict(self, remote: HLCTimestamp) -> HLCTimestamp:
        """Like :meth:`receive` but raises on an out-of-bounds remote clock.

        For callers that want to reject the whole message rather than
        just decline to advance the clock.
        """
        physical = self._physical_ms()
        self._stabilize(physical)
        if not self.is_within_drift(remote, physical):
            self.drift_rejections += 1
            self.last_rejection = remote
            raise ClockDriftRejection(remote, physical, self.max_drift_ms)
        return self.receive(remote)

    @property
    def current(self) -> HLCTimestamp:
        return HLCTimestamp(
            wall_ms=self._wall_ms,
            counter=self._counter,
            node_id=self.node_id,
        )

    @property
    def health(self) -> dict:
        """Drift-guard counters, for status output and alerting."""
        physical = self._physical_ms()
        return {
            "node_id": self.node_id,
            "logical_ms": self._wall_ms,
            "physical_ms": physical,
            "drift_ms": self._wall_ms - physical,
            "max_drift_ms": self.max_drift_ms,
            "drift_rejections": self.drift_rejections,
            "stabilizations": self.stabilizations,
            "last_rejection": (
                self.last_rejection.to_string() if self.last_rejection else None
            ),
        }
