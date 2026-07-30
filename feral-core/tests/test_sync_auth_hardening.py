"""Batch 2 — federated-sync auth hardening.

Covers:
  * constant-time passphrase comparison (uses hmac.compare_digest), and
  * the per-peer failed-attempt lockout limiter.
"""

from __future__ import annotations

import hmac

from memory import sync as sync_mod
from memory.sync import SyncAuthLimiter, verify_sync_passphrase


class TestConstantTimeCompare:
    def test_matches_correct_passphrase(self):
        assert verify_sync_passphrase("hunter2", "hunter2") is True

    def test_rejects_wrong_passphrase(self):
        assert verify_sync_passphrase("nope", "hunter2") is False

    def test_empty_expected_never_matches(self):
        assert verify_sync_passphrase("", "") is False
        assert verify_sync_passphrase("anything", "") is False

    def test_non_string_remote_is_safe(self):
        assert verify_sync_passphrase(None, "hunter2") is False  # type: ignore[arg-type]

    def test_uses_constant_time_primitive(self, monkeypatch):
        calls = {"n": 0}
        real = hmac.compare_digest

        def _spy(a, b):
            calls["n"] += 1
            return real(a, b)

        monkeypatch.setattr(sync_mod.hmac, "compare_digest", _spy)
        assert verify_sync_passphrase("s3cret", "s3cret") is True
        assert calls["n"] == 1


class TestSyncAuthLimiter:
    def test_allows_until_cap_then_locks_out(self):
        lim = SyncAuthLimiter()
        peer = "10.0.0.5"

        allowed, retry = lim.check(peer)
        assert allowed is True and retry == 0.0

        # One under the cap: still allowed.
        for _ in range(SyncAuthLimiter.MAX_FAILURES - 1):
            lim.record_failure(peer)
        assert lim.check(peer)[0] is True

        # Hitting the cap locks the peer out with a positive cooldown.
        lim.record_failure(peer)
        allowed, retry = lim.check(peer)
        assert allowed is False
        assert retry > 0.0

    def test_success_clears_failures(self):
        lim = SyncAuthLimiter()
        peer = "10.0.0.6"
        for _ in range(SyncAuthLimiter.MAX_FAILURES):
            lim.record_failure(peer)
        assert lim.check(peer)[0] is False

        lim.record_success(peer)
        assert lim.check(peer) == (True, 0.0)

    def test_lockout_is_per_peer(self):
        lim = SyncAuthLimiter()
        for _ in range(SyncAuthLimiter.MAX_FAILURES):
            lim.record_failure("attacker")
        assert lim.check("attacker")[0] is False
        # A different peer is unaffected.
        assert lim.check("friend") == (True, 0.0)

    def test_window_expiry_resets_count(self, monkeypatch):
        lim = SyncAuthLimiter()
        peer = "10.0.0.7"
        base = 1_000.0
        t = {"now": base}
        monkeypatch.setattr(sync_mod.time, "time", lambda: t["now"])

        for _ in range(SyncAuthLimiter.MAX_FAILURES - 1):
            lim.record_failure(peer)
        # Jump past the rolling window: the next failure starts fresh, so
        # the peer is not locked out by a stale near-cap count.
        t["now"] = base + SyncAuthLimiter.WINDOW_SECONDS + 1
        lim.record_failure(peer)
        assert lim.check(peer)[0] is True

    def test_bounded_key_count(self):
        lim = SyncAuthLimiter()
        for i in range(SyncAuthLimiter.MAX_KEYS + 50):
            lim.record_failure(f"peer-{i}")
        assert len(lim._peers) <= SyncAuthLimiter.MAX_KEYS
