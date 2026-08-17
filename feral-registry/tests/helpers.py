"""Bundle-building and publishing helpers shared by the HTTP-level tests.

The distinction these helpers keep straight is the one the registry got
wrong: :func:`skill_manifest` is the document that goes **inside** the
tarball (what FERAL loads at install), while :func:`metadata_envelope`
is the ``manifest_json`` form field (what the catalog serves). They are
different documents, and publishing the second in place of the first is
what made every published skill fail to install.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile

from nacl.signing import SigningKey

REVIEWER_HEADERS = {
    "Authorization": "Bearer test-reviewer-secret",
    "X-Reviewer-Actor": "test",
}


def skill_manifest(skill_id: str, *, version: str = "0.1.0", **overrides) -> dict:
    """A SkillManifest ``feral-core`` can load, for the tarball root."""
    manifest: dict = {
        "skill_id": skill_id,
        "version": version,
        "author": "feral",
        "brand": {"name": skill_id.replace("_", " ").title(), "primary_color": "#000000"},
        "description": f"Test skill {skill_id}.",
        "endpoints": [],
    }
    manifest.update(overrides)
    return manifest


def metadata_envelope(
    name: str,
    *,
    kind: str = "skill",
    version: str = "0.1.0",
    **extra,
) -> dict:
    """The ``manifest_json`` form field posted alongside the bundle."""
    envelope: dict = {
        "kind": kind,
        "name": name,
        "version": version,
        "description": f"Test item {name}.",
        "author": "feral",
    }
    if kind == "skill":
        envelope["skill_id"] = name
    envelope.update(extra)
    return envelope


def build_bundle(bundle_manifest: dict, *, impl: bytes | None = None) -> bytes:
    """Tar+gzip ``manifest.json`` (+ ``impl.py``) the way a publisher would."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add(tar, "manifest.json", json.dumps(bundle_manifest).encode("utf-8"))
        _add(tar, "impl.py", impl if impl is not None else b"def run():\n    return 'hello'\n")
    return buf.getvalue()


def _add(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


async def upsert_publisher(db_mod, models_mod, github_login: str, pubkey_hex: str | None) -> str:
    """Create the publisher, or point an existing one at ``pubkey_hex``."""
    from sqlalchemy import select

    async with db_mod.SessionLocal() as session:
        row = await session.execute(
            select(models_mod.Publisher).where(models_mod.Publisher.github_login == github_login)
        )
        pub = row.scalar_one_or_none()
        if pub is None:
            pub = models_mod.Publisher(
                github_login=github_login, github_id=123456, pubkey_hex=pubkey_hex
            )
            session.add(pub)
        else:
            pub.pubkey_hex = pubkey_hex
        await session.commit()
        await session.refresh(pub)
        return pub.id


def token_for(github_login: str) -> str:
    from feral_registry.auth import issue_publisher_token
    from feral_registry.config import get_settings

    token, _ = issue_publisher_token(github_login, get_settings())
    return token


async def publish_bundle(
    client,
    db_mod,
    models_mod,
    *,
    envelope: dict,
    bundle: bytes,
    login: str = "feral",
):
    """Sign and POST a bundle. Returns the raw httpx response."""
    sk = SigningKey.generate()
    await upsert_publisher(db_mod, models_mod, login, sk.verify_key.encode().hex())

    sha = hashlib.sha256(bundle).hexdigest()
    sig = base64.b64encode(sk.sign(sha.encode("ascii")).signature).decode("ascii")
    return await client.post(
        "/api/v1/publish",
        headers={"Authorization": f"Bearer {token_for(login)}"},
        files={"bundle": ("bundle.tar.gz", bundle, "application/gzip")},
        data={"signature": sig, "manifest_json": json.dumps(envelope)},
    )


async def publish_and_approve(
    client,
    db_mod,
    models_mod,
    *,
    name: str,
    kind: str = "skill",
    version: str = "0.1.0",
    login: str = "feral",
    envelope_extra: dict | None = None,
) -> str:
    """Publish a conformant item, approve it, return its id."""
    envelope = metadata_envelope(name, kind=kind, version=version, **(envelope_extra or {}))
    bundle = build_bundle(skill_manifest(name, version=version))
    resp = await publish_bundle(
        client, db_mod, models_mod, envelope=envelope, bundle=bundle, login=login
    )
    assert resp.status_code == 200, resp.text
    item_id = resp.json()["id"]
    approve = await client.post(
        f"/api/v1/review/{item_id}/approve", json={}, headers=REVIEWER_HEADERS
    )
    assert approve.status_code == 200, approve.text
    return item_id
