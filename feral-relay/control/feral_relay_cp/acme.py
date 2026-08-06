"""Issuing a certificate for a name, to the brain that owns the name.

The control plane runs on infrastructure the company operates. The brain
runs on a user's laptop. Once relay traffic crosses our machines, the
only thing keeping the local-first claim honest is that we cannot read
it, and the only thing keeping *that* true is that we never hold the
brain's TLS private key.

So the split is deliberate and is the point of this module:

* the **brain** generates a P-256 keypair and a CSR locally, and keeps
  the private half in its vault;
* the **control plane** receives only the CSR, proves who is asking,
  runs the ACME order, and hands back a signed certificate;
* a CSR is useless to whoever holds it without the matching private key,
  so a full compromise of this service yields certificates nobody can
  serve traffic with.

What the control plane must get right:

1. **The request is signed by the brain's Ed25519 identity**, and the
   relay_id it claims derives from that same key. This is the
   registration check, repeated, for the same reason it exists there: a
   signature alone only proves the sender holds *a* key.
2. **The CSR's subject matches the relay_id the signature proves.** This
   is the security core of the module. Without it, a brain could sign an
   honest request with its own key and attach a CSR for someone else's
   name, and we would hand it a valid certificate for a host it does not
   own. Every name in the CSR is checked, not just the common name,
   because a SAN nobody looked at is a certificate for whatever it says.
3. **The CSR is self-signed by its own key.** Proof of possession. It
   stops a brain from getting a certificate minted over a CSR it
   scraped from someone else's traffic.
4. **The DNS-01 answer is published under the relay's own name.** The
   ACME backend tells us what to publish; it does not get to choose
   where. See :mod:`feral_relay_cp.dns_store`.
5. **Nothing this module stores or returns contains private key
   material.** Asserted by a test, not by convention.

Rate limits and renewal priority
--------------------------------
Let's Encrypt allows 50 certificates per registered domain per week, and
every relay name lives under one registered domain,
``relay.feral.sh``. That budget is shared by every brain and it does not
refill early. Two consequences are load-bearing:

* Tests must never touch the production directory. :class:`AcmeBackend`
  exists so they can run against :class:`FakeAcmeBackend`, and
  :class:`LetsEncryptBackend` defaults to the staging directory with
  production requiring an explicit opt-in.
* **When the budget is constrained, renewals take strict priority over
  new issuance.** A renewal that misses its window takes a working relay
  offline; a new issuance that waits leaves a brain in the state it was
  already in. Any queue or scheduler placed in front of
  :func:`issue_certificate` must drain every pending renewal before it
  admits a first-time order, and must never let new issuance consume the
  last of the weekly allowance.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from .dns_store import (
    RELAY_BASE_DOMAIN,
    DnsChallengeStore,
    challenge_record_name,
    relay_domain,
)
from .identity import derive_relay_id, verify_signature

#: Same window registration uses. A captured order request stops being
#: interesting on the same schedule as a captured registration.
MAX_CLOCK_SKEW_SECONDS = 300

#: Let's Encrypt directories. Staging issues from an untrusted root and
#: has generous limits; production has 50 certificates per registered
#: domain per week and burning that is not recoverable by asking.
LETSENCRYPT_STAGING_DIRECTORY = "https://acme-staging-v02.api.letsencrypt.org/directory"
LETSENCRYPT_PRODUCTION_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"

#: The production ceiling, stated here so a scheduler can reason about it
#: without rediscovering it from an outage.
PRODUCTION_CERTIFICATES_PER_WEEK = 50

#: Accepted key types for a relay leaf. P-256 is what the brain
#: generates; the others are allowed so a future brain can move without
#: a control plane deploy. Anything weaker is refused rather than
#: quietly issued.
_MIN_RSA_BITS = 2048


class AcmeError(Exception):
    """An issuance that must not proceed, and why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class DnsChallenge:
    """One DNS-01 answer an ACME backend wants published.

    ``name`` is what the backend believes the record should be called.
    The order flow checks it against the name the relay_id is entitled
    to and refuses a mismatch, so this field is a claim, not an
    instruction.
    """

    name: str
    value: str


@dataclass(frozen=True)
class IssuedCertificate:
    """What a backend returns. Leaf plus chain, and no key.

    There is no private key field, and there must never be one: the key
    that matches this certificate was generated on the brain and has
    never been on this machine.
    """

    leaf_pem: str
    chain_pem: str
    not_after: float
    serial: str


@dataclass(frozen=True)
class IssuanceRecord:
    """What the control plane persists about an issuance.

    Enough to answer "who has a certificate, for what name, expiring
    when" without holding anything that could be used to impersonate a
    brain. The leaf and chain are public by construction (they are sent
    to every client that connects), so keeping them is not a leak; a
    private key would be, which is why there is no field for one.
    """

    relay_id: str
    domain: str
    serial: str
    not_after: float
    issued_at: float
    leaf_pem: str
    chain_pem: str


class IssuanceStore(Protocol):
    """What issuance needs from storage, and nothing more.

    Same shape as registration's ``BrainStore`` and for the same reason:
    the logic stays testable without a database, and swapping the
    backing store is not a rewrite.
    """

    def public_key_for(self, relay_id: str) -> Optional[str]: ...

    def seen_nonce(self, relay_id: str, nonce: str) -> bool: ...

    def remember_nonce(self, relay_id: str, nonce: str, expires_at: float) -> None: ...

    def record_issuance(self, record: IssuanceRecord) -> None: ...

    def last_issuance(self, relay_id: str) -> Optional[IssuanceRecord]: ...


class AcmeBackend(Protocol):
    """The ACME order, reduced to the four steps the flow needs.

    Pluggable so tests run against :class:`FakeAcmeBackend` and never
    against a real CA. An implementation is free to be as chatty with
    the CA as it likes between these calls; the flow only cares that
    challenges come back before validation and that finalisation takes
    the brain's CSR unmodified.
    """

    def create_order(self, domain: str) -> str: ...

    def dns_challenges(self, order_id: str) -> Sequence[DnsChallenge]: ...

    def await_validation(self, order_id: str) -> None: ...

    def finalize(self, order_id: str, csr_der: bytes) -> IssuedCertificate: ...


def canonical_payload(body: dict) -> bytes:
    """The exact bytes a brain signs for a certificate order.

    Sorted keys, no whitespace, same discipline as registration. The CSR
    is signed as the literal string that was transmitted, so a swapped
    CSR breaks the signature: that is what binds "who is asking" to
    "what they are asking for". A field in the request that is missing
    here would be unauthenticated, which is how signed protocols quietly
    stop protecting anything.
    """
    return json.dumps(
        {
            "relay_id": body["relay_id"],
            "public_key": body["public_key"],
            "csr": body["csr"],
            "ts": body["ts"],
            "nonce": body["nonce"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def issue_certificate(
    body: dict,
    store: IssuanceStore,
    dns: DnsChallengeStore,
    backend: AcmeBackend,
    *,
    base_domain: str = RELAY_BASE_DOMAIN,
    now: Optional[float] = None,
) -> IssuanceRecord:
    """Run an order for the name the requester proves it owns.

    Raises :class:`AcmeError` on anything suspect and issues nothing.
    Returns the record that was persisted, which contains the leaf and
    chain and no key material.
    """
    now = time.time() if now is None else now

    for field in ("relay_id", "public_key", "csr", "ts", "nonce", "signature"):
        if not body.get(field):
            raise AcmeError("missing_field", f"{field} is required")

    relay_id = str(body["relay_id"]).strip().lower()
    public_key_b64 = str(body["public_key"]).strip()
    nonce = str(body["nonce"]).strip()
    csr_text = str(body["csr"])

    try:
        ts = float(body["ts"])
    except (TypeError, ValueError):
        raise AcmeError("bad_timestamp", "ts must be a number")

    # Cheapest checks first, and they bound how long a captured order
    # request stays useful.
    if abs(now - ts) > MAX_CLOCK_SKEW_SECONDS:
        raise AcmeError(
            "stale_request",
            f"ts is more than {MAX_CLOCK_SKEW_SECONDS}s from our clock",
        )

    if store.seen_nonce(relay_id, nonce):
        raise AcmeError("replayed_nonce", "this request has already been used")

    # Verify before trusting any of the body, including the key it
    # carries and the CSR it carries.
    if not verify_signature(
        canonical_payload(body), body["signature"], public_key_b64
    ):
        raise AcmeError("bad_signature", "signature does not verify")

    # A valid signature proves the sender holds a key. This proves the
    # id they are claiming is the one that key is entitled to.
    try:
        expected_id = derive_relay_id(_decode_ed25519_key(public_key_b64))
    except Exception:
        raise AcmeError("bad_public_key", "public_key is not a valid Ed25519 key")

    if relay_id != expected_id:
        raise AcmeError(
            "relay_id_mismatch",
            "relay_id is not derived from this public key",
        )

    # Issuance follows registration. The derivation check above already
    # proves entitlement to the name, so this is not what stops
    # impersonation; it is what stops us minting certificates for names
    # that were never bound, and it catches a key that changed under an
    # id that is supposed to be permanent.
    bound_key = store.public_key_for(relay_id)
    if bound_key is None:
        raise AcmeError("unregistered", "this relay_id has not been registered")
    if bound_key != public_key_b64:
        raise AcmeError(
            "key_mismatch",
            "this relay_id is bound to a different key",
        )

    domain = relay_domain(relay_id, base_domain)
    csr = _parse_csr(csr_text)

    # The security core. Everything above proves who is asking; this is
    # what stops them asking for someone else's name.
    _assert_csr_is_for(csr, domain)

    csr_der = csr.public_bytes(_serialization().Encoding.DER)

    order_id = backend.create_order(domain)
    challenges = list(backend.dns_challenges(order_id))
    if not challenges:
        raise AcmeError("no_challenge", "the ACME backend offered no DNS-01 challenge")

    expected_record = challenge_record_name(relay_id, base_domain)
    published: list[str] = []
    try:
        for challenge in challenges:
            # The backend says where to publish. It does not get to
            # decide. A backend that has been compromised or is simply
            # confused must not be able to write a challenge answer
            # under another brain's label.
            if challenge.name != expected_record:
                raise AcmeError(
                    "challenge_name_mismatch",
                    f"backend asked to publish at {challenge.name!r}, "
                    f"but this order is for {expected_record!r}",
                )
            dns.publish(relay_id, challenge.value)
            published.append(challenge.value)

        backend.await_validation(order_id)
        issued = backend.finalize(order_id, csr_der)
    finally:
        for value in published:
            try:
                dns.retract(relay_id, value)
            except Exception:  # pragma: no cover - cleanup must not mask
                pass

    _assert_no_private_key(issued.leaf_pem, "leaf")
    _assert_no_private_key(issued.chain_pem, "chain")

    record = IssuanceRecord(
        relay_id=relay_id,
        domain=domain,
        serial=issued.serial,
        not_after=issued.not_after,
        issued_at=now,
        leaf_pem=issued.leaf_pem,
        chain_pem=issued.chain_pem,
    )
    store.record_issuance(record)
    store.remember_nonce(relay_id, nonce, now + MAX_CLOCK_SKEW_SECONDS * 2)
    return record


# ─────────────────────────────────────────────────────────────────────
# CSR inspection
# ─────────────────────────────────────────────────────────────────────


def _x509():
    from cryptography import x509

    return x509


def _serialization():
    from cryptography.hazmat.primitives import serialization

    return serialization


def _parse_csr(csr_text: str):
    """Accept a PEM CSR or base64 DER, and nothing malformed.

    Both encodings are accepted because a brain may reasonably send
    either; neither is trusted until the checks below pass.
    """
    x509 = _x509()
    raw = csr_text.strip()
    try:
        if "-----BEGIN CERTIFICATE REQUEST-----" in raw:
            csr = x509.load_pem_x509_csr(raw.encode("utf-8"))
        else:
            csr = x509.load_der_x509_csr(base64.b64decode(raw, validate=True))
    except Exception as exc:
        raise AcmeError("bad_csr", f"csr is not a readable CSR: {exc}")

    # Proof of possession. Without this, a CSR captured from another
    # brain could be replayed here by anyone holding an Ed25519
    # identity, and we would issue a certificate over a key we have no
    # evidence the requester holds.
    if not csr.is_signature_valid:
        raise AcmeError("bad_csr_signature", "the CSR is not signed by its own key")

    _assert_key_is_strong_enough(csr.public_key())
    return csr


def _assert_key_is_strong_enough(public_key) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    if isinstance(public_key, ec.EllipticCurvePublicKey):
        if not isinstance(public_key.curve, (ec.SECP256R1, ec.SECP384R1)):
            raise AcmeError(
                "weak_key",
                f"unsupported curve {public_key.curve.name!r}; use P-256",
            )
        return
    if isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size < _MIN_RSA_BITS:
            raise AcmeError(
                "weak_key",
                f"RSA keys must be at least {_MIN_RSA_BITS} bits",
            )
        return
    raise AcmeError(
        "weak_key",
        f"unsupported key type {type(public_key).__name__}",
    )


def csr_names(csr) -> tuple[str, ...]:
    """Every name a CSR asks to be valid for.

    Both the subject common name and the subjectAltName entries, because
    a certificate is valid for whatever is in it and a name nobody
    enumerated is a name nobody checked.
    """
    x509 = _x509()
    names: list[str] = []

    for attribute in csr.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME):
        value = attribute.value
        if isinstance(value, bytes):  # pragma: no cover - defensive
            value = value.decode("utf-8", "replace")
        names.append(str(value))

    try:
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except Exception:
        san = None

    if san is not None:
        for entry in san.value:
            if isinstance(entry, x509.DNSName):
                names.append(entry.value)
            else:
                # An IP or email SAN is not something this control plane
                # ever issues. Surface it as a name so the equality check
                # below refuses it rather than ignoring it.
                names.append(f"<non-dns:{type(entry).__name__}>")

    return tuple(names)


def _assert_csr_is_for(csr, domain: str) -> None:
    """The check that stops one brain getting another brain's name.

    Every name in the CSR must be exactly the domain the signature
    proves the requester owns. Not "contains", not "ends with", not "the
    common name matches and the SANs are whatever": a wildcard, a second
    SAN, or a non-DNS name is a refusal, because each of those is a
    certificate for something the requester did not prove.
    """
    names = csr_names(csr)
    if not names:
        raise AcmeError("csr_no_name", "the CSR requests no name at all")

    for name in names:
        if name.strip().lower() != domain:
            raise AcmeError(
                "csr_name_mismatch",
                f"the CSR requests {name!r}, but this key is only entitled "
                f"to {domain!r}",
            )


_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _assert_no_private_key(pem: str, what: str) -> None:
    """A certificate blob must never carry a key.

    Belt and braces: nothing in this module can produce one, but this is
    the invariant the whole design rests on, so it is checked at the
    boundary rather than assumed.
    """
    if _PRIVATE_KEY_MARKER.search(pem or ""):
        raise AcmeError(
            "private_key_in_certificate",
            f"the {what} contains private key material and will not be stored",
        )


def _decode_ed25519_key(public_key_b64: str) -> bytes:
    raw = base64.b64decode(public_key_b64, validate=True)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return raw


# ─────────────────────────────────────────────────────────────────────
# Backends
# ─────────────────────────────────────────────────────────────────────


class FakeAcmeBackend:
    """An ACME server that issues from a throwaway CA, in process.

    Exists so the whole order flow can be exercised without touching
    Let's Encrypt. It signs a real certificate with a real
    ``cryptography`` CA key, so the resulting leaf parses, chains, and
    carries the names from the CSR: the tests assert on a certificate
    rather than on a string that looks like one.

    It is a test double, not a CA. It performs no validation of the
    challenge it hands out beyond recording it, which is exactly what
    makes it useless in production and fine in tests.
    """

    def __init__(self, *, valid_days: int = 90, challenge_name: Optional[str] = None):
        self.valid_days = valid_days
        #: Override the record name in the challenge, to prove the order
        #: flow refuses a backend that points somewhere it should not.
        self.challenge_name = challenge_name
        self.orders: dict[str, dict] = {}
        self.validated: list[str] = []
        self.finalized: list[str] = []
        self._counter = 0
        self._ca_key = None
        self._ca_cert = None

    # -- CA -------------------------------------------------------

    def _ca(self):
        if self._ca_key is None:
            import datetime

            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec

            key = ec.generate_private_key(ec.SECP256R1())
            subject = x509.Name(
                [x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "FERAL Test CA")]
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .sign(key, hashes.SHA256())
            )
            self._ca_key, self._ca_cert = key, cert
        return self._ca_key, self._ca_cert

    # -- AcmeBackend ----------------------------------------------

    def create_order(self, domain: str) -> str:
        self._counter += 1
        order_id = f"order-{self._counter}"
        token = base64.urlsafe_b64encode(
            f"token-{order_id}".encode()
        ).decode().rstrip("=")
        self.orders[order_id] = {"domain": domain, "token": token}
        return order_id

    def dns_challenges(self, order_id: str) -> Sequence[DnsChallenge]:
        order = self.orders[order_id]
        name = self.challenge_name or f"_acme-challenge.{order['domain']}"
        return [DnsChallenge(name=name, value=order["token"])]

    def await_validation(self, order_id: str) -> None:
        self.validated.append(order_id)

    def finalize(self, order_id: str, csr_der: bytes) -> IssuedCertificate:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization

        self.finalized.append(order_id)
        csr = x509.load_der_x509_csr(csr_der)
        ca_key, ca_cert = self._ca()

        now = datetime.datetime.now(datetime.timezone.utc)
        not_after = now + datetime.timedelta(days=self.valid_days)
        builder = (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        )
        try:
            san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            builder = builder.add_extension(san.value, critical=False)
        except Exception:  # pragma: no cover - CSRs from this flow always have a SAN
            pass

        leaf = builder.sign(ca_key, hashes.SHA256())
        encoding = serialization.Encoding.PEM
        return IssuedCertificate(
            leaf_pem=leaf.public_bytes(encoding).decode("ascii"),
            chain_pem=ca_cert.public_bytes(encoding).decode("ascii"),
            not_after=not_after.timestamp(),
            serial=format(leaf.serial_number, "x"),
        )


class LetsEncryptBackend:
    """A real ACME client, defaulting to staging.

    Two things about this class are deliberate:

    **It defaults to staging.** Production has a hard ceiling of
    :data:`PRODUCTION_CERTIFICATES_PER_WEEK` certificates per registered
    domain per week, shared across every relay under
    ``relay.feral.sh``, and there is no way to un-spend it. Reaching the
    production directory requires passing ``allow_production=True``
    alongside the production URL, so no default, no typo, and no config
    file that forgot a key can point a test run at it.

    **Its dependency is optional.** ``acme`` is not a dependency of this
    package and may not be installed; the import happens at construction
    with a message that says what to install, rather than at module
    import where it would break every caller including the tests that
    never touch a real CA.

    Not exercised by any test in this repository, by design: the tested
    path is :class:`FakeAcmeBackend`. Issuance against Let's Encrypt has
    not been run.

    When the weekly budget is constrained, renewals must be admitted
    ahead of new issuance. A renewal that misses its window drops a live
    relay; a delayed first issuance leaves a brain exactly where it
    already was.
    """

    def __init__(
        self,
        *,
        account_key_pem: str,
        contact_email: str,
        directory_url: str = LETSENCRYPT_STAGING_DIRECTORY,
        allow_production: bool = False,
        poll_interval_seconds: float = 3.0,
        poll_timeout_seconds: float = 300.0,
    ):
        if directory_url == LETSENCRYPT_PRODUCTION_DIRECTORY and not allow_production:
            raise AcmeError(
                "production_not_opted_in",
                "the Let's Encrypt production directory requires "
                "allow_production=True. Production allows "
                f"{PRODUCTION_CERTIFICATES_PER_WEEK} certificates per "
                "registered domain per week and the budget does not refill "
                "early; use the staging directory unless you mean it.",
            )
        self.directory_url = directory_url
        self.account_key_pem = account_key_pem
        self.contact_email = contact_email
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self._client = None
        self._orders: dict[str, object] = {}
        self._counter = 0

    @property
    def is_staging(self) -> bool:
        return self.directory_url != LETSENCRYPT_PRODUCTION_DIRECTORY

    @staticmethod
    def _acme_modules():
        """Import the optional ACME client, or say what is missing."""
        try:
            from acme import challenges, client, crypto_util, messages  # noqa: F401
        except ImportError as exc:
            raise AcmeError(
                "acme_not_installed",
                "the `acme` package is required to talk to a real ACME "
                "server. Install it with `pip install acme`, or use "
                "FakeAcmeBackend for tests.",
            ) from exc
        return challenges, client, crypto_util, messages

    def _acme_client(self):
        if self._client is not None:
            return self._client

        challenges, client, _crypto_util, messages = self._acme_modules()
        from cryptography.hazmat.primitives import serialization
        import josepy as jose

        key = serialization.load_pem_private_key(
            self.account_key_pem.encode("utf-8"), password=None
        )
        account_key = jose.JWKRSA(key=key) if hasattr(key, "private_numbers") and hasattr(
            key, "key_size"
        ) else jose.JWKEC(key=key)

        net = client.ClientNetwork(account_key, user_agent="feral-relay-cp")
        directory = client.ClientV2.get_directory(self.directory_url, net)
        acme_client = client.ClientV2(directory, net=net)

        registration = messages.NewRegistration.from_data(
            email=self.contact_email, terms_of_service_agreed=True
        )
        try:
            account = acme_client.new_account(registration)
        except Exception:
            # An existing account for this key is the normal steady
            # state, not an error.
            account = acme_client.query_registration(
                messages.RegistrationResource(body=registration, uri=None)
            )
        net.account = account
        self._client = acme_client
        return acme_client

    def create_order(self, domain: str) -> str:
        _challenges, _client, crypto_util, _messages = self._acme_modules()
        acme_client = self._acme_client()
        # An order needs a CSR up front in ACME v2. We do not have the
        # brain's CSR yet at this point in the flow, so a throwaway
        # placeholder CSR is used for the order and the brain's real CSR
        # is what gets finalised. The placeholder key is generated here,
        # used once, and never stored or returned.
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography import x509

        throwaway = ec.generate_private_key(ec.SECP256R1())
        placeholder = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, domain)])
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False
            )
            .sign(throwaway, hashes.SHA256())
        )
        order = acme_client.new_order(placeholder.public_bytes(serialization.Encoding.PEM))
        self._counter += 1
        order_id = f"le-{self._counter}"
        self._orders[order_id] = order
        return order_id

    def dns_challenges(self, order_id: str) -> Sequence[DnsChallenge]:
        acme_challenges, _client, _crypto_util, _messages = self._acme_modules()
        acme_client = self._acme_client()
        order = self._orders[order_id]

        out: list[DnsChallenge] = []
        for authorization in order.authorizations:
            for challenge in authorization.body.challenges:
                if isinstance(challenge.chall, acme_challenges.DNS01):
                    name = challenge.chall.validation_domain_name(
                        authorization.body.identifier.value
                    )
                    value = challenge.chall.validation(acme_client.net.key)
                    out.append(DnsChallenge(name=name, value=value))
        return out

    def await_validation(self, order_id: str) -> None:
        acme_challenges, _client, _crypto_util, _messages = self._acme_modules()
        acme_client = self._acme_client()
        order = self._orders[order_id]

        for authorization in order.authorizations:
            for challenge in authorization.body.challenges:
                if isinstance(challenge.chall, acme_challenges.DNS01):
                    acme_client.answer_challenge(
                        challenge, challenge.chall.response(acme_client.net.key)
                    )

    def finalize(self, order_id: str, csr_der: bytes) -> IssuedCertificate:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        acme_client = self._acme_client()
        order = self._orders[order_id]

        csr = x509.load_der_x509_csr(csr_der)
        order = order.update(csr_pem=csr.public_bytes(serialization.Encoding.PEM))
        deadline = datetime.datetime.now() + datetime.timedelta(
            seconds=self.poll_timeout_seconds
        )
        finished = acme_client.poll_and_finalize(order, deadline)

        chain_pem = finished.fullchain_pem
        leaf, rest = _split_leaf_and_chain(chain_pem)
        cert = x509.load_pem_x509_certificate(leaf.encode("ascii"))
        return IssuedCertificate(
            leaf_pem=leaf,
            chain_pem=rest,
            not_after=_not_after_timestamp(cert),
            serial=format(cert.serial_number, "x"),
        )


def _split_leaf_and_chain(fullchain_pem: str) -> tuple[str, str]:
    """Split a fullchain into the leaf and everything above it."""
    marker = "-----END CERTIFICATE-----"
    parts = [p + marker for p in fullchain_pem.split(marker) if p.strip()]
    if not parts:
        raise AcmeError("bad_chain", "the CA returned no certificates")
    return parts[0].lstrip(), "".join(parts[1:]).lstrip()


def _not_after_timestamp(cert) -> float:
    """Expiry as an epoch float, across cryptography versions.

    ``not_valid_after`` is deprecated in favour of the timezone-aware
    ``not_valid_after_utc``. Reading the aware attribute when it exists
    keeps this correct on hosts that are not on UTC.
    """
    import datetime

    aware = getattr(cert, "not_valid_after_utc", None)
    if aware is not None:
        return aware.timestamp()
    naive = cert.not_valid_after  # pragma: no cover - older cryptography
    return naive.replace(tzinfo=datetime.timezone.utc).timestamp()
