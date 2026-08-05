"""The access mode and the bind host must never be able to disagree.

Regression cover for the reported pairing failure: a user clicked "Same
WiFi" in the web UI, which persisted ``access.pairing_mode = "local"``
and left ``network.bind_host`` at ``127.0.0.1``. The brain then handed
the phone a pair URL pointing at a LAN address nothing was listening on,
and the app spun forever. Every surface reported success.

The invariant these tests defend: after *any* writer runs, the persisted
mode and the persisted bind host agree.
"""

from __future__ import annotations

import json

import pytest

from config.access_mode import (
    AccessMode,
    apply_mode,
    coerce,
    configured_bind_host,
    current_mode,
    parse_strict,
    repair_contradiction,
)


@pytest.fixture(autouse=True)
def _contain_bind_host_env(monkeypatch):
    """``apply_mode`` mirrors the bind host into ``FERAL_BIND_HOST`` by
    design (see the comment in ``config/access_mode.py``). Scope that
    write to each test so it does not trip the conftest env-leak guard.
    """
    monkeypatch.setenv("FERAL_BIND_HOST", "127.0.0.1")


class _FakeConfig:
    """Minimal ConfigLoader stand-in: the two methods apply_mode uses."""

    def __init__(self, data: dict | None = None):
        self.data = data or {}

    def get(self, section, key, default=None):
        return (self.data.get(section) or {}).get(key, default)

    def update_settings(self, section, key, value):
        self.data.setdefault(section, {})[key] = value


class TestDerivation:
    def test_every_mode_derives_exactly_one_bind_host(self):
        assert AccessMode.LOCALHOST.bind_host == "127.0.0.1"
        assert AccessMode.LAN.bind_host == "0.0.0.0"
        assert AccessMode.TAILSCALE.bind_host == "127.0.0.1"
        # Relay is reached through an outbound tunnel, so it needs no
        # inbound listener at all. Binding 0.0.0.0 here would be strictly
        # more exposure for zero benefit.
        assert AccessMode.RELAY.bind_host == "127.0.0.1"

    def test_only_localhost_refuses_pairing(self):
        refusing = [m for m in AccessMode if not m.exposes_pairing]
        assert refusing == [AccessMode.LOCALHOST]

    def test_legacy_values_are_preserved_on_disk(self):
        """Existing settings.json files must keep working untouched."""
        assert AccessMode.LOCALHOST.value == "localhost"
        assert AccessMode.LAN.value == "local"
        assert AccessMode.TAILSCALE.value == "remote"


class TestParsing:
    def test_coerce_degrades_unknown_to_localhost(self):
        # A hand-edited settings.json must never stop a brain booting.
        assert coerce("nonsense") is AccessMode.LOCALHOST
        assert coerce(None) is AccessMode.LOCALHOST
        assert coerce("") is AccessMode.LOCALHOST

    def test_parse_strict_raises_on_unknown(self):
        # The write path must not silently turn a "Same WiFi" click into
        # localhost. That silent coercion is the original defect.
        with pytest.raises(ValueError):
            parse_strict("lan-ish")

    def test_both_parsers_pass_through_enum_members(self):
        assert coerce(AccessMode.RELAY) is AccessMode.RELAY
        assert parse_strict(AccessMode.RELAY) is AccessMode.RELAY

    def test_parsing_is_case_and_space_insensitive(self):
        assert parse_strict("  LOCAL ") is AccessMode.LAN


class TestApplyMode:
    @pytest.mark.parametrize("mode", list(AccessMode))
    def test_writes_both_keys_together(self, mode):
        cfg = _FakeConfig()
        result = apply_mode(cfg, mode)

        assert cfg.data["access"]["pairing_mode"] == mode.value
        assert cfg.data["network"]["bind_host"] == mode.bind_host
        assert result.mode is mode
        assert result.bind_host == mode.bind_host

    @pytest.mark.parametrize("mode", list(AccessMode))
    def test_persists_even_when_it_matches_the_default(self, mode):
        """Applying a mode records the choice rather than relying on a
        default that a later release could move."""
        cfg = _FakeConfig()
        apply_mode(cfg, mode)
        assert "pairing_mode" in cfg.data["access"]
        assert "bind_host" in cfg.data["network"]

    def test_repairs_the_reported_broken_state(self):
        """pairing_mode=local + bind_host=127.0.0.1 is what the web
        button used to produce."""
        cfg = _FakeConfig({
            "access": {"pairing_mode": "local"},
            "network": {"bind_host": "127.0.0.1"},
        })
        result = apply_mode(cfg, AccessMode.LAN)

        assert cfg.data["network"]["bind_host"] == "0.0.0.0"
        assert result.changed is True

    def test_reports_no_change_when_already_correct(self):
        cfg = _FakeConfig({
            "access": {"pairing_mode": "local"},
            "network": {"bind_host": "0.0.0.0"},
        })
        assert apply_mode(cfg, AccessMode.LAN).changed is False

    def test_rejects_an_unknown_mode_without_writing(self):
        cfg = _FakeConfig()
        with pytest.raises(ValueError):
            apply_mode(cfg, "sideways")
        assert cfg.data == {}

    def test_sibling_keys_survive(self):
        cfg = _FakeConfig({"network": {"port": 9090, "tls": True}})
        apply_mode(cfg, AccessMode.LAN)
        assert cfg.data["network"]["port"] == 9090
        assert cfg.data["network"]["tls"] is True


class TestRestartRequired:
    def test_false_when_nothing_is_serving(self, monkeypatch):
        monkeypatch.setattr("config.runtime._BOUND_HOST", None)
        assert apply_mode(_FakeConfig(), AccessMode.LAN).restart_required is False

    def test_true_when_the_live_listener_disagrees(self, monkeypatch):
        # The exact case that was invisible: brain running on loopback,
        # operator switches to LAN, nothing says it is not live yet.
        monkeypatch.setattr("config.runtime._BOUND_HOST", "127.0.0.1")
        assert apply_mode(_FakeConfig(), AccessMode.LAN).restart_required is True

    def test_false_when_the_live_listener_already_matches(self, monkeypatch):
        monkeypatch.setattr("config.runtime._BOUND_HOST", "0.0.0.0")
        assert apply_mode(_FakeConfig(), AccessMode.LAN).restart_required is False


class TestRepairContradiction:
    def test_returns_none_when_consistent(self):
        cfg = _FakeConfig({
            "access": {"pairing_mode": "local"},
            "network": {"bind_host": "0.0.0.0"},
        })
        assert repair_contradiction(cfg) is None

    def test_returns_none_on_a_fresh_install(self):
        """No churn on the common path: defaults already agree."""
        assert repair_contradiction(_FakeConfig()) is None

    def test_intent_wins_over_mechanism(self):
        """Someone who chose LAN meant to pair over WiFi. Fix the bind,
        do not silently demote them to localhost."""
        cfg = _FakeConfig({
            "access": {"pairing_mode": "local"},
            "network": {"bind_host": "127.0.0.1"},
        })
        result = repair_contradiction(cfg)

        assert result is not None
        assert cfg.data["access"]["pairing_mode"] == "local"
        assert cfg.data["network"]["bind_host"] == "0.0.0.0"

    def test_is_idempotent(self):
        cfg = _FakeConfig({
            "access": {"pairing_mode": "local"},
            "network": {"bind_host": "127.0.0.1"},
        })
        repair_contradiction(cfg)
        assert repair_contradiction(cfg) is None


class TestRoutesRefuseTheOldWritePath:
    """The web "Same WiFi" button posted straight to /api/config/update,
    a generic setter with no validation and no `access` branch. That is
    the exact route the reported failure came through.
    """

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock, patch

        from fastapi.testclient import TestClient

        from config.loader import ConfigLoader

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        config = ConfigLoader(project_dir=str(tmp_path))
        config.discover()

        mock = MagicMock()
        mock.config = config
        with patch("api.state.state", mock), \
             patch("api.routes.config.state", mock), \
             patch("api.routes.access.state", mock):
            from api.server import app
            yield TestClient(app, raise_server_exceptions=False), config

    def test_config_update_refuses_pairing_mode(self, client):
        c, config = client
        r = c.post("/api/config/update",
                   json={"section": "access", "key": "pairing_mode", "value": "local"})

        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "use_access_mode_endpoint"
        # And crucially, it did not write.
        assert config.get("access", "pairing_mode") != "local"

    def test_config_update_refuses_bind_host(self, client):
        c, config = client
        r = c.post("/api/config/update",
                   json={"section": "network", "key": "bind_host", "value": "0.0.0.0"})

        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "bind_host_is_derived"

    def test_unrelated_settings_still_write(self, client):
        """The guard must be surgical, not a blanket refusal."""
        c, _config = client
        r = c.post("/api/config/update",
                   json={"section": "features", "key": "streaming", "value": True})
        assert r.status_code == 200

    def test_access_mode_endpoint_writes_both_keys(self, client):
        c, config = client
        r = c.post("/api/access/mode", json={"mode": "local"})

        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["mode"] == "local"
        assert body["bind_host"] == "0.0.0.0"
        assert config.get("access", "pairing_mode") == "local"
        assert config.get("network", "bind_host") == "0.0.0.0"

    def test_access_mode_endpoint_rejects_unknown(self, client):
        c, _config = client
        r = c.post("/api/access/mode", json={"mode": "lan-ish"})

        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "invalid_mode"

    def test_access_mode_endpoint_accepts_relay(self, client):
        c, _config = client
        r = c.post("/api/access/mode", json={"mode": "relay"})

        assert r.status_code == 200
        assert r.json()["bind_host"] == "127.0.0.1"

    def test_restart_is_reported_when_the_listener_disagrees(
        self, client, monkeypatch
    ):
        monkeypatch.setattr("config.runtime._BOUND_HOST", "127.0.0.1")
        c, _config = client
        body = c.post("/api/access/mode", json={"mode": "local"}).json()

        assert body["restart_required"] is True
        assert body["restart"]["command"] == "feral restart"


class TestBootRepairThroughConfigLoader:
    def test_discover_heals_a_broken_install(self, tmp_path, monkeypatch):
        """End to end: the reported broken settings.json heals on boot."""
        from config.loader import ConfigLoader

        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))
        (home / "settings.json").write_text(json.dumps({
            "access": {"pairing_mode": "local"},
            "network": {"bind_host": "127.0.0.1", "port": 9090},
        }))

        cfg = ConfigLoader()
        cfg.discover()

        on_disk = json.loads((home / "settings.json").read_text())
        assert on_disk["network"]["bind_host"] == "0.0.0.0"
        assert on_disk["access"]["pairing_mode"] == "local"
        # Unrelated keys are untouched.
        assert on_disk["network"]["port"] == 9090
        assert current_mode(cfg) is AccessMode.LAN
        assert configured_bind_host(cfg) == "0.0.0.0"

    def test_env_mirror_keeps_the_settings_write_from_being_shadowed(
        self, tmp_path, monkeypatch
    ):
        """``brain_bind_host()`` ranks FERAL_BIND_HOST above settings.json,
        and boot seeds that variable. Without the mirror, applying a mode
        and then serving in the same process (``feral setup`` into
        ``feral serve``) would bind the stale value.
        """
        from config.runtime import brain_bind_host

        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))
        monkeypatch.setenv("FERAL_BIND_HOST", "127.0.0.1")

        apply_mode(_FakeConfig(), AccessMode.LAN)

        assert brain_bind_host() == "0.0.0.0"

    def test_relay_mode_survives_a_round_trip(self, tmp_path, monkeypatch):
        """`relay` is new; the loader used to coerce anything outside
        {local, localhost, remote} to localhost."""
        from config.loader import ConfigLoader

        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))
        (home / "settings.json").write_text(json.dumps({
            "access": {"pairing_mode": "relay"},
            "network": {"bind_host": "127.0.0.1"},
        }))

        cfg = ConfigLoader()
        cfg.discover()

        assert cfg.access_pairing_mode == "relay"
        assert current_mode(cfg) is AccessMode.RELAY
