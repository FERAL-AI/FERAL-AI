"""Real pairing records: dispatch checks do not rehash or refresh credentials."""
import time
from uuid import uuid4

import pytest

from security.device_pairing import DevicePairingStore, _get_backend


@pytest.fixture
def pairing(tmp_path):
    store = DevicePairingStore(db_path=str(tmp_path / "pairing.db"))
    pair = store.pair_device("fixture", mint_phone_bearer=True)
    return store, pair


def rows(store):
    conn = store._conn()
    try:
        return (list(conn.execute("SELECT * FROM paired_devices")),
                list(conn.execute("SELECT * FROM device_credentials")))
    finally:
        conn.close()


@pytest.mark.parametrize("kind,key,verify", [
    ("pair_token", "token", "verify_device"),
    ("phone_bearer", "phone_bearer", "verify_phone_bearer"),
])
def test_currentness_is_read_only_after_real_authentication(pairing, monkeypatch, kind, key, verify):
    store, pair = pairing
    assert getattr(store, verify)(pair[key]) == pair["device_id"]
    before = rows(store)

    def forbidden(*args):
        raise AssertionError("Repeated dispatch check hashed a credential")

    monkeypatch.setattr(_get_backend(), "verify", forbidden)
    for _ in range(10):
        assert store.admitted_credential_current(device_id=pair["device_id"], credential=pair[key], bearer_kind=kind)
    assert rows(store) == before
    assert not store.admitted_credential_current(device_id=str(uuid4()), credential=pair[key], bearer_kind=kind)
    assert not store.admitted_credential_current(device_id=pair["device_id"], credential="unknown", bearer_kind=kind)
    assert not store.admitted_credential_current(device_id=pair["device_id"], credential=pair[key], bearer_kind="legacy_key")


def test_removed_device_invalidates_retained_bearer(pairing):
    store, pair = pairing
    assert store.verify_phone_bearer(pair["phone_bearer"]) == pair["device_id"]
    assert store.revoke_device(pair["device_id"])
    # This covers databases retaining credential rows without FK cascading.
    conn = store._conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM device_credentials").fetchone()[0] == 1
    finally:
        conn.close()
    assert store.verify_phone_bearer(pair["phone_bearer"]) is None
    for key, kind in (("token", "pair_token"), ("phone_bearer", "phone_bearer")):
        assert not store.admitted_credential_current(device_id=pair["device_id"], credential=pair[key], bearer_kind=kind)


def test_rotation_and_expiry_are_not_extended_by_currentness(pairing):
    store, pair = pairing
    rotated = store.rotate_phone_bearer(pair["device_id"])
    assert rotated is not None
    assert not store.admitted_credential_current(device_id=pair["device_id"], credential=pair["phone_bearer"], bearer_kind="phone_bearer")
    assert store.verify_phone_bearer(rotated["phone_bearer"]) == pair["device_id"]
    conn = store._conn()
    try:
        expired = int(time.time()) - 1
        conn.execute("UPDATE paired_devices SET expires_at = ?", (expired,))
        conn.execute("UPDATE device_credentials SET expires_at = ?", (expired,))
        conn.commit()
    finally:
        conn.close()
    before = rows(store)
    assert not store.admitted_credential_current(device_id=pair["device_id"], credential=pair["token"], bearer_kind="pair_token")
    assert not store.admitted_credential_current(device_id=pair["device_id"], credential=rotated["phone_bearer"], bearer_kind="phone_bearer")
    assert rows(store) == before


@pytest.mark.parametrize("change", ["revoke", "rotate", "expire"])
def test_bearer_changed_during_password_verification_is_not_authenticated(pairing, monkeypatch, change):
    store, pair = pairing
    backend = _get_backend()
    original = backend.verify

    def verify_then_change(hash_value, secret):
        result = original(hash_value, secret)
        if change == "revoke":
            store.revoke_device(pair["device_id"])
        elif change == "rotate":
            store.rotate_phone_bearer(pair["device_id"])
        else:
            conn = store._conn()
            try:
                conn.execute("UPDATE device_credentials SET expires_at = ?", (int(time.time()) - 1,))
                conn.commit()
            finally:
                conn.close()
        return result

    monkeypatch.setattr(backend, "verify", verify_then_change)
    assert store.verify_phone_bearer(pair["phone_bearer"]) is None
