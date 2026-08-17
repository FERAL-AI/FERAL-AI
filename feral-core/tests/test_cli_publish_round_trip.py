"""``feral publish --skill`` from a clean directory, all the way through.

Runs the real ``cmd_publish``, captures the multipart request it builds,
and replays it into the real ``feral_registry`` FastAPI app. Nothing
between the two halves is stubbed: the tarball is really built from a
directory on disk, the signature is really produced by
``load_or_create_signing_key``, and the registry really verifies it with
``feral_registry/signing.py``.

This is the loop that could not complete. Publish was refused first on
``400 invalid manifest`` (no ``kind``, no ``name``) and then, once that
was fixed, on ``400 signature verification failed`` (the signature
covered the raw digest rather than the hex digest as ASCII). The item
therefore never existed, so nothing downstream of it -- resolution by
name, the consent preview, install -- was reachable by anyone outside
this repo.

Skipped when ``feral_registry`` is not importable: it is a separate
deployable and feral-core's CI installs only ``feral-core[all,dev]``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("feral_registry", reason="feral-registry is not installed here")

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

REGISTRY_BASE = "http://registry.test"
REVIEWER_SECRET = "round-trip-reviewer"

SKILL_MANIFEST = {
    "skill_id": "iot_light",
    "version": "2.3.0",
    "author": "acme",
    "brand": {"name": "Smart Bulb", "primary_color": "#f1c40f"},
    "description": "Standardized control for the connected smart bulb.",
    "permissions": ["smart_home"],
    "endpoints": [
        {
            "id": "set_color",
            "method": "WS_EXECUTE",
            "url": "local_daemon",
            "description": "Sets the RGB color of the light bulb.",
        },
    ],
}

IMPL = b"class Skill:\n    def execute(self, endpoint_id, args, vault):\n        return {}\n"


@pytest.fixture
def skill_dir(tmp_path) -> Path:
    """A clean skill directory, exactly what a developer would have."""
    src = tmp_path / "my-skill"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps(SKILL_MANIFEST, indent=2))
    (src / "impl.py").write_bytes(IMPL)
    (src / "README.md").write_text("# Smart Bulb\n")
    return src


@pytest.fixture
def publisher_token(tmp_path, monkeypatch) -> str:
    """``feral publisher login`` would have written this."""
    from cli.publish import _feral_home

    home = _feral_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "publisher.token").write_text("placeholder-replaced-below")
    return str(home / "publisher.token")


@pytest.fixture
async def registry(tmp_path, monkeypatch):
    """A live in-process registry with a publisher whose key we control."""
    monkeypatch.setenv("FERAL_REGISTRY_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.setenv(
        "FERAL_REGISTRY_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}"
    )
    monkeypatch.setenv("JWT_SECRET", "round-trip-secret")
    monkeypatch.setenv("FEATURED_PUBLISHERS", "acme")
    monkeypatch.setenv("FERAL_REGISTRY_PUBLIC_URL", REGISTRY_BASE)
    monkeypatch.setenv("FERAL_REGISTRY_REVIEWER_SECRET", REVIEWER_SECRET)

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from feral_registry import config as config_mod

    config_mod.get_settings.cache_clear()  # type: ignore[attr-defined]
    settings = config_mod.get_settings()

    from feral_registry import db as db_mod
    from feral_registry import main as main_mod
    from feral_registry import models as models_mod

    engine = create_async_engine(settings.db_url, echo=False, future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=db_mod.AsyncSession
    )
    monkeypatch.setattr(db_mod, "engine", engine, raising=False)
    monkeypatch.setattr(db_mod, "SessionLocal", session_factory, raising=False)

    app = main_mod.create_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url=REGISTRY_BASE
    ) as http:
        async with engine.begin() as conn:
            await conn.run_sync(db_mod.Base.metadata.create_all)
        yield http, session_factory, models_mod, settings
    await engine.dispose()


class _CapturedPost:
    """Stands in for ``httpx.post`` and keeps what was sent.

    ``cmd_publish`` passes an open file handle; the bytes are read here,
    inside the ``with`` block, so the replay has real content.
    """

    HTTPError = httpx.HTTPError

    def __init__(self):
        self.url = ""
        self.data: dict = {}
        self.bundle: bytes = b""
        self.headers: dict = {}
        self.response = httpx.Response(
            200,
            json={
                "id": "00000000-0000-0000-0000-000000000000",
                "sha256": "",
                "download_url": "",
                "verified": False,
                "status": "submitted",
                "visibility": "private",
                "message": "submission received, pending review",
            },
        )

    def post(self, url, files=None, data=None, headers=None, timeout=None):
        self.url = url
        self.data = dict(data or {})
        self.headers = dict(headers or {})
        name, handle, _mime = (files or {})["bundle"]
        self.bundle = handle.read()
        return self.response


@pytest.fixture
def captured_publish(monkeypatch, skill_dir, publisher_token):
    """Run the real ``cmd_publish`` and hand back what it tried to send."""
    import cli.publish as pub

    captured = _CapturedPost()
    monkeypatch.setattr(pub, "httpx", captured)
    monkeypatch.setattr(pub, "registry_base_url", lambda override=None: REGISTRY_BASE)

    pub.cmd_publish(skill_dir=str(skill_dir))
    assert captured.bundle, "cmd_publish sent no bundle"
    return captured


# ─────────────────────────────────────────────
# What cmd_publish puts on the wire
# ─────────────────────────────────────────────


def test_publish_posts_registry_metadata_not_the_domain_manifest(captured_publish):
    envelope = json.loads(captured_publish.data["manifest_json"])

    assert envelope["kind"] == "skill"
    assert envelope["name"] == "iot_light"
    assert envelope["version"] == "2.3.0"
    assert envelope["skill_id"] == "iot_light"
    # The SkillManifest is carried, not impersonated.
    assert envelope["original"]["brand"]["name"] == "Smart Bulb"
    assert "brand" not in envelope


def test_publish_signs_the_hex_digest_as_ascii(captured_publish):
    """The bytes the registry and the installer both verify."""
    from feral_registry.signing import sha256_bytes, verify_bundle_signature

    from cli.publish import load_or_create_signing_key

    pubkey_hex = load_or_create_signing_key(verbose=False).verify_key.encode().hex()
    sha_hex = sha256_bytes(captured_publish.bundle)

    assert verify_bundle_signature(
        pubkey_hex, captured_publish.data["signature"], sha_hex
    )
    assert captured_publish.data["sha256"] == sha_hex


def test_the_bundle_carries_the_skill_manifest_at_its_root(captured_publish, tmp_path):
    """What was tarred is what SkillPackage can load."""
    import tarfile
    from io import BytesIO

    dest = tmp_path / "unpacked"
    with tarfile.open(fileobj=BytesIO(captured_publish.bundle), mode="r:gz") as tar:
        tar.extractall(dest)

    from skills.package import SkillPackage

    pkg = SkillPackage(dest)
    assert pkg.load(), pkg.errors
    assert pkg.manifest is not None
    assert pkg.manifest.skill_id == "iot_light"
    assert pkg.manifest.brand.name == "Smart Bulb"
    assert (dest / "impl.py").is_file()


# ─────────────────────────────────────────────
# The same request, into the real registry
# ─────────────────────────────────────────────


async def _replay(registry, captured, *, login: str = "acme"):
    """Register the publisher's key, then POST the captured request."""
    http, session_factory, models_mod, settings = registry

    from cli.publish import load_or_create_signing_key

    pubkey_hex = load_or_create_signing_key(verbose=False).verify_key.encode().hex()
    async with session_factory() as session:
        session.add(
            models_mod.Publisher(github_login=login, github_id=7, pubkey_hex=pubkey_hex)
        )
        await session.commit()

    from feral_registry.auth import issue_publisher_token

    token, _ = issue_publisher_token(login, settings)
    return await http.post(
        "/api/v1/publish",
        headers={"Authorization": f"Bearer {token}"},
        files={"bundle": ("bundle.tar.gz", captured.bundle, "application/gzip")},
        data={
            "signature": captured.data["signature"],
            "manifest_json": captured.data["manifest_json"],
        },
    )


@pytest.mark.asyncio
async def test_the_registry_accepts_what_the_cli_produces(registry, captured_publish):
    resp = await _replay(registry, captured_publish)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Acceptance-gated: submitted and private until a reviewer acts.
    assert body["status"] == "submitted"
    assert body["visibility"] == "private"


@pytest.mark.asyncio
async def test_the_published_item_resolves_by_name_and_installs(
    registry, captured_publish, tmp_path
):
    """Publish, approve, resolve by the name a person types, install it."""
    http, *_ = registry
    resp = await _replay(registry, captured_publish)
    assert resp.status_code == 200, resp.text
    item_id = resp.json()["id"]

    approve = await http.post(
        f"/api/v1/review/{item_id}/approve",
        json={},
        headers={
            "Authorization": f"Bearer {REVIEWER_SECRET}",
            "X-Reviewer-Actor": "round-trip",
        },
    )
    assert approve.status_code == 200, approve.text

    # The name from the manifest, not the UUID.
    detail = await http.get("/api/v1/item/iot_light", params={"kind": "skill"})
    assert detail.status_code == 200, detail.text
    record = detail.json()
    assert record["id"] == item_id
    assert record["name"] == "iot_light"
    assert record["version"] == "2.3.0"

    # And the bytes behind that name are the installable ones.
    blob = await http.get(f"/api/v1/blobs/{record['sha256']}")
    assert blob.status_code == 200
    assert blob.content == captured_publish.bundle

    import tarfile
    from io import BytesIO

    dest = tmp_path / "installed"
    with tarfile.open(fileobj=BytesIO(blob.content), mode="r:gz") as tar:
        tar.extractall(dest)

    from skills.package import SkillPackage

    pkg = SkillPackage(dest)
    assert pkg.load(), pkg.errors
    assert pkg.manifest is not None and pkg.manifest.skill_id == "iot_light"


@pytest.mark.asyncio
async def test_a_tampered_bundle_is_still_refused(registry, captured_publish):
    """The signature check is untouched by any of this."""
    captured_publish.bundle = captured_publish.bundle + b"trailing garbage"
    resp = await _replay(registry, captured_publish)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "signature verification failed"
