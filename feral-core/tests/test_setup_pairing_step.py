"""The first-run wizard's phone-pairing hand-off.

The pairing *mechanism* lives in security/device_pairing.py; this pins the
wizard step that tells a first-run operator how to reach the Brain from
their phone (the gap the modular flow had — it dead-ended at localhost).
"""
from dataclasses import dataclass

from cli.setup.steps import pairing


@dataclass
class _Snap:
    mode: str = "localhost"
    bind_host: str = "127.0.0.1"
    lan_ipv4: str = ""
    remote_url: str = ""


def test_lan_mode_yields_phone_reachable_lan_url():
    url, warn = pairing.resolve_pair_url(_Snap(mode="lan", lan_ipv4="192.168.1.42"), 9090)
    assert url == "http://192.168.1.42:9090"
    assert warn is False


def test_localhost_mode_warns_phone_cannot_reach():
    url, warn = pairing.resolve_pair_url(_Snap(mode="localhost"), 9090)
    assert warn is True
    # Still actionable: a placeholder, never a bare loopback the phone can't use.
    assert "localhost" not in url and "127.0.0.1" not in url


def test_remote_mode_uses_tailscale_url():
    url, warn = pairing.resolve_pair_url(
        _Snap(mode="remote", remote_url="https://brain.example.ts.net/"), 9090
    )
    assert url == "https://brain.example.ts.net"  # trailing slash trimmed
    assert warn is False


def test_lan_without_detected_ip_falls_back_to_placeholder():
    url, warn = pairing.resolve_pair_url(_Snap(mode="lan", lan_ipv4=""), 9090)
    assert "<this-mac-lan-ip>" in url
    assert warn is False


def test_none_snapshot_is_safe():
    url, warn = pairing.resolve_pair_url(None, 9090)
    assert url.endswith(":9090")
    assert warn is False


def test_pairing_step_registered_before_finish():
    # The wizard must run the pairing hand-off, and before the finish
    # summary so the operator sees how to connect their phone.
    import asyncio
    from cli.setup import _run_async  # noqa: F401  (import-time wiring check)
    import inspect

    src = inspect.getsource(_run_async)
    assert '("pairing", pairing.run)' in src
    assert src.index('("pairing"') < src.index('("finish"')
