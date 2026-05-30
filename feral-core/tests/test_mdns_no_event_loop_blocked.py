"""A8 /  — mDNS advertise must not stall the asyncio event loop.

 ``advertise_brain`` called blocking ``zeroconf.Zeroconf.register_service``
directly from whatever context invoked it, including async startup hooks.
That triggered ``EventLoopBlocked`` watchdogs during boot.

These tests monkeypatch the zeroconf primitives with ones that sleep long
enough to matter, run a heartbeat coroutine that samples ``loop.time()``
every 10 ms, and assert the largest gap between heartbeats stays below
500 ms while advertising is in flight.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest


class _FakeServiceInfo:
    def __init__(self, *a, **kw):
        self.type = a[0] if a else kw.get("type_")
        self.name = a[1] if len(a) > 1 else kw.get("name")
        self.addresses = list(kw.get("addresses", []))
        self.port = kw.get("port", 0)
        self.properties = kw.get("properties", {})


class _FakeZeroconf:
    """Sync zeroconf stub whose ``register_service`` blocks for ~400 ms."""

    BLOCK_MS = 400

    def __init__(self, *a, **kw) -> None:
        self._registered: list[_FakeServiceInfo] = []

    def register_service(self, info: _FakeServiceInfo) -> None:
        time.sleep(self.BLOCK_MS / 1000.0)
        self._registered.append(info)

    def unregister_service(self, info: _FakeServiceInfo) -> None:
        if info in self._registered:
            self._registered.remove(info)

    def close(self) -> None:
        return None


class _FakeAsyncZeroconf:
    """Async zeroconf stub — ``async_register_service`` yields back via
    ``await asyncio.sleep(...)`` so the event loop stays responsive."""

    BLOCK_MS = 400

    def __init__(self, *a, **kw) -> None:
        self._registered: list = []
        self.zeroconf = _FakeZeroconf()

    async def async_register_service(self, info) -> None:
        await asyncio.sleep(self.BLOCK_MS / 1000.0)
        self._registered.append(info)

    async def async_unregister_service(self, info) -> None:
        if info in self._registered:
            self._registered.remove(info)

    async def async_close(self) -> None:
        return None


@pytest.fixture
def patched_zeroconf(monkeypatch: pytest.MonkeyPatch):
    """Install the fakes as the ``zeroconf`` / ``zeroconf.asyncio`` modules."""
    fake_zc_mod = types.ModuleType("zeroconf")
    fake_zc_mod.Zeroconf = _FakeZeroconf
    fake_zc_mod.ServiceInfo = _FakeServiceInfo
    fake_zc_mod.ServiceBrowser = object
    fake_zc_mod.ServiceListener = object

    fake_async_mod = types.ModuleType("zeroconf.asyncio")
    fake_async_mod.AsyncZeroconf = _FakeAsyncZeroconf
    fake_async_mod.AsyncServiceInfo = _FakeServiceInfo

    monkeypatch.setitem(sys.modules, "zeroconf", fake_zc_mod)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", fake_async_mod)

    import services.mdns as mdns_mod
    monkeypatch.setattr(mdns_mod, "_registration", None, raising=False)
    monkeypatch.setattr(mdns_mod, "_async_registration", None, raising=False)
    yield mdns_mod
    mdns_mod.stop_advertisement()


async def _heartbeat_watcher(stop: asyncio.Event) -> float:
    """Sample the event-loop clock every 10 ms, return the largest gap seen."""
    loop = asyncio.get_running_loop()
    last = loop.time()
    worst = 0.0
    while not stop.is_set():
        await asyncio.sleep(0.010)
        now = loop.time()
        gap = now - last
        if gap > worst:
            worst = gap
        last = now
    return worst


def test_advertise_brain_async_does_not_stall_loop(patched_zeroconf) -> None:
    mdns_mod = patched_zeroconf

    async def _run() -> tuple[bool, float]:
        stop = asyncio.Event()
        watcher = asyncio.create_task(_heartbeat_watcher(stop))
        ok = await mdns_mod.advertise_brain_async(port=9099, name="W24d-async")
        stop.set()
        worst = await watcher
        return ok, worst

    ok, worst_gap = asyncio.run(_run())
    assert ok is True
    assert worst_gap < 0.500, (
        f"advertise_brain_async must not stall the loop by >=500ms; "
        f"worst gap was {worst_gap*1000:.0f}ms"
    )


def test_advertise_brain_sync_from_running_loop_does_not_stall(
    patched_zeroconf,
) -> None:
    """When the legacy sync `advertise_brain` is called from an async
    context, it must offload to a thread and keep the loop responsive."""
    mdns_mod = patched_zeroconf

    async def _run() -> tuple[bool, float]:
        stop = asyncio.Event()
        watcher = asyncio.create_task(_heartbeat_watcher(stop))
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None, lambda: mdns_mod.advertise_brain(port=9098, name="W24d-sync")
        )

        nudge_task = asyncio.create_task(asyncio.sleep(0.050))
        await nudge_task
        stop.set()
        worst = await watcher
        return ok, worst

    ok, worst_gap = asyncio.run(_run())
    assert ok is True
    assert worst_gap < 0.500, (
        f"advertise_brain (sync) from a running loop must not stall it by "
        f">=500ms; worst gap was {worst_gap*1000:.0f}ms"
    )


def test_advertise_brain_sync_without_loop_still_works(patched_zeroconf) -> None:
    """Regression guard for the CLI / non-async boot path."""
    mdns_mod = patched_zeroconf
    ok = mdns_mod.advertise_brain(port=9097, name="W24d-cli")
    assert ok is True
    assert mdns_mod._registration is not None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
