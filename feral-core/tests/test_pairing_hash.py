""" — pairing-token hashing + TTL tests.

Covers:
  * register a pairing → row stored with token_hash, NEVER plaintext;
    plaintext is not recoverable from the row
  * verify_token(plaintext) within TTL → True (returns the device_id)
  * verify_token after TTL elapses → False (with a clear log line)
  * verify_token with the wrong plaintext → False
  * successful verify slides the TTL window
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from security.device_pairing import (
    DEFAULT_TTL_SECONDS,
    DevicePairingStore,
    _token_lookup,
)


@pytest.fixture
def store(tmp_path: Path) -> DevicePairingStore:
    return DevicePairingStore(db_path=str(tmp_path / "pairing.db"))


# ─────────────────────────────────────────────────────────────────────
# Hash-only storage
# ─────────────────────────────────────────────────────────────────────


class TestHashAtRest:
    def test_plaintext_never_persisted(self, store: DevicePairingStore, tmp_path: Path):
        result = store.pair_device("phone-1")
        token = result["token"]

        # Open the SQLite file directly and prove the plaintext is
        # nowhere on disk.
        conn = sqlite3.connect(str(tmp_path / "pairing.db"))
        try:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(paired_devices)"
                ).fetchall()
            }
            # Either the legacy `token` column is gone, or it's still
            # present (older SQLite) but always NULL for fresh rows.
            if "token" in cols:
                rows = conn.execute(
                    "SELECT token FROM paired_devices"
                ).fetchall()
                assert all(r[0] is None or r[0] == "" for r in rows), (
                    "fresh pairings must NEVER write plaintext into the "
                    "legacy `token` column"
                )

            # Hash + lookup are populated.
            row = conn.execute(
                "SELECT token_hash, token_lookup, hash_algo, ttl_seconds, "
                "       expires_at FROM paired_devices WHERE device_id = ?",
                (result["device_id"],),
            ).fetchone()
        finally:
            conn.close()

        token_hash, token_lookup, hash_algo, ttl_seconds, expires_at = row
        assert token_hash, "token_hash must be set"
        # Hash storage must NOT be the plaintext.
        assert token not in token_hash
        # The lookup is a deterministic SHA-256 of the plaintext.
        assert token_lookup == _token_lookup(token)
        assert hash_algo in {"argon2id", "bcrypt"}
        assert ttl_seconds == DEFAULT_TTL_SECONDS
        # expires_at lives in the future.
        assert expires_at > int(time.time())

    def test_pair_device_returns_plaintext_exactly_once(
        self, store: DevicePairingStore
    ):
        result = store.pair_device("phone-2")
        assert "token" in result
        assert len(result["token"]) == 64  # 32 bytes hex

        # list_devices NEVER includes the plaintext token.
        rows = store.list_devices()
        assert len(rows) == 1
        assert "token" not in rows[0], (
            "list_devices must not expose the plaintext token; the "
            "issuer-side caller is the ONLY place the plaintext exists"
        )
        assert rows[0]["token_lookup"] == _token_lookup(result["token"])

    def test_unique_tokens_for_distinct_pairings(self, store: DevicePairingStore):
        a = store.pair_device("A")
        b = store.pair_device("B")
        assert a["token"] != b["token"]
        assert a["device_id"] != b["device_id"]


# ─────────────────────────────────────────────────────────────────────
# verify_device — happy path + TTL + wrong-token
# ─────────────────────────────────────────────────────────────────────


class TestVerify:
    def test_within_ttl_returns_device_id(self, store: DevicePairingStore):
        result = store.pair_device("dev-1")
        assert store.verify_device(result["token"]) == result["device_id"]

    def test_unknown_token_returns_none(self, store: DevicePairingStore):
        assert store.verify_device("not-a-real-token") is None
        assert store.verify_device("") is None

    def test_wrong_plaintext_returns_none(self, store: DevicePairingStore):
        # Two independent pairings: each token is unique. Verifying one
        # token must NEVER match the other row.
        a = store.pair_device("A")
        b = store.pair_device("B")
        assert store.verify_device(a["token"]) == a["device_id"]
        assert store.verify_device(b["token"]) == b["device_id"]
        # Synthetic 64-hex-char "token" must not collide.
        forged = "f" * 64
        assert store.verify_device(forged) is None

    def test_expired_token_returns_none_with_log(
        self, store: DevicePairingStore, caplog
    ):
        import logging

        # Pair with a 1-second TTL → wait → verify must reject.
        result = store.pair_device("dev-short", ttl_seconds=1)
        # The first verify slides the window by ttl_seconds, so we
        # need to wait > 1s after the LAST successful verify to expire.
        time.sleep(1.5)

        with caplog.at_level(logging.INFO, logger="feral.device_pairing"):
            verdict = store.verify_device(result["token"])
        assert verdict is None
        assert any(
            "device_pairing.token_expired" in r.getMessage()
            for r in caplog.records
        ), "expired tokens must surface a clear log line"

    def test_freezegun_simulates_ttl_passage(self, store: DevicePairingStore):
        from freezegun import freeze_time

        # 60-second TTL → freeze, advance past expiry, verify rejects.
        with freeze_time("2026-04-25 12:00:00") as frozen:
            result = store.pair_device("dev-frozen", ttl_seconds=60)
            assert store.verify_device(result["token"]) == result["device_id"]
            frozen.tick(delta=120)
            assert store.verify_device(result["token"]) is None

    def test_successful_verify_slides_window(self, store: DevicePairingStore):
        from freezegun import freeze_time

        with freeze_time("2026-04-25 12:00:00") as frozen:
            result = store.pair_device("dev-slider", ttl_seconds=60)
            # Advance ALMOST to expiry, then verify (slides window).
            frozen.tick(delta=50)
            assert store.verify_device(result["token"]) == result["device_id"]
            # Advance another 50s — would have expired without the slide.
            frozen.tick(delta=50)
            assert store.verify_device(result["token"]) == result["device_id"]


# ─────────────────────────────────────────────────────────────────────
# Misc API contracts (mark_claimed, list_devices)
# ─────────────────────────────────────────────────────────────────────


class TestApiContracts:
    def test_mark_claimed_uses_hash_path(self, store: DevicePairingStore):
        result = store.pair_device("dev-claim")
        device_id = store.mark_claimed(result["token"])
        assert device_id == result["device_id"]

        rows = store.list_devices()
        assert rows[0]["claimed_at"] is not None

    def test_mark_claimed_rejects_unknown(self, store: DevicePairingStore):
        assert store.mark_claimed("nope") is None
        assert store.mark_claimed("") is None

    def test_revoke_device_removes_row(self, store: DevicePairingStore):
        a = store.pair_device("A")
        assert store.revoke_device(a["device_id"]) is True
        assert store.verify_device(a["token"]) is None

    def test_zero_ttl_is_rejected(self, store: DevicePairingStore):
        with pytest.raises(ValueError):
            store.pair_device("X", ttl_seconds=0)
        with pytest.raises(ValueError):
            store.pair_device("X", ttl_seconds=-1)
