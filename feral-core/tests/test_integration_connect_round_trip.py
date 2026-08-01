"""Connecting a third-party integration has to actually work.

Two defects are pinned here.

Home Assistant could not be connected unless it lived at the default
hostname
==========================================================================
The Settings card had one field, a long-lived token.
``HomeAssistantIntegration.__init__`` resolved its base URL from
``FERAL_HA_URL`` / ``HA_URL`` and otherwise assumed
``http://homeassistant.local:8123``, and no HTTP route accepted a URL --
while the provider's own setup text told the user to paste the token
"alongside your HA URL". A Home Assistant on a static IP, a non-default
port, or behind a reverse proxy therefore could not be connected from the
UI at all: the operator had to set an env var and restart the brain.

The "connected" badge meant "a token string exists"
===================================================
Nothing in the brain ran the probes. ``probe_all`` and every
integration's ``probe_connected`` had no production caller; the only
writer of the ``_probe_status`` cache fired once after an OAuth token
exchange, so 60 seconds later ``is_connected_cached`` fell back to token
presence and stayed there. A revoked token probed green forever.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture(autouse=True)
def clean_probe_cache():
    from integrations import _probe_status

    _probe_status.clear()
    yield
    _probe_status.clear()


@pytest.fixture(autouse=True)
def no_stray_sweeper():
    """Routes start the periodic sweep; never leak it into another test."""
    yield
    try:
        from integrations import probe_sweeper
    except ImportError:
        return
    task, probe_sweeper._task = probe_sweeper._task, None
    if task is not None:
        task.cancel()


@pytest.fixture(autouse=True)
def no_ha_env(monkeypatch):
    for var in ("HA_URL", "FERAL_HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(var, raising=False)


# ── Home Assistant base URL ──────────────────────────────────────


def test_default_url_unchanged_when_nothing_is_configured():
    from integrations.home_assistant import DEFAULT_BASE_URL, HomeAssistantIntegration

    assert HomeAssistantIntegration().base_url == DEFAULT_BASE_URL


@pytest.mark.parametrize("raw,expected", [
    ("http://10.0.0.4:8123", "http://10.0.0.4:8123"),
    ("http://10.0.0.4:8123/", "http://10.0.0.4:8123"),
    ("10.0.0.4:8123", "http://10.0.0.4:8123"),
    ("https://ha.example.com", "https://ha.example.com"),
    ("  https://ha.example.com/  ", "https://ha.example.com"),
])
def test_normalize_base_url_accepts_what_people_paste(raw, expected):
    from integrations.home_assistant import normalize_base_url

    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://ha.local", "http://"])
def test_normalize_base_url_rejects_junk(raw):
    from integrations.home_assistant import normalize_base_url

    with pytest.raises(ValueError):
        normalize_base_url(raw)


def test_set_base_url_retargets_the_integration_and_the_probe(monkeypatch):
    """The probe resolves its own URL from env, so both must agree.

    ``security.probe._probe_home_assistant`` reads ``HA_URL`` and nothing
    else. Without the export the integration would call the operator's
    Home Assistant while the probe deciding the badge called
    homeassistant.local, and the card would show "disconnected" over a
    working connection.
    """
    import os

    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()
    assert ha.set_base_url("http://10.0.0.4:8123") == "http://10.0.0.4:8123"
    assert ha.base_url == "http://10.0.0.4:8123"
    assert os.environ["HA_URL"] == "http://10.0.0.4:8123"
    monkeypatch.delenv("HA_URL", raising=False)


def test_set_base_url_drops_the_cached_client(monkeypatch):
    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()
    ha._http = MagicMock()
    ha.set_base_url("http://10.0.0.4:8123")
    assert ha._http is None
    monkeypatch.delenv("HA_URL", raising=False)


def test_saved_url_survives_a_restart(tmp_path, monkeypatch):
    """A URL set from Settings must still be there after a reboot."""
    from security.vault import BlindVault
    from integrations.home_assistant import HomeAssistantIntegration

    vault = BlindVault(vault_path=str(tmp_path / "credentials.json"))
    oauth = MagicMock()
    oauth._vault = vault

    HomeAssistantIntegration(oauth_manager=oauth).set_base_url("http://10.0.0.4:8123")
    monkeypatch.delenv("HA_URL", raising=False)

    reborn = HomeAssistantIntegration(
        oauth_manager=MagicMock(
            _vault=BlindVault(vault_path=str(tmp_path / "credentials.json")),
        ),
    )
    assert reborn.base_url == "http://10.0.0.4:8123"


def test_env_var_still_outranks_the_saved_url(tmp_path, monkeypatch):
    from security.vault import BlindVault
    from integrations.home_assistant import HomeAssistantIntegration

    vault = BlindVault(vault_path=str(tmp_path / "credentials.json"))
    vault.put("integration_config", "home_assistant_url", "http://10.0.0.4:8123")
    monkeypatch.setenv("HA_URL", "http://192.168.1.9:8123")

    ha = HomeAssistantIntegration(oauth_manager=MagicMock(_vault=vault))
    assert ha.base_url == "http://192.168.1.9:8123"


def test_addon_mode_ignores_a_pasted_url(monkeypatch):
    """Inside the add-on, Core is reached through the Supervisor proxy."""
    from integrations.home_assistant import (
        ADDON_DEFAULT_BASE_URL,
        HomeAssistantIntegration,
    )

    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    ha = HomeAssistantIntegration()
    assert ha.base_url == ADDON_DEFAULT_BASE_URL
    assert ha.set_base_url("http://10.0.0.4:8123") == ADDON_DEFAULT_BASE_URL


# ── HTTP surface ─────────────────────────────────────────────────


def _mock_state(tmp_path, *, ha=None):
    from security.vault import BlindVault

    mock = MagicMock()
    mock.vault = BlindVault(vault_path=str(tmp_path / "credentials.json"))
    mock.oauth = MagicMock()
    mock.oauth.list_providers.return_value = [
        {"id": "spotify", "name": "Spotify", "auth_type": "oauth2", "connected": True},
    ]
    mock.oauth.store_api_token = MagicMock()
    mock.oauth.revoke_token = MagicMock()
    mock.email = None
    mock.spotify = None
    mock.notion = None
    mock.home_assistant = ha
    return mock


@pytest.fixture
def client(tmp_path):
    from integrations.home_assistant import HomeAssistantIntegration

    oauth_vault = MagicMock()
    ha = HomeAssistantIntegration(oauth_manager=oauth_vault)
    mock = _mock_state(tmp_path, ha=ha)
    ha._oauth = mock.oauth
    mock.oauth._vault = mock.vault
    with patch("api.state.state", mock), \
         patch("api.routes.integrations_webhooks.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), mock


def test_ha_token_route_accepts_a_url(client, monkeypatch):
    c, mock = client
    with patch(
        "integrations._probe_status.refresh", AsyncMock(return_value=True),
    ):
        body = c.post("/api/integrations/token", json={
            "provider_id": "home_assistant",
            "url": "10.0.0.4:8123",
            "token": "llat-token",
        }).json()

    assert body["ok"] is True
    assert body["base_url"] == "http://10.0.0.4:8123"
    assert body["connected"] is True
    assert mock.home_assistant.base_url == "http://10.0.0.4:8123"
    mock.oauth.store_api_token.assert_called_once_with("home_assistant", "llat-token")
    monkeypatch.delenv("HA_URL", raising=False)


def test_ha_token_route_rejects_an_unusable_url(client):
    c, mock = client
    body = c.post("/api/integrations/token", json={
        "provider_id": "home_assistant",
        "url": "ftp://ha.local",
        "token": "llat-token",
    }).json()
    assert body["ok"] is False
    assert body["reason"] == "invalid_url"
    mock.oauth.store_api_token.assert_not_called()


def test_ha_token_route_reports_a_failed_probe(client, monkeypatch):
    """"Saved" and "reachable" are different claims; report both."""
    c, _mock = client
    with patch(
        "integrations._probe_status.refresh", AsyncMock(return_value=False),
    ):
        body = c.post("/api/integrations/token", json={
            "provider_id": "home_assistant",
            "url": "http://10.0.0.4:8123",
            "token": "llat-token",
        }).json()
    assert body["ok"] is True
    assert body["connected"] is False
    monkeypatch.delenv("HA_URL", raising=False)


# ── Something runs the probes ────────────────────────────────────


def test_refresh_route_exists_and_runs_every_probe(client):
    """The "Refresh status" action _probe_status has always documented."""
    c, _mock = client
    seen = []

    async def _fake_probe(provider_id, *, vault=None, **_kw):
        seen.append(provider_id)
        return True

    with patch("integrations._probe_status.refresh", _fake_probe), \
         patch(
             "integrations.probe_sweeper.known_provider_ids",
             lambda: ["spotify", "notion"],
         ):
        body = c.post("/api/integrations/refresh", json={}).json()

    assert body["ok"] is True
    assert sorted(seen) == ["notion", "spotify"]
    assert body["results"] == {"spotify": True, "notion": True}


def test_refresh_route_can_target_one_provider(client):
    c, _mock = client
    seen = []

    async def _fake_probe(provider_id, *, vault=None, **_kw):
        seen.append(provider_id)
        return False

    with patch("integrations._probe_status.refresh", _fake_probe), \
         patch(
             "integrations.probe_sweeper.known_provider_ids",
             lambda: ["spotify", "notion"],
         ):
        body = c.post(
            "/api/integrations/refresh", json={"provider_id": "spotify"},
        ).json()

    assert seen == ["spotify"]
    assert body["results"] == {"spotify": False}


def test_refresh_route_rejects_a_provider_with_no_probe(client):
    c, _mock = client
    with patch(
        "integrations.probe_sweeper.known_provider_ids", lambda: ["spotify"],
    ):
        body = c.post(
            "/api/integrations/refresh", json={"provider_id": "nope"},
        ).json()
    assert body["ok"] is False
    assert body["reason"] == "no_probe"


def test_listing_integrations_refreshes_stale_probes(client):
    """A page load must not render a badge nobody has verified."""
    c, _mock = client
    seen = []

    async def _fake_probe(provider_id, *, vault=None, **_kw):
        seen.append(provider_id)
        from integrations import _probe_status

        _probe_status.mark_probe_result(provider_id, ok=False, reason="unauthorized")
        return False

    with patch("integrations._probe_status.refresh", _fake_probe), \
         patch("integrations.probe_sweeper.known_provider_ids", lambda: ["spotify"]):
        body = c.get("/api/integrations").json()

    assert seen == ["spotify"]
    row = next(p for p in body["providers"] if p["id"] == "spotify")
    assert row["probe_verified"] is True
    assert row["probe_reason"] == "unauthorized"


def test_listing_integrations_marks_unverified_rows(client):
    """No probe result means "we have not checked", and it must show."""
    c, _mock = client
    with patch("integrations.probe_sweeper.known_provider_ids", lambda: []):
        body = c.get("/api/integrations").json()
    row = next(p for p in body["providers"] if p["id"] == "spotify")
    assert row["connected"] is True
    assert row["probe_verified"] is False


def test_listing_integrations_can_skip_the_sweep(client):
    c, _mock = client
    seen = []

    async def _fake_probe(provider_id, *, vault=None, **_kw):
        seen.append(provider_id)
        return True

    with patch("integrations._probe_status.refresh", _fake_probe), \
         patch("integrations.probe_sweeper.known_provider_ids", lambda: ["spotify"]):
        c.get("/api/integrations?refresh=0")
    assert seen == []


def test_listing_integrations_survives_a_probe_explosion(client):
    """A broken probe must not 500 the settings page."""
    c, _mock = client

    async def _boom(provider_id, *, vault=None, **_kw):
        raise RuntimeError("provider exploded")

    with patch("integrations._probe_status.refresh", _boom), \
         patch("integrations.probe_sweeper.known_provider_ids", lambda: ["spotify"]):
        r = c.get("/api/integrations")
    assert r.status_code == 200


def test_disconnect_turns_the_badge_off_immediately(client):
    """Otherwise a fresh green probe result outlives the revocation."""
    from integrations import _probe_status

    c, _mock = client
    _probe_status.mark_probe_result("spotify", ok=True, reason="ok")
    assert _probe_status.is_connected_cached("spotify", fallback=False) is True

    c.post("/api/integrations/disconnect/spotify")
    assert _probe_status.is_connected_cached("spotify", fallback=True) is False


# ── Sweeper unit behaviour ───────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_only_stale_skips_fresh_entries():
    from integrations import _probe_status, probe_sweeper

    _probe_status.mark_probe_result("spotify", ok=True, reason="ok")
    seen = []

    async def _fake_probe(provider_id, *, vault=None, **_kw):
        seen.append(provider_id)
        return True

    with patch("integrations._probe_status.refresh", _fake_probe):
        results = await probe_sweeper.sweep_once(
            provider_ids=["spotify", "notion"], only_stale=True,
        )
    assert seen == ["notion"]
    assert results == {"notion": True}


@pytest.mark.asyncio
async def test_sweep_records_results_in_the_cache():
    """The whole point: after a sweep, ``connected`` reflects the probe."""
    from integrations import _probe_status, probe_sweeper
    from security.probe import ProbeResult

    async def _fake_probe(provider_id, *, vault=None, force=False):
        return ProbeResult(
            provider=provider_id, ok=False, status_code=401,
            reason="unauthorized", detail="token revoked",
            probed_at=0.0, latency_ms=1.0,
        )

    with patch("security.probe.probe", _fake_probe), \
         patch("security.probe.registered_probe_ids", lambda: ["spotify"]):
        await probe_sweeper.sweep_once(provider_ids=["spotify"])

    # Token presence says True; the probe says the token is dead. The
    # probe wins, which is the behaviour the badge always claimed.
    assert _probe_status.is_connected_cached("spotify", fallback=True) is False


@pytest.mark.asyncio
async def test_sweeper_can_be_disabled(monkeypatch):
    from integrations import probe_sweeper

    monkeypatch.setenv("FERAL_PROBE_SWEEP_SECONDS", "0")
    assert probe_sweeper.ensure_started() is False
    assert probe_sweeper.is_running() is False


@pytest.mark.asyncio
async def test_sweep_interval_stays_under_the_cache_ttl():
    """A sweep slower than the TTL leaves badges reverting to token presence."""
    from integrations import _probe_status, probe_sweeper

    assert probe_sweeper.DEFAULT_SWEEP_SECONDS < _probe_status.STATUS_TTL_SECONDS


@pytest.mark.asyncio
async def test_ensure_started_is_idempotent(monkeypatch):
    from integrations import probe_sweeper

    monkeypatch.setenv("FERAL_PROBE_SWEEP_SECONDS", "30")

    async def _noop(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(probe_sweeper, "sweep_once", _noop)
    try:
        assert probe_sweeper.ensure_started() is True
        first = probe_sweeper._task
        assert probe_sweeper.ensure_started() is True
        assert probe_sweeper._task is first
    finally:
        await probe_sweeper.stop()
    assert probe_sweeper.is_running() is False
