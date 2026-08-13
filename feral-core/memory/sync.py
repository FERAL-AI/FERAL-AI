"""
FERAL Federated Memory — CRDT-based P2P sync
===============================================
Replicates memory across FERAL instances on the local network.
No cloud relay — all sync is peer-to-peer via mDNS discovery.

Protocol:
  1. mDNS discovery: find peers advertising _feral._tcp.local.
  2. WebSocket handshake with shared passphrase
  3. Exchange vector clocks to determine missing operations
  4. Send missing ops → merge via CRDT rules
  5. Periodic heartbeat to detect disconnections

Conflict resolution:
  - Notes/Knowledge: last-writer-wins (by HLC timestamp)
  - Episodes: union (never delete remote episodes)
  - Execution log: append-only
"""

from __future__ import annotations
import asyncio
import errno
import json
import logging
import os
import ssl
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

from config.loader import feral_data_home
from config.runtime import brain_port
from memory.hlc import HybridLogicalClock, HLCTimestamp

logger = logging.getLogger("feral.memory.sync")

SYNC_PORT = int(os.getenv("FERAL_SYNC_PORT", str(brain_port())))
SYNC_PASSPHRASE = os.getenv("FERAL_SYNC_PASSPHRASE", "")
SERVICE_TYPE = "_feral._tcp.local."

# audit-r12 A2 (v2026.5.38) — vault namespace + key for the persisted
# sync passphrase. ``ensure_sync_passphrase`` first reads the env var,
# then falls back to the vault, then auto-generates + persists +
# returns. The handshake at ``/sync`` rejects a connection with an
# empty passphrase (pre-fix it accepted, which made federated sync a
# zero-auth endpoint when ``FERAL_SYNC_PASSPHRASE`` was unset — the
# default).
_SYNC_VAULT_NAMESPACE = "sync"
_SYNC_VAULT_KEY = "passphrase"

# TLS mutual auth configuration
SYNC_TLS_CERT = os.getenv("FERAL_SYNC_TLS_CERT", "")
SYNC_TLS_KEY = os.getenv("FERAL_SYNC_TLS_KEY", "")
SYNC_TLS_CA = os.getenv("FERAL_SYNC_TLS_CA", "")
SYNC_REQUIRE_CLIENT_CERT = os.getenv("FERAL_SYNC_REQUIRE_CLIENT_CERT", "").lower() in ("1", "true", "yes")

# Static peer list fallback (comma-separated host:port pairs)
SYNC_PEERS = [p.strip() for p in os.getenv("FERAL_SYNC_PEERS", "").split(",") if p.strip()]

_MDNS_DISCOVERY_TIMEOUT = 30  # seconds before falling back to static peers


def ensure_sync_passphrase() -> str:
    """Resolve the federated-sync shared secret, generating + persisting
    it on first boot when none exists yet.

    Resolution order (each step short-circuits the rest):

    1. ``FERAL_SYNC_PASSPHRASE`` env var, when non-empty.
    2. ``BlindVault`` ``sync.passphrase`` namespace key (set by a
       previous boot of this brain).
    3. ``secrets.token_urlsafe(32)`` — a fresh 43-character random
       string. Persisted to the vault, exported as
       ``FERAL_SYNC_PASSPHRASE`` for any subprocess that re-reads
       env, and *printed once* via :func:`_print_passphrase_banner`
       so the operator can write it down to pair another brain.

    The vault read/write swallows ``VaultError`` (no vault yet,
    keychain missing) and falls back to a process-lifetime secret
    so that brain boot never blocks on the federation key. A loud
    warning is emitted in that path because the next boot will mint
    a fresh secret and existing peers will need to be re-paired.
    """
    global SYNC_PASSPHRASE

    env = os.getenv("FERAL_SYNC_PASSPHRASE", "").strip()
    if env:
        SYNC_PASSPHRASE = env
        return env

    try:
        from security.vault import get_vault, VaultError
    except Exception:  # pragma: no cover — vault module unavailable
        get_vault = None  # type: ignore
        VaultError = Exception  # type: ignore

    stored: Optional[str] = None
    if get_vault is not None:
        try:
            v = get_vault()
            stored = v.get(_SYNC_VAULT_NAMESPACE, _SYNC_VAULT_KEY)
        except VaultError as exc:
            logger.warning(
                "sync.passphrase_vault_read_failed: %s — using "
                "process-lifetime secret; rotate after fix.",
                exc,
            )

    if stored:
        SYNC_PASSPHRASE = stored
        os.environ["FERAL_SYNC_PASSPHRASE"] = stored
        return stored

    import secrets

    fresh = secrets.token_urlsafe(32)

    persisted = False
    if get_vault is not None:
        try:
            v = get_vault()
            v.put(
                _SYNC_VAULT_NAMESPACE,
                _SYNC_VAULT_KEY,
                fresh,
                stored_by="boot.auto-generate",
            )
            persisted = True
        except VaultError as exc:
            logger.warning(
                "sync.passphrase_vault_write_failed: %s — using "
                "process-lifetime secret. Set FERAL_SYNC_PASSPHRASE to "
                "pin a value across restarts.",
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — vault audit may raise
            logger.warning(
                "sync.passphrase_vault_write_failed: %s", exc,
            )

    SYNC_PASSPHRASE = fresh
    os.environ["FERAL_SYNC_PASSPHRASE"] = fresh
    _print_passphrase_banner(fresh, persisted=persisted)
    return fresh


def _print_passphrase_banner(passphrase: str, *, persisted: bool) -> None:
    """Render the auto-generated sync passphrase to stderr with framing
    so the operator notices it on first boot.

    Never logged at INFO/DEBUG so a routine log scrape doesn't pick
    the secret up. The banner is also written to
    ``$FERAL_HOME/sync_passphrase.first_boot`` (chmod 0600) so a
    headless install can still recover the value if the operator
    missed the boot output.
    """
    bar = "─" * 64
    location = "persisted to vault" if persisted else "PROCESS-LIFETIME ONLY"
    print(bar, file=sys.stderr, flush=True)
    print(
        f"  FERAL — federated sync passphrase ({location})", file=sys.stderr, flush=True,
    )
    print(bar, file=sys.stderr, flush=True)
    print(f"    {passphrase}", file=sys.stderr, flush=True)
    print(
        "  Set FERAL_SYNC_PASSPHRASE on every peer to pair with this brain.",
        file=sys.stderr, flush=True,
    )
    print(bar, file=sys.stderr, flush=True)

    try:
        home = Path(os.environ.get("FERAL_HOME", str(Path.home() / ".feral")))
        home.mkdir(parents=True, exist_ok=True)
        marker = home / "sync_passphrase.first_boot"
        marker.write_text(passphrase + "\n", encoding="utf-8")
        try:
            marker.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.debug("sync.passphrase_marker_write_failed: %s", exc)


def build_server_ssl_context() -> Optional[ssl.SSLContext]:
    """Build an SSL context for the sync WebSocket server, or None if TLS is not configured."""
    if not SYNC_TLS_CERT or not SYNC_TLS_KEY:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=SYNC_TLS_CERT, keyfile=SYNC_TLS_KEY)
    if SYNC_TLS_CA:
        ctx.load_verify_locations(cafile=SYNC_TLS_CA)
    if SYNC_REQUIRE_CLIENT_CERT:
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx.verify_mode = ssl.CERT_OPTIONAL
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    logger.info("Sync TLS server context created (client_cert=%s)", "required" if SYNC_REQUIRE_CLIENT_CERT else "optional")
    return ctx


def build_client_ssl_context() -> Optional[ssl.SSLContext]:
    """Build an SSL context for outgoing sync connections, or None if TLS is not configured."""
    if not SYNC_TLS_CA and not SYNC_TLS_CERT:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if SYNC_TLS_CA:
        ctx.load_verify_locations(cafile=SYNC_TLS_CA)
    if SYNC_TLS_CERT and SYNC_TLS_KEY:
        ctx.load_cert_chain(certfile=SYNC_TLS_CERT, keyfile=SYNC_TLS_KEY)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _parse_hlc(hlc_str: str) -> tuple:
    """Parse HLC string to comparable tuple: (wall_time, counter, node_id)."""
    parts = hlc_str.split(":", 2)
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0, parts[2] if len(parts) > 2 else "")
    except (ValueError, IndexError):
        return (0, 0, "")


_NODE_ID_FILENAME = "sync_node_id"


class DuplicateNodeIdError(RuntimeError):
    """Two SyncEngines advertised the same ``node_id``.

    The HLC protocol relies on every node carrying a globally unique
    identifier. A duplicate either means an operator copied
    ``~/.feral/sync_node_id`` between two machines, or two brains were
    cloned from the same disk image without rotating the file. The
    fix is destructive (delete the file on one side and restart) so
    we surface the error loudly rather than silently demoting one
    node's writes to merge conflicts.
    """


def stable_node_id(data_home: Optional[Path] = None) -> str:
    """Return the persistent per-brain HLC node id.

    The id is a UUID-v7-flavoured value (time-ordered, suitable for
    debugging "which brain wrote which op when") persisted to
    ``<data_home>/sync_node_id`` on first boot and re-read on every
    subsequent boot. This is part of the brain backup set; restoring
    a backup onto a *different* physical brain MUST be followed by
    ``rm ~/.feral/sync_node_id`` so the new brain rolls a fresh
    identity (otherwise the network sees two brains with the same id
    and the duplicate-detection guard fires).

    Concurrency: two cold-boot processes racing to create the file
    both write, but the read-back is deterministic because the
    filesystem serialises the create. We accept the tiny race window
    rather than introduce a lock — a duplicate id only matters at
    handshake time and the duplicate-detection guard catches it
    there.

    The function is sync because it runs from the sync ``__init__``
    boot path, before the event loop is up.
    """
    home = data_home if data_home is not None else feral_data_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / _NODE_ID_FILENAME

    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError as exc:
            logger.warning("sync_node_id read failed (%s); generating a new id", exc)

    # First boot for this brain (or unreadable file). Generate a fresh
    # UUID-v7-shaped id: wall-clock-ms in the high bits gives natural
    # ordering when an operator greps multi-brain logs, and uuid4()'s
    # randomness in the low bits guards against the 1-ms collision
    # window.
    wall_ms = int(time.time() * 1000)
    nonce = uuid4().hex[:12]
    fresh = f"{wall_ms:013d}-{nonce}"
    try:
        path.write_text(fresh + "\n", encoding="utf-8")
    except OSError as exc:
        logger.error("sync_node_id write failed: %s — node id will not survive restart", exc)
    return fresh


@dataclass
class SyncOperation:
    """A single write operation to be replicated."""
    op_id: str
    table: str
    op_type: str  # "insert", "update", "delete"
    row_id: str
    data: dict
    hlc: str
    origin_node: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "op_id": self.op_id,
            "table": self.table,
            "op_type": self.op_type,
            "row_id": self.row_id,
            "data": self.data,
            "hlc": self.hlc,
            "origin_node": self.origin_node,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d: dict) -> "SyncOperation":
        return SyncOperation(**d)


@dataclass
class VectorClock:
    """Tracks the latest HLC seen from each node."""
    clocks: dict[str, str] = field(default_factory=dict)

    def update(self, node_id: str, hlc: str):
        current = self.clocks.get(node_id, "0:0:")
        if _parse_hlc(hlc) > _parse_hlc(current):
            self.clocks[node_id] = hlc

    def to_dict(self) -> dict:
        return dict(self.clocks)

    @staticmethod
    def from_dict(d: dict) -> "VectorClock":
        return VectorClock(clocks=dict(d))


class SyncDiskFullError(OSError):
    """Raised when a WAL write fails because the underlying disk is full.

    Subclasses OSError so existing OSError handlers still match, while
    callers that want to react specifically (pause sync, surface a
    recoverable banner in the UI) can isinstance-check this type.
    """

    def __init__(self, message: str = "WAL write failed: no space left on device"):
        super().__init__(errno.ENOSPC, message)


class SyncWAL:
    """Write-Ahead Log for sync operations — stored in SQLite."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        # Serialize WAL writes within a process. SQLite already serializes
        # at the file level, but holding a Python-level lock means a
        # crashing thread can't leak a half-finished append to a peer
        # observer: append() is atomic from our callers' perspective.
        self._write_lock = threading.RLock()
        self._init_wal()

    def _init_wal(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_wal (
                op_id TEXT PRIMARY KEY,
                table_name TEXT NOT NULL,
                op_type TEXT NOT NULL,
                row_id TEXT NOT NULL,
                data TEXT NOT NULL,
                hlc TEXT NOT NULL,
                origin_node TEXT NOT NULL,
                timestamp REAL NOT NULL,
                synced_to TEXT DEFAULT '[]'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wal_hlc ON sync_wal(hlc)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wal_origin ON sync_wal(origin_node)")
        conn.commit()
        conn.close()

    def append(self, op: SyncOperation):
        """Synchronous WAL append. Used by the boot path and the few
        background callers that don't run inside an event loop. Async
        callers (every `MemoryStore` write hot path) MUST use
        :meth:`append_async` instead — calling this from `await`-land
        blocks the event loop on `sqlite3.connect` + `commit` for the
        full duration of the disk fsync.
        """
        with self._write_lock:
            conn = sqlite3.connect(self._db_path)
            try:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO sync_wal (op_id, table_name, op_type, row_id, data, hlc, origin_node, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                        (op.op_id, op.table, op.op_type, op.row_id, json.dumps(op.data), op.hlc, op.origin_node, op.timestamp),
                    )
                    conn.commit()
                except (OSError, sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                    # Disk-full / read-only fs / corrupted WAL all surface here.
                    # Translate ENOSPC to a SyncDiskFullError so callers can
                    # pause sync without sniffing errno strings, and let other
                    # disk errors propagate as-is (they're not recoverable
                    # by retry alone).
                    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                        raise SyncDiskFullError(str(exc)) from exc
                    msg = str(exc).lower()
                    if "no space" in msg or "disk full" in msg or "disk i/o" in msg:
                        raise SyncDiskFullError(str(exc)) from exc
                    raise
            finally:
                conn.close()

    async def append_async(self, op: SyncOperation) -> None:
        """Async wrapper around :meth:`append` that off-loads the
        sqlite3 fsync onto a worker thread.

        We deliberately do not rewrite the underlying append in
        ``aiosqlite`` because the sync version is still called from
        the boot path and from peer-applied changes (where there is
        no running event loop). Off-loading via ``asyncio.to_thread``
        keeps the event loop free for the chat / voice paths while
        the WAL append commits — which on a slow disk can be tens
        to hundreds of milliseconds and was the dominant slow-callback
        offender flagged by AUDIT-r14 finding 14.
        """
        await asyncio.to_thread(self.append, op)

    def integrity_check(self) -> dict:
        """Run SQLite integrity_check on the WAL file.

        Returns a dict shaped like:
            {"ok": True}                     # healthy
            {"ok": False, "error": "...",    # corruption / IO / open failure
             "detail": "..."}

        Never raises — the caller (store.refresh / sync engine) is the one
        that decides how to surface a recoverable error to the user.
        """
        try:
            conn = sqlite3.connect(self._db_path)
        except sqlite3.Error as exc:
            return {"ok": False, "error": "wal_open_failed", "detail": str(exc)}
        try:
            try:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
            except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
                return {"ok": False, "error": "wal_corruption", "detail": str(exc)}
            statuses = [r[0] for r in rows] if rows else []
            if statuses == ["ok"]:
                return {"ok": True}
            return {
                "ok": False,
                "error": "wal_corruption",
                "detail": "; ".join(statuses) or "integrity_check returned no rows",
            }
        finally:
            conn.close()

    def get_changes_since(self, hlc: str, exclude_node: str = "") -> list[SyncOperation]:
        threshold = _parse_hlc(hlc)
        conn = sqlite3.connect(self._db_path)
        try:
            if exclude_node:
                rows = conn.execute(
                    "SELECT op_id, table_name, op_type, row_id, data, hlc, origin_node, timestamp FROM sync_wal WHERE origin_node != ?",
                    (exclude_node,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT op_id, table_name, op_type, row_id, data, hlc, origin_node, timestamp FROM sync_wal",
                ).fetchall()

            ops = [
                SyncOperation(
                    op_id=r[0], table=r[1], op_type=r[2], row_id=r[3],
                    data=json.loads(r[4]), hlc=r[5], origin_node=r[6], timestamp=r[7],
                )
                for r in rows
                if _parse_hlc(r[5]) > threshold
            ]
            ops.sort(key=lambda op: _parse_hlc(op.hlc))
            return ops
        finally:
            conn.close()

    def mark_synced(self, op_id: str, peer_node: str):
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute("SELECT synced_to FROM sync_wal WHERE op_id=?", (op_id,)).fetchone()
            if row:
                synced = json.loads(row[0])
                if peer_node not in synced:
                    synced.append(peer_node)
                    conn.execute("UPDATE sync_wal SET synced_to=? WHERE op_id=?", (json.dumps(synced), op_id))
                    conn.commit()
        finally:
            conn.close()

    def mark_synced_many(self, op_ids: list[str], peer_node: str) -> int:
        """Record delivery of many operations to one peer, in one commit.

        The per-op :meth:`mark_synced` opens and closes a connection per
        call. A first sync against a fresh peer ships the entire WAL,
        which on the real store is 16,184 operations, so the one-shot
        version is the only one the exchange path can afford to use.
        Returns the number of rows actually changed.
        """
        if not op_ids:
            return 0
        conn = sqlite3.connect(self._db_path)
        try:
            changed = 0
            updates: list[tuple[str, str]] = []
            # Chunked so the IN list stays under SQLITE_MAX_VARIABLE_NUMBER
            # (999 on the conservative builds this ships against).
            for start in range(0, len(op_ids), 500):
                batch = op_ids[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"SELECT op_id, synced_to FROM sync_wal WHERE op_id IN ({placeholders})",
                    batch,
                ).fetchall()
                for op_id, raw in rows:
                    try:
                        synced = json.loads(raw or "[]")
                    except (TypeError, ValueError):
                        # A corrupt cell must not cost the whole batch;
                        # rewriting it from a known-good base is strictly
                        # better than leaving it unparseable forever.
                        synced = []
                    if not isinstance(synced, list):
                        synced = []
                    if peer_node in synced:
                        continue
                    synced.append(peer_node)
                    updates.append((json.dumps(synced), op_id))
            if updates:
                conn.executemany(
                    "UPDATE sync_wal SET synced_to=? WHERE op_id=?", updates
                )
                conn.commit()
                changed = len(updates)
            return changed
        finally:
            conn.close()

    @property
    def count(self) -> int:
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM sync_wal").fetchone()[0]
        finally:
            conn.close()


class SyncEngine:
    """
    Manages peer-to-peer memory replication.

    Uses mDNS for peer discovery (zeroconf) and WebSocket for data exchange.
    """

    def __init__(self, node_id: str, memory_store=None, db_path: str = None):
        self.node_id = node_id
        self._memory = memory_store
        self._hlc = HybridLogicalClock(node_id)
        self._vector_clock = VectorClock()

        wal_path = db_path or str(feral_data_home() / "sync_wal.db")
        self._wal = SyncWAL(wal_path)

        self._peers: dict[str, dict] = {}
        self._running = False
        self._zeroconf = None
        self._service_info = None
        # Audit-r9: track the browser handle so `stop_discovery` can
        # cancel it on shutdown (Async path needs `async_cancel`).
        self._service_browser = None

        # Per-peer asyncio locks so a chaos-killed handshake retry can't
        # interleave with a fresh outbound sync against the same peer.
        # Lazy-allocated in sync_with_peer because asyncio.Lock() in 3.11
        # binds to the running loop on first await.
        self._peer_locks: dict[str, asyncio.Lock] = {}
        # Pause flag flipped when a WAL write returns ENOSPC. Sync stays
        # quiet until resume() is called (typically after the operator
        # frees disk space and the next log_operation succeeds).
        self._io_paused = False
        self._io_pause_reason = ""

        # AUDIT-FIXES F-06. Strong references to the fire-and-forget mDNS
        # resolve tasks the async peer listener schedules. The event loop
        # holds tasks only weakly, so the previous bare create_task could be
        # collected between the zeroconf callback and the reply arriving,
        # dropping a peer that is on the network. Same shape as
        # ``MemoryStore._bg_tasks``; the done-callback discard keeps the set
        # bounded by the number of in-flight resolves.
        self._bg_tasks: set[asyncio.Task] = set()

        logger.info(f"SyncEngine initialized: node={node_id}, wal={wal_path}")

    def _track_bg_task(self, task: asyncio.Task) -> asyncio.Task:
        """Hold a strong reference to a fire-and-forget task. See F-06."""
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    @property
    def io_paused(self) -> bool:
        return self._io_paused

    def resume(self) -> bool:
        """Clear the IO-pause flag after the operator confirms disk recovery.

        Returns True if a probe write to the WAL succeeds. The probe is a
        single integrity_check (read-only) — appending a probe op would
        pollute the CRDT log.
        """
        check = self._wal.integrity_check()
        if not check.get("ok"):
            logger.warning("resume() denied: WAL integrity check failed: %s", check)
            return False
        self._io_paused = False
        self._io_pause_reason = ""
        logger.info("SyncEngine resumed after IO pause (node=%s)", self.node_id)
        return True

    def _build_operation(
        self, table: str, op_type: str, row_id: str, data: dict
    ) -> SyncOperation:
        """Mint a SyncOperation with a fresh HLC for *table.row_id*.

        Lifted out of ``log_operation`` so the sync and async paths
        can share the same HLC bump + envelope construction without
        duplicating logic.
        """
        if self._io_paused:
            raise SyncDiskFullError(
                f"sync paused (reason={self._io_pause_reason or 'unknown'})"
            )
        hlc_ts = self._hlc.now()
        return SyncOperation(
            op_id=str(uuid4()),
            table=table,
            op_type=op_type,
            row_id=row_id,
            data=data,
            hlc=hlc_ts.to_string(),
            origin_node=self.node_id,
        )

    def _on_wal_append_failure(
        self, table: str, op_type: str, exc: BaseException
    ) -> None:
        """Centralise the disk-full → pause + re-raise dance shared by
        both the sync and async log paths."""
        if isinstance(exc, SyncDiskFullError):
            self._io_paused = True
            self._io_pause_reason = "disk_full"
            logger.warning(
                "WAL disk full, sync paused (node=%s op=%s/%s): %s",
                self.node_id, table, op_type, exc,
            )
            raise exc
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            self._io_paused = True
            self._io_pause_reason = "disk_full"
            logger.warning(
                "WAL disk full (raw OSError), sync paused (node=%s op=%s/%s): %s",
                self.node_id, table, op_type, exc,
            )
            raise SyncDiskFullError(str(exc)) from exc
        raise exc

    def log_operation(self, table: str, op_type: str, row_id: str, data: dict) -> str:
        """Synchronous variant — kept for the boot path and any
        non-async caller. Async callers MUST use
        :meth:`log_operation_async` so the WAL fsync runs on a worker
        thread instead of blocking the event loop. See AUDIT-r14
        finding 14 (sync SyncWAL on episode_save hot path).

        Returns the HLC string for the op so the caller can persist
        ``hlc_string`` into the row itself — this is what makes the
        D12 LWW check on the receiving side possible.

        Raises SyncDiskFullError when the WAL filesystem is full; callers
        upstream (MemoryStore._log_sync) intentionally swallow the error
        so a full sync_wal.db never breaks a local note save.
        """
        op = self._build_operation(table, op_type, row_id, data)
        try:
            self._wal.append(op)
        except (SyncDiskFullError, OSError) as exc:
            self._on_wal_append_failure(table, op_type, exc)
            raise  # _on_wal_append_failure already re-raised; defensive
        self._vector_clock.update(self.node_id, op.hlc)
        return op.hlc

    async def log_operation_async(
        self, table: str, op_type: str, row_id: str, data: dict
    ) -> str:
        """Async-safe variant of :meth:`log_operation`.

        Identical semantics (HLC mint + WAL append + vector clock
        update + disk-full pause) but the sqlite3 commit runs on a
        worker thread so the calling coroutine yields control while
        the disk fsync resolves. This is the codepath the chat /
        voice / memory hot paths use.
        """
        op = self._build_operation(table, op_type, row_id, data)
        try:
            await self._wal.append_async(op)
        except (SyncDiskFullError, OSError) as exc:
            self._on_wal_append_failure(table, op_type, exc)
            raise  # defensive
        self._vector_clock.update(self.node_id, op.hlc)
        return op.hlc

    def get_changes_since(self, hlc: str) -> list[dict]:
        ops = self._wal.get_changes_since(hlc)
        return [op.to_dict() for op in ops]

    # Tables that the sync subsystem is willing to mutate. Anything
    # outside this set is rejected to block a malicious peer from
    # issuing ``DROP TABLE`` via the sync channel by stuffing the
    # ``table`` field. Mirrored in both the apply path and the delete
    # path.
    _SYNC_ALLOWED_TABLES = frozenset({
        "notes", "episodes", "conversations", "knowledge",
        "wiki_pages", "execution_log",
        # v2026.5.35 (F1) — the unified KG. ``add_entity`` and
        # ``add_relation`` log here so KG-native writes replicate
        # under the same HLC LWW gate that PR 2 D12 ships for the
        # flat tables.
        "entities", "relations",
    })

    async def apply_remote_changes(self, changes: list[dict]) -> int:
        """Apply operations received from a peer. Returns count of applied ops.

        v2026.5.34 (PR 2 D12): runs on the asyncio event loop and
        uses MemoryStore's connection pool — the previous code path
        opened a fresh ``sqlite3.connect`` per op which (a) blocked
        the event loop on a hot WAL and (b) bypassed the WAL +
        busy-timeout PRAGMAs the pool sets up. Each op is now
        gated by an HLC last-writer-wins check at the row level, so
        the arrival order on the wire no longer dictates which copy
        survives.
        """
        applied = 0
        for change_dict in changes:
            op = SyncOperation.from_dict(change_dict)

            remote_hlc = HLCTimestamp.from_string(op.hlc)
            self._hlc.receive(remote_hlc)
            self._vector_clock.update(op.origin_node, op.hlc)

            try:
                self._wal.append(op)
            except Exception as exc:  # pragma: no cover — WAL failure is rare
                logger.warning("apply_remote_changes WAL append failed: %s", exc)
                continue

            if self._memory:
                try:
                    if await self._apply_to_memory(op):
                        applied += 1
                except Exception as exc:
                    logger.warning("apply_remote_changes materialization failed: %s", exc)
                    continue
            else:
                applied += 1

        return applied

    async def _apply_to_memory(self, op: SyncOperation) -> bool:
        """Apply a sync operation to the local MemoryStore using the
        async pool, gated by HLC LWW.

        Returns ``True`` when the op was materialized, ``False`` when
        an HLC compare or table guard skipped it. Exceptions propagate
        up to ``apply_remote_changes`` which logs and continues with
        the next op.
        """
        if op.table not in self._SYNC_ALLOWED_TABLES:
            logger.warning("Sync rejected: unknown table %s", op.table)
            return False

        conn = await self._memory._conn()
        try:
            # LWW gate. Read the existing row's hlc_string and
            # compare with the remote op's HLC tuple. If remote is
            # not strictly greater, the row is already at a
            # later-or-equal version and the apply is a no-op.
            #
            # We compare on the (wall_ms, counter, node_id) tuple
            # because (wall_ms, counter) alone breaks ties on two
            # nodes that tick the same physical millisecond — the
            # node_id provides a deterministic tiebreaker.
            async with conn.execute(
                f"SELECT hlc_string FROM {op.table} WHERE id = ?",
                (op.row_id,),
            ) as cur:
                existing_row = await cur.fetchone()

            remote_tuple = _parse_hlc(op.hlc)
            existing_hlc = existing_row["hlc_string"] if existing_row and "hlc_string" in existing_row.keys() else ""
            existing_tuple = _parse_hlc(existing_hlc) if existing_hlc else (0, 0, "")

            if existing_row is not None and remote_tuple <= existing_tuple:
                logger.debug(
                    "sync LWW skip: table=%s id=%s remote=%s existing=%s",
                    op.table, op.row_id, op.hlc, existing_hlc,
                )
                return False

            if op.op_type == "delete":
                # Honour LWW for deletes too — a delete with an older
                # HLC than the surviving row's last-write is stale.
                # The placeholder row (id-only) was created above by
                # the gating SELECT so the check already ran.
                await conn.execute(
                    f"DELETE FROM {op.table} WHERE id = ?", (op.row_id,)
                )
                await conn.commit()
                return True

            if op.op_type != "insert":
                logger.warning("sync: unknown op_type %s for %s", op.op_type, op.table)
                return False

            d = op.data
            now = time.time()
            hlc = op.hlc
            if op.table == "notes":
                await conn.execute(
                    "INSERT OR REPLACE INTO notes "
                    "(id, content, tags, importance, source, created_at, updated_at, hlc_string) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        d.get("id", op.row_id), d.get("content", ""), d.get("tags", "[]"),
                        d.get("importance", "normal"), d.get("source", "sync"),
                        d.get("created_at", now), now, hlc,
                    ),
                )
            elif op.table == "episodes":
                # episodes are append-only by id; the LWW gate above
                # has already short-circuited a stale arrival.
                # INSERT OR REPLACE is now correct because the gate
                # only lets newer arrivals through.
                await conn.execute(
                    "INSERT OR REPLACE INTO episodes "
                    "(id, session_id, event_type, summary, detail, "
                    "importance, created_at, hlc_string) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        d.get("id", op.row_id), d.get("session_id", "sync"),
                        d.get("event_type", "synced"), d.get("summary", ""),
                        d.get("detail", ""), d.get("importance", 0.5),
                        d.get("created_at", now), hlc,
                    ),
                )
            elif op.table == "knowledge":
                await conn.execute(
                    "INSERT OR REPLACE INTO knowledge "
                    "(id, subject, predicate, object, confidence, source, "
                    "created_at, updated_at, hlc_string) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        d.get("id", op.row_id), d.get("subject", ""),
                        d.get("predicate", ""), d.get("object", ""),
                        d.get("confidence", 1.0), d.get("source", "sync"),
                        d.get("created_at", now), now, hlc,
                    ),
                )
            elif op.table == "execution_log":
                await conn.execute(
                    "INSERT OR REPLACE INTO execution_log "
                    "(id, session_id, skill_id, endpoint_id, args, "
                    "result_status, result_summary, latency_ms, "
                    "created_at, hlc_string) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        d.get("id", op.row_id), d.get("session_id", ""),
                        d.get("skill_id", ""), d.get("endpoint_id", ""),
                        d.get("args", "{}"), d.get("result_status", "unknown"),
                        d.get("result_summary", ""), d.get("latency_ms", 0),
                        d.get("created_at", now), hlc,
                    ),
                )
            elif op.table == "entities":
                # v2026.5.35 (F1) — entities are content-addressed by
                # ``_stable_kg_id(name, type)`` so two brains computing
                # the same name+type get the same id; the LWW gate
                # above already enforces strictly-newer arrivals.
                # ``embedding`` is recomputed locally on first read,
                # not shipped over the wire (saves ~3KB per entity).
                await conn.execute(
                    "INSERT OR REPLACE INTO entities "
                    "(id, name, entity_type, embedding, metadata, "
                    "mention_count, hlc_string, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        d.get("id", op.row_id), d.get("name", ""),
                        d.get("entity_type", "thing"), None,
                        d.get("metadata", "{}"), d.get("mention_count", 1),
                        hlc, d.get("created_at", now), now,
                    ),
                )
            elif op.table == "relations":
                # Relations are content-addressed by
                # ``_stable_kg_id(source_id, relation_type, target_id)``.
                # A peer that ships a relation whose source/target
                # entities haven't synced yet would FK-fail on
                # INSERT — guard by upserting placeholder entities
                # so the relation lands and the real entity rows
                # converge on their own HLC pass.
                src_id = d.get("source_id", "")
                tgt_id = d.get("target_id", "")
                for ent_id in (src_id, tgt_id):
                    if not ent_id:
                        continue
                    # The placeholder ``name`` is the id itself — the
                    # real entity row will arrive on its own sync op
                    # and overwrite this stub under LWW (the stub's
                    # hlc_string is empty, so anything newer wins).
                    await conn.execute(
                        "INSERT OR IGNORE INTO entities "
                        "(id, name, entity_type, metadata, mention_count, "
                        "hlc_string, created_at, updated_at) "
                        "VALUES (?, ?, 'thing', '{}', 0, '', ?, ?)",
                        (ent_id, ent_id, now, now),
                    )
                await conn.execute(
                    "INSERT OR REPLACE INTO relations "
                    "(id, source_id, relation_type, target_id, confidence, "
                    "evidence_text, source_origin, hlc_string, "
                    "created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        d.get("id", op.row_id), src_id,
                        d.get("relation_type", ""), tgt_id,
                        d.get("confidence", 1.0),
                        d.get("evidence_text", ""), "sync", hlc,
                        d.get("created_at", now), now,
                    ),
                )
            else:
                # ``wiki_pages`` and ``conversations`` are in the
                # allow-list for deletes but currently have no
                # peer-driven insert flow; if a peer sends one,
                # surface it so we notice the gap.
                logger.warning(
                    "sync: insert for table %s has no materializer", op.table
                )
                return False

            await conn.commit()
            return True
        finally:
            await self._memory._release(conn)

    def get_vector_clock(self) -> dict:
        return self._vector_clock.to_dict()

    @staticmethod
    def _get_lan_ip() -> str:
        """Get the real LAN IP address, not loopback."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        hostname = socket.gethostname()
        try:
            addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
            for addr in addrs:
                ip = addr[4][0]
                if not ip.startswith("127."):
                    return ip
        except Exception:
            pass
        return "127.0.0.1"

    def _load_static_peers(self):
        """Load peers from FERAL_SYNC_PEERS env var (host:port pairs)."""
        for entry in SYNC_PEERS:
            try:
                host, port_str = entry.rsplit(":", 1)
                port = int(port_str)
                peer_id = f"static-{host}:{port}"
                if peer_id not in self._peers:
                    self._peers[peer_id] = {
                        "address": host,
                        "port": port,
                        "discovered_at": time.time(),
                        "source": "static",
                    }
                    logger.info("Static peer added: %s:%d", host, port)
            except (ValueError, IndexError):
                logger.warning("Invalid static peer entry: %s (expected host:port)", entry)

    async def start_discovery(self):
        """Start mDNS service advertisement and peer discovery, with static peer fallback.

        Audit-r9 brief #08 fix: previously this method ran sync
        ``zeroconf.Zeroconf()`` + ``register_service()`` +
        ``ServiceBrowser(...)`` + ``zc.get_service_info(...)`` directly
        on the asyncio loop. Even on a clean LAN those calls block long
        enough for python-zeroconf to raise ``EventLoopBlocked``, which
        then surfaced as ``mDNS discovery skipped: EventLoopBlocked()``
        on every brain boot. Mirror the pattern in ``services/mdns.py``
        (``advertise_brain_async``): prefer ``zeroconf.asyncio.AsyncZeroconf``
        when available so the coroutine yields during I/O; fall back to
        ``loop.run_in_executor`` for the sync API so the loop still
        stays responsive on older zeroconf installs.
        """
        mdns_ok = False
        try:
            import socket
            from zeroconf import ServiceInfo

            try:
                from zeroconf.asyncio import (
                    AsyncZeroconf,
                    AsyncServiceBrowser,
                    AsyncServiceInfo,
                )
                have_async = True
            except ImportError:
                have_async = False

            ip = self._get_lan_ip()
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                f"feral-{self.node_id}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip)],
                port=SYNC_PORT,
                properties={
                    b"node_id": self.node_id.encode(),
                    b"version": b"1.0.0",
                },
            )

            engine = self

            class PeerListener:
                def __init__(self):
                    pass

                # Sync-API listener: used when AsyncServiceBrowser is
                # unavailable. Critically, `zc.get_service_info(...)`
                # is a blocking call; we offload it to a thread via
                # asyncio.run_coroutine_threadsafe so the listener
                # callback (which runs on a zeroconf-internal thread)
                # never blocks the asyncio loop.
                def add_service(self, zc, type_, name):
                    try:
                        info = zc.get_service_info(type_, name)
                    except Exception as exc:
                        logger.warning("mDNS get_service_info failed for %s: %s", name, exc)
                        return
                    self._record(info)

                def remove_service(self, zc, type_, name):
                    pass

                def update_service(self, zc, type_, name):
                    pass

                def _record(self, info):
                    if info and info.properties:
                        peer_id = info.properties.get(b"node_id", b"").decode()
                        if peer_id and peer_id != engine.node_id:
                            peer_addr = (
                                socket.inet_ntoa(info.addresses[0])
                                if info.addresses else ""
                            )
                            engine._peers[peer_id] = {
                                "address": peer_addr,
                                "port": info.port,
                                "discovered_at": time.time(),
                                "source": "mdns",
                            }
                            logger.info(
                                "Discovered peer: %s at %s:%d",
                                peer_id, peer_addr, info.port,
                            )

            class AsyncPeerListener(PeerListener):
                # Async-API listener: zeroconf calls back via
                # `add_service` on an asyncio task. We resolve the
                # service info via the async API so the loop stays
                # responsive even on slow networks.
                def add_service(self, zc, type_, name):
                    engine._track_bg_task(
                        asyncio.create_task(
                            self._async_resolve(zc, type_, name),
                            name=f"mdns-resolve-{name}",
                        )
                    )

                async def _async_resolve(self, zc, type_, name):
                    # `zc` here is the inner sync `Zeroconf` instance
                    # that `AsyncServiceBrowser` passes to handler
                    # callbacks. `AsyncServiceInfo.async_request` takes
                    # that Zeroconf directly.
                    try:
                        info = AsyncServiceInfo(type_, name)
                        ok = await info.async_request(zc, 3000)
                        if ok:
                            self._record(info)
                    except Exception as exc:
                        logger.warning(
                            "mDNS async resolve failed for %s: %s", name, exc,
                        )

            if have_async:
                self._zeroconf = AsyncZeroconf()
                async_info = AsyncServiceInfo(
                    self._service_info.type,
                    self._service_info.name,
                    addresses=list(self._service_info.addresses),
                    port=self._service_info.port,
                    properties=self._service_info.properties,
                )
                await self._zeroconf.async_register_service(async_info)
                logger.info(
                    "mDNS service registered (async): %s at %s:%d",
                    self.node_id, ip, SYNC_PORT,
                )
                self._service_browser = AsyncServiceBrowser(
                    self._zeroconf.zeroconf,
                    SERVICE_TYPE,
                    handlers=AsyncPeerListener(),
                )
            else:
                # Sync zeroconf via executor — the registration call
                # itself blocks for ~100-500ms while it sends gratuitous
                # announcements, so off-load it.
                from zeroconf import Zeroconf, ServiceBrowser

                loop = asyncio.get_running_loop()

                def _sync_register():
                    zc = Zeroconf()
                    zc.register_service(self._service_info)
                    return zc

                self._zeroconf = await loop.run_in_executor(None, _sync_register)
                logger.info(
                    "mDNS service registered: %s at %s:%d",
                    self.node_id, ip, SYNC_PORT,
                )
                self._service_browser = ServiceBrowser(
                    self._zeroconf, SERVICE_TYPE, PeerListener(),
                )

            mdns_ok = True
            self._running = True

            # Schedule a fallback check: if no mDNS peers found after timeout, add static peers
            if SYNC_PEERS:
                asyncio.get_event_loop().call_later(
                    _MDNS_DISCOVERY_TIMEOUT,
                    self._check_mdns_fallback,
                )

        except ImportError:
            logger.info("zeroconf not installed — mDNS discovery disabled. Install with: pip install zeroconf")
        except Exception as e:
            # Always include the exception class so a blank message
            # doesn't show up as `mDNS discovery failed:` with nothing
            # after the colon. INFO when there is no concrete error
            # text (typically "no networks available" on single-machine
            # boots), WARNING when there is something to look at.
            detail = str(e) or repr(e)
            level = logger.warning if detail else logger.info
            level("mDNS discovery skipped: %s (%s)", detail or "no peers", type(e).__name__)

        if not mdns_ok:
            self._running = True
            if SYNC_PEERS:
                logger.info("Using static peer list as primary discovery method")
                self._load_static_peers()
            else:
                # Single-machine setups have no peers by design --
                # advertise that, don't alarm.
                logger.info("Sync is local-only (no mDNS peers, no static peers configured).")

    def _check_mdns_fallback(self):
        """Called after mDNS timeout; adds static peers if no mDNS peers were found."""
        mdns_peers = [p for p in self._peers.values() if p.get("source") == "mdns"]
        if not mdns_peers:
            logger.warning(
                "No mDNS peers discovered within %ds — falling back to static peer list (%d entries)",
                _MDNS_DISCOVERY_TIMEOUT, len(SYNC_PEERS),
            )
            self._load_static_peers()

    async def stop_discovery(self):
        """Tear down mDNS registration without blocking the event loop.

        Audit-r9: now `start_discovery` may have produced either a sync
        ``Zeroconf`` (older installs) or an ``AsyncZeroconf``. Detect
        which one and use the appropriate close path; both run via
        ``asyncio.to_thread`` / native await so the FastAPI shutdown
        coroutine never blocks the loop.
        """
        self._running = False
        zc = self._zeroconf
        info = self._service_info
        browser = self._service_browser
        self._zeroconf = None
        self._service_info = None
        self._service_browser = None
        if zc is None:
            return

        # Async path — `AsyncZeroconf` exposes `async_unregister_all_services`
        # and `async_close`. Browser cleanup is async too.
        if hasattr(zc, "async_close"):
            try:
                if browser is not None and hasattr(browser, "async_cancel"):
                    try:
                        await asyncio.wait_for(browser.async_cancel(), timeout=2.0)
                    except Exception as exc:
                        logger.debug("SyncEngine.stop_discovery browser cancel: %s", exc)
                try:
                    await asyncio.wait_for(
                        zc.async_unregister_all_services(), timeout=2.0,
                    )
                except Exception as exc:
                    logger.debug("SyncEngine.stop_discovery unregister: %s", exc)
                await asyncio.wait_for(zc.async_close(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("SyncEngine.stop_discovery: AsyncZeroconf close timed out")
            except Exception as exc:
                logger.debug("SyncEngine.stop_discovery: %s", exc)
            return

        # Sync path — offload to worker thread.
        def _sync_close():
            try:
                if info is not None:
                    zc.unregister_service(info)
            except Exception as exc:
                logger.debug("sync engine unregister_service failed: %s", exc)
            try:
                zc.close()
            except Exception as exc:
                logger.debug("sync engine zeroconf.close failed: %s", exc)

        try:
            await asyncio.wait_for(asyncio.to_thread(_sync_close), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("SyncEngine.stop_discovery: zeroconf close timed out after 3s")
        except Exception as exc:
            logger.debug("SyncEngine.stop_discovery: %s", exc)

    async def sync_with_peer(
        self,
        peer_id: str,
        *,
        max_attempts: int = 3,
        connect_timeout: float = 5.0,
        handshake_timeout: float = 5.0,
        backoff_base: float = 1.0,
    ) -> dict:
        """Initiate a sync session with a discovered peer.

        Wraps the handshake + exchange in retry-with-backoff. Each attempt
        is bounded by connect_timeout + handshake_timeout so a peer that
        accepts the TCP connection then drops the websocket mid-handshake
        cannot stall the engine indefinitely.

        Returns:
            On success: {"success": True, "sent": N, "received": M, "peer": ..., "attempts": k}
            On disk full: {"success": False, "error": "disk_full", "io_paused": True}
            On exhausted retries: {"success": False, "error": str, "attempts": max_attempts}
        """
        peer = self._peers.get(peer_id)
        if not peer:
            return {"success": False, "error": f"Peer {peer_id} not found"}

        if self._io_paused:
            return {
                "success": False,
                "error": "io_paused",
                "io_paused": True,
                "reason": self._io_pause_reason,
            }

        lock = self._peer_locks.setdefault(peer_id, asyncio.Lock())

        t0 = time.time()
        last_err: Optional[BaseException] = None

        async with lock:
            for attempt in range(1, max_attempts + 1):
                try:
                    return await self._handshake_and_exchange(
                        peer_id, peer,
                        connect_timeout=connect_timeout,
                        handshake_timeout=handshake_timeout,
                        attempt=attempt,
                        started_at=t0,
                    )
                except SyncDiskFullError as exc:
                    self._io_paused = True
                    self._io_pause_reason = "disk_full"
                    logger.warning(
                        "Sync aborted by disk_full: peer=%s attempt=%d err=%s",
                        peer_id, attempt, exc,
                    )
                    return {
                        "success": False,
                        "error": "disk_full",
                        "io_paused": True,
                        "attempts": attempt,
                    }
                except ImportError:
                    return {"success": False, "error": "websockets not installed"}
                except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                    last_err = exc
                    logger.warning(
                        "Sync timeout: peer=%s attempt=%d/%d", peer_id, attempt, max_attempts,
                    )
                except Exception as exc:
                    last_err = exc
                    logger.warning(
                        "Sync handshake failed: peer=%s attempt=%d/%d err=%s",
                        peer_id, attempt, max_attempts, exc,
                    )

                if attempt < max_attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))

        elapsed_ms = (time.time() - t0) * 1000
        logger.warning(
            "Sync failed after %d attempts: peer=%s err=%s elapsed_ms=%.1f",
            max_attempts, peer_id, last_err, elapsed_ms,
        )
        return {
            "success": False,
            "error": str(last_err) if last_err is not None else "unknown",
            "attempts": max_attempts,
        }

    async def _handshake_and_exchange(
        self,
        peer_id: str,
        peer: dict,
        *,
        connect_timeout: float,
        handshake_timeout: float,
        attempt: int,
        started_at: float,
    ) -> dict:
        """One attempt: connect, exchange vector clocks, swap changes.

        Always closes the websocket via `async with ws:` so a kill at any
        point in the handshake leaves no orphaned task and no lingering
        socket handle.
        """
        import websockets

        addr = peer["address"]
        port = peer["port"]
        client_ssl = build_client_ssl_context()
        scheme = "wss" if client_ssl else "ws"
        uri = f"{scheme}://{addr}:{port}/sync"

        ws = await asyncio.wait_for(
            websockets.connect(uri, ssl=client_ssl),
            timeout=connect_timeout,
        )

        async with ws:
            await asyncio.wait_for(
                ws.send(json.dumps({
                    "type": "sync_request",
                    "node_id": self.node_id,
                    "vector_clock": self.get_vector_clock(),
                    "passphrase": SYNC_PASSPHRASE,
                })),
                timeout=handshake_timeout,
            )

            resp_raw = await asyncio.wait_for(ws.recv(), timeout=handshake_timeout)
            resp = json.loads(resp_raw)

            if resp.get("type") == "sync_error":
                return {"success": False, "error": resp.get("message", "rejected")}

            remote_vc = resp.get("vector_clock", {})
            peer_has = remote_vc.get(self.node_id, "0:0:")
            changes_for_peer = self._wal.get_changes_since(peer_has, exclude_node=peer_id)

            await asyncio.wait_for(
                ws.send(json.dumps({
                    "type": "sync_data",
                    "changes": [op.to_dict() for op in changes_for_peer],
                })),
                timeout=handshake_timeout,
            )

            remote_raw = await asyncio.wait_for(ws.recv(), timeout=handshake_timeout)
            remote_changes_msg = json.loads(remote_raw)
            remote_changes = remote_changes_msg.get("changes", [])
            applied = await self.apply_remote_changes(remote_changes)

            # Record per-peer delivery. ``SyncWAL.mark_synced`` had zero
            # callers anywhere in the tree (audit 2026-08-12): every one
            # of the 16,184 operations in the real store's WAL carried
            # ``synced_to = '[]'``, not because sync had never run but
            # because the column was unwritable by construction. That
            # made "which peers have this op?" unanswerable and left the
            # WAL with no basis on which it could ever be pruned.
            # Marked only after the peer's own change set came back,
            # which is the point at which it has demonstrably processed
            # ours. Failures here are logged, never fatal: the exchange
            # itself already succeeded and HLC, not this column, is what
            # drives what gets sent next time.
            if changes_for_peer:
                try:
                    await asyncio.to_thread(
                        self._wal.mark_synced_many,
                        [op.op_id for op in changes_for_peer],
                        peer_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "mark_synced_many failed for peer=%s over %d op(s): %s",
                        peer_id, len(changes_for_peer), exc,
                    )

            elapsed_ms = (time.time() - started_at) * 1000
            logger.info(
                "Sync complete: peer=%s ops_sent=%d ops_received=%d elapsed_ms=%.1f attempt=%d tls=%s",
                peer_id, len(changes_for_peer), applied, elapsed_ms, attempt, bool(client_ssl),
            )

            return {
                "success": True,
                "sent": len(changes_for_peer),
                "received": applied,
                "peer": peer_id,
                "attempts": attempt,
            }

    def export_to_bundle(self) -> dict:
        """Export all memory for manual sync (USB, AirDrop)."""
        bundle = {
            "node_id": self.node_id,
            "vector_clock": self.get_vector_clock(),
            "operations": self.get_changes_since("0:0:"),
            "exported_at": time.time(),
        }
        return bundle

    async def import_from_bundle(self, bundle: dict) -> int:
        """Import a memory bundle from another node."""
        changes = bundle.get("operations", [])
        return await self.apply_remote_changes(changes)

    @property
    def stats(self) -> dict:
        return {
            "node_id": self.node_id,
            "peers": list(self._peers.keys()),
            "peer_count": len(self._peers),
            "wal_entries": self._wal.count,
            "vector_clock": self.get_vector_clock(),
            "running": self._running,
        }
