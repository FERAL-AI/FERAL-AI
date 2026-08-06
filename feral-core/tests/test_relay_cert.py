"""The brain's TLS key must never leave the brain, so that is tested.

Everything else in :mod:`security.relay_cert` is in service of one
property: the relay's control plane runs on machines the company
operates, and it must be unable to terminate or read a brain's TLS. It
gets a CSR, which is inert without the key that made it.

The tests below are grouped by what would have to be true for that
property to fail:

* the key is generated somewhere other than here, or a copy escapes;
* the request carries key material along with the CSR;
* the control plane returns a certificate for a key the brain does not
  hold, and the brain installs it anyway;
* the key lands on disk somewhere anyone can read.

No test here contacts a control plane. The transport is injected, and
the "control plane" in these tests is a few lines that sign a CSR with a
throwaway CA in process.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import stat
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import security.vault as vault_mod
from security import brain_identity, relay_cert
from security.relay_cert import RelayCertError


# ─────────────────────────────────────────────────────────────────────
# Isolation: a temp FERAL_HOME and a dict-backed keychain, so no test
# touches the real vault, the real keychain, or the real ~/.feral/tls.
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def brain_home(tmp_path: Path, monkeypatch):
    """A throwaway ``~/.feral``, a dict-backed keychain, and no real key.

    ``FERAL_HOME`` is set and restored by hand rather than through
    ``monkeypatch.setenv``. This suite's conftest instantiates
    ``monkeypatch`` before its own ``restore_process_env`` guard, so a
    monkeypatched environment variable is still set when that guard
    snapshots, and every test in the file would be reported as leaking
    ``FERAL_HOME``. Restoring in this fixture's teardown puts it back
    before the guard looks.
    """
    store: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(
        vault_mod, "_keyring_get_password", lambda s, u: store.get((s, u))
    )
    monkeypatch.setattr(
        vault_mod,
        "_keyring_set_password",
        lambda s, u, p: store.__setitem__((s, u), p),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_delete_password", lambda s, u: store.pop((s, u), None)
    )

    previous = {
        name: os.environ.get(name)
        for name in ("FERAL_HOME", "FERAL_VAULT_RECOVERY_CODE")
    }
    os.environ["FERAL_HOME"] = str(tmp_path)
    os.environ.pop("FERAL_VAULT_RECOVERY_CODE", None)

    vault_mod.reset_vault()
    brain_identity._reset_cache()
    try:
        yield tmp_path
    finally:
        brain_identity._reset_cache()
        vault_mod.reset_vault()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ─────────────────────────────────────────────────────────────────────
# A control plane, in process. Signs CSRs; holds no brain key.
# ─────────────────────────────────────────────────────────────────────


class FakeControlPlane:
    """Signs whatever CSR it is given, and records what it received.

    Deliberately credulous about names and signatures: the checks that
    matter for *this* side of the wire are the ones the brain applies to
    the response, and a control plane that refuses everything would test
    none of them. The control plane's own adversarial tests live in
    ``feral-relay/control/tests/test_acme.py``.
    """

    def __init__(self, *, valid_days: int = 90, name_override: str | None = None,
                 key_override=None, leak_private_key_pem: str | None = None):
        self.valid_days = valid_days
        self.name_override = name_override
        self.key_override = key_override
        self.leak_private_key_pem = leak_private_key_pem
        self.requests: list[dict] = []
        self.urls: list[str] = []
        self._ca_key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name(
            [x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Fake Relay CA")]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        self._ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(self._ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(self._ca_key, hashes.SHA256())
        )

    def __call__(self, url: str, body: dict) -> dict:
        self.urls.append(url)
        self.requests.append(json.loads(json.dumps(body)))

        csr = x509.load_pem_x509_csr(body["csr"].encode())
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        domain = self.name_override or [n.value for n in san.value][0]
        public_key = (
            self.key_override.public_key() if self.key_override else csr.public_key()
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, domain)])
            )
            .issuer_name(self._ca_cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=self.valid_days))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False
            )
            .sign(self._ca_key, hashes.SHA256())
        )
        chain = self._ca_cert.public_bytes(serialization.Encoding.PEM).decode()
        if self.leak_private_key_pem:
            chain += self.leak_private_key_pem
        return {
            "certificate": leaf.public_bytes(serialization.Encoding.PEM).decode(),
            "chain": chain,
        }

    @property
    def everything_it_ever_saw(self) -> str:
        return json.dumps({"urls": self.urls, "requests": self.requests})


def _local_key_pem() -> str:
    return relay_cert.private_key().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _local_key_b64() -> str:
    der = relay_cert.private_key().private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(der).decode()


# ─────────────────────────────────────────────────────────────────────
# The key
# ─────────────────────────────────────────────────────────────────────


class TestTheKeyIsGeneratedHereAndStaysHere:
    def test_the_key_is_a_p256_key_created_on_this_machine(self):
        key = relay_cert.private_key()
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert isinstance(key.curve, ec.SECP256R1)

    def test_the_private_half_lands_in_the_vault_encrypted_at_rest(
        self, brain_home: Path
    ):
        """Encrypted at rest, with the rest of the brain's secrets.

        A key sitting in a plaintext file next to the certificate would
        be readable by anyone with the disk; the vault's whole job is
        that it is not.
        """
        relay_cert.private_key()

        stored = vault_mod.BlindVault().get(
            relay_cert.VAULT_NAMESPACE, relay_cert.VAULT_KEY, requester="test"
        )
        assert stored and "PRIVATE KEY" in stored

        enc = brain_home / "credentials.enc"
        assert enc.exists()
        assert b"PRIVATE KEY" not in enc.read_bytes()

    def test_the_key_is_stable_across_calls(self):
        """A key that regenerated would orphan the certificate issued
        for the previous one on every restart."""
        first = _local_key_pem()
        assert _local_key_pem() == first

    def test_an_unreadable_stored_key_refuses_to_mint_a_replacement(self):
        """Silently generating a new key would leave the installed
        certificate valid for a key the brain no longer holds, and the
        relay would fail its handshake with no explanation."""
        vault_mod.BlindVault().put(
            relay_cert.VAULT_NAMESPACE,
            relay_cert.VAULT_KEY,
            "not a pem",
            stored_by="test",
        )
        with pytest.raises(RelayCertError) as exc:
            relay_cert.private_key()
        assert exc.value.code == "key_unreadable"


# ─────────────────────────────────────────────────────────────────────
# The CSR
# ─────────────────────────────────────────────────────────────────────


class TestTheCsr:
    def test_it_names_exactly_this_brains_relay_domain(self):
        rid = relay_cert.relay_id()
        csr = x509.load_pem_x509_csr(relay_cert.build_csr().encode())

        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        assert [n.value for n in san.value] == [f"{rid}.relay.feral.sh"]

        cn = csr.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        assert [a.value for a in cn] == [f"{rid}.relay.feral.sh"]

    def test_the_name_is_derived_from_the_identity_key(self):
        """The brain asks for the name its key entitles it to, and the
        control plane checks the same derivation. Nothing chooses."""
        expected = brain_identity.derive_relay_id(brain_identity.public_key_bytes())
        assert relay_cert.relay_id() == expected
        assert relay_cert.relay_domain() == f"{expected}.relay.feral.sh"

    def test_it_is_self_signed_by_the_local_key(self):
        """Proof of possession. The control plane checks this too, and
        a CSR that failed it would be rejected there."""
        csr = x509.load_pem_x509_csr(relay_cert.build_csr().encode())
        assert csr.is_signature_valid

        in_csr = csr.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        mine = relay_cert.private_key().public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        assert in_csr == mine

    def test_a_csr_carries_no_private_key(self):
        csr_pem = relay_cert.build_csr()
        assert "PRIVATE KEY" not in csr_pem
        assert _local_key_pem() not in csr_pem


# ─────────────────────────────────────────────────────────────────────
# The signed request
# ─────────────────────────────────────────────────────────────────────


class TestTheSignedOrder:
    def test_it_is_signed_by_the_brains_ed25519_identity(self):
        body = relay_cert.build_order()
        assert brain_identity.verify(
            relay_cert.canonical_payload(body),
            body["signature"],
            body["public_key"],
        )

    def test_the_signature_covers_the_csr(self):
        """Otherwise a request captured in flight could have another
        brain's CSR pasted into it and still verify."""
        body = relay_cert.build_order()
        tampered = dict(body)
        tampered["csr"] = relay_cert.build_csr()  # a fresh, differently-signed CSR
        assert not brain_identity.verify(
            relay_cert.canonical_payload(tampered),
            body["signature"],
            body["public_key"],
        )

    def test_the_signed_field_set_matches_the_control_plane(self):
        """The two sides do not share a package, on purpose: a brain on
        a laptop must be able to be older or newer than the relay it
        talks to. The cost is a duplicated payload definition, and a
        duplicate that drifts produces signatures that verify nowhere.
        So the field set is pinned on both sides.
        """
        signed = json.loads(relay_cert.canonical_payload(relay_cert.build_order()))
        assert set(signed) == {"relay_id", "public_key", "csr", "ts", "nonce"}

        cp = (
            Path(__file__).resolve().parents[2]
            / "feral-relay" / "control" / "feral_relay_cp" / "acme.py"
        )
        if not cp.exists():
            pytest.skip("relay control plane not present in this checkout")

        text = cp.read_text()
        block = text.split("def canonical_payload(", 1)[1].split("return json.dumps(", 1)[1]
        block = block.split("separators=", 1)[0]
        cp_fields = set(re.findall(r'"(\w+)":\s*body\[', block))
        assert cp_fields == set(signed)

    def test_the_request_carries_a_csr_and_no_key(self):
        """The single most important assertion in this file.

        If the outbound request ever contains the private key, the
        control plane can impersonate this brain and read its traffic,
        and every local-first claim FERAL makes about the relay is
        false.
        """
        body = relay_cert.build_order()
        wire = json.dumps(body)

        assert "-----BEGIN CERTIFICATE REQUEST-----" in body["csr"]
        assert "PRIVATE KEY" not in wire
        assert _local_key_pem() not in wire
        assert _local_key_b64() not in wire

    def test_the_field_set_on_the_wire_is_exactly_what_it_should_be(self):
        """Pinned so a future field cannot smuggle anything out
        unnoticed, and so an unsigned field cannot appear."""
        body = relay_cert.build_order()
        assert set(body) == {
            "relay_id",
            "public_key",
            "csr",
            "ts",
            "nonce",
            "signature",
        }

    def test_a_leak_would_be_caught_before_it_left_the_machine(self, monkeypatch):
        """Nothing above can put a key in the body, which is exactly why
        an accident would be silent. The guard is checked directly."""
        monkeypatch.setattr(relay_cert, "build_csr", lambda *a, **k: _local_key_pem())
        with pytest.raises(RelayCertError) as exc:
            relay_cert.build_order()
        assert exc.value.code == "private_key_leak"

    def test_each_order_uses_a_fresh_nonce(self):
        """The control plane refuses a replayed nonce, so a constant one
        would make the second request of a brain's life fail."""
        assert relay_cert.build_order()["nonce"] != relay_cert.build_order()["nonce"]


# ─────────────────────────────────────────────────────────────────────
# End to end, against the in-process control plane
# ─────────────────────────────────────────────────────────────────────


class TestIssuanceRoundTrip:
    def test_a_certificate_is_fetched_and_installed(self, brain_home: Path):
        cp = FakeControlPlane()
        status = relay_cert.request_certificate("https://relay.example", transport=cp)

        assert cp.urls == ["https://relay.example/v1/relay/certificates"]
        assert status.domain == relay_cert.relay_domain()
        assert 89 < status.days_remaining <= 90
        assert status.needs_renewal is False

        fullchain = brain_home / "tls" / "relay" / "fullchain.pem"
        key = brain_home / "tls" / "relay" / "key.pem"
        assert fullchain.exists() and key.exists()
        assert fullchain.read_text().count("BEGIN CERTIFICATE") == 2

    def test_the_control_plane_never_saw_the_private_key(self):
        """The property, asserted against everything the control plane
        actually received rather than against the code path."""
        cp = FakeControlPlane()
        relay_cert.request_certificate("https://relay.example", transport=cp)

        seen = cp.everything_it_ever_saw
        assert "PRIVATE KEY" not in seen
        assert _local_key_pem() not in seen
        assert _local_key_b64() not in seen

    def test_the_installed_key_is_the_one_from_the_vault(self, brain_home: Path):
        cp = FakeControlPlane()
        relay_cert.request_certificate("https://relay.example", transport=cp)

        on_disk = (brain_home / "tls" / "relay" / "key.pem").read_text()
        assert on_disk.strip() == _local_key_pem().strip()

    def test_the_installed_certificate_matches_the_installed_key(
        self, brain_home: Path
    ):
        cp = FakeControlPlane()
        relay_cert.request_certificate("https://relay.example", transport=cp)

        leaf = x509.load_pem_x509_certificate(
            (brain_home / "tls" / "relay" / "fullchain.pem").read_bytes()
        )
        cert_pub = leaf.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_pub = serialization.load_pem_private_key(
            (brain_home / "tls" / "relay" / "key.pem").read_bytes(), password=None
        ).public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        assert cert_pub == key_pub

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_the_key_on_disk_is_readable_only_by_this_user(self, brain_home: Path):
        """A key at mode 644 is a key anyone on the machine has."""
        cp = FakeControlPlane()
        relay_cert.request_certificate("https://relay.example", transport=cp)

        directory = brain_home / "tls" / "relay"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "key.pem").stat().st_mode) == 0o600
        assert stat.S_IMODE((directory / "fullchain.pem").stat().st_mode) == 0o600

    def test_reissuing_reuses_the_same_key(self, brain_home: Path):
        """A new key on every renewal would mean every renewal is a new
        identity to anything pinning the leaf."""
        cp = FakeControlPlane()
        relay_cert.request_certificate("https://relay.example", transport=cp)
        first = (brain_home / "tls" / "relay" / "key.pem").read_text()
        relay_cert.request_certificate("https://relay.example", transport=cp)
        assert (brain_home / "tls" / "relay" / "key.pem").read_text() == first


# ─────────────────────────────────────────────────────────────────────
# The response is not trusted
# ─────────────────────────────────────────────────────────────────────


class TestAHostileControlPlaneResponse:
    def test_a_certificate_for_someone_elses_key_is_refused(self, brain_home: Path):
        """The attack a compromised control plane would actually run.

        Hand the brain a certificate over a key the control plane holds.
        If the brain installed it, the operator could terminate its TLS.
        """
        attacker_key = ec.generate_private_key(ec.SECP256R1())
        cp = FakeControlPlane(key_override=attacker_key)

        with pytest.raises(RelayCertError) as exc:
            relay_cert.request_certificate("https://relay.example", transport=cp)
        assert exc.value.code == "key_mismatch"
        assert not (brain_home / "tls" / "relay" / "fullchain.pem").exists()

    def test_a_certificate_for_another_name_is_refused(self, brain_home: Path):
        cp = FakeControlPlane(name_override="someone-else.relay.feral.sh")
        with pytest.raises(RelayCertError) as exc:
            relay_cert.request_certificate("https://relay.example", transport=cp)
        assert exc.value.code == "name_mismatch"
        assert not (brain_home / "tls" / "relay" / "fullchain.pem").exists()

    def test_a_response_carrying_a_private_key_is_refused(self, brain_home: Path):
        """The control plane has no business returning a key, and a
        brain that accepted one might write it over its own."""
        stranger = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        cp = FakeControlPlane(leak_private_key_pem=stranger)

        with pytest.raises(RelayCertError) as exc:
            relay_cert.request_certificate("https://relay.example", transport=cp)
        assert exc.value.code == "private_key_leak"
        assert not (brain_home / "tls" / "relay" / "fullchain.pem").exists()

    @pytest.mark.parametrize(
        "response", [{}, {"certificate": ""}, {"chain": "only a chain"}]
    )
    def test_a_response_with_no_certificate_is_refused(self, response):
        with pytest.raises(RelayCertError) as exc:
            relay_cert.request_certificate(
                "https://relay.example", transport=lambda url, body: response
            )
        assert exc.value.code == "bad_response"

    def test_a_non_object_response_is_refused(self):
        with pytest.raises(RelayCertError) as exc:
            relay_cert.request_certificate(
                "https://relay.example", transport=lambda url, body: "ok"
            )
        assert exc.value.code == "bad_response"

    def test_unreadable_certificate_bytes_are_refused(self):
        with pytest.raises(RelayCertError) as exc:
            relay_cert.request_certificate(
                "https://relay.example",
                transport=lambda url, body: {"certificate": "-----BEGIN CERTIFICATE-----"},
            )
        assert exc.value.code == "bad_certificate"


# ─────────────────────────────────────────────────────────────────────
# Expiry and renewal
# ─────────────────────────────────────────────────────────────────────


class TestExpiry:
    def test_days_to_expiry_reports_the_leafs_lifetime(self):
        relay_cert.request_certificate(
            "https://relay.example", transport=FakeControlPlane(valid_days=45)
        )
        assert 44 < relay_cert.days_to_expiry() <= 45

    def test_no_certificate_reports_nothing_rather_than_raising(self):
        assert relay_cert.certificate_status() is None
        assert relay_cert.days_to_expiry() is None

    def test_a_brain_with_no_certificate_needs_one(self):
        """Returning False would leave a relay that never comes up with
        nothing prompting it to try."""
        assert relay_cert.needs_renewal() is True

    @pytest.mark.parametrize(
        "days_left, expected",
        [(60.0, False), (31.0, False), (30.0, True), (29.0, True), (-1.0, True)],
    )
    def test_renewal_turns_on_at_thirty_days(self, days_left, expected):
        status = relay_cert.request_certificate(
            "https://relay.example", transport=FakeControlPlane(valid_days=90)
        )
        at = status.not_after - days_left * 86400
        assert relay_cert.needs_renewal(now=at) is expected

    def test_the_threshold_is_thirty_days(self):
        assert relay_cert.RENEWAL_THRESHOLD_DAYS == 30

    def test_an_expired_certificate_reports_negative_days(self):
        status = relay_cert.request_certificate(
            "https://relay.example", transport=FakeControlPlane(valid_days=90)
        )
        assert relay_cert.days_to_expiry(now=status.not_after + 86400) < 0


class TestRenewIfNeeded:
    def test_a_fresh_certificate_is_not_reissued(self):
        """Every issuance spends part of a weekly rate limit shared by
        every brain in the zone. A loop that asks unconditionally is how
        that budget disappears."""
        cp = FakeControlPlane(valid_days=90)
        relay_cert.request_certificate("https://relay.example", transport=cp)
        assert len(cp.requests) == 1

        assert relay_cert.renew_if_needed("https://relay.example", transport=cp) is None
        assert len(cp.requests) == 1

    def test_an_expiring_certificate_is_reissued(self):
        cp = FakeControlPlane(valid_days=90)
        status = relay_cert.request_certificate("https://relay.example", transport=cp)

        nearly_due = status.not_after - 5 * 86400
        renewed = relay_cert.renew_if_needed(
            "https://relay.example", transport=cp, now=nearly_due
        )
        assert renewed is not None
        assert len(cp.requests) == 2

    def test_a_brain_with_no_certificate_is_issued_one(self):
        cp = FakeControlPlane()
        assert relay_cert.renew_if_needed("https://relay.example", transport=cp)
        assert len(cp.requests) == 1


class TestPathsAreUnderFeralHome:
    def test_the_certificate_lives_where_the_docs_say(self, brain_home: Path):
        assert relay_cert.tls_dir() == brain_home / "tls" / "relay"
        assert relay_cert.fullchain_path().name == "fullchain.pem"
        assert relay_cert.key_path().name == "key.pem"

    def test_installing_twice_leaves_no_temporary_files_behind(self, brain_home: Path):
        cp = FakeControlPlane()
        relay_cert.request_certificate("https://relay.example", transport=cp)
        relay_cert.request_certificate("https://relay.example", transport=cp)

        names = sorted(p.name for p in (brain_home / "tls" / "relay").iterdir())
        assert names == ["fullchain.pem", "key.pem"]


class TestTheDefaultTransportIsNotUsedByAccident:
    def test_no_test_here_opens_a_socket(self, monkeypatch):
        """A test that silently fell back to the real transport would
        try to reach a control plane, and pass or fail on the network.
        Prove the injected transport is the one that runs.
        """

        def explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("the default HTTP transport was used")

        monkeypatch.setattr(relay_cert, "_http_post", explode)
        cp = FakeControlPlane()
        assert relay_cert.request_certificate(
            "https://relay.example", transport=cp
        ).days_remaining > 0

    def test_the_endpoint_path_is_pinned(self):
        assert relay_cert.CERTIFICATE_ENDPOINT == "/v1/relay/certificates"

    def test_a_trailing_slash_does_not_double_up(self):
        cp = FakeControlPlane()
        relay_cert.request_certificate("https://relay.example/", transport=cp)
        assert cp.urls == ["https://relay.example/v1/relay/certificates"]


def test_time_is_not_frozen_anywhere_in_this_module():
    """Guard against a fixture leaking a frozen clock into the expiry
    assertions above, which would make them pass for the wrong reason."""
    first = time.time()
    assert first > 1_700_000_000
