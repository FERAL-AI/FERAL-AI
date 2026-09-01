"""The operator-facing half of per-peer sync identity.

A roster nobody can reach is not a feature. These drive the real HTTP
routes with a real ``PeerRoster`` behind them, because the failure this
guards against is the one that shipped for the Skills page: a store that
works, a UI path that never reaches it.

``/api/sync/status`` is included on purpose. It is the endpoint an
operator checks to answer "is my sync healthy", so it is the one place
that must not let them come away believing peers are
identity-authenticated while any is still on the shared passphrase.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from security import peer_roster as pr_mod
from security.peer_roster import PeerRoster


class _FakeVault:
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
def roster(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_mod, "_VAULT_OVERRIDE", _FakeVault())
    monkeypatch.setattr(pr_mod, "_GRANT_CACHE", {})
    monkeypatch.setattr(pr_mod, "_GRANT_CACHE_LOADED", False)
    monkeypatch.delenv("FERAL_SYNC_REQUIRE_PEER_IDENTITY", raising=False)
    return PeerRoster(db_path=str(tmp_path / "peer_roster.db"))


@pytest.fixture
def client(roster):
    """A TestClient whose state carries a REAL roster.

    The rest of BrainState is a mock, but the roster is not: the point
    of these tests is that the route returns a body an operator can act
    on, and a mock roster would make every assertion about that body
    vacuous.
    """
    mock = MagicMock()
    mock.activity_log = deque()
    mock.sync_engine = MagicMock(stats={"running": True, "peer_count": 0})
    mock.sync_scheduler = None
    mock.peer_roster = roster
    with patch("api.state.state", mock), \
         patch("api.routes.identity_nodes_sync.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


class TestRosterRoutes:
    def test_invite_returns_the_grant_exactly_once(self, client, roster):
        r = client.post("/api/sync/roster/invite", json={"name": "laptop"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["secret"]

        listed = client.get("/api/sync/roster").json()
        assert body["secret"] not in str(listed)

    def test_invite_requires_a_name(self, client):
        r = client.post("/api/sync/roster/invite", json={})
        assert r.json() == {"ok": False, "error": "name required"}

    def test_roster_lists_peers_and_the_identity_mode(self, client, roster):
        grant = roster.invite_peer("laptop")
        roster.verify_peer(grant["secret"], node_id="brain-b")

        body = client.get("/api/sync/roster").json()

        assert body["ok"] is True
        assert body["identity_mode"] == "mixed"
        assert [p["node_id"] for p in body["peers"]] == ["brain-b"]

    def test_accept_stores_an_outbound_grant(self, client):
        r = client.post(
            "/api/sync/roster/accept",
            json={"label": "10.0.0.5:8888", "secret": "the-grant"},
        )
        assert r.json()["ok"] is True

        listed = client.get("/api/sync/roster").json()
        assert [g["label"] for g in listed["outbound_grants"]] == ["10.0.0.5:8888"]
        assert "the-grant" not in str(listed), (
            "the roster listing must not echo an outbound secret back"
        )

    def test_accept_requires_both_halves(self, client):
        r = client.post("/api/sync/roster/accept", json={"label": "x"})
        assert r.json()["ok"] is False

    def test_revoke_says_what_it_does_not_achieve(self, client, roster):
        """A route that says "revoked" and nothing else invites the
        reading that the data came back. It cannot."""
        grant = roster.invite_peer("laptop")
        row = roster.verify_peer(grant["secret"], node_id="brain-b")

        body = client.delete(f"/api/sync/roster/{row['peer_row_id']}").json()

        assert body["ok"] is True
        assert "not recall" in body["note"]
        assert roster.verify_peer(grant["secret"], node_id="brain-b") is None


class TestStatusTellsTheTruth:
    def test_a_fresh_install_is_reported_as_shared_passphrase(self, client):
        body = client.get("/api/sync/status").json()
        assert body["identity_mode"] == "shared_passphrase"
        assert body["enrolled_peers"] == 0

    def test_one_enrolled_peer_is_mixed_not_per_peer(self, client, roster):
        grant = roster.invite_peer("laptop")
        roster.verify_peer(grant["secret"], node_id="brain-b")

        body = client.get("/api/sync/status").json()

        assert body["identity_mode"] == "mixed"
        assert body["enrolled_peers"] == 1

    def test_stragglers_are_named_on_the_status_endpoint(self, client, roster):
        roster.record_shared_secret_peer("brain-c", address="10.0.0.9")

        body = client.get("/api/sync/status").json()

        assert [s["node_id"] for s in body["shared_secret_peers"]] == ["brain-c"]
        assert "feral sync peer invite" in body["identity_note"]

    def test_the_mixed_note_names_what_has_to_reach_zero(self, client, roster):
        """An operator in ``mixed`` needs to be told the exact condition
        for retiring the shared secret, not just that they are not done."""
        grant = roster.invite_peer("laptop")
        roster.verify_peer(grant["secret"], node_id="brain-b")
        roster.record_shared_secret_peer("brain-c")

        body = client.get("/api/sync/status").json()

        assert body["identity_mode"] == "mixed"
        assert "shared_secret_peers" in body["identity_note"]
        assert "FERAL_SYNC_REQUIRE_PEER_IDENTITY" in body["identity_note"]

    def test_strict_mode_is_reported(self, client, monkeypatch):
        monkeypatch.setenv("FERAL_SYNC_REQUIRE_PEER_IDENTITY", "1")
        body = client.get("/api/sync/status").json()
        assert body["identity_mode"] == "per_peer"
