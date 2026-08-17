"""Publish inspects the bundle, not only the metadata beside it.

The registry used to validate the ``manifest_json`` form field and never
open the tarball. The tarball is what FERAL installs, so a bundle whose
``manifest.json`` was the registry metadata envelope (no ``brand``, no
``description``) published successfully and then failed at install with
"invalid package: brand field required". That is exactly what shipped:
``scripts/seed_remote.py`` wrote the envelope into the tarball, so the
published ``robot_ext`` bundle could never be installed and the happy
path had never run.

``brand`` stays required. It is what the install dialog shows as the
skill's name and what ``SkillRegistry`` reads; the publish end was the
one that was wrong.
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from feral_registry.bundle import read_bundle_manifest, validate_bundle_for_kind

from .helpers import build_bundle, metadata_envelope, publish_bundle, skill_manifest


def _envelope_shaped_manifest() -> dict:
    """Exactly what seed_remote.py used to write into the tarball."""
    return {
        "kind": "skill",
        "name": "robot_ext",
        "version": "0.1.0",
        "description": "Standardized control capabilities.",
        "author": "feral-core",
        "skill_id": "robot_ext",
        "original": skill_manifest("robot_ext"),
    }


# ---------------------------------------------------------------------------
# The validator, at the unit.
# ---------------------------------------------------------------------------


def test_conformant_skill_bundle_has_no_problems():
    assert validate_bundle_for_kind("skill", build_bundle(skill_manifest("robot_ext"))) == []


def test_missing_brand_is_reported():
    manifest = skill_manifest("robot_ext")
    del manifest["brand"]
    problems = validate_bundle_for_kind("skill", build_bundle(manifest))
    assert any("brand" in p for p in problems)


def test_brand_without_a_name_is_reported():
    manifest = skill_manifest("robot_ext", brand={"primary_color": "#000000"})
    problems = validate_bundle_for_kind("skill", build_bundle(manifest))
    assert any("brand" in p and "name" in p for p in problems)


def test_missing_description_is_reported():
    manifest = skill_manifest("robot_ext")
    del manifest["description"]
    problems = validate_bundle_for_kind("skill", build_bundle(manifest))
    assert any("description" in p for p in problems)


def test_missing_skill_id_is_reported():
    manifest = skill_manifest("robot_ext")
    del manifest["skill_id"]
    problems = validate_bundle_for_kind("skill", build_bundle(manifest))
    assert any("skill_id" in p for p in problems)


def test_registry_envelope_in_the_tarball_is_named_as_such():
    problems = validate_bundle_for_kind("skill", build_bundle(_envelope_shaped_manifest()))
    assert any("envelope" in p for p in problems)
    assert any("brand" in p for p in problems)


def test_bundle_without_a_manifest_is_reported():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo("impl.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    assert validate_bundle_for_kind("skill", buf.getvalue()) == ["bundle contains no manifest.json"]


def test_non_archive_is_reported():
    problems = validate_bundle_for_kind("skill", b"not a tarball at all")
    assert problems and "readable .tar.gz" in problems[0]


def test_unparseable_manifest_is_reported():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"{ this is not json"
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    problems = validate_bundle_for_kind("skill", buf.getvalue())
    assert problems and "not valid JSON" in problems[0]


def test_root_manifest_wins_over_a_nested_one():
    """Mirrors ``sorted(rglob("manifest.json"))[0]`` in the installer.

    Validating a different file than the one install reads would be
    validation of something nobody runs.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, doc in (
            ("manifest.json", skill_manifest("root_skill")),
            ("vendor/manifest.json", {"kind": "skill", "name": "nested"}),
        ):
            data = json.dumps(doc).encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    manifest, error = read_bundle_manifest(buf.getvalue())
    assert error == ""
    assert manifest is not None and manifest["skill_id"] == "root_skill"


@pytest.mark.parametrize("kind", ["daemon", "mcp", "app", "agent", "workflow"])
def test_non_skill_kinds_are_not_inspected_yet(kind):
    """Deliberate: each kind's loader is its own contract."""
    assert validate_bundle_for_kind(kind, b"not a tarball at all") == []


# ---------------------------------------------------------------------------
# Through the HTTP endpoint.
# ---------------------------------------------------------------------------


async def test_publish_refuses_the_bundle_that_shipped(app_client):
    client, db_mod, models_mod = app_client
    resp = await publish_bundle(
        client,
        db_mod,
        models_mod,
        envelope=metadata_envelope("robot_ext"),
        bundle=build_bundle(_envelope_shaped_manifest()),
    )
    assert resp.status_code == 400, resp.text
    assert "would not install" in resp.json()["detail"]
    assert "brand" in resp.json()["detail"]


async def test_publish_accepts_a_loadable_skill_bundle(app_client):
    client, db_mod, models_mod = app_client
    resp = await publish_bundle(
        client,
        db_mod,
        models_mod,
        envelope=metadata_envelope("robot_ext"),
        bundle=build_bundle(skill_manifest("robot_ext")),
    )
    assert resp.status_code == 200, resp.text


async def test_bundle_check_runs_after_the_signature_check(app_client):
    """A bad bundle from an unverified publisher fails on the signature.

    Order matters: the registry must not spend work parsing bytes whose
    publisher has not been established.
    """
    client, db_mod, models_mod = app_client
    from .helpers import token_for, upsert_publisher

    await upsert_publisher(db_mod, models_mod, "feral", "ab" * 32)
    resp = await client.post(
        "/api/v1/publish",
        headers={"Authorization": f"Bearer {token_for('feral')}"},
        files={
            "bundle": ("b.tar.gz", build_bundle(_envelope_shaped_manifest()), "application/gzip"),
        },
        data={
            "signature": "AAAA",
            "manifest_json": json.dumps(metadata_envelope("robot_ext")),
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "signature verification failed"
