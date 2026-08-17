"""End-to-end publish + acceptance gate flow.

This file used to assert that a freshly published item appeared
immediately in the public catalog. After the registry became an
acceptance-gated app store the contract is different: a successful
publish lands a row in ``status=submitted`` / ``visibility=private``,
the public catalog and item endpoints fail closed until a reviewer
approves it, and the blob route refuses 404 to non-reviewers in the
meantime.
"""

from __future__ import annotations

import json

from .conftest import REVIEWER_SECRET
from .helpers import (
    build_bundle,
    metadata_envelope,
    publish_bundle,
    skill_manifest,
    token_for as _token_for,
    upsert_publisher as _make_publisher,
)


async def _publish_item(client, db_mod, models_mod, *, login: str = "feral") -> tuple[str, str]:
    """Publish a fresh skill bundle and return (item_id, sha256)."""
    import hashlib

    skill_id = f"hello_skill_{login}"
    bundle = build_bundle(skill_manifest(skill_id))
    r = await publish_bundle(
        client,
        db_mod,
        models_mod,
        envelope=metadata_envelope(skill_id),
        bundle=bundle,
        login=login,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == hashlib.sha256(bundle).hexdigest()
    return body["id"], body["sha256"]


# ---------------------------------------------------------------------------
# Publish lands in submitted/private (does not become user-installable).
# ---------------------------------------------------------------------------


async def test_publish_lands_in_submitted_private(app_client):
    client, db_mod, models_mod = app_client
    item_id, sha256 = await _publish_item(client, db_mod, models_mod)

    # Publish response advertises pending review state.
    # (re-check via the publisher's own view since publish response is
    # already validated inside _publish_item.)
    token = _token_for("feral")
    r = await client.get(
        "/api/v1/publisher/submissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    only = body["items"][0]
    assert only["id"] == item_id
    assert only["status"] == "submitted"
    assert only["visibility"] == "private"


async def test_public_catalog_hides_unapproved(app_client):
    client, db_mod, models_mod = app_client
    await _publish_item(client, db_mod, models_mod)

    r = await client.get("/api/v1/catalog", params={"kind": "skill"})
    assert r.status_code == 200
    catalog = r.json()
    assert catalog["total"] == 0
    assert catalog["items"] == []


async def test_public_item_detail_404_for_unapproved(app_client):
    client, db_mod, models_mod = app_client
    item_id, _ = await _publish_item(client, db_mod, models_mod)

    r = await client.get(f"/api/v1/item/{item_id}")
    assert r.status_code == 404
    assert r.json()["detail"] == "item not found"


async def test_public_blob_404_for_unapproved(app_client):
    client, db_mod, models_mod = app_client
    _, sha256 = await _publish_item(client, db_mod, models_mod)

    r = await client.get(f"/api/v1/blobs/{sha256}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Reviewer queue + approve/reject lifecycle.
# ---------------------------------------------------------------------------


async def test_reviewer_can_see_queue_and_approve(app_client):
    client, db_mod, models_mod = app_client
    item_id, sha256 = await _publish_item(client, db_mod, models_mod)

    rh = {"Authorization": f"Bearer {REVIEWER_SECRET}", "X-Reviewer-Actor": "alice"}

    r = await client.get("/api/v1/review/queue", headers=rh)
    assert r.status_code == 200, r.text
    queue = r.json()
    assert queue["total"] == 1
    only = queue["items"][0]
    assert only["id"] == item_id
    assert only["status"] == "submitted"
    assert only["visibility"] == "private"
    # publish_received audit row exists from the publish handler.
    events = only["events"]
    assert any(ev["event"] == "publish_received" for ev in events)

    r = await client.post(
        f"/api/v1/review/{item_id}/approve",
        json={"notes": "looks good"},
        headers=rh,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["visibility"] == "public"
    assert body["reviewed_by"] == "reviewer:alice"

    # After approval the public surfaces start working.
    r = await client.get("/api/v1/catalog", params={"kind": "skill"})
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = await client.get(f"/api/v1/item/{item_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    r = await client.get(f"/api/v1/blobs/{sha256}")
    assert r.status_code == 200


async def test_reviewer_reject_keeps_private(app_client):
    client, db_mod, models_mod = app_client
    item_id, sha256 = await _publish_item(client, db_mod, models_mod)

    rh = {"Authorization": f"Bearer {REVIEWER_SECRET}", "X-Reviewer-Actor": "bob"}
    r = await client.post(
        f"/api/v1/review/{item_id}/reject",
        json={"notes": "missing readme"},
        headers=rh,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["visibility"] == "private"

    r = await client.get(f"/api/v1/item/{item_id}")
    assert r.status_code == 404
    r = await client.get(f"/api/v1/blobs/{sha256}")
    assert r.status_code == 404


async def test_reviewer_quarantine_path(app_client):
    client, db_mod, models_mod = app_client
    item_id, _ = await _publish_item(client, db_mod, models_mod)

    rh = {"Authorization": f"Bearer {REVIEWER_SECRET}"}
    r = await client.post(f"/api/v1/review/{item_id}/quarantine", json={}, headers=rh)
    assert r.status_code == 200
    assert r.json()["status"] == "quarantined"
    assert r.json()["visibility"] == "private"


# ---------------------------------------------------------------------------
# Reviewer auth fail-closed.
# ---------------------------------------------------------------------------


async def test_review_queue_requires_auth(app_client):
    client, *_ = app_client
    r = await client.get("/api/v1/review/queue")
    assert r.status_code == 401


async def test_review_queue_rejects_wrong_secret(app_client):
    client, *_ = app_client
    r = await client.get(
        "/api/v1/review/queue",
        headers={"Authorization": "Bearer not-the-real-secret"},
    )
    assert r.status_code == 403


async def test_publisher_jwt_cannot_act_as_reviewer(app_client):
    client, db_mod, models_mod = app_client
    await _make_publisher(db_mod, models_mod, "feral", "ab" * 32)
    publisher_token = _token_for("feral")
    r = await client.get(
        "/api/v1/review/queue",
        headers={"Authorization": f"Bearer {publisher_token}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Pre-existing test contracts that still hold.
# ---------------------------------------------------------------------------


async def test_publish_requires_pubkey(app_client):
    client, db_mod, models_mod = app_client
    await _make_publisher(db_mod, models_mod, "nokey", None)

    token = _token_for("nokey")
    manifest = metadata_envelope("x", version="0.0.1")
    bundle = build_bundle(skill_manifest("x", version="0.0.1"))

    r = await client.post(
        "/api/v1/publish",
        headers={"Authorization": f"Bearer {token}"},
        files={"bundle": ("x.tar.gz", bundle, "application/gzip")},
        data={"signature": "AAAA", "manifest_json": json.dumps(manifest)},
    )
    assert r.status_code == 412
    assert "register pubkey" in r.json()["detail"]


async def test_health(app_client):
    client, _, _ = app_client
    r = await client.get("/api/v1/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
