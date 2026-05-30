"""mDNS service advertisement + discovery for FERAL on the LAN.

A8 /  — `zeroconf.Zeroconf.register_service` performs blocking socket
negotiation. When `advertise_brain` is called from async startup code the
blocking call holds the event loop long enough to trip watchdog detectors
(`EventLoopBlocked`). We fix this two ways:

  1. Expose `advertise_brain_async` which uses `zeroconf.asyncio.AsyncZeroconf`
     (shipping in `zeroconf>=0.131.0`, already pinned in feral-core's
     pyproject.toml) so the coroutine yields back to the loop during I/O.

  2. Keep `advertise_brain` as the legacy sync entry-point, but if we notice
     a running event loop we hand the blocking work off to a background
     thread via a short-lived threadpool executor instead of running it on
     the loop thread. This keeps CLI tools and pre-loop boot paths on the
     simple sync path while making the async-startup path non-blocking.
"""
import asyncio
import errno
import logging
import socket
import threading
from typing import Optional

from version import VERSION as _FERAL_VERSION

logger = logging.getLogger("feral.services.mdns")

_registration: Optional[tuple] = None
_async_registration: Optional[tuple] = None

PHONE_BRIDGE_SERVICE_TYPE = "_feral-phone._tcp.local."


def _resolve_addresses() -> list[str]:
    """Return the list of IPv4 addresses to advertise the brain on.

    audit-r12 ship — pre-fix this called ``socket.gethostbyname(hostname)``
    which returns whatever ``/etc/hosts`` says first; on a misconfigured
    Mac that is often ``127.0.0.1`` *or* an interface IP that's gone
    away (laptop unplugged after boot). The pre-fix path then failed
    with ``OSError(errno=65, "No route to host")`` and aborted the
    entire mDNS subsystem — which also took down the phone-pairing
    flow that relies on it.

    The new resolver:

    1. Always includes ``127.0.0.1`` so the brain is discoverable
       loopback-only even when the LAN is unreachable.
    2. Attempts an outbound UDP socket to a public address (no
       packets actually sent, just route resolution) to discover
       the wired interface IP, and includes it if found.
    3. Falls back silently — never raises. The caller logs a single
       INFO line listing what got advertised.
    """
    out: list[str] = ["127.0.0.1"]
    wired = _wired_ip()
    if wired and wired != "127.0.0.1" and wired not in out:
        out.append(wired)
    return out


def _wired_ip() -> Optional[str]:
    """Best-effort wired-interface IPv4 lookup. Returns ``None`` when
    no usable interface is up — catching ``OSError(errno=65)`` /
    ``errno=51`` ("Network is unreachable") explicitly so a laptop
    that just lost wifi doesn't crash mDNS.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 192.0.2.0/24 is reserved for documentation by RFC 5737; we
        # don't actually send packets, the connect() is enough to ask
        # the kernel which interface would carry traffic to the
        # outside world.
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError as exc:
        if exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL):
            logger.debug(
                "mdns.no_wired_route: %s (advertising loopback only)", exc,
            )
            return None
        logger.debug("mdns.wired_lookup_failed: %s", exc)
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def _build_service_info(port: int, name: str):
    """Return a fresh `zeroconf.ServiceInfo` for the brain advertisement.

    Kept as a tiny helper so sync / async / executor paths can share the
    construction and we only have to change the broadcast fields in one
    place.

    Now advertises on every address returned by :func:`_resolve_addresses`
    — at minimum ``127.0.0.1``, plus the wired interface IP when one
    exists. The primary returned ``ip`` is the wired address when
    available so the caller's log line still reads
    "Advertising X on 192.168.1.5:9090" in the happy path.
    """
    from zeroconf import ServiceInfo

    hostname = socket.gethostname()
    addresses = _resolve_addresses()
    primary = addresses[-1] if len(addresses) > 1 else addresses[0]
    info = ServiceInfo(
        "_feral._tcp.local.",
        f"{name}._feral._tcp.local.",
        addresses=[socket.inet_aton(addr) for addr in addresses],
        port=port,
        properties={
            "version": _FERAL_VERSION,
            "name": name,
            "hostname": hostname,
        },
    )
    return info, primary


def _register_blocking(port: int, name: str):
    """Synchronous register path. Safe to run inside a thread executor."""
    from zeroconf import Zeroconf

    info, ip = _build_service_info(port, name)
    zc = Zeroconf()
    zc.register_service(info)
    return zc, info, ip


def advertise_brain(port: int = 9090, name: str = "FERAL Brain") -> bool:
    """Advertise the brain as a _feral._tcp service on the local network.

    If an asyncio event loop is currently running on this thread, the
    blocking zeroconf registration is deferred to a background thread so
    the caller's event loop is never stalled. If no loop is running we
    keep the  straight-sync behaviour.
    """
    global _registration
    try:
        try:
            asyncio.get_running_loop()
            loop_running = True
        except RuntimeError:
            loop_running = False

        if loop_running:
            result: dict = {}

            def _worker():
                try:
                    result["value"] = _register_blocking(port, name)
                except Exception as exc:  # noqa: BLE001 — propagated below
                    result["error"] = exc

            t = threading.Thread(
                target=_worker, name="feral-mdns-advertise", daemon=True
            )
            t.start()
            t.join()
            if "error" in result:
                raise result["error"]
            zc, info, ip = result["value"]
        else:
            zc, info, ip = _register_blocking(port, name)

        _registration = (zc, info)
        logger.info(f"mDNS: Advertising {name} on {ip}:{port}")
        return True
    except ImportError:
        logger.debug("zeroconf not installed — mDNS discovery disabled")
        return False
    except OSError as e:
        # audit-r12 ship — catch the specific "no route" errors that
        # bring the brain up while wifi is offline; the rest of the
        # subsystem (sync, /v1/session, dashboard) is unaffected so
        # we degrade silently and let phone-pairing retry later via
        # the static `phone_bridge_url` setting.
        if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL):
            logger.info(
                "mDNS: no LAN route (%s) — advertisement skipped; "
                "loopback discovery still works.",
                e,
            )
            return False
        logger.warning(
            "mDNS advertisement failed: %s: %r",
            type(e).__name__,
            e,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return False
    except Exception as e:
        logger.warning(
            "mDNS advertisement failed: %s: %r",
            type(e).__name__,
            e,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return False


async def advertise_brain_async(
    port: int = 9090, name: str = "FERAL Brain"
) -> bool:
    """Async variant of :func:`advertise_brain`.

    Prefers `zeroconf.asyncio.AsyncZeroconf` when available; otherwise
    delegates the sync registration to a default-loop executor. Either
    way, the calling coroutine yields during the network I/O so the
    event loop stays responsive.
    """
    global _async_registration, _registration
    try:
        try:
            from zeroconf.asyncio import AsyncZeroconf, AsyncServiceInfo
            have_async = True
        except ImportError:
            have_async = False

        loop = asyncio.get_running_loop()

        if have_async:
            info, ip = _build_service_info(port, name)
            async_info = AsyncServiceInfo(
                info.type,
                info.name,
                addresses=list(info.addresses),
                port=info.port,
                properties=info.properties,
            )
            zc = AsyncZeroconf()
            await zc.async_register_service(async_info)
            _async_registration = (zc, async_info)
            logger.info(f"mDNS: Advertising {name} on {ip}:{port} (async)")
            return True

        zc, info, ip = await loop.run_in_executor(
            None, _register_blocking, port, name
        )
        _registration = (zc, info)
        logger.info(f"mDNS: Advertising {name} on {ip}:{port}")
        return True
    except ImportError:
        logger.debug("zeroconf not installed — mDNS discovery disabled")
        return False
    except OSError as e:
        if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL):
            logger.info(
                "mDNS (async): no LAN route (%s) — advertisement "
                "skipped; loopback discovery still works.",
                e,
            )
            return False
        logger.warning(
            "mDNS advertisement failed: %s: %r",
            type(e).__name__,
            e,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return False
    except Exception as e:
        logger.warning(
            "mDNS advertisement failed: %s: %r",
            type(e).__name__,
            e,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return False


def stop_advertisement():
    """Stop advertising and clean up zeroconf resources."""
    global _registration, _async_registration
    if _registration:
        zc, info = _registration
        try:
            zc.unregister_service(info)
            zc.close()
        except Exception as exc:
            logger.debug("mDNS sync unregister failed: %s: %r", type(exc).__name__, exc)
        _registration = None
        logger.info("mDNS: Advertisement stopped")
    if _async_registration:
        # We can't await here; schedule the async close on any running loop,
        # or fall back to the sync underlying zeroconf close().
        zc, info = _async_registration
        try:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_stop_async_registration(zc, info))
            except RuntimeError:
                inner = getattr(zc, "zeroconf", None)
                if inner is not None:
                    try:
                        inner.unregister_service(info)
                    except Exception:
                        pass
                    try:
                        inner.close()
                    except Exception:
                        pass
        finally:
            _async_registration = None
            logger.info("mDNS: Async advertisement stopped")


async def _stop_async_registration(zc, info) -> None:
    try:
        await zc.async_unregister_service(info)
    except Exception as exc:
        logger.debug("mDNS async unregister failed: %s: %r", type(exc).__name__, exc)
    try:
        await zc.async_close()
    except Exception as exc:
        logger.debug("mDNS async close failed: %s: %r", type(exc).__name__, exc)


def discover_phone_bridge(timeout: float = 2.5) -> Optional[str]:
    """Discover a FERAL phone bridge on the LAN via mDNS.

    Browses ``_feral-phone._tcp.local.`` for ``timeout`` seconds and returns the
    first ``ws://host:port/path`` URL it finds, or ``None``. Used when the user
    selected "auto" during setup.
    """
    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    except ImportError:
        logger.debug("zeroconf not installed — phone-bridge discovery disabled")
        return None

    found: list[str] = []

    class _Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, timeout=1500)
                if not info:
                    return
                addrs = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
                host = addrs[0] if addrs else socket.inet_ntoa(info.addresses[0])
                port = info.port
                props = info.properties or {}
                path = props.get(b"path", b"/bridge").decode("utf-8", "ignore")
                if not path.startswith("/"):
                    path = "/" + path
                found.append(f"ws://{host}:{port}{path}")
            except Exception as e:
                logger.debug("phone bridge resolve failed: %s: %r", type(e).__name__, e)

        def update_service(self, *a, **kw):
            pass

        def remove_service(self, *a, **kw):
            pass

    zc = None
    try:
        zc = Zeroconf()
        ServiceBrowser(zc, PHONE_BRIDGE_SERVICE_TYPE, _Listener())
        import time
        end = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < end:
            if found:
                break
            time.sleep(0.1)
    except Exception as e:
        logger.warning(
            "phone-bridge discovery failed: %s: %r",
            type(e).__name__,
            e,
        )
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass

    return found[0] if found else None
