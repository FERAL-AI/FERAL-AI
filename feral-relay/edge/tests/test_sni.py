"""The SNI peek reads attacker-controlled bytes before anything is
authenticated, so it is tested adversarially and against real TLS output
rather than only against bytes this repo built itself.

A parser tested solely on its own fixtures agrees with itself. The
capture below comes from Python's ssl module driving a real handshake,
so the shape is one an actual client produces.
"""

from __future__ import annotations

import socket
import ssl
import threading

import pytest

from feral_relay_edge.sni import peek_sni


# ── Real ClientHello capture ───────────────────────────────────────


def _capture_client_hello(server_hostname: str | None) -> bytes:
    """Return the literal first flight a real TLS client writes."""
    ours, theirs = socket.socketpair()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _drive():
        try:
            ctx.wrap_socket(
                ours,
                server_hostname=server_hostname,
                do_handshake_on_connect=True,
            )
        except Exception:
            pass  # no server on the far end; we only want the first write

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    theirs.settimeout(5)
    try:
        data = theirs.recv(65536)
    finally:
        theirs.close()
        thread.join(timeout=5)
    return data


@pytest.fixture(scope="module")
def real_hello() -> bytes:
    data = _capture_client_hello("abc123.relay.feral.sh")
    if not data:
        pytest.skip("could not capture a ClientHello in this environment")
    return data


class TestAgainstRealTls:
    def test_finds_the_server_name(self, real_hello):
        outcome = peek_sni(real_hello)
        assert outcome.status == "found"
        assert outcome.host == "abc123.relay.feral.sh"

    def test_a_relay_id_hostname_survives_intact(self):
        """The names this edge actually routes are 32 base32 chars."""
        host = "olgw5bbcyqd7w3ijq2ipceylpxwx5qxx.relay.feral.sh"
        data = _capture_client_hello(host)
        if not data:
            pytest.skip("capture unavailable")
        assert peek_sni(data).host == host

    def test_every_prefix_is_safe(self, real_hello):
        """The important one.

        For each truncation of a real ClientHello the parser must not
        raise, and must never claim to have found a host it has not
        fully read. Anything else means a fragmented packet could route
        a connection to the wrong brain.
        """
        for cut in range(len(real_hello)):
            outcome = peek_sni(real_hello[:cut])
            assert outcome.status in {"need_more", "malformed", "not_tls"}, cut
            assert outcome.host is None, cut

        assert peek_sni(real_hello).status == "found"

    def test_trailing_bytes_do_not_confuse_it(self, real_hello):
        """The socket buffer may already hold the next flight."""
        outcome = peek_sni(real_hello + b"\x17\x03\x03\x00\x10" + b"\x00" * 16)
        assert outcome.status == "found"


class TestNonTls:
    @pytest.mark.parametrize(
        "payload",
        [
            b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",
            b"SSH-2.0-OpenSSH_9.0\r\n",
            b"\x00\x00\x00\x00",
            b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n",
        ],
    )
    def test_obvious_non_tls_is_rejected_immediately(self, payload):
        assert peek_sni(payload).status == "not_tls"

    def test_empty_input_is_need_more_not_a_verdict(self):
        assert peek_sni(b"").status == "need_more"


class TestMalformed:
    def test_a_handshake_record_with_a_bogus_version(self):
        assert peek_sni(b"\x16\xff\xff\x00\x05hello").status == "malformed"

    def test_a_record_claiming_more_than_a_record_may_hold(self):
        # 0xFFFF exceeds the 2^14 payload ceiling.
        assert peek_sni(b"\x16\x03\x01\xff\xff").status == "malformed"

    def test_a_zero_length_record(self):
        assert peek_sni(b"\x16\x03\x01\x00\x00").status == "malformed"

    def test_a_handshake_that_is_not_a_client_hello(self):
        # Type 0x02 is ServerHello, which has no business arriving here.
        body = b"\x02\x00\x00\x01\x00"
        record = b"\x16\x03\x01" + len(body).to_bytes(2, "big") + body
        assert peek_sni(record).status == "malformed"

    def test_absurdly_long_input_stops_buffering(self):
        """Without a ceiling the edge would buffer per connection for as
        long as an attacker kept sending."""
        payload = b"\x16\x03\x01\x3f\xff" + b"\x00" * 200
        outcome = peek_sni(payload + b"\x00" * (1 << 17))
        assert outcome.status == "malformed"


class TestFragmentation:
    def test_byte_at_a_time_never_answers_early(self, real_hello):
        """Feeding one byte at a time must produce need_more until the
        ClientHello is complete, then found exactly once."""
        found_at = None
        for size in range(1, len(real_hello) + 1):
            outcome = peek_sni(real_hello[:size])
            if outcome.status == "found":
                found_at = size
                break
            assert outcome.status == "need_more", (size, outcome)
        assert found_at == len(real_hello)

    def test_a_hello_split_across_two_records_is_reassembled(self, real_hello):
        """Legal, rare, and it would work on every client anyone tests
        with if we only handled the single-record case."""
        header, body = real_hello[:5], real_hello[5:]
        assert header[0] == 0x16
        half = len(body) // 2
        split = (
            b"\x16" + header[1:3] + len(body[:half]).to_bytes(2, "big") + body[:half]
            + b"\x16" + header[1:3] + len(body[half:]).to_bytes(2, "big") + body[half:]
        )
        outcome = peek_sni(split)
        assert outcome.status == "found"
        assert outcome.host == "abc123.relay.feral.sh"


class TestHostnameSanity:
    """The host selects which tunnel a connection is spliced into, so
    anything surprising is refused rather than coerced."""

    @staticmethod
    def _hello_with_host(raw_host: bytes) -> bytes:
        name = b"\x00" + len(raw_host).to_bytes(2, "big") + raw_host
        server_name_list = len(name).to_bytes(2, "big") + name
        ext = b"\x00\x00" + len(server_name_list).to_bytes(2, "big") + server_name_list
        exts = len(ext).to_bytes(2, "big") + ext
        body = (
            b"\x03\x03" + b"\x00" * 32 + b"\x00"
            + b"\x00\x02\x00\x2f" + b"\x01\x00" + exts
        )
        hs = b"\x01" + len(body).to_bytes(3, "big") + body
        return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs

    def test_a_well_formed_host_is_accepted(self):
        out = peek_sni(self._hello_with_host(b"brain.relay.feral.sh"))
        assert out.status == "found"
        assert out.host == "brain.relay.feral.sh"

    def test_case_is_normalised(self):
        out = peek_sni(self._hello_with_host(b"BRAIN.Relay.Feral.SH"))
        assert out.host == "brain.relay.feral.sh"

    def test_a_trailing_dot_is_stripped(self):
        out = peek_sni(self._hello_with_host(b"brain.relay.feral.sh."))
        assert out.host == "brain.relay.feral.sh"

    @pytest.mark.parametrize(
        "raw",
        [
            b"brain..relay.feral.sh",          # empty label
            b"-brain.relay.feral.sh",          # leading hyphen
            b"brain.relay.feral.sh/../evil",   # path traversal shape
            b"brain relay.feral.sh",           # space
            b"brain\x00.relay.feral.sh",       # embedded NUL
            b"\xff\xfe not ascii",             # non-ascii
            b"a" * 300,                        # over the DNS limit
            b"",                               # empty
        ],
    )
    def test_suspicious_hosts_are_refused(self, raw):
        assert peek_sni(self._hello_with_host(raw)).status == "no_sni"


class TestNoSni:
    def test_a_client_hello_without_extensions(self):
        body = (
            b"\x03\x03" + b"\x00" * 32 + b"\x00"
            + b"\x00\x02\x00\x2f" + b"\x01\x00"
        )
        hs = b"\x01" + len(body).to_bytes(3, "big") + body
        record = b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs
        assert peek_sni(record).status == "no_sni"

    def test_extensions_present_but_no_server_name(self):
        ext = b"\x00\x17\x00\x00"  # extended_master_secret, empty
        exts = len(ext).to_bytes(2, "big") + ext
        body = (
            b"\x03\x03" + b"\x00" * 32 + b"\x00"
            + b"\x00\x02\x00\x2f" + b"\x01\x00" + exts
        )
        hs = b"\x01" + len(body).to_bytes(3, "big") + body
        record = b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs
        assert peek_sni(record).status == "no_sni"


def test_peek_never_raises_on_random_input():
    """Fuzz-ish. The boundary must return an outcome for anything."""
    import random

    rng = random.Random(20260805)
    for _ in range(3000):
        n = rng.randrange(0, 400)
        buf = bytes(rng.randrange(256) for _ in range(n))
        outcome = peek_sni(buf)
        assert outcome.status in {
            "found", "need_more", "not_tls", "no_sni", "malformed",
        }
        if outcome.status != "found":
            assert outcome.host is None
