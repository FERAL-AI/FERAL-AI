"""Pairing-flow tests — typed /api/devices/pair body + QR modes +
``/api/devices/pair/complete`` ack.

The v2 client has three first-class pairing paths:
  * ``kind=browser`` — Web-phone via the /pair?t=TOKEN landing page
  * ``kind=hup``     — daemon / node SDK with an explicit node_id
  * ``kind=name``    — label-only pair (legacy behaviour)

All three must land in DevicePairingStore with their typed metadata
and survive a round-trip through ``GET /api/devices/paired``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    from config.loader import ConfigLoader
    from security.device_pairing import DevicePairingStore

    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover()
    # Default to Mode A so /pair/url tests get a usable payload without
    # needing a Tailscale tunnel. test_pair_modes.py covers the other
    # modes explicitly.
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr(
        "api.routes.devices._detect_lan_ip", lambda: "192.168.50.9"
    )

    store = DevicePairingStore(db_path=str(tmp_path / "pairs.db"))

    mock = MagicMock()
    mock.config = config
    mock.device_pairing_store = store
    with patch("api.state.state", mock), patch("api.routes.devices.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), store


# ── typed body ──────────────────────────────────────────────────────


def test_pair_name_kind_default(client):
    c, store = client
    r = c.post("/api/devices/pair", json={"name": "office-laptop"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "name"
    assert body["name"] == "office-laptop"
    assert "token" in body and len(body["token"]) >= 32
    assert store.verify_device(body["token"]) == body["device_id"]


def test_pair_browser_kind(client):
    c, _store = client
    r = c.post("/api/devices/pair", json={
        "kind": "browser",
        "name": "iphone-safari",
        "platform": "iOS 17 Safari",
        "capabilities": ["camera", "mic", "location"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "browser"
    assert body["platform"] == "iOS 17 Safari"
    assert body["capabilities"] == ["camera", "mic", "location"]


def test_pair_hup_kind(client):
    c, _store = client
    r = c.post("/api/devices/pair", json={
        "kind": "hup",
        "name": "W300 glasses",
        "node_id": "feral-w300-0001",
        "capabilities": ["camera", "display", "haptic"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "hup"
    assert body["node_id"] == "feral-w300-0001"


def test_pair_rejects_unknown_kind(client):
    c, _store = client
    r = c.post("/api/devices/pair", json={"kind": "telepathy"})
    assert r.status_code == 400


def test_pair_rejects_non_list_capabilities(client):
    c, _store = client
    r = c.post("/api/devices/pair", json={
        "kind": "browser",
        "capabilities": "camera,mic",
    })
    assert r.status_code == 400


# ── QR + url helpers ────────────────────────────────────────────────


def test_pair_url_returns_unified_v1_payload(client):
    c, store = client
    r = c.get("/api/devices/pair/url?name=Pixel-8")
    assert r.status_code == 200
    body = r.json()
    # Unified v1 shape (post-Phase-3 redesign). The legacy ``mode=web``
    # string is replaced by the access mode (local|localhost|remote).
    assert body["v"] == 1
    assert body["mode"] in {"local", "remote"}
    assert body["url"].startswith("http")
    assert "/pair?t=" in body["url"]
    assert body["token"] in body["url"]
    assert body["brain_id"]
    assert body["expires"] > 0
    # Record must exist with kind="pending". It was "browser" until the
    # devices-lane fix: this endpoint mints a token and the brain cannot
    # know what will scan the QR, so asserting "a browser was paired"
    # here was a lie. On the audited install it produced 61 rows of
    # kind='browser' (43 never claimed by anything), which is why the
    # owner saw browser connections he never made. The claimant declares
    # what it is at /api/devices/pair/complete.
    rows = store.list_devices()
    assert rows and rows[0]["kind"] == "pending"


def test_pair_qr_mode_validation(client):
    c, _store = client
    r = c.get("/api/devices/pair/qr?mode=nonsense")
    assert r.status_code == 400


# ── /api/devices/pair/complete ──────────────────────────────────────


def test_pair_complete_marks_claim(client):
    c, store = client
    issued = c.get("/api/devices/pair/url?name=browser").json()
    token = issued["token"]

    r = c.post("/api/devices/pair/complete", json={"token": token})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["device_id"]

    # Row should now have claimed_at set.
    rows = store.list_devices()
    assert rows[0]["claimed_at"] is not None


def test_pair_complete_unknown_token(client):
    c, _store = client
    r = c.post("/api/devices/pair/complete", json={"token": "notaknowntoken"})
    assert r.status_code == 404


def test_pair_complete_missing_token(client):
    c, _store = client
    r = c.post("/api/devices/pair/complete", json={})
    assert r.status_code == 400


# ── paired list exposes typed metadata ──────────────────────────────


def test_prune_unclaimed_only(client):
    c, store = client
    # Row 1: unclaimed (will be pruned)
    c.post("/api/devices/pair", json={"kind": "browser", "name": "abandoned"})
    # Row 2: unclaimed but young — we pass older_than_seconds=0 so all
    # unclaimed are eligible.
    c.post("/api/devices/pair", json={"kind": "name", "name": "old"})
    # Row 3: claimed via /api/devices/pair/complete
    issued = c.get("/api/devices/pair/url?name=keeper").json()
    c.post("/api/devices/pair/complete", json={"token": issued["token"]})

    r = c.post("/api/devices/pair/prune", json={"older_than_seconds": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["pruned"] == 2
    assert body["kept"] == 1

    remaining = store.list_devices()
    assert len(remaining) == 1
    assert remaining[0]["name"] == "keeper"
    assert remaining[0]["claimed_at"] is not None


def test_prune_default_threshold_keeps_recent_rows(client):
    c, _store = client
    # Fresh unclaimed row — default prune threshold is 30 min so it stays.
    c.post("/api/devices/pair", json={"kind": "name", "name": "fresh"})
    r = c.post("/api/devices/pair/prune", json={})
    assert r.status_code == 200
    assert r.json()["pruned"] == 0
    assert r.json()["kept"] == 1


def test_prune_empty_store_is_noop(client):
    c, _store = client
    r = c.post("/api/devices/pair/prune", json={"older_than_seconds": 0})
    assert r.status_code == 200
    assert r.json() == {"success": True, "pruned": 0, "kept": 0, "rows": []}


def test_list_paired_includes_kind_and_capabilities(client):
    c, _store = client
    c.post("/api/devices/pair", json={
        "kind": "hup",
        "name": "Wristband",
        "node_id": "band-1",
        "capabilities": ["heart_rate", "haptic"],
    })
    # The default ``/api/devices/paired`` view filters out unclaimed
    # rows (see api/routes/devices.list_paired_devices). The pair we
    # just minted is unclaimed — no daemon has attached yet — so we
    # must opt into ``include_unclaimed=true`` to see it.
    r = c.get("/api/devices/paired?include_unclaimed=true")
    assert r.status_code == 200
    devices = r.json()["devices"]
    assert len(devices) == 1
    row = devices[0]
    assert row["kind"] == "hup"
    assert row["node_id"] == "band-1"
    assert "heart_rate" in row["capabilities"]
