"""Issuance is where a name gets handed to a key, so it gets attacked.

Two properties carry the whole design, and most of what follows is one
way to break one of them:

* **A brain can only get a certificate for its own name.** The signature
  proves who is asking; the CSR says what they are asking for; the check
  between the two is the only thing standing between an honest brain and
  a certificate for someone else's host.
* **The control plane never holds a private key.** It receives a CSR,
  which is useless without the key that made it. If any of these tests
  find key material in what the control plane stores or returns, the
  local-first claim is false and the relay is a man in the middle.

Every ACME interaction here runs against :class:`FakeAcmeBackend`. No
test in this file opens a socket, and none of them touches Let's
Encrypt, staging or otherwise.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from feral_relay_cp.acme import (
    LETSENCRYPT_PRODUCTION_DIRECTORY,
    LETSENCRYPT_STAGING_DIRECTORY,
    MAX_CLOCK_SKEW_SECONDS,
    AcmeError,
    FakeAcmeBackend,
    IssuanceRecord,
    LetsEncryptBackend,
    canonical_payload,
    csr_names,
    issue_certificate,
)
from feral_relay_cp.dns_store import (
    DEFAULT_TTL_SECONDS,
    DnsStoreError,
    MemoryDnsChallengeStore,
    challenge_record_name,
    relay_domain,
)
from feral_relay_cp.identity import derive_relay_id


# ─────────────────────────────────────────────────────────────────────
# Doubles and builders
# ─────────────────────────────────────────────────────────────────────


class MemoryIssuanceStore:
    def __init__(self):
        self.keys: dict[str, str] = {}
        self.nonces: set[tuple[str, str]] = set()
        self.issuances: dict[str, IssuanceRecord] = {}
        self.all_issuances: list[IssuanceRecord] = []

    def register(self, relay_id: str, public_key_b64: str) -> None:
        self.keys[relay_id] = public_key_b64

    def public_key_for(self, relay_id):
        return self.keys.get(relay_id)

    def seen_nonce(self, relay_id, nonce):
        return (relay_id, nonce) in self.nonces

    def remember_nonce(self, relay_id, nonce, expires_at):
        self.nonces.add((relay_id, nonce))

    def record_issuance(self, record):
        self.issuances[record.relay_id] = record
        self.all_issuances.append(record)

    def last_issuance(self, relay_id):
        return self.issuances.get(relay_id)


class Brain:
    """A brain, as the control plane sees it: an identity and a CSR.

    Mirrors what ``feral-core``'s ``relay_cert`` does on the other side
    of the wire. The TLS private key is created here and stays here: the
    tests assert the control plane never sees its bytes.
    """

    def __init__(self):
        self.identity = ed25519.Ed25519PrivateKey.generate()
        self.pub = self.identity.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.pub_b64 = base64.b64encode(self.pub).decode("ascii")
        self.relay_id = derive_relay_id(self.pub)
        self.tls_key = ec.generate_private_key(ec.SECP256R1())

    @property
    def domain(self) -> str:
        return relay_domain(self.relay_id)

    @property
    def tls_private_key_der(self) -> bytes:
        return self.tls_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    @property
    def tls_private_key_pem(self) -> str:
        return self.tls_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")

    def csr(self, *, names=None, key=None, common_name=True) -> str:
        names = [self.domain] if names is None else list(names)
        key = key or self.tls_key
        builder = x509.CertificateSigningRequestBuilder()
        if common_name:
            builder = builder.subject_name(
                x509.Name(
                    [x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, names[0])]
                )
            )
        else:
            builder = builder.subject_name(x509.Name([]))
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
            critical=False,
        )
        csr = builder.sign(key, hashes.SHA256())
        return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def order(self, *, csr=None, relay_id=None, ts=None, nonce="n-1", sign_with=None):
        body = {
            "relay_id": relay_id or self.relay_id,
            "public_key": self.pub_b64,
            "csr": self.csr() if csr is None else csr,
            "ts": time.time() if ts is None else ts,
            "nonce": nonce,
        }
        signer = sign_with or self.identity
        body["signature"] = base64.b64encode(
            signer.sign(canonical_payload(body))
        ).decode("ascii")
        return body


@pytest.fixture
def brain():
    return Brain()


@pytest.fixture
def store(brain):
    s = MemoryIssuanceStore()
    s.register(brain.relay_id, brain.pub_b64)
    return s


@pytest.fixture
def dns():
    return MemoryDnsChallengeStore()


@pytest.fixture
def backend():
    return FakeAcmeBackend()


# ─────────────────────────────────────────────────────────────────────
# The happy path, so the failures below mean something
# ─────────────────────────────────────────────────────────────────────


class TestHonestIssuance:
    def test_a_correctly_signed_order_yields_a_certificate_for_its_own_name(
        self, brain, store, dns, backend
    ):
        record = issue_certificate(brain.order(), store, dns, backend)

        assert record.relay_id == brain.relay_id
        assert record.domain == f"{brain.relay_id}.relay.feral.sh"

        leaf = x509.load_pem_x509_certificate(record.leaf_pem.encode())
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        assert [n.value for n in san.value] == [brain.domain]

    def test_the_issued_certificate_matches_the_key_the_brain_kept(
        self, brain, store, dns, backend
    ):
        """The certificate is worthless to anyone but the brain.

        The leaf's public key must be the one from the CSR, which is the
        one whose private half never left the laptop.
        """
        record = issue_certificate(brain.order(), store, dns, backend)
        leaf = x509.load_pem_x509_certificate(record.leaf_pem.encode())

        issued_pub = leaf.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        brain_pub = brain.tls_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        assert issued_pub == brain_pub

    def test_the_order_actually_ran(self, brain, store, dns, backend):
        issue_certificate(brain.order(), store, dns, backend)
        assert backend.validated == ["order-1"]
        assert backend.finalized == ["order-1"]

    def test_a_base64_der_csr_is_accepted_too(self, brain, store, dns, backend):
        pem = brain.csr()
        der = x509.load_pem_x509_csr(pem.encode()).public_bytes(
            serialization.Encoding.DER
        )
        body = brain.order(csr=base64.b64encode(der).decode("ascii"))
        record = issue_certificate(body, store, dns, backend)
        assert record.domain == brain.domain


# ─────────────────────────────────────────────────────────────────────
# The property the whole module exists for
# ─────────────────────────────────────────────────────────────────────


class TestNoPrivateKeyEverReachesTheControlPlane:
    def test_nothing_stored_or_returned_contains_key_material(
        self, brain, store, dns, backend
    ):
        """The claim that makes the relay safe to operate.

        The brain's TLS private key is generated on the brain. If any
        byte of it, or any PEM private key header at all, turns up in
        what the control plane stored, returned, or published to DNS,
        then whoever runs the relay can impersonate the brain and the
        local-first promise is false.
        """
        body = brain.order()
        record = issue_certificate(body, store, dns, backend)

        surfaces = {
            "request body": json.dumps(body),
            "issuance record": json.dumps(record.__dict__, default=str),
            "leaf": record.leaf_pem,
            "chain": record.chain_pem,
            "stored issuances": json.dumps(
                [r.__dict__ for r in store.all_issuances], default=str
            ),
            "dns store": json.dumps(
                [r.__dict__ for r in dns.live_records()], default=str
            ),
        }

        secret_pem = brain.tls_private_key_pem
        secret_b64 = base64.b64encode(brain.tls_private_key_der).decode("ascii")

        for where, text in surfaces.items():
            assert "PRIVATE KEY" not in text, f"private key header found in {where}"
            assert secret_pem not in text, f"the brain's key PEM found in {where}"
            assert secret_b64 not in text, f"the brain's key bytes found in {where}"

    def test_the_issuance_record_has_no_field_that_could_hold_a_key(self):
        """Asserted on the shape, not just on one run's contents.

        A future field named ``private_key`` would pass the content
        check above on every test that happens not to populate it. Pin
        the field set instead.
        """
        assert set(IssuanceRecord.__dataclass_fields__) == {
            "relay_id",
            "domain",
            "serial",
            "not_after",
            "issued_at",
            "leaf_pem",
            "chain_pem",
        }

    def test_a_backend_returning_a_key_in_the_chain_is_refused(
        self, brain, store, dns
    ):
        """Defence in depth against a backend we did not write.

        Nothing in this flow can produce a private key, but the whole
        design rests on that, so a chain carrying one is refused at the
        boundary rather than stored.
        """

        class LeakyBackend(FakeAcmeBackend):
            def finalize(self, order_id, csr_der):
                issued = super().finalize(order_id, csr_der)
                return type(issued)(
                    leaf_pem=issued.leaf_pem,
                    chain_pem=issued.chain_pem + brain.tls_private_key_pem,
                    not_after=issued.not_after,
                    serial=issued.serial,
                )

        with pytest.raises(AcmeError) as exc:
            issue_certificate(brain.order(), store, dns, LeakyBackend())
        assert exc.value.code == "private_key_in_certificate"
        assert store.all_issuances == []


# ─────────────────────────────────────────────────────────────────────
# The security core: a CSR for a name you do not own
# ─────────────────────────────────────────────────────────────────────


class TestACsrForAnotherBrainsNameIsRefused:
    def test_an_attacker_cannot_get_a_certificate_for_a_victims_name(
        self, dns, backend
    ):
        """The attack this module exists to stop.

        The attacker is entirely legitimate: a registered brain, its own
        Ed25519 key, its own relay_id, a correctly signed request. The
        only dishonest thing is the CSR, which names the victim's host.
        Everything except the subject check would let this through.
        """
        victim = Brain()
        attacker = Brain()
        store = MemoryIssuanceStore()
        store.register(attacker.relay_id, attacker.pub_b64)
        store.register(victim.relay_id, victim.pub_b64)

        body = attacker.order(csr=attacker.csr(names=[victim.domain]))

        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "csr_name_mismatch"
        assert store.all_issuances == []
        assert dns.live_records() == ()

    def test_a_smuggled_second_san_is_refused(self, brain, store, dns, backend):
        """The subtler version of the same attack.

        The first name is honest, so a check that only looked at the
        common name or at ``names[0]`` would pass. A certificate is valid
        for every name in it, so every name is checked.
        """
        victim = Brain()
        body = brain.order(csr=brain.csr(names=[brain.domain, victim.domain]))

        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "csr_name_mismatch"

    def test_a_wildcard_csr_is_refused(self, brain, store, dns, backend):
        """One wildcard would cover every brain in the zone."""
        body = brain.order(csr=brain.csr(names=["*.relay.feral.sh"]))
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "csr_name_mismatch"

    def test_a_suffix_lookalike_domain_is_refused(self, brain, store, dns, backend):
        """``x.relay.feral.sh.evil.com`` ends with nothing we own, and
        ``evil-<id>.relay.feral.sh`` merely contains the id. A check
        written with ``in`` or ``endswith`` would accept one of them."""
        for name in (
            f"{brain.relay_id}.relay.feral.sh.evil.example",
            f"evil-{brain.relay_id}.relay.feral.sh",
            f"sub.{brain.domain}",
        ):
            body = brain.order(csr=brain.csr(names=[name]), nonce=f"n-{name}")
            with pytest.raises(AcmeError) as exc:
                issue_certificate(body, store, dns, backend)
            assert exc.value.code == "csr_name_mismatch", name

    def test_a_non_dns_san_is_refused(self, brain, store, dns, backend):
        """An IP SAN is not a name this control plane validates."""
        import ipaddress

        builder = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(
                x509.Name(
                    [x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, brain.domain)]
                )
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName(brain.domain),
                        x509.IPAddress(ipaddress.ip_address("10.0.0.1")),
                    ]
                ),
                critical=False,
            )
        )
        csr = builder.sign(brain.tls_key, hashes.SHA256())
        body = brain.order(
            csr=csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
        )
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "csr_name_mismatch"

    def test_a_csr_with_no_name_at_all_is_refused(self, brain, store, dns, backend):
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([]))
            .sign(brain.tls_key, hashes.SHA256())
        )
        body = brain.order(
            csr=csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
        )
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "csr_no_name"

    def test_csr_names_enumerates_both_the_cn_and_every_san(self, brain):
        """The name check is only as good as this enumeration, so pin it."""
        other = Brain()
        pem = brain.csr(names=[brain.domain, other.domain])
        names = csr_names(x509.load_pem_x509_csr(pem.encode()))
        assert names == (brain.domain, brain.domain, other.domain)


# ─────────────────────────────────────────────────────────────────────
# Identity: who is asking
# ─────────────────────────────────────────────────────────────────────


class TestForgedRequestsAreRefused:
    def test_claiming_someone_elses_relay_id_is_refused(self, dns, backend):
        """Same attack as registration's, at a worse moment.

        Succeeding here would produce a real certificate rather than
        just a bad database row.
        """
        victim = Brain()
        attacker = Brain()
        store = MemoryIssuanceStore()
        store.register(victim.relay_id, victim.pub_b64)

        body = attacker.order(
            relay_id=victim.relay_id, csr=attacker.csr(names=[victim.domain])
        )
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "relay_id_mismatch"

    def test_a_request_signed_by_a_different_key_is_refused(
        self, brain, store, dns, backend
    ):
        other = ed25519.Ed25519PrivateKey.generate()
        body = brain.order(sign_with=other)
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "bad_signature"

    def test_swapping_the_csr_after_signing_is_refused(
        self, brain, store, dns, backend
    ):
        """The signature covers the CSR, so a request captured in flight
        cannot have someone else's CSR pasted into it."""
        victim = Brain()
        body = brain.order()
        body["csr"] = victim.csr()
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "bad_signature"

    def test_every_signed_field_is_actually_covered(self, brain):
        """A request field missing from canonical_payload is
        unauthenticated. Assert the set so adding one fails loudly."""
        signed = json.loads(canonical_payload(brain.order()))
        assert set(signed) == {"relay_id", "public_key", "csr", "ts", "nonce"}

    def test_a_csr_not_signed_by_its_own_key_is_refused(
        self, brain, store, dns, backend
    ):
        """Proof of possession.

        A CSR is public once it is sent. Without this check, an attacker
        who captured a victim's CSR could not use it for the victim's
        name (the subject check stops that), but a CSR is also how the
        control plane learns which key the certificate binds to, and
        issuing over a key nobody proved they hold is a certificate we
        cannot say anything about.
        """
        pem = brain.csr()
        der = bytearray(
            x509.load_pem_x509_csr(pem.encode()).public_bytes(
                serialization.Encoding.DER
            )
        )
        der[-1] ^= 0xFF  # corrupt the CSR's own signature
        body = brain.order(csr=base64.b64encode(bytes(der)).decode("ascii"))
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code in {"bad_csr", "bad_csr_signature"}

    def test_an_unregistered_brain_is_refused(self, brain, dns, backend):
        with pytest.raises(AcmeError) as exc:
            issue_certificate(brain.order(), MemoryIssuanceStore(), dns, backend)
        assert exc.value.code == "unregistered"

    def test_a_relay_id_bound_to_a_different_key_is_refused(
        self, brain, dns, backend
    ):
        """Unreachable while derivation holds, kept because it is the
        invariant we care about: a future change to derivation must not
        silently turn rebinding into a supported operation."""
        store = MemoryIssuanceStore()
        store.register(brain.relay_id, Brain().pub_b64)
        with pytest.raises(AcmeError) as exc:
            issue_certificate(brain.order(), store, dns, backend)
        assert exc.value.code == "key_mismatch"


class TestReplayAndMalformedInput:
    def test_a_replayed_order_is_refused(self, brain, store, dns, backend):
        body = brain.order(nonce="same")
        issue_certificate(body, store, dns, backend)
        with pytest.raises(AcmeError) as exc:
            issue_certificate(dict(body), store, dns, backend)
        assert exc.value.code == "replayed_nonce"

    @pytest.mark.parametrize("delta", [1, -1])
    def test_a_request_outside_the_clock_window_is_refused(
        self, brain, store, dns, backend, delta
    ):
        ts = time.time() + delta * (MAX_CLOCK_SKEW_SECONDS + 60)
        with pytest.raises(AcmeError) as exc:
            issue_certificate(brain.order(ts=ts), store, dns, backend)
        assert exc.value.code == "stale_request"

    @pytest.mark.parametrize(
        "field", ["relay_id", "public_key", "csr", "ts", "nonce", "signature"]
    )
    def test_a_missing_field_is_refused(self, brain, store, dns, backend, field):
        body = brain.order()
        body.pop(field)
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "missing_field"

    def test_a_garbage_csr_is_refused(self, brain, store, dns, backend):
        with pytest.raises(AcmeError) as exc:
            issue_certificate(
                brain.order(csr="not a csr"), store, dns, backend
            )
        assert exc.value.code == "bad_csr"

    def test_a_non_numeric_timestamp_is_refused(self, brain, store, dns, backend):
        with pytest.raises(AcmeError) as exc:
            issue_certificate(brain.order(ts="yesterday"), store, dns, backend)
        assert exc.value.code == "bad_timestamp"

    def test_a_weak_rsa_key_is_refused(self, brain, store, dns, backend):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        body = brain.order(csr=brain.csr(key=weak))
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "weak_key"

    def test_a_weak_curve_is_refused(self, brain, store, dns, backend):
        weak = ec.generate_private_key(ec.SECP192R1())
        body = brain.order(csr=brain.csr(key=weak))
        with pytest.raises(AcmeError) as exc:
            issue_certificate(body, store, dns, backend)
        assert exc.value.code == "weak_key"


# ─────────────────────────────────────────────────────────────────────
# DNS-01
# ─────────────────────────────────────────────────────────────────────


class TestTheChallengeGoesUnderTheRelaysOwnName:
    def test_a_backend_pointing_at_another_name_is_refused(
        self, brain, store, dns
    ):
        """The backend says what to publish, not where.

        A compromised or confused ACME backend must not be able to make
        us write a challenge answer under a name we did not open this
        order for.
        """
        victim = Brain()
        backend = FakeAcmeBackend(
            challenge_name=challenge_record_name(victim.relay_id)
        )
        with pytest.raises(AcmeError) as exc:
            issue_certificate(brain.order(), store, dns, backend)
        assert exc.value.code == "challenge_name_mismatch"
        assert dns.live_records() == ()

    def test_the_answer_is_published_during_the_order_and_retracted_after(
        self, brain, store, dns
    ):
        seen: list[tuple[str, ...]] = []
        name = challenge_record_name(brain.relay_id)

        class ObservingBackend(FakeAcmeBackend):
            def await_validation(self, order_id):
                seen.append(dns.txt_values(name))
                super().await_validation(order_id)

        issue_certificate(brain.order(), store, dns, ObservingBackend())

        assert len(seen) == 1 and len(seen[0]) == 1, "no answer was live to validate"
        assert dns.txt_values(name) == (), "the answer outlived the order"

    def test_a_failed_order_still_retracts_its_answer(self, brain, store, dns):
        """A crashed finalisation must not leave a challenge answerable."""

        class FailingBackend(FakeAcmeBackend):
            def finalize(self, order_id, csr_der):
                raise RuntimeError("the CA fell over")

        with pytest.raises(RuntimeError):
            issue_certificate(brain.order(), store, dns, FailingBackend())
        assert dns.live_records() == ()


class TestTheDnsStore:
    def test_a_record_name_is_derived_from_the_relay_id_not_supplied(self, dns):
        """There is no API that takes a record name, which is the point:
        an order cannot publish under a label it does not own."""
        assert not hasattr(dns, "publish_at")
        rid = derive_relay_id(b"\x01" * 32)
        assert dns.publish(rid, "v").name == f"_acme-challenge.{rid}.relay.feral.sh"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "UPPERCASE" * 4,
            "short",
            "../../etc/passwd",
            "abcdefghijklmnopqrstuvwxyz234567.evil.example",
            "abcdefghijklmnopqrstuvwxyz23456\n",
            "a" * 33,
        ],
    )
    def test_a_relay_id_that_is_not_a_relay_id_never_reaches_the_zone(self, dns, bad):
        with pytest.raises(DnsStoreError) as exc:
            dns.publish(bad, "value")
        assert exc.value.code == "bad_relay_id"

    def test_an_oversized_txt_value_is_refused(self, dns):
        rid = derive_relay_id(b"\x02" * 32)
        with pytest.raises(DnsStoreError) as exc:
            dns.publish(rid, "x" * 256)
        assert exc.value.code == "bad_value"

    def test_two_concurrent_orders_can_both_be_answerable(self, dns):
        """A renewal overlapping a fresh order is the normal case, and
        RFC 8555 lets any matching TXT record satisfy the challenge."""
        rid = derive_relay_id(b"\x03" * 32)
        name = challenge_record_name(rid)
        dns.publish(rid, "first")
        dns.publish(rid, "second")
        assert dns.txt_values(name) == ("first", "second")

        dns.retract(rid, "first")
        assert dns.txt_values(name) == ("second",)

    def test_an_abandoned_answer_stops_being_served(self, dns):
        """The failure mode of a lost cleanup is minutes, not forever."""
        rid = derive_relay_id(b"\x04" * 32)
        name = challenge_record_name(rid)
        start = 1_000_000.0
        dns.publish(rid, "orphan", now=start)
        assert dns.txt_values(name, now=start + 10) == ("orphan",)
        assert dns.txt_values(name, now=start + DEFAULT_TTL_SECONDS + 1) == ()

    def test_retracting_something_absent_is_not_an_error(self, dns):
        rid = derive_relay_id(b"\x05" * 32)
        assert dns.retract(rid, "never-published") is False


# ─────────────────────────────────────────────────────────────────────
# Rate limit safety
# ─────────────────────────────────────────────────────────────────────


class TestProductionIsOptIn:
    def test_the_default_directory_is_staging(self):
        """Production is 50 certificates per registered domain per week,
        shared by every relay, and it does not refill early."""
        backend = LetsEncryptBackend(
            account_key_pem="unused-in-this-test", contact_email="ops@example.com"
        )
        assert backend.directory_url == LETSENCRYPT_STAGING_DIRECTORY
        assert backend.is_staging is True

    def test_production_without_the_opt_in_is_refused(self):
        with pytest.raises(AcmeError) as exc:
            LetsEncryptBackend(
                account_key_pem="unused-in-this-test",
                contact_email="ops@example.com",
                directory_url=LETSENCRYPT_PRODUCTION_DIRECTORY,
            )
        assert exc.value.code == "production_not_opted_in"

    def test_production_with_the_opt_in_is_allowed(self):
        backend = LetsEncryptBackend(
            account_key_pem="unused-in-this-test",
            contact_email="ops@example.com",
            directory_url=LETSENCRYPT_PRODUCTION_DIRECTORY,
            allow_production=True,
        )
        assert backend.is_staging is False

    def test_constructing_the_real_backend_opens_no_connection(self):
        """Construction must stay inert.

        If building the object talked to the CA, importing a module or
        running a test that never issues anything could still consume
        budget or create an account.
        """
        backend = LetsEncryptBackend(
            account_key_pem="unused-in-this-test", contact_email="ops@example.com"
        )
        assert backend._client is None

    def test_the_optional_acme_dependency_fails_with_an_actionable_message(self):
        """`acme` is not a dependency of this package. If it is absent,
        the error must say what to install rather than surfacing an
        ImportError from three frames down."""
        try:
            import acme  # noqa: F401
        except ImportError:
            backend = LetsEncryptBackend(
                account_key_pem="unused-in-this-test",
                contact_email="ops@example.com",
            )
            with pytest.raises(AcmeError) as exc:
                backend.create_order("example.relay.feral.sh")
            assert exc.value.code == "acme_not_installed"
            assert "pip install acme" in str(exc.value)
        else:
            pytest.skip("acme is installed; the missing-dependency path is moot")


class TestRenewalPriorityIsDocumented:
    def test_the_module_states_the_renewal_rule(self):
        """The rule is a scheduling constraint this module cannot
        enforce on its own, so it is written where whoever builds the
        scheduler will read it. Pinned so it cannot be dropped silently.
        """
        import feral_relay_cp.acme as acme_mod

        text = " ".join(
            ((acme_mod.__doc__ or "") + (LetsEncryptBackend.__doc__ or "")).split()
        ).lower()
        assert "renewals take strict priority over new issuance" in text
        assert "50 certificates per registered domain per week" in text
