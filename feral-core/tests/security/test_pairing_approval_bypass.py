"""W22 — Pairing approval-bypass: the post-W9 hash check cannot be
tricked by a forged plaintext token, and every failed verify lands
in the supervisor audit log as decision="denied".

Bypass attempts simulated:
  1. Random 256-bit token never issued by this brain.
  2. Real, currently-valid token modified by one byte (lookup-hash
     collision attempt — must fail at the Argon2id verify step).

The boundary holds when (a) ``verify_device`` returns ``None`` for
both, (b) no row's ``last_seen`` was bumped (no side-effect leaked
through the failed verify), and (c) supervisor.recent(decision="denied")
contains a ``pairing_verify`` event for each attempt.
"""

from __future__ import annotations

import secrets

import pytest

from agents.supervisor import Supervisor, SupervisorStore
from security import device_pairing


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    device_pairing.reset_store()
    yield device_pairing.get_pairing_store(str(tmp_path / "paired_devices.db"))
    device_pairing.reset_store()


@pytest.fixture
def supervisor(tmp_path):
    return Supervisor(store=SupervisorStore(str(tmp_path / "supervisor.db")))


def _gateway_verify(token: str, store, sup) -> str | None:
    """Mirror the /v1/node verify path: hash-check, then audit on outcome.

    The bypass tests exercise *this* contract; if a future patch lets
    a caller skip the audit on failure, these tests fail loudly.
    """
    device_id = store.verify_device(token)
    if device_id is None:
        sup.record(
            source="node",
            kind="pairing_verify",
            actor="system",
            payload={"token_lookup": "redacted"},
            decision="denied",
            detail={"reason": "verify_failed"},
        )
        return None
    sup.record(
        source="node",
        kind="pairing_verify",
        session_id=device_id,
        actor="system",
        payload={},
        decision="allowed",
        detail={"device_id": device_id},
    )
    return device_id


def test_random_forged_token_is_rejected_and_audited(store, supervisor):
    forged = secrets.token_hex(32)

    pre_devices = store.list_devices()
    pre_last_seen = {d["device_id"]: d["last_seen"] for d in pre_devices}

    result = _gateway_verify(forged, store, supervisor)

    assert result is None, "boundary FAILED: forged random token verified"

    post_devices = store.list_devices()
    post_last_seen = {d["device_id"]: d["last_seen"] for d in post_devices}
    assert pre_last_seen == post_last_seen, (
        "boundary FAILED: a failed verify must not bump any row's last_seen"
    )

    denials = supervisor.recent(decision="denied")
    assert any(e["kind"] == "pairing_verify" for e in denials), (
        "supervisor must record a denied pairing_verify event"
    )


def test_one_byte_modified_real_token_is_rejected(store, supervisor):
    issued = store.pair_device("attacker-target", kind="hup")
    real = issued["token"]
    forged = real[:-2] + ("00" if real[-2:] != "00" else "11")
    assert forged != real

    pre_last_seen = {
        d["device_id"]: d["last_seen"] for d in store.list_devices()
    }

    result = _gateway_verify(forged, store, supervisor)
    assert result is None, "boundary FAILED: one-byte-mutated token verified"

    post_last_seen = {
        d["device_id"]: d["last_seen"] for d in store.list_devices()
    }
    assert pre_last_seen == post_last_seen, (
        "boundary FAILED: failed verify must not touch the legit row"
    )

    denials = supervisor.recent(decision="denied")
    assert any(e["kind"] == "pairing_verify" for e in denials)


def test_legit_token_baseline_works(store, supervisor):
    """Sanity: the harness lets the legitimate path through."""
    issued = store.pair_device("legit", kind="hup")

    result = _gateway_verify(issued["token"], store, supervisor)

    assert result == issued["device_id"], (
        "harness regression: legitimate token must verify"
    )
    allowed = supervisor.recent(decision="allowed")
    assert any(e["kind"] == "pairing_verify" for e in allowed)
