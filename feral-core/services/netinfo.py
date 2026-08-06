"""Where is this machine reachable? One answer, shared by every caller.

There were three separate LAN detectors, each subtly wrong in a
different way, and the pair QR depended on the worst of them:

* ``api/routes/devices.py::_detect_lan_ip`` connected to ``8.8.8.8:80``
  with **no timeout**. Behind a captive portal that call can hang, and
  it hung inside the request that mints a pairing QR.
* ``cli/setup/network.py::_detect_lan_ipv4`` had a 0.5s timeout but the
  same public-resolver target.
* ``services/mdns.py::_wired_ip`` had the right target and the right
  errno handling but no timeout, and returned a single address.

This module takes the good half of each: the RFC-5737 documentation
address as the route probe (so the answer does not depend on a public
resolver being routable, which is exactly the captive-portal case), a
timeout, the explicit unreachable-errno handling, and **all** private
addresses rather than one.

Returning all of them matters. A laptop with ethernet and wifi, or a VM
bridge, or Docker's ``docker0``, has several private addresses, and the
QR used to advertise whichever one the kernel happened to pick. That is
a real cause of "the QR has the wrong IP".

No packet is ever sent. ``connect()`` on a UDP socket only asks the
kernel which local address would carry traffic to that destination.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import socket

logger = logging.getLogger("feral.netinfo")

# RFC 5737 reserves 192.0.2.0/24 for documentation. Nothing routes there,
# which is the point: we want the kernel's routing decision, not a
# reachable host. Port 9 is discard.
_ROUTE_PROBE = ("192.0.2.1", 9)

_UNREACHABLE = (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL)


def _is_usable(ip: str) -> bool:
    """Private, non-loopback, non-link-local IPv4.

    Link-local (169.254/16) is excluded deliberately. It means DHCP
    failed, so advertising it to a phone promises a route that does not
    exist.
    """
    try:
        addr = ipaddress.IPv4Address(ip)
    except (ipaddress.AddressValueError, ValueError):
        return False
    return addr.is_private and not addr.is_loopback and not addr.is_link_local


def default_route_ipv4(timeout: float = 0.5) -> str:
    """The address the kernel would use to leave this machine, or "".

    This is the single best candidate when there is more than one, which
    is why it leads the list from :func:`detect_lan_ipv4s`.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.connect(_ROUTE_PROBE)
        ip = sock.getsockname()[0]
        return ip if _is_usable(ip) else ""
    except OSError as exc:
        if exc.errno in _UNREACHABLE:
            logger.debug("netinfo.no_route: %s (no LAN address available)", exc)
        else:
            logger.debug("netinfo.route_probe_failed: %s", exc)
        return ""
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _enumerated_ipv4s() -> list[str]:
    """Every usable IPv4 across interfaces, or [] if we cannot enumerate.

    ``ifaddr`` arrives transitively with ``zeroconf``, which lives in an
    extra rather than the base dependency set, so this degrades to empty
    rather than being required. The default-route probe above is stdlib
    and always available, so a base install still gets one correct
    address; it just does not learn about the second interface.
    """
    try:
        import ifaddr
    except ImportError:
        return []

    found: list[str] = []
    try:
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if not ip.is_IPv4:
                    continue
                addr = str(ip.ip)
                if _is_usable(addr) and addr not in found:
                    found.append(addr)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("netinfo.enumerate_failed: %s", exc)
        return []
    return found


def detect_lan_ipv4s(timeout: float = 0.5) -> list[str]:
    """Every address a phone on this network might reach us on.

    Default route first, then any other private IPv4 this machine holds.
    Empty when there is no usable network, which callers must treat as
    "cannot pair over the LAN" rather than falling back to loopback.
    """
    ordered: list[str] = []

    primary = default_route_ipv4(timeout=timeout)
    if primary:
        ordered.append(primary)

    for addr in _enumerated_ipv4s():
        if addr not in ordered:
            ordered.append(addr)

    return ordered


def detect_lan_ipv4(timeout: float = 0.5) -> str:
    """The single best LAN address, or "". Convenience over the list."""
    addresses = detect_lan_ipv4s(timeout=timeout)
    return addresses[0] if addresses else ""


def local_hostname() -> str:
    """The bare hostname, without a trailing dot or ``.local`` suffix.

    Load-bearing for iOS. Apple's ``NSAllowsLocalNetworking`` ATS
    exception unambiguously covers ``.local`` names; whether it covers a
    raw RFC1918 literal is the commonly disputed case. So the
    ``<hostname>.local`` candidate is the guaranteed-working LAN path if
    the literal turns out to be blocked, and it costs nothing to emit.
    """
    name = socket.gethostname()
    name = name.rstrip(".")
    if name.endswith(".local"):
        name = name[: -len(".local")]
    return name


def mdns_hostname() -> str:
    """``<hostname>.local``, the Bonjour name for this machine."""
    return f"{local_hostname()}.local"
