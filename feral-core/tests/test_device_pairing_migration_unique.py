"""A10 /  —  pairing migration must handle UNIQUE-constrained token.

 schemas commonly declared ``token TEXT UNIQUE NOT NULL``. SQLite's
``ALTER TABLE paired_devices DROP COLUMN token`` refuses to drop columns
that carry a UNIQUE constraint even on 3.35+, so the old migration fell
back to "leave the column in place (set to empty string)" and left the
legacy column permanently stranded.

 replaces the fallback with the canonical SQLite table-rebuild pattern
(create-new, copy, drop, rename, recreate indexes). These tests pin that
behaviour: after migrating a UNIQUE-legacy DB the final schema has no
`token` column at all, and the regular pair / verify API still works.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pytest

from security.device_pairing import DevicePairingStore


def _seed_legacy_unique_db(path: Path, rows: list[tuple[str, str]]) -> None:
    """ schema with a UNIQUE plaintext ``token`` column.

    Mirrors the failure case that produced the maintainer's terminal
    log entry: UNIQUE + NOT NULL on ``token`` causes the subsequent
    DROP COLUMN to fail with
    ``OperationalError: cannot drop UNIQUE column``.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE paired_devices (
                device_id TEXT PRIMARY KEY,
                name      TEXT NOT NULL,
                token     TEXT NOT NULL UNIQUE,
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


def _column_names(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[1]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    finally:
        conn.close()


def test_unique_token_column_is_removed_via_rebuild(tmp_path: Path) -> None:
    db = tmp_path / "legacy_unique.db"
    _seed_legacy_unique_db(
        db,
        [
            ("legacy-A", "test-key-do-not-commit-A"),
            ("legacy-B", "test-key-do-not-commit-B"),
        ],
    )

    store = DevicePairingStore(db_path=str(db))
    summary = store.migration_summary

    assert sorted(summary["migrated"]) == ["legacy-A", "legacy-B"]
    assert summary["dropped_token_column"] is True, (
        "UNIQUE-token rebuild path must mark the column as dropped"
    )

    cols = _column_names(str(db), "paired_devices")
    assert "token" not in cols, (
        f"table rebuild must remove the legacy plaintext column; cols={cols}"
    )
    for required in ("token_hash", "token_lookup", "hash_algo",
                     "ttl_seconds", "expires_at"):
        assert required in cols, (
            f"rebuild must preserve the W9 columns; missing {required}; "
            f"cols={cols}"
        )


def test_needs_rotation_log_preserved_across_rebuild(tmp_path: Path) -> None:
    db = tmp_path / "legacy_unique_log.db"
    _seed_legacy_unique_db(
        db,
        [
            ("legacy-A", "test-key-do-not-commit-A"),
            ("legacy-B", "test-key-do-not-commit-B"),
        ],
    )

    store = DevicePairingStore(db_path=str(db))
    flagged_ids = {row["device_id"] for row in store.needs_rotation()}
    assert flagged_ids == {"legacy-A", "legacy-B"}
    for row in store.needs_rotation():
        assert row["reason"] == "w9_plaintext_to_hash_migration"


def test_no_plaintext_survives_rebuild(tmp_path: Path) -> None:
    db = tmp_path / "legacy_unique_scrub.db"
    _seed_legacy_unique_db(
        db,
        [("legacy-A", "test-key-do-not-commit")],
    )

    DevicePairingStore(db_path=str(db))

    conn = sqlite3.connect(str(db))
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert "test-key-do-not-commit" not in dump, (
        "rebuild must NOT preserve plaintext anywhere in the DB "
        "(neither in paired_devices nor in needs_rotation_log)"
    )


def test_fresh_pair_after_unique_rebuild_works(tmp_path: Path) -> None:
    db = tmp_path / "legacy_unique_repair.db"
    _seed_legacy_unique_db(
        db,
        [("legacy-A", "test-key-do-not-commit")],
    )

    store = DevicePairingStore(db_path=str(db))

    assert store.verify_device("test-key-do-not-commit") is None

    result = store.pair_device("after-rebuild-device")
    assert store.verify_device(result["token"]) == result["device_id"]


def test_rebuild_logs_unique_rebuild_ok(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "legacy_unique_log_line.db"
    _seed_legacy_unique_db(
        db,
        [("legacy-A", "test-key-do-not-commit")],
    )

    caplog.set_level(logging.INFO, logger="feral.device_pairing")
    DevicePairingStore(db_path=str(db))

    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "device_pairing.migration.unique_rebuild_ok" in m for m in messages
    ), (
        "rebuild success must emit the dedicated INFO breadcrumb; "
        f"got messages={messages}"
    )
    assert any(
        "device_pairing.migration: DROP COLUMN unsupported" in m for m in messages
    ), (
        "the breadcrumb from the unsupported DROP COLUMN must still be "
        "emitted BEFORE the rebuild runs so operators keep the trail. "
        "(In 2026.5.13 the level dropped from WARNING to INFO with a "
        "friendlier 'no action needed' message; the breadcrumb itself "
        "is still required.)"
    )


def test_unique_rebuild_idempotent_on_second_open(tmp_path: Path) -> None:
    db = tmp_path / "legacy_unique_idem.db"
    _seed_legacy_unique_db(
        db,
        [("legacy-A", "test-key-do-not-commit")],
    )
    store_a = DevicePairingStore(db_path=str(db))
    first = store_a.needs_rotation()

    store_b = DevicePairingStore(db_path=str(db))
    second = store_b.needs_rotation()

    assert {r["device_id"] for r in first} == {r["device_id"] for r in second}
    assert len(first) == len(second) == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
