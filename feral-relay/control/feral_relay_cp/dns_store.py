"""The TXT records that prove control of ``<relay_id>.relay.feral.sh``.

DNS-01 works by the CA asking a question only the zone operator can
answer: publish this exact value at ``_acme-challenge.<name>``. We are
the zone operator for ``relay.feral.sh``, so the control plane writes the
answer here and the authoritative server reads it back out.

That makes this module a small but real piece of the security boundary,
and two properties matter more than anything else it does:

1. **A record is addressed by relay_id, never by a caller-supplied
   name.** A store that accepted an arbitrary record name would let one
   brain's order publish a challenge answer under another brain's label,
   which is exactly the thing DNS-01 is supposed to make impossible. The
   only way in is :meth:`publish`, which builds the name itself.
2. **Answers expire.** An order that crashes between publishing and
   finalising must not leave a challenge permanently answerable. Every
   value carries an expiry and reads filter on it, so the failure mode of
   a lost cleanup is a stale record for minutes rather than forever.

Multiple values can be live for one name at once, and that is not an
edge case: a renewal and a fresh order overlapping is the normal way a
long-lived relay behaves. RFC 8555 lets a validation succeed if *any*
TXT record at the name matches, so the store is a set rather than a
single slot, and :meth:`retract` removes one value rather than clearing
the name.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol

#: The zone we are authoritative for. A relay's name is one label under
#: it, so a challenge record is two labels under it.
RELAY_BASE_DOMAIN = "relay.feral.sh"

#: The label ACME fixes for DNS-01. Not configurable, by the RFC.
ACME_CHALLENGE_LABEL = "_acme-challenge"

#: How long a published answer stays servable without being refreshed.
#: Long enough for a CA to retry a slow validation, short enough that a
#: crashed order stops being useful quickly.
DEFAULT_TTL_SECONDS = 900

#: A relay id is ``base32_lower(sha256(pubkey)[:20])``: exactly 32
#: characters from ``[a-z2-7]``. Anything else is not an id this control
#: plane issued a name for, and must never reach a zone file.
_RELAY_ID_RE = re.compile(r"^[a-z2-7]{32}$")


class DnsStoreError(ValueError):
    """A record that must not be written, and why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def relay_domain(relay_id: str, base_domain: str = RELAY_BASE_DOMAIN) -> str:
    """The name a relay is entitled to. Validated, never trusted raw."""
    if not _RELAY_ID_RE.match(relay_id or ""):
        raise DnsStoreError(
            "bad_relay_id",
            "a relay_id is 32 characters of [a-z2-7]",
        )
    return f"{relay_id}.{base_domain}"


def challenge_record_name(
    relay_id: str, base_domain: str = RELAY_BASE_DOMAIN
) -> str:
    """The full TXT record name a DNS-01 answer belongs at."""
    return f"{ACME_CHALLENGE_LABEL}.{relay_domain(relay_id, base_domain)}"


@dataclass(frozen=True)
class TxtRecord:
    """One live challenge answer.

    Deliberately holds no key material and no CSR. This record is read by
    the authoritative DNS server, which is the least trusted process in
    the issuance path, so it gets the least information.
    """

    relay_id: str
    name: str
    value: str
    expires_at: float


class DnsChallengeStore(Protocol):
    """What the ACME order needs from DNS, and nothing more.

    Narrow on purpose: the production implementation will be backed by
    whatever the authoritative server reads (a table, a zone file, an
    API), and the order flow must not have to care which.
    """

    def publish(
        self, relay_id: str, value: str, *, ttl_seconds: int = ..., now: Optional[float] = ...
    ) -> TxtRecord: ...

    def retract(self, relay_id: str, value: str) -> bool: ...

    def txt_values(
        self, record_name: str, *, now: Optional[float] = ...
    ) -> tuple[str, ...]: ...


class MemoryDnsChallengeStore:
    """In-process store. Correct for one control plane, and for tests.

    Thread-safe because a control plane serving two concurrent orders is
    the expected case, not a stress test.
    """

    def __init__(self, base_domain: str = RELAY_BASE_DOMAIN):
        self.base_domain = base_domain
        self._records: dict[str, dict[str, TxtRecord]] = {}
        self._lock = threading.Lock()

    def publish(
        self,
        relay_id: str,
        value: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: Optional[float] = None,
    ) -> TxtRecord:
        """Make ``value`` answerable at this relay's challenge name.

        The name is derived from ``relay_id`` here rather than accepted
        from the caller, so no order can publish under a name it does not
        own even if the ACME backend asks it to.
        """
        now = time.time() if now is None else now
        if not value or not isinstance(value, str):
            raise DnsStoreError("bad_value", "a TXT value must be a non-empty string")
        # A DNS-01 key authorization digest is 43 base64url characters.
        # Refusing anything long keeps a hostile backend from stuffing a
        # zone with arbitrary payloads.
        if len(value) > 255:
            raise DnsStoreError("bad_value", "a TXT value must be at most 255 characters")

        name = challenge_record_name(relay_id, self.base_domain)
        record = TxtRecord(
            relay_id=relay_id,
            name=name,
            value=value,
            expires_at=now + max(1, int(ttl_seconds)),
        )
        with self._lock:
            self._records.setdefault(name, {})[value] = record
        return record

    def retract(self, relay_id: str, value: str) -> bool:
        """Remove one answer. Returns whether it was there.

        Scoped to a single value so retiring a finished order cannot tear
        down a concurrent one at the same name.
        """
        name = challenge_record_name(relay_id, self.base_domain)
        with self._lock:
            bucket = self._records.get(name)
            if not bucket or value not in bucket:
                return False
            del bucket[value]
            if not bucket:
                del self._records[name]
            return True

    def retract_all(self, relay_id: str) -> int:
        """Drop every answer for a relay. For operator cleanup only."""
        name = challenge_record_name(relay_id, self.base_domain)
        with self._lock:
            bucket = self._records.pop(name, {})
            return len(bucket)

    def txt_values(
        self, record_name: str, *, now: Optional[float] = None
    ) -> tuple[str, ...]:
        """What the authoritative server should serve for a name.

        Expired values are filtered on read rather than swept on a timer,
        so a store that is never written again still stops answering.
        """
        now = time.time() if now is None else now
        with self._lock:
            bucket = self._records.get(record_name, {})
            live = [r.value for r in bucket.values() if r.expires_at > now]
        return tuple(sorted(live))

    def purge_expired(self, *, now: Optional[float] = None) -> int:
        """Drop expired records. Housekeeping, not a correctness step:
        :meth:`txt_values` already refuses to serve them."""
        now = time.time() if now is None else now
        removed = 0
        with self._lock:
            for name in list(self._records):
                bucket = self._records[name]
                for value in [v for v, r in bucket.items() if r.expires_at <= now]:
                    del bucket[value]
                    removed += 1
                if not bucket:
                    del self._records[name]
        return removed

    def live_records(self, *, now: Optional[float] = None) -> tuple[TxtRecord, ...]:
        """Every unexpired record. For the DNS server's zone dump."""
        now = time.time() if now is None else now
        with self._lock:
            return tuple(
                r
                for bucket in self._records.values()
                for r in bucket.values()
                if r.expires_at > now
            )
