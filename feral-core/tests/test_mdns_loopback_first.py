"""audit-r12 ship — mDNS advertises on loopback first, then wired
interface IP when a route exists; ``OSError(errno=65)`` /
``errno=51`` ("No route to host" / "Network is unreachable") on a
disconnected wifi network no longer crashes the subsystem.

Pre-fix, ``_build_service_info`` did ``socket.gethostbyname(hostname)``
and used whatever ``/etc/hosts`` returned first. On a Mac that lost
wifi after boot the call returned an interface IP that was no
longer reachable, ``zeroconf.register_service`` raised
``OSError(errno=65)``, and the broad ``except Exception`` aborted
the entire subsystem — which also took down the phone-pairing
discovery flow.
"""

from __future__ import annotations

import errno
import socket

import pytest


def _reload_mdns():
    """Return the ``services.mdns`` module.

    Deliberately does NOT pop ``sys.modules`` or call
    :func:`importlib.reload`: both approaches break other tests in
    the suite that hold module references — pop replaces the object
    (orphaning prior ``monkeypatch.setattr(mdns, ...)`` calls) and
    reload rebuilds module-level constants in a way that races sibling
    fixtures. The tests below monkeypatch the symbols they need
    (``_wired_ip``, ``socket.socket``, ``_register_blocking``) so the
    module-level state stays stable for everyone else.
    """
    from services import mdns  # noqa: WPS433
    return mdns


class TestResolveAddresses:
    def test_always_includes_loopback(self):
        mdns = _reload_mdns()
        addrs = mdns._resolve_addresses()
        assert "127.0.0.1" in addrs

    def test_loopback_first(self):
        """Loopback comes first in the list so a brain with no LAN
        route is still discoverable from the same host."""
        mdns = _reload_mdns()
        addrs = mdns._resolve_addresses()
        assert addrs[0] == "127.0.0.1"

    # These patch services.netinfo rather than the module-local
    # ``_wired_ip``: address resolution is now shared with the pair QR
    # and the setup wizard, which previously each had their own detector
    # and disagreed with this one.

    def test_wired_appended_when_route_exists(self, monkeypatch):
        mdns = _reload_mdns()
        monkeypatch.setattr(
            "services.netinfo.detect_lan_ipv4s", lambda timeout=0.5: ["192.168.1.42"]
        )
        addrs = mdns._resolve_addresses()
        assert "127.0.0.1" in addrs
        assert "192.168.1.42" in addrs
        assert addrs[-1] == "192.168.1.42"

    def test_every_lan_address_is_advertised(self, monkeypatch):
        """Multi-homed hosts were advertising whichever one the kernel
        picked, which is a real cause of "found it, cannot reach it"."""
        mdns = _reload_mdns()
        monkeypatch.setattr(
            "services.netinfo.detect_lan_ipv4s",
            lambda timeout=0.5: ["192.168.1.42", "10.0.0.7"],
        )
        addrs = mdns._resolve_addresses()
        assert addrs == ["127.0.0.1", "192.168.1.42", "10.0.0.7"]

    def test_wired_skipped_on_no_route(self, monkeypatch):
        mdns = _reload_mdns()
        monkeypatch.setattr("services.netinfo.detect_lan_ipv4s", lambda timeout=0.5: [])
        addrs = mdns._resolve_addresses()
        assert addrs == ["127.0.0.1"]

    def test_wired_dedup_when_equal_to_loopback(self, monkeypatch):
        """A misconfigured host that reports ``127.0.0.1`` as a LAN
        address must not produce a duplicated entry."""
        mdns = _reload_mdns()
        monkeypatch.setattr(
            "services.netinfo.detect_lan_ipv4s", lambda timeout=0.5: ["127.0.0.1"]
        )
        addrs = mdns._resolve_addresses()
        assert addrs == ["127.0.0.1"]


class TestWiredIpRouteAware:
    def test_returns_none_on_network_unreachable(self, monkeypatch):
        """``socket.connect`` raising ``ENETUNREACH`` (wifi off) must
        produce ``None``, never raise."""
        mdns = _reload_mdns()

        class _NoRouteSocket:
            def __init__(self, *a, **kw):
                pass

            def connect(self, *_a, **_kw):
                raise OSError(errno.ENETUNREACH, "Network is unreachable")

            def getsockname(self):
                raise AssertionError("must not reach")

            def close(self):
                pass

        monkeypatch.setattr(socket, "socket", _NoRouteSocket)
        assert mdns._wired_ip() is None

    def test_returns_none_on_no_route_to_host(self, monkeypatch):
        mdns = _reload_mdns()

        class _Sock:
            def __init__(self, *a, **kw):
                pass

            def connect(self, *_a, **_kw):
                raise OSError(errno.EHOSTUNREACH, "No route to host")

            def close(self):
                pass

        monkeypatch.setattr(socket, "socket", _Sock)
        assert mdns._wired_ip() is None

    def test_returns_address_on_happy_path(self, monkeypatch):
        mdns = _reload_mdns()

        class _Sock:
            def __init__(self, *a, **kw):
                self._closed = False

            def connect(self, *_a, **_kw):
                pass

            def getsockname(self):
                return ("10.0.0.42", 12345)

            def close(self):
                self._closed = True

        monkeypatch.setattr(socket, "socket", _Sock)
        assert mdns._wired_ip() == "10.0.0.42"


class TestAdvertiseBrainNoRouteFallback:
    def test_no_route_oserror_degrades_silently(self, monkeypatch, caplog):
        import logging

        mdns = _reload_mdns()

        def _explode(*_a, **_kw):
            raise OSError(errno.EHOSTUNREACH, "No route to host")

        monkeypatch.setattr(mdns, "_register_blocking", _explode)
        with caplog.at_level(logging.INFO, logger="feral.services.mdns"):
            ok = mdns.advertise_brain(port=9090, name="Test FERAL")
        assert ok is False
        # The handler logged at INFO (not WARNING) so the operator log
        # isn't noisy when the laptop is offline.
        messages = [r.message for r in caplog.records]
        assert any(
            "no LAN route" in m for m in messages
        ), f"expected 'no LAN route' log line, got {messages!r}"
