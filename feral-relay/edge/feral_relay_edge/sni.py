"""Read the SNI out of a TLS ClientHello without terminating the TLS.

This is what lets the edge route a connection to the right brain while
remaining unable to read it. The brain holds the certificate and the
private key; the edge sees the one field it needs, in the clear, by
construction: the server name is sent before encryption begins.

Every byte this module reads is attacker-controlled and arrives before
anything has been authenticated, so the rules here are strict:

* **Never read past the buffer.** Every read goes through a reader that
  raises rather than slicing short, because a short slice in Python is
  silent and would turn a truncated packet into a confident wrong
  answer.
* **Distinguish "not yet" from "no".** A ClientHello can be split across
  TCP segments, and legally across several TLS records. Returning
  ``no_sni`` for data that simply has not arrived would drop valid
  connections under load, which is the kind of bug that only shows up in
  production.
* **Bound everything.** A length field is a promise from a stranger.
  Without a ceiling, a handshake header claiming 16MB would have the
  edge buffer 16MB per connection.
* **Never raise at the boundary.** :func:`peek_sni` returns an outcome
  for every input including garbage, so a caller cannot forget a
  ``try``.

Encrypted Client Hello would remove the field entirely and break SNI
routing for everyone doing it this way. It is not yet on by default in
Apple clients, but it is a question of when, so nothing here should be
read as an assumption that SNI is permanent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

#: TLS record content type for handshake records.
_CONTENT_TYPE_HANDSHAKE = 0x16
#: Handshake message type for ClientHello.
_HANDSHAKE_CLIENT_HELLO = 0x01
#: Extension type for server_name (RFC 6066).
_EXT_SERVER_NAME = 0x0000
#: server_name_list entry type for a DNS hostname.
_NAME_TYPE_HOST = 0x00

#: A TLS record's payload may not exceed 2^14 plus expansion. Anything
#: claiming more is malformed, not merely large.
_MAX_RECORD_PAYLOAD = 1 << 14

#: Ceiling on reassembled handshake bytes. A real ClientHello is a few
#: hundred bytes; post-quantum key shares push it to a few kilobytes.
#: 64KB is generous and still bounds per-connection memory.
_MAX_HANDSHAKE_BYTES = 1 << 16

Status = Literal["found", "need_more", "not_tls", "no_sni", "malformed"]


@dataclass(frozen=True)
class SniOutcome:
    """What we learned, and whether it is safe to act on it."""

    status: Status
    host: Optional[str] = None

    @property
    def is_final(self) -> bool:
        """False only for ``need_more``, the one status worth waiting on."""
        return self.status != "need_more"


class _NeedMore(Exception):
    """The buffer ended mid-structure. Not an error; read more bytes."""


class _Malformed(Exception):
    """The bytes are self-inconsistent. More data will not help."""


class _Reader:
    """Bounds-checked cursor. Every read can fail, and says how."""

    __slots__ = ("_buf", "_pos")

    def __init__(self, buf: bytes) -> None:
        self._buf = buf
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._buf) - self._pos

    def take(self, n: int) -> bytes:
        if n < 0:
            raise _Malformed("negative length")
        end = self._pos + n
        if end > len(self._buf):
            raise _NeedMore()
        out = self._buf[self._pos:end]
        self._pos = end
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        raw = self.take(2)
        return (raw[0] << 8) | raw[1]

    def u24(self) -> int:
        raw = self.take(3)
        return (raw[0] << 16) | (raw[1] << 8) | raw[2]

    def skip(self, n: int) -> None:
        self.take(n)


def _reassemble_handshake(buf: bytes) -> bytes:
    """Concatenate handshake payloads across TLS records.

    A ClientHello is usually one record, and is allowed not to be.
    Handling only the common case would work on every client anyone
    tests with and fail on some real one later.
    """
    reader = _Reader(buf)
    handshake = bytearray()

    while True:
        content_type = reader.u8()
        if content_type != _CONTENT_TYPE_HANDSHAKE:
            raise _Malformed("not a handshake record")

        legacy_version = reader.u16()
        # 0x03xx covers SSL 3.0 through TLS 1.2, and TLS 1.3 still puts
        # a legacy 0x0303 here. Anything else is not TLS.
        if (legacy_version >> 8) != 0x03:
            raise _Malformed("not a TLS record version")

        length = reader.u16()
        if length == 0 or length > _MAX_RECORD_PAYLOAD:
            raise _Malformed("implausible record length")

        handshake.extend(reader.take(length))
        if len(handshake) > _MAX_HANDSHAKE_BYTES:
            raise _Malformed("handshake too large")

        # Do we have the whole handshake message yet?
        if len(handshake) >= 4:
            declared = (handshake[1] << 16) | (handshake[2] << 8) | handshake[3]
            if len(handshake) - 4 >= declared:
                return bytes(handshake)

        if reader.remaining == 0:
            raise _NeedMore()


def _extract_sni(handshake: bytes) -> Optional[str]:
    """Pull server_name out of a complete ClientHello body."""
    reader = _Reader(handshake)

    if reader.u8() != _HANDSHAKE_CLIENT_HELLO:
        raise _Malformed("not a ClientHello")
    reader.u24()  # declared body length, already satisfied by reassembly

    reader.skip(2)   # legacy_version
    reader.skip(32)  # random
    reader.skip(reader.u8())    # session_id
    reader.skip(reader.u16())   # cipher_suites
    reader.skip(reader.u8())    # compression_methods

    # Extensions are optional in the grammar. A ClientHello without them
    # cannot carry SNI, and that is a definite "no", not "not yet".
    if reader.remaining == 0:
        return None

    ext_total = reader.u16()
    ext_bytes = reader.take(ext_total)
    ext_reader = _Reader(ext_bytes)

    while ext_reader.remaining > 0:
        ext_type = ext_reader.u16()
        ext_len = ext_reader.u16()
        ext_data = ext_reader.take(ext_len)
        if ext_type != _EXT_SERVER_NAME:
            continue

        entries = _Reader(ext_data)
        entries.u16()  # server_name_list length
        while entries.remaining > 0:
            name_type = entries.u8()
            name = entries.take(entries.u16())
            if name_type == _NAME_TYPE_HOST:
                return _decode_host(name)
        return None

    return None


def _decode_host(raw: bytes) -> Optional[str]:
    """Decode and sanity-check a hostname from the wire.

    Rejects rather than normalises. This value selects which brain's
    tunnel a connection is spliced into, so anything surprising is
    refused rather than coerced into something that might match the
    wrong entry.
    """
    if not raw or len(raw) > 253:
        return None
    try:
        host = raw.decode("ascii").strip().rstrip(".").lower()
    except UnicodeDecodeError:
        return None
    if not host:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-.")
    if not set(host) <= allowed:
        return None
    if ".." in host or host.startswith(".") or host.startswith("-"):
        return None
    if any(len(label) == 0 or len(label) > 63 for label in host.split(".")):
        return None
    return host


def peek_sni(buf: bytes) -> SniOutcome:
    """Read the SNI from buffered bytes. Never raises.

    ``need_more`` means keep reading from the socket. Every other status
    is final, and the caller should stop buffering.
    """
    if not buf:
        return SniOutcome("need_more")

    # Cheapest possible rejection, and the common non-TLS case: anything
    # speaking HTTP, SSH or similar fails on the first byte.
    if buf[0] != _CONTENT_TYPE_HANDSHAKE:
        return SniOutcome("not_tls")

    try:
        handshake = _reassemble_handshake(buf)
    except _NeedMore:
        if len(buf) > _MAX_HANDSHAKE_BYTES:
            return SniOutcome("malformed")
        return SniOutcome("need_more")
    except _Malformed:
        return SniOutcome("malformed")

    try:
        host = _extract_sni(handshake)
    except _NeedMore:
        # The handshake message is complete by construction here, so a
        # short read means the message lied about its own structure.
        return SniOutcome("malformed")
    except _Malformed:
        return SniOutcome("malformed")

    if host is None:
        return SniOutcome("no_sni")
    return SniOutcome("found", host)
