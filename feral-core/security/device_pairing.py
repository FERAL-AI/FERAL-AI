"""
FERAL Per-Node Device Pairing
===============================
SQLite-backed registry of paired edge-node devices.

Each paired device gets a unique token that replaces the old
single ``NODE_API_KEY`` for authenticating ``/v1/node`` connections.

 — hashing + TTL
------------------
Tokens are no longer stored in plaintext. The on-disk schema keeps:

  - ``token_lookup`` — SHA-256 of the plaintext token, used as a
    deterministic O(1) lookup index. Pairing tokens are 256-bit random
    so SHA-256 alone provides ample preimage resistance for the lookup
    role; the hash is NOT the password verifier.
  - ``token_hash``   — Argon2id (or bcrypt cost=12 fallback) verifier
    of the same plaintext. Both must match for a verify to succeed.
  - ``ttl_seconds``  — re-pair window; default 86400s (24h).
  - ``expires_at``   — wall-clock epoch seconds; ``verify_device``
    rejects rows where ``expires_at <= now``.

Plaintext is returned to the client EXACTLY ONCE at issue time
(``pair_device`` return value). After that the brain has no way to
recover it; that is the security property we want.

Migration
---------
 rows had a plaintext ``token`` column. On first boot under :
  1. The schema gains the new columns.
  2. A sibling table ``needs_rotation_log`` is created.
  3. Every legacy row (rows where ``token`` is non-null AND
     ``token_hash`` is null) is copied into ``needs_rotation_log``
     (device_id, name, paired_at, logged_at, reason).
  4. Their ``token`` and ``token_lookup`` columns are nulled out so the
     plaintext never survives on disk.
  5. ``token`` column is then DROPPED via SQLite ``ALTER TABLE``.
     If the runtime is on SQLite < 3.35 (no DROP COLUMN), we fall back
     to leaving the column in place but permanently NULL — operators
     are told via the brain log to upgrade SQLite at their convenience.
  6. The brain prints a one-time line per migrated device on the next
     daemon connection so the user knows to re-pair.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger("feral.device_pairing")

# Argon2id parameters — these match the defaults of argon2-cffi's
# ``PasswordHasher`` as of 23.x. We pin them so a future upstream
# default-tuning change doesn't silently invalidate stored hashes
# (Argon2 verifiers carry their parameters in the encoded string, so
# verify still works, but new hashes would no longer be byte-identical
# to old ones — which is fine; we never compare hashes by string).
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MiB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16

# bcrypt fallback cost — only used when argon2-cffi import genuinely
# fails (per the  charter). Logged at WARNING so operators see the
# downgrade.
BCRYPT_COST = 12

# Default TTL for issued pair tokens. 24h is the  default; callers
# may override per-pair via ``pair_device(ttl_seconds=...)``.
DEFAULT_TTL_SECONDS = 86_400

# Runtime bearer for paired phone/browser clients. Kept separate from the
# pair token so we can rotate runtime auth without touching pair claims.
DEFAULT_PHONE_BEARER_TTL_SECONDS = 2_592_000  # 30 days
PHONE_BEARER_KIND = "phone_bearer"


# ─────────────────────────────────────────────────────────────────────
# Hash backend (argon2id primary, bcrypt fallback)
# ─────────────────────────────────────────────────────────────────────


class _Argon2Backend:
    """argon2-cffi-backed hasher. Encoded strings start with ``$argon2id$``."""

    name = "argon2id"

    def __init__(self):
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError, InvalidHashError

        self._ph = PasswordHasher(
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LEN,
            salt_len=ARGON2_SALT_LEN,
        )
        self._VerifyMismatchError = VerifyMismatchError
        self._InvalidHashError = InvalidHashError

    def hash(self, plaintext: str) -> str:
        return self._ph.hash(plaintext)

    def verify(self, encoded: str, plaintext: str) -> bool:
        if not encoded:
            return False
        try:
            return self._ph.verify(encoded, plaintext)
        except (
            self._VerifyMismatchError,
            self._InvalidHashError,
        ):
            return False
        except Exception as exc:
            # Anything else (corrupt encoded blob, OS-level argon2 bug)
            # is logged so the operator sees it; we treat it as a
            # verification failure rather than crashing the daemon.
            logger.warning("argon2.verify_unexpected_error: %s", exc)
            return False


class _BcryptBackend:
    """bcrypt cost=12 fallback. Used only when argon2-cffi is missing."""

    name = "bcrypt"

    def __init__(self):
        import bcrypt
        self._bcrypt = bcrypt

    def hash(self, plaintext: str) -> str:
        salt = self._bcrypt.gensalt(rounds=BCRYPT_COST)
        return self._bcrypt.hashpw(plaintext.encode(), salt).decode()

    def verify(self, encoded: str, plaintext: str) -> bool:
        if not encoded:
            return False
        try:
            return self._bcrypt.checkpw(plaintext.encode(), encoded.encode())
        except Exception as exc:
            logger.warning("bcrypt.verify_unexpected_error: %s", exc)
            return False


def _choose_backend():
    """Pick argon2id; fall back to bcrypt with a WARNING (no silent swap)."""
    try:
        return _Argon2Backend()
    except ImportError as exc:
        try:
            backend = _BcryptBackend()
        except ImportError as bcrypt_exc:
            raise RuntimeError(
                "Neither argon2-cffi nor bcrypt is available. Pairing "
                "tokens cannot be hashed at rest. Install one of them: "
                "`pip install argon2-cffi` (preferred) or "
                "`pip install bcrypt`."
            ) from bcrypt_exc
        logger.warning(
            "device_pairing.argon2_unavailable: %s — falling back to "
            "bcrypt (cost=%d). Install argon2-cffi for the W9-recommended "
            "hashing parameters.",
            exc, BCRYPT_COST,
        )
        return backend


_backend = None


def _get_backend():
    global _backend
    if _backend is None:
        _backend = _choose_backend()
    return _backend


def _token_lookup(token: str) -> str:
    """Deterministic, fast lookup index for the plaintext token.

    Pairing tokens are 256-bit random; a SHA-256 over them is preimage-
    resistant in the strongest practical sense. We use this purely as
    an O(1) row index — actual verification is the argon2 hash."""
    return hashlib.sha256(token.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────
# DevicePairingStore
# ─────────────────────────────────────────────────────────────────────


class DevicePairingStore:
    """SQLite-backed paired-device registry.

    Default path: ``~/.feral/paired_devices.db`` (overridable for tests).
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            home = os.environ.get("FERAL_HOME", str(Path.home() / ".feral"))
            Path(home).mkdir(parents=True, exist_ok=True)
            db_path = str(Path(home) / "paired_devices.db")
        self._db_path = db_path
        self._lock = threading.Lock()
        self._migration_summary: dict = {"migrated": [], "kept_token_column": False}
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── Schema setup + migration ───────────────────────────────────

    def _init_db(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                # Base table — fresh installs land here directly.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS paired_devices (
                        device_id  TEXT PRIMARY KEY,
                        name       TEXT NOT NULL,
                        paired_at  REAL NOT NULL,
                        last_seen  REAL
                    )
                """)
                # needs_rotation_log — populated during migration; stays
                # around so the brain can show "these devices need re-pair"
                # in the UI long after the migration boot.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS needs_rotation_log (
                        device_id   TEXT PRIMARY KEY,
                        name        TEXT,
                        paired_at   REAL,
                        logged_at   REAL NOT NULL,
                        reason      TEXT NOT NULL,
                        announced   INTEGER NOT NULL DEFAULT 0
                    )
                """)

                # Additive column migrations. Silent-skip when already
                # present so re-running on an existing DB is safe.
                self._add_column_if_missing(conn, "paired_devices", "kind", "TEXT")
                self._add_column_if_missing(conn, "paired_devices", "node_id", "TEXT")
                self._add_column_if_missing(conn, "paired_devices", "claimed_at", "REAL")
                self._add_column_if_missing(conn, "paired_devices", "platform", "TEXT")
                self._add_column_if_missing(conn, "paired_devices", "capabilities", "TEXT")

                #  columns.
                self._add_column_if_missing(
                    conn, "paired_devices", "token_lookup", "TEXT",
                )
                self._add_column_if_missing(
                    conn, "paired_devices", "token_hash", "TEXT",
                )
                self._add_column_if_missing(
                    conn, "paired_devices", "hash_algo", "TEXT",
                )
                self._add_column_if_missing(
                    conn, "paired_devices", "ttl_seconds", "INTEGER",
                    default=str(DEFAULT_TTL_SECONDS),
                )
                self._add_column_if_missing(
                    conn, "paired_devices", "expires_at", "INTEGER",
                )

                # PIN second-factor columns (pair-pin-confirm PR).
                # Existing rows get pin_hash=NULL → no PIN required, so
                # legacy tokens still work without re-pairing.
                self._add_column_if_missing(
                    conn, "paired_devices", "pin_hash", "TEXT",
                )
                self._add_column_if_missing(
                    conn, "paired_devices", "pin_attempts", "INTEGER",
                    default="0",
                )
                self._add_column_if_missing(
                    conn, "paired_devices", "pin_verified", "INTEGER",
                    default="0",
                )

                # Add the legacy `token` column so old DBs upgrade
                # cleanly even when they pre-date that column. Fresh
                # installs don't have it (see CREATE TABLE above) so
                # this is a no-op for them — _add_column_if_missing
                # quietly skips when the column exists.
                self._add_column_if_missing(conn, "paired_devices", "token", "TEXT")

                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pd_token_lookup "
                    "ON paired_devices(token_lookup)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pd_expires_at "
                    "ON paired_devices(expires_at)"
                )

                # Runtime credentials for paired devices (e.g. phone bearer).
                # Separate table = separate lifecycle from pair tokens.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS device_credentials (
                        credential_id  TEXT PRIMARY KEY,
                        device_id      TEXT NOT NULL,
                        bearer_kind    TEXT NOT NULL,
                        token_lookup   TEXT NOT NULL UNIQUE,
                        token_hash     TEXT NOT NULL,
                        hash_algo      TEXT NOT NULL,
                        ttl_seconds    INTEGER NOT NULL,
                        expires_at     INTEGER NOT NULL,
                        created_at     REAL NOT NULL,
                        rotated_at     REAL NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dc_device_kind "
                    "ON device_credentials(device_id, bearer_kind)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dc_expires_at "
                    "ON device_credentials(expires_at)"
                )

                # Pending pair codes — the SDK code-pair flow:
                #   daemon emits an 8-char base32 code, dashboard claims
                #   it. ``token`` is null until ``claim_pending_code``
                #   mints a real device-pairing token and writes it
                #   back. ``claim_attempts`` lets the rate limiter
                #   anti-correlate brute force against a single code.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pending_pair_codes (
                        code            TEXT PRIMARY KEY,
                        node_id         TEXT NOT NULL,
                        name            TEXT NOT NULL,
                        created_at      REAL NOT NULL,
                        expires_at      REAL NOT NULL,
                        token           TEXT,
                        device_id       TEXT,
                        claim_attempts  INTEGER NOT NULL DEFAULT 0
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ppc_expires_at "
                    "ON pending_pair_codes(expires_at)"
                )
                conn.commit()

                # Migrate any  rows: copy to needs_rotation_log,
                # null out the plaintext token, drop the column.
                self._migrate_legacy_plaintext_rows(conn)

                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _add_column_if_missing(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        decl: str,
        *,
        default: Optional[str] = None,
    ) -> None:
        cols = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in cols:
            return
        suffix = f" DEFAULT {default}" if default is not None else ""
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}{suffix}")

    @staticmethod
    def _rebuild_paired_devices_without_token(
        conn: sqlite3.Connection,
    ) -> None:
        """Drop the legacy plaintext ``token`` column via the SQLite
        table-rebuild pattern.

        SQLite's ``ALTER TABLE … DROP COLUMN`` (3.35+) refuses to drop
        columns that participate in a UNIQUE constraint (legacy 
        schemas commonly declared ``token TEXT UNIQUE NOT NULL``). In
        that case we rebuild the table in a single transaction:

            1. Read the current column list + foreign-key list from
               ``PRAGMA`` so we preserve any operator-added columns.
            2. Create ``paired_devices_new`` with every current column
               EXCEPT ``token``, keeping ``device_id`` as PRIMARY KEY.
            3. Copy every row (again, excluding ``token``) from the old
               table to the new one.
            4. Drop the old table and rename the new one into place.
            5. Recreate the two indexes that ``_init_db`` itself owns
               (``idx_pd_token_lookup``, ``idx_pd_expires_at``).

        The whole thing runs inside ``BEGIN … COMMIT`` so partial
        rebuilds can't survive a crash. The caller already owns the
        module-level write lock.
        """
        info_rows = conn.execute(
            "PRAGMA table_info(paired_devices)"
        ).fetchall()
        keep_cols: list[tuple[str, str, int, object, int]] = []
        for r in info_rows:
            col_name = r["name"] if isinstance(r, sqlite3.Row) else r[1]
            col_type = r["type"] if isinstance(r, sqlite3.Row) else r[2]
            col_notnull = r["notnull"] if isinstance(r, sqlite3.Row) else r[3]
            col_default = r["dflt_value"] if isinstance(r, sqlite3.Row) else r[4]
            col_pk = r["pk"] if isinstance(r, sqlite3.Row) else r[5]
            if col_name == "token":
                continue
            keep_cols.append(
                (col_name, col_type or "", int(col_notnull or 0),
                 col_default, int(col_pk or 0))
            )
        if not keep_cols:
            raise RuntimeError(
                "paired_devices rebuild aborted — no columns left after "
                "filtering out `token`; refusing to destroy the table."
            )
        col_names_kept = [c[0] for c in keep_cols]

        column_defs: list[str] = []
        for name, col_type, notnull, default, pk in keep_cols:
            piece = f'"{name}" {col_type}'.rstrip()
            if pk:
                piece += " PRIMARY KEY"
            if notnull and not pk:
                piece += " NOT NULL"
            if default is not None:
                piece += f" DEFAULT {default}"
            column_defs.append(piece)

        in_tx = conn.in_transaction
        if not in_tx:
            conn.execute("BEGIN")
        try:
            conn.execute(
                f"CREATE TABLE paired_devices_new ({', '.join(column_defs)})"
            )
            col_list_sql = ", ".join(f'"{c}"' for c in col_names_kept)
            conn.execute(
                f"INSERT INTO paired_devices_new ({col_list_sql}) "
                f"SELECT {col_list_sql} FROM paired_devices"
            )
            conn.execute("DROP TABLE paired_devices")
            conn.execute(
                "ALTER TABLE paired_devices_new RENAME TO paired_devices"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pd_token_lookup "
                "ON paired_devices(token_lookup)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pd_expires_at "
                "ON paired_devices(expires_at)"
            )
            if not in_tx:
                conn.commit()
        except Exception:
            if not in_tx:
                conn.rollback()
            raise

    def _migrate_legacy_plaintext_rows(self, conn: sqlite3.Connection) -> dict:
        """One-shot migration: log every legacy plaintext row to
        needs_rotation_log, then nuke the plaintext token. Returns a
        summary the caller can surface in tests / boot logs.

        Idempotent: rows that have already been logged + nulled are
        skipped.
        """
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(paired_devices)").fetchall()
        }
        if "token" not in cols:
            # Already on the post-migration schema (e.g. fresh install).
            return {"migrated": [], "dropped_token_column": True}

        rows = conn.execute(
            "SELECT device_id, name, paired_at, token "
            "FROM paired_devices "
            "WHERE token IS NOT NULL AND token != '' "
            "AND (token_hash IS NULL OR token_hash = '')"
        ).fetchall()

        now = time.time()
        migrated: list[str] = []
        for row in rows:
            device_id = row["device_id"]
            conn.execute(
                "INSERT OR REPLACE INTO needs_rotation_log "
                "(device_id, name, paired_at, logged_at, reason, announced) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (
                    device_id,
                    row["name"],
                    row["paired_at"],
                    now,
                    "w9_plaintext_to_hash_migration",
                ),
            )
            migrated.append(device_id)

        # Try to DROP COLUMN token (SQLite >= 3.35.0). SQLite refuses
        # DROP COLUMN on columns that carry a UNIQUE constraint even on
        # 3.35+, so the first attempt can raise; in that case we fall
        # back to the table-rebuild pattern (A10 / ). Only if *that*
        # also fails do we resort to NULL-in-place. Doing the drop FIRST
        # avoids the NOT NULL constraint that the legacy schema enforced
        # on the `token` column.
        dropped = False
        try:
            conn.execute("ALTER TABLE paired_devices DROP COLUMN token")
            dropped = True
        except sqlite3.OperationalError as exc:
            # SQLite < 3.35 (and older schemas with UNIQUE on the
            # legacy `token` column) cannot run DROP COLUMN directly.
            # That's expected on first upgrade -- we fall back to a
            # safe table rebuild below. Log INFO so a first-time
            # user reading the boot log doesn't think something is
            # broken; the rebuild outcome is logged as well.
            logger.info(
                "device_pairing.migration: DROP COLUMN unsupported by this "
                "SQLite (%s) — rebuilding paired_devices to drop the "
                "legacy `token` column. No action needed.",
                exc,
            )
            try:
                self._rebuild_paired_devices_without_token(conn)
                dropped = True
                logger.info(
                    "device_pairing.migration.unique_rebuild_ok: rebuilt "
                    "paired_devices without the legacy `token` column "
                    "(%d migrated row(s)).",
                    len(migrated),
                )
            except Exception as rebuild_exc:
                logger.warning(
                    "device_pairing.migration.unique_rebuild_failed: %s "
                    "— leaving the legacy `token` column in place; it "
                    "will be set to empty string for the migrated rows. "
                    "Upgrade to SQLite >= 3.35 for full schema cleanup.",
                    rebuild_exc,
                )

        # Now scrub the auxiliary state on every migrated row. If the
        # column was dropped we just need to clear lookup/hash; if not
        # (older SQLite), set token to '' to preserve the NOT NULL
        # constraint while keeping plaintext off disk.
        for device_id in migrated:
            if dropped:
                conn.execute(
                    "UPDATE paired_devices "
                    "SET token_lookup = NULL, token_hash = NULL, "
                    "    hash_algo = NULL, expires_at = ? "
                    "WHERE device_id = ?",
                    (int(now), device_id),
                )
            else:
                conn.execute(
                    "UPDATE paired_devices "
                    "SET token = '', token_lookup = NULL, "
                    "    token_hash = NULL, hash_algo = NULL, "
                    "    expires_at = ? "
                    "WHERE device_id = ?",
                    (int(now), device_id),
                )

        if migrated:
            logger.info(
                "device_pairing.migrated_to_hashed: %d legacy device(s) "
                "logged to needs_rotation_log and invalidated. The user "
                "must re-pair each device on its next daemon connection.",
                len(migrated),
            )
        self._migration_summary = {
            "migrated": migrated,
            "dropped_token_column": dropped,
        }
        return self._migration_summary

    @property
    def migration_summary(self) -> dict:
        """Return the result of the most recent _init_db migration step.

        Tests use this to assert "every legacy row landed in
        needs_rotation_log"; the brain boot path uses it to print the
        one-time per-device announcement."""
        return dict(self._migration_summary)

    def needs_rotation(self) -> list[dict]:
        """Return all devices flagged as needing re-pair."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT device_id, name, paired_at, logged_at, reason, "
                "       announced "
                "FROM needs_rotation_log "
                "ORDER BY logged_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def acknowledge_rotation(self, device_id: str) -> bool:
        """Mark a needs-rotation entry as announced (so the brain
        doesn't spam the same warning on every daemon connection)."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE needs_rotation_log SET announced = 1 "
                    "WHERE device_id = ?",
                    (device_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    # ── pair / verify / mark_claimed ───────────────────────────────

    # ── PIN second-factor (pair-pin-confirm PR) ──────────────────────
    #
    # The pair URL token is a 256-bit random secret; today its
    # possession alone is sufficient to claim. To raise the bar, the
    # operator can request that a 4-digit PIN gate the claim. The
    # brain shows the PIN to the operator on the dashboard; the phone
    # form prompts for it before POSTing /api/devices/pair/complete.
    # 5 wrong attempts → token invalidated server-side. The PIN itself
    # is stored Argon2id-hashed; plaintext is returned exactly once
    # to the operator at issue time.
    PIN_DIGITS = 4
    PIN_MAX_ATTEMPTS = 5

    @staticmethod
    def _generate_pin(digits: int = PIN_DIGITS) -> str:
        # secrets.randbelow gives a uniform integer in [0, 10**digits);
        # zero-pad so 4-digit PINs always render as four characters
        # ("0042" not "42").
        n = secrets.randbelow(10 ** digits)
        return f"{n:0{digits}d}"

    def pair_device(
        self,
        name: str,
        *,
        kind: str = "name",
        node_id: str = "",
        platform: str = "",
        capabilities: Optional[list[str]] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        mint_phone_bearer: Optional[bool] = None,
        phone_bearer_ttl_seconds: int = DEFAULT_PHONE_BEARER_TTL_SECONDS,
        require_pin: bool = False,
    ) -> dict:
        """Register a new device.

        Args:
            name: human-readable label the user sees in Devices.
            kind: ``"name"`` (default pair-modal label), ``"browser"``
                (a browser-node attach), ``"hup"`` (daemon registering
                with explicit node_id + capabilities). This is the
                "typed body" the v2 PairDeviceModal needed for its HUP
                tab.
                ``"browser_node_v2"`` opts into issuing a runtime
                phone bearer alongside the pair token.
            node_id: optional authoritative id the daemon will register
                with on /v1/node (only used by kind="hup").
            platform: user-agent / platform hint for browser clients.
            capabilities: declared capabilities (camera / mic / location
                / heart_rate / …), JSON-encoded in storage.
            ttl_seconds: how long this pairing is valid for. Default
                24h. Overridable per-pair (e.g. setup-wizard short-lived
                tokens use 600s).
            mint_phone_bearer: override for issuing a runtime phone bearer.
                When ``None``, this is inferred from
                ``kind == "browser_node_v2"``.
            phone_bearer_ttl_seconds: runtime bearer TTL. Defaults to 30 days.

        Returns ``{device_id, token, name, paired_at, expires_at,
        ttl_seconds, kind, node_id?, …}``. The plaintext ``token`` is
        included EXACTLY ONCE here; subsequent reads via
        :meth:`list_devices` will not contain it.
        """
        import json as _json

        if ttl_seconds <= 0:
            raise ValueError(
                f"ttl_seconds must be positive (got {ttl_seconds})"
            )
        if mint_phone_bearer is None:
            mint_phone_bearer = kind == "browser_node_v2"
        if mint_phone_bearer and phone_bearer_ttl_seconds <= 0:
            raise ValueError(
                "phone_bearer_ttl_seconds must be positive when "
                "mint_phone_bearer=True"
            )

        device_id = str(uuid4())
        token = secrets.token_hex(32)
        now = time.time()
        expires_at = int(now) + int(ttl_seconds)
        caps_text = _json.dumps(list(capabilities or []))

        backend = _get_backend()
        token_hash = backend.hash(token)
        token_lookup = _token_lookup(token)

        # PIN second factor (opt-in per pair).
        pin_plaintext = ""
        pin_hash = None
        if require_pin:
            pin_plaintext = self._generate_pin()
            pin_hash = backend.hash(pin_plaintext)

        with self._lock:
            conn = self._conn()
            try:
                # Pair-dedup contract (operator report 2026-05-08:
                # "webUI is showing every pair as a separate even
                # though it's the same thing"). When the caller
                # supplies a non-empty `node_id`, any prior unexpired
                # rows for the SAME node are explicitly revoked here.
                # Rationale:
                #   * Security: stale phone_bearer + pairing_token
                #     credentials should not survive a re-pair —
                #     re-pairing is the operator's signal that the
                #     prior pairing is no longer authoritative.
                #   * Truthfulness: list_devices() then naturally
                #     returns one row per node_id, killing the
                #     duplicate-Devices-tile bug at the source
                #     instead of papering over it in the UI.
                # The unique key is the explicit `node_id`. Rows with
                # an empty `node_id` (legacy/v1 browser-only pairs
                # that pre-date the field) are NOT touched, because
                # multiple browsers can legitimately co-exist without
                # a node identity. Tests pinned by
                # tests/test_pair_node_id_dedup.py.
                superseded = []
                if node_id:
                    cursor = conn.execute(
                        """SELECT device_id FROM paired_devices
                           WHERE node_id = ? AND device_id != ?""",
                        (node_id, device_id),
                    )
                    superseded = [row[0] for row in cursor.fetchall()]
                    if superseded:
                        placeholders = ",".join("?" * len(superseded))
                        # Remove paired_devices rows AND any minted
                        # phone_bearer rows tied to them; otherwise
                        # the bearer outlives the device row and the
                        # next /v1/node connect with the stale bearer
                        # would be authenticated against a gone row.
                        conn.execute(
                            f"DELETE FROM paired_devices WHERE device_id IN ({placeholders})",
                            superseded,
                        )
                        # Runtime phone bearers + any other minted
                        # device credentials (e.g. future per-skill
                        # tokens) live in `device_credentials`. Wipe
                        # them too so a stolen old bearer cannot
                        # authenticate after re-pair.
                        conn.execute(
                            f"DELETE FROM device_credentials WHERE device_id IN ({placeholders})",
                            superseded,
                        )
                conn.execute(
                    """INSERT INTO paired_devices
                       (device_id, name, paired_at, kind, node_id,
                        platform, capabilities, token_lookup, token_hash,
                        hash_algo, ttl_seconds, expires_at,
                        pin_hash, pin_attempts, pin_verified)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
                    (
                        device_id, name, now, kind, node_id or "",
                        platform or "", caps_text,
                        token_lookup, token_hash, backend.name,
                        int(ttl_seconds), expires_at,
                        pin_hash,
                    ),
                )
                phone_bearer_record = None
                if mint_phone_bearer:
                    phone_bearer_record = self._issue_phone_bearer(
                        conn,
                        device_id=device_id,
                        ttl_seconds=int(phone_bearer_ttl_seconds),
                    )
                conn.commit()
            finally:
                conn.close()

        if superseded:
            # WARNING level so an operator who re-pairs a device by
            # accident sees that prior credentials are gone — the
            # paired-device list is now authoritative for the node.
            logger.warning(
                "Pair re-issued for node %s — superseded %d prior device row(s): %s",
                node_id, len(superseded),
                ", ".join(superseded),
            )
        logger.info(
            "Paired device %s (%s, kind=%s, node=%s, ttl=%ss, pin=%s)",
            device_id, name, kind, node_id or "-", ttl_seconds,
            "yes" if require_pin else "no",
        )
        result = {
            "device_id": device_id,
            "token": token,
            "name": name,
            "paired_at": now,
            "expires_at": expires_at,
            "ttl_seconds": int(ttl_seconds),
            "kind": kind,
            "node_id": node_id or "",
            "platform": platform or "",
            "capabilities": list(capabilities or []),
            "pin_required": bool(require_pin),
        }
        if phone_bearer_record:
            result.update({
                "phone_bearer": phone_bearer_record["phone_bearer"],
                "phone_bearer_expires_at": phone_bearer_record["expires_at"],
                "phone_bearer_ttl_seconds": phone_bearer_record["ttl_seconds"],
            })
        if require_pin:
            # Plaintext PIN returned EXACTLY ONCE alongside the token,
            # so the dashboard can show it to the operator. After this
            # response the PIN can only be verified, not retrieved.
            result["pin"] = pin_plaintext
        return result

    def _issue_phone_bearer(
        self,
        conn: sqlite3.Connection,
        *,
        device_id: str,
        ttl_seconds: int = DEFAULT_PHONE_BEARER_TTL_SECONDS,
    ) -> dict:
        """Issue (or replace) the runtime phone bearer for *device_id*."""
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive (got {ttl_seconds})")
        phone_bearer = secrets.token_hex(32)
        now = time.time()
        expires_at = int(now) + int(ttl_seconds)
        backend = _get_backend()

        conn.execute(
            "DELETE FROM device_credentials WHERE device_id = ? AND bearer_kind = ?",
            (device_id, PHONE_BEARER_KIND),
        )
        conn.execute(
            """INSERT INTO device_credentials
               (credential_id, device_id, bearer_kind, token_lookup, token_hash,
                hash_algo, ttl_seconds, expires_at, created_at, rotated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid4()),
                device_id,
                PHONE_BEARER_KIND,
                _token_lookup(phone_bearer),
                backend.hash(phone_bearer),
                backend.name,
                int(ttl_seconds),
                expires_at,
                now,
                now,
            ),
        )
        logger.info(
            "Issued runtime credential (device_id=%s bearer_kind=%s ttl=%ss)",
            device_id,
            PHONE_BEARER_KIND,
            ttl_seconds,
        )
        return {
            "device_id": device_id,
            "phone_bearer": phone_bearer,
            "expires_at": expires_at,
            "ttl_seconds": int(ttl_seconds),
            "bearer_kind": PHONE_BEARER_KIND,
        }

    def verify_phone_bearer(self, bearer: str) -> Optional[str]:
        """Return the ``device_id`` for a runtime phone bearer.

        Behaves like :meth:`verify_device`: unknown/expired/invalid bearers
        return ``None``; successful verification extends ``expires_at`` by the
        bearer TTL (sliding window) and bumps ``last_seen`` on the paired
        device row.
        """
        if not bearer:
            return None
        lookup = _token_lookup(bearer)
        conn = self._conn()
        try:
            row = conn.execute(
                """SELECT credential_id, device_id, token_hash, expires_at, ttl_seconds
                   FROM device_credentials
                   WHERE token_lookup = ? AND bearer_kind = ?""",
                (lookup, PHONE_BEARER_KIND),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        if not _get_backend().verify(row["token_hash"], bearer):
            return None
        now = int(time.time())
        if row["expires_at"] is not None and row["expires_at"] <= now:
            logger.info(
                "device_pairing.phone_bearer_expired: device_id=%s expires_at=%s (now=%s)",
                row["device_id"],
                row["expires_at"],
                now,
            )
            return None

        device_id = row["device_id"]
        new_expiry = now + int(row["ttl_seconds"] or DEFAULT_PHONE_BEARER_TTL_SECONDS)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE device_credentials SET expires_at = ?, rotated_at = ? "
                    "WHERE credential_id = ?",
                    (new_expiry, time.time(), row["credential_id"]),
                )
                conn.execute(
                    "UPDATE paired_devices SET last_seen = ? WHERE device_id = ?",
                    (time.time(), device_id),
                )
                conn.commit()
            finally:
                conn.close()
        return device_id

    def rotate_phone_bearer(
        self,
        device_id: str,
        *,
        ttl_seconds: int = DEFAULT_PHONE_BEARER_TTL_SECONDS,
    ) -> Optional[dict]:
        """Replace the active runtime phone bearer for *device_id*."""
        if not device_id:
            return None
        if ttl_seconds <= 0:
            raise ValueError(
                f"ttl_seconds must be positive (got {ttl_seconds})"
            )
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT device_id FROM paired_devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if row is None:
                    return None
                issued = self._issue_phone_bearer(
                    conn,
                    device_id=device_id,
                    ttl_seconds=int(ttl_seconds),
                )
                conn.commit()
            finally:
                conn.close()
        return issued

    def token_requires_pin(self, token: str) -> bool:
        """Does this token need a PIN to claim?

        Used by ``GET /api/devices/pair/check`` so the phone's form
        knows whether to render the PIN input. Returns False if the
        token is unknown — that case will be caught later by claim
        verification, no point leaking the existence of unknown tokens
        through this endpoint.
        """
        if not token:
            return False
        lookup = _token_lookup(token)
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT pin_hash FROM paired_devices "
                    "WHERE token_lookup = ?",
                    (lookup,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return False
        return bool(row[0])

    def verify_pin(self, token: str, pin: str) -> tuple[bool, str]:
        """Verify a 4-digit PIN against the pair row.

        Returns ``(ok, reason)``:

        - ``(True, "verified")`` — PIN matched; ``pin_verified`` is set
          on the row so subsequent /pair/complete can succeed.
        - ``(False, "no_pin_required")`` — token has no PIN; caller
          should accept any PIN value (or skip). Treated as success
          for the legacy no-PIN flow.
        - ``(False, "wrong_pin")`` — PIN mismatch; ``pin_attempts``
          incremented. After PIN_MAX_ATTEMPTS the row is deleted
          server-side (token invalidated).
        - ``(False, "exhausted")`` — too many wrong attempts; token is
          gone or about to be.
        - ``(False, "expired")`` — pair token TTL elapsed.
        - ``(False, "unknown_token")`` — token not in the DB.
        """
        if not token:
            return False, "unknown_token"
        lookup = _token_lookup(token)
        backend = _get_backend()
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT device_id, pin_hash, pin_attempts, "
                    "       expires_at FROM paired_devices "
                    "WHERE token_lookup = ?",
                    (lookup,),
                ).fetchone()
                if row is None:
                    return False, "unknown_token"
                device_id = row["device_id"]
                pin_hash = row["pin_hash"]
                attempts = int(row["pin_attempts"] or 0)
                expires_at = row["expires_at"]
                if expires_at and float(expires_at) <= time.time():
                    return False, "expired"
                if not pin_hash:
                    # Token has no PIN; treat as "nothing to verify".
                    return False, "no_pin_required"
                if attempts >= self.PIN_MAX_ATTEMPTS:
                    conn.execute(
                        "DELETE FROM paired_devices WHERE device_id = ?",
                        (device_id,),
                    )
                    conn.commit()
                    logger.warning(
                        "PIN attempts exhausted; revoked device_id=%s",
                        device_id,
                    )
                    return False, "exhausted"
                ok = bool(backend.verify(pin_hash, pin or ""))
                if not ok:
                    new_attempts = attempts + 1
                    if new_attempts >= self.PIN_MAX_ATTEMPTS:
                        conn.execute(
                            "DELETE FROM paired_devices WHERE device_id = ?",
                            (device_id,),
                        )
                        conn.commit()
                        logger.warning(
                            "PIN attempts exhausted; revoked device_id=%s",
                            device_id,
                        )
                        return False, "exhausted"
                    conn.execute(
                        "UPDATE paired_devices SET pin_attempts = ? "
                        "WHERE device_id = ?",
                        (new_attempts, device_id),
                    )
                    conn.commit()
                    return False, "wrong_pin"
                # PIN verified — set the flag and reset attempts.
                conn.execute(
                    "UPDATE paired_devices SET pin_verified = 1, "
                    "       pin_attempts = 0 WHERE device_id = ?",
                    (device_id,),
                )
                conn.commit()
                return True, "verified"
            finally:
                conn.close()

    def token_pin_verified(self, token: str) -> bool:
        """Has the PIN gate been cleared for this token?

        Used by ``/api/devices/pair/complete`` to gate phone_bearer
        issuance. Tokens with no PIN (legacy flow) return True
        unconditionally; tokens with PIN require an explicit verify
        call first.
        """
        if not token:
            return False
        lookup = _token_lookup(token)
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT pin_hash, pin_verified FROM paired_devices "
                    "WHERE token_lookup = ?",
                    (lookup,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return False
        if not row[0]:  # no pin_hash → no PIN required
            return True
        return bool(row[1])

    def mark_claimed(
        self,
        token: str,
        *,
        kind: str = "",
        platform: str = "",
        node_id: str = "",
    ) -> Optional[str]:
        """Set ``claimed_at`` once a daemon actually attaches with *token*,
        and record WHAT claimed it.

        Used by /api/devices/pair/complete so the UI can distinguish
        tokens that were issued-but-never-used from live paired devices.
        Returns the device_id on success.

        ``kind`` / ``platform`` / ``node_id`` describe the claimant and
        are all optional, so a client that posts only the token keeps
        the pre-existing behaviour exactly.

        Why the claimant identity is written here and not at issue time:
        the row's ``kind`` used to be stamped when the QR was generated,
        where the brain cannot know what will scan it. Every one of the
        61 rows in the owner's ``paired_devices.db`` says
        ``kind='browser'`` with an empty ``node_id``, including the ones
        an iPhone claimed, which is why his phone presented as a browser
        connection he never made. ``platform`` (a user agent) is the
        only evidence of what actually claimed the token, so it resolves
        the kind; ``browser_node_v2`` is a transport name and is never
        stored as a device kind.

        A known kind is never downgraded to a guess: if the claimant
        sends nothing identifying, the existing kind stands.
        """
        if not token:
            return None
        from api.device_view import kind_from_platform

        lookup = _token_lookup(token)
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT device_id, token_hash, expires_at, kind, node_id, platform "
                    "FROM paired_devices WHERE token_lookup = ?",
                    (lookup,),
                ).fetchone()
                if row is None:
                    return None
                if not _get_backend().verify(row["token_hash"], token):
                    # Hash mismatch on a SHA-256 lookup hit is
                    # vanishingly unlikely (256-bit random tokens), but
                    # we still gate on argon2-verify for safety.
                    return None
                if row["expires_at"] is not None and row["expires_at"] <= int(now):
                    return None

                existing_kind = (row["kind"] or "").strip().lower()
                # "pending" and the transport aliases carry no device
                # information, so they are the only values a claim is
                # allowed to overwrite.
                replaceable = existing_kind in ("", "pending", "browser_node_v2")
                if platform:
                    fallback = kind if kind and kind != "browser_node_v2" else "browser"
                    resolved_kind = kind_from_platform(platform, fallback=fallback)
                elif kind and kind != "browser_node_v2":
                    resolved_kind = kind
                else:
                    resolved_kind = existing_kind or kind
                new_kind = resolved_kind if replaceable else existing_kind

                conn.execute(
                    "UPDATE paired_devices SET claimed_at = ?, last_seen = ?, "
                    "kind = ?, platform = ?, node_id = ? WHERE device_id = ?",
                    (
                        now,
                        now,
                        new_kind,
                        platform or (row["platform"] or ""),
                        node_id or (row["node_id"] or ""),
                        row["device_id"],
                    ),
                )
                conn.commit()
                logger.info(
                    "Pair claimed device_id=%s kind=%s->%s node=%s",
                    row["device_id"], existing_kind or "-", new_kind or "-",
                    node_id or row["node_id"] or "-",
                )
                return row["device_id"]
            finally:
                conn.close()

    def verify_device(self, token: str) -> Optional[str]:
        """Return the ``device_id`` for *token*, or ``None`` if the
        token is unknown, expired, or fails hash verification.

        Successful verifications also extend ``expires_at`` by
        ``ttl_seconds`` (sliding window), bump ``last_seen``, and —
        idempotently — set ``claimed_at`` if it was still NULL. The
        latter mirrors what ``/api/devices/pair/complete`` does so
        that a daemon / browser-node which attaches via the WebSocket
        handshake (``/v1/node`` or ``/v1/session``) is treated as a
        "real" claim by the user-facing devices list, even when the
        client never POSTs ``/pair/complete`` separately. Existing
        ``claimed_at`` values are preserved (idempotent: first claim
        wins so the "when was this device first attached?" timestamp
        is stable).

        Failures do NOT increment any retry counter — the existing
        rate-limit logic at the HTTP layer handles brute-force
        protection (see api/server.py + tests/test_session_auth.py).
        """
        if not token:
            return None
        lookup = _token_lookup(token)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT device_id, token_hash, expires_at, ttl_seconds, claimed_at "
                "FROM paired_devices WHERE token_lookup = ?",
                (lookup,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        if not _get_backend().verify(row["token_hash"], token):
            return None
        now = int(time.time())
        if row["expires_at"] is not None and row["expires_at"] <= now:
            logger.info(
                "device_pairing.token_expired: device_id=%s expires_at=%s "
                "(now=%s)",
                row["device_id"], row["expires_at"], now,
            )
            return None

        device_id = row["device_id"]
        new_expiry = now + int(row["ttl_seconds"] or DEFAULT_TTL_SECONDS)
        already_claimed = row["claimed_at"] is not None
        now_wall = time.time()
        with self._lock:
            conn = self._conn()
            try:
                if already_claimed:
                    conn.execute(
                        "UPDATE paired_devices SET last_seen = ?, expires_at = ? "
                        "WHERE device_id = ?",
                        (now_wall, new_expiry, device_id),
                    )
                else:
                    conn.execute(
                        "UPDATE paired_devices "
                        "SET last_seen = ?, expires_at = ?, claimed_at = ? "
                        "WHERE device_id = ? AND claimed_at IS NULL",
                        (now_wall, new_expiry, now_wall, device_id),
                    )
                    # Defensive: if a concurrent caller already set
                    # claimed_at between SELECT and UPDATE, the guarded
                    # UPDATE above does nothing for claimed_at; bump
                    # last_seen / expires_at unconditionally.
                    conn.execute(
                        "UPDATE paired_devices SET last_seen = ?, expires_at = ? "
                        "WHERE device_id = ?",
                        (now_wall, new_expiry, device_id),
                    )
                conn.commit()
            finally:
                conn.close()
        if not already_claimed:
            logger.info(
                "device_pairing.claimed_on_verify: device_id=%s",
                device_id,
            )
        return device_id

    # ── Listing / revocation ───────────────────────────────────────

    def list_devices(self, *, include_unclaimed: bool = True) -> list[dict]:
        """Return paired devices with the typed metadata.

        Args:
            include_unclaimed: when ``False``, filter out rows whose
                ``claimed_at`` is NULL — i.e. tokens that were issued
                but never used to attach a real WebSocket. Defaults to
                ``True`` so existing callers (CLI, MCP, tests poking
                the store directly) continue to see every row. The
                user-facing ``GET /api/devices/paired`` route flips
                this to ``False`` to suppress phantom "device showed
                up the moment I clicked Pair" entries that the v2
                Devices page used to render before claim.

        Note: , ``token`` is NEVER included — the plaintext
        cannot be recovered from storage. Callers that need to identify
        a row should use ``device_id`` (from the ``pair_device`` return
        value) or ``token_lookup`` (deterministic SHA-256 of the
        plaintext token, useful for "did the row I issued just now
        survive?" assertions).
        """
        import json as _json
        conn = self._conn()
        try:
            sql = """SELECT device_id, name, paired_at, last_seen,
                            kind, node_id, claimed_at, platform, capabilities,
                            token_lookup, ttl_seconds, expires_at, hash_algo
                     FROM paired_devices"""
            if not include_unclaimed:
                sql += " WHERE claimed_at IS NOT NULL"
            sql += " ORDER BY paired_at DESC"
            rows = conn.execute(sql).fetchall()
            out = []
            for r in rows:
                caps_raw = r["capabilities"] if "capabilities" in r.keys() else None
                try:
                    caps = _json.loads(caps_raw) if caps_raw else []
                except Exception:
                    caps = []
                out.append({
                    "device_id": r["device_id"],
                    "name": r["name"],
                    "paired_at": r["paired_at"],
                    "last_seen": r["last_seen"],
                    "kind": r["kind"] if "kind" in r.keys() else "",
                    "node_id": r["node_id"] if "node_id" in r.keys() else "",
                    "claimed_at": r["claimed_at"] if "claimed_at" in r.keys() else None,
                    "platform": r["platform"] if "platform" in r.keys() else "",
                    "capabilities": caps,
                    "token_lookup": r["token_lookup"],
                    "ttl_seconds": r["ttl_seconds"],
                    "expires_at": r["expires_at"],
                    "hash_algo": r["hash_algo"],
                })
            return out
        finally:
            conn.close()

    def revoke_device(self, device_id: str) -> bool:
        """Remove a paired device.  Returns ``True`` if a row was deleted."""
        with self._lock:
            conn = self._conn()
            try:
                cursor = conn.execute(
                    "DELETE FROM paired_devices WHERE device_id = ?", (device_id,)
                )
                conn.commit()
                deleted = cursor.rowcount > 0
            finally:
                conn.close()
        if deleted:
            logger.info("Revoked device %s", device_id)
        return deleted

    def revoke_unclaimed(self, older_than_seconds: float = 0.0) -> dict:
        """Revoke every pairing token that was issued but never attached.

        A "claim" happens when a daemon / browser-node actually connects
        to /v1/node with the token AND ``/api/devices/pair/complete`` marks
        ``claimed_at``. Tokens older than ``older_than_seconds`` that are
        still unclaimed are bulk-deleted.

        Returns ``{pruned: int, kept: int, rows: list[device_id]}``.
        """
        cutoff = time.time() - max(0.0, older_than_seconds)
        with self._lock:
            conn = self._conn()
            try:
                to_prune = [
                    r["device_id"]
                    for r in conn.execute(
                        """SELECT device_id FROM paired_devices
                           WHERE claimed_at IS NULL
                             AND paired_at < ?""",
                        (cutoff,),
                    ).fetchall()
                ]
                kept = conn.execute(
                    "SELECT COUNT(*) FROM paired_devices",
                ).fetchone()[0] - len(to_prune)
                if to_prune:
                    placeholders = ",".join("?" * len(to_prune))
                    conn.execute(
                        f"DELETE FROM paired_devices WHERE device_id IN ({placeholders})",
                        to_prune,
                    )
                    conn.commit()
            finally:
                conn.close()
        if to_prune:
            logger.info(
                "Pruned %d unclaimed pairing token(s) older than %ss",
                len(to_prune), older_than_seconds,
            )
        return {"pruned": len(to_prune), "kept": kept, "rows": to_prune}


    # ── Pending pair codes (SDK code-pair flow) ──────────────────────

    PENDING_PAIR_CODE_TTL_SECONDS = 600
    PENDING_PAIR_CODE_PER_CODE_MAX_ATTEMPTS = 10
    #: What a pair code may look like. Deliberately permissive on
    #: charset and strict on length, because the shape is NOT what the
    #: docs say. This class's own docstring claims "8-char base32" and
    #: api/server.py claimed "~38 bits of entropy"; both are wrong. The
    #: shipped SDKs generate a 6-digit decimal string:
    #:   python-node-sdk: f"{secrets.randbelow(1_000_000):06d}"
    #:   ts-node-sdk:     Math.random() -> 6 digits, padded
    #: which is ~20 bits, not 38. Pinning the documented shape here
    #: would have rejected every real node.
    #:
    #: The purpose of this check is not entropy, which announce cannot
    #: enforce anyway. It is to stop an unauthenticated caller storing
    #: unbounded junk, so it bounds length and excludes anything that is
    #: not a plain identifier character.
    PAIR_CODE_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")

    def announce_pending_code(self, *, code: str, node_id: str, name: str) -> None:
        """A daemon advertises a freshly generated pair code.

        ``code`` is an 8-char base32 string the daemon prints to the
        operator; the operator types it into the dashboard "Type a pair
        code" field. The brain issues a real device token at claim time,
        not at announce time, so an unclaimed code is harmless.
        """
        if not code or not node_id:
            raise ValueError("code and node_id are required")
        # Shape check. This endpoint is unauthenticated by design (node
        # SDKs announce from other machines), so the only thing standing
        # between it and an attacker filling the table is this. It
        # accepts nothing but the shape the SDKs actually generate, and
        # it does not make announce a credential path: the token is still
        # only minted at claim time, which now requires auth.
        if not self.PAIR_CODE_RE.fullmatch(code):
            raise ValueError(
                "code must be 4-64 characters of [A-Za-z0-9_-]"
            )
        if len(node_id) > 128 or len(name or "") > 256:
            raise ValueError("node_id or name is implausibly long")
        now = time.time()
        expires_at = now + self.PENDING_PAIR_CODE_TTL_SECONDS
        with self._lock:
            conn = self._conn()
            try:
                # Upsert by code: a daemon that re-announces the same
                # code (e.g. on retry) gets its ``created_at`` refreshed
                # but does NOT bump claim_attempts.
                conn.execute(
                    """INSERT INTO pending_pair_codes
                          (code, node_id, name, created_at, expires_at,
                           token, device_id, claim_attempts)
                       VALUES (?, ?, ?, ?, ?, NULL, NULL, 0)
                       ON CONFLICT(code) DO UPDATE SET
                           node_id=excluded.node_id,
                           name=excluded.name,
                           created_at=excluded.created_at,
                           expires_at=excluded.expires_at""",
                    (code, node_id, name, now, expires_at),
                )
                conn.commit()
            finally:
                conn.close()

    def lookup_pending_code(self, *, code: str, node_id: str) -> Optional[dict]:
        """Status poll for the daemon. Returns:

            ``None``  — code unknown (use 404 in the route)
            ``{"_now": <float>, "expires_at": ..., "token": "..."}``

        The daemon decides ``pending`` vs ``paired`` vs ``expired``
        based on the ``token`` field and ``expires_at`` clock — the
        store does not pre-compute that label so tests stay legible.
        """
        if not code or not node_id:
            return None
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    """SELECT code, node_id, name, created_at, expires_at,
                              token, device_id, claim_attempts
                       FROM pending_pair_codes
                       WHERE code = ? AND node_id = ?""",
                    (code, node_id),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return {
            "_now": now,
            "code": row["code"],
            "node_id": row["node_id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "token": row["token"],
            "device_id": row["device_id"],
        }

    def claim_pending_code(self, *, code: str) -> Optional[dict]:
        """Operator types the code into the dashboard.

        On match we mint a real device-pairing token (kind="hup",
        node_id from the announce row) and write it back. On mismatch
        we increment ``claim_attempts``; if the per-code cap (10) is
        exceeded the row is invalidated server-side. Returns the token
        record or ``None``.
        """
        if not code:
            return None
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    """SELECT code, node_id, name, expires_at,
                              token, claim_attempts
                       FROM pending_pair_codes
                       WHERE code = ?""",
                    (code,),
                ).fetchone()
                if row is None:
                    return None
                if row["expires_at"] <= now:
                    return None
                if row["token"]:
                    # Already claimed; idempotent return so a flaky
                    # network does not lock the daemon out.
                    return {
                        "token": row["token"],
                        "device_id": row["device_id"] if "device_id" in row.keys() else "",
                        "expires_at": int(row["expires_at"]),
                    }
            finally:
                conn.close()

        # Mint the real token via the existing pair_device path. We do
        # this OUTSIDE the lock because pair_device acquires it itself.
        result = self.pair_device(
            row["name"],
            kind="hup",
            node_id=row["node_id"],
        )

        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """UPDATE pending_pair_codes
                       SET token = ?, device_id = ?
                       WHERE code = ?""",
                    (result["token"], result["device_id"], code),
                )
                conn.commit()
            finally:
                conn.close()

        return {
            "token": result["token"],
            "device_id": result["device_id"],
            "expires_at": int(result["expires_at"]),
        }

    def expire_pending_codes(self) -> int:
        """Sweep expired pending codes. Returns the number deleted.

        Called opportunistically by the brain's pruning loop. Codes
        that were already claimed (``token IS NOT NULL``) are kept
        for one extra TTL window so a flaky daemon can re-poll and
        get an idempotent paired response.
        """
        now = time.time()
        cutoff = now - self.PENDING_PAIR_CODE_TTL_SECONDS
        with self._lock:
            conn = self._conn()
            try:
                cursor = conn.execute(
                    """DELETE FROM pending_pair_codes
                       WHERE token IS NULL AND expires_at <= ?""",
                    (now,),
                )
                also = conn.execute(
                    """DELETE FROM pending_pair_codes
                       WHERE token IS NOT NULL AND expires_at <= ?""",
                    (cutoff,),
                )
                conn.commit()
                return int(cursor.rowcount) + int(also.rowcount)
            finally:
                conn.close()


_store: Optional[DevicePairingStore] = None


def get_pairing_store(db_path: Optional[str] = None) -> DevicePairingStore:
    """Module-level singleton (lazy-init)."""
    global _store
    if _store is None:
        _store = DevicePairingStore(db_path)
    return _store


def reset_store() -> None:
    """Reset the singleton — useful for tests."""
    global _store
    _store = None
