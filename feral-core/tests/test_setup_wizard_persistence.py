"""Regression tests for the setup wizard's persistence + validation.

Covers the three correctness defects that made `feral setup` lie to the
operator:

* F1 — ``WizardState.save()`` rewrote ``settings.json`` from a snapshot
  taken at wizard start, deleting the ``access`` block the network step
  had written to disk mid-run. Choosing Tailscale never persisted.
* F2 — ``apply_lan`` persisted ``access.pairing_mode = "localhost"``,
  which ``GET /api/devices/pair/url`` refuses to pair against, while the
  next wizard step told the operator to go pair a phone.
* F3 — the provider picker offered catalog entries with no runtime
  adapter (``bedrock`` / ``together`` / ``fireworks``), so setup
  "succeeded" and the first chat turn failed.
"""

from __future__ import annotations

import json

import pytest

from cli.setup import network
from cli.setup.state import WizardState


# ---------------------------------------------------------------------------
# F1 — out-of-band writers must survive state.save()
# ---------------------------------------------------------------------------


class TestSaveDoesNotClobberDiskWrites:
    def test_access_block_survives_save(self, tmp_path, monkeypatch):
        """The exact reported sequence: network step writes access.* to
        disk, then the wizard's own save() runs at the end of the run."""
        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))

        state = WizardState.load(home)
        state.set_setting("llm", "provider", "openai")

        # The network step persists straight to settings.json via the
        # shared core (same path `feral access remote-up` uses).
        network._persist_remote_url("https://brain.foo.ts.net")

        state.mark_complete()
        state.save()

        data = json.loads((home / "settings.json").read_text())
        assert data["access"]["pairing_mode"] == "remote"
        assert data["access"]["remote_provider"] == "tailscale"
        assert data["access"]["tailscale"]["tailnet_url"] == "https://brain.foo.ts.net"
        # ...and the wizard's own keys are still there too.
        assert data["llm"]["provider"] == "openai"
        assert data["meta"]["setup_complete"] is True

    def test_mark_complete_alone_preserves_disk_state(self, tmp_path, monkeypatch):
        """mark_complete() runs before save() and clobbered identically."""
        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))

        state = WizardState.load(home)
        network._persist_bind_host("0.0.0.0")
        network._persist_pairing_mode("local")

        state.mark_complete()

        data = json.loads((home / "settings.json").read_text())
        assert data["access"]["pairing_mode"] == "local"
        assert data["network"]["bind_host"] == "0.0.0.0"

    def test_nested_sibling_keys_are_not_dropped(self, tmp_path, monkeypatch):
        """A shallow merge would drop network.port when the wizard
        writes network.bind_host."""
        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))

        (home / "settings.json").write_text(json.dumps({
            "network": {"port": 9191, "tls": True},
        }))

        state = WizardState.load(home)
        state.set_setting("network", "bind_host", "0.0.0.0")
        state.save()

        data = json.loads((home / "settings.json").read_text())
        assert data["network"] == {
            "port": 9191, "tls": True, "bind_host": "0.0.0.0",
        }

    def test_in_memory_settings_match_disk_after_save(self, tmp_path, monkeypatch):
        """The finish step renders from state.settings, so the merged
        result has to land back in memory too."""
        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))

        state = WizardState.load(home)
        network._persist_remote_url("https://brain.foo.ts.net")
        state.save()

        assert state.settings["access"]["pairing_mode"] == "remote"


# ---------------------------------------------------------------------------
# F2 — LAN must persist the pairing mode that can actually pair
# ---------------------------------------------------------------------------


class TestLanPairingMode:
    @pytest.mark.asyncio
    async def test_apply_lan_persists_local_not_localhost(self, tmp_path, monkeypatch):
        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))

        await network.apply_lan()

        data = json.loads((home / "settings.json").read_text())
        assert data["access"]["pairing_mode"] == "local"

    @pytest.mark.asyncio
    async def test_lan_mode_can_emit_a_pair_url(self, tmp_path, monkeypatch):
        """End-to-end intent: after the wizard's LAN branch, the pairing
        route must not raise PairUnavailable."""
        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))

        await network.apply_lan()

        from api.routes import devices

        persisted = json.loads((home / "settings.json").read_text())

        class _Cfg:
            access_pairing_mode = persisted["access"]["pairing_mode"]
            access_remote_url = ""

        monkeypatch.setattr(devices.state, "config", _Cfg(), raising=False)
        monkeypatch.setattr(devices, "_detect_lan_ip", lambda: "192.168.1.42")

        assert devices._resolve_pair_origin().startswith("http://192.168.1.42:")

    @pytest.mark.asyncio
    async def test_localhost_still_persists_localhost(self, tmp_path, monkeypatch):
        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))

        await network.apply_localhost()

        data = json.loads((home / "settings.json").read_text())
        assert data["access"]["pairing_mode"] == "localhost"

    @pytest.mark.asyncio
    async def test_snapshot_reads_local_as_lan(self, tmp_path, monkeypatch):
        home = tmp_path / "feral"
        home.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(home))
        (home / "settings.json").write_text(json.dumps({
            "access": {"pairing_mode": "local"},
            "network": {"bind_host": "127.0.0.1"},
        }))

        snap = await network.get_snapshot()
        assert snap.mode == "lan"


# ---------------------------------------------------------------------------
# F3 — only runtime-dialable providers may be offered
# ---------------------------------------------------------------------------


class TestProviderPickerGate:
    def test_adapterless_catalog_providers_are_not_offered(self):
        from providers.catalog import get_shared_catalog
        from cli.setup.steps.llm import _selectable_providers

        catalog = get_shared_catalog()
        offered = {d.provider_id for d in _selectable_providers(catalog)}
        catalogued = {d.provider_id for d in catalog.list_providers()}

        # The three the runtime has no adapter for.
        assert not offered & {"bedrock", "together", "fireworks"}
        # But the real ones are all still there.
        assert {"openai", "anthropic", "ollama"} <= offered
        assert offered < catalogued

    def test_every_offered_provider_is_runtime_supported(self):
        from agents.llm_provider import is_supported_catalog_provider
        from providers.catalog import get_shared_catalog
        from cli.setup.steps.llm import _selectable_providers

        for desc in _selectable_providers(get_shared_catalog()):
            assert is_supported_catalog_provider(desc.provider_id), desc.provider_id

    def test_hidden_ids_are_disclosed(self):
        from providers.catalog import get_shared_catalog
        from cli.setup.steps.llm import _unsupported_provider_ids

        hidden = set(_unsupported_provider_ids(get_shared_catalog()))
        assert hidden == {"bedrock", "together", "fireworks"}

    def test_built_options_never_include_an_adapterless_provider(self):
        from providers.catalog import get_shared_catalog
        from cli.setup.steps.llm import _build_options

        options = _build_options(get_shared_catalog(), {}, None)
        assert not {o.id for o in options} & {"bedrock", "together", "fireworks"}
