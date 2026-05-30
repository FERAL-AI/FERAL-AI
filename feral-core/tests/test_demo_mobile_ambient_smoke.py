"""Smoke test for the (private) mobile-ambient demo pipeline.

The demo script itself lives at private/demos/demo_mobile_ambient.sh
and is gitignored. This test is the PUBLIC guarantee that the
underlying Brain wiring the demo depends on still works. It asserts
four moving parts end-to-end against a real TestClient:

    1. POST /api/devices/pair/url issues a token + scannable URL.
    2. The pair record lands in DevicePairingStore.
    3. /api/devices/pair/complete marks the token claimed.
    4. The handshake + claim flow is ready for BrowserNode.js to attach
       (we don't stand up a real WebSocket here — the unit tests in
       test_pair_flows.py already cover /v1/node), but we verify the
       HTTP scaffolding that BrowserNode calls out-of-band.

If this test breaks, the private demo breaks — fix the test first.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    from config.loader import ConfigLoader
    from security.device_pairing import DevicePairingStore

    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover()
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr(
        "api.routes.devices._detect_lan_ip", lambda: "192.168.50.9"
    )

    store = DevicePairingStore(db_path=str(tmp_path / "demo_pairs.db"))

    mock = MagicMock()
    mock.config = config
    mock.device_pairing_store = store
    with patch("api.state.state", mock), patch("api.routes.devices.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), store


def test_demo_step_1_issue_pair_url(brain):
    """demo_mobile_ambient.sh calls GET /api/devices/pair/url on the Brain."""
    c, store = brain
    r = c.get("/api/devices/pair/url?name=demo-phone")
    assert r.status_code == 200, r.text
    body = r.json()
    # Post-Phase-3: unified v1 payload, ``mode`` is now the access mode.
    assert body["v"] == 1
    assert body["mode"] == "local"
    assert body["url"].startswith("http")
    assert "/pair?t=" in body["url"]
    assert len(body["token"]) >= 32
    # Record landed in the pairing store with kind="browser".
    rows = store.list_devices()
    assert len(rows) == 1
    assert rows[0]["kind"] == "browser"
    assert rows[0]["name"] == "demo-phone"


def test_demo_step_2_token_is_unclaimed_until_complete(brain):
    """Until BrowserNode.js calls /api/devices/pair/complete the token
    should be issued-but-unclaimed so the Paired UI can display that.

    : the plaintext token is no longer recoverable from
    ``list_devices()`` — only the deterministic ``token_lookup`` (a
    SHA-256 over the plaintext) is exposed. We verify the lookup
    matches the issued token instead of comparing plaintext.
    """
    import hashlib
    c, store = brain
    issued = c.get("/api/devices/pair/url").json()
    token = issued["token"]

    row = store.list_devices()[0]
    assert row["claimed_at"] is None
    expected_lookup = hashlib.sha256(token.encode()).hexdigest()
    assert row["token_lookup"] == expected_lookup


def test_demo_step_3_complete_marks_claimed(brain):
    """BrowserNode.js POSTs to /api/devices/pair/complete after the WS
    register succeeds. That flips claimed_at so the UI shows the device
    as live.

    : identify the row by the device_id returned from
    ``/api/devices/pair/complete`` (or by the deterministic
    ``token_lookup``) — the plaintext token is no longer stored.
    """
    c, store = brain
    issued = c.get("/api/devices/pair/url").json()
    token = issued["token"]

    r = c.post("/api/devices/pair/complete", json={"token": token})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["device_id"]

    row = [d for d in store.list_devices() if d["device_id"] == body["device_id"]][0]
    assert row["claimed_at"] is not None


def test_demo_step_4_bad_token_is_rejected(brain):
    """Catch typos: an unknown token must 404 at pair/complete."""
    c, _store = brain
    r = c.post("/api/devices/pair/complete", json={"token": "deadbeef"})
    assert r.status_code == 404


def test_demo_supervisor_events_endpoint_is_tailable(brain):
    """The shell script polls /api/supervisor/events?source=node to
    narrate the session. The endpoint must exist and return a JSON
    shape the shell `python3 -c json.load` snippet can parse even when
    the Supervisor is not wired (it's optional at boot)."""
    c, _store = brain
    r = c.get("/api/supervisor/events?limit=10&source=node")
    # Supervisor may not be wired on the bare TestClient fixture →
    # that's a documented 503 in api/routes/supervisor.py. Either
    # response is tailable by the script — which defaults to "empty
    # list" on any non-200.
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert "events" in body
        assert isinstance(body["events"], list)
