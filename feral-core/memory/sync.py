"""
FERAL Federated Memory — CRDT-based P2P sync
===============================================
Replicates memory across FERAL instances on the local network.
No cloud relay — all sync is peer-to-peer via mDNS discovery.

Protocol:
  1. mDNS discovery: find peers advertising _feral._tcp.local.
  2. WebSocket handshake with a per-peer grant when one has been
     enrolled (``security/peer_roster.py``), falling back to the shared
     passphrase for peers that have not migrated yet
  3. Exchange vector clocks to determine missing operations
  4. Send missing ops → merge via CRDT rules
  5. Periodic heartbeat to detect disconnections

Conflict resolution:
  - Notes/Knowledge: last-writer-wins (by HLC timestamp)
  - Episodes: union (never delete remote episodes)
  - Execution log: append-only
  - Deletes: tombstoned in ``memory.db``'s ``sync_tombstones``. The
    hard DELETE takes the row's ``hlc_string`` with it, so the
    tombstone is what the LWW gate compares against afterwards. See
    ``MemoryStore.prune_tombstones`` for the retention trade-off.

Scoped sharing:
  Replication is NOT all-or-nothing. Every WAL operation carries a
  ``scope``, every peer is granted a set of scopes on the roster, and
  an operation crosses only if its scope is in that peer's set. The
  default everywhere is ``private``, which never replicates: an
  unscoped write, a pre-existing WAL row, a scope name that does not
  parse, and an operation from a peer running an older build are all
  private, and all stay put. See ``security/sync_scopes.py`` for the
  vocabulary and ``PeerRoster.grant_scope`` for the grants.

  Both directions are enforced, on this brain, from this brain's
  roster. ``SyncWAL.get_changes_for_peer`` filters what is sent;
  ``SyncEngine.apply_remote_changes_from_peer`` re-checks what
  arrives. The receive check is not redundant: the send filter runs
  on whichever brain is sending, and the peer's brain belongs to
  somebody else.

  Revoking a scope stops FUTURE replication in it. It does not and
  cannot recall operations that already crossed; those live on a disk
  this brain does not control.
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
from security.sync_scopes import (
    DENY_ALL,
    INHERIT as _SCOPE_INHERIT,
    PRIVATE,
    normalise_scope,
    normalise_scope_set,
)

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


# ── Wire framing ────────────────────────────────────────────────────────
#
# The change set used to go out as ONE websocket frame. ``websockets``
# defaults ``max_size`` to 1 MiB on both ends, so any peer holding more
# than a megabyte of history was unsyncable: the reader closed the
# connection with 1009 (message too big), sync_with_peer burned its
# retries, backed off, and retried forever. Measured on the owner's live
# WAL at the time of the fix: 16,324 operations, 6.24 MiB in the ``data``
# column alone, before the per-op envelope.
#
# Raising ``max_size`` alone is not a fix. The WAL grows with every
# write, so any constant is a deadline rather than a bound. The exchange
# is chunked instead, and the limits below are the belt to that braces.
#
# SYNC_MAX_FRAME_BYTES is the SENDER's budget per frame. 256 KiB sits an
# order of magnitude under the stock 1 MiB reader default, which means a
# FERAL brain can still sync with a peer that never raised its own
# receive limit, including an older build of FERAL itself.
SYNC_MAX_FRAME_BYTES = int(os.getenv("FERAL_SYNC_MAX_FRAME_BYTES", str(256 * 1024)))

# The RECEIVER's per-frame ceiling, passed to ``websockets.connect``.
# Larger than the sender's budget because one operation is indivisible:
# a single episode with a multi-megabyte ``detail`` has to cross in one
# frame or not at all. An op larger than this is undeliverable and says
# so in the log rather than wedging the exchange.
SYNC_MAX_RECV_BYTES = int(os.getenv("FERAL_SYNC_MAX_RECV_BYTES", str(8 * 1024 * 1024)))

# Cap on how many operations one exchange will accept before it gives
# up. Chunking means the reader now loops on ``recv``; without a bound a
# peer could stream frames at it indefinitely. 500k is ~30x the owner's
# entire WAL, so it can only be reached by something pathological.
SYNC_MAX_OPS_PER_EXCHANGE = int(
    os.getenv("FERAL_SYNC_MAX_OPS_PER_EXCHANGE", str(500_000))
)

# ── WAL retention ───────────────────────────────────────────────────────
#
# See :meth:`SyncWAL.prune` for the trade-off this number encodes.
WAL_MAX_AGE_SECONDS = float(
    os.getenv("FERAL_SYNC_WAL_MAX_AGE_SECONDS", str(90 * 24 * 60 * 60))
)

# How many origins a peer's vector clock may carry before
# ``SyncWAL.get_changes_for_peer`` stops expressing one SQL clause per
# origin. 150 origins is 750 bind variables, still inside the 999
# SQLITE_MAX_VARIABLE_NUMBER floor, with room for the rest of the query.
_MAX_VC_CLAUSES = 150


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

    This is ONE secret shared by every peer, which is the whole reason
    ``security/peer_roster.py`` exists. It remains the fallback so an
    existing two-brain setup keeps working across the upgrade; enrol
    each peer with ``feral sync peer invite`` and then set
    ``FERAL_SYNC_REQUIRE_PEER_IDENTITY=1`` to retire it.
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
    """A single write operation to be replicated.

    ``scope`` is the sharing boundary. It defaults to
    :data:`~security.sync_scopes.PRIVATE`, which never replicates, so an
    operation constructed by code that has never heard of scopes stays
    on the brain that wrote it. Every construction path in this module
    normalises through ``normalise_scope``; nothing trusts the field
    verbatim, least of all when it arrived from a peer.
    """
    op_id: str
    table: str
    op_type: str  # "insert", "update", "delete"
    row_id: str
    data: dict
    hlc: str
    origin_node: str
    timestamp: float = field(default_factory=time.time)
    scope: str = PRIVATE

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
            "scope": self.scope,
        }

    @staticmethod
    def from_dict(d: dict) -> "SyncOperation":
        """Decode one operation off the wire.

        Unknown keys are DROPPED rather than splatted into the
        constructor, and ``scope`` is re-normalised rather than
        trusted. Both matter because the dict on this path came from a
        brain somebody else owns: the old ``SyncOperation(**d)`` handed
        a peer a ``TypeError`` primitive over any unexpected key, and a
        peer that could name the scope field freely could name a scope
        that does not parse and hope the comparison went its way. Every
        unrecognised or malformed scope resolves to
        :data:`~security.sync_scopes.PRIVATE`, which no grant can ever
        contain, so it is refused at the receive check.
        """
        fields = {
            "op_id", "table", "op_type", "row_id",
            "data", "hlc", "origin_node", "timestamp",
        }
        kwargs = {k: v for k, v in d.items() if k in fields}
        return SyncOperation(**kwargs, scope=normalise_scope(d.get("scope")))


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


class SyncFrameOverflowError(RuntimeError):
    """A peer streamed more operations in one exchange than
    ``SYNC_MAX_OPS_PER_EXCHANGE`` allows."""


def sync_data_frames(ops: list) -> list[dict]:
    """Split a change set into ``sync_data`` messages, none of which
    serialises above :data:`SYNC_MAX_FRAME_BYTES`.

    Always returns at least one message, so "I have nothing for you" is
    still an explicit end-of-stream rather than a silence the reader has
    to time out on.

    ``more`` marks every message but the last. A peer running the
    pre-chunking build sends one message with no ``more`` key at all,
    which reads as falsy and terminates the loop after one frame, so
    new and old builds still interoperate, at the old capacity.

    An operation that alone exceeds the budget gets a frame to itself.
    Operations are indivisible, so the alternative is to drop it. The
    reader's ``max_size`` is :data:`SYNC_MAX_RECV_BYTES`, well above the
    sender's budget, precisely to leave room for this case.
    """
    batches: list[list[dict]] = []
    current: list[dict] = []
    # {"type":"sync_data","changes":[],"seq":0,"more":false} and slack
    # for a 6-digit seq.
    envelope = 64
    size = envelope
    for op in ops:
        d = op.to_dict() if hasattr(op, "to_dict") else op
        cost = len(json.dumps(d)) + 1
        if current and size + cost > SYNC_MAX_FRAME_BYTES:
            batches.append(current)
            current = []
            size = envelope
        current.append(d)
        size += cost
    batches.append(current)

    last = len(batches) - 1
    return [
        {"type": "sync_data", "changes": b, "seq": i, "more": i < last}
        for i, b in enumerate(batches)
    ]


class SyncProtocolMessage(Exception):
    """A non-``sync_data`` message interrupted a chunked read.

    Carries the message so the caller can report the peer's own error
    text rather than a generic protocol failure.
    """

    def __init__(self, message):
        self.message = message
        super().__init__(str(message))


async def recv_sync_data(recv_message) -> list[dict]:
    """Read a chunked change set: keep receiving until a message that
    does not set ``more``.

    ``recv_message`` is an awaitable callable returning one already
    decoded message dict, so this works for both the client
    (``json.loads(await ws.recv())``) and the FastAPI server
    (``await ws.receive_json()``).

    Anything that is not a ``sync_data`` message ends the read and is
    returned to the caller's own handling via
    :class:`SyncProtocolMessage`; in practice that is ``sync_error``.
    """
    changes: list[dict] = []
    while True:
        msg = await recv_message()
        if not isinstance(msg, dict):
            raise SyncProtocolMessage(msg)
        if msg.get("type") not in (None, "sync_data"):
            raise SyncProtocolMessage(msg)
        batch = msg.get("changes") or []
        if not isinstance(batch, list):
            batch = []
        changes.extend(batch)
        if len(changes) > SYNC_MAX_OPS_PER_EXCHANGE:
            raise SyncFrameOverflowError(
                f"peer streamed more than {SYNC_MAX_OPS_PER_EXCHANGE} ops in one exchange"
            )
        if not msg.get("more"):
            return changes


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
        # This file holds full note bodies and episode text as JSON row
        # payloads, so it is inside the at-rest envelope with
        # memory.db. Decrypt a sync_wal.db.enc if that is all there is,
        # then chmod 0600 whatever we end up opening. Both calls are
        # best-effort by contract: prepare_sync_wal_for_boot never
        # raises, and harden_db_mode logs rather than blocking the
        # open. See memory/at_rest.py for the failure posture.
        from memory.at_rest import harden_db_mode, prepare_sync_wal_for_boot

        wal_file = Path(self._db_path)
        prepare_sync_wal_for_boot(wal_file)
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
        # The HLC's numeric parts, split out of the string.
        #
        # ``hlc`` is "wall_ms:counter:node_id", and lexicographic order
        # over that is NOT HLC order: "999:0:n" sorts above "1000:0:n"
        # because wall_ms is variable width. That is why the original
        # ``get_changes_since`` pulled the whole table and filtered in
        # Python: ``idx_wal_hlc`` was on the string and unusable for a
        # range scan. Measured cost of that choice: 2,961 ms to return
        # ZERO new operations at 500k rows.
        #
        # Splitting the two integers out makes the comparison numeric,
        # which SQLite can index. Within one ``origin_node`` the HLC's
        # own node_id is constant, so (wall, counter) is the complete
        # ordering: the third tuple element only ever breaks ties
        # BETWEEN nodes, which a per-origin range never spans.
        existing = {r[1] for r in conn.execute("PRAGMA table_info(sync_wal)")}
        # The sharing boundary, added as a column rather than migrated
        # onto the eight source tables because the WAL *is* the
        # replication boundary: nothing reaches a peer except through a
        # row of this table, so one column here is the complete
        # enforcement surface.
        #
        # ``DEFAULT 'private'`` is the whole fail-closed story for
        # history. Every operation written before this column existed
        # gets the never-replicate scope, permanently and without a
        # backfill pass that could get it wrong. That is a deliberate
        # one-way door: pre-existing memory is NOT retroactively
        # classified, and re-scoping it later means a migration onto
        # the source tables, not a rewrite of this column. The
        # alternative, defaulting to a shareable scope, would take an
        # operator's entire personal history and mark it poolable
        # because of a schema change they never asked for.
        if "scope" not in existing:
            conn.execute(
                f"ALTER TABLE sync_wal ADD COLUMN scope TEXT NOT NULL DEFAULT '{PRIVATE}'"
            )
        # Indexed because every send-side query now carries an ``IN``
        # over the peer's granted scopes on top of the HLC range.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wal_scope ON sync_wal(scope)")
        if "hlc_wall" not in existing:
            conn.execute("ALTER TABLE sync_wal ADD COLUMN hlc_wall INTEGER NOT NULL DEFAULT 0")
        if "hlc_counter" not in existing:
            conn.execute("ALTER TABLE sync_wal ADD COLUMN hlc_counter INTEGER NOT NULL DEFAULT 0")
        if "hlc_wall" not in existing or "hlc_counter" not in existing:
            # Backfill. An existing WAL has every row at the 0 default,
            # which would make every op look older than every watermark
            # and silently stop replication. Done in one statement so a
            # crash mid-backfill leaves the transaction unapplied and
            # the next boot retries it.
            conn.execute(
                "UPDATE sync_wal SET "
                "hlc_wall = CAST(substr(hlc, 1, instr(hlc, ':') - 1) AS INTEGER), "
                "hlc_counter = CAST("
                "  substr(hlc, instr(hlc, ':') + 1,"
                "         instr(substr(hlc, instr(hlc, ':') + 1), ':') - 1)"
                " AS INTEGER) "
                "WHERE instr(hlc, ':') > 0"
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wal_hlc ON sync_wal(hlc)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wal_origin ON sync_wal(origin_node)")
        # The index the exchange path actually uses: a per-origin range
        # scan over the numeric HLC.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wal_origin_hlc "
            "ON sync_wal(origin_node, hlc_wall, hlc_counter)"
        )
        # Global HLC order, for the single-watermark query and to let
        # both queries satisfy ORDER BY from the index instead of a temp
        # B-tree. Verified with EXPLAIN QUERY PLAN on the live WAL:
        # without it, "what is new since X" is
        # ``SCAN sync_wal`` + ``USE TEMP B-TREE FOR ORDER BY``; with it,
        # ``SEARCH sync_wal USING INDEX idx_wal_hlc_num (hlc_wall>?)``.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wal_hlc_num ON sync_wal(hlc_wall, hlc_counter)"
        )
        # Retention sweeps by age.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wal_timestamp ON sync_wal(timestamp)")
        conn.commit()
        conn.close()
        # After the file exists. SQLite creates it 0666 & ~umask, which
        # is 0644 on a default install: two audits found this file
        # world readable with 6.24 MB of plaintext row payloads in it.
        harden_db_mode(wal_file)

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
                    wall, counter, _ = _parse_hlc(op.hlc)
                    # ``normalise_scope`` again, at the last point
                    # before the value becomes durable. The op may have
                    # been built anywhere; what gets persisted is only
                    # ever a name this process could parse, so a stored
                    # scope can never be a string the send filter has
                    # to reason about specially.
                    conn.execute(
                        "INSERT OR REPLACE INTO sync_wal (op_id, table_name, op_type, row_id, data, hlc, origin_node, timestamp, hlc_wall, hlc_counter, scope) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (op.op_id, op.table, op.op_type, op.row_id, json.dumps(op.data), op.hlc, op.origin_node, op.timestamp, wall, counter, normalise_scope(op.scope)),
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

    _SELECT_COLS = (
        "SELECT op_id, table_name, op_type, row_id, data, hlc, origin_node, "
        "timestamp, scope FROM sync_wal"
    )

    @staticmethod
    def _rows_to_ops(rows) -> list[SyncOperation]:
        return [
            SyncOperation(
                op_id=r[0], table=r[1], op_type=r[2], row_id=r[3],
                data=json.loads(r[4]), hlc=r[5], origin_node=r[6], timestamp=r[7],
                # Re-normalised on the way out as well as on the way
                # in. The WAL is a file on disk that an operator, a
                # restore, or a partially-applied migration can leave
                # holding anything; a row whose scope no longer parses
                # reads back as private and stops replicating, which is
                # the failure direction we want.
                scope=normalise_scope(r[8]),
            )
            for r in rows
        ]

    @staticmethod
    def _scope_clause(allowed_scopes) -> tuple[str, list]:
        """SQL fragment restricting a query to a peer's granted scopes.

        ``allowed_scopes=None`` means "no peer boundary here" and adds
        no clause. That value is for LOCAL callers only: bundle export
        of your own brain, and tests. Every peer-facing path passes a
        real set, and :meth:`get_changes_for_peer` makes the argument
        mandatory precisely so that a peer path cannot reach the
        unrestricted form by forgetting a keyword.

        A real but EMPTY set is the common case (a peer nobody has
        granted anything) and must select zero rows, not all of them.
        It is rendered as a clause that no row satisfies rather than
        short-circuited in Python, so the "empty means everything"
        SQL-building bug has nowhere to live.
        """
        if allowed_scopes is None:
            return "", []
        scopes = sorted(normalise_scope_set(allowed_scopes))
        if not scopes:
            return " AND 0", []
        placeholders = ",".join("?" * len(scopes))
        return f" AND scope IN ({placeholders})", list(scopes)

    def scope_for_row(self, table: str, row_id: str) -> str:
        """The scope of the newest logged operation for one row.

        Deletes use this to inherit the scope their row was written
        under, so a delete travels exactly as far as the insert did and
        never further. A row with no surviving WAL operation (pruned,
        or written before this column existed) resolves to
        :data:`~security.sync_scopes.PRIVATE`: the delete then stays
        local, which leaves a peer holding a copy it already had rather
        than announcing the existence of a row we never shared.
        """
        try:
            conn = sqlite3.connect(self._db_path)
        except sqlite3.Error:
            return PRIVATE
        try:
            row = conn.execute(
                "SELECT scope FROM sync_wal WHERE table_name = ? AND row_id = ? "
                "ORDER BY hlc_wall DESC, hlc_counter DESC LIMIT 1",
                (table, row_id),
            ).fetchone()
        except sqlite3.Error:
            return PRIVATE
        finally:
            conn.close()
        return normalise_scope(row[0]) if row else PRIVATE

    def get_changes_since(
        self,
        hlc: str,
        exclude_node: str = "",
        *,
        allowed_scopes=None,
    ) -> list[SyncOperation]:
        """Every operation strictly newer than one global HLC watermark.

        A single watermark across all origins is only correct when the
        caller genuinely means "everything after this point in my own
        log", which is bundle export and the tests. The exchange wants
        :meth:`get_changes_for_peer` instead, because one watermark
        cannot express what a peer has of each *other* node's writes.

        ``allowed_scopes`` defaults to ``None``, meaning "no peer
        boundary". That default is correct for the callers this method
        has (local bundle export, tests) and WRONG for anything that
        speaks to a peer, which is why no peer path calls it directly:
        :meth:`get_changes_for_peer` requires the argument and threads
        it into every internal use of this method.

        The filter runs in SQL against the numeric HLC columns. It used
        to be a Python comprehension over ``fetchall()`` of the entire
        table: 2,961 ms to return zero rows at 500k rows in the WAL,
        every 30 seconds, per peer.
        """
        wall, counter, _ = _parse_hlc(hlc)
        # The leading ``hlc_wall >= ?`` is what makes this seekable. A
        # bare ``(a > ? OR (a = ? AND b > ?))`` is opaque to the query
        # planner and degrades to a full scan; the redundant lower bound
        # gives it a range to open the index on. Confirmed against the
        # live WAL with EXPLAIN QUERY PLAN.
        sql = (
            f"{self._SELECT_COLS} "
            "WHERE hlc_wall >= ? AND (hlc_wall > ? OR hlc_counter > ?)"
        )
        params: list = [wall, wall, counter]
        if exclude_node:
            sql += " AND origin_node != ?"
            params.append(exclude_node)
        scope_sql, scope_params = self._scope_clause(allowed_scopes)
        sql += scope_sql
        params.extend(scope_params)
        sql += " ORDER BY hlc_wall, hlc_counter"
        conn = sqlite3.connect(self._db_path)
        try:
            return self._rows_to_ops(conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    def get_changes_for_peer(
        self,
        remote_vc: dict,
        *,
        allowed_scopes,
        exclude_node: str = "",
        limit: Optional[int] = None,
    ) -> list[SyncOperation]:
        """Every operation the peer is missing IN THE SCOPES IT WAS
        GRANTED, judged PER ORIGIN.

        ``allowed_scopes`` is keyword-only and has NO DEFAULT. That is
        the point: this is the send-side enforcement of scoped sharing,
        and a default would be a way to reach the unfiltered form by
        omission. Pass ``roster.granted_scopes(peer_node_id)``. The
        empty set is a legitimate value and means the peer receives
        nothing, which is what an authenticated peer with no grants is
        entitled to.

        A send filter alone is not the whole enforcement, and cannot
        be. It runs on this brain, so it governs what an honest local
        build transmits; it says nothing about what a modified peer
        sends back. The other half is
        :meth:`SyncEngine.apply_remote_changes_from_peer`, which
        re-checks every arriving operation against the same grant set.
        The two together are what makes the boundary hold when the
        brain on the other end is owned by somebody else.

        This is the correct reading of a vector clock and the fix for a
        defect that lost data silently. The exchange used to cut the
        change set at ``remote_vc[self.node_id]``, the peer's high-water
        mark for MY OWN writes, and apply it as the cutoff for
        operations of every origin. Two consequences, both measured:

        - A relay node that also writes locally never forwarded a
          third party's operation older than its own last write. The
          op was not delayed; it was filtered out of every future
          exchange, permanently, with nothing logged.
        - A node that rarely writes has no entry in the peer's clock,
          so its cutoff stayed at ``"0:0:"`` and it re-sent its entire
          WAL every 30 seconds forever.

        ``remote_vc`` maps origin node_id to the newest HLC that peer
        has seen from that origin. An origin absent from the map is one
        the peer has never heard of, so everything from it is new.

        ``exclude_node`` must be the peer's REAL node_id, from the
        handshake response. ``_load_static_peers`` keys peers as
        ``f"static-{host}:{port}"``, which matches no ``origin_node``,
        so passing the local dictionary key made the filter a no-op and
        echoed the peer's own operations back at it.
        """
        watermarks: dict[str, tuple] = {}
        for origin, watermark in (remote_vc or {}).items():
            if not isinstance(origin, str) or not isinstance(watermark, str):
                continue
            wall, counter, _ = _parse_hlc(watermark)
            watermarks[origin] = (wall, counter)

        if not watermarks:
            return self.get_changes_since(
                "0:0:", exclude_node=exclude_node, allowed_scopes=allowed_scopes,
            )[: limit if limit is not None else None]

        # Each origin costs 5 bind variables (4 in its own clause, 1 in
        # the catch-all NOT IN), so the clause form is only viable while
        # the peer's clock stays well inside SQLITE_MAX_VARIABLE_NUMBER,
        # which is 999 on the conservative builds this ships against.
        # A long-lived fleet accumulates node ids: the owner's live WAL
        # carries 113 distinct origins because a regenerated
        # ``sync_node_id`` mints a new one. Past the threshold, fall
        # back to a coarse SQL prefilter at the OLDEST watermark and
        # apply the exact per-origin rule in Python. That is a superset
        # narrowed correctly, never a different answer.
        if len(watermarks) > _MAX_VC_CLAUSES:
            floor = min(watermarks.values())
            coarse = self.get_changes_since(
                f"{floor[0]}:{floor[1]}:",
                exclude_node=exclude_node,
                allowed_scopes=allowed_scopes,
            )
            out = []
            for op in coarse:
                wall, counter, _ = _parse_hlc(op.hlc)
                mark = watermarks.get(op.origin_node)
                if mark is None or (wall, counter) > mark:
                    out.append(op)
                    if limit is not None and len(out) >= limit:
                        break
            return out

        clauses: list[str] = []
        params: list = []
        for origin, (wall, counter) in watermarks.items():
            clauses.append(
                "(origin_node = ? AND hlc_wall >= ? AND (hlc_wall > ? OR hlc_counter > ?))"
            )
            params.extend([origin, wall, wall, counter])

        known = list(watermarks)
        placeholders = ",".join("?" * len(known))
        clauses.append(f"origin_node NOT IN ({placeholders})")
        params.extend(known)

        sql = f"{self._SELECT_COLS} WHERE ({' OR '.join(clauses)})"
        if exclude_node:
            sql += " AND origin_node != ?"
            params.append(exclude_node)
        scope_sql, scope_params = self._scope_clause(allowed_scopes)
        sql += scope_sql
        params.extend(scope_params)
        sql += " ORDER BY hlc_wall, hlc_counter"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        conn = sqlite3.connect(self._db_path)
        try:
            return self._rows_to_ops(conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    def prune(self, max_age_seconds: float = WAL_MAX_AGE_SECONDS) -> int:
        """Drop WAL operations older than ``max_age_seconds``. Returns
        the number removed.

        THE TRADE-OFF, stated plainly. Nothing pruned this table before
        this method existed. There was no ``DELETE FROM sync_wal``
        anywhere in the tree, and the owner's live WAL had reached
        16,324 operations and 12 MB across 132 days, growing without
        limit for as long as the brain keeps writing.

        Pruning does NOT delete the user's data. The materialised row
        is in ``memory.db`` with its ``hlc_string`` intact; what a prune
        discards is the ability to REPLICATE that row incrementally.
        The hole it opens is precise: a peer whose watermark predates
        the horizon will never be sent the pruned operations, so a row
        written more than ``max_age_seconds`` ago and never delivered
        to that peer stays undelivered. Recovery is
        ``export_to_bundle`` / ``import_from_bundle`` over a channel
        the operator picks, which is the same answer as for a device
        being re-paired.

        90 days, matching ``MemoryStore.prune_tombstones``, and the
        match is the point rather than a coincidence. A tombstone is
        what stops a peer's stale insert from resurrecting a deleted
        row. If the WAL outlived tombstones, a peer could still be
        shipped a delete whose tombstone this side had already dropped;
        if tombstones outlived the WAL, they would be defending against
        operations no longer capable of arriving. One horizon for both
        keeps the resurrection window a single number.

        What it costs at the observed write rate: 90 days of the
        owner's history is roughly 11,500 operations and 4.3 MiB of
        payload, against 16,324 and 6.24 MiB unbounded-and-climbing.
        The bound is on ninety days of writes, not on the lifetime of
        the brain, which is the property that was missing.

        Deliberately NOT the policy: "prune what every peer has
        acknowledged". ``synced_to`` now records who received an
        operation, but nothing records who is still a peer, so the rule
        never terminates for a device that leaves the fleet. It is the
        same reason ``prune_tombstones`` rejected it, and the same
        missing peer roster.

        Also deliberately NOT the policy: "prune only operations
        superseded by a newer write to the same row". That rule is
        lossless (last-writer-wins means a superseded op can never
        change any peer's state) and it reclaims almost nothing here.
        Measured on the live WAL: 346 of 16,324 operations are
        superseded, 0.07 MiB of 6.24 MiB. The table is 92% ``episodes``,
        which are append-only with unique ids and therefore never
        supersede anything. A lossless rule that frees 2% is not a
        retention policy, so the age horizon is the one that ships and
        the paragraph above is the price of it.
        """
        cutoff = time.time() - max_age_seconds
        with self._write_lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute("DELETE FROM sync_wal WHERE timestamp < ?", (cutoff,))
                conn.commit()
                removed = cur.rowcount or 0
            finally:
                conn.close()
        if removed:
            logger.info("sync.wal_pruned count=%d horizon_s=%.0f", removed, max_age_seconds)
        return removed

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


class PeerListener:
    """mDNS listener for the sync service.

    Defined at module level rather than inside ``start_discovery``. It
    used to be a closure over ``engine``, which meant the only way to
    exercise arrival and departure was to bring up real zeroconf on a
    real network, and that is a large part of why ``remove_service``
    could sit as a bare ``pass`` for as long as it did.

    ``zc.get_service_info(...)`` is blocking; this class is the sync-API
    fallback used only when ``AsyncServiceBrowser`` is unavailable, and
    zeroconf calls it from its own thread.
    """

    def __init__(self, engine: "SyncEngine"):
        self.engine = engine

    def add_service(self, zc, type_, name):
        try:
            info = zc.get_service_info(type_, name)
        except Exception as exc:
            logger.warning("mDNS get_service_info failed for %s: %s", name, exc)
            return
        self._record(info)

    def remove_service(self, zc, type_, name):
        """A peer left the network.

        This was ``pass``. The consequences were not cosmetic: the peer
        stayed in ``SyncEngine._peers`` forever, so the scheduler kept
        dialling a brain that had gone, its per-peer lock leaked, and
        nothing anywhere recorded when it was last seen. "Who is still a
        peer" had no answer, which is the gap
        ``MemoryStore.prune_tombstones`` names in its own docstring.

        Departure is NOT revocation: the peer's grant stays valid and it
        rejoins on the next advertisement. What changes is that the fact
        is now written down, and it survives a restart.
        """
        peer_id = self.engine._service_names.pop(name, "")
        if not peer_id:
            # Either a service we never resolved, or our own
            # advertisement. Nothing to forget.
            return
        self.engine.forget_peer(peer_id, reason="mdns_departure")

    def update_service(self, zc, type_, name):
        pass

    def _record(self, info):
        import socket

        if not info or not info.properties:
            return
        peer_id = info.properties.get(b"node_id", b"").decode()
        if not peer_id or peer_id == self.engine.node_id:
            return
        peer_addr = (
            socket.inet_ntoa(info.addresses[0]) if info.addresses else ""
        )
        self.engine._peers[peer_id] = {
            "address": peer_addr,
            "port": info.port,
            "discovered_at": time.time(),
            "source": "mdns",
        }
        # Remember which mDNS service name maps to which peer, because
        # ``remove_service`` is handed the NAME and nothing else. Without
        # this map a departure cannot be attributed to a peer at all.
        service_name = getattr(info, "name", "") or ""
        if service_name:
            self.engine._service_names[service_name] = peer_id
        self.engine.note_peer_seen(peer_id, peer_addr)
        logger.info(
            "Discovered peer: %s at %s:%s", peer_id, peer_addr, info.port,
        )


class AsyncPeerListener(PeerListener):
    """Async-API listener: zeroconf calls back via ``add_service`` on an
    asyncio task, so the resolve happens through the async API and the
    loop stays responsive on slow networks."""

    def add_service(self, zc, type_, name):
        self.engine._track_bg_task(
            asyncio.create_task(
                self._async_resolve(zc, type_, name),
                name=f"mdns-resolve-{name}",
            )
        )

    async def _async_resolve(self, zc, type_, name):
        # ``zc`` here is the inner sync ``Zeroconf`` instance that
        # ``AsyncServiceBrowser`` passes to handler callbacks.
        # ``AsyncServiceInfo.async_request`` takes that Zeroconf
        # directly.
        try:
            from zeroconf.asyncio import AsyncServiceInfo

            info = AsyncServiceInfo(type_, name)
            ok = await info.async_request(zc, 3000)
            if ok:
                self._record(info)
        except Exception as exc:
            logger.warning("mDNS async resolve failed for %s: %s", name, exc)


class SyncEngine:
    """
    Manages peer-to-peer memory replication.

    Uses mDNS for peer discovery (zeroconf) and WebSocket for data exchange.
    """

    def __init__(self, node_id: str, memory_store=None, db_path: str = None):
        self.node_id = node_id
        self._memory = memory_store
        # Bound explicitly by the brain that owns this engine (see
        # ``set_peer_roster``); ``None`` falls back to the process
        # global, which is correct for the single-brain-per-process
        # deployment FERAL actually ships.
        self._peer_roster = None
        self._hlc = HybridLogicalClock(node_id)
        self._vector_clock = VectorClock()

        # Inbound ops dropped by the clock-drift and malformed-HLC
        # gates in ``apply_remote_changes``. Surfaced via ``stats``
        # so an operator can alert on a peer with a broken clock rather
        # than discovering it later as silently lost writes.
        self._hlc_drift_rejections = 0
        self._hlc_malformed_rejections = 0

        wal_path = db_path or str(feral_data_home() / "sync_wal.db")
        self._wal = SyncWAL(wal_path)

        self._peers: dict[str, dict] = {}
        # mDNS service name -> peer node_id. ``remove_service`` is
        # handed only the service name, so without this map a departure
        # cannot be attributed to a peer and the entry can never be
        # dropped. See ``PeerListener.remove_service``.
        self._service_names: dict[str, str] = {}
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

    #: Sentinel for ``scope=``: take the scope of the row's newest
    #: existing WAL operation. Used by delete emitters so a delete
    #: replicates exactly as far as the write it undoes. Defined in
    #: ``security/sync_scopes.py`` so a caller in ``memory/store.py``
    #: can name it without importing this module.
    SCOPE_INHERIT = _SCOPE_INHERIT

    def _resolve_scope(self, table: str, row_id: str, scope) -> str:
        """Turn a caller's ``scope=`` argument into a stored scope name.

        WHERE SCOPE COMES FROM, and why it is not derived from the
        table. Deriving it (``episodes`` is always shareable, ``notes``
        never, and so on) makes the sharing boundary a property of the
        schema. The first person to add a table then gets whatever
        posture the fallback branch happens to have, silently, and the
        operator has no record of an intent that was never expressed.
        Caller-supplied scope puts the decision at the write, where the
        human intent actually exists, and leaves the WAL row as the
        audit record of what was decided. It also degrades correctly:
        a call site that has not been taught about scopes passes
        nothing and gets ``private``.

        The one exception is a delete, which passes
        :data:`SCOPE_INHERIT`. A delete carries no user intent of its
        own about sharing; it must reach exactly the peers the original
        write reached. Inheriting from the row's newest logged
        operation does that, and an unknown lineage (pruned WAL, or a
        row written before the column existed) resolves to ``private``
        so the delete stays local rather than announcing a row we
        never shared.

        COST. The inherit branch is a synchronous sqlite3 read, and
        ``log_operation_async`` reaches it from the event loop. It is
        on the DELETE path only, and deletes are rare on a real store
        (386 note deletes total on the reference store, against 16,184
        WAL operations), which is the same trade
        ``MemoryStore._record_tombstone`` already takes one call later
        on the same path. No insert ever reaches this branch.
        """
        if isinstance(scope, str) and scope == self.SCOPE_INHERIT:
            return self._wal.scope_for_row(table, row_id)
        return normalise_scope(scope)

    def _build_operation(
        self, table: str, op_type: str, row_id: str, data: dict, scope=None
    ) -> SyncOperation:
        """Mint a SyncOperation with a fresh HLC for *table.row_id*.

        Lifted out of ``log_operation`` so the sync and async paths
        can share the same HLC bump + envelope construction without
        duplicating logic.

        ``scope`` defaults to ``None``, which resolves to
        :data:`~security.sync_scopes.PRIVATE`. Every write that does
        not name a scope is therefore unreplicable, which is the
        posture this whole change is for.
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
            scope=self._resolve_scope(table, row_id, scope),
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

    def log_operation(
        self, table: str, op_type: str, row_id: str, data: dict, scope=None,
    ) -> str:
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
        op = self._build_operation(table, op_type, row_id, data, scope)
        try:
            self._wal.append(op)
        except (SyncDiskFullError, OSError) as exc:
            self._on_wal_append_failure(table, op_type, exc)
            raise  # _on_wal_append_failure already re-raised; defensive
        self._vector_clock.update(self.node_id, op.hlc)
        return op.hlc

    async def log_operation_async(
        self, table: str, op_type: str, row_id: str, data: dict, scope=None,
    ) -> str:
        """Async-safe variant of :meth:`log_operation`.

        Identical semantics (HLC mint + WAL append + vector clock
        update + disk-full pause) but the sqlite3 commit runs on a
        worker thread so the calling coroutine yields control while
        the disk fsync resolves. This is the codepath the chat /
        voice / memory hot paths use.
        """
        op = self._build_operation(table, op_type, row_id, data, scope)
        try:
            await self._wal.append_async(op)
        except (SyncDiskFullError, OSError) as exc:
            self._on_wal_append_failure(table, op_type, exc)
            raise  # defensive
        self._vector_clock.update(self.node_id, op.hlc)
        return op.hlc

    def get_changes_since(self, hlc: str, *, allowed_scopes=None) -> list[dict]:
        """Local view of the log. ``allowed_scopes=None`` means no peer
        boundary, which is correct here because the only production
        caller is :meth:`export_to_bundle` (an operator exporting their
        OWN brain to their own USB stick). Nothing peer-facing calls
        this; the exchange uses ``SyncWAL.get_changes_for_peer``, whose
        scope argument is mandatory."""
        ops = self._wal.get_changes_since(hlc, allowed_scopes=allowed_scopes)
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

    def scopes_for_peer(self, peer_node_id: str) -> frozenset[str]:
        """Scopes this brain shares with one peer, by node_id.

        Fails closed at every step. No roster, a roster that raises, a
        blank node_id, or a peer nobody has granted anything all give
        the empty set, and the empty set means "send nothing, accept
        nothing". There is deliberately no branch anywhere in this
        method that can return a wider set than the roster stated.
        """
        try:
            roster = self._roster()
        except Exception as exc:  # noqa: BLE001, denial is the safe answer
            logger.warning(
                "sync: peer roster unavailable for scope lookup (%s), "
                "denying all scopes for node=%s", exc, peer_node_id,
            )
            return DENY_ALL
        if roster is None:
            return DENY_ALL
        try:
            return roster.granted_scopes(peer_node_id)
        except Exception as exc:  # noqa: BLE001, denial is the safe answer
            logger.warning(
                "sync: scope lookup failed for node=%s (%s), denying all.",
                peer_node_id, exc,
            )
            return DENY_ALL

    async def apply_remote_changes_from_peer(
        self, changes: list[dict], *, peer_node_id: str,
    ) -> int:
        """THE RECEIVE-SIDE ENFORCEMENT POINT. Every peer-facing caller
        uses this; nothing peer-facing calls
        :meth:`apply_remote_changes` directly.

        Why a receive check exists at all when the send filter already
        runs: the send filter runs on the brain that is sending, and
        that brain belongs to somebody else. A peer running a modified
        build can put any scope on any operation, or none. A
        send-side-only filter protects an honest peer from our
        mistakes; it does nothing about a dishonest one, and the entire
        point of federation is that the other brain is not ours. So the
        grant set is applied a second time here, from OUR roster, over
        operations we did not construct.

        ``tests/test_sync_scoped_sharing.py`` asserts that the two
        peer-facing call sites (``api/server.py`` and
        ``_handshake_and_exchange``) route through this method, so the
        wider ``apply_remote_changes`` cannot quietly acquire a third
        peer caller.
        """
        return await self.apply_remote_changes(
            changes, allowed_scopes=self.scopes_for_peer(peer_node_id),
        )

    async def apply_remote_changes(self, changes: list[dict], *, allowed_scopes=None) -> int:
        """Apply operations received from a peer. Returns count of applied ops.

        ``allowed_scopes=None`` means "no peer boundary": the caller is
        local and trusted (``import_from_bundle`` of a bundle the
        operator carried on a USB stick, and tests). Peer traffic must
        NOT reach this form. Use
        :meth:`apply_remote_changes_from_peer`, which resolves the
        grant set from the roster and cannot be called without a peer
        identity.

        v2026.5.34 (PR 2 D12): runs on the asyncio event loop and
        uses MemoryStore's connection pool — the previous code path
        opened a fresh ``sqlite3.connect`` per op which (a) blocked
        the event loop on a hot WAL and (b) bypassed the WAL +
        busy-timeout PRAGMAs the pool sets up. Each op is now
        gated by an HLC last-writer-wins check at the row level, so
        the arrival order on the wire no longer dictates which copy
        survives.
        """
        scope_gate = (
            None if allowed_scopes is None else normalise_scope_set(allowed_scopes)
        )
        applied = 0
        rejected_scopes: dict[str, int] = {}
        for change_dict in changes:
            op = SyncOperation.from_dict(change_dict)

            # Scope gate, BEFORE the op touches the local WAL. An
            # operation this brain was not granted is not merely
            # unmaterialised, it is not recorded either: writing it to
            # our WAL would make this node a relay for memory it has no
            # permission to hold, and the next peer's send filter would
            # be the only thing standing between that row and a third
            # brain. Dropped, counted, and logged in aggregate rather
            # than per op, because a peer can control the volume here.
            #
            # ``op.scope`` is already normalised by
            # ``SyncOperation.from_dict``, so an operation with no
            # scope field, a junk scope, or the reserved ``private``
            # arrives as ``private`` and can match no grant set:
            # ``normalise_scope_set`` drops ``private`` from every set
            # it builds.
            if scope_gate is not None and op.scope not in scope_gate:
                rejected_scopes[op.scope] = rejected_scopes.get(op.scope, 0) + 1
                continue

            # Malformed HLC. ``from_string`` raises on junk, and an
            # unguarded raise here aborts the whole batch: one bad op
            # from a peer would drop every later op in the same
            # message. Skip the op, keep the batch.
            try:
                remote_hlc = HLCTimestamp.from_string(op.hlc)
            except (ValueError, IndexError):
                self._hlc_malformed_rejections += 1
                logger.warning(
                    "sync rejected: malformed HLC %r from node=%s table=%s id=%s",
                    op.hlc, op.origin_node, op.table, op.row_id,
                )
                continue

            # Clock-drift gate. This must reject the *operation*, not
            # merely decline to advance the local clock: the LWW gate
            # in ``_apply_to_memory`` compares ``op.hlc`` directly, so
            # an op carrying a far-future timestamp would win every
            # conflict for that row and keep winning. Guarding only the
            # clock leaves that hole wide open.
            if not self._hlc.is_within_drift(remote_hlc):
                self._hlc_drift_rejections += 1
                logger.error(
                    "sync rejected: HLC %s from node=%s is beyond the max "
                    "clock drift (%dms), op dropped for table=%s id=%s. "
                    "Check NTP on the peer, or tune "
                    "FERAL_SYNC_MAX_CLOCK_DRIFT_MS.",
                    op.hlc, op.origin_node, self._hlc.max_drift_ms,
                    op.table, op.row_id,
                )
                continue

            self._hlc.receive(remote_hlc)

            try:
                self._wal.append(op)
            except Exception as exc:  # pragma: no cover, WAL failure is rare
                logger.warning("apply_remote_changes WAL append failed: %s", exc)
                continue

            # Advance the clock only once the op is durably in our WAL.
            #
            # This used to run BEFORE the append, so an append that
            # raised left the watermark already moved past an op we do
            # not have. The peer's next change set is cut at that
            # watermark, so it never re-sends it: one transient sqlite
            # error and the operation is gone from this node for good,
            # with a warning that reads like it was retried. The
            # watermark's meaning is "everything up to here is on my
            # disk", and it has to be written when that becomes true.
            self._vector_clock.update(op.origin_node, op.hlc)

            if self._memory:
                try:
                    if await self._apply_to_memory(op):
                        applied += 1
                except Exception as exc:
                    logger.warning("apply_remote_changes materialization failed: %s", exc)
                    continue
            else:
                applied += 1

        if rejected_scopes:
            logger.warning(
                "sync: dropped %d incoming operation(s) in ungranted scope(s) "
                "%s. The peer sent memory this brain never agreed to hold; "
                "grant the scope with `feral sync peer scope grant` if that "
                "was intended.",
                sum(rejected_scopes.values()),
                ", ".join(sorted(rejected_scopes)),
            )
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

            # Tombstone gate. When the live row is absent the LWW check
            # above has nothing to compare against: ``existing_tuple``
            # falls back to ``(0, 0, "")`` and every arriving op wins.
            # That is the resurrection hole. The tombstone carries the
            # HLC of the delete that removed the row, so it stands in
            # for the row's ``hlc_string`` after the row itself is gone
            # and the same strictly-greater rule applies to it.
            tombstone_tuple: Optional[tuple] = None
            if existing_row is None:
                tombstone_tuple = await self._read_tombstone(
                    conn, op.table, op.row_id
                )
                if tombstone_tuple is not None and remote_tuple <= tombstone_tuple:
                    logger.debug(
                        "sync tombstone skip: table=%s id=%s remote=%s op=%s",
                        op.table, op.row_id, op.hlc, op.op_type,
                    )
                    return False

            if op.op_type == "delete":
                # Honour LWW for deletes too: a delete with an older
                # HLC than the surviving row's last-write is stale, and
                # the gate above has already rejected that case.
                await conn.execute(
                    f"DELETE FROM {op.table} WHERE id = ?", (op.row_id,)
                )
                await self._write_tombstone(conn, op.table, op.row_id, op.hlc)
                await conn.commit()
                return True

            if op.op_type != "insert":
                logger.warning("sync: unknown op_type %s for %s", op.op_type, op.table)
                return False

            d = op.data
            now = time.time()
            hlc = op.hlc
            if op.table == "notes":
                # UPSERT, not INSERT OR REPLACE, for the same reason as
                # ``episodes`` below. ``notes`` has always had its
                # ``notes_fts_update`` AFTER UPDATE trigger, but REPLACE
                # is a DELETE plus an INSERT and fires NEITHER the delete
                # trigger (recursive_triggers is off by default) NOR the
                # update trigger, so the trigger being present never
                # helped: measured, 3 re-deliveries of one note id left
                # notes at rows=1 / notes_fts=3, 2 of them orphans
                # holding superseded note text.
                await conn.execute(
                    "INSERT INTO notes "
                    "(id, content, tags, importance, source, created_at, updated_at, hlc_string) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "content = excluded.content, "
                    "tags = excluded.tags, "
                    "importance = excluded.importance, "
                    "source = excluded.source, "
                    "created_at = excluded.created_at, "
                    "updated_at = excluded.updated_at, "
                    "hlc_string = excluded.hlc_string",
                    (
                        d.get("id", op.row_id), d.get("content", ""), d.get("tags", "[]"),
                        d.get("importance", "normal"), d.get("source", "sync"),
                        d.get("created_at", now), now, hlc,
                    ),
                )
            elif op.table == "episodes":
                # episodes are append-only by id; the LWW gate above
                # has already short-circuited a stale arrival, so a
                # second arrival for the same id is genuinely newer.
                #
                # UPSERT, not INSERT OR REPLACE. REPLACE is a DELETE
                # followed by an INSERT, and SQLite does not fire delete
                # triggers for a REPLACE-induced delete unless
                # ``recursive_triggers`` is on, which it is not by
                # default. So ``episodes_ad`` never ran, the row took a
                # fresh rowid, ``episodes_ai`` inserted a second FTS row,
                # and the superseded text stayed in episodes_fts forever:
                # three replaces of one id left 5 FTS rows, 3 of them
                # orphans. ON CONFLICT DO UPDATE keeps the rowid and
                # fires the AFTER UPDATE trigger (store.py's
                # ``episodes_fts_update``), which reindexes the row in
                # place.
                await conn.execute(
                    "INSERT INTO episodes "
                    "(id, session_id, event_type, summary, detail, "
                    "importance, created_at, hlc_string) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "session_id = excluded.session_id, "
                    "event_type = excluded.event_type, "
                    "summary = excluded.summary, "
                    "detail = excluded.detail, "
                    "importance = excluded.importance, "
                    "created_at = excluded.created_at, "
                    "hlc_string = excluded.hlc_string",
                    (
                        d.get("id", op.row_id), d.get("session_id", "sync"),
                        d.get("event_type", "synced"), d.get("summary", ""),
                        d.get("detail", ""), d.get("importance", 0.5),
                        d.get("created_at", now), hlc,
                    ),
                )
            elif op.table == "knowledge":
                # UPSERT for the same reason as ``notes`` above: measured,
                # 3 re-deliveries of one fact id left knowledge at
                # rows=1 / knowledge_fts=3, 2 of them orphans holding the
                # superseded object text.
                await conn.execute(
                    "INSERT INTO knowledge "
                    "(id, subject, predicate, object, confidence, source, "
                    "created_at, updated_at, hlc_string) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "subject = excluded.subject, "
                    "predicate = excluded.predicate, "
                    "object = excluded.object, "
                    "confidence = excluded.confidence, "
                    "source = excluded.source, "
                    "created_at = excluded.created_at, "
                    "updated_at = excluded.updated_at, "
                    "hlc_string = excluded.hlc_string",
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

            if tombstone_tuple is not None:
                # The row was deleted and has now been legitimately
                # re-created by a strictly newer write. Retire the
                # tombstone in the same transaction as the insert, so
                # the row's own hlc_string is the gate from here on and
                # the tombstone table does not accumulate entries for
                # ids that are live again.
                await conn.execute(
                    "DELETE FROM sync_tombstones "
                    "WHERE table_name = ? AND row_id = ?",
                    (op.table, op.row_id),
                )

            await conn.commit()
            return True
        finally:
            await self._memory._release(conn)

    @staticmethod
    async def _read_tombstone(conn, table: str, row_id: str) -> Optional[tuple]:
        """Return the HLC tuple of the delete that removed ``row_id``,
        or ``None`` when no tombstone is on file.

        A missing ``sync_tombstones`` table (a memory.db written by a
        build older than this one and never reopened through
        ``MemoryStore._init_db``) reports "no tombstone" rather than
        failing the op: that is the pre-tombstone behaviour, which is
        wrong but not worse than refusing to sync at all.
        """
        try:
            async with conn.execute(
                "SELECT hlc_string FROM sync_tombstones "
                "WHERE table_name = ? AND row_id = ?",
                (table, row_id),
            ) as cur:
                row = await cur.fetchone()
        except sqlite3.OperationalError as exc:
            logger.warning("sync: tombstone lookup failed (%s)", exc)
            return None
        if row is None:
            return None
        return _parse_hlc(row["hlc_string"] or "")

    @staticmethod
    async def _write_tombstone(conn, table: str, row_id: str, hlc: str) -> None:
        """Record that ``row_id`` was deleted at ``hlc``.

        Failures are logged, never raised. The caller has already issued
        the DELETE on this connection and is about to commit it; letting
        a tombstone problem propagate would roll the delete back and
        leave the row alive, which is the worse of the two outcomes.
        """
        try:
            await conn.execute(
                "INSERT INTO sync_tombstones "
                "(table_name, row_id, hlc_string, deleted_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(table_name, row_id) DO UPDATE SET "
                "hlc_string = excluded.hlc_string, "
                "deleted_at = excluded.deleted_at",
                (table, row_id, hlc, time.time()),
            )
        except sqlite3.OperationalError as exc:
            logger.warning(
                "sync: tombstone write failed table=%s id=%s (%s)",
                table, row_id, exc,
            )

    def get_vector_clock(self) -> dict:
        return self._vector_clock.to_dict()

    # -- Membership -----------------------------------------------------

    def _roster(self):
        """The peer roster this engine answers to, or ``None``.

        Prefers an explicitly attached roster over the process global.
        A roster is per BRAIN, and the process global is only the right
        answer when there is exactly one brain in the process; two
        engines in one process (the end-to-end harness, and any future
        embedding) must not share one membership list, or a grant
        minted on one becomes a grant on the other and the per-peer
        boundary silently becomes global again.

        Never fatal. Sync predates the roster and must keep working on a
        brain where the roster DB is unwritable. What is lost in that
        case is the durability of membership, not the exchange itself,
        with one deliberate exception: :meth:`scopes_for_peer` reads
        ``None`` as "grant nothing", because a missing roster cannot
        prove anything was shared.
        """
        if self._peer_roster is not None:
            return self._peer_roster
        try:
            from security.peer_roster import get_peer_roster
            return get_peer_roster()
        except Exception as exc:  # noqa: BLE001, roster is advisory here
            logger.warning("sync: peer roster unavailable: %s", exc)
            return None

    def set_peer_roster(self, roster) -> None:
        """Bind this engine to one brain's roster. See :meth:`_roster`."""
        self._peer_roster = roster

    def note_peer_seen(self, peer_id: str, address: str = "") -> None:
        """Persist liveness for a peer we just heard from.

        Seeing an advertisement is evidence the brain exists, which is
        what ``last_seen`` means. It is deliberately NOT evidence that it
        holds a valid grant, so this never extends a grant window.
        """
        roster = self._roster()
        if roster is None:
            return
        try:
            roster.mark_seen(peer_id, address=address)
        except Exception as exc:  # noqa: BLE001, liveness is advisory
            logger.warning("sync: mark_seen failed for %s: %s", peer_id, exc)

    def forget_peer(self, peer_id: str, *, reason: str = "departure") -> bool:
        """Drop a peer from the live set and record the departure.

        Returns True when the peer was actually present. Idempotent, so
        a duplicate zeroconf callback is harmless.
        """
        record = self._peers.pop(peer_id, None)
        # The per-peer lock is keyed by peer id and would otherwise
        # accumulate one entry per brain that has ever been on the
        # network.
        self._peer_locks.pop(peer_id, None)
        for name, mapped in list(self._service_names.items()):
            if mapped == peer_id:
                self._service_names.pop(name, None)
        address = (record or {}).get("address", "") or ""
        roster = self._roster()
        if roster is not None:
            try:
                roster.mark_departed(peer_id, address=address)
            except Exception as exc:  # noqa: BLE001, membership is advisory
                logger.warning(
                    "sync: mark_departed failed for %s: %s", peer_id, exc,
                )
        if record is not None:
            logger.info(
                "Peer left: %s at %s (%s)", peer_id, address or "-", reason,
            )
        return record is not None

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
                    handlers=AsyncPeerListener(self),
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
                    self._zeroconf, SERVICE_TYPE, PeerListener(self),
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

        # ``max_size`` is mandatory here, not a tuning knob. The library
        # default is 1 MiB and the peer's change set arrives as frames
        # this connection has to read; a first sync against a fresh peer
        # ships that peer's entire history. Chunking (see
        # ``sync_data_frames``) keeps ordinary frames two orders of
        # magnitude below this, and this ceiling covers the one case
        # chunking cannot help with: a single indivisible operation.
        ws = await asyncio.wait_for(
            websockets.connect(uri, ssl=client_ssl, max_size=SYNC_MAX_RECV_BYTES),
            timeout=connect_timeout,
        )

        # Per-peer grant, when this brain holds one for the peer it is
        # dialling. Sent ALONGSIDE the shared passphrase, never instead
        # of it, so a peer still on the old build keeps authenticating
        # us the way it always did. The receiving side decides which one
        # it honours (see ``security.peer_roster.authenticate_sync_peer``),
        # and a peer that has enrolled us will ignore the passphrase.
        peer_grant = ""
        try:
            from security.peer_roster import resolve_outbound_grant

            peer_grant = resolve_outbound_grant(
                peer_id=peer_id, address=addr, port=port,
            )
        except Exception as exc:  # noqa: BLE001, fall back to the shared secret
            logger.warning(
                "sync: outbound grant lookup failed for %s: %s", peer_id, exc,
            )

        async with ws:
            handshake = {
                "type": "sync_request",
                "node_id": self.node_id,
                "vector_clock": self.get_vector_clock(),
                "passphrase": SYNC_PASSPHRASE,
            }
            if peer_grant:
                handshake["peer_grant"] = peer_grant
            await asyncio.wait_for(
                ws.send(json.dumps(handshake)),
                timeout=handshake_timeout,
            )

            resp_raw = await asyncio.wait_for(ws.recv(), timeout=handshake_timeout)
            resp = json.loads(resp_raw)

            if resp.get("type") == "sync_error":
                return {"success": False, "error": resp.get("message", "rejected")}

            remote_vc = resp.get("vector_clock", {})

            # The peer's REAL node_id, off the handshake, not the local
            # dictionary key. ``_load_static_peers`` keys peers as
            # ``static-{host}:{port}``, which matches no ``origin_node``,
            # so the old ``exclude_node=peer_id`` was a no-op for every
            # statically configured peer and echoed its own writes back.
            remote_node_id = resp.get("node_id") or ""

            # Per-origin cutoffs. The old single cutoff was
            # ``remote_vc[self.node_id]``, what the peer has of MY
            # writes, applied to ops of every origin. See
            # ``SyncWAL.get_changes_for_peer``.
            # Off the loop: this is a synchronous sqlite3 query whose
            # cost scales with the WAL, and it runs once per peer per
            # cadence tick.
            # Scoped sharing, send side. The grant set is resolved
            # from OUR roster against the peer's real node_id off the
            # handshake, never against the local dictionary key: a
            # statically configured peer is keyed ``static-host:port``,
            # which is not a node_id and would resolve to no grants
            # (safe, but wrong for an operator who did grant something).
            # An empty set here is not an error, it is an authenticated
            # peer that has been granted nothing, and it correctly
            # sends zero operations.
            allowed_scopes = self.scopes_for_peer(remote_node_id)
            changes_for_peer = await asyncio.to_thread(
                self._wal.get_changes_for_peer,
                remote_vc,
                allowed_scopes=allowed_scopes,
                exclude_node=remote_node_id or peer_id,
            )

            for frame in sync_data_frames(changes_for_peer):
                await asyncio.wait_for(
                    ws.send(json.dumps(frame)), timeout=handshake_timeout,
                )

            async def _recv_one():
                return json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=handshake_timeout)
                )

            try:
                remote_changes = await recv_sync_data(_recv_one)
            except SyncProtocolMessage as interrupt:
                msg = interrupt.message
                if isinstance(msg, dict) and msg.get("type") == "sync_error":
                    return {"success": False, "error": msg.get("message", "rejected")}
                raise
            # Scoped sharing, receive side. Same grant set, applied a
            # second time to operations WE DID NOT BUILD. See
            # ``apply_remote_changes_from_peer`` for why the send
            # filter above is not sufficient on its own.
            applied = await self.apply_remote_changes_from_peer(
                remote_changes, peer_node_id=remote_node_id,
            )

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
            # Recorded under the peer's REAL node_id, for the same
            # reason ``exclude_node`` uses it: a statically configured
            # peer's local key is ``static-{host}:{port}``, and a
            # delivery record keyed on host and port answers "which
            # address did I send this to", not "which brain has it".
            # The address changes with DHCP; the node_id does not.
            if changes_for_peer:
                try:
                    await asyncio.to_thread(
                        self._wal.mark_synced_many,
                        [op.op_id for op in changes_for_peer],
                        remote_node_id or peer_id,
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
        """Export all memory for manual sync (USB, AirDrop).

        Deliberately unscoped: this is the operator exporting their OWN
        brain to their own removable media, not a peer exchange, so the
        bundle carries everything including ``private`` operations. The
        scope of each operation travels in the bundle, so importing it
        into a second brain the same operator owns preserves the
        boundary rather than flattening it. Do not repurpose this to
        hand a bundle to somebody else's brain: that is a peer
        exchange and belongs on the scoped path.
        """
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
            "clock": self._hlc.health,
            "hlc_drift_rejections": self._hlc_drift_rejections,
            "hlc_malformed_rejections": self._hlc_malformed_rejections,
        }
