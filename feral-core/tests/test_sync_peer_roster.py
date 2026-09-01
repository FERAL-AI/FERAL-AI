"""Per-peer identity for federated memory sync.

Before ``security/peer_roster.py`` every peer brain authenticated with
ONE shared plaintext passphrase, compared with a plain ``!=`` at
``api/server.py``. Three properties follow from that and every test in
this module pins one of them:

  1. No peer had an identity, so "revoke that one brain" was
     unexpressible. ``PeerRoster`` gives each peer its own argon2id-
     hashed grant, bound to the node_id that redeems it.
  2. The comparison short-circuited on the first differing byte. The
     shared passphrase is an operator-visible string in the common case,
     so that is a real oracle. It is ``hmac.compare_digest`` now.
  3. There was no membership list at all, which is what
     ``MemoryStore.prune_tombstones`` names as the reason it cannot
     prune by acknowledgement. ``active_peer_ids`` answers it.

The migration tests are the load-bearing ones. An install that upgrades
into this code must keep syncing, and its operator must never be told
peers are identity-authenticated while any of them is still presenting
the shared secret.
"""

from __future__ import annotations

import time

import pytest

from security import peer_roster as pr_mod
from security.peer_roster import (
    PeerRoster,
    authenticate_sync_peer,
    identity_mode,
    resolve_outbound_grant,
    store_outbound_grant,
    forget_outbound_grant,
    load_outbound_grants,
)


class _FakeVault:
    """Stands in for the BlindVault so a test never touches the
    operator's real ``~/.feral`` vault or their keychain."""

    def __init__(self):
        self.data: dict[str, dict[str, str]] = {}

    def put(self, namespace, key, value, *, stored_by="user"):
        self.data.setdefault(namespace, {})[key] = value

    def get(self, namespace, key, *, requester="executor"):
        return self.data.get(namespace, {}).get(key)

    def remove_from(self, namespace, key, *, removed_by="user"):
        return self.data.get(namespace, {}).pop(key, None) is not None

    def list_namespace(self, namespace):
        return list(self.data.get(namespace, {}).keys())


@pytest.fixture
def roster(tmp_path):
    return PeerRoster(db_path=str(tmp_path / "peer_roster.db"))


@pytest.fixture(autouse=True)
def _isolated_grants(monkeypatch):
    """Every test gets a private vault and an empty grant cache."""
    vault = _FakeVault()
    monkeypatch.setattr(pr_mod, "_VAULT_OVERRIDE", vault)
    monkeypatch.setattr(pr_mod, "_GRANT_CACHE", {})
    monkeypatch.setattr(pr_mod, "_GRANT_CACHE_LOADED", False)
    monkeypatch.delenv("FERAL_SYNC_REQUIRE_PEER_IDENTITY", raising=False)
    return vault


# ---------------------------------------------------------------------------
# Invite, redeem, bind
# ---------------------------------------------------------------------------


class TestInviteAndVerify:
    def test_secret_is_returned_once_and_never_again(self, roster):
        """The property that makes the store worth having: the plaintext
        is unrecoverable after issue, exactly like a pair token."""
        grant = roster.invite_peer("laptop")

        assert grant["secret"]
        listed = roster.list_peers()
        assert len(listed) == 1
        blob = repr(listed)
        assert grant["secret"] not in blob
        assert "secret" not in listed[0]

    def test_first_use_binds_the_grant_to_a_node_id(self, roster):
        grant = roster.invite_peer("laptop")

        peer = roster.verify_peer(grant["secret"], node_id="brain-b", address="10.0.0.5")

        assert peer is not None
        assert peer["node_id"] == "brain-b"
        assert peer["newly_bound"] is True
        assert roster.list_peers()[0]["status"] == "active"

    def test_a_bound_grant_is_refused_for_a_different_brain(self, roster):
        """A grant names one peer. Replaying it from a second brain is
        the thing per-peer identity exists to stop, and it is checked
        with ``hmac.compare_digest`` rather than ``==``."""
        grant = roster.invite_peer("laptop")
        assert roster.verify_peer(grant["secret"], node_id="brain-b") is not None

        assert roster.verify_peer(grant["secret"], node_id="brain-c") is None

    def test_wrong_secret_is_refused(self, roster):
        roster.invite_peer("laptop")
        assert roster.verify_peer("not-the-grant", node_id="brain-b") is None

    def test_unredeemed_invite_expires_on_a_hard_deadline(self, roster):
        """The invite window does NOT slide. An invite nobody redeems is
        dead rather than a live credential lying around."""
        grant = roster.invite_peer("laptop", invite_ttl_seconds=1)
        time.sleep(1.1)

        assert roster.verify_peer(grant["secret"], node_id="brain-b") is None
        assert roster.list_peers()[0]["status"] == "invite_expired"

    def test_bound_grant_lapses_when_the_peer_stops_syncing(self, roster):
        """Short-expiry grants that lapse are the defence that works.
        Revocation cannot recall replicated data; a window that closes
        on its own at least stops the next exchange without the operator
        having to push anything."""
        grant = roster.invite_peer("laptop", ttl_seconds=1)
        assert roster.verify_peer(grant["secret"], node_id="brain-b") is not None
        time.sleep(1.1)

        assert roster.verify_peer(grant["secret"], node_id="brain-b") is None
        assert roster.list_peers()[0]["status"] == "lapsed"

    def test_each_successful_handshake_renews_the_window(self, roster):
        grant = roster.invite_peer("laptop", ttl_seconds=60)
        first = roster.verify_peer(grant["secret"], node_id="brain-b")
        time.sleep(1.05)
        second = roster.verify_peer(grant["secret"], node_id="brain-b")

        assert second["expires_at"] > first["expires_at"]

    def test_unbound_grant_needs_a_node_id_to_bind(self, roster):
        grant = roster.invite_peer("laptop")
        assert roster.verify_peer(grant["secret"], node_id="") is None

    def test_revoked_grant_stops_working(self, roster):
        grant = roster.invite_peer("laptop")
        row = roster.verify_peer(grant["secret"], node_id="brain-b")

        assert roster.revoke_peer(row["peer_row_id"]) is True
        assert roster.verify_peer(grant["secret"], node_id="brain-b") is None
        assert roster.list_peers()[0]["status"] == "revoked"

    def test_revoke_is_idempotent(self, roster):
        grant = roster.invite_peer("laptop")
        row = roster.verify_peer(grant["secret"], node_id="brain-b")
        assert roster.revoke_peer(row["peer_row_id"]) is True
        assert roster.revoke_peer(row["peer_row_id"]) is False

    def test_two_peers_get_two_independent_grants(self, roster):
        """The whole point. Revoking one leaves the other syncing, which
        the shared passphrase could not express."""
        a = roster.invite_peer("laptop")
        b = roster.invite_peer("studio")
        row_a = roster.verify_peer(a["secret"], node_id="brain-a")
        roster.verify_peer(b["secret"], node_id="brain-b")

        roster.revoke_peer(row_a["peer_row_id"])

        assert roster.verify_peer(a["secret"], node_id="brain-a") is None
        assert roster.verify_peer(b["secret"], node_id="brain-b") is not None


# ---------------------------------------------------------------------------
# Membership and liveness
# ---------------------------------------------------------------------------


class TestMembership:
    def test_active_peer_ids_answers_the_prune_tombstones_question(self, roster):
        """``MemoryStore.prune_tombstones`` states verbatim that pruning
        by acknowledgement needs "a peer roster with liveness, which this
        codebase does not have". This is that roster."""
        g1 = roster.invite_peer("laptop")
        g2 = roster.invite_peer("studio")
        roster.verify_peer(g1["secret"], node_id="brain-a")
        roster.verify_peer(g2["secret"], node_id="brain-b")

        assert roster.active_peer_ids() == ["brain-a", "brain-b"]

    def test_departed_peer_drops_out_of_the_active_set(self, roster):
        g = roster.invite_peer("laptop")
        roster.verify_peer(g["secret"], node_id="brain-a")

        assert roster.mark_departed("brain-a", address="10.0.0.5") is True
        assert roster.active_peer_ids() == []
        assert roster.list_peers()[0]["status"] == "departed"

    def test_departure_is_persisted_not_just_forgotten_in_memory(self, roster, tmp_path):
        """A departure survives a restart. It was previously written
        nowhere at all: ``PeerListener.remove_service`` was ``pass``."""
        g = roster.invite_peer("laptop")
        roster.verify_peer(g["secret"], node_id="brain-a", address="10.0.0.5")
        roster.mark_departed("brain-a")

        reopened = PeerRoster(db_path=str(tmp_path / "peer_roster.db"))
        row = reopened.list_peers()[0]
        assert row["departed_at"] is not None
        assert row["last_seen"] is not None
        assert row["last_address"] == "10.0.0.5"

    def test_a_departed_peer_that_comes_back_is_a_member_again(self, roster):
        g = roster.invite_peer("laptop")
        roster.verify_peer(g["secret"], node_id="brain-a")
        roster.mark_departed("brain-a")

        assert roster.mark_seen("brain-a", address="10.0.0.9") is True
        assert roster.active_peer_ids() == ["brain-a"]

    def test_mark_seen_does_not_extend_a_grant(self, roster):
        """Seeing an mDNS advertisement is evidence a brain exists, not
        evidence it still holds a valid grant. Conflating the two would
        make a lapsed peer immortal as long as it kept advertising."""
        g = roster.invite_peer("laptop", ttl_seconds=1)
        roster.verify_peer(g["secret"], node_id="brain-a")
        time.sleep(1.1)
        roster.mark_seen("brain-a")

        assert roster.verify_peer(g["secret"], node_id="brain-a") is None
        assert roster.active_peer_ids() == []

    def test_lapsed_peer_is_not_active(self, roster):
        g = roster.invite_peer("laptop", ttl_seconds=1)
        roster.verify_peer(g["secret"], node_id="brain-a")
        time.sleep(1.1)
        assert roster.active_peer_ids() == []

    def test_active_peer_ids_can_require_recent_contact(self, roster):
        g = roster.invite_peer("laptop")
        roster.verify_peer(g["secret"], node_id="brain-a")
        assert roster.active_peer_ids(within_seconds=60) == ["brain-a"]
        assert roster.active_peer_ids(within_seconds=0.0) == []

    def test_unredeemed_invites_can_be_pruned(self, roster):
        roster.invite_peer("stale", invite_ttl_seconds=1)
        live = roster.invite_peer("fresh")
        time.sleep(1.1)

        assert roster.prune_unredeemed_invites() == 1
        remaining = roster.list_peers()
        assert len(remaining) == 1
        assert remaining[0]["name"] == "fresh"
        assert roster.verify_peer(live["secret"], node_id="brain-a") is not None


# ---------------------------------------------------------------------------
# Migration off the shared passphrase
# ---------------------------------------------------------------------------


class TestSharedPassphraseMigration:
    def test_existing_install_keeps_syncing_on_the_shared_secret(self, roster):
        """An install that upgrades into this code has an empty roster.
        It must NOT break."""
        auth = authenticate_sync_peer(
            node_id="brain-b", secret="", passphrase="s3cret",
            expected_passphrase="s3cret", roster=roster,
        )
        assert auth.ok is True
        assert auth.mode == "shared"

    def test_shared_secret_use_is_recorded_so_the_operator_can_see_it(self, roster):
        authenticate_sync_peer(
            node_id="brain-b", secret="", passphrase="s3cret",
            expected_passphrase="s3cret", roster=roster, address="10.0.0.5",
        )
        authenticate_sync_peer(
            node_id="brain-b", secret="", passphrase="s3cret",
            expected_passphrase="s3cret", roster=roster, address="10.0.0.5",
        )

        ledger = roster.shared_secret_peers()
        assert [e["node_id"] for e in ledger] == ["brain-b"]
        assert ledger[0]["uses"] == 2

    def test_mode_is_never_reported_as_per_peer_while_shared_is_accepted(self, roster):
        """The trap this is written against: an operator enrols one peer,
        sees "per_peer", and believes the shared secret is gone. It is
        not, so the honest answer is ``mixed``."""
        assert identity_mode(roster, strict=False) == "shared_passphrase"

        g = roster.invite_peer("laptop")
        roster.verify_peer(g["secret"], node_id="brain-b")

        assert identity_mode(roster, strict=False) == "mixed"
        assert identity_mode(roster, strict=True) == "per_peer"

    def test_an_unredeemed_invite_does_not_count_as_migration_progress(self, roster):
        roster.invite_peer("laptop")
        assert roster.has_enrolled_peers() is False
        assert identity_mode(roster, strict=False) == "shared_passphrase"

    def test_strict_mode_refuses_the_shared_secret_and_says_how_to_fix_it(self, roster):
        auth = authenticate_sync_peer(
            node_id="brain-b", secret="", passphrase="s3cret",
            expected_passphrase="s3cret", roster=roster, strict=True,
        )
        assert auth.ok is False
        assert auth.reason == "peer_identity_required"
        assert "feral sync peer invite" in auth.message

    def test_strict_mode_reads_the_env_var(self, roster, monkeypatch):
        monkeypatch.setenv("FERAL_SYNC_REQUIRE_PEER_IDENTITY", "1")
        auth = authenticate_sync_peer(
            node_id="brain-b", secret="", passphrase="s3cret",
            expected_passphrase="s3cret", roster=roster,
        )
        assert auth.ok is False
        assert auth.reason == "peer_identity_required"

    def test_enrolling_a_peer_clears_it_from_the_straggler_list(self, roster):
        authenticate_sync_peer(
            node_id="brain-b", secret="", passphrase="s3cret",
            expected_passphrase="s3cret", roster=roster,
        )
        assert len(roster.shared_secret_peers()) == 1

        g = roster.invite_peer("laptop")
        auth = authenticate_sync_peer(
            node_id="brain-b", secret=g["secret"], passphrase="s3cret",
            expected_passphrase="s3cret", roster=roster,
        )

        assert auth.mode == "per_peer"
        assert roster.shared_secret_peers() == []


# ---------------------------------------------------------------------------
# The handshake decision
# ---------------------------------------------------------------------------


class TestAuthenticateSyncPeer:
    def test_valid_grant_authenticates(self, roster):
        g = roster.invite_peer("laptop")
        auth = authenticate_sync_peer(
            node_id="brain-b", secret=g["secret"], passphrase="",
            expected_passphrase="s3cret", roster=roster,
        )
        assert auth.ok is True
        assert auth.mode == "per_peer"
        assert auth.peer["node_id"] == "brain-b"

    def test_a_bad_grant_never_falls_back_to_the_shared_passphrase(self, roster):
        """The downgrade guard. If a wrong grant fell through to the
        shared secret, an attacker holding only the passphrase could
        strip the per-peer layer at will, and a genuinely lapsed grant
        would look like a working sync instead of an expired one."""
        auth = authenticate_sync_peer(
            node_id="brain-b", secret="wrong-grant", passphrase="s3cret",
            expected_passphrase="s3cret", roster=roster,
        )
        assert auth.ok is False
        assert auth.reason == "invalid_peer_grant"

    def test_wrong_passphrase_is_refused(self, roster):
        auth = authenticate_sync_peer(
            node_id="brain-b", secret="", passphrase="wrong",
            expected_passphrase="s3cret", roster=roster,
        )
        assert auth.ok is False
        assert auth.reason == "invalid_passphrase"

    def test_unset_local_passphrase_is_refused(self, roster):
        """audit-r12 A2's zero-auth guard, preserved through the move
        into the roster module."""
        auth = authenticate_sync_peer(
            node_id="brain-b", secret="", passphrase="",
            expected_passphrase="", roster=roster,
        )
        assert auth.ok is False
        assert auth.reason == "passphrase_unset"

    def test_a_grant_still_works_when_the_passphrase_is_unset(self, roster):
        """Once a peer is enrolled it does not need the shared secret at
        all, which is what makes retiring the passphrase possible."""
        g = roster.invite_peer("laptop")
        auth = authenticate_sync_peer(
            node_id="brain-b", secret=g["secret"], passphrase="",
            expected_passphrase="", roster=roster,
        )
        assert auth.ok is True
        assert auth.mode == "per_peer"

    def test_the_comparison_is_constant_time(self):
        """``api/server.py`` compared the passphrase with ``!=``, which
        short-circuits on the first differing byte. Pin the fix at the
        source: the decision function must use ``hmac.compare_digest``
        and no equality operator on the secret.
        """
        import inspect

        src = inspect.getsource(authenticate_sync_peer)
        assert "hmac.compare_digest" in src
        assert "!= expected_passphrase" not in src
        assert "== expected_passphrase" not in src

    def test_the_endpoint_no_longer_compares_the_passphrase_itself(self):
        """The old branch at ``api/server.py`` was
        ``if remote_pass != expected_pass``. It must be gone, not merely
        supplemented, or the constant-time path would be dead code.
        """
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "api" / "server.py"
        text = src.read_text()
        assert "remote_pass != expected_pass" not in text
        assert "authenticate_sync_peer" in text


# ---------------------------------------------------------------------------
# Outbound grants (what this brain presents when it dials)
# ---------------------------------------------------------------------------


class TestOutboundGrants:
    def test_round_trip_by_label(self):
        store_outbound_grant("10.0.0.5:8888", "the-grant", address="10.0.0.5:8888")
        assert resolve_outbound_grant(address="10.0.0.5", port=8888) == "the-grant"

    def test_resolves_by_node_id(self):
        store_outbound_grant("brain-b", "the-grant")
        assert resolve_outbound_grant(peer_id="brain-b") == "the-grant"

    def test_resolves_a_static_peer_key(self):
        """``_load_static_peers`` keys peers as ``static-{host}:{port}``.
        A lookup that only understood mDNS node ids would silently miss
        every statically configured peer, which is the same shape of bug
        ``exclude_node`` already shipped once."""
        store_outbound_grant("10.0.0.5:8888", "the-grant")
        assert resolve_outbound_grant(peer_id="static-10.0.0.5:8888") == "the-grant"

    def test_unknown_peer_resolves_to_empty_not_an_error(self):
        assert resolve_outbound_grant(peer_id="nobody") == ""

    def test_secret_is_stored_in_the_vault_not_a_plaintext_file(self, _isolated_grants):
        store_outbound_grant("brain-b", "the-grant")
        assert "the-grant" in _isolated_grants.data["sync_peer_grants"]["brain-b"]

    def test_forget_removes_it(self):
        store_outbound_grant("brain-b", "the-grant")
        assert forget_outbound_grant("brain-b") is True
        assert resolve_outbound_grant(peer_id="brain-b") == ""

    def test_grants_survive_a_cold_cache_by_reading_the_vault(self, monkeypatch):
        store_outbound_grant("brain-b", "the-grant")
        monkeypatch.setattr(pr_mod, "_GRANT_CACHE", {})
        monkeypatch.setattr(pr_mod, "_GRANT_CACHE_LOADED", False)

        assert load_outbound_grants()["brain-b"]["secret"] == "the-grant"

    def test_empty_label_or_secret_is_rejected(self):
        with pytest.raises(ValueError):
            store_outbound_grant("", "x")
        with pytest.raises(ValueError):
            store_outbound_grant("label", "")
