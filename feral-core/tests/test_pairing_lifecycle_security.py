"""Pairing lifecycle + open-path hardening contracts.

Four orthogonal contracts are pinned here, each with a real route /
store call (not a MagicMock that pretends success):

1. ``GET /api/devices/paired`` excludes unclaimed rows by default.
2. ``GET /api/devices/paired?include_unclaimed=true`` includes them.
3. ``DevicePairingStore.verify_device`` — the codepath the
   ``/v1/node`` and ``/v1/session`` WebSocket handshakes use to
   authenticate an attaching device — sets ``claimed_at`` if the
   row was previously unclaimed (idempotent: a second verify on
   an already-claimed row preserves the original timestamp).
4. The token-issuance endpoints ``/api/devices/pair/url`` and
   ``/api/devices/pair/qr`` are no longer in the unauthenticated
   open-path allowlist — a non-loopback client without a Bearer
   token gets 401, while the phone-side claim flow
   (``/pair/check``, ``/pair/verify_pin``, ``/pair/complete``)
   stays open so a brand-new phone with no API key can finish
   pairing.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


# ──────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Real DevicePairingStore against an isolated tmp DB."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    from security.device_pairing import DevicePairingStore
    return DevicePairingStore(db_path=str(tmp_path / "pairs.db"))


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    """FastAPI TestClient wired to the real DevicePairingStore.

    ``conftest.py`` already monkeypatches ``is_localhost`` to treat
    the ``testclient`` host as loopback so the API-key middleware
    doesn't 401 every request — that's the right default for most
    route tests. Tests in this file that explicitly want to hit the
    *non-loopback* branch use the ``non_loopback_client`` fixture
    below instead.
    """
    from config.loader import ConfigLoader

    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover()
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr(
        "api.routes.devices._detect_lan_ip", lambda: "192.168.50.9"
    )

    state_mock = MagicMock()
    state_mock.config = config
    state_mock.device_pairing_store = store
    with patch("api.state.state", state_mock), patch("api.routes.devices.state", state_mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def non_loopback_client(tmp_path, monkeypatch, store):
    """Override ``is_localhost`` back to the strict definition so the
    API-key middleware exercises the *non*-loopback branch. Mirrors
    the helper in ``test_a4_pair_middleware.py``.

    Also wires a config with ``pairing_mode=local`` so the pairing
    routes are *exposed* (otherwise they short-circuit with the Mode-B
    "pairing not available" payload and the auth middleware never
    runs).
    """
    from security import session_auth as _sa
    from config.loader import ConfigLoader

    def _strict(host):
        return host in ("127.0.0.1", "::1", "localhost")

    monkeypatch.setattr(_sa, "is_localhost", _strict)
    try:
        import api.server as _server
        monkeypatch.setattr(_server, "is_localhost", _strict, raising=False)
    except Exception:
        pass

    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover()
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr(
        "api.routes.devices._detect_lan_ip", lambda: "192.168.50.9"
    )

    state_mock = MagicMock()
    state_mock.config = config
    state_mock.device_pairing_store = store
    with patch("api.state.state", state_mock), patch("api.routes.devices.state", state_mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


# ──────────────────────────────────────────────────────────────────
# 1+2. /api/devices/paired claim filter
# ──────────────────────────────────────────────────────────────────


def test_paired_default_excludes_unclaimed_rows(client, store):
    # Mint two pair tokens. Neither has been claimed (no daemon
    # attached, no /pair/complete call) so both rows are unclaimed.
    issued_a = store.pair_device("phantom-a", kind="browser")
    issued_b = store.pair_device("phantom-b", kind="name")

    # Sanity: both rows exist when we ask the store directly.
    all_rows = store.list_devices()
    ids = {r["device_id"] for r in all_rows}
    assert issued_a["device_id"] in ids
    assert issued_b["device_id"] in ids

    # Default user-facing route hides them.
    r = client.get("/api/devices/paired")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["devices"] == []


def test_paired_default_includes_claimed_rows(client, store):
    # Mint two: one we'll claim, one we won't.
    claimed = store.pair_device("real-device", kind="hup", node_id="node-1")
    unclaimed = store.pair_device("never-attached", kind="browser")

    # Mark the first as claimed via the same path /pair/complete uses.
    assert store.mark_claimed(claimed["token"]) == claimed["device_id"]

    r = client.get("/api/devices/paired")
    assert r.status_code == 200, r.text
    devices = r.json()["devices"]
    assert len(devices) == 1
    row = devices[0]
    assert row["device_id"] == claimed["device_id"]
    assert row["claimed_at"] is not None
    # Payload-shape regression: every key the v1/v2 client reads must
    # still be present on the row.
    for key in (
        "device_id", "name", "paired_at", "last_seen",
        "kind", "node_id", "claimed_at", "platform", "capabilities",
    ):
        assert key in row, f"missing payload key: {key}"
    # The unclaimed row must NOT leak via the default view.
    assert all(d["device_id"] != unclaimed["device_id"] for d in devices)


def test_paired_include_unclaimed_query_returns_all_rows(client, store):
    claimed = store.pair_device("attached", kind="hup", node_id="node-2")
    store.mark_claimed(claimed["token"])
    unclaimed = store.pair_device("phantom", kind="browser")

    r = client.get("/api/devices/paired?include_unclaimed=true")
    assert r.status_code == 200, r.text
    devices = r.json()["devices"]
    ids = {d["device_id"] for d in devices}
    assert claimed["device_id"] in ids
    assert unclaimed["device_id"] in ids
    # Exactly the two we minted, nothing fabricated.
    assert len(devices) == 2


def test_paired_include_unclaimed_false_query_matches_default(client, store):
    """``?include_unclaimed=false`` is explicit-form of the default."""
    claimed = store.pair_device("attached", kind="hup", node_id="node-3")
    store.mark_claimed(claimed["token"])
    store.pair_device("phantom", kind="browser")

    default = client.get("/api/devices/paired").json()
    explicit = client.get("/api/devices/paired?include_unclaimed=false").json()
    assert default == explicit
    assert len(default["devices"]) == 1
    assert default["devices"][0]["device_id"] == claimed["device_id"]


# ──────────────────────────────────────────────────────────────────
# 3. verify_device sets claimed_at idempotently
# ──────────────────────────────────────────────────────────────────


def test_verify_device_marks_claim_on_first_attach(store):
    """The codepath the WebSocket handshake (``/v1/node``) uses must
    flip ``claimed_at`` on first verify, so a daemon that authenticates
    via the WS handshake (and never POSTs ``/pair/complete``) is still
    treated as a real claim by the user-facing devices list.
    """
    issued = store.pair_device("daemon-a", kind="hup", node_id="node-a")
    device_id = issued["device_id"]

    # Pre-condition: row is unclaimed.
    pre = [r for r in store.list_devices() if r["device_id"] == device_id][0]
    assert pre["claimed_at"] is None

    # The codepath /v1/node hits via ``_verify_credential``.
    assert store.verify_device(issued["token"]) == device_id

    # Post-condition: claimed_at is now set, and the row shows up in
    # the default paired view.
    post = [r for r in store.list_devices() if r["device_id"] == device_id][0]
    assert post["claimed_at"] is not None
    assert isinstance(post["claimed_at"], (int, float))


def test_verify_device_claim_is_idempotent(store):
    """A second verify on an already-claimed row preserves the
    original ``claimed_at`` (first claim wins) — the timestamp is
    when the device first attached, not the most recent reconnect.
    """
    issued = store.pair_device("daemon-b", kind="hup", node_id="node-b")
    device_id = issued["device_id"]

    assert store.verify_device(issued["token"]) == device_id
    first = [r for r in store.list_devices() if r["device_id"] == device_id][0]
    first_claimed = first["claimed_at"]
    assert first_claimed is not None

    # Sleep a measurable amount so a buggy "always overwrite" update
    # would produce a different timestamp.
    time.sleep(0.05)

    assert store.verify_device(issued["token"]) == device_id
    second = [r for r in store.list_devices() if r["device_id"] == device_id][0]
    assert second["claimed_at"] == first_claimed
    # last_seen still moves with each verify (sliding window).
    assert (second["last_seen"] or 0) >= (first["last_seen"] or 0)


def test_verify_device_compatible_with_pair_complete(store):
    """``/api/devices/pair/complete`` (which routes through
    ``mark_claimed``) and ``verify_device`` must converge on the same
    behaviour: a row that was claimed via ``mark_claimed`` first, then
    verified via the WS handshake, keeps its original ``claimed_at``.
    """
    issued = store.pair_device("daemon-c", kind="hup", node_id="node-c")
    device_id = issued["device_id"]

    assert store.mark_claimed(issued["token"]) == device_id
    after_complete = [
        r for r in store.list_devices() if r["device_id"] == device_id
    ][0]
    original_claimed_at = after_complete["claimed_at"]
    assert original_claimed_at is not None

    time.sleep(0.05)
    assert store.verify_device(issued["token"]) == device_id

    after_verify = [
        r for r in store.list_devices() if r["device_id"] == device_id
    ][0]
    assert after_verify["claimed_at"] == original_claimed_at


def test_verify_device_unknown_token_does_not_create_phantom_row(store):
    """Belt-and-braces: verifying an unknown token must not create or
    mutate any paired_devices row.
    """
    pre_count = len(store.list_devices())
    assert store.verify_device("0" * 64) is None
    assert len(store.list_devices()) == pre_count


# ──────────────────────────────────────────────────────────────────
# 4. /pair/url + /pair/qr no longer unauth-open
# ──────────────────────────────────────────────────────────────────


def test_pair_url_requires_auth_off_loopback(non_loopback_client):
    """The token-issuance endpoint must not be in the open allowlist:
    a non-loopback client with no Bearer header gets 401.
    """
    r = non_loopback_client.get("/api/devices/pair/url?name=phantom")
    assert r.status_code == 401, r.text


def test_pair_qr_requires_auth_off_loopback(non_loopback_client):
    r = non_loopback_client.get("/api/devices/pair/qr?name=phantom")
    assert r.status_code == 401, r.text


def test_phone_claim_endpoints_remain_open_off_loopback(non_loopback_client):
    """Regression: hardening ``/pair/url`` + ``/pair/qr`` must NOT
    accidentally close the phone-side claim endpoints. A brand-new
    phone scanning the QR has the URL token but no API key, so the
    check / verify_pin / complete trio must still be reachable.

    We don't supply a real token — these endpoints either 400/401/404
    on a bad token, but **never** the 401-from-the-API-key-middleware
    that would mean the path is no longer in the open allowlist. We
    assert the negative: middleware did NOT slap an
    "Authorization: Bearer …" requirement onto them.
    """
    r_check = non_loopback_client.get("/api/devices/pair/check?t=nope")
    assert r_check.status_code != 401, r_check.text
    body = r_check.json()
    # /pair/check returns {pin_required, pin_length} for any token —
    # known, unknown, or empty — so a 200 here proves the middleware
    # let it through to the route handler.
    assert "pin_required" in body
    assert "pin_length" in body

    r_verify = non_loopback_client.post(
        "/api/devices/pair/verify_pin",
        json={"token": "", "pin": ""},
    )
    # Empty token → route returns 400 ("token and pin required"),
    # which is what we want — it means the API-key middleware did
    # not 401 us before the route ran.
    assert r_verify.status_code != 401, r_verify.text

    r_complete = non_loopback_client.post(
        "/api/devices/pair/complete",
        json={"token": ""},
    )
    assert r_complete.status_code != 401, r_complete.text


def test_pair_url_works_on_loopback(client):
    """Sanity: the dashboard (loopback / authed) can still issue
    pair URLs — the hardening must not break the legitimate caller.
    """
    r = client.get("/api/devices/pair/url?name=Pixel-8")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["v"] == 1
    assert body["token"]
    assert body["url"].startswith("http")
