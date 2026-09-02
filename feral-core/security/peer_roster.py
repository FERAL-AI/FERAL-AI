"""
FERAL Sync Peer Roster
======================
Per-peer identity for brain-to-brain memory sync.

Before this module, ``/sync`` authenticated every peer with ONE shared
plaintext passphrase (``memory.sync.ensure_sync_passphrase``), compared
with a plain ``!=``. That has three consequences an operator cannot work
around:

* every peer is the same principal, so "who wrote this" and "stop
  sharing with that one brain" are both unanswerable;
* rotating the secret re-pairs every peer at once (the docstring of
  ``ensure_sync_passphrase`` already admitted this);
* there is no membership list at all, so
  ``MemoryStore.prune_tombstones`` cannot prune by "every peer has
  acknowledged this delete" and says so in its own docstring.

The mechanism here is NOT new. It is ``security/device_pairing.py``
applied to the other side of the brain: the same argon2id-primary /
bcrypt-fallback backend, the same SHA-256 ``token_lookup`` O(1) index,
the same "plaintext returned exactly once at issue time", and the same
``needs_rotation`` ledger used as the migration path off a legacy
plaintext secret. The hash backend is *imported* from that module rather
than re-declared, so there is one place where the argon2-or-bcrypt
choice is made and one place where it is logged.

A peer brain is a fuller principal than a device
------------------------------------------------
A paired device streams events at the brain. A peer brain replicates
inserts AND deletes into the local store, so the defaults are tighter
than ``device_pairing``'s:

* ``invite_expires_at``, a HARD, non-sliding deadline (1 hour by
  default) by which an invite must be redeemed. An invite nobody
  redeems is dead, not pending forever.
* ``expires_at``, a SLIDING grant window (7 days by default) renewed on
  every successful handshake. A peer that stops talking for a week
  lapses on its own. This is deliberate: revocation cannot recall data
  that has already replicated, so a grant that lapses is worth more than
  a revocation list that has to be pushed.
* the credential is BOUND to the presenting ``node_id`` on first use
  (trust-on-first-use inside the invite window, exactly like
  ``DevicePairingStore.mark_claimed``). After binding, the same secret
  presented by a different brain is refused.

What revocation actually achieves
---------------------------------
``revoke_peer`` stops FUTURE exchanges. It does not and cannot un-send
memory the peer already holds, and it cannot delete the copy on their
disk. Read it as "this brain will no longer accept or send", never as
"the data came back". ``revoke_scope`` is the same sentence one level
down: it stops future replication in one named scope and recalls
nothing that already crossed.

Scope grants: whether, then what
--------------------------------
``sync_peers`` answers WHETHER a brain may connect. ``peer_scope_grants``
answers WHAT it gets once it has, and the default is nothing. Enrolling
a peer hands it no memory; an operator has to name a scope with
``grant_scope`` before one byte replicates. See ``security/sync_scopes.py``
for the vocabulary and the fail-closed rules, and ``memory/sync.py`` for
the two enforcement points (send filter and receive check).

Migration off the shared passphrase
-----------------------------------
Nothing breaks on upgrade. Three modes, resolved by
:func:`identity_mode`:

``shared_passphrase``
    No peer has been enrolled yet. The shared passphrase is still
    accepted, every acceptance is logged at WARNING and recorded in
    ``shared_secret_log``, and the mode string says so.

``mixed``
    At least one peer is enrolled, but the shared passphrase is STILL
    accepted so an un-migrated peer keeps working. The mode is never
    reported as identity-authenticated while this is true.

``per_peer``
    ``FERAL_SYNC_REQUIRE_PEER_IDENTITY=1``. The shared passphrase is
    refused with a message naming the command that fixes it. Operator
    flips this once ``shared_secret_peers()`` is empty; it is never
    flipped automatically, because auto-promotion is precisely how a
    working two-brain setup would break in silence.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

from security.device_pairing import _get_backend, _token_lookup

logger = logging.getLogger("feral.security.peer_roster")

#: Sliding grant window, renewed on every successful handshake. A peer
#: that has not synced for this long must be re-invited.
DEFAULT_PEER_TTL_SECONDS = int(
    os.getenv("FERAL_SYNC_PEER_TTL_SECONDS", str(7 * 24 * 60 * 60))
)

#: Hard deadline for redeeming an invite. Not sliding: an invite that is
#: never used stays dead rather than lingering as a live credential.
DEFAULT_INVITE_TTL_SECONDS = int(
    os.getenv("FERAL_SYNC_PEER_INVITE_TTL_SECONDS", str(60 * 60))
)

#: Vault namespace for OUTBOUND grants (the secrets this brain presents
#: when it dials a peer). The inbound half is hashed in SQLite; the
#: outbound half has to be recoverable by construction, so it lives
#: encrypted at rest in the BlindVault next to the sync passphrase
#: rather than in a plaintext file.
VAULT_NAMESPACE = "sync_peer_grants"

#: Test / embedding seam. When set, :func:`_vault` returns this instead
#: of the process vault, so a test never writes into the operator's real
#: ``~/.feral`` vault.
_VAULT_OVERRIDE = None

#: In-process mirror of the outbound grants. Populated by
#: :func:`store_outbound_grant` and by the first vault read. Keeps sync
#: working for the rest of the process when the vault is unavailable
#: (same degradation ``ensure_sync_passphrase`` already chose).
_GRANT_CACHE: dict[str, dict] = {}

#: Has the vault been read into ``_GRANT_CACHE`` yet? The dialling path
#: asks for a grant on every handshake and the vault is keychain-backed,
#: so an unconditional read there would put a keychain round-trip on the
#: sync cadence. One read per process, refreshed on write.
_GRANT_CACHE_LOADED = False


def require_peer_identity() -> bool:
    """Is the brain refusing the shared passphrase outright?"""
    return os.getenv("FERAL_SYNC_REQUIRE_PEER_IDENTITY", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ─────────────────────────────────────────────────────────────────────
# PeerRoster
# ─────────────────────────────────────────────────────────────────────


class PeerRoster:
    """SQLite-backed registry of peer brains allowed to sync with us.

    Default path: ``$FERAL_HOME/peer_roster.db``.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            home = os.environ.get("FERAL_HOME", str(Path.home() / ".feral"))
            Path(home).mkdir(parents=True, exist_ok=True)
            db_path = str(Path(home) / "peer_roster.db")
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    @property
    def db_path(self) -> str:
        return self._db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sync_peers (
                        peer_row_id       TEXT PRIMARY KEY,
                        name              TEXT NOT NULL,
                        node_id           TEXT,
                        token_lookup      TEXT,
                        token_hash        TEXT,
                        hash_algo         TEXT,
                        created_at        REAL NOT NULL,
                        bound_at          REAL,
                        last_seen         REAL,
                        last_address      TEXT,
                        ttl_seconds       INTEGER NOT NULL,
                        expires_at        INTEGER,
                        invite_expires_at INTEGER NOT NULL,
                        departed_at       REAL,
                        revoked_at        REAL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sp_token_lookup "
                    "ON sync_peers(token_lookup)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sp_node_id "
                    "ON sync_peers(node_id)"
                )
                # The migration ledger. Same role needs_rotation_log
                # plays for device pairing: it is how an operator is
                # told, loudly and durably, that a peer is still
                # authenticating with the shared secret.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS shared_secret_log (
                        node_id     TEXT PRIMARY KEY,
                        first_seen  REAL NOT NULL,
                        last_seen   REAL NOT NULL,
                        address     TEXT,
                        uses        INTEGER NOT NULL DEFAULT 1,
                        announced   INTEGER NOT NULL DEFAULT 0,
                        reason      TEXT NOT NULL
                    )
                """)
                # Per-peer scope grants: WHAT this brain shares with a
                # peer, as opposed to sync_peers, which is WHETHER a
                # peer may connect at all.
                #
                # Keyed on node_id, not peer_row_id, and that is the
                # load-bearing choice. node_id is the only identifier
                # present at BOTH enforcement points: the send filter
                # reads it from the handshake response, the receive
                # check reads it from the handshake request, and the
                # WAL's own ``origin_node`` is a node_id. peer_row_id
                # exists only on the brain that minted the credential,
                # so the dialling side has no row to key on and a
                # peer_row_id table would leave one of the two
                # enforcement points unable to answer the question.
                #
                # A grant is authorisation layered strictly AFTER
                # authentication. A row here admits nobody: the peer
                # still has to get past ``authenticate_sync_peer``.
                # That is why revoking a credential does not need to
                # cascade into this table.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS peer_scope_grants (
                        node_id     TEXT NOT NULL,
                        scope       TEXT NOT NULL,
                        granted_at  REAL NOT NULL,
                        note        TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (node_id, scope)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_psg_node_id "
                    "ON peer_scope_grants(node_id)"
                )
                conn.commit()
            finally:
                conn.close()

    # ── Enrolment ──────────────────────────────────────────────────

    def invite_peer(
        self,
        name: str,
        *,
        ttl_seconds: int = DEFAULT_PEER_TTL_SECONDS,
        invite_ttl_seconds: int = DEFAULT_INVITE_TTL_SECONDS,
    ) -> dict:
        """Mint a per-peer grant. The plaintext ``secret`` is in the
        return value EXACTLY ONCE and is unrecoverable afterwards.

        The operator carries it to the other brain and runs
        ``feral sync peer accept``. Until it is redeemed, the row is
        unbound: it names no ``node_id`` and dies at
        ``invite_expires_at``.
        """
        if not (name or "").strip():
            raise ValueError("name is required")
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive (got {ttl_seconds})")
        if invite_ttl_seconds <= 0:
            raise ValueError(
                f"invite_ttl_seconds must be positive (got {invite_ttl_seconds})"
            )

        peer_row_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        now = time.time()
        invite_expires_at = int(now) + int(invite_ttl_seconds)
        backend = _get_backend()

        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO sync_peers
                       (peer_row_id, name, node_id, token_lookup, token_hash,
                        hash_algo, created_at, ttl_seconds, invite_expires_at)
                       VALUES (?, ?, '', ?, ?, ?, ?, ?, ?)""",
                    (
                        peer_row_id, name.strip(),
                        _token_lookup(secret), backend.hash(secret),
                        backend.name, now, int(ttl_seconds), invite_expires_at,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        logger.info(
            "peer_roster.invited name=%s row=%s invite_ttl=%ss grant_ttl=%ss",
            name, peer_row_id, invite_ttl_seconds, ttl_seconds,
        )
        return {
            "peer_row_id": peer_row_id,
            "name": name.strip(),
            "secret": secret,
            "invite_expires_at": invite_expires_at,
            "invite_ttl_seconds": int(invite_ttl_seconds),
            "ttl_seconds": int(ttl_seconds),
        }

    def verify_peer(
        self,
        secret: str,
        *,
        node_id: str,
        address: str = "",
    ) -> Optional[dict]:
        """Authenticate one inbound handshake. Returns the peer record
        on success, ``None`` on every failure.

        SHA-256 ``token_lookup`` finds the row in O(1); argon2id is what
        actually verifies it; ``hmac.compare_digest`` gates the
        node-identity binding so a matched credential presented by the
        wrong brain does not leak through a short-circuiting ``==``.

        Success renews the sliding window, stamps ``last_seen`` /
        ``last_address``, and clears any recorded departure.
        """
        if not secret:
            return None
        lookup = _token_lookup(secret)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM sync_peers WHERE token_lookup = ?",
                (lookup,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        if not _get_backend().verify(row["token_hash"] or "", secret):
            return None
        if row["revoked_at"] is not None:
            logger.warning(
                "peer_roster.revoked_credential_presented row=%s name=%s",
                row["peer_row_id"], row["name"],
            )
            return None

        now = int(time.time())
        bound_node = (row["node_id"] or "").strip()
        presented = (node_id or "").strip()

        if bound_node:
            if not presented or not hmac.compare_digest(bound_node, presented):
                logger.warning(
                    "peer_roster.node_id_mismatch row=%s bound=%s presented=%s "
                    "- a grant is valid for exactly one brain.",
                    row["peer_row_id"], bound_node, presented or "-",
                )
                return None
            if row["expires_at"] is not None and int(row["expires_at"]) <= now:
                logger.info(
                    "peer_roster.grant_lapsed row=%s node=%s expires_at=%s (now=%s) "
                    "- re-invite the peer.",
                    row["peer_row_id"], bound_node, row["expires_at"], now,
                )
                return None
        else:
            if int(row["invite_expires_at"]) <= now:
                logger.info(
                    "peer_roster.invite_expired row=%s name=%s, mint a new one.",
                    row["peer_row_id"], row["name"],
                )
                return None
            if not presented:
                logger.warning(
                    "peer_roster.unbound_grant_without_node_id row=%s, the "
                    "handshake must advertise a node_id to bind a grant.",
                    row["peer_row_id"],
                )
                return None

        ttl = int(row["ttl_seconds"] or DEFAULT_PEER_TTL_SECONDS)
        new_expiry = now + ttl
        now_wall = time.time()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """UPDATE sync_peers
                       SET node_id = ?,
                           bound_at = COALESCE(bound_at, ?),
                           last_seen = ?,
                           last_address = ?,
                           expires_at = ?,
                           departed_at = NULL
                       WHERE peer_row_id = ?""",
                    (
                        presented, now_wall, now_wall,
                        address or (row["last_address"] or ""),
                        new_expiry, row["peer_row_id"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        if not bound_node:
            logger.info(
                "peer_roster.bound row=%s name=%s node_id=%s addr=%s, this "
                "grant is now valid only for that brain.",
                row["peer_row_id"], row["name"], presented, address or "-",
            )
        return {
            "peer_row_id": row["peer_row_id"],
            "name": row["name"],
            "node_id": presented,
            "bound": True,
            "newly_bound": not bound_node,
            "expires_at": new_expiry,
            "ttl_seconds": ttl,
            "last_address": address or (row["last_address"] or ""),
        }

    # ── Membership ─────────────────────────────────────────────────

    def list_peers(self) -> list[dict]:
        """Every roster row. Never contains a secret: the plaintext
        cannot be recovered from storage."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT peer_row_id, name, node_id, created_at, bound_at, "
                "       last_seen, last_address, ttl_seconds, expires_at, "
                "       invite_expires_at, departed_at, revoked_at, hash_algo "
                "FROM sync_peers ORDER BY created_at DESC"
            ).fetchall()
        finally:
            conn.close()
        now = int(time.time())
        out = []
        for r in rows:
            bound = bool((r["node_id"] or "").strip())
            if r["revoked_at"] is not None:
                status = "revoked"
            elif not bound:
                status = (
                    "invited" if int(r["invite_expires_at"]) > now
                    else "invite_expired"
                )
            elif r["expires_at"] is not None and int(r["expires_at"]) <= now:
                status = "lapsed"
            elif r["departed_at"] is not None:
                status = "departed"
            else:
                status = "active"
            out.append({
                "peer_row_id": r["peer_row_id"],
                "name": r["name"],
                "node_id": r["node_id"] or "",
                "status": status,
                "created_at": r["created_at"],
                "bound_at": r["bound_at"],
                "last_seen": r["last_seen"],
                "last_address": r["last_address"] or "",
                "ttl_seconds": r["ttl_seconds"],
                "expires_at": r["expires_at"],
                "invite_expires_at": r["invite_expires_at"],
                "departed_at": r["departed_at"],
                "revoked_at": r["revoked_at"],
                "hash_algo": r["hash_algo"],
                # What is actually shared with this peer. Empty until an
                # operator grants something: enrolling a brain lets it
                # connect, it does not hand it any memory.
                "scopes": sorted(self.granted_scopes(r["node_id"] or "")),
            })
        return out

    def has_enrolled_peers(self) -> bool:
        """Has any peer ever redeemed a grant on this brain?

        Deliberately counts BOUND rows, not invites. An operator who
        minted an invite and never used it has not migrated anything,
        and reporting ``mixed`` at that point would be a lie in the
        reassuring direction.
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_peers "
                "WHERE node_id IS NOT NULL AND node_id != '' "
                "AND revoked_at IS NULL"
            ).fetchone()
        finally:
            conn.close()
        return int(row["n"]) > 0

    def active_peer_ids(self, within_seconds: Optional[float] = None) -> list[str]:
        """Node ids of peers that are currently members.

        This is the liveness roster ``MemoryStore.prune_tombstones``
        names as missing. A peer counts when it is bound, not revoked,
        not marked departed, inside its sliding window, and (optionally)
        seen within ``within_seconds``.
        """
        now = time.time()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT node_id, last_seen, expires_at FROM sync_peers "
                "WHERE node_id IS NOT NULL AND node_id != '' "
                "AND revoked_at IS NULL AND departed_at IS NULL"
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            if r["expires_at"] is not None and int(r["expires_at"]) <= int(now):
                continue
            if within_seconds is not None:
                seen = r["last_seen"]
                if seen is None or (now - float(seen)) > float(within_seconds):
                    continue
            out.append(r["node_id"])
        return sorted(set(out))

    def mark_seen(self, node_id: str, *, address: str = "") -> bool:
        """Record liveness for a bound peer without re-authenticating.

        Called from mDNS discovery: seeing a peer advertise itself is
        evidence it exists, which is what ``last_seen`` means. It is NOT
        evidence it holds a valid grant, so this never touches
        ``expires_at``.
        """
        if not node_id:
            return False
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE sync_peers SET last_seen = ?, last_address = ?, "
                    "       departed_at = NULL "
                    "WHERE node_id = ? AND node_id != ''",
                    (now, address or "", node_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def mark_departed(self, node_id: str, *, address: str = "") -> bool:
        """Record that a peer left the network.

        ``PeerListener.remove_service`` was ``pass``, so a peer that
        went away stayed in ``SyncEngine._peers`` forever and its
        last-seen was never written anywhere that survives a restart.
        A departure is a membership fact, so it is persisted here rather
        than only dropped from an in-memory dict.

        Departure is NOT revocation. The grant is still valid; the brain
        is simply not on the network right now.
        """
        if not node_id:
            return False
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE sync_peers SET departed_at = ?, last_seen = ?, "
                    "       last_address = COALESCE(NULLIF(?, ''), last_address) "
                    "WHERE node_id = ? AND node_id != '' AND departed_at IS NULL",
                    (now, now, address or "", node_id),
                )
                conn.commit()
                changed = cur.rowcount > 0
            finally:
                conn.close()
        if changed:
            logger.info("peer_roster.departed node=%s addr=%s", node_id, address or "-")
        return changed

    def revoke_peer(self, peer_row_id: str) -> bool:
        """Stop accepting this grant.

        What this achieves: no future handshake with this credential is
        accepted, and no further memory flows either way over it.

        What it does NOT achieve: the peer keeps every operation it has
        already replicated. Revocation is not recall. Say that to the
        operator rather than letting the word imply otherwise.
        """
        if not peer_row_id:
            return False
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                # ``token_lookup`` and ``token_hash`` are deliberately
                # KEPT. Neither is plaintext, and keeping them is what
                # lets ``verify_peer`` tell "someone is still presenting
                # the grant you revoked" apart from "someone presented
                # garbage". Dropping the lookup index would make the
                # revoked row unfindable and that signal unloggable.
                cur = conn.execute(
                    "UPDATE sync_peers SET revoked_at = ? "
                    "WHERE peer_row_id = ? AND revoked_at IS NULL",
                    (now, peer_row_id),
                )
                conn.commit()
                changed = cur.rowcount > 0
            finally:
                conn.close()
        if changed:
            logger.warning(
                "peer_roster.revoked row=%s, future exchanges refused. Memory "
                "already replicated to that peer is NOT recalled.",
                peer_row_id,
            )
        return changed

    # ── Scope grants ───────────────────────────────────────────────

    def grant_scope(self, node_id: str, scope: str, *, note: str = "") -> str:
        """Share one named scope with one peer brain.

        Returns the normalised scope name. Raises
        :class:`~security.sync_scopes.InvalidScopeError` on a name that
        could never replicate, including the reserved ``private``: a
        grant that silently does nothing is worse than a refusal,
        because the operator walks away believing sharing is on.

        Nothing is granted by default. A peer that has authenticated
        but holds no row here receives nothing and may send nothing.
        """
        from security.sync_scopes import require_shareable_scope

        node = (node_id or "").strip()
        if not node:
            raise ValueError("node_id is required to grant a scope")
        normalised = require_shareable_scope(scope)
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO peer_scope_grants (node_id, scope, granted_at, note) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(node_id, scope) DO UPDATE SET note = excluded.note",
                    (node, normalised, now, note or ""),
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(
            "peer_roster.scope_granted node=%s scope=%s, operations in this "
            "scope will now replicate to and from that brain.",
            node, normalised,
        )
        return normalised

    def revoke_scope(self, node_id: str, scope: str) -> bool:
        """Stop sharing one scope with one peer. Returns whether a grant
        was actually removed.

        What this achieves: from the next exchange onward, no operation
        in this scope is sent to that peer, and any operation in this
        scope the peer sends is rejected.

        What it does NOT achieve: every operation already replicated
        under this scope is on the peer's disk and stays there. That
        brain is owned by somebody else. Revocation is not recall, and
        nothing in this codebase can make it one.
        """
        from security.sync_scopes import normalise_scope

        node = (node_id or "").strip()
        normalised = normalise_scope(scope)
        if not node:
            return False
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "DELETE FROM peer_scope_grants WHERE node_id = ? AND scope = ?",
                    (node, normalised),
                )
                conn.commit()
                removed = (cur.rowcount or 0) > 0
            finally:
                conn.close()
        if removed:
            logger.warning(
                "peer_roster.scope_revoked node=%s scope=%s, future exchanges "
                "in this scope refused. Data already replicated under it is "
                "NOT recalled.",
                node, normalised,
            )
        return removed

    def granted_scopes(self, node_id: str) -> frozenset[str]:
        """The scope set shared with one peer. Empty for an unknown peer.

        This is the value both enforcement points consult, so it fails
        closed at every step: a blank node_id, a database error, or a
        stored name that no longer parses all resolve to the empty set
        rather than to "everything". ``normalise_scope_set`` also drops
        ``private``, so a hand-edited row naming the reserved scope
        cannot widen a grant.
        """
        from security.sync_scopes import DENY_ALL, normalise_scope_set

        node = (node_id or "").strip()
        if not node:
            return DENY_ALL
        try:
            conn = self._conn()
        except sqlite3.Error as exc:
            logger.warning(
                "peer_roster.scope_lookup_failed node=%s: %s, denying all "
                "scopes for this exchange.", node, exc,
            )
            return DENY_ALL
        try:
            rows = conn.execute(
                "SELECT scope FROM peer_scope_grants WHERE node_id = ?",
                (node,),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning(
                "peer_roster.scope_lookup_failed node=%s: %s, denying all "
                "scopes for this exchange.", node, exc,
            )
            return DENY_ALL
        finally:
            conn.close()
        return normalise_scope_set(r["scope"] for r in rows)

    def list_scope_grants(self) -> list[dict]:
        """Every scope grant, for ``feral sync peer scope list``."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT node_id, scope, granted_at, note FROM peer_scope_grants "
                "ORDER BY node_id, scope"
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "node_id": r["node_id"],
                "scope": r["scope"],
                "granted_at": r["granted_at"],
                "note": r["note"] or "",
            }
            for r in rows
        ]

    def prune_unredeemed_invites(self) -> int:
        """Delete invites that expired without ever being redeemed."""
        now = int(time.time())
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "DELETE FROM sync_peers "
                    "WHERE (node_id IS NULL OR node_id = '') "
                    "AND invite_expires_at <= ?",
                    (now,),
                )
                conn.commit()
                removed = cur.rowcount or 0
            finally:
                conn.close()
        if removed:
            logger.info("peer_roster.pruned_unredeemed_invites count=%d", removed)
        return removed

    # ── Shared-passphrase migration ledger ─────────────────────────

    def record_shared_secret_peer(self, node_id: str, *, address: str = "") -> None:
        """A peer authenticated with the shared passphrase. Log it.

        The ledger is the whole migration story: it is how the operator
        finds out WHICH brains still have to be enrolled, and it is why
        :func:`identity_mode` can refuse to call a setup
        identity-authenticated while any entry is unacknowledged.
        """
        node_id = (node_id or "unknown").strip() or "unknown"
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO shared_secret_log
                          (node_id, first_seen, last_seen, address, uses,
                           announced, reason)
                       VALUES (?, ?, ?, ?, 1, 0, ?)
                       ON CONFLICT(node_id) DO UPDATE SET
                           last_seen = excluded.last_seen,
                           address = excluded.address,
                           uses = uses + 1""",
                    (node_id, now, now, address or "", "shared_passphrase"),
                )
                conn.commit()
            finally:
                conn.close()

    def shared_secret_peers(self) -> list[dict]:
        """Peers still authenticating with the shared passphrase."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT node_id, first_seen, last_seen, address, uses, "
                "       announced, reason FROM shared_secret_log "
                "ORDER BY last_seen DESC"
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def acknowledge_shared_secret(self, node_id: str) -> bool:
        """Mark one ledger entry announced, so the operator is not
        warned about the same brain on every single handshake."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE shared_secret_log SET announced = 1 WHERE node_id = ?",
                    (node_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def clear_shared_secret_peer(self, node_id: str) -> bool:
        """Drop a ledger entry once that brain is enrolled."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "DELETE FROM shared_secret_log WHERE node_id = ?", (node_id,)
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()


# ─────────────────────────────────────────────────────────────────────
# Authentication decision
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SyncPeerAuth:
    """The outcome of one ``/sync`` handshake credential check."""

    ok: bool
    mode: str            # "per_peer" | "shared" | "rejected"
    reason: str          # machine-readable
    message: str = ""    # operator-facing, sent on the wire when rejected
    peer: Optional[dict] = field(default=None)


def identity_mode(roster: Optional[PeerRoster], *, strict: Optional[bool] = None) -> str:
    """``per_peer`` | ``mixed`` | ``shared_passphrase``.

    Never reports ``per_peer`` on the strength of enrolments alone. As
    long as the shared passphrase would still be accepted, the answer is
    ``mixed`` even if every peer happens to be enrolled, because the
    endpoint would accept a shared-secret handshake from anything that
    knows the passphrase.
    """
    if strict is None:
        strict = require_peer_identity()
    if strict:
        return "per_peer"
    if roster is not None and roster.has_enrolled_peers():
        return "mixed"
    return "shared_passphrase"


_ENROL_HINT = (
    "Run `feral sync peer invite <name>` on this brain and "
    "`feral sync peer accept <host:port> <grant>` on the peer."
)


def authenticate_sync_peer(
    *,
    node_id: str,
    secret: str,
    passphrase: str,
    expected_passphrase: str,
    roster: Optional[PeerRoster],
    address: str = "",
    strict: Optional[bool] = None,
) -> SyncPeerAuth:
    """Decide whether one ``/sync`` handshake is allowed in.

    Lives here rather than inline in ``api/server.py`` so the decision
    is unit-testable without standing up a websocket, and so there is
    exactly one place that knows the precedence rules.

    Precedence, and why:

    1. A presented per-peer grant is the ONLY thing consulted when one
       is presented. A bad grant is a rejection, never a fall-through to
       the shared passphrase: falling through would hand an attacker a
       free downgrade, and would also mask a genuinely lapsed grant as a
       working sync.
    2. Otherwise the shared passphrase, unless ``strict``.
    3. Comparison is ``hmac.compare_digest``, not ``!=``. The shared
       passphrase is a low-entropy operator-chosen string in the common
       case, so a byte-at-a-time comparison is a real oracle.
    """
    if strict is None:
        strict = require_peer_identity()

    if secret:
        peer = None
        if roster is not None:
            peer = roster.verify_peer(secret, node_id=node_id, address=address)
        if peer is not None:
            if roster is not None and node_id:
                roster.clear_shared_secret_peer(node_id)
            return SyncPeerAuth(
                ok=True, mode="per_peer", reason="peer_grant_verified", peer=peer,
            )
        logger.warning(
            "sync.auth_rejected reason=invalid_peer_grant node=%s addr=%s",
            node_id or "-", address or "-",
        )
        return SyncPeerAuth(
            ok=False,
            mode="rejected",
            reason="invalid_peer_grant",
            message=(
                "invalid_peer_grant: this grant is unknown, revoked, lapsed, or "
                "bound to a different node_id. " + _ENROL_HINT
            ),
        )

    if strict:
        logger.warning(
            "sync.auth_rejected reason=peer_identity_required node=%s addr=%s",
            node_id or "-", address or "-",
        )
        return SyncPeerAuth(
            ok=False,
            mode="rejected",
            reason="peer_identity_required",
            message=(
                "peer_identity_required: FERAL_SYNC_REQUIRE_PEER_IDENTITY is set, "
                "so the shared passphrase is refused. " + _ENROL_HINT
            ),
        )

    if not expected_passphrase:
        return SyncPeerAuth(
            ok=False,
            mode="rejected",
            reason="passphrase_unset",
            message=(
                "Local sync passphrase unset, set FERAL_SYNC_PASSPHRASE on this "
                "brain and retry."
            ),
        )

    if not passphrase or not hmac.compare_digest(
        str(passphrase), str(expected_passphrase)
    ):
        return SyncPeerAuth(
            ok=False,
            mode="rejected",
            reason="invalid_passphrase",
            message="Invalid passphrase",
        )

    if roster is not None:
        try:
            roster.record_shared_secret_peer(node_id, address=address)
        except Exception as exc:  # noqa: BLE001, ledger must never block sync
            logger.warning("peer_roster.ledger_write_failed: %s", exc)
    logger.warning(
        "sync.shared_passphrase_accepted node=%s addr=%s, this peer is NOT "
        "identity-authenticated. %s",
        node_id or "-", address or "-", _ENROL_HINT,
    )
    return SyncPeerAuth(
        ok=True, mode="shared", reason="shared_passphrase",
    )


# ─────────────────────────────────────────────────────────────────────
# Outbound grants (what THIS brain presents when it dials a peer)
# ─────────────────────────────────────────────────────────────────────


def _vault():
    if _VAULT_OVERRIDE is not None:
        return _VAULT_OVERRIDE
    from security.vault import get_vault
    return get_vault()


def store_outbound_grant(
    label: str,
    secret: str,
    *,
    address: str = "",
    name: str = "",
) -> dict:
    """Remember the grant a peer issued us, under ``label``.

    ``label`` is what the dialling side matches on. Use the peer's
    ``host:port`` when that is what you know (the common case: an
    operator pastes a grant next to the address they will dial), or the
    peer's ``node_id`` once you know it. :func:`resolve_outbound_grant`
    tries both.

    The secret has to be recoverable to be presented, so it goes to the
    BlindVault (encrypted at rest, same home as the sync passphrase),
    never to a plaintext file. If the vault is unavailable the grant is
    kept for the lifetime of the process and the operator is warned,
    which is the same degradation ``ensure_sync_passphrase`` chose.
    """
    if not (label or "").strip():
        raise ValueError("label is required")
    if not (secret or "").strip():
        raise ValueError("secret is required")
    label = label.strip()
    record = {
        "label": label,
        "secret": secret.strip(),
        "address": address or "",
        "name": name or "",
        "stored_at": time.time(),
    }
    _GRANT_CACHE[label] = record
    try:
        _vault().put(
            VAULT_NAMESPACE, label, json.dumps(record),
            stored_by="sync.peer_accept",
        )
    except Exception as exc:  # noqa: BLE001, vault absence must not block pairing
        logger.warning(
            "peer_roster.grant_vault_write_failed label=%s: %s, the grant is "
            "held for this process only and will be lost on restart.",
            label, exc,
        )
    else:
        logger.info("peer_roster.grant_stored label=%s addr=%s", label, address or "-")
    return {"label": label, "address": record["address"], "name": record["name"]}


def load_outbound_grants(*, refresh: bool = False) -> dict[str, dict]:
    """Every outbound grant, keyed by label. Includes the plaintext
    secret: callers use it to authenticate, so it cannot be redacted
    here. Never log the return value.

    Reads the vault at most once per process unless ``refresh`` is set.
    """
    global _GRANT_CACHE_LOADED
    if _GRANT_CACHE_LOADED and not refresh:
        return dict(_GRANT_CACHE)
    out: dict[str, dict] = dict(_GRANT_CACHE)
    try:
        vault = _vault()
        for label in vault.list_namespace(VAULT_NAMESPACE):
            raw = vault.get(VAULT_NAMESPACE, label, requester="sync")
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning(
                    "peer_roster.grant_unparseable label=%s, ignoring.", label,
                )
                continue
            if isinstance(record, dict) and record.get("secret"):
                out.setdefault(label, record)
                _GRANT_CACHE.setdefault(label, record)
    except Exception as exc:  # noqa: BLE001, vault absence degrades to cache
        logger.debug("peer_roster.grant_vault_read_failed: %s", exc)
    _GRANT_CACHE_LOADED = True
    return out


def forget_outbound_grant(label: str) -> bool:
    """Drop a stored outbound grant. Local only: the peer's copy of the
    hashed half is unaffected, so ask them to revoke as well."""
    label = (label or "").strip()
    if not label:
        return False
    removed = _GRANT_CACHE.pop(label, None) is not None
    try:
        removed = _vault().remove_from(
            VAULT_NAMESPACE, label, removed_by="sync.peer_forget",
        ) or removed
    except Exception as exc:  # noqa: BLE001
        logger.warning("peer_roster.grant_vault_remove_failed label=%s: %s", label, exc)
    return removed


def resolve_outbound_grant(
    *,
    peer_id: str = "",
    address: str = "",
    port: Optional[int] = None,
) -> str:
    """Find the secret to present when dialling this peer, or ``""``.

    Match order is most-specific first: the peer's ``node_id``, then
    ``host:port``, then bare host. ``SyncEngine._load_static_peers`` keys
    peers as ``static-{host}:{port}`` and mDNS keys them by node_id, so
    a lookup that only understood one of those shapes would silently
    miss half the peers.
    """
    grants = load_outbound_grants()
    if not grants:
        return ""
    candidates = []
    if peer_id:
        candidates.append(peer_id)
        if peer_id.startswith("static-"):
            candidates.append(peer_id[len("static-"):])
    if address and port is not None:
        candidates.append(f"{address}:{port}")
    if address:
        candidates.append(address)
    for key in candidates:
        record = grants.get(key)
        if record and record.get("secret"):
            return str(record["secret"])
    # Fall back to a grant that names this address explicitly, for the
    # case where the operator labelled it by peer name.
    for record in grants.values():
        stored = str(record.get("address") or "")
        if not stored:
            continue
        if stored in candidates or (
            address and port is not None and stored == f"{address}:{port}"
        ):
            return str(record.get("secret") or "")
    return ""


# ─────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────

_roster: Optional[PeerRoster] = None


def get_peer_roster(db_path: Optional[str] = None) -> PeerRoster:
    """Module-level singleton (lazy-init), mirroring
    ``device_pairing.get_pairing_store``."""
    global _roster
    if _roster is None:
        _roster = PeerRoster(db_path)
    return _roster


def reset_peer_roster() -> None:
    """Reset the singleton and the grant cache, used by tests."""
    global _roster, _GRANT_CACHE_LOADED
    _roster = None
    _GRANT_CACHE.clear()
    _GRANT_CACHE_LOADED = False
