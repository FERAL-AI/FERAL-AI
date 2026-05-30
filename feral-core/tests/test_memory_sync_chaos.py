"""
Chaos tests for federated memory sync ( / roadmap §3.3 #1).

Covers four failure modes the sync layer must survive without taking
the brain down with it:

  - kill_peer_mid_handshake : peer drops the websocket the moment our
    handshake-version frame arrives. The initiator must retry with
    backoff, eventually give up cleanly, leak no asyncio task and no
    file descriptor.
  - corrupt_wal             : a stray byte appended to the SQLite WAL
    file. store.refresh() must detect it and refuse to apply remote
    changes.
  - disk_full               : every WAL append raises ENOSPC. Sync
    must surface the error, flip its IO-pause flag, release its lock,
    and resume cleanly once disk pressure clears.
  - mdns_fail_static_fallback: zeroconf.Zeroconf() raises on init.
    start_discovery() must fall through to the static peer list and
    NOT propagate the exception into the asyncio event loop.

These tests are marked `chaos` so they can be selected (or excluded)
in CI via `pytest -m chaos`.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import tempfile

import pytest

from memory.store import MemoryStore
from memory.sync import (
    SyncDiskFullError,
    SyncEngine,
    SyncWAL,
    SYNC_PASSPHRASE,
)

pytestmark = pytest.mark.chaos


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(tmpdir: str, node_id: str) -> SyncEngine:
    db = os.path.join(tmpdir, f"{node_id}.db")
    wal = os.path.join(tmpdir, f"{node_id}_wal.db")
    store = MemoryStore(db_path=db)
    engine = SyncEngine(node_id=node_id, memory_store=store, db_path=wal)
    store.set_sync_engine(engine)
    return engine


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 1) kill_peer_mid_handshake
# ---------------------------------------------------------------------------


class TestKillPeerMidHandshake:
    """Drop the WS at the moment the handshake-version frame is received."""

    @pytest.mark.asyncio
    async def test_drops_handshake_then_retries_then_gives_up(self):
        websockets = pytest.importorskip("websockets")

        port = _free_port()
        connections_seen = {"count": 0}

        async def hostile(websocket):
            # Read exactly one frame (the sync_request) then slam the
            # connection shut without sending the vector_clock reply.
            connections_seen["count"] += 1
            try:
                await websocket.recv()
            except Exception:
                pass
            await websocket.close(code=1011, reason="chaos: kill mid-handshake")

        server = await websockets.serve(hostile, "127.0.0.1", port)
        tasks_before = {t for t in asyncio.all_tasks() if not t.done()}

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                engine = _make_engine(tmpdir, "node-a")
                engine._peers["hostile-peer"] = {
                    "address": "127.0.0.1",
                    "port": port,
                    "discovered_at": 0.0,
                    "source": "static",
                }

                result = await engine.sync_with_peer(
                    "hostile-peer",
                    max_attempts=3,
                    connect_timeout=2.0,
                    handshake_timeout=2.0,
                    backoff_base=0.05,
                )

                # The initiator must give up cleanly, not raise.
                assert result["success"] is False
                assert result.get("attempts") == 3
                # Backoff means we re-attempted: hostile() was hit 3 times.
                assert connections_seen["count"] == 3, (
                    f"expected 3 retry attempts, saw {connections_seen['count']}"
                )
                # IO is healthy (the failure was network-side, not disk).
                assert engine.io_paused is False
        finally:
            server.close()
            await server.wait_closed()

        # No orphaned task left behind by the failed sync session.
        await asyncio.sleep(0.05)
        tasks_after = {t for t in asyncio.all_tasks() if not t.done()}
        leaked = tasks_after - tasks_before - {asyncio.current_task()}
        assert not leaked, f"leaked asyncio tasks after killed sync: {leaked!r}"


# ---------------------------------------------------------------------------
# 2) corrupt_wal
# ---------------------------------------------------------------------------


class TestCorruptWAL:
    """Append a single random byte to memory.db; refresh() must catch it."""

    async def test_corruption_detected_and_surfaced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, "node-corrupt")
            store = engine._memory

            # Create some real content so the file isn't empty.
            await store.save("note before corruption", tags=["chaos"])
            engine.log_operation(
                "notes", "insert", "n1",
                {"id": "n1", "content": "from-sync-wal", "source": "node-corrupt"},
            )

            healthy = await store.refresh()
            assert healthy["ok"] is True, healthy

            wal_path = engine._wal._db_path
            assert os.path.exists(wal_path), wal_path

            # Append junk bytes inside the SQLite header region — guaranteed
            # to trip integrity_check on the next open.
            with open(wal_path, "r+b") as fh:
                fh.seek(50)
                fh.write(b"\x00\xff\xde\xad\xbe\xef" * 16)

            broken = await store.refresh()
            assert broken["ok"] is False, broken
            assert broken.get("error") in {"wal_corruption", "wal_open_failed"}, broken
            # We surface it as a structured dict, not a raw exception.
            assert isinstance(broken, dict)
            assert "sync_wal" in broken or "error" in broken

            # And we refused to apply: a real apply attempt would explode,
            # but here we assert the gate returned not-ok BEFORE any
            # remote-apply path could run.
            assert engine.io_paused is False  # corruption is a separate signal


# ---------------------------------------------------------------------------
# 3) disk_full
# ---------------------------------------------------------------------------


class TestDiskFull:
    """Simulate ENOSPC at the WAL layer."""

    def test_log_operation_pauses_then_resumes(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, "node-fullfs")

            real_append = SyncWAL.append
            calls = {"n": 0}

            def fake_append(self, op):
                calls["n"] += 1
                raise OSError(errno.ENOSPC, "No space left on device")

            monkeypatch.setattr(SyncWAL, "append", fake_append)

            with pytest.raises(SyncDiskFullError) as exc_info:
                engine.log_operation(
                    "notes", "insert", "row1",
                    {"id": "row1", "content": "won't land", "source": "node-fullfs"},
                )
            assert exc_info.value.errno == errno.ENOSPC
            assert engine.io_paused is True
            assert engine._io_pause_reason == "disk_full"

            # While paused, further log_operation calls fail fast — no
            # additional WAL writes are attempted (lock cleanly released
            # by the previous failure, so this call doesn't block).
            calls_at_pause = calls["n"]
            with pytest.raises(SyncDiskFullError):
                engine.log_operation(
                    "notes", "insert", "row2",
                    {"id": "row2", "content": "also blocked", "source": "node-fullfs"},
                )
            assert calls["n"] == calls_at_pause, (
                "paused engine should not retry the failing append"
            )

            # Disk recovers — restore the real append, call resume(),
            # and confirm the next log_operation lands.
            monkeypatch.setattr(SyncWAL, "append", real_append)
            assert engine.resume() is True
            assert engine.io_paused is False

            engine.log_operation(
                "notes", "insert", "row3",
                {"id": "row3", "content": "post-recovery", "source": "node-fullfs"},
            )
            ops = engine.get_changes_since("0:0:")
            assert any(op["row_id"] == "row3" for op in ops), (
                "expected post-recovery op to land in WAL"
            )

    @pytest.mark.asyncio
    async def test_sync_with_peer_short_circuits_when_paused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, "node-paused")
            engine._peers["any-peer"] = {
                "address": "127.0.0.1",
                "port": 1,
                "discovered_at": 0.0,
                "source": "static",
            }
            engine._io_paused = True
            engine._io_pause_reason = "disk_full"

            result = await engine.sync_with_peer("any-peer", max_attempts=1)
            assert result["success"] is False
            assert result["error"] == "io_paused"
            assert result["io_paused"] is True


# ---------------------------------------------------------------------------
# 4) mdns_fail_static_fallback
# ---------------------------------------------------------------------------


class TestMDNSFailStaticFallback:
    """zeroconf raising at init must NOT bubble into the asyncio loop."""

    @pytest.mark.asyncio
    async def test_zeroconf_raises_then_static_peers_loaded(self, monkeypatch):
        import sys
        import types

        fake_zc = types.ModuleType("zeroconf")

        class _BoomZeroconf:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("chaos: zeroconf interface unavailable")

        class _ServiceInfo:
            def __init__(self, *args, **kwargs):
                pass

        class _ServiceBrowser:
            def __init__(self, *args, **kwargs):
                pass

        fake_zc.Zeroconf = _BoomZeroconf
        fake_zc.ServiceInfo = _ServiceInfo
        fake_zc.ServiceBrowser = _ServiceBrowser

        monkeypatch.setitem(sys.modules, "zeroconf", fake_zc)

        import memory.sync as sync_mod
        monkeypatch.setattr(
            sync_mod, "SYNC_PEERS",
            ["192.168.99.10:9090", "10.0.0.42:8081"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, "node-no-mdns")

            await engine.start_discovery()

            assert engine._running is True
            assert len(engine._peers) >= 2, engine._peers
            addrs = {p["address"] for p in engine._peers.values()}
            assert "192.168.99.10" in addrs
            assert "10.0.0.42" in addrs
            # All static peers — no mDNS source slipped through.
            assert all(p.get("source") == "static" for p in engine._peers.values())

            # No leftover zeroconf object got created (the constructor blew up).
            assert engine._zeroconf is None

            await engine.stop_discovery()
