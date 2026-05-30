""" — pairing-token schema migration tests.

Covers the legacy → hashed migration:

  * a SQLite DB seeded with the old schema (with a plaintext `token`
    column) is migrated on first open
  * the migration adds the new columns (token_hash, token_lookup,
    hash_algo, ttl_seconds, expires_at)
  * every legacy row is logged into the ``needs_rotation_log`` sibling
    table — the brain reads this table on next daemon connection to
    flag re-pair
  * the legacy ``token`` column is NULLed (or removed if SQLite
    supports DROP COLUMN) so plaintext does not stay on disk
  * verify_device() refuses tokens for needs-rotation rows — the user
    MUST re-pair to land a hash
  * the migration is idempotent across reopens
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from security.device_pairing import DevicePairingStore


def _seed_legacy_db(path: Path, rows: list[tuple[str, str]]) -> None:
    """Write a SQLite file in the *old* schema (no hash, plaintext token).

    Mirrors the  schema as tightly as possible: a ``token`` TEXT
    NOT NULL column, no ``token_hash``, no ``ttl_seconds``,
    no ``expires_at``.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE paired_devices (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token TEXT NOT NULL,
                paired_at REAL NOT NULL,
                last_seen REAL,
                claimed_at REAL
            )
            """
        )
        now = time.time()
        for device_id, token in rows:
            conn.execute(
                "INSERT INTO paired_devices(device_id, name, token, paired_at) "
                "VALUES (?, ?, ?, ?)",
                (device_id, f"name-of-{device_id}", token, now),
            )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# Schema migration
# ─────────────────────────────────────────────────────────────────────


class TestSchemaMigration:
    def test_new_columns_present_after_migration(self, tmp_path: Path):
        db = tmp_path / "pairing.db"
        _seed_legacy_db(db, [("legacy-A", "test-key-do-not-commit")])

        DevicePairingStore(db_path=str(db))  # opens & migrates.

        conn = sqlite3.connect(str(db))
        try:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(paired_devices)"
                ).fetchall()
            }
        finally:
            conn.close()

        for required in {
            "token_hash",
            "token_lookup",
            "hash_algo",
            "ttl_seconds",
            "expires_at",
        }:
            assert required in cols, (
                f"migration must add column {required}; got {cols}"
            )

    def test_needs_rotation_log_populated(self, tmp_path: Path):
        db = tmp_path / "pairing.db"
        _seed_legacy_db(
            db,
            [
                ("legacy-A", "test-key-do-not-commit-A"),
                ("legacy-B", "test-key-do-not-commit-B"),
            ],
        )

        store = DevicePairingStore(db_path=str(db))
        flagged = store.needs_rotation()
        flagged_ids = {row["device_id"] for row in flagged}
        assert flagged_ids == {"legacy-A", "legacy-B"}

        # Each entry carries the WHY (so the brain can show a
        # consistent banner) and a wall-clock timestamp.
        for row in flagged:
            assert row["reason"] == "w9_plaintext_to_hash_migration"
            assert isinstance(row["logged_at"], float)
            assert row["logged_at"] > 0
            # `announced` starts at 0 so the brain prints exactly one
            # banner per device on the next daemon connection.
            assert row["announced"] == 0

    def test_migration_summary_returns_migrated_ids(self, tmp_path: Path):
        db = tmp_path / "pairing.db"
        _seed_legacy_db(
            db,
            [
                ("legacy-A", "test-key-do-not-commit-A"),
                ("legacy-B", "test-key-do-not-commit-B"),
            ],
        )

        store = DevicePairingStore(db_path=str(db))
        summary = store.migration_summary
        assert sorted(summary["migrated"]) == ["legacy-A", "legacy-B"]

    def test_legacy_plaintext_is_scrubbed(self, tmp_path: Path):
        db = tmp_path / "pairing.db"
        _seed_legacy_db(db, [("legacy-A", "test-key-do-not-commit")])

        DevicePairingStore(db_path=str(db))

        conn = sqlite3.connect(str(db))
        try:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(paired_devices)"
                ).fetchall()
            }
            if "token" in cols:
                rows = conn.execute(
                    "SELECT token FROM paired_devices WHERE device_id = ?",
                    ("legacy-A",),
                ).fetchall()
                assert all(r[0] is None or r[0] == "" for r in rows), (
                    "legacy plaintext token must be NULLed after migration"
                )

            log_rows = conn.execute(
                "SELECT * FROM needs_rotation_log"
            ).fetchall()
            assert log_rows
            log_blob = " ".join(str(c) for r in log_rows for c in r)
            assert "test-key-do-not-commit" not in log_blob, (
                "needs_rotation_log must NEVER preserve the plaintext "
                "token — the whole point is to force re-pair"
            )
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────
# verify_device on a migrated row
# ─────────────────────────────────────────────────────────────────────


class TestVerifyAfterMigration:
    def test_legacy_token_is_rejected(self, tmp_path: Path):
        db = tmp_path / "pairing.db"
        _seed_legacy_db(db, [("legacy-A", "test-key-do-not-commit")])

        store = DevicePairingStore(db_path=str(db))
        # The legacy plaintext is NOT a valid pairing token any more.
        # The user must re-pair (and the brain has the needs_rotation_log
        # to show them why).
        assert store.verify_device("test-key-do-not-commit") is None
        # mark_claimed is similarly blocked.
        assert store.mark_claimed("test-key-do-not-commit") is None

    def test_acknowledge_rotation_marks_announced(self, tmp_path: Path):
        db = tmp_path / "pairing.db"
        _seed_legacy_db(db, [("legacy-A", "test-key-do-not-commit")])
        store = DevicePairingStore(db_path=str(db))

        assert store.acknowledge_rotation("legacy-A") is True
        flagged = store.needs_rotation()
        # Still in the log table (so audit trail survives), but marked
        # as announced so the brain doesn't re-spam the banner.
        assert flagged and flagged[0]["device_id"] == "legacy-A"
        assert flagged[0]["announced"] == 1

    def test_fresh_pair_after_migration_works(self, tmp_path: Path):
        db = tmp_path / "pairing.db"
        _seed_legacy_db(db, [("legacy-A", "test-key-do-not-commit")])
        store = DevicePairingStore(db_path=str(db))

        # The user re-pairs; the new device has its own device_id and
        # the new pairing verifies normally.
        result = store.pair_device("a fresh device after migration")
        assert store.verify_device(result["token"]) == result["device_id"]
        # The legacy row stays flagged so the audit trail is intact.
        assert {r["device_id"] for r in store.needs_rotation()} == {
            "legacy-A"
        }


# ─────────────────────────────────────────────────────────────────────
# Idempotency — repeated opens don't re-flag rows
# ─────────────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_second_open_does_not_double_flag(self, tmp_path: Path):
        db = tmp_path / "pairing.db"
        _seed_legacy_db(db, [("legacy-A", "test-key-do-not-commit")])

        store_a = DevicePairingStore(db_path=str(db))
        first = store_a.needs_rotation()

        store_b = DevicePairingStore(db_path=str(db))
        second = store_b.needs_rotation()

        assert {r["device_id"] for r in first} == {
            r["device_id"] for r in second
        }, (
            "migration must be idempotent — opening the DB twice must "
            "NOT produce duplicate needs_rotation entries"
        )
        assert len(first) == len(second) == 1

    def test_fresh_db_has_no_flagged_rows(self, tmp_path: Path):
        store = DevicePairingStore(db_path=str(tmp_path / "fresh.db"))
        assert store.needs_rotation() == []
        assert store.migration_summary["migrated"] == []
