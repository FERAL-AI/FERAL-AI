"""The relay's TLS certificate, obtained without giving away the key.

FERAL's local-first claim survives the relay only if one thing is true:
**the brain's TLS private key never leaves the brain.** The relay's
control plane runs on infrastructure the company operates. If it held
the key that terminates a brain's TLS, it could read the traffic, and
"your data stays on your machine" would be marketing rather than
architecture.

So the split here is not a convenience, it is the property:

* this module generates a P-256 keypair **on the brain** and puts the
  private half in the BlindVault, encrypted at rest alongside the rest
  of the brain's secrets;
* it sends the control plane a **CSR**, which is a public key plus a
  name plus a self-signature. A CSR is inert: whoever holds it cannot
  serve traffic with it, cannot decrypt anything, and cannot derive the
  key that made it;
* the control plane runs the ACME order and returns a signed
  certificate. A full compromise of that service yields certificates
  nobody can use.

The request is signed with the brain's Ed25519 identity
(:mod:`security.brain_identity`) so the control plane knows who is
asking, and the name in the CSR is ``<relay_id>.relay.feral.sh``, which
is derived from that same identity key. The control plane checks the two
agree; this side never asks for a name it is not entitled to, so a
mismatch here would be a bug rather than an attack.

What lands on disk
------------------
``~/.feral/tls/relay/fullchain.pem`` and ``~/.feral/tls/relay/key.pem``,
both mode 600 in a mode 700 directory, because a TLS terminator needs
files rather than a vault lookup. The vault copy stays authoritative: it
is what survives a wiped ``~/.feral/tls`` and what is encrypted at rest.

Renewal
-------
Certificates are short-lived by design. :func:`needs_renewal` turns true
at :data:`RENEWAL_THRESHOLD_DAYS` days remaining, which leaves a wide
margin over a 90-day lifetime for a brain that is asleep, offline, or
behind a rate-limited control plane.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("feral.security.relay_cert")

#: The zone the control plane is authoritative for.
RELAY_BASE_DOMAIN = "relay.feral.sh"

#: Where the TLS key lives. A separate namespace from the Ed25519
#: identity so that a caller reaching for one cannot accidentally read
#: the other.
VAULT_NAMESPACE = "relay_tls"
VAULT_KEY = "p256_private_key_pem"

#: Renew with this many days left. Well clear of a 90-day lifetime, so a
#: laptop that has been shut for three weeks still has room, and so a
#: control plane that is rate limited has time to work through a queue.
RENEWAL_THRESHOLD_DAYS = 30

#: Where the control plane accepts orders.
CERTIFICATE_ENDPOINT = "/v1/relay/certificates"

_DEFAULT_TIMEOUT_SECONDS = 30.0

_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

#: The response callable: ``(url, body) -> parsed json``. Injected so
#: tests exercise the whole flow without a network, and so a brain
#: behind a proxy can supply its own transport.
Transport = Callable[[str, dict], dict]


class RelayCertError(RuntimeError):
    """A certificate operation that must not proceed, and why.

    Carries a code for the same reason registration's error does: the
    caller (a CLI, a status endpoint) needs to distinguish "not issued
    yet" from "the control plane returned something wrong" without
    matching on prose.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class CertificateStatus:
    """What the brain knows about its own certificate.

    ``days_remaining`` is a float rather than an int so a status surface
    can show hours near the end without a second calculation, and so
    ``needs_renewal`` does not flip a day early from truncation.
    """

    domain: str
    not_after: float
    days_remaining: float
    needs_renewal: bool
    fullchain_path: str
    key_path: str


# ─────────────────────────────────────────────────────────────────────
# Lazy imports, matching brain_identity's shape
# ─────────────────────────────────────────────────────────────────────


def _crypto():
    """Import ``cryptography`` lazily so a brain without it still boots."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RelayCertError(
            "cryptography_missing",
            "the `cryptography` package is required for relay certificates",
        ) from exc
    return x509, hashes, serialization, ec


def _vault():
    from security.vault import BlindVault

    return BlindVault()


def _feral_home() -> Path:
    try:
        from config.loader import feral_home

        return feral_home()
    except Exception:  # pragma: no cover - config is always present in a brain
        env = os.environ.get("FERAL_HOME")
        return Path(env) if env else Path.home() / ".feral"


def tls_dir() -> Path:
    return _feral_home() / "tls" / "relay"


def fullchain_path() -> Path:
    return tls_dir() / "fullchain.pem"


def key_path() -> Path:
    return tls_dir() / "key.pem"


def relay_id() -> str:
    """This brain's relay id, derived from its identity key.

    Derived rather than read from settings on purpose: the persisted
    ``meta.relay_id`` is a cache, and a certificate must be requested for
    the name the key actually entitles this brain to, not for whatever a
    settings file last happened to say.
    """
    from security.brain_identity import derive_relay_id, public_key_bytes

    return derive_relay_id(public_key_bytes())


def relay_domain(rid: Optional[str] = None) -> str:
    return f"{rid or relay_id()}.{RELAY_BASE_DOMAIN}"


# ─────────────────────────────────────────────────────────────────────
# The key, which does not travel
# ─────────────────────────────────────────────────────────────────────


def _load_private_key():
    """Return the stored TLS key, or None when there is not one."""
    _x509, _hashes, serialization, _ec = _crypto()
    try:
        stored = _vault().get(VAULT_NAMESPACE, VAULT_KEY, requester="relay_cert")
    except Exception as exc:
        raise RelayCertError("vault_unavailable", f"vault unavailable: {exc}") from exc
    if not stored:
        return None
    try:
        return serialization.load_pem_private_key(stored.encode("utf-8"), password=None)
    except Exception as exc:
        raise RelayCertError(
            "key_unreadable",
            "the stored relay TLS key is unreadable. Refusing to mint a "
            "replacement, because the certificate on disk was issued for "
            "the old key and would silently stop matching it.",
        ) from exc


def _create_private_key():
    """Generate a P-256 key and persist it. First issuance only.

    Generated here, on the brain. This function is the reason the
    control plane cannot impersonate a relay: the only copy of this key
    is in this machine's vault.
    """
    _x509, _hashes, serialization, ec = _crypto()
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    try:
        _vault().put(VAULT_NAMESPACE, VAULT_KEY, pem, stored_by="relay_cert")
    except Exception as exc:
        raise RelayCertError(
            "vault_unavailable", f"could not persist the relay TLS key: {exc}"
        ) from exc
    logger.info("relay_cert: generated a new P-256 TLS key (private half stays local)")
    return key


def private_key():
    """The brain's relay TLS key, creating one on first use."""
    return _load_private_key() or _create_private_key()


def public_key_pem() -> str:
    """The public half. Safe to send anywhere; sent nowhere on its own,
    since the CSR already carries it."""
    _x509, _hashes, serialization, _ec = _crypto()
    return private_key().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


# ─────────────────────────────────────────────────────────────────────
# The CSR, which does travel
# ─────────────────────────────────────────────────────────────────────


def build_csr(rid: Optional[str] = None, key=None) -> str:
    """A CSR for this brain's name, and only for this brain's name.

    Exactly one DNS name, matching the common name. The control plane
    refuses anything else, and asking for more than we are entitled to
    would be a bug worth failing on here rather than a request worth
    sending.
    """
    x509, hashes, serialization, _ec = _crypto()
    domain = relay_domain(rid)
    key = key or private_key()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, domain)])
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def canonical_payload(body: dict) -> bytes:
    """The exact bytes signed for a certificate order.

    Must stay byte-identical to ``feral_relay_cp.acme.canonical_payload``
    on the control plane. The duplication is deliberate for the same
    reason the relay_id derivation is duplicated: a brain on a user's
    laptop must be able to be older or newer than the relay it talks to,
    so the two sides do not share a package. Tests on both sides pin the
    signed field set, so a change to one that is not made to the other
    fails loudly instead of producing signatures that verify nowhere.
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


def build_order(rid: Optional[str] = None, *, now: Optional[float] = None) -> dict:
    """The signed request body. Contains a CSR and no key.

    Built as its own function so a test can assert on exactly what would
    go over the wire without a transport in the way.
    """
    from security import brain_identity

    rid = rid or relay_id()
    body = {
        "relay_id": rid,
        "public_key": brain_identity.public_key_b64(),
        "csr": build_csr(rid),
        "ts": time.time() if now is None else now,
        "nonce": secrets.token_hex(16),
    }
    body["signature"] = brain_identity.sign(canonical_payload(body))

    # The invariant, checked at the one place it could be violated.
    # Nothing above can put a key here, and that is exactly why an
    # accident would be silent without this.
    _assert_no_private_key(json.dumps(body), "the outbound certificate request")
    return body


def _assert_no_private_key(text: str, what: str) -> None:
    if _PRIVATE_KEY_MARKER.search(text or ""):
        raise RelayCertError(
            "private_key_leak",
            f"{what} contains private key material. Refusing to continue: "
            f"the brain's TLS key must never leave this machine.",
        )


# ─────────────────────────────────────────────────────────────────────
# Talking to the control plane
# ─────────────────────────────────────────────────────────────────────


def _http_post(url: str, body: dict) -> dict:
    """Default transport. Plain stdlib, no new dependency."""
    import urllib.error
    import urllib.request

    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_DEFAULT_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RelayCertError(
            "control_plane_refused",
            f"the control plane returned {exc.code}: {detail}",
        ) from exc
    except Exception as exc:
        raise RelayCertError(
            "control_plane_unreachable", f"could not reach the control plane: {exc}"
        ) from exc


def request_certificate(
    control_plane_url: str,
    *,
    transport: Optional[Transport] = None,
    install: bool = True,
    now: Optional[float] = None,
) -> CertificateStatus:
    """Ask the control plane to sign this brain's CSR.

    Everything the control plane returns is checked before it is
    trusted, because a certificate that does not match the local key is
    either a bug or an attempt to get this brain to serve a key someone
    else holds:

    * no private key material in the response, ever;
    * the leaf's public key is the one in this brain's vault;
    * the leaf is valid for this brain's name and no other.
    """
    post = transport or _http_post
    rid = relay_id()
    body = build_order(rid, now=now)

    url = control_plane_url.rstrip("/") + CERTIFICATE_ENDPOINT
    response = post(url, body)
    if not isinstance(response, dict):
        raise RelayCertError(
            "bad_response", "the control plane returned a non-object response"
        )

    leaf_pem = (response.get("certificate") or response.get("leaf_pem") or "").strip()
    chain_pem = (response.get("chain") or response.get("chain_pem") or "").strip()
    if not leaf_pem:
        raise RelayCertError("bad_response", "the response carried no certificate")

    _assert_no_private_key(leaf_pem, "the issued certificate")
    _assert_no_private_key(chain_pem, "the issued chain")

    _verify_leaf_matches_local_key(leaf_pem)
    _verify_leaf_is_for(leaf_pem, relay_domain(rid))

    if not install:
        return _status_from_pem(leaf_pem, relay_domain(rid))

    return install_certificate(leaf_pem, chain_pem, rid=rid)


def _verify_leaf_matches_local_key(leaf_pem: str) -> None:
    """A certificate for a key we do not hold is worse than none.

    It would not work, and if it did it would mean somebody else's key
    is being presented as this brain's.
    """
    x509, _hashes, serialization, _ec = _crypto()
    try:
        leaf = x509.load_pem_x509_certificate(leaf_pem.encode("utf-8"))
    except Exception as exc:
        raise RelayCertError("bad_certificate", f"unreadable certificate: {exc}") from exc

    issued = leaf.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    mine = private_key().public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if issued != mine:
        raise RelayCertError(
            "key_mismatch",
            "the issued certificate is for a different public key than the "
            "one in this brain's vault. Refusing to install it.",
        )


def _verify_leaf_is_for(leaf_pem: str, domain: str) -> None:
    x509, _hashes, _serialization, _ec = _crypto()
    leaf = x509.load_pem_x509_certificate(leaf_pem.encode("utf-8"))
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = [n.value for n in san.value if isinstance(n, x509.DNSName)]
    except Exception:
        names = []
    if names != [domain]:
        raise RelayCertError(
            "name_mismatch",
            f"the issued certificate is valid for {names!r}, not for "
            f"{domain!r} alone. Refusing to install it.",
        )


# ─────────────────────────────────────────────────────────────────────
# Installing
# ─────────────────────────────────────────────────────────────────────


def install_certificate(
    leaf_pem: str, chain_pem: str = "", *, rid: Optional[str] = None
) -> CertificateStatus:
    """Write the certificate and key where a TLS terminator can read
    them, and nowhere anyone else can.

    Mode 600 on both files inside a mode 700 directory. ``key.pem`` is a
    second copy of what is already in the vault; the vault copy is
    authoritative and encrypted at rest, this one exists because TLS
    servers take paths.
    """
    _x509, _hashes, serialization, _ec = _crypto()
    _assert_no_private_key(leaf_pem, "the certificate being installed")
    _assert_no_private_key(chain_pem, "the chain being installed")

    rid = rid or relay_id()
    directory = tls_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError as exc:  # pragma: no cover - platform dependent
        logger.warning("relay_cert: could not chmod %s: %s", directory, exc)

    fullchain = leaf_pem.strip() + "\n"
    if chain_pem.strip():
        fullchain += chain_pem.strip() + "\n"

    key_pem = private_key().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    _write_private(fullchain_path(), fullchain)
    _write_private(key_path(), key_pem)

    logger.info("relay_cert: installed certificate for %s", relay_domain(rid))
    return _status_from_pem(leaf_pem, relay_domain(rid))


def _write_private(path: Path, text: str) -> None:
    """Write mode 600, and be mode 600 before there is content in it.

    Creating the file with the restrictive mode rather than chmod-ing
    afterwards closes the window where a secret is world-readable on
    disk.
    """
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ─────────────────────────────────────────────────────────────────────
# Expiry
# ─────────────────────────────────────────────────────────────────────


def _not_after_timestamp(cert) -> float:
    """Expiry as an epoch float, across cryptography versions.

    ``not_valid_after`` is deprecated in favour of the timezone-aware
    ``not_valid_after_utc``; reading the aware attribute when present
    keeps this right on a host that is not on UTC.
    """
    import datetime

    aware = getattr(cert, "not_valid_after_utc", None)
    if aware is not None:
        return aware.timestamp()
    naive = cert.not_valid_after  # pragma: no cover - older cryptography
    return naive.replace(tzinfo=datetime.timezone.utc).timestamp()


def _status_from_pem(
    leaf_pem: str, domain: str, *, now: Optional[float] = None
) -> CertificateStatus:
    x509, _hashes, _serialization, _ec = _crypto()
    now = time.time() if now is None else now
    try:
        leaf = x509.load_pem_x509_certificate(leaf_pem.encode("utf-8"))
    except Exception as exc:
        raise RelayCertError("bad_certificate", f"unreadable certificate: {exc}") from exc

    not_after = _not_after_timestamp(leaf)
    days = (not_after - now) / 86400.0
    return CertificateStatus(
        domain=domain,
        not_after=not_after,
        days_remaining=days,
        needs_renewal=days <= RENEWAL_THRESHOLD_DAYS,
        fullchain_path=str(fullchain_path()),
        key_path=str(key_path()),
    )


def certificate_status(*, now: Optional[float] = None) -> Optional[CertificateStatus]:
    """The installed certificate's status, or None when there is none.

    ``None`` rather than an exception, because "no certificate yet" is
    the ordinary state of a brain that has not joined a relay, and a
    status surface should not have to catch to render it.
    """
    path = fullchain_path()
    if not path.exists():
        return None
    return _status_from_pem(path.read_text(), relay_domain(), now=now)


def days_to_expiry(*, now: Optional[float] = None) -> Optional[float]:
    """Days until the installed certificate expires. Negative if it
    already has, so a caller can tell "expiring" from "expired"."""
    status = certificate_status(now=now)
    return None if status is None else status.days_remaining


def needs_renewal(*, now: Optional[float] = None) -> bool:
    """True at :data:`RENEWAL_THRESHOLD_DAYS` days remaining.

    Also true when there is no certificate at all: a brain that has
    never been issued one needs the same action a brain with an expiring
    one needs, and returning False would leave a relay that never comes
    up with nothing prompting it.
    """
    status = certificate_status(now=now)
    return True if status is None else status.needs_renewal


def renew_if_needed(
    control_plane_url: str,
    *,
    transport: Optional[Transport] = None,
    now: Optional[float] = None,
) -> Optional[CertificateStatus]:
    """Request a certificate only when one is actually due.

    Returns the new status, or ``None`` when nothing needed doing. Every
    issuance spends part of a weekly rate limit shared by every brain
    under ``relay.feral.sh``, so a renewal loop that asks unconditionally
    is not merely wasteful, it is how a zone runs out of certificates.
    """
    if not needs_renewal(now=now):
        return None
    return request_certificate(control_plane_url, transport=transport, now=now)


def _reset_for_tests() -> None:
    """No module-level cache to clear today. Present so a future one has
    an obvious place to be cleared from, matching brain_identity."""
    return None


__all__ = [
    "CERTIFICATE_ENDPOINT",
    "RELAY_BASE_DOMAIN",
    "RENEWAL_THRESHOLD_DAYS",
    "VAULT_KEY",
    "VAULT_NAMESPACE",
    "CertificateStatus",
    "RelayCertError",
    "build_csr",
    "build_order",
    "canonical_payload",
    "certificate_status",
    "days_to_expiry",
    "fullchain_path",
    "install_certificate",
    "key_path",
    "needs_renewal",
    "private_key",
    "public_key_pem",
    "relay_domain",
    "relay_id",
    "renew_if_needed",
    "request_certificate",
    "tls_dir",
]
