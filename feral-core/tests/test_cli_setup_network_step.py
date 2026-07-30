"""Tests for the wizard's network step.

Drives the step against a mocked ``cli.setup.network`` core so the
test doesn't need a real Tailscale install. Verifies the three
profile branches (localhost / LAN / Tailscale) and the truthful
error path when Tailscale fails to apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from cli import ui_kit
from cli.setup import network
from cli.setup.state import WizardState
from cli.setup.steps import network as network_step


@dataclass
class _Snap:
    mode: str = "localhost"
    bind_host: str = "127.0.0.1"
    lan_ipv4: str = "192.168.1.42"
    remote_url: str = ""
    funnel_active: bool = False
    funnel_ports: list = field(default_factory=list)
    tailscale: object = field(
        default_factory=lambda: type(
            "_T",
            (),
            {
                "installed": False,
                "running": False,
                "logged_in": False,
                "dns_name": "",
                "ipv4": "",
                "tailnet": "",
                "error": "",
            },
        )()
    )


@pytest.mark.asyncio
async def test_step_chooses_localhost(monkeypatch, tmp_path):
    state = WizardState.load(tmp_path / "feral")

    async def fake_get_snapshot():
        return _Snap()

    async def fake_apply_localhost():
        return _Snap(mode="localhost", bind_host="127.0.0.1")

    monkeypatch.setattr(network, "get_snapshot", fake_get_snapshot)
    monkeypatch.setattr(network_step.network, "get_snapshot", fake_get_snapshot)
    monkeypatch.setattr(network_step.network, "apply_localhost", fake_apply_localhost)
    monkeypatch.setattr(ui_kit, "pick", lambda *a, **kw: "localhost")

    await network_step.run(state)
    assert state.get_setting("network", "bind_host") == "127.0.0.1"
    assert state.get_setting("access", "pairing_mode") == "localhost"


@pytest.mark.asyncio
async def test_step_chooses_lan_with_confirmation(monkeypatch, tmp_path):
    state = WizardState.load(tmp_path / "feral")

    async def fake_get_snapshot():
        return _Snap()

    async def fake_apply_lan(bind_host="0.0.0.0"):
        return _Snap(mode="lan", bind_host=bind_host, lan_ipv4="192.168.1.42")

    monkeypatch.setattr(network_step.network, "get_snapshot", fake_get_snapshot)
    monkeypatch.setattr(network_step.network, "apply_lan", fake_apply_lan)
    monkeypatch.setattr(ui_kit, "pick", lambda *a, **kw: "lan")
    monkeypatch.setattr(ui_kit, "confirm", lambda *a, **kw: True)

    await network_step.run(state)
    assert state.get_setting("network", "bind_host") == "0.0.0.0"
    # Mode A is spelled "local" at the config layer — this is the value
    # ``GET /api/devices/pair/url`` needs in order to emit a pair URL.
    assert state.get_setting("access", "pairing_mode") == "local"


@pytest.mark.asyncio
async def test_lan_skipped_when_user_declines_warning(monkeypatch, tmp_path):
    state = WizardState.load(tmp_path / "feral")

    apply_called = {"n": 0}

    async def fake_get_snapshot():
        return _Snap()

    async def fake_apply_lan(bind_host="0.0.0.0"):
        apply_called["n"] += 1
        return _Snap(mode="lan", bind_host=bind_host)

    monkeypatch.setattr(network_step.network, "get_snapshot", fake_get_snapshot)
    monkeypatch.setattr(network_step.network, "apply_lan", fake_apply_lan)
    monkeypatch.setattr(ui_kit, "pick", lambda *a, **kw: "lan")
    monkeypatch.setattr(ui_kit, "confirm", lambda *a, **kw: False)

    await network_step.run(state)
    assert apply_called["n"] == 0
    # Setting wasn't touched
    assert state.get_setting("network", "bind_host") is None


@pytest.mark.asyncio
async def test_step_tailscale_success(monkeypatch, tmp_path):
    state = WizardState.load(tmp_path / "feral")

    async def fake_get_snapshot():
        return _Snap()

    async def fake_apply_tailscale_funnel():
        return _Snap(
            mode="remote",
            bind_host="127.0.0.1",
            remote_url="https://brain.cafe.ts.net",
        )

    monkeypatch.setattr(network_step.network, "get_snapshot", fake_get_snapshot)
    monkeypatch.setattr(
        network_step.network, "apply_tailscale_funnel", fake_apply_tailscale_funnel
    )
    monkeypatch.setattr(ui_kit, "pick", lambda *a, **kw: "tailscale")

    await network_step.run(state)
    assert state.get_setting("access", "pairing_mode") == "remote"
    assert (
        (state.settings["access"]["tailscale"] or {}).get("tailnet_url")
        == "https://brain.cafe.ts.net"
    )


@pytest.mark.asyncio
async def test_step_tailscale_failure_shows_remediation(monkeypatch, tmp_path, capsys):
    state = WizardState.load(tmp_path / "feral")

    async def fake_get_snapshot():
        return _Snap()

    async def failing_apply():
        raise network.NetworkApplyError(
            code="tailscale_not_installed",
            message="tailscale binary missing",
            remediation="brew install --cask tailscale",
        )

    monkeypatch.setattr(network_step.network, "get_snapshot", fake_get_snapshot)
    monkeypatch.setattr(network_step.network, "apply_tailscale_funnel", failing_apply)
    monkeypatch.setattr(ui_kit, "pick", lambda *a, **kw: "tailscale")
    # Decline the LAN fallback so the test stays in the failure branch.
    monkeypatch.setattr(ui_kit, "confirm", lambda *a, **kw: False)

    await network_step.run(state)
    out = capsys.readouterr().out
    assert "tailscale_not_installed" in out
    assert "brew install" in out
