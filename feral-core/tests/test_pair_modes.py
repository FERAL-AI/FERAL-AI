"""Phase 3 / C3.1 — pair URL resolver across Mode A / B / C.

Covers the rewrite of ``feral-core/api/routes/devices.py:_pair_payload``
and ``_resolve_pair_origin`` per the design at
``.internal/audit-v2026.5.5/A4-pairing-redesign.md`` §2-§3.

These tests run against a real ``ConfigLoader`` (with a tmp FERAL_HOME)
so the new ``access`` namespace + ``brain_id`` lazy generation are
exercised end-to-end, not mocked away.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Real ConfigLoader pointed at a fresh tmp home; mocked state with
    the real config + a real DevicePairingStore so we exercise the
    actual code path. ``state.config`` is the SHARED config the route
    module reads — patching both ``api.state.state`` and
    ``api.routes.devices.state`` mirrors the existing fixture style.
    """
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_PUBLIC_BASE_URL", raising=False)

    from config.loader import ConfigLoader
    from security.device_pairing import DevicePairingStore

    config = ConfigLoader(project_dir=str(tmp_path))
    config.discover()
    store = DevicePairingStore(db_path=str(tmp_path / "pairs.db"))

    mock_state = MagicMock()
    mock_state.config = config
    mock_state.device_pairing_store = store

    with (
        patch("api.state.state", mock_state),
        patch("api.routes.devices.state", mock_state),
    ):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=True), config, store


# ── default mode is localhost; pairing is unavailable ──────────────


def test_default_mode_is_localhost_and_pair_url_is_unavailable(env):
    c, config, store = env
    assert config.access_pairing_mode == "localhost"
    before = len(store.list_devices())

    r = c.get("/api/devices/pair/url?name=phone-A")
    assert r.status_code == 409, r.text
    body = r.json()
    assert "Mode B" in body["detail"] or "localhost" in body["detail"].lower()
    # 409 responses must not leak orphan rows in paired_devices.
    assert len(store.list_devices()) == before


def test_pair_qr_returns_409_in_localhost(env):
    c, _, store = env
    before = len(store.list_devices())
    r = c.get("/api/devices/pair/qr?name=phone-A")
    assert r.status_code == 409, r.text
    # 409 responses must not leak orphan rows in paired_devices.
    assert len(store.list_devices()) == before


# ── Mode A — LAN ────────────────────────────────────────────────────


def test_mode_local_emits_lan_url(env, monkeypatch):
    c, config, store = env
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "192.168.50.9")

    r = c.get("/api/devices/pair/url?name=phone-LAN")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["v"] == 1
    assert body["mode"] == "local"
    assert body["url"].startswith("http://192.168.50.9:9090/pair?t=")
    assert body["token"] and len(body["token"]) >= 32
    assert body["brain_id"]
    assert body["expires"] > 0
    assert body["name"] == "FERAL Brain"
    assert body["diagnostic"]["advertised_lan_ip"] == "192.168.50.9"
    assert any(
        "AP" in c or "isolation" in c.lower()
        for c in body["diagnostic"]["honest_caveats"]
    )


def test_mode_local_no_lan_ip_returns_409(env, monkeypatch):
    c, config, store = env
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "")
    before = len(store.list_devices())
    r = c.get("/api/devices/pair/url?name=phone-LAN")
    assert r.status_code == 409
    assert "LAN IP not detected" in r.json()["detail"]
    assert len(store.list_devices()) == before


def test_mode_local_emits_brain_port_not_hardcoded_9090(env, monkeypatch):
    c, config, _ = env
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setenv("FERAL_PORT", "8080")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "10.0.0.5")

    r = c.get("/api/devices/pair/url?name=phone-LAN")
    assert r.status_code == 200
    assert r.json()["url"].startswith("http://10.0.0.5:8080/pair?t=")


# ── Mode A — LAN, but the live listener disagrees ──────────────────
#
# ``bind_host`` is read once at bind time, so applying a mode to a
# running brain does not move the listener. Advertising the LAN address
# anyway is the reported bug: the QR looked fine and nothing answered.


def test_mode_local_with_loopback_listener_returns_409(env, monkeypatch):
    c, config, store = env
    from config.runtime import record_bound_host

    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "192.168.50.9")
    record_bound_host("127.0.0.1")
    before = len(store.list_devices())

    r = c.get("/api/devices/pair/url?name=phone-LAN")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "127.0.0.1" in detail and "restart" in detail.lower()
    # The unreachable address must not appear anywhere in the refusal —
    # printing it is what made the old failure look like a network fault.
    assert "192.168.50.9" not in detail
    assert len(store.list_devices()) == before


def test_mode_local_with_lan_listener_emits_url(env, monkeypatch):
    c, config, _ = env
    from config.runtime import record_bound_host

    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "192.168.50.9")
    record_bound_host("0.0.0.0")

    r = c.get("/api/devices/pair/url?name=phone-LAN")
    assert r.status_code == 200, r.text
    assert r.json()["diagnostic"]["listening_on"] == "0.0.0.0"


def test_no_live_listener_does_not_block_pairing(env, monkeypatch):
    """A CLI invocation has no listener in-process; that is not a conflict."""
    c, config, _ = env
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "192.168.50.9")

    r = c.get("/api/devices/pair/url?name=phone-LAN")
    assert r.status_code == 200, r.text
    assert r.json()["diagnostic"]["listening_on"] is None


# ── Relay — in the enum, no transport behind it yet ────────────────


def test_mode_relay_refuses_rather_than_advertising_lan(env, monkeypatch):
    """Regression: ``relay`` must not inherit the LAN branch.

    ``AccessMode.RELAY`` binds loopback, so falling through to the LAN
    branch would advertise ``http://<lan-ip>:<port>`` with nothing
    listening on it — the exact defect the resolver exists to prevent.
    """
    c, config, store = env
    config.update_settings("access", "pairing_mode", "relay")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "192.168.50.9")
    before = len(store.list_devices())

    r = c.get("/api/devices/pair/url?name=phone-relay")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "192.168.50.9" not in detail
    assert "relay" in detail.lower()
    assert len(store.list_devices()) == before


def test_every_access_mode_is_handled_by_the_resolver(env, monkeypatch):
    """No mode may reach the LAN branch by falling off the if-chain.

    Enumerating the enum means adding a member without teaching the
    resolver about it fails here rather than in a user's pair modal.
    """
    from config.access_mode import AccessMode

    c, config, _ = env
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "192.168.50.9")

    for mode in AccessMode:
        config.update_settings("access", "pairing_mode", mode.value)
        r = c.get(f"/api/devices/pair/url?name=phone-{mode.value}")
        assert r.status_code in (200, 409), (mode, r.status_code, r.text)
        if r.status_code == 200:
            # Only a mode that actually exposes the network may emit a URL.
            assert mode.exposes_pairing, mode
            advertised = r.json()["diagnostic"]["advertised_lan_ip"]
            if mode.bind_host in ("127.0.0.1", "::1", "localhost"):
                raise AssertionError(
                    f"{mode.value} binds loopback but advertised {advertised}"
                )


# ── Mode C — Remote ─────────────────────────────────────────────────


def test_mode_remote_uses_tailnet_url_when_set(env):
    c, config, _ = env
    config.update_settings("access", "pairing_mode", "remote")
    config.update_settings("access", "tailscale", {"funnel": True, "tailnet_url": "https://feral-mac.tailnet-foo.ts.net"})

    r = c.get("/api/devices/pair/url?name=phone-Tailnet")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "remote"
    assert body["url"].startswith("https://feral-mac.tailnet-foo.ts.net/pair?t=")


def test_mode_remote_falls_back_to_public_base_url(env, monkeypatch):
    c, config, _ = env
    config.update_settings("access", "pairing_mode", "remote")
    monkeypatch.setenv("FERAL_PUBLIC_BASE_URL", "https://configured.example.com")

    r = c.get("/api/devices/pair/url?name=phone-Tailnet")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"].startswith("https://configured.example.com/pair?t=")


def test_mode_remote_with_no_url_returns_409(env, monkeypatch):
    c, config, store = env
    config.update_settings("access", "pairing_mode", "remote")
    monkeypatch.delenv("FERAL_PUBLIC_BASE_URL", raising=False)
    config.update_settings("access", "tailscale", {"funnel": True, "tailnet_url": ""})
    before = len(store.list_devices())

    r = c.get("/api/devices/pair/url?name=phone-Tailnet")
    assert r.status_code == 409
    assert "remote-up" in r.json()["detail"] or "FERAL_PUBLIC_BASE_URL" in r.json()["detail"]
    assert len(store.list_devices()) == before


def test_mode_remote_rejects_loopback_public_url(env, monkeypatch):
    c, config, _ = env
    config.update_settings("access", "pairing_mode", "remote")
    monkeypatch.setenv("FERAL_PUBLIC_BASE_URL", "http://localhost:9090")

    r = c.get("/api/devices/pair/url?name=phone-Tailnet")
    assert r.status_code == 409
    assert "loopback" in r.json()["detail"].lower()


# ── Unified payload schema ──────────────────────────────────────────


def test_emitted_payload_matches_unified_v1_schema(env, monkeypatch):
    c, config, _ = env
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "10.20.30.40")

    r = c.get("/api/devices/pair/url?name=phone")
    assert r.status_code == 200
    body = r.json()
    expected_keys = {
        "v", "mode", "url", "token", "brain_id", "expires",
        "name", "device_id", "diagnostic",
    }
    assert expected_keys.issubset(set(body)), body
    assert body["v"] == 1


def test_qr_endpoint_mode_app_query_is_deprecated_but_emits_v1(env, monkeypatch, caplog):
    c, config, _ = env
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "10.20.30.40")

    with caplog.at_level("WARNING", logger="feral.pair"):
        r = c.get("/api/devices/pair/qr?name=phone&mode=app")
    assert r.status_code == 200
    # The qr route returns either StreamingResponse (PNG) or a JSON
    # fallback when ``qrcode`` isn't installed. Either way the shape
    # MUST contain v=1 if JSON.
    if r.headers.get("content-type", "").startswith("application/json"):
        body = r.json()
        assert body.get("pairing_info", {}).get("v") == 1
    assert any("deprecated_mode_app_query" in m for m in caplog.messages)


# ── The QR carries identity, not just a token ──────────────────────


def test_pair_url_carries_identity_blob(env, monkeypatch):
    """The scanned string must convey brain_id, not only the token.

    The QR encodes ``payload["url"]`` alone, so anything outside that
    string cannot reach a phone. Without the blob, ``brain_id`` is
    structurally undeliverable and a client cannot tell one brain from
    another when re-pairing.
    """
    import base64
    import json
    from urllib.parse import parse_qs, urlparse

    c, config, _ = env
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "192.168.50.9")

    r = c.get("/api/devices/pair/url?name=phone")
    assert r.status_code == 200, r.text
    body = r.json()

    query = parse_qs(urlparse(body["url"]).query)
    assert query["t"] == [body["token"]]

    raw = query["p"][0]
    # urlsafe_b64encode padding is stripped for URL cleanliness.
    decoded = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert decoded["brain_id"] == body["brain_id"]
    assert decoded["device_id"] == body["device_id"]
    assert decoded["mode"] == body["mode"]
    assert decoded["expires"] == body["expires"]
    # The reachability prose belongs to the pair modal, not the QR: it is
    # long, and QR density costs scan reliability.
    assert "diagnostic" not in decoded


def test_pair_url_stays_a_working_link(env, monkeypatch):
    """A plain camera app must still open the /pair page."""
    c, config, _ = env
    config.update_settings("access", "pairing_mode", "local")
    monkeypatch.setattr("api.routes.devices._detect_lan_ip", lambda: "192.168.50.9")

    r = c.get("/api/devices/pair/url?name=phone")
    assert r.json()["url"].startswith("http://192.168.50.9:9090/pair?t=")


# ── Brain ID stability ─────────────────────────────────────────────


def test_brain_id_is_stable_across_reads(env):
    _, config, _ = env
    a = config.brain_id
    b = config.brain_id
    assert a and a == b
    # Persisted to settings.json, so a fresh ConfigLoader observes the
    # same value.
    from config.loader import ConfigLoader
    fresh = ConfigLoader(project_dir=str(config.user_home))
    fresh.discover()
    assert fresh.brain_id == a


# ── No more hardcoded `port = 9090` literal in _pair_payload ───────


def test_no_hardcoded_port_9090_in_pair_payload_source():
    """Regression guard: `port = 9090` literal is gone."""
    import inspect
    from api.routes import devices as devices_mod

    src = inspect.getsource(devices_mod)
    # The constant ``9090`` may still appear elsewhere in the module
    # (e.g. log strings), but never as the hardcoded port assignment
    # ``port = 9090`` that ignored runtime config.
    assert "port = 9090" not in src
