"""``GET /api/v1/item/{ref}`` resolves a name as well as a UUID.

Before this landed the endpoint matched only ``Item.id``, the UUID
primary key. Every caller passes a name: an ``AppManifest`` declares
``skill_dependencies: ["robot_ext"]``, a user runs ``feral install
robot_ext``, and the catalog lists ``robot_ext``. So
``/api/v1/item/robot_ext`` was a 404 on an item plainly visible in the
catalog, and every name-declared skill dependency resolved as "not
published in the registry".

``UniqueConstraint("kind", "name", "version")`` makes a name a natural
key, so these tests pin what a name resolves to, including the cases
where it resolves to nothing rather than to an arbitrary pick.
"""

from __future__ import annotations

import pytest

from .helpers import REVIEWER_HEADERS, publish_and_approve


async def test_uuid_still_resolves(app_client):
    """The old contract does not move. Ids are permalinks."""
    client, db_mod, models_mod = app_client
    item_id = await publish_and_approve(client, db_mod, models_mod, name="robot_ext")

    r = await client.get(f"/api/v1/item/{item_id}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == item_id
    assert r.json()["name"] == "robot_ext"


async def test_name_resolves(app_client):
    """The case that was broken: the string a manifest actually writes."""
    client, db_mod, models_mod = app_client
    item_id = await publish_and_approve(client, db_mod, models_mod, name="robot_ext")

    r = await client.get("/api/v1/item/robot_ext")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == item_id
    assert body["name"] == "robot_ext"
    assert body["download_url"].endswith(body["sha256"])


async def test_unknown_name_is_404(app_client):
    client, db_mod, models_mod = app_client
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext")

    r = await client.get("/api/v1/item/no_such_skill")
    assert r.status_code == 404
    assert r.json()["detail"] == "item not found"


async def test_name_of_unapproved_item_is_404_not_a_leak(app_client):
    """Resolution runs inside the visibility filter, not around it.

    A name must not confirm the existence of a pending submission any
    more than a UUID does.
    """
    client, db_mod, models_mod = app_client
    from .helpers import build_bundle, metadata_envelope, publish_bundle, skill_manifest

    resp = await publish_bundle(
        client,
        db_mod,
        models_mod,
        envelope=metadata_envelope("pending_skill"),
        bundle=build_bundle(skill_manifest("pending_skill")),
    )
    assert resp.status_code == 200, resp.text

    r = await client.get("/api/v1/item/pending_skill")
    assert r.status_code == 404

    # The reviewer, who may see everything, resolves the same name.
    r = await client.get("/api/v1/item/pending_skill", headers=REVIEWER_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"


# ---------------------------------------------------------------------------
# Several versions of one kind: latest wins, because that is what
# `feral install robot_ext` means.
# ---------------------------------------------------------------------------


async def test_bare_name_resolves_to_the_highest_version(app_client):
    client, db_mod, models_mod = app_client
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext", version="0.9.0")
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext", version="0.10.0")
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext", version="0.2.0")

    r = await client.get("/api/v1/item/robot_ext")
    assert r.status_code == 200, r.text
    # 0.10.0, not 0.9.0: ordered numerically, not as strings.
    assert r.json()["version"] == "0.10.0"


async def test_version_query_pins_an_older_release(app_client):
    client, db_mod, models_mod = app_client
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext", version="0.9.0")
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext", version="1.0.0")

    r = await client.get("/api/v1/item/robot_ext", params={"version": "0.9.0"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "0.9.0"


async def test_unorderable_version_set_is_an_explicit_error(app_client):
    """No arbitrary pick when the versions cannot be ordered."""
    client, db_mod, models_mod = app_client
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext", version="1.0.0")
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext", version="1.0.0-beta")

    r = await client.get("/api/v1/item/robot_ext")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "1.0.0" in detail and "1.0.0-beta" in detail
    assert "version=" in detail

    # And the escape hatch the message names actually works.
    r = await client.get("/api/v1/item/robot_ext", params={"version": "1.0.0-beta"})
    assert r.status_code == 200
    assert r.json()["version"] == "1.0.0-beta"


# ---------------------------------------------------------------------------
# Several kinds under one name: a hard error, because no rule chooses.
# ---------------------------------------------------------------------------


async def test_name_across_two_kinds_is_409_not_a_coin_flip(app_client):
    client, db_mod, models_mod = app_client
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext")
    await publish_and_approve(
        client,
        db_mod,
        models_mod,
        name="robot_ext",
        kind="daemon",
        envelope_extra={"node_id": "robot_ext", "capabilities": ["move"]},
    )

    r = await client.get("/api/v1/item/robot_ext")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "daemon" in detail and "skill" in detail
    assert "kind=" in detail


async def test_kind_query_disambiguates(app_client):
    client, db_mod, models_mod = app_client
    skill_id = await publish_and_approve(client, db_mod, models_mod, name="robot_ext")
    daemon_id = await publish_and_approve(
        client,
        db_mod,
        models_mod,
        name="robot_ext",
        kind="daemon",
        envelope_extra={"node_id": "robot_ext", "capabilities": ["move"]},
    )

    r = await client.get("/api/v1/item/robot_ext", params={"kind": "skill"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == skill_id

    r = await client.get("/api/v1/item/robot_ext", params={"kind": "daemon"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == daemon_id


async def test_unknown_kind_is_rejected_by_the_schema(app_client):
    client, db_mod, models_mod = app_client
    await publish_and_approve(client, db_mod, models_mod, name="robot_ext")

    r = await client.get("/api/v1/item/robot_ext", params={"kind": "banana"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Version ordering, at the unit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [
        ("1", (1,)),
        ("1.2", (1, 2)),
        ("0.10.0", (0, 10, 0)),
        ("2026.4.18", (2026, 4, 18)),
        ("1.0.0-beta", None),
        ("v1.0.0", None),
        ("", None),
        ("latest", None),
    ],
)
def test_version_sort_key(version, expected):
    from feral_registry.resolve import version_sort_key

    assert version_sort_key(version) == expected


def test_numeric_ordering_beats_string_ordering():
    from feral_registry.resolve import version_sort_key

    assert version_sort_key("0.10.0") > version_sort_key("0.9.0")
    assert "0.10.0" < "0.9.0"  # the bug this avoids
