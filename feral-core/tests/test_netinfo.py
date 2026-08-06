"""One LAN detector, replacing three that disagreed.

The one the pair QR used connected to 8.8.8.8:80 with no timeout, so it
could hang inside the request that mints a pairing code, which is
exactly what a captive portal does. It also returned a single address on
machines that have several.
"""

from __future__ import annotations

import errno
import socket

import pytest

from services import netinfo


class TestUsableAddresses:
    @pytest.mark.parametrize("ip", ["192.168.1.5", "10.0.0.7", "172.16.3.9"])
    def test_private_ipv4_is_usable(self, ip):
        assert netinfo._is_usable(ip) is True

    @pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.1.1"])
    def test_loopback_is_not_a_lan_address(self, ip):
        assert netinfo._is_usable(ip) is False

    def test_link_local_is_excluded(self):
        """169.254/16 means DHCP failed. Advertising it promises a route
        that does not exist."""
        assert netinfo._is_usable("169.254.10.1") is False

    def test_public_addresses_are_excluded(self):
        assert netinfo._is_usable("8.8.8.8") is False

    @pytest.mark.parametrize("ip", ["", "not-an-ip", "::1", "999.1.1.1"])
    def test_garbage_is_rejected_without_raising(self, ip):
        assert netinfo._is_usable(ip) is False


class TestDefaultRoute:
    def test_returns_the_socket_local_address(self, monkeypatch):
        monkeypatch.setattr(netinfo, "_fake", None, raising=False)

        class _Sock:
            def settimeout(self, _): pass
            def connect(self, _): pass
            def getsockname(self): return ("192.168.50.9", 54321)
            def close(self): pass

        monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock())
        assert netinfo.default_route_ipv4() == "192.168.50.9"

    def test_no_route_returns_empty_not_loopback(self, monkeypatch):
        """A brain with no network must say so, not advertise 127.0.0.1."""

        class _Sock:
            def settimeout(self, _): pass
            def connect(self, _):
                raise OSError(errno.ENETUNREACH, "Network is unreachable")
            def getsockname(self): raise AssertionError("unreachable")
            def close(self): pass

        monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock())
        assert netinfo.default_route_ipv4() == ""

    def test_a_loopback_route_is_not_reported_as_lan(self, monkeypatch):
        class _Sock:
            def settimeout(self, _): pass
            def connect(self, _): pass
            def getsockname(self): return ("127.0.0.1", 5)
            def close(self): pass

        monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock())
        assert netinfo.default_route_ipv4() == ""

    def test_probe_target_is_not_a_public_resolver(self):
        """RFC 5737 documentation space, so the answer does not depend on
        a public resolver being routable. That dependency is what made
        the old detector hang behind a captive portal."""
        assert netinfo._ROUTE_PROBE[0].startswith("192.0.2.")

    def test_a_timeout_is_always_set(self, monkeypatch):
        seen = {}

        class _Sock:
            def settimeout(self, value): seen["timeout"] = value
            def connect(self, _): pass
            def getsockname(self): return ("10.1.2.3", 1)
            def close(self): pass

        monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock())
        netinfo.default_route_ipv4()
        assert seen["timeout"] == 0.5


class TestMultipleAddresses:
    def test_default_route_leads_the_list(self, monkeypatch):
        monkeypatch.setattr(netinfo, "default_route_ipv4", lambda timeout=0.5: "10.0.0.7")
        monkeypatch.setattr(
            netinfo, "_enumerated_ipv4s", lambda: ["192.168.1.5", "10.0.0.7"]
        )
        assert netinfo.detect_lan_ipv4s() == ["10.0.0.7", "192.168.1.5"]

    def test_no_duplicates(self, monkeypatch):
        monkeypatch.setattr(netinfo, "default_route_ipv4", lambda timeout=0.5: "10.0.0.7")
        monkeypatch.setattr(netinfo, "_enumerated_ipv4s", lambda: ["10.0.0.7"])
        assert netinfo.detect_lan_ipv4s() == ["10.0.0.7"]

    def test_enumeration_still_reports_when_there_is_no_default_route(self, monkeypatch):
        """A host-only network has interfaces but no way out."""
        monkeypatch.setattr(netinfo, "default_route_ipv4", lambda timeout=0.5: "")
        monkeypatch.setattr(netinfo, "_enumerated_ipv4s", lambda: ["192.168.56.1"])
        assert netinfo.detect_lan_ipv4s() == ["192.168.56.1"]

    def test_nothing_at_all_is_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(netinfo, "default_route_ipv4", lambda timeout=0.5: "")
        monkeypatch.setattr(netinfo, "_enumerated_ipv4s", lambda: [])
        assert netinfo.detect_lan_ipv4s() == []
        assert netinfo.detect_lan_ipv4() == ""

    def test_missing_ifaddr_degrades_rather_than_raising(self, monkeypatch):
        """ifaddr rides in with zeroconf, which is an extra. A base
        install must still get its default-route address."""
        import builtins

        real_import = builtins.__import__

        def _no_ifaddr(name, *args, **kwargs):
            if name == "ifaddr":
                raise ImportError("no ifaddr in a base install")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_ifaddr)
        assert netinfo._enumerated_ipv4s() == []


class TestHostname:
    def test_local_suffix_is_stripped(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostname", lambda: "macbook.local")
        assert netinfo.local_hostname() == "macbook"
        assert netinfo.mdns_hostname() == "macbook.local"

    def test_trailing_dot_is_stripped(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostname", lambda: "macbook.")
        assert netinfo.local_hostname() == "macbook"

    def test_bare_hostname_gains_the_suffix_exactly_once(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostname", lambda: "macbook")
        assert netinfo.mdns_hostname() == "macbook.local"


def test_real_machine_answers_coherently():
    """Not mocked. Whatever this machine has, the answers agree."""
    addresses = netinfo.detect_lan_ipv4s()
    assert isinstance(addresses, list)
    assert all(netinfo._is_usable(a) for a in addresses)
    if addresses:
        assert netinfo.detect_lan_ipv4() == addresses[0]
    else:
        assert netinfo.detect_lan_ipv4() == ""


# ── mDNS TXT records ───────────────────────────────────────────────
#
# Discovery listed brains and gave a client nothing to act on: no
# brain_id, so a discovered service could not be matched against a
# stored credential, and no pairing state, so an unpairable brain
# looked identical to a pairable one.


class TestMdnsTxtRecords:
    def test_carries_brain_id(self):
        """The field iOS Bonjour discovery is blocked on."""
        from services.mdns import _txt_properties

        props = _txt_properties("FERAL Brain", "host", 9090)
        assert "brain_id" in props

    def test_every_value_is_a_string(self):
        """TXT is a bag of bytes; zeroconf will not encode an int."""
        from services.mdns import _txt_properties

        props = _txt_properties("FERAL Brain", "host", 9090)
        assert all(isinstance(v, str) for v in props.values()), props

    def test_port_is_carried_as_a_string(self):
        from services.mdns import _txt_properties

        assert _txt_properties("FERAL Brain", "host", 8080)["port"] == "8080"

    def test_values_fit_the_txt_length_limit(self):
        """255 bytes per entry, applied per key by zeroconf."""
        from services.mdns import _txt_properties

        for key, value in _txt_properties("FERAL Brain", "host", 9090).items():
            assert len(f"{key}={value}".encode()) <= 255, key

    def test_a_config_failure_does_not_take_down_discovery(self, monkeypatch):
        """mDNS advertises at boot. A config read that raises here would
        take the whole pairing flow down with it."""
        import services.mdns as mdns_mod

        class _Exploding:
            @property
            def brain_id(self):
                raise RuntimeError("settings unreadable")

        monkeypatch.setattr(
            mdns_mod, "_txt_properties", mdns_mod._txt_properties, raising=False
        )
        import api.state as state_mod

        monkeypatch.setattr(state_mod.state, "config", _Exploding(), raising=False)

        props = mdns_mod._txt_properties("FERAL Brain", "host", 9090)
        # Degrades to the always-available fields rather than raising.
        assert props["name"] == "FERAL Brain"
        assert "version" in props
